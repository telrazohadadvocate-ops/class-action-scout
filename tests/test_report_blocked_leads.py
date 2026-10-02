#!/usr/bin/env python3
"""
scripts/report_blocked_leads.py must classify by record ids, not by
known_case_ref == title, must leave manual decisions alone, and must not write.

Usage:  python tests/test_report_blocked_leads.py
"""
import os, sys, sqlite3, tempfile, subprocess, hashlib
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
from database.models import init_database, get_session, Lead

FAILURES = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: FAILURES.append(label)

f = tempfile.mktemp(suffix=".db"); url = f"sqlite:///{f}"
init_database(url); s = get_session(url)
def add(**kw):
    l = Lead(**kw); s.add(l); s.commit(); return l.id
SELF_NOTE = "🔁 כפילות של ליד #{} (כותרת זהה)"

a = add(title="Self dup story about fees", company="Acme", relevance_score=7.0)
l = s.get(Lead, a); l.is_duplicate_of_known = True; l.dedup_group_id = str(a)
l.known_case_ref = l.title; l.notes = SELF_NOTE.format(a); s.commit()

canon = add(title="Genuine story", company="Beta", relevance_score=7.0, dedup_group_id="x")
g = add(title="Genuine story", company="Beta", relevance_score=7.0, is_duplicate_of_known=True,
        dedup_group_id=str(canon), known_case_ref="Genuine story", notes=SELF_NOTE.format(canon))

c2 = add(title="Shared headline here", company="Gamma", relevance_score=6.0)
r = add(title="Shared headline here", company="Gamma", relevance_score=6.0)
l = s.get(Lead, r); l.is_duplicate_of_known = True; l.dedup_group_id = str(r)
l.known_case_ref = l.title; l.notes = SELF_NOTE.format(r); s.commit()

m = add(title="Manual separate call", company="Delta", relevance_score=6.0, dup_review="separate")
l = s.get(Lead, m); l.is_duplicate_of_known = True; l.dedup_group_id = str(m)
l.notes = SELF_NOTE.format(m); s.commit()

k = add(title="Meta faces new class action over ads", company="Meta", relevance_score=6.0,
        is_duplicate_of_known=True, known_case_ref="Meta — advertiser class action (Iron Tribe parallel)")
l = s.get(Lead, k); l.dedup_group_id = str(k); s.commit()

z = add(title="Zero no reason", company="Eps", relevance_score=0.0, relevance_reasoning="")
nz = add(title="Real low verdict", company="Eps", relevance_score=2.0, relevance_reasoning="לא רלוונטי")
s.close()

before = hashlib.sha256(open(f, "rb").read()).hexdigest()
out_csv = tempfile.mktemp(suffix=".csv")
p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "report_blocked_leads.py"),
                    "--db", f, "--out", out_csv], capture_output=True, text=True)
check("script exits 0", p.returncode == 0)
rows = list(__import__("csv").reader(open(out_csv, encoding="utf-8")))[1:]
cat = {int(r[1]): r[0] for r in rows}
prop = {int(r[1]): r[6] for r in rows}

check("self-duplicate proven by ids", cat.get(a) == "self_duplicate")
check("...proposal: unmerge + targeted Stage 2/3", "unmerge" in prop.get(a, "") and "Stage 2/3" in prop.get(a, ""))
check("known_case_ref == title but points at another id -> genuine, kept", cat.get(g) == "genuine_duplicate")
check("self-duplicate with a real identical-title lead -> re-point, not unmerge",
      cat.get(r) == "self_duplicate" and f"#{c2}" in prop.get(r, ""))
check("manual decision left alone", cat.get(m) == "manual_decision")
check("firm known-case flag reported separately, generic match called out",
      cat.get(k) == "known_case" and "GENERIC" in [x for x in rows if int(x[1]) == k][0][5])
check("score 0 + empty reasoning -> stage-1 suspect", cat.get(z) == "stage1_suspect")
check("a real low verdict is not touched", nz not in cat)
check("database file unchanged (read-only)", hashlib.sha256(open(f, "rb").read()).hexdigest() == before)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)"))
sys.exit(1 if FAILURES else 0)
