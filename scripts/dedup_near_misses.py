#!/usr/bin/env python3
"""
Dedup diagnostics — near-miss report (READ-ONLY, no DB writes).

Prints, for the current DB:
  1. Coverage: how many leads have an embedding / dedup_group_id (leads with no
     embedding were never clustered, so they can silently duplicate).
  2. The most-similar lead pairs, split into:
       - MERGED     (cosine >= threshold; already clustered)
       - NEAR-MISS  (band just below threshold; SAME case, different outlet?)
     so you can judge whether to loosen DEDUP_THRESHOLD before changing it.

Similarity is computed with numpy (vectorized) — the full pairwise matrix, not
a Python double loop. Use --limit to sample when there are very many embeddings.

Usage (run in the Render Shell so it sees the production /var/data/scout.db):
  python scripts/dedup_near_misses.py [--band 0.10] [--top 40] [--limit 800]
"""
import os, sys, json, argparse
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from config.settings import DATABASE_URL, DEDUP_THRESHOLD
from database.models import init_database, get_session, Lead


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", type=float, default=0.10,
                    help="near-miss window below threshold (default 0.10)")
    ap.add_argument("--top", type=int, default=40,
                    help="max pairs to print per section (default 40)")
    ap.add_argument("--limit", type=int, default=None,
                    help="sample at most N embedded leads for the pairwise "
                         "comparison (default: all)")
    args = ap.parse_args()

    thr = DEDUP_THRESHOLD
    lo = thr - args.band

    init_database(DATABASE_URL)
    db = get_session(DATABASE_URL)
    leads = db.query(Lead).all()

    n = len(leads)
    n_emb = sum(1 for l in leads if l.embedding)
    n_grp = sum(1 for l in leads if l.dedup_group_id is not None)
    print("=" * 64)
    print(f"Dedup diagnostics   threshold={thr}   near-miss band=[{lo:.2f}, {thr:.2f})")
    print("=" * 64)
    print(f"leads total ............ {n}")
    print(f"  with embedding ....... {n_emb}   (never clustered: {n - n_emb})")
    print(f"  with dedup_group_id .. {n_grp}   (never clustered: {n - n_grp})")

    embedded = []
    for l in leads:
        if not l.embedding:
            continue
        try:
            embedded.append((l, json.loads(l.embedding)))
        except Exception:
            pass

    if args.limit and len(embedded) > args.limit:
        embedded = embedded[:args.limit]
        print(f"\n(sampled first {args.limit} of {n_emb} embedded leads for comparison)")

    m = len(embedded)
    if m < 2:
        print("\nNot enough embedded leads to compare.")
        return

    # Vectorized cosine: normalize rows, upper-triangle of (X · Xᵀ)
    mat = np.asarray([e for _, e in embedded], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1e-12
    normed = mat / norms
    sims = normed @ normed.T
    iu, ju = np.triu_indices(m, k=1)
    scores = sims[iu, ju]

    keep = np.where(scores >= lo)[0]
    keep = keep[np.argsort(-scores[keep])]

    def show(title, idxs):
        print(f"\n{title}  ({len(idxs)})")
        print("-" * 64)
        for k in idxs[:args.top]:
            s = float(scores[k])
            a = embedded[int(iu[k])][0]
            b = embedded[int(ju[k])][0]
            same = ("  <-- SAME COMPANY" if (a.company and b.company and
                    a.company.strip().lower() == b.company.strip().lower()) else "")
            print(f"[{s:.3f}] #{a.id} {(a.company or '?')[:22]} | {a.title[:40]}")
            print(f"        #{b.id} {(b.company or '?')[:22]} | {b.title[:40]}{same}")

    ks = scores[keep]
    merged = keep[ks >= thr]
    nearmiss = keep[(ks >= lo) & (ks < thr)]
    show("MERGED (>= threshold — already clustered)", merged)
    show("NEAR-MISS (just below threshold — inspect these)", nearmiss)

    print("\n" + "=" * 64)
    print("If NEAR-MISS pairs are clearly the SAME lawsuit (same company, same")
    print("matter), consider lowering DEDUP_THRESHOLD toward the near-miss scores.")
    print("If they are DIFFERENT lawsuits, keep the threshold — lowering it would")
    print("merge distinct cases and hide a real lead. No changes were written.")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    main()
