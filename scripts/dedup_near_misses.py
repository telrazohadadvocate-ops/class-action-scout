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

Usage (run in the Render Shell so it sees the production /var/data/scout.db):
  python scripts/dedup_near_misses.py [--band 0.10] [--top 40]
"""
import os, sys, json, math, argparse
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import DATABASE_URL, DEDUP_THRESHOLD
from database.models import init_database, get_session, Lead


def cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    ma = math.sqrt(sum(x * x for x in a))
    mb = math.sqrt(sum(x * x for x in b))
    return dot / (ma * mb) if ma and mb else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", type=float, default=0.10,
                    help="near-miss window below threshold (default 0.10)")
    ap.add_argument("--top", type=int, default=40,
                    help="max pairs to print (default 40)")
    args = ap.parse_args()

    thr = DEDUP_THRESHOLD
    lo = thr - args.band

    init_database(DATABASE_URL)
    db = get_session(DATABASE_URL)
    leads = db.query(Lead).all()

    n = len(leads)
    n_emb = sum(1 for l in leads if l.embedding)
    n_grp = sum(1 for l in leads if l.dedup_group_id is not None)
    print(f"\n{'='*64}")
    print(f"Dedup diagnostics   threshold={thr}   near-miss band=[{lo:.2f}, {thr:.2f})")
    print(f"{'='*64}")
    print(f"leads total ............ {n}")
    print(f"  with embedding ....... {n_emb}   (no embedding = never clustered: {n - n_emb})")
    print(f"  with dedup_group_id .. {n_grp}   (no group = never clustered: {n - n_grp})")

    embedded = []
    for l in leads:
        if not l.embedding:
            continue
        try:
            embedded.append((l, json.loads(l.embedding)))
        except Exception:
            pass

    pairs = []
    for i in range(len(embedded)):
        for j in range(i + 1, len(embedded)):
            s = cosine(embedded[i][1], embedded[j][1])
            pairs.append((s, embedded[i][0], embedded[j][0]))
    pairs.sort(key=lambda t: t[0], reverse=True)

    def show(title, rows):
        print(f"\n{title}  ({len(rows)})")
        print("-" * 64)
        for s, a, b in rows[:args.top]:
            same = "  ⟵ SAME COMPANY" if (a.company and b.company and
                    a.company.strip().lower() == b.company.strip().lower()) else ""
            print(f"[{s:.3f}] #{a.id} {(a.company or '?')[:22]} | {a.title[:40]}")
            print(f"        #{b.id} {(b.company or '?')[:22]} | {b.title[:40]}{same}")

    merged = [p for p in pairs if p[0] >= thr]
    nearmiss = [p for p in pairs if lo <= p[0] < thr]
    show("MERGED (>= threshold — already clustered)", merged)
    show("NEAR-MISS (just below threshold — inspect these)", nearmiss)

    print(f"\n{'='*64}")
    print("If NEAR-MISS pairs are clearly the SAME lawsuit (same company, same")
    print(f"matter), consider lowering DEDUP_THRESHOLD toward the near-miss scores.")
    print("If they are DIFFERENT lawsuits, keep the threshold — lowering it would")
    print("merge distinct cases and hide a real lead. No changes were written.")
    print(f"{'='*64}\n")


if __name__ == "__main__":
    main()
