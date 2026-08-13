#!/usr/bin/env python3
"""
Tests for the exact-title auto-merge rule (analysis/dedup.py + cluster_leads).

Identical title + identical company merges regardless of embedding score,
because the embedding text is "company | title | israeli_law_basis" and a
differing law-basis field alone dragged byte-identical headlines below the
0.87 auto-merge floor and into the manual review queue.

Usage:  python tests/test_title_merge_rule.py
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from analysis.dedup import normalize_for_match, title_match_key, MIN_TITLE_KEY_LEN
from scripts.backfill_embeddings import cluster_leads

FAILURES = []


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILURES.append(label)


class FakeLead:
    """Stand-in for the Lead model — cluster_leads only touches these fields."""
    def __init__(self, lid, title, company=None):
        self.id = lid
        self.title = title
        self.company = company
        self.notes = None
        self.dedup_group_id = None
        self.is_duplicate_of_known = None
        self.known_case_ref = None
        self.suspected_dup_of = None
        self.suspected_dup_score = None
        self.dup_review = None


# Two vectors ~0.856 apart: below the 0.87 auto-merge floor, inside the
# [0.82, 0.87) review band. This reproduces the reported calibration problem —
# identical titles that scored 0.85-0.867 and went to manual review.
V_A = [1.0, 0.0, 0.0, 0.0]
V_B = [0.856, 0.517, 0.0, 0.0]     # cos(V_A, V_B) ~= 0.856
V_FAR = [0.0, 0.0, 1.0, 0.0]

AUTO, REVIEW = 0.87, 0.82


def test_normalization():
    print("\n### normalization")
    check("case and whitespace ignored",
          normalize_for_match("Acme  Corp SUED") == normalize_for_match("acme corp sued"))
    check("punctuation stripped",
          normalize_for_match("Acme, Corp. — 'sued'!") == normalize_for_match("Acme Corp sued"))
    check("Hebrew normalizes",
          normalize_for_match("תובענה ייצוגית — סלקום") == normalize_for_match("תובענה ייצוגית סלקום"))
    check("digits kept (they distinguish cases)",
          normalize_for_match("Case 123") != normalize_for_match("Case 456"))
    check("genuinely different titles stay different",
          normalize_for_match("Apple battery throttling")
          != normalize_for_match("Apple App Store fees"))

    print("\n### key construction")
    long_a = "A" * MIN_TITLE_KEY_LEN
    check("same title + same company share a key",
          title_match_key(long_a, "Acme") == title_match_key(long_a.lower(), "acme"))
    check("same title + DIFFERENT company do not",
          title_match_key(long_a, "Acme") != title_match_key(long_a, "Globex"))
    check("short/generic title yields no key (falls through to embeddings)",
          title_match_key("עדכון", "Acme") is None)
    check("empty title yields no key", title_match_key("", "Acme") is None)
    check("None title/company handled", title_match_key(None, None) is None)


def test_clustering():
    T = "Acme Ltd overcharged customers on currency conversion fees"

    print("\n### identical title + company merges below the auto threshold")
    a = FakeLead(1, T, "Acme")
    b = FakeLead(2, T, "Acme")
    res = cluster_leads([(a, V_A), (b, V_B)], [], AUTO, REVIEW, dry_run=False)
    check("scored in the review band, so this is the real scenario",
          REVIEW <= 0.86 < AUTO)
    check("merged by title, not by embedding",
          res["merged_title"] == 1 and res["merged_emb"] == 0)
    check("nothing sent to review", res["review"] == 0)
    check("duplicate joined the canonical's group",
          b.is_duplicate_of_known is True and b.dedup_group_id == "1")
    check("note records the title rule", "כותרת זהה" in (b.notes or ""))

    print("\n### punctuation/case-only differences also merge")
    a = FakeLead(1, T, "Acme")
    b = FakeLead(2, "  acme ltd, OVERCHARGED customers on currency-conversion fees!! ",
                 "acme")
    res = cluster_leads([(a, V_A), (b, V_B)], [], AUTO, REVIEW, dry_run=False)
    check("normalized-equal titles merge", res["merged_title"] == 1)

    print("\n### same title but DIFFERENT company does not title-merge")
    a = FakeLead(1, T, "Acme")
    b = FakeLead(2, T, "Globex")
    res = cluster_leads([(a, V_A), (b, V_B)], [], AUTO, REVIEW, dry_run=False)
    check("falls through to the embedding tiers → review",
          res["merged_title"] == 0 and res["review"] == 1)
    check("review record intact",
          b.dup_review == "pending" and b.suspected_dup_of == 1)

    print("\n### embedding tiers still work for different wording")
    a = FakeLead(1, "Acme overcharged customers on FX fees", "Acme")
    b = FakeLead(2, "Acme hit with suit over hidden currency markups", "Acme")
    res = cluster_leads([(a, V_A), (b, V_B)], [], AUTO, REVIEW, dry_run=False)
    check("0.856 still lands in review, not auto-merge",
          res["review"] == 1 and res["merged"] == 0)

    a = FakeLead(1, "Acme overcharged customers on FX fees", "Acme")
    b = FakeLead(2, "Acme hit with suit over hidden currency markups", "Acme")
    res = cluster_leads([(a, V_A), (b, [0.999, 0.045, 0.0, 0.0])], [], AUTO, REVIEW, False)
    check("high cosine still auto-merges by embedding",
          res["merged_emb"] == 1 and res["merged_title"] == 0)

    a = FakeLead(1, "Acme overcharged customers on FX fees", "Acme")
    b = FakeLead(2, "Globex data breach exposed customer records", "Globex")
    res = cluster_leads([(a, V_A), (b, V_FAR)], [], AUTO, REVIEW, dry_run=False)
    check("unrelated leads stay canonical", res["unique"] == 2)

    print("\n### full titles are compared, not truncated display strings")
    # Identical for the first 60 chars (longer than the 48/60-char display
    # truncations) but different lawsuits.
    shared = "Acme Ltd faces class action in Tel Aviv District Court over "
    a = FakeLead(1, shared + "misleading battery life claims", "Acme")
    b = FakeLead(2, shared + "undisclosed data sharing with third parties", "Acme")
    res = cluster_leads([(a, V_A), (b, V_B)], [], AUTO, REVIEW, dry_run=False)
    check("long shared prefix does NOT title-merge", res["merged_title"] == 0)

    print("\n### title rule respects dry-run and canonical-only indexing")
    a = FakeLead(1, T, "Acme")
    b = FakeLead(2, T, "Acme")
    res = cluster_leads([(a, V_A), (b, V_B)], [], AUTO, REVIEW, dry_run=True)
    check("dry run counts the merge", res["merged_title"] == 1)
    check("dry run mutates nothing",
          b.is_duplicate_of_known is None and b.dedup_group_id is None
          and b.notes is None)

    # A third identical lead must attach to the canonical, not to the duplicate.
    a = FakeLead(1, T, "Acme")
    b = FakeLead(2, T, "Acme")
    c = FakeLead(3, T, "Acme")
    res = cluster_leads([(a, V_A), (b, V_B), (c, V_B)], [], AUTO, REVIEW, False)
    check("three identical titles → one canonical, two merged",
          res["unique"] == 1 and res["merged_title"] == 2)
    check("all point at the same group",
          b.dedup_group_id == "1" and c.dedup_group_id == "1")

    print("\n### a recorded keep-separate decision overrides the title rule")
    seed = FakeLead(10, T, "Acme")
    seed.dedup_group_id = "10"
    seed.dup_review = "separate"      # human said: not the same lawsuit as #11
    seed.suspected_dup_of = 11
    other = FakeLead(11, T, "Acme")
    res = cluster_leads([(other, V_B)], [(seed, V_A)], AUTO, REVIEW, dry_run=False)
    check("title rule does not reverse the human decision",
          res["merged_title"] == 0)
    check("lead stays canonical", other.is_duplicate_of_known is None)

    # ...but a keep-separate against some OTHER lead must not shield it.
    seed = FakeLead(10, T, "Acme")
    seed.dedup_group_id = "10"
    seed.dup_review = "separate"
    seed.suspected_dup_of = 99        # unrelated pair
    other = FakeLead(11, T, "Acme")
    res = cluster_leads([(other, V_B)], [(seed, V_A)], AUTO, REVIEW, dry_run=False)
    check("unrelated keep-separate does not block the merge",
          res["merged_title"] == 1)

    print("\n### seeded canonicals absorb identical titles (incremental path)")
    seed = FakeLead(10, T, "Acme")
    seed.dedup_group_id = "10"
    newb = FakeLead(11, T, "Acme")
    res = cluster_leads([(newb, V_B)], [(seed, V_A)], AUTO, REVIEW, dry_run=False)
    check("new lead merges into the seeded group",
          res["merged_title"] == 1 and newb.dedup_group_id == "10")


def main():
    test_normalization()
    test_clustering()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
