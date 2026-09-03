#!/usr/bin/env python3
"""
Read-only scoring diagnostic — answers "are new leads actually being scored?"

Run it in the Render Shell so it reads /var/data/scout.db:
  python scripts/diagnose_scoring.py            # last 30 days
  python scripts/diagnose_scoring.py --days 7

Writes nothing. It reports, per ingest day, how far each lead got down the
pipeline (Stage 1 -> Stage 2 -> Stage 3), which is the only way to tell a
"never analysed" lead apart from a "analysed and genuinely scored low" one:

  Stage 1 ran  -> relevance_score IS NOT NULL
  Stage 2 ran  -> strength_score IS NOT NULL   (legal_analysis populated)
  Stage 3 ran  -> priority_score IS NOT NULL   (israel_applicable populated)

A day with leads but zero Stage-2/Stage-3 rows means the deep pipeline never
reached them (dedup merge, relevance gate, or an interrupted run). A day with
full Stage-3 coverage but no score >= 7 is a calibration result, not a wiring
failure.
"""
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import func

from config.settings import (
    DATABASE_URL, DATABASE_PATH, MIN_RELEVANCE_SCORE, HIGH_PRIORITY_THRESHOLD,
)
from database.models import init_database, get_session, Lead


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="lookback window (default 30)")
    args = ap.parse_args()

    init_database(DATABASE_URL)
    db = get_session(DATABASE_URL)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).replace(tzinfo=None)

    print(f"\nDB: {DATABASE_PATH}")
    print(f"Window: last {args.days} days (scraped_at >= {since:%Y-%m-%d})")
    print(f"MIN_RELEVANCE_SCORE={MIN_RELEVANCE_SCORE}  HIGH_PRIORITY_THRESHOLD={HIGH_PRIORITY_THRESHOLD}")

    # ── Per-day pipeline depth ────────────────────────────────────────────────
    day = func.date(Lead.scraped_at)
    rows = (
        db.query(
            day.label("d"),
            func.count(Lead.id),
            func.sum(func.coalesce(Lead.is_duplicate_of_known, 0)),
            func.count(Lead.relevance_score),
            func.count(Lead.strength_score),
            func.count(Lead.priority_score),
            func.count(Lead.score_reasoning),
            func.max(Lead.priority_score),
        )
        .filter(Lead.scraped_at >= since)
        .group_by(day)
        .order_by(day)
        .all()
    )

    print(f"\n{'date':<12}{'leads':>6}{'merged':>8}{'stage1':>8}{'stage2':>8}"
          f"{'stage3':>8}{'reasons':>9}{'max score':>11}")
    print("-" * 70)
    for d, total, merged, s1, s2, s3, sr, mx in rows:
        print(f"{str(d):<12}{total:>6}{int(merged or 0):>8}{s1:>8}{s2:>8}"
              f"{s3:>8}{sr:>9}{(f'{mx:.1f}' if mx is not None else '-'):>11}")

    # ── Where the funnel loses leads ──────────────────────────────────────────
    win = db.query(Lead).filter(Lead.scraped_at >= since)
    total = win.count()
    merged = win.filter(Lead.is_duplicate_of_known == True).count()  # noqa: E712
    below = win.filter(Lead.relevance_score < MIN_RELEVANCE_SCORE).count()
    eligible = win.filter(
        Lead.relevance_score >= MIN_RELEVANCE_SCORE,
        (Lead.is_duplicate_of_known.is_(None)) | (Lead.is_duplicate_of_known == False),
    )
    elig_n = eligible.count()

    print(f"\nFunnel over {total} lead(s) in window:")
    print(f"  merged as duplicates .............. {merged}")
    print(f"  relevance < {MIN_RELEVANCE_SCORE} (Stage 1 gate) ....... {below}")
    print(f"  eligible for Stage 2/3 ............ {elig_n}")
    print(f"    missing legal_analysis .......... {eligible.filter((Lead.legal_analysis.is_(None)) | (Lead.legal_analysis == '')).count()}")
    print(f"    missing strength_score (S2) ..... {eligible.filter(Lead.strength_score.is_(None)).count()}")
    print(f"    missing priority_score (S3) ..... {eligible.filter(Lead.priority_score.is_(None)).count()}")
    print(f"    missing israel_applicable (S3) .. {eligible.filter(Lead.israel_applicable.is_(None)).count()}")
    print(f"    missing score_reasoning (S3) .... {eligible.filter(Lead.score_reasoning.is_(None)).count()}")
    print(f"    already_filed_il = True ......... {eligible.filter(Lead.already_filed_il == True).count()}")  # noqa: E712
    print(f"    israel_applicable = False ....... {eligible.filter(Lead.israel_applicable == False).count()}")  # noqa: E712

    # ── Score + confidence distribution among leads Stage 3 actually reached ──
    scored = eligible.filter(Lead.priority_score.isnot(None))
    print(f"\nScored leads in window: {scored.count()}")
    for lo, hi in ((0, 3), (3, 5), (5, 7), (7, 8.5), (8.5, 10.01)):
        n = scored.filter(Lead.priority_score >= lo, Lead.priority_score < hi).count()
        print(f"  {lo:>4}-{hi:<5} {n}")
    print("  by value_confidence (the 0.6/0.8/1.0 multiplier — 'low' can never reach 7):")
    for conf, n in (
        db.query(Lead.value_confidence, func.count(Lead.id))
        .filter(Lead.scraped_at >= since, Lead.priority_score.isnot(None))
        .group_by(Lead.value_confidence).all()
    ):
        print(f"    {str(conf or 'NULL'):<8} {n}")
    print(f"  matches_expertise=True .......... "
          f"{scored.filter(Lead.matches_expertise == True).count()}")  # noqa: E712

    # ── Fingerprint of the Aug-11..Aug-30 silent-truncation window ────────────
    #
    # In that window the Stage-3 response was cut at max_tokens=1100 and the old
    # parser returned {} without raising. The lead was then scored from no data:
    # value_confidence defaults to "low", every figure is 0 (so the value
    # component is 0.0), and score_reasoning is never written — while
    # priority_score IS written and looks like a real verdict.
    #
    #   ceiling in that state = (0 + 10 + 10)/3 * 0.6 = 4.0
    #
    # So this exact combination is a lead that was never really scored, and it
    # is invisible unless you look for it.
    ghost = db.query(Lead).filter(
        Lead.priority_score.isnot(None),
        Lead.score_reasoning.is_(None),
        Lead.value_confidence == "low",
    )
    n_ghost = ghost.count()
    n_ghost_win = ghost.filter(Lead.scraped_at >= since).count()
    print(f"\nScored-from-a-truncated-response (priority_score set, score_reasoning "
          f"NULL,\n  value_confidence='low' — capped at 4.0 by construction): "
          f"{n_ghost} total, {n_ghost_win} in window")
    if n_ghost:
        worst = ghost.order_by(Lead.priority_score.desc()).first()
        print(f"  highest such score: {worst.priority_score} (#{worst.id}) — "
              f"these need scripts/reanalyze_leads.py --force")

    # ── Last lead that actually cleared the alert bar ─────────────────────────
    top = (
        db.query(Lead)
        .filter(Lead.priority_score >= HIGH_PRIORITY_THRESHOLD)
        .order_by(Lead.scraped_at.desc())
        .first()
    )
    if top:
        print(f"\nMost recent lead >= {HIGH_PRIORITY_THRESHOLD} (whole table): "
              f"#{top.id} {top.scraped_at:%Y-%m-%d} score={top.priority_score} "
              f"— {top.title[:60]}")
    else:
        print(f"\nNo lead in the table has ever reached {HIGH_PRIORITY_THRESHOLD}.")
    print()


if __name__ == "__main__":
    main()
