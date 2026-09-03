#!/usr/bin/env python3
"""
Guard tests for the two rules that keep a bad score out of the database.

  1. A lead whose Stage 2 failed must come out of a run with priority_score
     still NULL. It has no strength_score, and certification is a third of the
     composite — scoring it anyway blends in a 0 and stores a low number that
     is indistinguishable from a real verdict, so nothing ever revisits it.
     This is the failure that hid the Aug-11..Aug-30 outage: leads carried
     confident-looking scores capped at 4.0.

  2. already_filed_il has NO effect on priority_score — it is a card tag. The
     flag is an LLM reading of the source text and can be wrong. At the old
     x0.4 a flagged lead could not exceed 3.9: under the alert threshold and
     off the bottom of the dashboard, so a false positive was terminal. The
     view side of the same rule lives in tests/test_dashboard_defaults.py
     (hide_filed defaults to false).

Runs offline; no API key and no network.

Usage:  python tests/test_scoring_guards.py
"""
import os
import re
import sys
import pathlib

os.environ["PYTHONUTF8"] = "1"

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from analysis.claude_analyzer import AnalysisError
import analysis.value_estimator as ve
from analysis.value_estimator import _compute_priority_score, estimate_value
from config.settings import HIGH_PRIORITY_THRESHOLD

FAILURES = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


class FakeLead:
    """Only the attributes the scorer touches."""
    id = 1
    title = "Widget Co overcharges Israeli subscribers"
    raw_content = "Some article body."
    estimated_class_size = "100000"
    operates_in_israel = True
    israeli_law_basis = "חוק הגנת הצרכן"
    legal_analysis = "..."
    value_confidence = "high"

    def __init__(self, strength, filed=False, expertise=True):
        self.strength_score = strength
        self.already_filed_il = filed
        self.matches_expertise = expertise


class ExplodingClient:
    """Any API call is a test failure — the guard must fire before the request."""
    class messages:
        @staticmethod
        def create(**_kw):
            raise AssertionError("estimate_value called the API without a strength_score")


# ── 1. no strength_score → no score ────────────────────

def test_no_strength_score_is_refused():
    print("\n### a lead with no Stage-2 analysis is never scored")
    lead = FakeLead(strength=None)
    lead.priority_score = None

    raised = None
    try:
        estimate_value(lead, ExplodingClient(), "test-model")
    except AnalysisError as e:
        raised = e
    except AssertionError as e:          # the client fired: guard is missing
        check("refuses before making an API call", False)
        print("        " + str(e))
        return

    check("raises AnalysisError instead of scoring", raised is not None)
    check("priority_score is left NULL", lead.priority_score is None)
    check("the reason names the missing component",
          raised is not None and "strength_score" in str(raised))


def test_stage3_only_runs_on_stage2_survivors():
    """
    The loop-level half of the same rule. Stage 3 must iterate the list built
    from successful Stage-2 leads, not the full candidate list — otherwise the
    guard above turns every Stage-2 failure into a logged Stage-3 error.
    """
    print("\n### the pipeline does not hand Stage-2 failures to Stage 3")
    src = pathlib.Path(ROOT, "main.py").read_text(encoding="utf-8")

    stage3 = re.search(
        r"# 3\. STAGE 3 — VALUE ESTIMATION(.*?)# 3\.5", src, re.S,
    )
    check("Stage 3 block still exists in the scan pipeline", stage3 is not None)
    if not stage3:
        return
    body = stage3.group(1)
    check("Stage 3 iterates the Stage-2 survivors (leads_scored)",
          "for lead in leads_scored" in body)
    check("Stage 3 no longer iterates every candidate",
          "for lead, _, _ in leads_for_analysis" not in body)
    check("estimate_value is still called from the live scan",
          "estimate_value(lead" in body)

    stage2 = re.search(r"# 3\. STAGE 2 — DEEP ANALYSIS(.*?)# 3\. STAGE 3", src, re.S)
    check("Stage 2 collects survivors only after a successful analyze()",
          stage2 is not None
          and "leads_scored.append(lead)" in stage2.group(1)
          and stage2.group(1).index("continue")
              < stage2.group(1).index("leads_scored.append(lead)"))


# ── 2. already_filed_il ranks, not buries ──────────────

def test_already_filed_has_no_score_effect():
    print("\n### already_filed_il is a tag, not a score input")
    profiles = [
        ("top lead",    10, 60_000_000),
        ("strong lead",  8, 20_000_000),
        ("mid lead",     7, 20_000_000),
        ("weak lead",    4,  1_000_000),
    ]
    for label, strength, value in profiles:
        filed = _compute_priority_score(FakeLead(strength, filed=True), value, True)
        clean = _compute_priority_score(FakeLead(strength, filed=False), value, True)
        check(f"{label}: flagged scores identically to unflagged "
              f"({filed} == {clean})", filed == clean)

    top_filed = _compute_priority_score(FakeLead(10, filed=True), 60_000_000, True)
    check(f"a flagged lead can still reach the alert threshold "
          f"({top_filed} >= {HIGH_PRIORITY_THRESHOLD})",
          top_filed >= HIGH_PRIORITY_THRESHOLD)

    check("no already-filed multiplier is left in the module",
          not hasattr(ve, "ALREADY_FILED_MULTIPLIER"))
    src = pathlib.Path(ROOT, "analysis", "value_estimator.py").read_text(encoding="utf-8")
    scorer = src[src.index("def _compute_priority_score"):]
    scorer = scorer[:scorer.index("\ndef ")]
    check("the scorer does not read already_filed_il at all",
          "already_filed_il" not in scorer.replace(
              "# already_filed_il is intentionally absent here", ""))


def test_non_israeli_suppression_is_untouched():
    """The 0.3 non-Israeli factor is a different call and stays as it was."""
    print("\n### the non-Israeli suppression is unchanged")
    il = _compute_priority_score(FakeLead(9), 20_000_000, True)
    non_il = _compute_priority_score(FakeLead(9), 20_000_000, False)
    check("a lead with no Israeli nexus is still suppressed", non_il < il)


def main():
    test_no_strength_score_is_refused()
    test_stage3_only_runs_on_stage2_survivors()
    test_already_filed_has_no_score_effect()
    test_non_israeli_suppression_is_untouched()

    print()
    if FAILURES:
        print("FAILED (%d):" % len(FAILURES))
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
