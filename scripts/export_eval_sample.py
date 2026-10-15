#!/usr/bin/env python3
"""
Export a stratified sample of leads for hand-labelling (the "gold" eval set).
Opens the database READ-ONLY and writes nothing to it.

Why stratified: a random sample would be almost all low-relevance noise. The
questions we need answered are (a) are the hot leads really good, and (b) is
something good being thrown away just under the gate — so the sample is drawn
from every band, with extra weight right under the Stage-1 gate.

Strata (each lead appears once; canonical leads only — no duplicates):
  reviewed      status already set by a person (pursuing / dismissed / reviewed)
  hot           priority_score >= HIGH_PRIORITY_THRESHOLD
  mid           analysed, priority 5 .. threshold
  low           analysed, priority < 5
  edge_reject   Stage 1 score just under the gate (gate-1 .. gate)
  reject        Stage 1 score well under the gate

Output: one compact JSON array on stdout (paste it back), counts on stderr.

Usage (Render Shell):
  python scripts/export_eval_sample.py
  python scripts/export_eval_sample.py --days 180 --seed 7
"""
import os
import sys
import json
import html
import random
import argparse
import sqlite3

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from config.settings import DATABASE_PATH, MIN_RELEVANCE_SCORE, HIGH_PRIORITY_THRESHOLD

QUOTA = {"reviewed": 12, "hot": 8, "mid": 7, "low": 5, "edge_reject": 10, "reject": 5}


def stratum_where(gate: float, hot: float) -> dict:
    return {
        "reviewed": "status IN ('pursuing','dismissed','reviewed')",
        "hot": f"priority_score >= {hot}",
        "mid": f"priority_score >= 5 AND priority_score < {hot}",
        "low": "priority_score < 5",
        "edge_reject": f"relevance_score >= {gate - 1} AND relevance_score < {gate}",
        "reject": f"relevance_score < {gate - 1}",
    }


def short(x, n):
    x = html.unescape((x or "").replace("\n", " ").strip())
    return x if len(x) <= n else x[: n - 1] + "…"


def rnd(x):
    return None if x is None else round(float(x), 1)


def export(db_path, days=120, seed=42, quota=None):
    quota = quota or QUOTA
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    base = (
        "FROM leads WHERE scraped_at >= datetime('now', ?) "
        "AND stage1_error IS NULL AND relevance_score IS NOT NULL "
        "AND COALESCE(is_duplicate_of_known, 0) = 0 "
        "AND (dedup_group_id IS NULL OR dedup_group_id = CAST(id AS TEXT))"
    )
    rng = random.Random(seed)
    picked, seen, counts = [], set(), {}
    for name, where in stratum_where(float(MIN_RELEVANCE_SCORE), float(HIGH_PRIORITY_THRESHOLD)).items():
        rows = [dict(r) for r in con.execute(
            f"SELECT * {base} AND {where} ORDER BY id", (f"-{int(days)} days",))]
        rows = [r for r in rows if r["id"] not in seen]
        rng.shuffle(rows)
        take = rows[: quota.get(name, 0)]
        counts[name] = (len(take), len(rows))
        for r in take:
            seen.add(r["id"])
            picked.append({
                "id": r["id"], "s": name,
                "t": short(r["title"], 160), "co": short(r["company"], 40),
                "src": r["source_name"], "url": r["source_url"],
                "d": (r["scraped_at"] or "")[:10],
                "rel": rnd(r["relevance_score"]), "pri": rnd(r["priority_score"]),
                "str": rnd(r["strength_score"]), "st": r["status"],
                "why": short(r["relevance_reasoning"], 220),
            })
    con.close()
    return picked, counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DATABASE_PATH))
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    picked, counts = export(a.db, a.days, a.seed)
    for k, (n, avail) in counts.items():
        print(f"{k:12} {n:3} of {avail}", file=sys.stderr)
    print(f"total        {len(picked)}", file=sys.stderr)
    print(json.dumps(picked, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
