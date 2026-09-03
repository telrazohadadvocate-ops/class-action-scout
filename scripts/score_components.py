#!/usr/bin/env python3
"""
Read-only decomposition of priority_score into its three components, so you can
see which one is holding the distribution down.

  python scripts/score_components.py --days 30
  python scripts/score_components.py --days 30 --sample 15   # per-lead rows too

Reads the DB directly (run it in the Render shell), or an /api/leads dump:

  python scripts/score_components.py --from-json leads.json

To produce that dump, ask the API for EVERYTHING — its defaults hide non-Israeli
leads, which are exactly the ones the x0.3 suppression acts on, so the default
view would bias the very number you are trying to measure:

  /api/leads?hide_non_israeli=false&hide_filed=false&hide_duplicates=false&limit=2000

priority_score = (value + certification + expertise)/3 * CONF_MULTIPLIER
                 [ * 0.3 if israel_applicable is False ]

Each component is recovered from stored columns:

  certification  strength_score                       (exact)
  expertise      10.0 if matches_expertise else 3.0   (exact)
  confidence     CONF_MULTIPLIER[value_confidence]    (exact)
  value          from value_high when it was stored   (exact)
                 else from est_class_size x est_damage_per_member  (approximate:
                 those columns hold the midpoints, not the high end, so this
                 under-reads the value component slightly — rows are marked ~)

Writes nothing. The counterfactuals at the end re-run the same arithmetic with
one factor neutralised at a time, which is what identifies the actual drag.
"""
import os
import re
import sys
import json
import math
import argparse
import statistics

os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta, timezone

from config.settings import DATABASE_URL, MIN_RELEVANCE_SCORE, HIGH_PRIORITY_THRESHOLD
from database.models import init_database, get_session, Lead
from analysis.value_estimator import (
    CONF_MULTIPLIER, _IL_CEILING, _PROMPT,
    _VALUE_LOG_ANCHOR_LOW, _VALUE_LOG_ANCHOR_HIGH,
    _VALUE_SCORE_LOW, _VALUE_SCORE_HIGH,
)


class JsonLead:
    """An /api/leads row, wearing the column names the rest of this file uses."""

    _MAP = {
        "id": "id", "title": "title", "company": "company",
        "priority_score": "priorityScore", "strength_score": "strengthScore",
        "matches_expertise": "matchesExpertise", "value_confidence": "valueConfidence",
        "israel_applicable": "israelApplicable", "value_high": "valueHigh",
        "est_class_size": "estClassSize", "est_damage_per_member": "estDamagePerMember",
        "is_duplicate_of_known": "isDuplicate",
    }

    def __init__(self, row):
        for attr, key in self._MAP.items():
            setattr(self, attr, row.get(key))
        stamp = row.get("scrapedAt") or ""
        try:
            self.scraped_at = datetime.fromisoformat(stamp) if stamp else None
        except ValueError:
            self.scraped_at = None


def confidence_rubric() -> dict:
    """
    The grading rubric, read out of the live prompt rather than restated here —
    a copy would drift from the text the model is actually given.
    """
    block = re.search(r"^confidence:\n((?:  \".+\n)+)", _PROMPT, re.M)
    if not block:
        return {}
    out = {}
    for line in block.group(1).splitlines():
        m = re.match(r'\s*"(high|medium|low)"\s*—\s*(.+?)\s*\\?$', line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def value_component(v_high):
    """The same log10 mapping _compute_priority_score uses."""
    if not v_high or v_high <= 0:
        return 0.0
    v = min(float(v_high), _IL_CEILING)
    comp = (
        _VALUE_SCORE_LOW
        + (math.log10(max(v, 1.0)) - _VALUE_LOG_ANCHOR_LOW)
        / (_VALUE_LOG_ANCHOR_HIGH - _VALUE_LOG_ANCHOR_LOW)
        * (_VALUE_SCORE_HIGH - _VALUE_SCORE_LOW)
    )
    return max(0.0, min(10.0, comp))


def describe(name, xs, width=13):
    if not xs:
        print(f"  {name:<{width}} (no data)")
        return
    xs = sorted(xs)
    print(f"  {name:<{width}} median {statistics.median(xs):5.2f}   mean "
          f"{statistics.fmean(xs):5.2f}   min {xs[0]:5.2f}   max {xs[-1]:5.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--sample", type=int, default=0,
                    help="also print this many per-lead rows, lowest score first")
    ap.add_argument("--from-json", metavar="PATH",
                    help="read an /api/leads dump instead of the database")
    args = ap.parse_args()

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).replace(tzinfo=None)

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            payload = json.load(fh)
        raw = payload.get("leads", payload) if isinstance(payload, dict) else payload
        leads = [JsonLead(r) for r in raw]
        print(f"\nSource: {args.from_json} ({len(leads)} row(s) as exported)")
        leads = [
            l for l in leads
            if l.priority_score is not None
            and not l.is_duplicate_of_known
            and (l.scraped_at is None or l.scraped_at.replace(tzinfo=None) >= since)
        ]
    else:
        init_database(DATABASE_URL)
        db = get_session(DATABASE_URL)
        leads = (
            db.query(Lead)
            .filter(
                Lead.scraped_at >= since,
                Lead.priority_score.isnot(None),
                (Lead.is_duplicate_of_known.is_(None)) | (Lead.is_duplicate_of_known == False),  # noqa: E712
            )
            .all()
        )
    leads.sort(key=lambda l: l.priority_score)
    if not leads:
        print(f"\nNo scored leads in the last {args.days} days.\n")
        return

    rows = []
    for l in leads:
        v_high, approx = l.value_high, False
        if not v_high and l.est_class_size and l.est_damage_per_member:
            v_high, approx = l.est_class_size * l.est_damage_per_member, True
        rows.append({
            "lead": l,
            "value": value_component(v_high),
            "cert": max(0.0, min(10.0, float(l.strength_score or 0))),
            "exp": 10.0 if l.matches_expertise else 3.0,
            "mult": CONF_MULTIPLIER.get((l.value_confidence or "low"), 0.6),
            "il_false": l.israel_applicable is False,
            "approx": approx,
        })

    print(f"\n{len(rows)} scored lead(s) in the last {args.days} days "
          f"(threshold {HIGH_PRIORITY_THRESHOLD}, relevance >= {MIN_RELEVANCE_SCORE})")
    print(f"  reaching {HIGH_PRIORITY_THRESHOLD}: "
          f"{sum(1 for r in rows if (r['lead'].priority_score or 0) >= HIGH_PRIORITY_THRESHOLD)}")
    approx_n = sum(1 for r in rows if r["approx"])
    if approx_n:
        print(f"  value component approximated from midpoints on {approx_n} lead(s) "
              f"(value_high not stored — confidence below medium, or no Israeli nexus)")

    print("\nComponent distribution (each is 0-10; the score is their mean, then scaled):")
    describe("value", [r["value"] for r in rows])
    describe("certification", [r["cert"] for r in rows])
    describe("expertise", [r["exp"] for r in rows])
    describe("score", [r["lead"].priority_score for r in rows])

    print("\nMultipliers applied after the blend.")
    print("Each grade is shown with the rubric line that earns it, read from the "
          "live\nprompt — a grade nothing can earn is a flat discount, not a "
          "calibration:")
    rubric = confidence_rubric()
    conf_counts = {}
    for r in rows:
        conf_counts[(r["lead"].value_confidence or "NULL")] = \
            conf_counts.get((r["lead"].value_confidence or "NULL"), 0) + 1
    for conf in ("high", "medium", "low", "NULL"):
        n = conf_counts.get(conf, 0)
        m = CONF_MULTIPLIER.get(conf, 0.6)
        share = f"{100.0*n/len(rows):4.0f}%" if rows else "   -"
        print(f"\n  value_confidence={conf:<7} {n:>4} lead(s) {share}  x{m}   "
              f"(ceiling with this multiplier: {round(10*m,1)})")
        if conf in rubric:
            print(f"      earned by: {rubric[conf]}")
        if n == 0 and conf in rubric:
            print(f"      ^ NOTHING earned this grade — the discount is unconditional")
    print(f"  israel_applicable=False   {sum(1 for r in rows if r['il_false']):>4} lead(s)  x0.3")
    print(f"  matches_expertise=False   "
          f"{sum(1 for r in rows if r['exp'] == 3.0):>4} lead(s)  "
          f"(expertise component 3.0 instead of 10.0)")

    # ── Counterfactuals: neutralise one factor at a time ──────────────────────
    def rescore(rs, *, mult=True, il=True, value=None, cert=None, exp=None):
        out = []
        for r in rs:
            v = r["value"] if value is None else value
            c = r["cert"] if cert is None else cert
            e = r["exp"] if exp is None else exp
            s = (v + c + e) / 3.0
            if mult:
                s *= r["mult"]
            s = max(1.0, min(10.0, round(s, 1)))
            if il and r["il_false"]:
                s = max(1.0, round(s * 0.3, 1))
            out.append(s)
        return out

    base = rescore(rows)
    scenarios = [
        ("as scored today",                      base),
        ("without the confidence multiplier",    rescore(rows, mult=False)),
        ("without the non-Israeli x0.3",         rescore(rows, il=False)),
        ("with a perfect value component (10)",  rescore(rows, value=10.0)),
        ("with a perfect certification (10)",    rescore(rows, cert=10.0)),
        ("with expertise matched on all (10)",   rescore(rows, exp=10.0)),
    ]
    print(f"\nCounterfactual — how many of these {len(rows)} leads would reach "
          f"{HIGH_PRIORITY_THRESHOLD}, changing one factor at a time:")
    for label, scores in scenarios:
        n = sum(1 for s in scores if s >= HIGH_PRIORITY_THRESHOLD)
        print(f"  {n:>4}/{len(rows)}   median {statistics.median(scores):4.1f}   {label}")

    if args.sample:
        print(f"\nLowest-scoring {min(args.sample, len(rows))} leads:")
        print(f"  {'score':>5} {'value':>6} {'cert':>5} {'exp':>5} {'mult':>5} "
              f"{'IL':>5}  company / title")
        for r in rows[:args.sample]:
            l = r["lead"]
            print(f"  {l.priority_score:>5} {r['value']:>6.2f}{'~' if r['approx'] else ' '}"
                  f"{r['cert']:>4} {r['exp']:>5} {r['mult']:>5} "
                  f"{str(l.israel_applicable):>5}  "
                  f"{(l.company or '?')[:18]:18} {(l.title or '')[:42]}")
    print()


if __name__ == "__main__":
    main()
