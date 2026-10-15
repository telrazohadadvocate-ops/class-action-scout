#!/usr/bin/env python3
"""
Eval tools — export_eval_sample.py and run_eval.py, offline, real SQLite file.

  * the export is read-only (file bytes unchanged) and draws from every stratum
  * duplicates, Stage-1 errors and old leads are left out; no lead appears twice
  * HTML entities in titles are decoded
  * run_eval STORED mode: gate/alert recall and precision from stored scores,
    ext items count as missed, bad labels are refused
  * run_eval LIVE mode uses the real Stage-1/2/3 code on a transient lead and
    never writes to the database

Usage:  python tests/test_eval_tools.py
"""
import os, sys, json, types, hashlib, tempfile, logging
from datetime import datetime, timedelta, timezone

os.environ["PYTHONUTF8"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
_TMP = tempfile.mkdtemp(prefix="scout_eval_"); os.chdir(_TMP)

_pacer = types.ModuleType("scrapers.pacer_monitor")
class _NoPacer:
    def login(self): return False
    def close(self): pass
_pacer.PacerMonitorClient = _NoPacer
sys.modules["scrapers.pacer_monitor"] = _pacer

from database.models import init_database, get_session, Lead
import export_eval_sample as EX
import run_eval as RE
for h in list(logging.getLogger().handlers):
    logging.getLogger().removeHandler(h)

FAILURES = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: FAILURES.append(label)

db_file = os.path.join(_TMP, "scout.db")
url = f"sqlite:///{db_file}"
init_database(url)
s = get_session(url)
now = datetime.now(timezone.utc).replace(tzinfo=None)

def add(title, rel, pri=None, strength=None, status="new", days=1, **kw):
    l = Lead(title=title, relevance_score=rel, priority_score=pri, strength_score=strength,
             status=status, scraped_at=now - timedelta(days=days), source_name="src",
             source_url=f"http://x/{title}", raw_content=f"body of {title}",
             relevance_reasoning="because", **kw)
    s.add(l); s.flush()
    if l.dedup_group_id is None:
        l.dedup_group_id = str(l.id)
    return l

for i in range(12): add(f"hot{i}", 8, pri=7.5, strength=7)
for i in range(10): add(f"mid{i}", 6, pri=6, strength=5)
for i in range(8):  add(f"low{i}", 5, pri=3, strength=3)
for i in range(15): add(f"edge{i}", 3)
for i in range(9):  add(f"rej{i}", 1)
for i in range(3):  add(f"pursue{i}", 8, pri=8, strength=8, status="pursuing")
add("dismissed0", 6, pri=7.2, strength=6, status="dismissed")
add("&quot;Quoted&quot; edge", 3.5)
dupe = add("dupe", 9, pri=9, strength=9, is_duplicate_of_known=True)
member = add("member", 9, pri=9, strength=9); member.dedup_group_id = "1"
add("err", None, stage1_error="boom")
add("old", 9, pri=9, strength=9, days=400)
s.commit(); s.close()

# ── 1. export ──────────────────────────────────────────────────────────
print("1. export")
digest = lambda: hashlib.sha256(open(db_file, "rb").read()).hexdigest()
before = digest()
picked, counts = EX.export(db_file, days=120, seed=1)
check("database file unchanged by the export", digest() == before)
strata = {p["s"] for p in picked}
check("every stratum is represented", strata == set(EX.QUOTA))
check("quotas respected", all(n <= EX.QUOTA[k] for k, (n, _) in counts.items()))
check("reviewed stratum holds the person-labelled leads",
      sorted(p["t"] for p in picked if p["s"] == "reviewed") == ["dismissed0", "pursue0", "pursue1", "pursue2"])
ids = [p["id"] for p in picked]
check("no lead appears twice", len(ids) == len(set(ids)))
titles = {p["t"] for p in picked}
check("duplicates, cluster members, stage-1 errors and old leads excluded",
      not titles & {"dupe", "member", "err", "old"})
check("edge_reject is just under the gate", all(3 <= p["rel"] < 4 for p in picked if p["s"] == "edge_reject"))
check("HTML entities decoded", EX.short("&quot;Quoted&quot; edge", 160) == '"Quoted" edge'
      and not any("&quot;" in p["t"] for p in picked))
check("same seed → same sample", [p["id"] for p in EX.export(db_file, 120, 1)[0]] == ids)

# ── 2. run_eval STORED ─────────────────────────────────────────────────
print("\n2. run_eval stored")
by_title = {}
import sqlite3
con = sqlite3.connect(db_file)
for i, t in con.execute("SELECT id, title FROM leads"): by_title[t] = i
con.close()
gold = {"items": [
    {"id": by_title["hot0"], "label": "yes"},
    {"id": by_title["hot1"], "label": "no", "note": "not for us"},
    {"id": by_title["mid0"], "label": "yes"},
    {"id": by_title["edge0"], "label": "yes", "note": "should pass"},
    {"id": by_title["rej0"], "label": "no"},
    {"id": by_title["low0"], "label": "maybe"},
    {"ext": "amazon-ads", "title": "Amazon Sponsored Ads auction", "content": "ads", "label": "yes"},
    {"id": 999999, "label": "yes"},
]}
gpath = os.path.join(_TMP, "gold.json"); json.dump(gold, open(gpath, "w"))
items = RE.attach_db_rows(RE.load_gold(gpath), db_file)
check("unknown lead id skipped", len(items) == 7)
res = [{"key": it["key"], "label": it["label"], "note": it.get("note", ""), "title": it["title"],
        **RE.score_stored(it)} for it in items]
st = RE.summarize(res, 4.0, 7.0, scored_stage3=True)
check("gate recall 2/4 (hot0, mid0 pass; edge0 and ext missed)", st["gate_recall"] == (2, 4))
check("ext item counted as missed at the gate", any(r["key"] == "amazon-ads" for r in st["missed_at_gate"]))
check("alert recall 1/4", st["alert_recall"] == (1, 4))
check("alert precision 1/2 (hot1 labelled no)", st["alert_precision"] == (1, 2))
check("mid0 listed as analysed-but-not-hot", [r["key"] for r in st["missed_alert"]] == [f"#{by_title['mid0']}"])
json.dump({"items": [{"id": 1, "label": "good"}]}, open(gpath, "w"))
try:
    RE.load_gold(gpath); refused = False
except ValueError:
    refused = True
check("bad label refused", refused)

# ── 3. run_eval LIVE (fake Claude) ────────────────────────────────────
print("\n3. run_eval live")
import analysis.value_estimator as ve
class FakeAnalyzer:
    client = None; model = "fake"
    def classify(self, title, content, source_type):
        if "boom" in title: return {"relevance_score": None, "stage1_error": "api down"}
        return {"relevance_score": 6.0 if "edge" in title or "Amazon" in title else 2.0,
                "company": "Acme", "sector": "x", "reasoning": "live reason"}
    def analyze(self, title, content, classification):
        return {"strength_score": 7.0, "priority": "high", "legal_analysis": "x",
                "recommended_action": "x", "comparable_cases": []}
ve.estimate_value = lambda lead, client, model: setattr(lead, "priority_score", 7.8)
import main as M
scout = M.ClassActionScout.__new__(M.ClassActionScout); scout.analyzer = FakeAnalyzer()
before = digest()
live_items = [it for it in items if it["key"] in (f"#{by_title['edge0']}", "amazon-ads", f"#{by_title['rej0']}")]
live_items.append({"key": "x-boom", "title": "boom", "content": "", "source_type": "x", "label": "yes"})
out = {it["key"]: RE.score_live(it, scout, full=True) for it in live_items}
e0, r0 = out[f"#{by_title['edge0']}"], out[f"#{by_title['rej0']}"]
check("edge0 now passes the gate and is scored", e0["rel"] == 6.0 and e0["pri"] == 7.8)
check("ext item scored live", out["amazon-ads"]["pri"] == 7.8)
check("below-gate item not sent to stage 2", r0["rel"] == 2.0 and r0["pri"] is None)
check("technical error reported, not a score", out["x-boom"]["rel"] is None and out["x-boom"].get("error") == "api down")
check("live run did not touch the database", digest() == before)

# ── 4. the committed gold set is well-formed ──────────────────────────
print("\n4. eval/gold.json")
g = RE.load_gold(RE.DEFAULT_GOLD)
check("gold set loads and every label is valid", len(g) >= 30)
check("every item has an id or ext text", all(("id" in i) or (i.get("ext") and i.get("title")) for i in g))
check("no lead labelled twice", len([i["id"] for i in g if "id" in i]) == len({i["id"] for i in g if "id" in i}))

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)"))
sys.exit(1 if FAILURES else 0)
