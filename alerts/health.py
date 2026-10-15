"""
Run health checks and the daily summary email.

Why this exists: four failures in this project were silent — Voyage rate
limits, truncated JSON, leads matched against themselves, and an exhausted
Anthropic credit balance. Each was logged, none was surfaced, and each was
found days or weeks later. Two rules fix that:

  1. HEALTH ALERT — after every run, any stage that received input and produced
     nothing (or mostly errors) sends an email the same day.
  2. DAILY SUMMARY — one email per day whether or not there are hot leads.
     If the summary does not arrive, the scan or the mail path is broken.
     Silence is no longer ambiguous.

Pure functions only; sending and de-duplication live in main.py.
"""
import html as _html
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:  # pragma: no cover
    LOCAL_TZ = timezone(timedelta(hours=3))

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://scout-web-0l5o.onrender.com")

# A stage is "failing" when at least this share of what it received errored.
STAGE1_ERROR_RATIO = 0.5
STAGE1_MIN_SAMPLE = 3
# Every eligible lead merged as a duplicate is suspicious only on a real batch.
DEDUP_ALL_MERGED_MIN = 5
# The same set of problems is not re-sent within this window.
HEALTH_REPEAT_HOURS = 6


def diagnose_api_error(errors) -> str:
    """Plain-Hebrew cause for the most common API failures, or ''."""
    text = " ".join(e for e in errors if e).lower()
    if "credit balance" in text or "billing" in text:
        return ("נגמר הקרדיט בחשבון Anthropic API. יש לטעון ב-"
                "console.anthropic.com/settings/billing (ולשקול Auto-reload).")
    if "authentication_error" in text or "x-api-key" in text or "invalid api key" in text:
        return "מפתח ה-API של Anthropic נדחה. יש לבדוק את ANTHROPIC_API_KEY ב-Render."
    if "rate_limit" in text or "error code: 429" in text:
        return "חריגה ממגבלת הקצב של Anthropic API (429)."
    if "overloaded" in text or "error code: 529" in text:
        return "שרתי Anthropic עמוסים (529) — בדרך כלל חולף מעצמו."
    if "not_found_error" in text and "model" in text:
        return "המודל המוגדר (CLAUDE_MODEL) לא נמצא — ייתכן שהוצא משימוש."
    return ""


def evaluate_run(stats: dict) -> list:
    """
    Problems in one run, as [{"code", "text"}]. Empty list means healthy.
    `code` is stable (used to de-duplicate repeat alerts); `text` is Hebrew.
    """
    issues = []
    sc, s1, dd = stats.get("scrape", {}), stats.get("stage1", {}), stats.get("dedup", {})
    s2, s3 = stats.get("stage2", {}), stats.get("stage3", {})
    errors = stats.get("errors", [])
    cause = diagnose_api_error(errors)

    if stats.get("crash"):
        issues.append({"code": "crash",
                       "text": f"הריצה קרסה באמצע: {stats['crash'][:300]}"})

    n_src = sc.get("sources", 0)
    failed = sc.get("failed_sources", [])
    if n_src and sc.get("found", 0) == 0:
        issues.append({"code": "scrape_empty",
                       "text": f"אף אחד מ-{n_src} המקורות לא החזיר פריטים."})
    elif n_src and len(failed) * 2 >= n_src:
        issues.append({"code": "scrape_failed",
                       "text": f"{len(failed)} מתוך {n_src} מקורות נכשלו: {', '.join(failed)}"})

    n1, e1 = s1.get("classified", 0), s1.get("error", 0)
    if n1 >= STAGE1_MIN_SAMPLE and e1 / n1 >= STAGE1_ERROR_RATIO:
        issues.append({"code": "stage1_errors",
                       "text": f"שלב 1 (סיווג) נכשל ב-{e1} מתוך {n1} פריטים."
                               + (f" סיבה: {cause}" if cause else "")})

    if dd.get("in", 0) >= DEDUP_ALL_MERGED_MIN and dd.get("unique", 0) == 0:
        issues.append({"code": "dedup_all",
                       "text": f"כל {dd['in']} הלידים שעברו את הסף סומנו ככפילויות — אף אחד לא הגיע לניתוח."})

    if s2.get("in", 0) and s2.get("ok", 0) == 0:
        issues.append({"code": "stage2_zero",
                       "text": f"שלב 2 (ניתוח משפטי) קיבל {s2['in']} לידים ולא הפיק אף ניתוח."
                               + (f" סיבה: {cause}" if cause else "")})

    if s3.get("in", 0) and s3.get("ok", 0) == 0:
        issues.append({"code": "stage3_zero",
                       "text": f"שלב 3 (ציון ושווי) קיבל {s3['in']} לידים ולא הפיק אף ציון."
                               + (f" סיבה: {cause}" if cause else "")})
    return issues


def issue_signature(issues) -> str:
    return "health:" + ",".join(sorted(i["code"] for i in issues))


def local_day(now_utc: datetime) -> str:
    return now_utc.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


# ── HTML ────────────────────────────────────────────────────────────────────

_WRAP = """<!DOCTYPE html><html dir="rtl" lang="he"><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:24px 12px;background:#F0F1F7;font-family:Arial,Helvetica,sans-serif;direction:rtl;">
<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td align="center">
<table cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;">
<tr><td style="padding:24px 28px;text-align:right;" dir="rtl">{body}
<p style="margin:24px 0 0;font-size:12px;color:#7B8099;">
<a href="{url}" style="color:#5E6AD2;">פתיחת הדשבורד</a> · Class Action Scout</p>
</td></tr></table></td></tr></table></body></html>"""


def _esc(x) -> str:
    # Scraped titles often arrive already HTML-encoded (&quot;); decode once
    # first so they are not shown as literal "&quot;" in the email.
    return _html.escape(_html.unescape(str(x if x is not None else "")))


def _issue_list(issues) -> str:
    items = "".join(f'<li style="margin-bottom:6px;" dir="rtl">{_esc(i["text"])}</li>' for i in issues)
    return f'<ul style="margin:8px 0 0;padding-right:18px;font-size:14px;color:#1A1D2E;">{items}</ul>'


def _counts_table(rows) -> str:
    tr = "".join(
        f'<tr><td style="padding:4px 0;font-size:14px;color:#4A5068;">{_esc(k)}</td>'
        f'<td style="padding:4px 0;font-size:14px;font-weight:700;color:#1A1D2E;text-align:left;">{_esc(v)}</td></tr>'
        for k, v in rows)
    return f'<table width="100%" cellpadding="0" cellspacing="0" border="0">{tr}</table>'


def run_counts_rows(stats: dict) -> list:
    sc, s1, dd = stats.get("scrape", {}), stats.get("stage1", {}), stats.get("dedup", {})
    s2, s3 = stats.get("stage2", {}), stats.get("stage3", {})
    return [
        ("פריטים שנמצאו במקורות", sc.get("found", 0)),
        ("חדשים", sc.get("new", 0)),
        ("סווגו (שלב 1)", s1.get("classified", 0)),
        ("עברו את הסף", s1.get("passed", 0)),
        ("שגיאות סיווג", s1.get("error", 0)),
        ("נכנסו לניתוח (שלב 2)", s2.get("in", 0)),
        ("נותחו בהצלחה", s2.get("ok", 0)),
        ("קיבלו ציון (שלב 3)", s3.get("ok", 0)),
    ]


def build_health_email(issues, stats, now_utc):
    stamp = now_utc.astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")
    body = (
        '<h2 style="margin:0 0 4px;font-size:20px;color:#B42318;">⚠ תקלה בריצת הסקאוט</h2>'
        f'<p style="margin:0 0 12px;font-size:13px;color:#7B8099;">{stamp}</p>'
        + _issue_list(issues)
        + '<h3 style="margin:20px 0 6px;font-size:15px;color:#1A1D2E;">מספרי הריצה</h3>'
        + _counts_table(run_counts_rows(stats))
    )
    subject = f"⚠ Scout — תקלה בריצה ({stamp})"
    return subject, _WRAP.format(body=body, url=DASHBOARD_URL)


def _lead_list(items, tag="ol") -> str:
    lis = "".join(
        f'<li style="margin-bottom:8px;"><b>עדיפות {_esc(t["score"])}</b>'
        + (f' · חוזק {_esc(t["strength"])}' if t.get("strength") is not None else '')
        + ' · '
        f'<bdi>{_esc(t["company"])}</bdi> — '
        + (f'<a href="{DASHBOARD_URL}/?lead={int(t["id"])}" style="color:#1A1D2E;"><bdi>{_esc(t["title"])}</bdi></a>'
           if t.get("id") else f'<bdi>{_esc(t["title"])}</bdi>')
        + (' <span style="color:#7B8099;">(לא ישים בישראל)</span>' if t.get("israel") is False else '')
        + '</li>'
        for t in items)
    return f'<{tag} style="margin:0;padding-right:18px;font-size:14px;color:#1A1D2E;">{lis}</{tag}>'


def build_digest_email(d: dict, issues, now_utc):
    """d comes from ClassActionScout._collect_digest."""
    date_str = now_utc.astimezone(LOCAL_TZ).strftime("%d/%m/%Y")
    status = ('<p style="margin:0 0 16px;font-size:14px;color:#067647;font-weight:700;">✓ המערכת תקינה</p>'
              if not issues else
              '<p style="margin:0 0 4px;font-size:14px;color:#B42318;font-weight:700;">⚠ יש תקלות:</p>'
              + _issue_list(issues) + '<div style="height:12px;"></div>')
    rows = [
        ("לידים חדשים (24 שעות)", d["new"]),
        ("סווגו", d["classified"]),
        ("עברו את הסף", d["passed"]),
        ("נותחו וקיבלו ציון", d["scored"]),
        (f"ציון {d['threshold']:g} ומעלה", d["hot"]),
        ("ממתינים לסיווג חוזר (שגיאה טכנית)", d["pending_retry"]),
    ]
    top = ""
    if d["top"]:
        top = ('<h3 style="margin:20px 0 6px;font-size:15px;color:#1A1D2E;">המובילים ב-24 השעות האחרונות</h3>'
               '<p style="margin:0 0 6px;font-size:12px;color:#7B8099;">עדיפות = הציון שקובע אם ליד הוא חם (סף '
               f'{d["threshold"]:g}); חוזק = הערכת חוזק הטענה המשפטית, כפי שמופיע במייל ההתראה.</p>'
               + _lead_list(d["top"], "ol"))
    else:
        top = '<p style="margin:16px 0 0;font-size:14px;color:#4A5068;">לא נותחו לידים חדשים ב-24 השעות האחרונות.</p>'
    near = ""
    if d.get("near"):
        near = (f'<h3 style="margin:20px 0 6px;font-size:15px;color:#1A1D2E;">קרובים לסף '
                f'({d.get("near_floor", 6.0):g} עד מתחת ל-{d["threshold"]:g})</h3>'
                '<p style="margin:0 0 6px;font-size:12px;color:#7B8099;">לא נשלחה עליהם התראה. כדאי להעיף מבט, '
                'ולסמן בדשבורד "ליד בשבילנו?" — זה משפר את המדידה.</p>'
                + _lead_list(d["near"], "ul"))
    body = (
        f'<h2 style="margin:0 0 12px;font-size:20px;color:#1A1D2E;">סיכום יומי — {date_str}</h2>'
        + status + _counts_table(rows) + top + near
    )
    hot = d["hot"]
    subject = (f"Scout — סיכום יומי {date_str}"
               + (f" · {hot} לידים חמים" if hot else "")
               + (" · ⚠ תקלה" if issues else ""))
    return subject, _WRAP.format(body=body, url=DASHBOARD_URL)
