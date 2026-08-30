"""
Claude AI Analyzer for Class Action Scout
==========================================
Two-stage analysis:
  Stage 1 — Quick classification & Israel relevance scoring
  Stage 2 — Deep legal analysis (only for high-relevance items)

Both stages ask for JSON through structured outputs (output_config / json_schema),
so a well-formed response is guaranteed by the API rather than reconstructed from
prose. Two failure modes are handled explicitly instead of silently:

  * Truncation — a response that stops at max_tokens is cut mid-JSON. It is
    retried once with a larger cap, then raised. Hebrew legal analysis runs well
    past 3000 output tokens, which is what made this the normal path.
  * Unparseable output — logged at ERROR with the response tail, and raised.

Deep analysis raises AnalysisError rather than returning a zeroed result. A lead
skipped with a loud error keeps NULL fields and can be re-analysed later; a lead
written with strength_score=0 looks like a real verdict and is never revisited.
"""
import sys
import os

# Fix encoding before importing anthropic/httpx
os.environ["PYTHONUTF8"] = "1"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import json
import re
import logging
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

# Load prompt templates
PROMPTS_DIR = Path(__file__).parent / "prompts"

# ── Output token ceilings ─────────────────────────────────────────────────────
# Measured on real leads: a full Hebrew legal analysis runs ~3400 output tokens,
# so the previous 3000 cap truncated routinely. You are billed for tokens
# generated, not for the ceiling, so headroom here costs nothing.
MAX_TOKENS_CLASSIFY = 2000
MAX_TOKENS_ANALYZE  = 8000
MAX_TOKENS_PATTERN  = 4000


class AnalysisError(RuntimeError):
    """
    Raised when a stage cannot produce a trustworthy result (truncated response,
    unparseable JSON, API failure). Callers must skip the lead — never write a
    default score, which is indistinguishable from a real low verdict.
    """


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "relevance_score": {"type": "number"},
        "company": {"type": "string"},
        "sector": {"type": "string"},
        "reasoning": {"type": "string"},
        "operates_in_israel": {"type": "boolean"},
        "israeli_law_basis": {"type": "string"},
        "estimated_class_size": {"type": "string"},
    },
    "required": ["relevance_score", "company", "reasoning"],
    "additionalProperties": False,
}

_ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "strength_score": {"type": "number"},
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "legal_analysis": {"type": "string"},
        "applicable_statutes": {"type": "array", "items": {"type": "string"}},
        "certification_probability": {"type": "string"},
        "evidence_available": {"type": "string"},
        "evidence_needed": {"type": "string"},
        "expert_opinion_needed": {"type": "string"},
        "comparable_cases": {"type": "array", "items": {"type": "string"}},
        "recommended_action": {"type": "string"},
        "estimated_damages": {"type": "string"},
        "risks": {"type": "string"},
        "already_filed_il": {"type": "boolean"},
        "already_filed_details": {"type": "string"},
    },
    "required": [
        "strength_score", "priority", "legal_analysis",
        "recommended_action", "already_filed_il", "already_filed_details",
    ],
    "additionalProperties": False,
}


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt template not found: {path}")


class ClaudeAnalyzer:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    # ── Request helper ─────────────────────────────────

    def _request_json(self, user_msg: str, max_tokens: int, schema: dict,
                      label: str, required: tuple = ()) -> dict:
        """
        One JSON request with a schema, a truncation retry, and no silent
        degradation. Raises AnalysisError if a trustworthy result is impossible.
        """
        caps = (max_tokens, max_tokens * 2)
        for attempt, cap in enumerate(caps, start=1):
            try:
                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=cap,
                    messages=[{"role": "user", "content": user_msg}],
                    output_config={"format": {"type": "json_schema", "schema": schema}},
                )
            except Exception as e:
                raise AnalysisError(f"{label}: API call failed: {e}") from e

            if resp.stop_reason == "max_tokens":
                logger.error(
                    "%s: response TRUNCATED at max_tokens=%s (attempt %s/%s) — "
                    "the JSON is cut mid-object and cannot be trusted",
                    label, cap, attempt, len(caps),
                )
                continue

            text = next((b.text for b in resp.content if b.type == "text"), "")
            return self._parse_json(text, label=label, required=required)

        raise AnalysisError(
            f"{label}: response still truncated at max_tokens={caps[-1]} — skipped"
        )

    # ── Stage 1: Classification ────────────────────────

    def classify(self, title: str, content: str, source_type: str) -> dict:
        prompt = _load_prompt("classify")
        user_msg = prompt.format(
            title=title,
            content=content[:3000],
            source_type=source_type,
        )

        try:
            result = self._request_json(
                user_msg, MAX_TOKENS_CLASSIFY, _CLASSIFY_SCHEMA,
                label=f"classify '{title[:50]}'", required=("relevance_score",),
            )
            result["relevance_score"] = self._to_float(result.get("relevance_score", 0))
            logger.debug(f"Classification result: {result.get('relevance_score')} - {result.get('company')}")
            return result
        except AnalysisError as e:
            # Stage 1 keeps its contract (score 0 skips deep analysis) but the
            # failure is now loud rather than a silent zero.
            logger.error(f"Classification failed — item skipped: {e}")
            return {"relevance_score": 0, "error": str(e)}

    # ── Stage 2: Deep legal analysis ───────────────────

    def analyze(self, title: str, content: str, classification: dict) -> dict:
        """
        Raises AnalysisError on truncation, unparseable JSON, or API failure.
        Callers must skip the lead rather than persist a default score.
        """
        prompt = _load_prompt("legal_analysis")
        user_msg = prompt.format(
            title=title,
            content=content[:5000],
            classification=json.dumps(classification, ensure_ascii=False),
        )

        result = self._request_json(
            user_msg, MAX_TOKENS_ANALYZE, _ANALYZE_SCHEMA,
            label=f"deep analysis '{title[:50]}'",
            required=("strength_score", "priority"),
        )

        result["strength_score"] = self._to_float(result.get("strength_score", 0))

        # Normalize priority
        priority = str(result.get("priority", "low")).lower().strip()
        if priority not in ("high", "medium", "low"):
            if result["strength_score"] >= 7:
                priority = "high"
            elif result["strength_score"] >= 4:
                priority = "medium"
            else:
                priority = "low"
        result["priority"] = priority

        logger.info(f"  Deep analysis: strength={result['strength_score']}, priority={result['priority']}")
        return result

    # ── Pattern detection (weekly) ─────────────────────

    def detect_patterns(self, complaints: list[dict]) -> dict:
        prompt = _load_prompt("pattern")
        user_msg = prompt.format(
            complaints=json.dumps(complaints[:50], ensure_ascii=False)
        )

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS_PATTERN,
                messages=[{"role": "user", "content": user_msg}],
            )
            if resp.stop_reason == "max_tokens":
                logger.error("Pattern detection: response TRUNCATED at max_tokens=%s",
                             MAX_TOKENS_PATTERN)
                return {"patterns": [], "error": "truncated"}
            text = next((b.text for b in resp.content if b.type == "text"), "")
            return self._parse_json(text, label="pattern detection")
        except Exception as e:
            logger.error(f"Pattern detection failed: {e}")
            return {"patterns": [], "error": str(e)}

    # ── Helpers ────────────────────────────────────────

    @staticmethod
    def _to_float(val) -> float:
        """Safely convert any value to float"""
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _parse_json(text: str, label: str = "response", required: tuple = ()) -> dict:
        """
        Extract JSON from Claude's response.

        Strict parse, then outermost-object brace matching, then — as a last
        resort that logs at ERROR — regex field extraction. The regex result is
        accepted ONLY if it carries every field in `required`; otherwise this
        raises, because a half-populated dict becomes a wrong score on a lead
        that nothing will ever revisit.
        """
        text = text.strip()

        # Remove markdown code fences
        if "```" in text:
            text = re.sub(r"```(?:json)?\s*", "", text)
            text = text.replace("```", "").strip()

        # Try direct parse first — the normal path under structured outputs
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Find the outermost JSON object by matching braces
        depth = 0
        start = None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        pass
                    start = None

        # Last resort: regex extraction. Loud — this is not a normal path.
        logger.error(
            "%s: JSON PARSE FAILED (%d chars) — falling back to regex extraction. "
            "Response tail: %s",
            label, len(text), text[-300:] if text else "<empty>",
        )
        result = {"raw_response": text[:500], "parse_degraded": True}

        for field in ["relevance_score", "strength_score", "certification_probability"]:
            match = re.search(rf'"{field}"\s*:\s*(\d+(?:\.\d+)?)', text)
            if match:
                result[field] = float(match.group(1))

        for field in ["company", "priority", "sector", "israeli_law_basis"]:
            match = re.search(rf'"{field}"\s*:\s*"([^"]*)"', text)
            if match:
                result[field] = match.group(1)

        missing = [f for f in required if f not in result]
        if missing:
            raise AnalysisError(
                f"{label}: unparseable JSON and regex could not recover {missing} — skipped"
            )
        logger.error(
            "%s: proceeding on REGEX-EXTRACTED fields only — other fields are lost",
            label,
        )
        return result
