"""
Claude AI Analyzer for Class Action Scout
==========================================
Two-stage analysis:
  Stage 1 — Quick classification & Israel relevance scoring
  Stage 2 — Deep legal analysis (only for high-relevance items)
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


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt template not found: {path}")


class ClaudeAnalyzer:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    # ── Stage 1: Classification ────────────────────────

    def classify(self, title: str, content: str, source_type: str) -> dict:
        prompt = _load_prompt("classify")
        user_msg = prompt.format(
            title=title,
            content=content[:3000],
            source_type=source_type,
        )

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=1500,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = resp.content[0].text
            result = self._parse_json(text)
            # Ensure relevance_score is a number
            result["relevance_score"] = self._to_float(result.get("relevance_score", 0))
            logger.debug(f"Classification result: {result.get('relevance_score')} - {result.get('company')}")
            return result
        except Exception as e:
            logger.error(f"Classification failed: {e}")
            return {"relevance_score": 0, "error": str(e)}

    # ── Stage 2: Deep legal analysis ───────────────────

    def analyze(self, title: str, content: str, classification: dict) -> dict:
        prompt = _load_prompt("legal_analysis")
        user_msg = prompt.format(
            title=title,
            content=content[:5000],
            classification=json.dumps(classification, ensure_ascii=False),
        )

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=3000,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = resp.content[0].text
            logger.debug(f"Deep analysis raw response length: {len(text)}")
            result = self._parse_json(text)

            # Ensure critical fields exist and are correct types
            result["strength_score"] = self._to_float(result.get("strength_score", 0))
            if result["strength_score"] == 0 and "raw_response" not in result:
                # Try to extract score from text if JSON parse partially failed
                score_match = re.search(r'"strength_score"\s*:\s*(\d+(?:\.\d+)?)', text)
                if score_match:
                    result["strength_score"] = float(score_match.group(1))

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
        except Exception as e:
            logger.error(f"Deep analysis failed for '{title[:50]}': {e}")
            return {"strength_score": 0, "priority": "low", "error": str(e)}

    # ── Pattern detection (weekly) ─────────────────────

    def detect_patterns(self, complaints: list[dict]) -> dict:
        prompt = _load_prompt("pattern")
        user_msg = prompt.format(
            complaints=json.dumps(complaints[:50], ensure_ascii=False)
        )

        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                messages=[{"role": "user", "content": user_msg}],
            )
            return self._parse_json(resp.content[0].text)
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
    def _parse_json(text: str) -> dict:
        """Extract JSON from Claude's response — robust parser"""
        text = text.strip()

        # Remove markdown code fences
        if "```" in text:
            text = re.sub(r"```(?:json)?\s*", "", text)
            text = text.replace("```", "").strip()

        # Try direct parse first
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

        # Last resort: try to extract key fields with regex
        logger.warning(f"JSON parse failed, attempting regex extraction from: {text[:200]}")
        result = {"raw_response": text[:500]}

        # Extract common numeric fields
        for field in ["relevance_score", "strength_score", "certification_probability"]:
            match = re.search(rf'"{field}"\s*:\s*(\d+(?:\.\d+)?)', text)
            if match:
                result[field] = float(match.group(1))

        # Extract string fields
        for field in ["company", "priority", "sector", "israeli_law_basis"]:
            match = re.search(rf'"{field}"\s*:\s*"([^"]*)"', text)
            if match:
                result[field] = match.group(1)

        return result
