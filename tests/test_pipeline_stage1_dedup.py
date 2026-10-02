#!/usr/bin/env python3
"""
Pipeline guard tests: Stage 1 gate -> dedup -> Stage 2, against a real SQLite
file DB with a fake classifier, fake Voyage and no network.

Covers the outage from 2026-08-14: a lead was committed in Stage 1, then
_semantic_dedup built its title index from the DB, found the lead itself, and
marked it "duplicate of itself" -- so nothing reached Stage 2.

  1. A new lead over the gate reaches Stage 2 (Voyage on / off / failing)
  2. A lead under the gate is stored and not analysed
  3. Real duplicates are still blocked: vs an existing lead (title and
     embedding) and within the same batch
  4. raw_sources.lead_id points at the lead after the run
  5. A Stage-1 technical failure is stored as NULL + error, never as 0, and
     is retried on the next run (bounded)
  6. A crash mid-Stage-1 does not make saved-but-unclassified sources
     permanently skipped
  7. ClaudeAnalyzer.classify: truncation / API error / bad score / degraded
     parse -> NULL + error; valid JSON with "unknown" values -> accepted
  8. The Stage-1 output schema matches the fields the prompt asks for
  9. Per-stage counts and the duplicate's target id are logged

Usage:  python tests/test_pipeline_stage1_dedup.py
"""
import os
import re
import sys
import json
import types
import hashlib
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

os.environ["PYTHONUTF8"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="scout_t_")
os.chdir(_TMP)                       # main.py opens scout.log in the cwd

# No playwright / network: stub the PACER module the pipeline imports lazily.
_pacer = types.ModuleType("scrapers.pacer_monitor")
class _NoPacer:
    def login(self): return False
    def close(self): pass
_pacer.PacerMonitorClient = _NoPacer
sys.modules["scrapers.pacer_monitor"] = _pacer

import main as M
import analysis.value_estimator as ve
from analysis.dedup import SemanticDeduplicator
from analysis.claude_analyzer import ClaudeAnalyzer, _CLASSIFY_SCHEMA
from database.models import init_database, get_session, Lead, RawSource
from config.settings import REVIEW_THRESHOLD, MIN_RELEVANCE_SCORE

M.time.sleep = lambda s: None        # _embed_batched backs off with sleep()
for _h in list(logging.getLogger().handlers):   # keep test output readable
    logging.getLogger().removeHandler(_h)

FAILURES = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


# ── Fakes ────────────────────────────────────────────────────────────────

@dataclass
class Item:
    url: str
    title: str
    content: str = "body"
    source_name: str = "fake_src"
    date: datetime = None


class Scraper:
    def __init__(self, items): self.items = items
    def scrape(self): return list(self.items)


def ok(score, company="Acme"):
    return {"relevance_score": float(score), "company": company, "sector": "consumer",
            "reasoning": "נימוק", "operates_in_israel": True,
            "product_available_in_israel": None, "israeli_class_size_estimate": "unknown",
            "israeli_law_basis": "חוק הגנת הצרכן", "estimated_class_size": "אלפים",
            "stage1_error": None}


def err(msg="API call failed"):
    return {"relevance_score": None, "stage1_error": msg}


class Analyzer:
    """classify() answers from a title->result map; a result may be an
    Exception (raised) or a list consumed one per call."""
    def __init__(self, cmap):
        self.cmap = cmap
        self.classified, self.analyzed = [], []
        self.client, self.model = None, "fake"

    def classify(self, title, content, source_type):
        self.classified.append(title)
        r = self.cmap[title]
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, BaseException):
            raise r
        return dict(r)

    def analyze(self, title, content, classification):
        self.analyzed.append(title)
        return {"strength_score": 6.0, "priority": "medium", "legal_analysis": "ניתוח",
                "recommended_action": "לבדוק", "comparable_cases": [],
                "already_filed_il": False, "already_filed_details": ""}


def _vec(key):
    h = hashlib.sha256(key.encode()).digest()
    return [((b / 255.0) - 0.5) for b in h[:16]]


def dedup(mode="on", vec_map=None):
    """mode: on | off | fail. vec_map: substring-of-text -> vector."""
    d = SemanticDeduplicator(api_key="", threshold=REVIEW_THRESHOLD)
    if mode == "off":
        return d
    d._client = object()
    def batch(texts):
        if mode == "fail":
            raise RuntimeError("voyage down")
        out = []
        for t in texts:
            v = next((vv for k, vv in (vec_map or {}).items() if k in t), None)
            out.append(v or _vec(t))
        return out
    d.compute_embeddings_batch = batch
    return d


ve.estimate_value = lambda lead, client, model: setattr(lead, "priority_score", 5.0)


def make(items, analyzer, ded, db_file=None):
    db_file = db_file or tempfile.mktemp(suffix=".db", dir=_TMP)
    url = f"sqlite:///{db_file}"
    init_database(url)
    s = M.ClassActionScout.__new__(M.ClassActionScout)
    s.db = get_session(url)
    s.analyzer = analyzer
    s.pinkas = None
    s.scrapers = {"fake_src": Scraper(items)}
    s.deduplicator = ded
    s._send_run_alerts = lambda: None
    return s, db_file


def seed(s, **kw):
    base = dict(title="t", company="Acme", relevance_score=7.0, strength_score=6.0,
                priority="medium", source_url=f"https://seed/{datetime.now().timestamp()}")
    base.update(kw)
    l = Lead(**base)
    s.db.add(l); s.db.commit()
    return l.id


def lead_by_url(s, url):
    return s.db.query(Lead).filter_by(source_url=url).one()


class Capture(logging.Handler):
    def __init__(self): super().__init__(); self.lines = []
    def emit(self, r): self.lines.append(r.getMessage())


# ── 1. Over the gate -> Stage 2, for every Voyage state ─────────────────
print("\n1. new lead over the gate reaches Stage 2 (no self-duplicate)")
T = "Zeta Hit With Privacy Class Action Over Tracking Pixel"
for mode in ("on", "off", "fail"):
    a = Analyzer({T: ok(7, "Zeta")})
    s, _ = make([Item("https://x/1", T)], a, dedup(mode))
    s.run(skip_pinkas=True)
    l = lead_by_url(s, "https://x/1")
    check(f"[voyage {mode}] score-7 lead analysed in Stage 2", a.analyzed == [T])
    check(f"[voyage {mode}] not marked duplicate", not l.is_duplicate_of_known)
    check(f"[voyage {mode}] no self-reference in dedup fields",
          l.known_case_ref != l.title and f"#{l.id} " not in (l.notes or ""))

# ── 2. Under the gate ───────────────────────────────────────────────────
print("\n2. lead under the gate is stored and not analysed")
a = Analyzer({"Minor US-only bank fee suit": ok(3)})
s, _ = make([Item("https://x/2", "Minor US-only bank fee suit")], a, dedup("on"))
s.run(skip_pinkas=True)
l = lead_by_url(s, "https://x/2")
check("relevance stored as 3", l.relevance_score == 3.0)
check("not analysed", a.analyzed == [])
check("no stage1_error", getattr(l, "stage1_error", None) is None)

# ── 3. Real duplicates are still blocked ────────────────────────────────
print("\n3. real duplicates are blocked and point at the right record")
D = "Acme Sued Over Hidden Subscription Fees"
a = Analyzer({D: ok(7)})
s, _ = make([Item("https://x/3", D)], a, dedup("on"))
canon = seed(s, title=D)
s.run(skip_pinkas=True)
l = lead_by_url(s, "https://x/3")
check("identical title as an existing lead -> duplicate", bool(l.is_duplicate_of_known))
check("duplicate points at the existing id", l.dedup_group_id == str(canon) and f"#{canon}" in (l.notes or ""))
check("duplicate not analysed", a.analyzed == [])

a = Analyzer({D: ok(7)})
s, _ = make([Item("https://x/3a", D), Item("https://x/3b", D)], a, dedup("on"))
s.run(skip_pinkas=True)
l1, l2 = lead_by_url(s, "https://x/3a"), lead_by_url(s, "https://x/3b")
check("same batch: first analysed once", a.analyzed == [D] and not l1.is_duplicate_of_known)
check("same batch: second is duplicate of the first", bool(l2.is_duplicate_of_known)
      and l2.dedup_group_id == str(l1.id))

for mode in ("off", "fail"):
    a = Analyzer({D: ok(7)})
    s, _ = make([Item("https://x/3c", D), Item("https://x/3d", D)], a, dedup(mode))
    s.run(skip_pinkas=True)
    l1, l2 = lead_by_url(s, "https://x/3c"), lead_by_url(s, "https://x/3d")
    check(f"[voyage {mode}] same-batch identical title still caught by title rule",
          a.analyzed == [D] and bool(l2.is_duplicate_of_known) and l2.dedup_group_id == str(l1.id))

V = [1.0] + [0.0] * 15
a = Analyzer({"Acme reworded headline about fees": ok(7)})
s, _ = make([Item("https://x/3e", "Acme reworded headline about fees")], a,
            dedup("on", {"reworded": V}))
canon = seed(s, title="Original Acme fee story", embedding=json.dumps(V), dedup_group_id="seed")
s.run(skip_pinkas=True)
l = lead_by_url(s, "https://x/3e")
check("semantic duplicate of an existing lead -> merged", bool(l.is_duplicate_of_known)
      and f"#{canon}" in (l.notes or ""))

a = Analyzer({D: ok(6)})
s, _ = make([Item("https://x/3f", D)], a, dedup("on"))
seed(s, title=D, relevance_score=3.0, strength_score=None, priority=None)
s.run(skip_pinkas=True)
check("a lead that never passed the gate does not absorb a new eligible one", a.analyzed == [D])

# ── 4. raw source linked ────────────────────────────────────────────────
print("\n4. raw_sources.lead_id is set")
a = Analyzer({"A": ok(7), "B": ok(2)})
s, _ = make([Item("https://x/4a", "A"), Item("https://x/4b", "B")], a, dedup("on"))
s.run(skip_pinkas=True)
for u in ("https://x/4a", "https://x/4b"):
    raw = s.db.query(RawSource).filter_by(url=u).one()
    check(f"{u[-2:]}: raw.lead_id == lead.id", raw.lead_id is not None and raw.lead_id == lead_by_url(s, u).id)

# ── 5. Stage-1 technical failure -> NULL + error, retried ───────────────
print("\n5. Stage-1 failure is NULL + error, retried, bounded")
E = "Transient failure story"
a = Analyzer({E: [err("truncated"), ok(7)]})
s, f = make([Item("https://x/5", E)], a, dedup("on"))
s.run(skip_pinkas=True)
l = lead_by_url(s, "https://x/5")
check("relevance_score is NULL, not 0", l.relevance_score is None)
check("stage1_error recorded", bool(getattr(l, "stage1_error", None)))
check("not analysed", a.analyzed == [])
s.run(skip_pinkas=True)                       # same scraper output, new run
s.db.expire_all(); l = lead_by_url(s, "https://x/5")
check("retried on the next run and now scored", l.relevance_score == 7.0 and a.analyzed == [E])
check("error cleared after success", getattr(l, "stage1_error", "x") is None)
check("still exactly one lead for the URL", s.db.query(Lead).filter_by(source_url="https://x/5").count() == 1)

a = Analyzer({"Always fails": [err(), err(), err(), err(), err()]})
s, _ = make([Item("https://x/5b", "Always fails")], a, dedup("on"))
for _ in range(5):
    s.run(skip_pinkas=True)
check("retries stop after the attempt cap", a.classified.count("Always fails") == M.MAX_STAGE1_ATTEMPTS)

# ── 6. Crash mid-Stage-1 does not orphan saved sources ─────────────────
print("\n6. failures mid-run: one bad item, and a process death")

class Die(BaseException):
    """Simulates the process dying (OOM kill, redeploy) — not caught per item."""

a = Analyzer({"G1": ok(2), "G2": RuntimeError("unexpected bug"), "G3": ok(5)})
s, _ = make([Item("https://x/6x", "G1"), Item("https://x/6y", "G2"), Item("https://x/6z", "G3")],
            a, dedup("on"))
s.run(skip_pinkas=True)
check("an ordinary exception on one item does not stop the run", a.analyzed == ["G3"])
l = lead_by_url(s, "https://x/6y")
check("...and that item is stored NULL + error for retry", l.relevance_score is None and bool(l.stage1_error))

items = [Item("https://x/6a", "C1"), Item("https://x/6b", "C2"), Item("https://x/6c", "C3")]
a = Analyzer({"C1": ok(2), "C2": Die(), "C3": ok(2)})
s, f = make(items, a, dedup("on"))
try:
    s.run(skip_pinkas=True)
except Die:
    pass
s.db.close()                                  # process died; nothing uncommitted survives
a2 = Analyzer({"C1": ok(2), "C2": ok(2), "C3": ok(2)})
s2, _ = make(items, a2, dedup("on"), db_file=f)
s2.run(skip_pinkas=True)
for it in items:
    n = s2.db.query(Lead).filter_by(source_url=it.url).count()
    check(f"{it.title}: classified after the restart, exactly one lead", n == 1)
check("the restart reported them as resumed", s2.last_run_stats["scrape"]["resumed"] == 3)
s2.run(skip_pinkas=True)
check("a third run re-classifies nothing", a2.classified.count("C1") == 1)

# ── 7. ClaudeAnalyzer.classify contract ────────────────────────────────
print("\n7. ClaudeAnalyzer.classify: failures are NULL + error, never 0")

class Resp:
    def __init__(self, text, stop="end_turn"):
        self.stop_reason = stop
        self.content = [types.SimpleNamespace(type="text", text=text)]

def analyzer_with(*responses):
    ca = ClaudeAnalyzer.__new__(ClaudeAnalyzer)
    ca.model = "m"
    seq = list(responses)
    def create(**kw):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    ca.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    return ca

good = json.dumps({**{k: v for k, v in ok(6).items() if k != "stage1_error"},
                   "operates_in_israel": None})
r = analyzer_with(Resp(good)).classify("t", "c", "international")
check("valid JSON with 'unknown'/null values accepted", r["relevance_score"] == 6.0 and r.get("stage1_error") is None
      and r.get("operates_in_israel") is None)
r = analyzer_with(Resp('{"relevance_score": 7', "max_tokens"), Resp('{"relevance', "max_tokens")).classify("t", "c", "x")
check("truncated twice -> NULL + error", r["relevance_score"] is None and r.get("stage1_error"))
r = analyzer_with(RuntimeError("503")).classify("t", "c", "x")
check("API error -> NULL + error", r["relevance_score"] is None and r.get("stage1_error"))
for bad in (0, 11, "high"):
    body = json.dumps({**json.loads(good), "relevance_score": bad})
    r = analyzer_with(Resp(body)).classify("t", "c", "x")
    check(f"score {bad!r} outside 1-10 -> NULL + error", r["relevance_score"] is None and r.get("stage1_error"))
body = json.dumps({**json.loads(good), "reasoning": ""})
r = analyzer_with(Resp(body)).classify("t", "c", "x")
check("empty reasoning -> NULL + error", r["relevance_score"] is None and r.get("stage1_error"))
r = analyzer_with(Resp('garbage "relevance_score": 8 more garbage')).classify("t", "c", "x")
check("regex-recovered (degraded) parse -> NULL + error", r["relevance_score"] is None and r.get("stage1_error"))

# ── 8. Schema matches prompt ───────────────────────────────────────────
print("\n8. Stage-1 schema matches the fields the prompt asks for")
prompt = open(os.path.join(ROOT, "analysis", "prompts", "classify.txt"), encoding="utf-8").read()
block = prompt[prompt.index("{{"):prompt.index("}}")]
asked = set(re.findall(r'^\s*"(\w+)"\s*:', block, re.M))
props = set(_CLASSIFY_SCHEMA["properties"])
check(f"prompt fields == schema properties ({sorted(asked ^ props) or 'match'})", asked == props)
check("every field required", set(_CLASSIFY_SCHEMA["required"]) == props)
def nullable(p):
    s = _CLASSIFY_SCHEMA["properties"][p]
    return any(o.get("type") == "null" for o in s.get("anyOf", [])) or "null" in (s.get("type") if isinstance(s.get("type"), list) else [])
check("operates_in_israel accepts null", nullable("operates_in_israel"))
check("product_available_in_israel accepts null", nullable("product_available_in_israel"))

# ── 9. Logging ──────────────────────────────────────────────────────────
print("\n9. per-stage counts and decision reasons are logged")
cap = Capture(); logging.getLogger("scout").addHandler(cap)
a = Analyzer({D: ok(7), "Low": ok(2), "Bad": err()})
s, _ = make([Item("https://x/9a", D), Item("https://x/9b", "Low"), Item("https://x/9c", "Bad")], a, dedup("on"))
canon = seed(s, title=D)
s.run(skip_pinkas=True)
logging.getLogger("scout").removeHandler(cap)
text = "\n".join(cap.lines)
st = getattr(s, "last_run_stats", {})
check("stats: stage1 counts", st.get("stage1") == {"classified": 3, "passed": 1, "below_gate": 1, "error": 1, "retried": 0})
check("stats: dedup counts", st.get("dedup", {}).get("merged") == 1 and st.get("dedup", {}).get("unique") == 0)
check("log names the duplicate's target id", f"duplicate of #{canon}" in text)
check("log gives the Stage-1 reason per record", "below gate" in text and "stage1 error" in text)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)"))
sys.exit(1 if FAILURES else 0)
