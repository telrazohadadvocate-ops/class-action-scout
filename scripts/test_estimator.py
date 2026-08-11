#!/usr/bin/env python3
"""
Dry-run test: estimate_value() on 3 sample leads, one at a time.
Does NOT write to the database.
"""
import os, sys
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
from config.settings import DATABASE_URL, ANTHROPIC_API_KEY, CLAUDE_MODEL
from database.models import init_database, get_session, Lead
from analysis.value_estimator import estimate_value, nis_bucket


def pick_leads(db):
    """Return 2 Israeli-nexus leads + 1 US-only lead."""
    il = (
        db.query(Lead)
        .filter(Lead.strength_score.isnot(None))
        .filter(Lead.operates_in_israel == True)
        .order_by(Lead.id.desc())
        .limit(2)
        .all()
    )
    # US lead: no strength_score requirement — many haven't been through Stage 2
    us = (
        db.query(Lead)
        .filter(Lead.operates_in_israel == False)
        .order_by(Lead.id.desc())
        .limit(1)
        .all()
    )
    return il[:2] + us[:1]


def run_one(lead, client):
    """Run estimate_value on a single lead, then restore original values."""
    orig = {k: getattr(lead, k) for k in (
        "value_low", "value_high", "est_class_size", "est_damage_per_member",
        "priority_score", "value_confidence", "value_reasoning", "israel_applicable",
    )}
    try:
        estimate_value(lead, client, CLAUDE_MODEL)
        bucket = nis_bucket(lead.value_low, lead.value_high)
        print(f"  israel_applicable : {lead.israel_applicable}")
        print(f"  confidence        : {lead.value_confidence}")
        print(f"  value bucket      : {bucket or '(none)'}")
        print(f"  priority_score    : {lead.priority_score}")
        print(f"  reasoning         : {(lead.value_reasoning or '')[:140]}")
    finally:
        for k, v in orig.items():
            setattr(lead, k, v)


def main():
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    init_database(DATABASE_URL)
    db = get_session(DATABASE_URL)

    leads = pick_leads(db)
    if not leads:
        print("No leads with strength_score found.")
        return

    print(f"\nModel: {CLAUDE_MODEL}  |  DB: {DATABASE_URL}\n")

    for i, lead in enumerate(leads, 1):
        label = "Israeli" if lead.operates_in_israel else ("US-only" if lead.operates_in_israel is False else "unknown")
        print(f"[{i}/{len(leads)}] Lead #{lead.id} [{label}] — {lead.title[:70]}")
        sys.stdout.flush()
        run_one(lead, client)
        print()
        sys.stdout.flush()

    db.expunge_all()
    print("Done — no DB changes written.")


if __name__ == "__main__":
    main()
