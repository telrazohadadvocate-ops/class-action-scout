"""
Class Action Scout — Configuration
===================================
All settings in one place. Create a .env file from .env.example for credentials.
"""
import os
from pathlib import Path

# ── Load .env file if present ──────────────────────────
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# ── Paths ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent

# On Render, use the persistent disk; locally use data/ in repo root.
_ON_RENDER = bool(os.getenv("RENDER"))
DATA_DIR = Path("/var/data") if _ON_RENDER else BASE_DIR / "data"

DATABASE_PATH = DATA_DIR / "scout.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"
LOG_DIR = BASE_DIR / "logs"

# Persistent-disk paths for credentials that must survive redeploys.
# OUTLOOK_TOKEN_PATH is overridable via env var; the default lives on the Render
# persistent disk (/var/data) so the OAuth token survives deploys. Setting the
# env var lets you relocate it without a code change.
PACER_COOKIES_PATH = DATA_DIR / "pacer_cookies.json"
OUTLOOK_TOKEN_PATH = Path(os.getenv("OUTLOOK_TOKEN_PATH", str(DATA_DIR / "outlook_token.json")))

# ── Web Dashboard Auth ─────────────────────────────────
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

import secrets as _secrets
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or _secrets.token_hex(32)

# ── Semantic deduplication ─────────────────────────────
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
DEDUP_THRESHOLD = float(os.getenv("DEDUP_THRESHOLD", "0.85"))

# ── API Keys (set via env vars or .env file) ───────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# ── PacerMonitor ───────────────────────────────────────
PACER_USERNAME = os.getenv("PACER_USERNAME", "")
PACER_PASSWORD = os.getenv("PACER_PASSWORD", "")

# ── Gmail (legacy — superseded by Outlook/Graph) ──────
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# ── Outlook / Microsoft Graph (Law360 newsletters) ────
OUTLOOK_CLIENT_ID = os.getenv("OUTLOOK_CLIENT_ID", "")
OUTLOOK_TENANT_ID = os.getenv("OUTLOOK_TENANT_ID", "common")
OUTLOOK_USER_EMAIL = os.getenv("OUTLOOK_USER_EMAIL", "Ohad@levin-telraz.co.il")

# ── Scoring thresholds ─────────────────────────────────
MIN_RELEVANCE_SCORE = 4        # 1-10; below this → skip deep analysis
HIGH_PRIORITY_THRESHOLD = 7    # above this → immediate alert
SCRAPE_DELAY_SECONDS = 3.0     # polite delay between HTTP requests

# ── Scheduling ─────────────────────────────────────────
DAILY_RUN_HOUR = 6             # 06:00 local time
WEEKLY_REPORT_DAY = "sunday"
WEEKLY_REPORT_HOUR = 8

# ── Email alerts (via Microsoft Graph — reuses the Outlook delegated token) ──
# Recipient defaults to the authenticated mailbox (OUTLOOK_USER_EMAIL) when unset.
ALERT_RECIPIENT = os.getenv("ALERT_RECIPIENT", "")

# ── Sources ────────────────────────────────────────────
SOURCES = {
    # ═══ INTERNATIONAL — Class Action News ═══
    "classaction_org": {
        "enabled": True,
        "url": "https://www.classaction.org/news",
        "type": "international",
        "desc": "US class action filings & settlements",
    },
    "topclassactions": {
        "enabled": True,
        "url": "https://topclassactions.com/category/lawsuit-settlements/open-lawsuit-settlements/",
        "type": "international",
        "desc": "Open US settlements & lawsuits",
    },
    "law360_classaction": {
        "enabled": True,
        "url": "https://www.law360.com/classaction",
        "type": "international",
        "desc": "LexisNexis Law360 — Class Action section (requires login for full articles)",
        "notes": "Free headlines + summaries; full text needs subscription",
    },
    "law360_consumer": {
        "enabled": True,
        "url": "https://www.law360.com/consumerprotection",
        "type": "international",
        "desc": "LexisNexis Law360 — Consumer Protection section",
    },

    # ═══ INTERNATIONAL — Regulatory / Government ═══
    "ftc_enforcement": {
        "enabled": True,
        "url": "https://www.ftc.gov/news-events/news/press-releases",
        "type": "regulatory",
        "desc": "FTC press releases — enforcement actions, consent orders",
    },
    "cfpb_enforcement": {
        "enabled": True,
        "url": "https://www.consumerfinance.gov/enforcement/actions/",
        "type": "regulatory",
        "desc": "CFPB enforcement actions — banking, fintech, lending",
    },
    "uk_cat": {
        "enabled": True,
        "url": "https://www.catribunal.org.uk/cases",
        "type": "international",
        "desc": "UK Competition Appeal Tribunal — class action/collective proceedings",
        "notes": "Key for tech antitrust (Google, Apple, Meta cases)",
    },

    # ═══ ISRAELI NEWS ═══
    "themarker": {
        "enabled": True,
        "url": "https://www.themarker.com/law",
        "type": "local",
        "desc": "TheMarker — Israeli business/legal news",
    },
    "globes": {
        "enabled": True,
        "url": "https://www.globes.co.il/news/tag/%D7%AA%D7%95%D7%91%D7%A2%D7%A0%D7%95%D7%AA%20%D7%99%D7%99%D7%A6%D7%95%D7%92%D7%99%D7%95%D7%AA",
        "type": "local",
        "desc": "Globes — class action tagged articles",
    },
    "calcalist": {
        "enabled": True,
        "url": "https://www.calcalist.co.il/tags/תובענות-ייצוגיות",
        "type": "local",
        "desc": "Calcalist — class action tagged articles",
    },
    "ynet_consumer": {
        "enabled": True,
        "url": "https://www.ynet.co.il/tags/צרכנות",
        "type": "local",
        "desc": "Ynet — consumer complaints and news",
    },

    # ═══ ISRAELI FORUMS & CONSUMER COMPLAINTS ═══
    "fxp_consumer": {
        "enabled": True,
        "url": "https://www.fxp.co.il/forumdisplay.php?f=5765",
        "type": "local_forum",
        "desc": "FXP Consumer Forum — grassroots complaints",
        "notes": "Requires Playwright for JS-rendered content",
    },
    "trustpilot_il": {
        "enabled": False,  # Cloudflare bot-protection returns 403 even with browser headers; needs Playwright to bypass — low-signal source, not worth the overhead
        "url": "https://www.trustpilot.com/categories",  # il.trustpilot.com 301s here
        "type": "local_forum",
        "desc": "Trustpilot Israel — consumer reviews & complaints",
    },

    # ═══ ISRAELI REGULATORS ═══
    # consumer_protection_il, competition_authority_il, telecom_authority_il are
    # disabled: gov.il returns 403 to Render's US-based datacenter IPs (geo-block).
    # Re-enable via a residential/Israeli proxy, or replace with Google Alerts RSS feeds.
    "consumer_protection_il": {
        "enabled": False,  # geo-blocked on US servers (403)
        "url": "https://www.gov.il/he/departments/consumer-protection-and-fair-trade-authority/govil-landing-page",
        "type": "regulatory_il",
        "desc": "רשות הגנת הצרכן — enforcement, warnings, recalls",
    },
    "competition_authority_il": {
        "enabled": False,  # geo-blocked on US servers (403)
        "url": "https://www.gov.il/he/departments/competition_authority/govil-landing-page",
        "type": "regulatory_il",
        "desc": "רשות התחרות — decisions, exemptions, enforcement",
    },
    "capital_markets_il": {
        "enabled": True,
        "url": "https://www.isa.gov.il/sites/ISAEng/Pages/default.aspx",
        "type": "regulatory_il",
        "desc": "רשות שוק ההון — enforcement actions, investor alerts",
    },
    "telecom_authority_il": {
        "enabled": False,  # geo-blocked on US servers (403)
        "url": "https://www.gov.il/he/departments/ministry_of_communication",
        "type": "regulatory_il",
        "desc": "משרד התקשורת — telecom enforcement, consumer protection",
    },

    # ═══ OUTLOOK — Law360 newsletters ═══
    "outlook_law360": {
        "enabled": True,
        "url": "",  # email-based; no HTTP URL needed
        "type": "newsletter",
        "desc": "Law360 newsletters delivered to Ohad@levin-telraz.co.il (via Microsoft Graph)",
        "notes": "Run scripts/setup_outlook.py once to authenticate before enabling",
    },

    # ═══ GOOGLE ALERTS (RSS feeds) ═══
    "google_alerts_classaction_il": {
        "enabled": True,
        "url": "https://www.google.com/alerts/feeds/16238509347590318752/16797255801742262455",
        "type": "alerts",
        "desc": "Google Alert: תובענה ייצוגית / בקשת אישור (replaces geo-blocked gov.il)",
        "notes": "Atom feed — parsed by GoogleAlertsRSSScraper",
    },
    "google_alerts_consumer_il": {
        "enabled": False,
        "url": "",
        "type": "alerts",
        "desc": "Google Alert: הגנת הצרכן הפרה",
        "notes": "Create at google.com/alerts, select RSS delivery, paste URL here",
    },
}

# ── Known focus areas from past cases ──────────────────
# These guide the AI analysis toward areas the firm has expertise in
FIRM_EXPERTISE = [
    "consumer protection / הגנת הצרכן",
    "deceptive advertising / הטעיה בפרסום",
    "tech companies operating in Israel",
    "automotive defects and fuel/range misrepresentation",
    "privacy violations / tracking pixels / data sharing",
    "currency conversion markups / hidden fees",
    "e-commerce platform abuse (Buy Box, pricing)",
    "banking fees / overdraft / credit card charges",
    "construction defects / contractor disputes",
    "labor law violations / extension orders",
]

# Past cases the firm has worked on — used for duplicate detection
# and to avoid re-researching known opportunities
KNOWN_CASES = [
    {"name": "Roblox — Robux USD markup + PEGI violations", "status": "active", "case": "ת\"צ"},
    {"name": "Amazon — Buy Box manipulation", "status": "active", "case": "ת\"צ 18196-07-24"},
    {"name": "Sano — Rizpaz foam-free claim", "status": "research", "case": None},
    {"name": "Perplexity AI — tracking pixel privacy", "status": "research", "case": None},
    {"name": "SERES — EV range misrepresentation", "status": "research", "case": None},
    {"name": "Champion Motors/VW — OCU3 ECall defect", "status": "evaluated", "case": None},
    {"name": "Mazda Israel — settlement approved", "status": "settled", "case": "ת\"צ 37933-08-17"},
    {"name": "Meta — advertiser class action (Iron Tribe parallel)", "status": "research", "case": None},
    {"name": "Google — Taylor v. Google settlement exclusion", "status": "research", "case": None},
    {"name": "Microsoft/OpenAI — ChatGPT pricing antitrust", "status": "evaluated", "case": None},
]
