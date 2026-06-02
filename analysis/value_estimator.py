"""
Value estimation and composite priority scoring for class action leads.

All monetary figures are ROUGH ORDER-OF-MAGNITUDE estimates for internal
triage only — NOT legal opinions or financial advice.
"""
import json
import re
import math
import logging

logger = logging.getLogger(__name__)

# ── Scoring weights (must sum to 1.0) ─────────────────────────────────────────
WEIGHT_VALUE         = 1 / 3   # monetary value component
WEIGHT_CERTIFICATION = 1 / 3   # case strength / cert probability
WEIGHT_EXPERTISE     = 1 / 3   # firm expertise fit

# Log10 scale anchors for value component: ₪100K → 0, ₪100M → 10
_LOG_MIN = math.log10(100_000)       # 5.0
_LOG_MAX = math.log10(100_000_000)   # 8.0

_PROMPT = """\
You are a legal analyst providing rough ORDER-OF-MAGNITUDE monetary estimates \
for class action lead triage. These are for internal prioritisation only and \
are NOT legal opinions or financial advice.

Lead text:
{text}
{hint}

Estimate:
- class_size_low / class_size_high: plausible range of class members (integers)
- damage_per_member_low / damage_per_member_high: plausible average per-member \
damage in Israeli Shekels (NIS). For US/international cases, estimate only the \
Israeli-consumer portion.
- confidence: "high" (solid evidence), "medium" (partial evidence), or "low" \
(speculative — missing data → widen range and use "low")
- reasoning: 1-2 sentence explanation in plain language

Rules:
- If data is missing, WIDEN the range and lower confidence — never guess precise numbers
- Return ONLY valid JSON, no markdown, no surrounding text

{{"class_size_low":N,"class_size_high":N,"damage_per_member_low":N,\
"damage_per_member_high":N,"confidence":"high|medium|low","reasoning":"..."}}
"""


def estimate_value(lead, client, model: str) -> None:
    """
    Estimate monetary value range and compute composite priority_score.
    Writes all results directly onto the lead object; caller must commit.
    """
    text = f"{lead.title}\n\n{lead.raw_content or ''}"[:4000]
    hint = (
        f"\nHint — Stage-1 estimated_class_size: {lead.estimated_class_size}"
        if lead.estimated_class_size else ""
    )
    prompt = _PROMPT.format(text=text, hint=hint)

    value_high_for_score = None
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _parse_json(resp.content[0].text)

        cs_low  = _f(data.get("class_size_low"))
        cs_high = _f(data.get("class_size_high"))
        dm_low  = _f(data.get("damage_per_member_low"))
        dm_high = _f(data.get("damage_per_member_high"))

        confidence = str(data.get("confidence", "low")).lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        reasoning = str(data.get("reasoning", ""))[:500]

        v_low  = cs_low  * dm_low
        v_high = cs_high * dm_high

        lead.value_low             = v_low  if v_low  > 0 else None
        lead.value_high            = v_high if v_high > 0 else None
        lead.est_class_size        = int((cs_low + cs_high) / 2) if cs_high > 0 else None
        lead.est_damage_per_member = (dm_low + dm_high) / 2     if dm_high > 0 else None
        lead.value_confidence      = confidence
        lead.value_reasoning       = reasoning
        value_high_for_score       = v_high if v_high > 0 else None

        logger.info(
            "  [value] ₪%s–₪%s (%s) — %s",
            _fmt(v_low), _fmt(v_high), confidence, lead.title[:50],
        )

    except Exception as e:
        logger.error("estimate_value AI call failed for lead %s: %s", getattr(lead, "id", "?"), e)

    lead.priority_score = _compute_priority_score(lead, value_high_for_score)


def _compute_priority_score(lead, value_high) -> float:
    """Return a 1-10 composite score blending value, cert probability, expertise."""
    # Component 1: value on log10 scale
    if value_high and value_high > 0:
        log_v    = math.log10(max(float(value_high), 1.0))
        val_comp = (log_v - _LOG_MIN) / (_LOG_MAX - _LOG_MIN) * 10.0
        val_comp = max(0.0, min(10.0, val_comp))
    else:
        val_comp = 0.0

    # Component 2: certification probability (strength_score already 0-10)
    cert_comp = max(0.0, min(10.0, float(lead.strength_score or 0)))

    # Component 3: expertise fit
    exp_comp = 10.0 if lead.matches_expertise else 3.0

    score = (
        WEIGHT_VALUE         * val_comp
        + WEIGHT_CERTIFICATION * cert_comp
        + WEIGHT_EXPERTISE     * exp_comp
    )
    return max(1.0, min(10.0, round(score, 1)))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _f(val) -> float:
    try:
        return max(0.0, float(val or 0))
    except (TypeError, ValueError):
        return 0.0


def _fmt(v) -> str:
    if not v or v <= 0:
        return "0"
    if v >= 1e9:
        return f"{v/1e9:.1f}B"
    if v >= 1e6:
        return f"{v/1e6:.1f}M"
    if v >= 1e3:
        return f"{v/1e3:.0f}K"
    return f"{v:.0f}"


def _parse_json(text: str) -> dict:
    text = text.strip()
    if "```" in text:
        text = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    pass
                start = None
    return {}
