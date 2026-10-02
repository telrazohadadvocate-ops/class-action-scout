#!/usr/bin/env python3
"""
DRY report — leads that were blocked by mistake, with a targeted restoration
proposal. Opens the database READ-ONLY and writes nothing to it.

Categories
  self_duplicate      flagged a duplicate of ITSELF (the Aug-14 bug). Proven by
                      record ids — the dedup note names the lead's own id, or its
                      dedup_group_id is its own id with no firm known-case behind
                      the flag. known_case_ref == title alone is NOT treated as
                      proof: a genuine duplicate of another lead looks the same.
  genuine_duplicate   points at a DIFFERENT record — left alone.
  manual_decision     dup_review is merged/separate — a human call, left alone.
  known_case          flagged by the firm KNOWN_CASES check, not by dedup. Listed
                      with the word that matched, because generic words
                      ("class", "action", "google", "settlement") match far too
                      much. Review by hand; no automatic proposal.
  stage1_suspect      relevance_score = 0 with empty reasoning — the signature of
                      a Stage-1 technical failure stored as a verdict. Cannot be
                      proven from the row; proposal is to mark it for retry.

Usage (Render Shell):
  python scripts/report_blocked_leads.py
  python scripts/report_blocked_leads.py --out /var/data/blocked_proposal.csv
  python scripts/report_blocked_leads.py --db path/to/scout.db --sample 20
"""
import os
import re
import sys
import csv
import argparse
import sqlite3

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from config.settings import DATABASE_PATH, MIN_RELEVANCE_SCORE, KNOWN_CASES
from analysis.dedup import title_match_key

NOTE_ID = re.compile(r"כפילות של ליד #(\d+)")
MANUAL = {"merged", "separate"}
GENERIC_WORDS = {"class", "action", "settlement", "lawsuit", "claims", "google",
                 "meta", "amazon", "consumer", "privacy", "pricing"}
KNOWN_NAMES = {k["name"] for k in KNOWN_CASES}


def known_case_word(company, title, content, known_name):
    """Re-derive which topic word made _check_known_cases match (for review)."""
    kl = known_name.lower()
    topic = kl.split("—")[1].strip() if "—" in kl else ""
    text = f"{title} {content or ''}".lower()
    words = [w for w in topic.split() if len(w) > 3 and w in text]
    return words[0] if words else ("<company only>" if not topic else "")


def classify_row(r, canon_by_key):
    lid = r["id"]
    notes = r.get("notes") or ""
    note_ids = [int(x) for x in NOTE_ID.findall(notes)]
    group_self = (r.get("dedup_group_id") or "") == str(lid)

    if (r.get("dup_review") or "") in MANUAL:
        return "manual_decision", f"dup_review={r['dup_review']}", ""
    if (r.get("known_case_ref") or "") in KNOWN_NAMES:
        w = known_case_word(r.get("company"), r.get("title"), r.get("raw_content"), r["known_case_ref"])
        flag = " — GENERIC MATCH, likely false positive" if w in GENERIC_WORDS else ""
        return "known_case", f"matched '{w}' in {r['known_case_ref']}{flag}", "review by hand"
    if lid in note_ids or (group_self and not note_ids):
        evidence = []
        if lid in note_ids:
            evidence.append(f"note names its own id #{lid}")
        if group_self:
            evidence.append(f"dedup_group_id = own id {lid}")
        other = [i for i in note_ids if i != lid]
        if other:
            evidence.append(f"note also names #{other}")
        key = title_match_key(r.get("title"), r.get("company"))
        canon = canon_by_key.get(key) if key else None
        if canon is not None and canon != lid:
            proposal = f"re-point to #{canon} (genuine identical-title lead)"
        elif (r.get("relevance_score") or 0) >= MIN_RELEVANCE_SCORE:
            proposal = ("unmerge (is_duplicate_of_known=0, dedup_group_id=NULL, "
                        "known_case_ref=NULL if = title, drop self note) + targeted Stage 2/3")
        else:
            proposal = "unmerge only (below gate — no analysis)"
        return "self_duplicate", "; ".join(evidence), proposal
    return "genuine_duplicate", f"points at #{note_ids or r.get('dedup_group_id')}", "keep"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DATABASE_PATH))
    ap.add_argument("--out", default="", help="write the full proposal as CSV here")
    ap.add_argument("--sample", type=int, default=10)
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    want = ["id", "title", "company", "relevance_score", "relevance_reasoning",
            "strength_score", "is_duplicate_of_known", "known_case_ref",
            "dedup_group_id", "dup_review", "notes", "scraped_at", "raw_content"]
    sel = ", ".join(c for c in want if c in cols)
    rows = [dict(r) for r in con.execute(f"SELECT {sel} FROM leads")]

    canon_by_key = {}
    for r in sorted(rows, key=lambda x: x["id"]):
        if not r.get("is_duplicate_of_known") and (r.get("relevance_score") or 0) >= MIN_RELEVANCE_SCORE:
            k = title_match_key(r.get("title"), r.get("company"))
            if k and k not in canon_by_key:
                canon_by_key[k] = r["id"]

    out = []
    for r in rows:
        if r.get("is_duplicate_of_known"):
            cat, evidence, proposal = classify_row(r, canon_by_key)
            out.append((cat, r, evidence, proposal))
        rel, reason = r.get("relevance_score"), (r.get("relevance_reasoning") or "").strip()
        if rel == 0 and not reason:
            out.append(("stage1_suspect", r, "relevance_score=0, empty reasoning",
                        "mark for Stage-1 retry: relevance_score=NULL, "
                        "stage1_error='legacy: score 0, empty reasoning', stage1_attempts=0"))

    print(f"DB (read-only): {args.db}   leads: {len(rows)}   gate: {MIN_RELEVANCE_SCORE}")
    print("Nothing is written. This is a proposal only.\n")
    order = ["self_duplicate", "stage1_suspect", "known_case", "manual_decision", "genuine_duplicate"]
    for cat in order:
        items = [o for o in out if o[0] == cat]
        gate = sum(1 for _, r, _, _ in items if (r.get("relevance_score") or 0) >= MIN_RELEVANCE_SCORE)
        print(f"== {cat}: {len(items)}" + (f"  ({gate} at or above the gate)" if cat == "self_duplicate" else ""))
        for _, r, ev, prop in items[: args.sample]:
            print(f"   #{r['id']} [{r.get('relevance_score')}] {str(r.get('scraped_at'))[:10]} "
                  f"{(r.get('title') or '')[:60]}")
            print(f"      evidence: {ev}")
            print(f"      proposal: {prop}")
        if len(items) > args.sample:
            print(f"   ... {len(items) - args.sample} more")
        print()

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["category", "id", "relevance_score", "scraped_at", "title", "evidence", "proposal"])
            for cat, r, ev, prop in out:
                w.writerow([cat, r["id"], r.get("relevance_score"), r.get("scraped_at"),
                            r.get("title"), ev, prop])
        print(f"Full proposal written to {args.out}")


if __name__ == "__main__":
    main()
