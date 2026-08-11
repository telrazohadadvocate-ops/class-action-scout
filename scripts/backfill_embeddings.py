#!/usr/bin/env python3
"""
Backfill Voyage AI embeddings for existing leads and run semantic deduplication.

Usage:
  python scripts/backfill_embeddings.py [--dry-run] [--recluster]
                                        [--review-threshold R] [--auto-threshold A]

Phase 1 — Embed leads where embedding IS NULL (resumable, rate-limited for free tier).
Phase 2 — Two-tier clustering:
            similarity >= auto   → auto-merge (duplicate, hidden by default)
            [review, auto)       → flag as SUSPECTED duplicate (dup_review="pending")
            < review             → canonical (unique)
  --recluster re-clusters ALL embedded leads from scratch under the current
  thresholds (use after changing REVIEW/AUTO) and reports merge vs flag counts.
"""
import os, sys, json, time, argparse
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from config.settings import (
    DATABASE_URL, VOYAGE_API_KEY, REVIEW_THRESHOLD, AUTO_MERGE_THRESHOLD,
)
from database.models import init_database, get_session, Lead
from analysis.dedup import SemanticDeduplicator

BATCH_SIZE = 100            # max texts per Voyage API call (free tier allows up to 128)
TEXT_MAX_CHARS = 500        # truncate each lead's text to stay well under 10K TPM
SLEEP_BETWEEN_BATCHES = 25  # seconds — yields ~2-3 RPM (free-tier limit is 3 RPM)
RETRY_SLEEP = 60            # seconds to wait after a RateLimitError before retrying
MAX_RETRIES = 3


def embed_with_retry(dedup, texts):
    """Call compute_embeddings_batch with retry on RateLimitError."""
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


def cluster_leads(embedded_leads, seed_pairs, auto_thr, review_thr, dry_run):
    """
    Greedy, vectorized, two-tier clustering.

    embedded_leads: [(lead, emb_list)] to assign (in id order).
    seed_pairs:     [(lead, emb_list)] already-canonical, to seed the index.

    Mutates each lead (unless dry_run): dedup_group_id, is_duplicate_of_known,
    known_case_ref, suspected_dup_of, suspected_dup_score, dup_review.
    Returns dict(merged, review, unique, merge_pairs, review_pairs).
    """
    n_merged = n_review = n_unique = 0
    merge_pairs, review_pairs = [], []
    if not embedded_leads:
        return dict(merged=0, review=0, unique=0, merge_pairs=[], review_pairs=[])

    new_mat = np.asarray([e for _, e in embedded_leads], dtype=np.float32)
    nn = np.linalg.norm(new_mat, axis=1, keepdims=True)
    nn[nn == 0] = 1e-12
    normed = new_mat / nn
    dim = normed.shape[1]

    canon_mat = np.empty((len(seed_pairs) + len(embedded_leads), dim), dtype=np.float32)
    canon_leads = []
    count = 0
    if seed_pairs:
        pm = np.asarray([e for _, e in seed_pairs], dtype=np.float32)
        pn = np.linalg.norm(pm, axis=1, keepdims=True)
        pn[pn == 0] = 1e-12
        canon_mat[:len(seed_pairs)] = pm / pn
        canon_leads = [l for l, _ in seed_pairs]
        count = len(seed_pairs)

    for i, (lead, _) in enumerate(embedded_leads):
        best_s, best_lead = 0.0, None
        if count:
            sims = canon_mat[:count] @ normed[i]
            k = int(sims.argmax())
            best_s, best_lead = float(sims[k]), canon_leads[k]

        if best_lead is not None and best_s >= auto_thr:
            # High confidence — auto-merge (do NOT add to the canonical index)
            merge_pairs.append((lead, best_lead, best_s))
            if not dry_run:
                lead.is_duplicate_of_known = True
                lead.dedup_group_id = best_lead.dedup_group_id or str(best_lead.id)
                lead.known_case_ref = best_lead.title
                lead.suspected_dup_of = None
                lead.suspected_dup_score = None
                lead.dup_review = None
                note = f"🔁 כפילות של ליד #{best_lead.id} (דמיון {best_s:.0%})"
                if not lead.notes or note not in lead.notes:
                    lead.notes = (lead.notes + "\n" if lead.notes else "") + note
            n_merged += 1
        elif best_lead is not None and best_s >= review_thr:
            # Borderline — keep as its own canonical, but flag for manual review
            review_pairs.append((lead, best_lead, best_s))
            if not dry_run:
                lead.is_duplicate_of_known = None
                lead.dedup_group_id = str(lead.id)
                lead.known_case_ref = None
                lead.suspected_dup_of = best_lead.id
                lead.suspected_dup_score = round(best_s, 4)
                lead.dup_review = "pending"
            canon_mat[count] = normed[i]
            canon_leads.append(lead)
            count += 1
            n_review += 1
        else:
            if not dry_run:
                lead.is_duplicate_of_known = None
                lead.dedup_group_id = str(lead.id)
                lead.known_case_ref = None
                lead.suspected_dup_of = None
                lead.suspected_dup_score = None
                lead.dup_review = None
            canon_mat[count] = normed[i]
            canon_leads.append(lead)
            count += 1
            n_unique += 1

    return dict(merged=n_merged, review=n_review, unique=n_unique,
                merge_pairs=merge_pairs, review_pairs=review_pairs)


def main():
    parser = argparse.ArgumentParser(
        description="Backfill semantic embeddings and dedup existing leads"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and report without writing to DB")
    parser.add_argument("--recluster", action="store_true",
                        help="Re-cluster ALL embedded leads from scratch under the "
                             "current thresholds (use after changing REVIEW/AUTO)")
    parser.add_argument("--review-threshold", type=float, default=None,
                        help=f"Suspected-duplicate floor (default {REVIEW_THRESHOLD})")
    parser.add_argument("--auto-threshold", type=float, default=None,
                        help=f"Auto-merge floor (default {AUTO_MERGE_THRESHOLD})")
    args = parser.parse_args()

    review_thr = args.review_threshold if args.review_threshold is not None else REVIEW_THRESHOLD
    auto_thr = args.auto_threshold if args.auto_threshold is not None else AUTO_MERGE_THRESHOLD

    print(f"\n{'='*60}")
    print(f"Backfill Embeddings — Two-Tier Semantic Deduplication")
    print(f"Thresholds: auto-merge >= {auto_thr}, review >= {review_thr}")
    print(f"Recluster:  {args.recluster}    Dry run: {args.dry_run}")
    print(f"{'='*60}\n")

    dedup = SemanticDeduplicator(api_key=VOYAGE_API_KEY, threshold=review_thr)
    if not dedup.enabled:
        print("ERROR: VOYAGE_API_KEY not set or voyageai package not installed.")
        print("Set VOYAGE_API_KEY in your .env file and run: pip install voyageai")
        sys.exit(1)

    init_database(DATABASE_URL)
    db = get_session(DATABASE_URL)

    # ── Phase 1: Compute and save embeddings (resumable) ─────────────────
    to_embed = (
        db.query(Lead).filter(Lead.embedding.is_(None)).order_by(Lead.id).all()
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

    # ── Phase 2: Two-tier clustering ─────────────────────────────────────
    if args.recluster:
        all_leads = (
            db.query(Lead).filter(Lead.embedding.isnot(None)).order_by(Lead.id).all()
        )
        print(f"\nRe-clustering {len(all_leads)} embedded leads from scratch.")
        print("  (resets dedup_group_id / suspected-dup fields for those leads)")
        if not args.dry_run:
            for l in all_leads:
                l.dedup_group_id = None
                l.is_duplicate_of_known = None
                l.known_case_ref = None
                l.suspected_dup_of = None
                l.suspected_dup_score = None
                l.dup_review = None
        embedded_leads = []
        for l in all_leads:
            try:
                embedded_leads.append((l, json.loads(l.embedding)))
            except Exception:
                pass
        seed_pairs = []
    else:
        leads = (
            db.query(Lead).filter(Lead.dedup_group_id.is_(None)).order_by(Lead.id).all()
        )
        print(f"\nLeads without dedup_group_id: {len(leads)}")
        if not leads:
            print("Nothing to cluster.")
            return
        embedded_leads = []
        for l in leads:
            if not l.embedding:
                continue
            try:
                embedded_leads.append((l, json.loads(l.embedding)))
            except Exception:
                pass
        skipped = len(leads) - len(embedded_leads)
        if skipped:
            print(f"  ({skipped} leads still have no embedding — left unclustered)")
        # Seed with already-clustered leads so new ones merge into existing groups
        seed_pairs = []
        for l in (
            db.query(Lead)
            .filter(Lead.embedding.isnot(None), Lead.dedup_group_id.isnot(None))
            .all()
        ):
            try:
                seed_pairs.append((l, json.loads(l.embedding)))
            except Exception:
                pass
        print(f"Seeding index with {len(seed_pairs)} already-clustered leads.")

    res = cluster_leads(embedded_leads, seed_pairs, auto_thr, review_thr, args.dry_run)

    print(f"\n{'─'*60}")
    print(f"Results  (auto-merge >= {auto_thr}, review >= {review_thr})")
    print(f"  Canonical (unique):        {res['unique']}")
    print(f"  Auto-merged (duplicates):  {res['merged']}")
    print(f"  Flagged for review:        {res['review']}")
    print(f"{'─'*60}\n")

    if res["review_pairs"]:
        print("Flagged for review (borderline — verify same lawsuit vs same company):")
        for dup, canon, score in res["review_pairs"][:40]:
            print(f"  [{score:.3f}] #{dup.id} \"{(dup.title or '')[:48]}\"")
            print(f"          ≈ #{canon.id} \"{(canon.title or '')[:48]}\"")
        if len(res["review_pairs"]) > 40:
            print(f"  ... and {len(res['review_pairs']) - 40} more")
        print()

    if args.dry_run:
        print("DRY RUN — no changes written to database.")
    else:
        db.commit()
        print(f"Done. {res['unique']} canonical, {res['merged']} merged, "
              f"{res['review']} flagged for review.")


if __name__ == "__main__":
    main()
