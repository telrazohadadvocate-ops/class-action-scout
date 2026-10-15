"""
Ask Scout — questions about one lead, answered by Claude with web search.

The judgement lives in prompts/ask_scout.txt. This module only builds the
lead's context, calls the API, collects the answer text and its sources, and
prices the call so a monthly budget can be enforced.
"""
import json
import logging

from analysis.claude_analyzer import _load_prompt

logger = logging.getLogger("scout")

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 4,
                   "user_location": {"type": "approximate", "country": "IL", "timezone": "Asia/Jerusalem"}}
MAX_TOKENS_ASK = 2500
MAX_CONTINUATIONS = 3                 # a long server-tool turn can come back as pause_turn

# USD prices for cost tracking (Claude Sonnet 4.6; web search $10 / 1,000).
# Only used to stop at the monthly budget — not billing.
PRICE_IN_PER_TOKEN = 3 / 1_000_000
PRICE_OUT_PER_TOKEN = 15 / 1_000_000
PRICE_PER_SEARCH = 10 / 1000

FIELD_CHECK_QUESTION = (
    "בצע בדיקת שטח לליד הזה: נמכר/בשימוש בישראל ומי היבואן, האם כבר הוגשה ייצוגית דומה בישראל, "
    "האם התביעה או ההסדר בחו\"ל כוללים או מחריגים חברי קבוצה מחוץ לארה\"ב, ומה הקבוצה והעילה בישראל. "
    "סיים בשורת הכרעה."
)


def lead_context(lead, related_titles=()) -> str:
    def cut(x, n):
        x = (x or "").strip()
        return x if len(x) <= n else x[: n - 1] + "…"
    parts = [
        f"Title: {lead.title}",
        f"Company: {lead.company or 'unknown'} · Sector: {lead.sector or 'unknown'}",
        f"Source: {lead.source_name or ''} · URL: {lead.source_url or ''}",
        f"Found: {lead.scraped_at:%Y-%m-%d}" if lead.scraped_at else "",
        f"Scores: relevance {lead.relevance_score}, strength {lead.strength_score}, priority {lead.priority_score}",
        f"Already filed in Israel (bot's reading): {lead.already_filed_il} {cut(lead.already_filed_details, 300)}",
        f"Stage-1 reasoning: {cut(lead.relevance_reasoning, 800)}",
        f"Legal analysis: {cut(lead.legal_analysis, 3000)}",
        f"Recommended action: {cut(lead.recommended_action, 600)}",
        f"Score reasoning: {cut(lead.score_reasoning, 1200)}",
        "Other articles on the same case: " + ("; ".join(cut(t, 140) for t in related_titles) or "none"),
        f"Article text: {cut(lead.raw_content, 4000)}",
    ]
    return "\n".join(p for p in parts if p)


def firm_cases_text(known_cases) -> str:
    return "\n".join(f"- {k['name']} ({k.get('status') or ''}{', ' + k['case'] if k.get('case') else ''})"
                     for k in known_cases) or "- none listed"


def _usage_cost(usage) -> float:
    if usage is None:
        return 0.0
    tin = getattr(usage, "input_tokens", 0) or 0
    tin += getattr(usage, "cache_read_input_tokens", 0) or 0
    tin += getattr(usage, "cache_creation_input_tokens", 0) or 0
    tout = getattr(usage, "output_tokens", 0) or 0
    stu = getattr(usage, "server_tool_use", None)
    searches = (getattr(stu, "web_search_requests", 0) or 0) if stu is not None else 0
    return tin * PRICE_IN_PER_TOKEN + tout * PRICE_OUT_PER_TOKEN + searches * PRICE_PER_SEARCH


def _collect(content, texts, sources):
    for b in content or []:
        if getattr(b, "type", "") == "text":
            texts.append(b.text or "")
            for c in getattr(b, "citations", None) or []:
                url = getattr(c, "url", None)
                if url and url not in {s["url"] for s in sources}:
                    sources.append({"url": url, "title": getattr(c, "title", "") or url})


def answer(client, model, system_prompt: str, history, question: str) -> dict:
    """
    history: [{"role": "user"|"assistant", "content": str}, …] — earlier turns, oldest first.
    Returns {"text", "sources", "cost_usd"}; raises on an API failure (the caller records it).
    """
    messages = [{"role": m["role"], "content": m["content"]} for m in history if m.get("content")]
    messages.append({"role": "user", "content": question})
    texts, sources, cost = [], [], 0.0
    for _ in range(MAX_CONTINUATIONS + 1):
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS_ASK, system=system_prompt,
            messages=messages, tools=[WEB_SEARCH_TOOL],
        )
        cost += _usage_cost(getattr(resp, "usage", None))
        _collect(resp.content, texts, sources)
        if getattr(resp, "stop_reason", "") != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    text = "".join(texts).strip() or "לא התקבלה תשובה. נסה לנסח את השאלה מחדש."
    return {"text": text, "sources": sources[:8], "cost_usd": round(cost, 4)}


def build_system_prompt(lead, related_titles, known_cases) -> str:
    return _load_prompt("ask_scout").format(
        firm_cases=firm_cases_text(known_cases),
        lead_context=lead_context(lead, related_titles),
    )


def dumps_sources(sources) -> str:
    return json.dumps(sources or [], ensure_ascii=False)
