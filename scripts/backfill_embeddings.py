#!/usr/bin/env python3
"""
Backfill Voyage AI embeddings for existing leads and run semantic deduplication.

Usage:
  python scripts/backfill_embeddings.py [--dry-run] [--recluster [--force-all]]
                                        [--review-threshold R] [--auto-threshold A]

Phase 1 — Embed leads where embedding IS NULL (resumable, rate-limited for free tier).
Phase 2 — Two-tier clustering:
            similarity >= auto   → auto-merge (duplicate, hidden by default)
            [review, auto)       → flag as SUSPECTED duplicate (dup_review="pending")
            < review             → canonical (unique)
  --recluster re-clusters ALL embedded leads from scratch under the current
  thresholds (use after changing REVIEW/AUTO) and reports merge vs flag counts.
  Leads a human already resolved (dup_review "merged" or "separate") are held
  back and left exactly as they are; --force-all opts into discarding those
  manual decisions too.
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

# dup_review values that mean "a human decided this" — never overwritten by a
# recluster unless --force-all is passed.
MANUAL_REVIEW_STATES = ("merged", "separate")


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


def repair_protected_groups(db, protected, dry_run):
    """
    A manual merge stores dedup_group_id = str(canonical_lead.id). If that
    canonical lead was itself re-clustered into someone else's group, the
    protected lead would be left pointing at a group nobody else is in — the
    decision survives but stops grouping anything. Re-point it at wherever its
    canonical ended up. Returns the number of leads re-pointed.
    """
    fixed = 0
    for l in protected:
        if not l.is_duplicate_of_known or not l.dedup_group_id:
            continue
        try:
            canonical_id = int(l.dedup_group_id)
        except (TypeError, ValueError):
            continue  # non-numeric group id — not one we can trace
        canonical = db.query(Lead).get(canonical_id)
        if not canonical or not canonical.dedup_group_id:
            continue
        if canonical.dedup_group_id != l.dedup_group_id:
            print(f"  re-pointing #{l.id}: group {l.dedup_group_id} → "
                  f"{canonical.dedup_group_id} (canonical #{canonical_id} moved)")
            if not dry_run:
                l.dedup_group_id = canonical.dedup_group_id
            fixed += 1
    return fixed


def main():
    parser = argparse.ArgumentParser(
        description="Backfill semantic embeddings and dedup existing leads"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and report without writing to DB")
    parser.add_argument("--recluster", action="store_true",
                        help="Re-cluster ALL embedded leads from scratch under the "
                             "current thresholds (use after changing REVIEW/AUTO)")
    parser.add_argument("--force-all", action="store_true",
                        help="With --recluster, ALSO discard manual merge / "
                             "keep-separate decisions (destructive — off by default)")
    parser.add_argument("--review-threshold", type=float, default=None,
                        help=f"Suspected-duplicate floor (default {REVIEW_THRESHOLD})")
    parser.add_argument("--auto-threshold", type=float, default=None,
                        help=f"Auto-merge floor (default {AUTO_MERGE_THRESHOLD})")
    args = parser.parse_args()

    if args.force_all and not args.recluster:
        parser.error("--force-all only applies to --recluster")

    review_thr = args.review_threshold if args.review_threshold is not None else REVIEW_THRESHOLD
    auto_thr = args.auto_threshold if args.auto_threshold is not None else AUTO_MERGE_THRESHOLD

    print(f"\n{'='*60}")
    print(f"Backfill Embeddings — Two-Tier Semantic Deduplication")
    print(f"Thresholds: auto-merge >= {auto_thr}, review >= {review_thr}")
    print(f"Recluster:  {args.recluster}"
          + ("  (--force-all: manual decisions WILL be discarded)" if args.force_all
             else "  (manual decisions preserved)" if args.recluster else "")
          + f"    Dry run: {args.dry_run}")
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

        # Hold back human-resolved leads unless --force-all says otherwise.
        if args.force_all:
            protected, to_recluster = [], all_leads
        else:
            protected = [l for l in all_leads
                         if (l.dup_review or "") in MANUAL_REVIEW_STATES]
            to_recluster = [l for l in all_leads
                            if (l.dup_review or "") not in MANUAL_REVIEW_STATES]

        print(f"\nRe-clustering {len(to_recluster)} of {len(all_leads)} embedded leads "
              f"from scratch.")
        print("  (resets dedup_group_id / suspected-dup fields for those leads)")
        if protected:
            n_kept_merged = sum(1 for l in protected if l.dup_review == "merged")
            n_kept_sep = sum(1 for l in protected if l.dup_review == "separate")
            print(f"  Preserving {len(protected)} manually-resolved leads "
                  f"({n_kept_merged} merged, {n_kept_sep} kept-separate) — "
                  f"pass --force-all to discard those decisions.")
        elif args.force_all:
            n_manual = sum(1 for l in all_leads
                           if (l.dup_review or "") in MANUAL_REVIEW_STATES)
            print(f"  --force-all: DISCARDING {n_manual} manual merge / "
                  f"keep-separate decisions.")

        if not args.dry_run:
            for l in to_recluster:
                l.dedup_group_id = None
                l.is_duplicate_of_known = None
                l.known_case_ref = None
                l.suspected_dup_of = None
                l.suspected_dup_score = None
                l.dup_review = None

        embedded_leads = []
        for l in to_recluster:
            try:
                embedded_leads.append((l, json.loads(l.embedding)))
            except Exception:
                pass

        # Preserved canonicals seed the index so re-clustered leads can still
        # join their groups. Preserved duplicates stay out, matching how
        # auto-merged leads are handled inside cluster_leads.
        seed_pairs = []
        for l in protected:
            if l.is_duplicate_of_known:
                continue
            try:
                seed_pairs.append((l, json.loads(l.embedding)))
            except Exception:
                pass
        if seed_pairs:
            print(f"Seeding index with {len(seed_pairs)} preserved canonical leads.")
    else:
        protected = []
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

    n_repointed = repair_protected_groups(db, protected, args.dry_run) if protected else 0

    print(f"\n{'─'*60}")
    print(f"Results  (auto-merge >= {auto_thr}, review >= {review_thr})")
    print(f"  Canonical (unique):        {res['unique']}")
    print(f"  Auto-merged (duplicates):  {res['merged']}")
    print(f"  Flagged for review:        {res['review']}")
    if protected:
        print(f"  Preserved (manual):        {len(protected)}"
              + (f"  ({n_repointed} re-pointed)" if n_repointed else ""))
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
              f"{res['review']} flagged for review"
              + (f", {len(protected)} manual decisions preserved." if protected else "."))


if __name__ == "__main__":
    main()
