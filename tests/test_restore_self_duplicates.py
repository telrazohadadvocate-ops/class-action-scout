#!/usr/bin/env python3
"""
scripts/restore_self_duplicates.py — offline, real SQLite file, fake Claude
and fake Voyage.

  * dry-run writes nothing and calls no Claude
  * self-duplicate with a genuine identical-title lead -> re-pointed, not analysed
  * self-duplicate that semantically duplicates another lead -> merged into it
  * two restored self-duplicates with the same headline -> one analysed, one merged
  * a plain self-duplicate -> unmerged, Stage 2 + 3, tagged; self note removed
  * below-gate self-duplicate -> unmerged only
  * manual decision and genuine duplicate untouched
  * no record ends up a duplicate of itself
  * alert cap applied
  * resumable after a Stage-2 failure; finished leads never re-analysed
  * --limit processes the oldest N only
Plus the firm known-case matcher no longer fires on generic words.

Usage:  python tests/test_restore_self_duplicates.py
"""
import os, sys, json, types, logging, tempfile, hashlib
os.environ["PYTHONUTF8"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
_TMP = tempfile.mkdtemp(prefix="scout_r_"); os.chdir(_TMP)

import main as M
M.time.sleep = lambda s: None
for h in list(logging.getLogger().handlers):
    logging.getLogger().removeHandler(h)
import analysis.value_estimator as ve
from analysis.claude_analyzer import AnalysisError
from analysis.dedup import SemanticDeduplicator
from database.models import init_database, get_session, Lead
from config.settings import REVIEW_THRESHOLD
import restore_self_duplicates as R

FAILURES = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: FAILURES.append(label)

ve.estimate_value = lambda lead, client, model: setattr(lead, "priority_score", 8.0)

class Analyzer:
    def __init__(self, fail=()):
        self.fail, self.analyzed, self.client, self.model = set(fail), [], None, "fake"
    def analyze(self, title, content, classification):
        if title in self.fail:
            self.fail.discard(title); raise AnalysisError("truncated")
        self.analyzed.append(title)
        return {"strength_score": 6.0, "priority": "medium", "legal_analysis": "x",
                "recommended_action": "y", "comparable_cases": [],
                "already_filed_il": False, "already_filed_details": ""}

V = [1.0] + [0.0] * 15
def dedup():
    d = SemanticDeduplicator(api_key="", threshold=REVIEW_THRESHOLD)
    d._client = object()
    def batch(texts):
        out = []
        for i, t in enumerate(texts):
            if "reworded" in t: out.append(V)
            else:
                h = hashlib.sha256(t.encode()).digest()   # deterministic, near-orthogonal
                out.append([0.0] + [(b / 255.0) - 0.5 for b in h[:15]])
        return out
    d.compute_embeddings_batch = batch
    return d

SELF = "🔁 כפילות של ליד #{} (כותרת זהה)"

def build():
    f = tempfile.mktemp(suffix=".db", dir=_TMP); url = f"sqlite:///{f}"
    init_database(url); db = get_session(url)
    ids = {}
    def add(key, **kw):
        l = Lead(**kw); db.add(l); db.commit(); ids[key] = l.id; return l
    def selfdup(key, **kw):
        l = add(key, **kw)
        l.is_duplicate_of_known = True; l.dedup_group_id = str(l.id)
        l.known_case_ref = l.title; l.notes = SELF.format(l.id); db.commit()
    selfdup("A", title="Unique story about hidden fees", company="Acme", relevance_score=7.0)
    add("C", title="Shared headline about refunds", company="Beta", relevance_score=7.0,
        strength_score=6.0, dedup_group_id="Cg")
    selfdup("B", title="Shared headline about refunds", company="Beta", relevance_score=6.0)
    add("E", title="Original gamma story", company="Gamma", relevance_score=7.0, strength_score=6.0,
        embedding=json.dumps(V), dedup_group_id="Eg")
    selfdup("D", title="Gamma reworded headline", company="Gamma", relevance_score=6.0)
    selfdup("F", title="Twin story on tracking", company="Zed", relevance_score=6.0)
    selfdup("G", title="Twin story on tracking", company="Zed", relevance_score=6.0)
    add("H", title="Manual separate call", company="Delta", relevance_score=6.0,
        is_duplicate_of_known=True, dup_review="separate")
    db.get(Lead, ids["H"]).dedup_group_id = str(ids["H"]); db.get(Lead, ids["H"]).notes = SELF.format(ids["H"])
    add("I", title="Genuine dup", company="Eps", relevance_score=6.0, is_duplicate_of_known=True,
        dedup_group_id=str(ids["C"]), notes=SELF.format(ids["C"]))
    selfdup("J", title="Below gate self dup", company="Omega", relevance_score=3.0)
    db.commit()
    return db, ids

def snap(db):
    db.expire_all()
    return [(l.id, l.is_duplicate_of_known, l.dedup_group_id, l.notes, l.strength_score,
             l.priority_score, l.known_case_ref) for l in db.query(Lead).order_by(Lead.id)]

quiet = lambda *a, **k: None

# ── dry run ─────────────────────────────────────────────────────────────
print("\n1. dry run")
db, ids = build()
before = snap(db)
s = R.run(db, analyzer=None, deduplicator=dedup(), dry_run=True, log=quiet)
check("finds the 6 self-duplicates (not the manual one, not the genuine one)", s["self_duplicates"] == 6)
check("database unchanged", snap(db) == before)

# ── full run ────────────────────────────────────────────────────────────
print("\n2. full run")
db, ids = build()
a = Analyzer()
s = R.run(db, a, dedup(), alert_cap=1, log=quiet)
db.expire_all(); L = lambda k: db.get(Lead, ids[k])
check("A unmerged, analysed, tagged",
      not L("A").is_duplicate_of_known and L("A").strength_score == 6.0
      and L("A").priority_score == 8.0 and R.RESTORE_MARK in (L("A").notes or ""))
check("A self note removed, self known_case_ref cleared",
      f"#{ids['A']} " not in (L("A").notes or "") and L("A").known_case_ref is None)
check("B re-pointed to the genuine identical-title lead C, not analysed",
      L("B").is_duplicate_of_known and L("B").dedup_group_id == "Cg"
      and f"#{ids['C']}" in L("B").notes and L("B").strength_score is None)
check("D merged into E by embedding, not analysed",
      L("D").is_duplicate_of_known and f"#{ids['E']}" in L("D").notes and L("D").strength_score is None)
check("F analysed, G merged into F",
      L("F").strength_score == 6.0 and L("G").is_duplicate_of_known
      and L("G").dedup_group_id == str(ids["F"]) and L("G").strength_score is None)
check("J unmerged only (below gate)", not L("J").is_duplicate_of_known and L("J").strength_score is None)
check("H (manual) untouched", L("H").dup_review == "separate" and L("H").is_duplicate_of_known)
check("I (genuine) untouched", L("I").dedup_group_id == str(ids["C"]))
check("exactly A and F went to Stage 2", sorted(a.analyzed) == sorted([L("A").title, L("F").title]))
self_refs = [l.id for l in db.query(Lead) if l.is_duplicate_of_known and l.dedup_group_id == str(l.id)
             and l.dup_review not in ("merged", "separate")]
check("no record is a duplicate of itself", self_refs == [])
check("alert cap: 1 left un-alerted, the other stamped",
      s["alert_kept"] == 1 and s["alert_suppressed"] == 1)

# ── resumable ───────────────────────────────────────────────────────────
print("\n3. resumable after a Stage-2 failure")
db, ids = build()
first_title = db.get(Lead, ids["A"]).title
a = Analyzer(fail={first_title})
R.run(db, a, dedup(), log=quiet)
db.expire_all()
check("A tagged and unmerged but unscored after the failure",
      db.get(Lead, ids["A"]).strength_score is None and R.RESTORE_MARK in db.get(Lead, ids["A"]).notes)
s2 = R.run(db, a, dedup(), log=quiet)
db.expire_all()
check("re-run analyses A", db.get(Lead, ids["A"]).strength_score == 6.0 and s2["resumed"] == 1)
check("F not analysed twice", a.analyzed.count(db.get(Lead, ids["F"]).title) == 1)

# ── limit ───────────────────────────────────────────────────────────────
print("\n4. --limit")
db, ids = build()
a = Analyzer()
s = R.run(db, a, dedup(), limit=1, log=quiet)
db.expire_all()
check("only the oldest self-duplicate processed",
      s["selected"] == 1 and not db.get(Lead, ids["A"]).is_duplicate_of_known
      and db.get(Lead, ids["F"]).is_duplicate_of_known)

# ── known-case matcher ─────────────────────────────────────────────────
print("\n5. firm known-case matcher ignores generic words")
sc = M.ClassActionScout.__new__(M.ClassActionScout)
def known(company, title):
    l = Lead(title=title, company=company, raw_content="")
    return sc._check_known_cases(l)
check("Meta + 'class action' is NOT the firm's Meta case", not known("Meta", "Meta Hit With New Class Action Over Ads"))
check("Meta + Iron Tribe advertiser IS", known("Meta", "Iron Tribe advertiser suit against Meta expands"))
check("any Google article is NOT the Taylor case", not known("Google", "Google Sued Over Search Rankings"))
check("Taylor v. Google IS", known("Google", "Taylor v. Google settlement leaves out Israelis"))
check("Amazon Buy Box manipulation IS", known("Amazon", "Amazon Buy Box manipulation claims advance"))
check("Amazon 'manipulation' alone is NOT", not known("Amazon", "Amazon price manipulation probe"))

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)"))
sys.exit(1 if FAILURES else 0)
