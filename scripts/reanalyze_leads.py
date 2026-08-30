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
  python scripts/reanalyze_leads.py --force --first-alert-cap 15  # cap the backlog email

Merged duplicates are skipped — the alert filter drops them, so re-scoring one
can never produce an alert.

Flags:
  --force / --all      re-run every deep-analysis lead, even if already populated
  --limit N            process at most N leads (test small before the full run)
  --dry-run            compute + log, but roll back (no DB writes)
  --batch-size N       leads committed per batch (default 10)
  --first-alert-cap N  after re-scoring, leave only the top N un-alerted leads over
                       the threshold and mark the rest as already alerted, so the
                       next scan emails a digest instead of the whole backlog.
                       Safe to pass on every chunk of a --limit'd run.
"""
import os, sys, json, time, argparse
from datetime import datetime
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic

from config.settings import (
    DATABASE_URL, ANTHROPIC_API_KEY, CLAUDE_MODEL, MIN_RELEVANCE_SCORE,
    HIGH_PRIORITY_THRESHOLD,
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
    # Merged duplicates are excluded: the alert filter drops them, so re-scoring
    # one can never produce an alert — it is spend with no possible output.
    q = db.query(Lead).filter(
        Lead.relevance_score >= MIN_RELEVANCE_SCORE,
        (Lead.is_duplicate_of_known.is_(None)) | (Lead.is_duplicate_of_known == False),
    )
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


def cap_first_alert(db, cap: int) -> tuple:
    """
    Keep only the top `cap` un-alerted leads over the threshold; mark the rest as
    already alerted so the next scan does not email the entire backlog at once.

    Applied over the whole table, not just this run's batch, and against the same
    predicate _send_run_alerts uses — so running it after each chunk of a
    --limit'd re-score always leaves exactly the global top `cap` un-alerted.
    """
    pending = (
        db.query(Lead)
        .filter(
            Lead.priority_score >= HIGH_PRIORITY_THRESHOLD,
            Lead.alerted_at.is_(None),
            (Lead.is_duplicate_of_known.is_(None)) | (Lead.is_duplicate_of_known == False),
        )
        .order_by(Lead.priority_score.desc(), Lead.id.desc())
        .all()
    )
    suppressed = pending[cap:]
    stamped = datetime.utcnow()
    for lead in suppressed:
        lead.alerted_at = stamped
    db.commit()
    return len(pending) - len(suppressed), len(suppressed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", "--all", dest="force", action="store_true",
                    help="re-run every deep-analysis lead, even if populated")
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N leads")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + log but do not write to the DB")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--first-alert-cap", type=int, default=None, metavar="N",
                    help="after re-scoring, leave only the top N un-alerted leads "
                         "over the threshold and mark the rest as already alerted, "
                         "so the next scan does not email the whole backlog")
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
        if args.first_alert_cap is not None and not args.dry_run:
            kept, suppressed = cap_first_alert(db, args.first_alert_cap)
            print(f"First-alert cap {args.first_alert_cap}: {kept} lead(s) left for the "
                  f"next scan to email, {suppressed} marked as already alerted.")
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

    if args.first_alert_cap is not None:
        if args.dry_run:
            print(f"(dry-run) first-alert cap {args.first_alert_cap} not applied")
        else:
            kept, suppressed = cap_first_alert(db, args.first_alert_cap)
            print(f"First-alert cap {args.first_alert_cap}: {kept} lead(s) left for the "
                  f"next scan to email, {suppressed} marked as already alerted.")

    if args.dry_run:
        db.rollback()


if __name__ == "__main__":
    main()
