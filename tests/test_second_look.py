#!/usr/bin/env python3
"""
Stage-1 second look — an item just under the gate is re-read in full before it
is dropped. Offline: fake Claude, fake fetch.

  * only scores in [SECOND_LOOK_MIN, gate) get a second look; others never fetch
  * the second verdict replaces the first only when it reaches the gate
  * no URL, no longer text, a fetch error or a second-look error → first verdict kept
  * SECOND_LOOK_ENABLED=false and analyzers without second_look → first verdict kept
  * the prompt file formats cleanly and carries the first screen's reasoning
  * run_eval measures through the same path

Usage:  python tests/test_second_look.py
"""
import os, sys, types, tempfile

os.environ["PYTHONUTF8"] = "1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.chdir(tempfile.mkdtemp(prefix="scout_2nd_"))

_pacer = types.ModuleType("scrapers.pacer_monitor")
class _NoPacer:
    def login(self): return False
    def close(self): pass
_pacer.PacerMonitorClient = _NoPacer
sys.modules["scrapers.pacer_monitor"] = _pacer

import main as M
from analysis.claude_analyzer import _load_prompt
import run_eval as RE

FAILURES = []
def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: FAILURES.append(label)

LONG = "full article text about Israeli advertisers " * 40

class Fake:
    def __init__(self, first, second=None, second_err=False):
        self.first, self.second, self.second_err, self.calls = first, second, second_err, 0
    def classify(self, title, content, source_type):
        return {"relevance_score": self.first, "reasoning": "first look", "company": "Acme"}
    def second_look(self, title, full_text, source_type, first):
        self.calls += 1
        if self.second_err:
            return {"relevance_score": None, "stage1_error": "api down"}
        return {"relevance_score": self.second, "reasoning": "the full text names Israeli sellers", "company": "Acme"}

FETCHES = []
def run(first, second=None, url="http://x/a", content="short summary", fetch=LONG, err=False, analyzer=None):
    s = M.ClassActionScout.__new__(M.ClassActionScout)
    s.analyzer = analyzer or Fake(first, second, err)
    def fake_fetch(u):
        FETCHES.append(u)
        if isinstance(fetch, Exception): raise fetch
        return fetch
    s._fetch_article_text = fake_fetch
    FETCHES.clear()
    return s._classify_safely("Amazon ads", content, "international", url=url), s.analyzer, list(FETCHES)

print("1. who gets a second look")
r, a, f = run(3.0, 6.0)
check("3 → second look at 6 replaces it", r["relevance_score"] == 6.0 and a.calls == 1)
check("reasoning says it was a second look and from what score", r["reasoning"].startswith("מבט שני") and "עלה מ-3" in r["reasoning"])
r, a, f = run(2.0, 4.0)
check("2 (the floor) is looked at; 4 passes", r["relevance_score"] == 4.0)
r, a, f = run(1.0, 9.0)
check("1 → no fetch, no second look", f == [] and a.calls == 0 and r["relevance_score"] == 1.0)
r, a, f = run(5.0, 9.0)
check("5 (already passing) → no fetch", f == [] and a.calls == 0 and r["relevance_score"] == 5.0)
r, a, f = run(3.9, 3.5)
check("second look still under the gate → first verdict kept", r["relevance_score"] == 3.9 and r["reasoning"] == "first look")

print("\n2. nothing to gain → first verdict")
r, a, f = run(3.0, 8.0, url=None)
check("no URL → no fetch", f == [] and r["relevance_score"] == 3.0)
r, a, f = run(3.0, 8.0, fetch="tiny")
check("full text not longer than the summary → no second call", a.calls == 0 and r["relevance_score"] == 3.0)
r, a, f = run(3.0, 8.0, fetch=RuntimeError("timeout"))
check("fetch error → first verdict, no crash", r["relevance_score"] == 3.0)
r, a, f = run(3.0, None, err=True)
check("second-look API error → first verdict kept", r["relevance_score"] == 3.0 and not r.get("stage1_error"))
class NoSecond:
    def classify(self, title, content, source_type): return {"relevance_score": 3.0, "reasoning": "x"}
r, a, f = run(3.0, analyzer=NoSecond())
check("analyzer without second_look → no fetch", f == [] and r["relevance_score"] == 3.0)
M.SECOND_LOOK_ENABLED = False
r, a, f = run(3.0, 8.0)
check("SECOND_LOOK_ENABLED=false → off", f == [] and r["relevance_score"] == 3.0)
M.SECOND_LOOK_ENABLED = True
class Boom:
    def classify(self, *a, **k): raise RuntimeError("bug")
r, a, f = run(3.0, analyzer=Boom())
check("first-look exception still becomes a stage-1 error", r["relevance_score"] is None and "bug" in r["stage1_error"])

print("\n3. prompt")
p = _load_prompt("second_look").format(title="T {x}", content="body {y}", source_type="international",
                                       first_score=3.0, first_reasoning="first said no")
check("formats cleanly, braces in text are safe", "body {y}" in p and "first said no" in p and "3.0/10" in p)
check("asks about business customers and the full-text-is-a-paywall case",
      "advertisers" in p and "paywall" in p)

print("\n4. run_eval goes through the same path")
s = M.ClassActionScout.__new__(M.ClassActionScout); s.analyzer = Fake(3.0, 7.0)
s._fetch_article_text = lambda u: LONG
res = RE.score_live({"title": "t", "content": "c", "source_type": "international", "url": "http://x"}, s, full=False)
check("eval sees the second-look score", res["rel"] == 7.0)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILURE(S)"))
sys.exit(1 if FAILURES else 0)
