#!/usr/bin/env python3
"""
Re-run Stage 2 (deep legal analysis) + Stage 3 (value estimation) over EXISTING
leads, to backfill fields added after they were first analysed:
  - already_filed_il / already_filed_details   (Stage 2)
  - score_reasoning + recomputed strength/priority/priority_score (Stage 2 + 3)

Batched and resumable — commits after every batch, so an interruption only loses
the in-flight batch. In default (non-force) mode it processes only leads that are
MISSING the new fields, so simply re-running continues where it left off.

Usage (run in the Render Shell so it hits /var/data/scout.db):
  python scripts/reanalyze_leads.py --limit 3          # test on 3 leads first
  python scripts/reanalyze_leads.py                    # backfill the gaps
  python scripts/reanalyze_leads.py --force            # re-run ALL qualifying leads
  python scripts/reanalyze_leads.py --dry-run --limit 3   # no DB writes

Flags:
  --force / --all   re-run every deep-analysis lead, even if already populated
  --limit N         process at most N leads (test small before the full run)
  --dry-run         compute + log, but roll back (no DB writes)
  --batch-size N    leads committed per batch (default 10)
"""
import os, sys, json, time, argparse
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic

from config.settings import (
    DATABASE_URL, ANTHROPIC_API_KEY, CLAUDE_MODEL, MIN_RELEVANCE_SCORE,
)
from database.models import init_database, get_session, Lead
from analysis.claude_analyzer import ClaudeAnalyzer
from analysis.value_estimator import estimate_value

BATCH_SIZE            = 10    # leads per commit
SLEEP_BETWEEN_CALLS   = 0.6   # seconds between AI calls (politeness / rate limits)
SLEEP_BETWEEN_BATCHES = 5     # extra pause between batches


def build_classification(lead) -> dict:
    """Reconstruct the Stage-1 classification dict from stored lead columns."""
    return {
        "relevance_score": lead.relevance_score,
        "company": lead.company or "",
        "sector": lead.sector or "",
        "operates_in_israel": lead.operates_in_israel,
        "israeli_law_basis": lead.israeli_law_basis or "",
        "estimated_class_size": lead.estimated_class_size or "",
        "reasoning": lead.relevance_reasoning or "",
    }


def select_leads(db, force: bool, limit):
    """Leads that qualify for deep analysis; in non-force mode, only the gaps."""
    q = db.query(Lead).filter(Lead.relevance_score >= MIN_RELEVANCE_SCORE)
    if not force:
        q = q.filter(
            (Lead.already_filed_il.is_(None)) | (Lead.score_reasoning.is_(None))
        )
    q = q.order_by(Lead.id)
    if limit:
        q = q.limit(limit)
    return q.all()


def reanalyze_one(lead, analyzer, client):
    """Stage 2 then Stage 3 on a single lead (mirrors main.py pipeline order)."""
    analysis = analyzer.analyze(
        title=lead.title,
        content=lead.raw_content or "",
        classification=build_classification(lead),
    )
    lead.legal_analysis = analysis.get("legal_analysis", "")
    lead.strength_score = analysis.get("strength_score", 0)
    lead.priority = analysis.get("priority", "low")
    lead.recommended_action = analysis.get("recommended_action", "")
    lead.comparable_cases = json.dumps(
        analysis.get("comparable_cases", []), ensure_ascii=False
    )
    lead.already_filed_il = bool(analysis.get("already_filed_il", False))
    lead.already_filed_details = analysis.get("already_filed_details", "") or ""

    time.sleep(SLEEP_BETWEEN_CALLS)  # gap between the two AI calls

    # Stage 3 reads the fresh Stage-2 fields we just set (legal_analysis,
    # strength_score, already_filed_il) and writes value_* + score_reasoning.
    estimate_value(lead, client, CLAUDE_MODEL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", "--all", dest="force", action="store_true",
                    help="re-run every deep-analysis lead, even if populated")
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N leads")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + log but do not write to the DB")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set. Set it in .env or environment.")
        sys.exit(1)

    init_database(DATABASE_URL)
    db = get_session(DATABASE_URL)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    analyzer = ClaudeAnalyzer(api_key=ANTHROPIC_API_KEY, model=CLAUDE_MODEL)

    leads = select_leads(db, args.force, args.limit)
    total = len(leads)

    print(f"\n{'='*64}")
    print(f"Re-analyze leads — Stage 2 (legal) + Stage 3 (value)")
    print(f"Model:      {CLAUDE_MODEL}")
    print(f"Mode:       {'FORCE (all qualifying)' if args.force else 'gaps only (missing new fields)'}")
    print(f"Limit:      {args.limit or 'none'}")
    print(f"Dry run:    {args.dry_run}")
    print(f"To process: {total}")
    print(f"{'='*64}\n")

    if total == 0:
        print("Nothing to process.")
        return

    # Snapshot pre-run priority_score for the before/after summary (immutable floats)
    before_scores = {l.id: l.priority_score for l in leads}

    batches = [leads[i:i + args.batch_size] for i in range(0, total, args.batch_size)]
    done = errors = 0

    for bi, batch in enumerate(batches, start=1):
        print(f"Batch {bi}/{len(batches)} ({len(batch)} leads)...")
        for lead in batch:
            try:
                reanalyze_one(lead, analyzer, client)
                done += 1
                print(f"  [{done}/{total}] #{lead.id} {(lead.company or '?')[:20]:20} "
                      f"strength={lead.strength_score} priority={lead.priority} "
                      f"filed={lead.already_filed_il} score={lead.priority_score}")
            except Exception as e:
                errors += 1
                print(f"  [ERR] #{lead.id}: {e}")
            time.sleep(SLEEP_BETWEEN_CALLS)

        if args.dry_run:
            print(f"  (dry-run) batch {bi} not committed")
        else:
            db.commit()
            print(f"  ✓ committed batch {bi}/{len(batches)}")

        if bi < len(batches):
            time.sleep(SLEEP_BETWEEN_BATCHES)

    # ── Summary (compute BEFORE any rollback — db.rollback() expires the
    #    in-memory objects and would re-read the old values) ──────────────
    from collections import Counter

    def _bucket(s):
        if s is None:
            return "none"
        if s < 3:   return "1-3"
        if s < 5:   return "3-5"
        if s < 7:   return "5-7"
        if s < 8.5: return "7-8.5"
        return "8.5-10"

    order = ["1-3", "3-5", "5-7", "7-8.5", "8.5-10", "none"]
    filed = sum(1 for l in leads if l.already_filed_il)
    with_reason = sum(1 for l in leads if l.score_reasoning)
    before_dist = Counter(_bucket(before_scores.get(l.id)) for l in leads)
    after_dist = Counter(_bucket(l.priority_score) for l in leads)

    print(f"\n{'='*64}")
    print(f"Done. Processed: {done}  Errors: {errors}"
          + ("   (DRY RUN — nothing written)" if args.dry_run else ""))
    print(f"  already_filed_il = True:    {filed}/{len(leads)}")
    print(f"  score_reasoning populated:  {with_reason}/{len(leads)}")
    print(f"  priority_score distribution (before -> after):")
    for b in order:
        bd, ad = before_dist.get(b, 0), after_dist.get(b, 0)
        if bd or ad:
            print(f"    {b:>7}: {bd:>3} -> {ad:>3}")
    print(f"{'='*64}\n")

    if args.dry_run:
        db.rollback()


if __name__ == "__main__":
    main()
