#!/usr/bin/env python3
"""
Alert threshold 6.5 (from 7) and the daily summary's "near the threshold" list.

  * a new lead at 6.5–6.9 is alerted
  * the backlog guard: a lead scraped before HIGH_PRIORITY_THRESHOLD_SINCE is
    alerted only at the old bar (7), so lowering the threshold does not mail out
    months of old 6.5–6.9 leads at once
  * duplicates are never alerted; nothing is alerted twice
  * the summary lists leads in [NEAR_HOT_FLOOR, threshold) as "near", not the
    ones already in the top list, not below the floor, not duplicates

Offline.  Usage:  python tests/test_alert_threshold.py
"""
import os, sys, types, tempfile
from datetime import datetime, timedelta, timezone

os.environ["PYTHONUTF8"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp(prefix="scout_thr_"))

_pacer = types.ModuleType("scrapers.pacer_monitor")
class _NoPacer:
    def login(self): return False
    def close(self): pass
_pacer.PacerMonitorClient = _NoPacer
sys.modules["scrapers.pacer_monitor"] = _pacer

import main as M
from alerts import email_sender, health
from config import settings
from database.models import init_database, get_session, Lead

FAILURES = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: FAILURES.append(label)

check("threshold is 6.5", settings.HIGH_PRIORITY_THRESHOLD == 6.5)
check("near floor below threshold", settings.NEAR_HOT_FLOOR < settings.HIGH_PRIORITY_THRESHOLD)

url = "sqlite:///" + os.path.join(os.getcwd(), "t.db")
init_database(url)
db = get_session(url)
now = datetime.now(timezone.utc).replace(tzinfo=None)
since = datetime.strptime(settings.HIGH_PRIORITY_THRESHOLD_SINCE, "%Y-%m-%d")
old = since - timedelta(days=30)

def add(title, pri, when, **kw):
    l = Lead(title=title, priority_score=pri, scraped_at=when, relevance_score=6, **kw)
    db.add(l); db.flush(); return l

new_66   = add("new 6.6", 6.6, now)
new_64   = add("new 6.4", 6.4, now)
old_68   = add("old 6.8 backlog", 6.8, old)
old_72   = add("old 7.2", 7.2, old)
dup_90   = add("new dup 9.0", 9.0, now, is_duplicate_of_known=True)
done_80  = add("already alerted 8.0", 8.0, now, alerted_at=now)
db.commit()

SENT = []
email_sender.send_alert_email = lambda leads: SENT.append([l["title"] for l in leads]) or True
s = M.ClassActionScout.__new__(M.ClassActionScout); s.db = db

print("1. alerts")
s._send_run_alerts()
got = set(SENT[0]) if SENT else set()
check("new 6.6 alerted", "new 6.6" in got)
check("new 6.4 not alerted", "new 6.4" not in got)
check("old 6.8 (backlog) NOT alerted", "old 6.8 backlog" not in got)
check("old 7.2 still alerted at the old bar", "old 7.2" in got)
check("duplicate not alerted", "new dup 9.0" not in got)
check("already-alerted lead not re-sent", "already alerted 8.0" not in got)
SENT.clear(); s._send_run_alerts()
check("second run sends nothing", SENT == [])

print("\n2. daily summary near list")
for i, p in enumerate([9.1, 8.5, 8.0, 7.7, 7.4]):          # fills the top-5
    add(f"top {i}", p, now)
add("near 6.3", 6.3, now); add("near 6.0", 6.0, now); add("below 5.9", 5.9, now)
add("near dup 6.2", 6.2, now, is_duplicate_of_known=True)
db.commit()
d = s._collect_digest(datetime.now(timezone.utc))
near = [t["title"] for t in d["near"]]
top = [t["title"] for t in d["top"]]
check("near holds 6.0–6.49 leads", {"near 6.3", "near 6.0", "new 6.4"} <= set(near))
check("near excludes below the floor", "below 5.9" not in near)
check("near excludes the alert band", "new 6.6" not in near)
check("near excludes duplicates", "near dup 6.2" not in near)
check("near and top do not overlap", not set(near) & set(top))
check("near sorted high to low", [t["score"] for t in d["near"]] == sorted([t["score"] for t in d["near"]], reverse=True))
_, h = health.build_digest_email(d, [], datetime.now(timezone.utc))
check("summary shows the near section with the range", "קרובים לסף" in h and "6.5" in h)
check("near leads are links to the lead", f"?lead=" in h.split("קרובים לסף")[1])
d2 = dict(d, near=[])
_, h2 = health.build_digest_email(d2, [], datetime.now(timezone.utc))
check("no near section when there is none", "קרובים לסף" not in h2)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)"))
sys.exit(1 if FAILURES else 0)
