#!/usr/bin/env python3
"""
Backfill Voyage AI embeddings for existing leads and run semantic deduplication.

Usage:
  python scripts/backfill_embeddings.py [--dry-run] [--threshold 0.85]

Processes all leads without a dedup_group_id, computes embeddings in batches,
clusters them, and marks duplicates.  Run once after deploying the dedup feature.
"""
import os, sys, json, argparse
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import DATABASE_URL, VOYAGE_API_KEY, DEDUP_THRESHOLD
from database.models import init_database, get_session, Lead
from analysis.dedup import SemanticDeduplicator

BATCH_SIZE = 128


def main():
    parser = argparse.ArgumentParser(description="Backfill semantic embeddings and dedup existing leads")
    parser.add_argument("--dry-run", action="store_true", help="Compute and report without writing to DB")
    parser.add_argument("--threshold", type=float, default=None, help="Override dedup threshold (default from env)")
    args = parser.parse_args()

    threshold = args.threshold if args.threshold is not None else DEDUP_THRESHOLD

    print(f"\n{'='*60}")
    print(f"Backfill Embeddings — Semantic Deduplication")
    print(f"Threshold: {threshold}")
    print(f"Dry run:   {args.dry_run}")
    print(f"{'='*60}\n")

    dedup = SemanticDeduplicator(api_key=VOYAGE_API_KEY, threshold=threshold)
    if not dedup.enabled:
        print("ERROR: VOYAGE_API_KEY not set or voyageai package not installed.")
        print("Set VOYAGE_API_KEY in your .env file and run: pip install voyageai")
        sys.exit(1)

    init_database(DATABASE_URL)
    db = get_session(DATABASE_URL)

    # All leads without a dedup_group_id, ordered by id (oldest first = canonical)
    leads = db.query(Lead).filter(Lead.dedup_group_id.is_(None)).order_by(Lead.id).all()
    print(f"Leads without dedup_group_id: {len(leads)}")

    if not leads:
        print("Nothing to do.")
        return

    # Build texts for batch embedding
    texts = [
        f"{(l.company or '')} | {l.title} | {(l.israeli_law_basis or '')}"
        for l in leads
    ]

    # Compute embeddings in batches
    print(f"Computing embeddings in batches of {BATCH_SIZE}...")
    all_embeddings = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        embs = dedup.compute_embeddings_batch(batch)
        all_embeddings.extend(embs)
        print(f"  {min(i + BATCH_SIZE, len(texts))}/{len(texts)} embeddings computed")

    # Cluster: first lead in each cluster = canonical
    canonical = []   # list of (lead, embedding)
    n_unique = 0
    n_duplicates = 0
    duplicate_pairs = []

    for lead, emb in zip(leads, all_embeddings):
        if not emb:
            # Embedding failed — treat as unique
            if not args.dry_run:
                lead.dedup_group_id = str(lead.id)
            n_unique += 1
            continue

        match, score = dedup.find_duplicate(emb, canonical)
        if match:
            duplicate_pairs.append((lead, match, score))
            if not args.dry_run:
                lead.is_duplicate_of_known = True
                lead.dedup_group_id = match.dedup_group_id or str(match.id)
                lead.known_case_ref = match.title
                note = f"🔁 כפילות של ליד #{match.id} (דמיון {score:.0%})"
                if not lead.notes or note not in lead.notes:
                    lead.notes = (lead.notes + "\n" if lead.notes else "") + note
            n_duplicates += 1
        else:
            if not args.dry_run:
                lead.embedding = json.dumps(emb)
                lead.dedup_group_id = str(lead.id)
            canonical.append((lead, emb))
            n_unique += 1

    # Summary
    print(f"\n{'─'*60}")
    print(f"Results:")
    print(f"  Unique (canonical):  {n_unique}")
    print(f"  Duplicates merged:   {n_duplicates}")
    print(f"{'─'*60}\n")

    if duplicate_pairs:
        print("Duplicate pairs found:")
        for dup, canon, score in duplicate_pairs:
            print(f"  [{score:.0%}] #{dup.id} \"{dup.title[:50]}\"")
            print(f"         ≈ #{canon.id} \"{canon.title[:50]}\"")
            print()

    if args.dry_run:
        print("DRY RUN — no changes written to database.")
    else:
        db.commit()
        print(f"Done. {n_unique} leads marked canonical, {n_duplicates} duplicates merged.")


if __name__ == "__main__":
    main()
