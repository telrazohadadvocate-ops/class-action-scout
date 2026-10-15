#!/usr/bin/env python3
"""
Health alert + daily summary — offline, real SQLite file, fake Claude, fake mail.

  * evaluate_run: each failure mode raises the right issue; a healthy run none
  * the credit-exhausted error is named in plain Hebrew
  * a run whose Stage 1 errors out sends a health alert the same run
  * the same problems are not re-sent within the repeat window
  * a crash mid-run still sends the alert and the exception still propagates
  * the daily summary goes out once per local day — also on a run with no
    new items, and also when healthy
  * a mail failure never raises and is recorded as *_error (so it is retried)
  * DAILY_DIGEST_ENABLED=false turns the summary off

Usage:  python tests/test_health_alerts.py
"""
import os, sys, types, logging, tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

os.environ["PYTHONUTF8"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="scout_h_"); os.chdir(_TMP)

_pacer = types.ModuleType("scrapers.pacer_monitor")
class _NoPacer:
    def login(self): return False
    def close(self): pass
_pacer.PacerMonitorClient = _NoPacer
sys.modules["scrapers.pacer_monitor"] = _pacer

import main as M
M.time.sleep = lambda s: None
for h in list(logging.getLogger().handlers):
    logging.getLogger().removeHandler(h)
import analysis.value_estimator as ve
from analysis.dedup import SemanticDeduplicator
from alerts import health, email_sender
from database.models import init_database, get_session, Lead, AlertLog
from config.settings import REVIEW_THRESHOLD

FAILURES = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: FAILURES.append(label)

CREDIT = ("API call failed: Error code: 400 - {'type': 'error', 'error': {'type': "
          "'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API.'}}")

SENT = []
MAIL_OK = [True]
def fake_send(subject, html):
    SENT.append((subject, html)); return MAIL_OK[0]
email_sender.send_email = fake_send
ve.estimate_value = lambda lead, client, model: setattr(lead, "priority_score", 7.5)

@dataclass
class Item:
    url: str; title: str; content: str = "body"; source_name: str = "fake_src"; date: datetime = None

class Scraper:
    def __init__(self, items, boom=False): self.items, self.boom = items, boom
    def scrape(self):
        if self.boom: raise RuntimeError("site down")
        return list(self.items)

def ok(score):
    return {"relevance_score": float(score), "company": "Acme", "sector": "x", "reasoning": "r",
            "operates_in_israel": True, "product_available_in_israel": None,
            "israeli_class_size_estimate": "", "israeli_law_basis": "", "estimated_class_size": "",
            "stage1_error": None}

class Analyzer:
    def __init__(self, mode="ok"): self.mode, self.client, self.model = mode, None, "m"
    def classify(self, title, content, source_type):
        if self.mode == "credit": return {"relevance_score": None, "stage1_error": CREDIT}
        if self.mode == "crash": raise SystemError("boom")   # not caught per item? (it is) — see crash test
        return ok(7)
    def analyze(self, title, content, classification):
        return {"strength_score": 7.0, "priority": "high", "legal_analysis": "x",
                "recommended_action": "y", "comparable_cases": [],
                "already_filed_il": False, "already_filed_details": ""}

def make(items, mode="ok", db_file=None, scraper_boom=False):
    db_file = db_file or tempfile.mktemp(suffix=".db", dir=_TMP); url = f"sqlite:///{db_file}"
    init_database(url)
    s = M.ClassActionScout.__new__(M.ClassActionScout)
    s.db = get_session(url); s.analyzer = Analyzer(mode); s.pinkas = None
    s.scrapers = {"fake_src": Scraper(items, scraper_boom)}
    s.deduplicator = SemanticDeduplicator(api_key="", threshold=REVIEW_THRESHOLD)
    s._send_run_alerts = lambda: None
    return s, db_file

def statuses(s):
    return [a.status for a in s.db.query(AlertLog).order_by(AlertLog.id)]

def items(n, tag):
    return [Item(f"https://x/{tag}/{i}", f"{tag} story number {i} about fees") for i in range(n)]

# ── 1. evaluate_run ─────────────────────────────────────────────────────
print("\n1. evaluate_run")
base = lambda: {"scrape": {"found": 10, "new": 10, "resumed": 0, "sources": 4, "failed_sources": []},
                "stage1": {"classified": 10, "passed": 3, "below_gate": 7, "error": 0, "retried": 0},
                "dedup": {"in": 3, "unique": 3, "merged": 0, "review": 0},
                "stage2": {"in": 3, "ok": 3, "failed": 0}, "stage3": {"in": 3, "ok": 3, "failed": 0},
                "errors": [], "crash": None}
check("healthy run -> no issues", health.evaluate_run(base()) == [])
st = base(); st["stage1"].update(classified=10, error=8, passed=0, below_gate=2); st["errors"] = [CREDIT]
iss = health.evaluate_run(st)
check("stage-1 errors -> issue", [i["code"] for i in iss] == ["stage1_errors"])
check("credit exhaustion named in Hebrew", "נגמר הקרדיט" in iss[0]["text"])
st = base(); st["stage2"].update(ok=0, failed=3)
check("stage 2 in>0 ok=0 -> issue", [i["code"] for i in health.evaluate_run(st)] == ["stage2_zero"])
st = base(); st["stage3"].update(ok=0, failed=3)
check("stage 3 in>0 ok=0 -> issue", [i["code"] for i in health.evaluate_run(st)] == ["stage3_zero"])
st = base(); st["dedup"].update({"in": 6, "unique": 0, "merged": 6}); st["stage2"].update(**{"in": 0, "ok": 0}); st["stage3"].update(**{"in": 0, "ok": 0})
check("all eligible merged -> issue", [i["code"] for i in health.evaluate_run(st)] == ["dedup_all"])
st = base(); st["scrape"].update(found=0, new=0)
check("no source returned anything -> issue", "scrape_empty" in [i["code"] for i in health.evaluate_run(st)])
st = base(); st["scrape"]["failed_sources"] = ["a", "b"]
check("half the sources failed -> issue", "scrape_failed" in [i["code"] for i in health.evaluate_run(st)])
st = base(); st["crash"] = "RuntimeError: x"
check("crash -> issue", "crash" in [i["code"] for i in health.evaluate_run(st)])
check("one stage-1 error out of 10 is not an alarm",
      health.evaluate_run({**base(), "stage1": {"classified": 10, "passed": 3, "below_gate": 6, "error": 1, "retried": 0}}) == [])

# ── 2. a credit-exhausted run alerts the same run ───────────────────────
print("\n2. Stage-1 failure run")
SENT.clear()
s, f = make(items(5, "a"), mode="credit")
s.run(skip_pinkas=True)
subjects = [x[0] for x in SENT]
check("health alert sent", any("תקלה" in x and "סיכום" not in x for x in subjects))
check("alert names the credit problem", any("נגמר הקרדיט" in h for _, h in SENT))
check("daily summary also sent, flagged", any("סיכום יומי" in x and "תקלה" in x for x in subjects))
check("both recorded", statuses(s) == ["health:stage1_errors", "digest"])

# ── 3. same problem not re-sent; daily summary once a day ──────────────
print("\n3. de-duplication")
SENT.clear()
s2, _ = make(items(5, "b"), mode="credit", db_file=f)
s2.run(skip_pinkas=True)
check("same problems within the window -> nothing sent", SENT == [])
old = s2.db.query(AlertLog).filter(AlertLog.status.like("health:%")).one()
old.sent_at = datetime.utcnow() - timedelta(hours=health.HEALTH_REPEAT_HOURS + 1); s2.db.commit()
s3, _ = make(items(5, "c"), mode="credit", db_file=f)
s3.run(skip_pinkas=True)
check("after the window -> re-sent once", len(SENT) == 1 and "תקלה" in SENT[0][0])

# ── 4. healthy run: summary only, also with no new items ───────────────
print("\n4. healthy runs")
SENT.clear()
s, f = make(items(3, "d"))
s.run(skip_pinkas=True)
check("healthy run -> one email, the summary", len(SENT) == 1 and "סיכום יומי" in SENT[0][0])
check("summary reports hot leads", "לידים חמים" in SENT[0][0] and "המערכת תקינה" in SENT[0][1])
# Sources answer, but everything is already known -> "No new items" early return.
known = items(2, "h")
s, f = make(known)
s.run(skip_pinkas=True)
s.db.query(AlertLog).delete(); s.db.query(Lead).update({Lead.scraped_at: datetime.utcnow() - timedelta(days=3)}); s.db.commit()
SENT.clear()
s2, _ = make(known, db_file=f); s2.run(skip_pinkas=True)
check("run with no new items still sends the summary", len(SENT) == 1 and "סיכום יומי" in SENT[0][0])
check("...and says nothing was analysed", "לא נותחו לידים חדשים" in SENT[0][1])
SENT.clear()
s3, _ = make(known, db_file=f); s3.run(skip_pinkas=True)
check("second run the same day -> no second summary", SENT == [])
SENT.clear()
s4, _ = make([], db_file=f); s4.run(skip_pinkas=True)
check("sources returning nothing at all -> health alert", any("לא החזיר פריטים" in h for _, h in SENT))

# ── 5. crash still alerts, exception propagates ────────────────────────
print("\n5. crash")
SENT.clear()
s, f = make(items(2, "e"))
def boom(*a, **k): raise RuntimeError("dedup exploded")
s._semantic_dedup = boom
raised = False
try:
    s.run(skip_pinkas=True)
except RuntimeError:
    raised = True
check("exception still propagates", raised)
check("crash alert sent", any("קרסה" in h for _, h in SENT))

# ── 6. mail failure ────────────────────────────────────────────────────
print("\n6. mail failure")
SENT.clear(); MAIL_OK[0] = False
s, f = make(items(5, "f"), mode="credit")
try:
    s.run(skip_pinkas=True); no_raise = True
except Exception:
    no_raise = False
MAIL_OK[0] = True
check("failed send does not raise", no_raise)
check("recorded as *_error, not as sent", statuses(s) == ["health_error", "digest_error"])
SENT.clear()
s2, _ = make([], mode="credit", db_file=f); s2.run(skip_pinkas=True)
check("summary retried on the next run after a failed send", any("סיכום יומי" in x for x, _ in SENT))

# ── 7. digest switch ──────────────────────────────────────────────────
print("\n7. DAILY_DIGEST_ENABLED=false")
SENT.clear(); os.environ["DAILY_DIGEST_ENABLED"] = "false"
s, f = make(items(2, "g")); s.run(skip_pinkas=True)
os.environ.pop("DAILY_DIGEST_ENABLED")
check("no summary when disabled", SENT == [])

# ── 8. digest rendering ───────────────────────────────────────────────
print("\n8. digest rendering")
d = {"new": 1, "classified": 1, "passed": 1, "scored": 1, "hot": 1, "threshold": 7.0,
     "pending_retry": 0,
     "top": [{"title": '&quot;תלויים בין שמיים לארץ&quot;: בקשה', "company": "flydubai",
              "score": 8.8, "strength": 7.5, "israel": True}]}
_, h = health.build_digest_email(d, [], datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc))
check("HTML entity in a scraped title is not shown as &quot;", "&amp;quot;" not in h)
check("priority and strength are both labelled", "עדיפות 8.8" in h and "חוזק 7.5" in h)
s, f = make([Item(url="http://x/q", title="&quot;Quoted&quot; title")]); s.run(skip_pinkas=True)
dg = s._collect_digest(datetime.now(timezone.utc))
check("collected title is unescaped once", dg["top"] and dg["top"][0]["title"] == '"Quoted" title')

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)"))
sys.exit(1 if FAILURES else 0)
