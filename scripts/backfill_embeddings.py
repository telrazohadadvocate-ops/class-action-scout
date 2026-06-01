#!/usr/bin/env python3
"""
Backfill Voyage AI embeddings for existing leads and run semantic deduplication.

Usage:
  python scripts/backfill_embeddings.py [--dry-run] [--threshold 0.85]

Phase 1 — Embed leads where embedding IS NULL (resumable, rate-limited for free tier).
Phase 2 — Run cosine-similarity clustering to assign dedup_group_id.
"""
import os, sys, json, time, argparse
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import DATABASE_URL, VOYAGE_API_KEY, DEDUP_THRESHOLD
from database.models import init_database, get_session, Lead
from analysis.dedup import SemanticDeduplicator

BATCH_SIZE = 100          # max texts per Voyage API call (free tier allows up to 128)
TEXT_MAX_CHARS = 500      # truncate each lead's text to stay well under 10K TPM
SLEEP_BETWEEN_BATCHES = 25  # seconds — yields ~2-3 RPM (free-tier limit is 3 RPM)
RETRY_SLEEP = 60          # seconds to wait after a RateLimitError before retrying
MAX_RETRIES = 3


def embed_with_retry(dedup, texts):
    """Call compute_embeddings_batch with exponential-ish retry on RateLimitError."""
    import voyageai
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return dedup.compute_embeddings_batch(texts)
        except voyageai.error.RateLimitError:
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_SLEEP * attempt
            print(f"  RateLimitError — sleeping {wait}s then retrying "
                  f"(attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(wait)
    return []  # unreachable


def main():
    parser = argparse.ArgumentParser(
        description="Backfill semantic embeddings and dedup existing leads"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and report without writing to DB")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Override dedup threshold (default from env)")
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

    # ── Phase 1: Compute and save embeddings (resumable) ─────────────────
    to_embed = (
        db.query(Lead)
        .filter(Lead.embedding.is_(None))
        .order_by(Lead.id)
        .all()
    )
    total_to_embed = len(to_embed)
    print(f"Leads without embeddings: {total_to_embed}")

    if to_embed:
        batches = [to_embed[i:i + BATCH_SIZE] for i in range(0, total_to_embed, BATCH_SIZE)]
        n_batches = len(batches)
        total_done = 0

        for batch_idx, batch in enumerate(batches, start=1):
            texts = [
                f"{(l.company or '')} | {l.title} | {(l.israeli_law_basis or '')}"[:TEXT_MAX_CHARS]
                for l in batch
            ]

            embs = embed_with_retry(dedup, texts)

            if not args.dry_run:
                for lead, emb in zip(batch, embs):
                    if emb:
                        lead.embedding = json.dumps(emb)
                db.commit()

            total_done += len(batch)
            print(f"Batch {batch_idx}/{n_batches}: embedded {len(batch)} leads "
                  f"(total done: {total_done}/{total_to_embed})")

            if batch_idx < n_batches:
                print(f"  Sleeping {SLEEP_BETWEEN_BATCHES}s to respect 3 RPM rate limit...")
                time.sleep(SLEEP_BETWEEN_BATCHES)
    else:
        print("All leads already have embeddings — skipping Phase 1.\n")

    # ── Phase 2: Dedup clustering ─────────────────────────────────────────
    leads = (
        db.query(Lead)
        .filter(Lead.dedup_group_id.is_(None))
        .order_by(Lead.id)
        .all()
    )
    print(f"\nLeads without dedup_group_id: {len(leads)}")

    if not leads:
        print("Nothing to cluster.")
        return

    canonical = []   # list of (lead, embedding)
    n_unique = 0
    n_duplicates = 0
    duplicate_pairs = []

    for lead in leads:
        emb = json.loads(lead.embedding) if lead.embedding else []

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
