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

# Confidence multiplier applied to the blended score after weighting.
CONF_MULTIPLIER = {
    "high":   1.0,
    "medium": 0.8,
    "low":    0.6,
}

# Log10 scale recalibrated to Israeli realistic ceiling:
#   ₪1M  → val_comp ≈ 3   (small case, below average)
#   ₪5M  → val_comp ≈ 5.4 (typical small-medium)
#   ₪20M → val_comp ≈ 7.4 (solid case)
#   ₪60M → val_comp ≈ 9   (top of realistic scale)
#   above ₪60M → clamped to 10
_VALUE_LOG_ANCHOR_LOW  = math.log10(1_000_000)     # 6.0
_VALUE_LOG_ANCHOR_HIGH = math.log10(60_000_000)    # ~7.778
_VALUE_SCORE_LOW  = 3.0
_VALUE_SCORE_HIGH = 9.0

# Israeli realistic ceiling — estimates above this are capped unless strongly justified
_IL_CEILING = 60_000_000

_PROMPT = """\
You are a legal analyst providing rough ORDER-OF-MAGNITUDE estimates for Israeli \
class action lead triage. These are for internal prioritisation ONLY — NOT legal \
opinions or financial advice.

Lead text:
{text}
{hint}

━━ STEP 1 — Israeli nexus check ━━
Does this lead have a genuine Israeli nexus? Answer true if ANY of:
  • Israeli consumers or residents are directly affected
  • Israeli law applies (Consumer Protection Law, Privacy Protection Law, Securities Law, etc.)
  • The company operates in Israel and the harm affected Israeli users

Answer false if this is primarily a US or other foreign case with no clear Israeli \
consumer or legal hook.

Stage-1 classification hints (use as input; apply independent judgment):
  operates_in_israel: {operates_in_israel}
  israeli_law_basis:  {israeli_law_basis}

━━ STEP 2 — Estimates (ONLY when israel_applicable = true) ━━

class_size_low / class_size_high — plausible range of affected ISRAELI class members.

damage_per_member_low / damage_per_member_high — actual harm-based NIS amount per member.
  Use ISRAELI NORMS. Israeli class actions have NO fixed statutory per-member amounts \
like US BIPA ($1,000–$5,000). NEVER use US statutory figures.
  Israeli benchmarks:
    Consumer overcharge / misleading pricing  →  50–300 NIS/member
    Privacy / data breach, no financial harm  →  100–500 NIS/member
    Privacy / data breach, financial harm or sensitive data  →  300–2,000 NIS/member
    Financial product mis-selling or hidden fees  →  500–5,000 NIS/member
    Employment / labour violations  →  1,000–10,000 NIS/member
  When uncertain, default to 100–300 NIS/member (typical Israeli consumer case).

Total value reality check:
  Israeli class action settlement ranges:
    Small   ≈ ₪1–5M  |  Average ≈ ₪5–20M  |  Large ≈ ₪20–60M
  Above ₪60M is a rare exception requiring strong concrete evidence in the text.
  Cap your estimates at ₪60M unless you can justify higher with specific facts.

confidence:
  "high"   — named Israeli statute with quantifiable damages AND concrete class size signal
  "medium" — identifiable Israeli cause of action AND class size roughly estimable
  "low"    — cause unclear, OR both class size AND damages require pure speculation

reasoning — 1–2 sentences: state the Israeli nexus (or why none) and how you derived \
the damage-per-member figure. If israel_applicable = false, say so plainly.

If israel_applicable = false: set all numeric fields to null.

Return ONLY valid JSON, no markdown, no surrounding text:
{{"israel_applicable":true,"class_size_low":N,"class_size_high":N,\
"damage_per_member_low":N,"damage_per_member_high":N,"confidence":"high|medium|low",\
"reasoning":"..."}}
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
    safe_text = text.replace("{", "{{").replace("}", "}}")
    safe_hint = hint.replace("{", "{{").replace("}", "}}")
    oii = str(lead.operates_in_israel) if lead.operates_in_israel is not None else "unknown"
    ilb = str(lead.israeli_law_basis or "unknown")
    prompt = _PROMPT.format(
        text=safe_text, hint=safe_hint,
        operates_in_israel=oii, israeli_law_basis=ilb,
    )

    israel_applicable = None
    value_high_for_score = None
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _parse_json(resp.content[0].text)

        israel_applicable = bool(data.get("israel_applicable", True))
        lead.israel_applicable = israel_applicable

        confidence = str(data.get("confidence", "low")).lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        reasoning = str(data.get("reasoning", ""))[:500]
        lead.value_confidence = confidence
        lead.value_reasoning  = reasoning

        if israel_applicable:
            cs_low  = _f(data.get("class_size_low"))
            cs_high = _f(data.get("class_size_high"))
            dm_low  = _f(data.get("damage_per_member_low"))
            dm_high = _f(data.get("damage_per_member_high"))

            v_low  = cs_low  * dm_low
            v_high = cs_high * dm_high

            # Enforce Israeli realistic ceiling
            if v_high > _IL_CEILING:
                v_high = _IL_CEILING
                if v_low > _IL_CEILING:
                    v_low = _IL_CEILING * 0.3
            if v_low > _IL_CEILING:
                v_low = _IL_CEILING

            # Wide uncertainty range → downgrade confidence
            if v_high > 0 and v_low > 0 and v_high / v_low > 5:
                confidence = _downgrade_conf(confidence)
                lead.value_confidence = confidence

            lead.est_class_size        = int((cs_low + cs_high) / 2) if cs_high > 0 else None
            lead.est_damage_per_member = (dm_low + dm_high) / 2      if dm_high > 0 else None
            value_high_for_score       = v_high if v_high > 0 else None

            # Store values only when display conditions are met (IL=True AND conf>=medium)
            if confidence in ("high", "medium") and v_high > 0:
                lead.value_low  = v_low if v_low > 0 else None
                lead.value_high = v_high
            else:
                lead.value_low  = None
                lead.value_high = None

            logger.info(
                "  [value] IL=True ₪%s–₪%s (%s) — %s",
                _fmt(v_low), _fmt(v_high), confidence, lead.title[:50],
            )
        else:
            lead.value_low             = None
            lead.value_high            = None
            lead.est_class_size        = None
            lead.est_damage_per_member = None
            logger.info("  [value] IL=False (no Israeli nexus) — %s", lead.title[:50])

    except Exception as e:
        logger.error("estimate_value AI call failed for lead %s: %s", getattr(lead, "id", "?"), e)

    lead.priority_score = _compute_priority_score(lead, value_high_for_score, israel_applicable)


def _compute_priority_score(lead, value_high, israel_applicable) -> float:
    """Return a 1-10 composite score blending value, cert probability, expertise."""
    # Component 1: value on recalibrated log10 scale (₪1M→3, ₪60M→9)
    if value_high and value_high > 0:
        log_v = math.log10(max(float(value_high), 1.0))
        val_comp = (
            _VALUE_SCORE_LOW
            + (log_v - _VALUE_LOG_ANCHOR_LOW)
            / (_VALUE_LOG_ANCHOR_HIGH - _VALUE_LOG_ANCHOR_LOW)
            * (_VALUE_SCORE_HIGH - _VALUE_SCORE_LOW)
        )
        val_comp = max(0.0, min(10.0, val_comp))
    else:
        val_comp = 0.0

    # Component 2: certification probability (strength_score already 0-10)
    cert_comp = max(0.0, min(10.0, float(lead.strength_score or 0)))

    # Component 3: expertise fit
    exp_comp = 10.0 if lead.matches_expertise else 3.0

    blend = (
        WEIGHT_VALUE         * val_comp
        + WEIGHT_CERTIFICATION * cert_comp
        + WEIGHT_EXPERTISE     * exp_comp
    )

    confidence = getattr(lead, "value_confidence", None) or "low"
    mult = CONF_MULTIPLIER.get(confidence, CONF_MULTIPLIER["low"])
    score = max(1.0, min(10.0, round(blend * mult, 1)))

    # Non-Israeli leads are suppressed — they are not real opportunities for this firm
    if israel_applicable is False:
        score = max(1.0, round(score * 0.3, 1))

    return score


def nis_bucket(v_low, v_high):
    """
    Return a display bucket label for a value range, or None if no displayable value.
    Uses the midpoint of the range to select the bucket.
    """
    if not v_high and not v_low:
        return None
    hi = v_high or v_low or 0
    lo = v_low or 0
    mid = (lo + hi) / 2
    if mid <= 0:
        return None
    if mid < 5_000_000:
        return "₪1-5M"
    if mid < 20_000_000:
        return "₪5-20M"
    if mid < 60_000_000:
        return "₪20-60M"
    return "₪60M+ (חריג)"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _downgrade_conf(conf: str) -> str:
    return {"high": "medium", "medium": "low", "low": "low"}[conf]


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
