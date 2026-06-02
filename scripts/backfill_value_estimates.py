#!/usr/bin/env python3
"""
Backfill value estimates and priority scores for existing leads.

Processes all leads where priority_score IS NULL (i.e. estimate_value has
not yet run). Resumable — re-running safely skips already-processed leads.

Usage:
  python scripts/backfill_value_estimates.py [--dry-run] [--batch-size 20]
"""
import os, sys, time, argparse
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic

from config.settings import DATABASE_URL, ANTHROPIC_API_KEY, CLAUDE_MODEL
from database.models import init_database, get_session, Lead
from analysis.value_estimator import estimate_value

BATCH_SIZE         = 20   # leads per commit
SLEEP_BETWEEN_CALLS = 1.0  # seconds between AI calls (politeness)
SLEEP_BETWEEN_BATCHES = 5  # extra pause between batches


def main():
    parser = argparse.ArgumentParser(
        description="Backfill value estimates and priority scores for existing leads"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute but do not write to DB")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"Leads per commit (default {BATCH_SIZE})")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set. Set it in .env or environment.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"Backfill Value Estimates")
    print(f"Model:    {CLAUDE_MODEL}")
    print(f"Dry run:  {args.dry_run}")
    print(f"{'='*60}\n")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    init_database(DATABASE_URL)
    db = get_session(DATABASE_URL)

    to_process = (
        db.query(Lead)
        .filter(Lead.priority_score.is_(None))
        .filter(Lead.strength_score.isnot(None))  # only leads that completed Stage 2
        .order_by(Lead.id)
        .all()
    )
    total = len(to_process)
    print(f"Leads missing priority_score (Stage 2 complete): {total}\n")

    if total == 0:
        print("Nothing to backfill.")
        return

    batches = [to_process[i:i + args.batch_size]
               for i in range(0, total, args.batch_size)]
    done = 0
    errors = 0

    for batch_idx, batch in enumerate(batches, start=1):
        print(f"Batch {batch_idx}/{len(batches)} ({len(batch)} leads)...")
        for lead in batch:
            try:
                if not args.dry_run:
                    estimate_value(lead, client, CLAUDE_MODEL)
                else:
                    # Dry-run: still call to show what would be written
                    estimate_value(lead, client, CLAUDE_MODEL)
                    # Then immediately undo
                    lead.value_low = lead.value_high = lead.est_class_size = None
                    lead.est_damage_per_member = lead.priority_score = None
                    lead.value_confidence = lead.value_reasoning = None
                done += 1
                print(f"  [{done}/{total}] #{lead.id} — score={lead.priority_score} "
                      f"value={lead.value_high}")
            except Exception as e:
                errors += 1
                print(f"  [{done}/{total}] #{lead.id} ERROR: {e}")

            time.sleep(SLEEP_BETWEEN_CALLS)

        if not args.dry_run:
            db.commit()
            print(f"  Committed batch {batch_idx}.")

        if batch_idx < len(batches):
            time.sleep(SLEEP_BETWEEN_BATCHES)

    print(f"\n{'='*60}")
    print(f"Done. Processed: {done}  Errors: {errors}")
    if args.dry_run:
        print("DRY RUN — no changes written to database.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
