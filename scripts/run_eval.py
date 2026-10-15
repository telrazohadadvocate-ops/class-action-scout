#!/usr/bin/env python3
"""
Measure the pipeline against the hand-labelled gold set (eval/gold.json).
Never writes to the database — leads are read READ-ONLY and every re-run
result lives only in memory (and in --out, if given).

Gold format:
  {"items": [
     {"id": 1234, "label": "yes" | "maybe" | "no", "note": "..."},
     {"ext": "amazon-sponsored-ads", "title": "...", "content": "...",
      "source_type": "international", "label": "yes", "note": "missed lead"}
  ]}
  "id" items take title/content from the database; "ext" items carry their own
  text (leads the bot never saw, or saw and dropped before saving).

Modes
  (default)      STORED — the scores already in the database. Free, no API calls.
                 Ext items have no stored score and count as missed.
  --live         re-run Stage 1 with the CURRENT prompt (one cheap call per item).
  --live --full  also Stage 2 + Stage 3 for items that pass the gate, so the
                 hot/alert decision is measured too (≈3 calls per passing item).

What it reports
  gate recall    share of "yes" items that pass the Stage-1 gate  (misses = lost forever)
  alert recall   share of "yes" items that reach the hot threshold (alerted)
  alert precision share of alerted items labelled "yes"
  every "yes" that was missed, and every "no" that was alerted, with the reason

Usage (Render Shell):
  python scripts/run_eval.py
  python scripts/run_eval.py --live --out /var/data/eval_run.json
  python scripts/run_eval.py --live --full --limit 10
"""
import os
import sys
import json
import argparse
import sqlite3

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from config.settings import DATABASE_PATH, MIN_RELEVANCE_SCORE, HIGH_PRIORITY_THRESHOLD

DEFAULT_GOLD = os.path.join(ROOT, "eval", "gold.json")


def load_gold(path):
    with open(path, encoding="utf-8") as f:
        items = json.load(f)["items"]
    for it in items:
        if it.get("label") not in ("yes", "maybe", "no"):
            raise ValueError(f"bad label in gold item {it.get('id') or it.get('ext')}: {it.get('label')!r}")
    return items


def attach_db_rows(items, db_path):
    ids = [it["id"] for it in items if "id" in it]
    rows = {}
    if ids:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        q = ",".join("?" * len(ids))
        for r in con.execute(f"SELECT * FROM leads WHERE id IN ({q})", ids):
            rows[r["id"]] = dict(r)
        con.close()
    out = []
    for it in items:
        if "id" in it:
            r = rows.get(it["id"])
            if r is None:
                print(f"  ! lead #{it['id']} not in database — skipped", file=sys.stderr)
                continue
            out.append({**it, "key": f"#{it['id']}", "title": r["title"],
                        "content": r["raw_content"] or "", "source_type": r["source_type"] or "unknown",
                        "stored": {"rel": r["relevance_score"], "pri": r["priority_score"],
                                   "str": r["strength_score"], "why": r["relevance_reasoning"] or ""}})
        else:
            out.append({**it, "key": it["ext"], "content": it.get("content", ""),
                        "source_type": it.get("source_type", "international"),
                        "stored": {"rel": None, "pri": None, "str": None, "why": "never scored"}})
    return out


def score_stored(it):
    return dict(it["stored"])


def score_live(it, scout, full):
    """Re-run the real pipeline code on a transient Lead (never added to a session)."""
    from database.models import Lead
    lead = Lead(title=it["title"], raw_content=it["content"], source_type=it["source_type"])
    c = scout.analyzer.classify(it["title"], it["content"], it["source_type"])
    decision = scout._apply_stage1(lead, c)
    res = {"rel": lead.relevance_score, "pri": None, "str": None,
           "why": lead.relevance_reasoning or lead.stage1_error or ""}
    if decision == "error":
        res["error"] = lead.stage1_error
        return res
    if full and decision == "pass":
        from analysis.value_estimator import estimate_value
        try:
            a = scout.analyzer.analyze(title=it["title"], content=it["content"], classification=c)
            scout._apply_stage2(lead, a)
            estimate_value(lead, scout.analyzer.client, scout.analyzer.model)
            res.update(pri=lead.priority_score, str=lead.strength_score)
        except Exception as e:
            res["error"] = f"stage2/3: {e}"[:300]
    return res


def make_scout():
    """The pipeline's own helpers, without __init__ (which opens the DB and builds scrapers)."""
    import main as M
    from analysis.claude_analyzer import ClaudeAnalyzer
    from config.settings import ANTHROPIC_API_KEY, CLAUDE_MODEL
    s = M.ClassActionScout.__new__(M.ClassActionScout)
    s.analyzer = ClaudeAnalyzer(api_key=ANTHROPIC_API_KEY, model=CLAUDE_MODEL)
    return s


def summarize(results, gate, hot, scored_stage3):
    def passes(r):
        return r["rel"] is not None and r["rel"] >= gate
    def alerted(r):
        return r["pri"] is not None and r["pri"] >= hot
    yes = [r for r in results if r["label"] == "yes"]
    no = [r for r in results if r["label"] == "no"]
    s = {
        "n": len(results), "yes": len(yes), "maybe": sum(r["label"] == "maybe" for r in results),
        "no": len(no), "errors": sum(1 for r in results if r.get("error")),
        "gate_recall": (sum(passes(r) for r in yes), len(yes)),
        "gate_false_pass": (sum(passes(r) for r in no), len(no)),
        "missed_at_gate": [r for r in yes if not passes(r)],
    }
    if scored_stage3:
        al = [r for r in results if alerted(r)]
        s["alert_recall"] = (sum(alerted(r) for r in yes), len(yes))
        s["alert_precision"] = (sum(r["label"] == "yes" for r in al), len(al))
        s["missed_alert"] = [r for r in yes if passes(r) and not alerted(r)]
        s["false_alert"] = [r for r in no if alerted(r)]
    return s


def pct(t):
    a, b = t
    return f"{a}/{b}" + (f"  ({100 * a / b:.0f}%)" if b else "")


def fmt(v):
    return "—" if v is None else f"{v:.1f}"


def print_report(s, mode, gate, hot):
    print(f"\n=== Eval — {mode} — gate {gate:g}, hot {hot:g} ===")
    print(f"items {s['n']}: yes {s['yes']} · maybe {s['maybe']} · no {s['no']}"
          + (f" · technical errors {s['errors']}" if s["errors"] else ""))
    print(f"gate recall      {pct(s['gate_recall'])}   (yes items that reach deep analysis)")
    print(f"gate false pass  {pct(s['gate_false_pass'])}   (no items that cost a deep analysis)")
    if "alert_recall" in s:
        print(f"alert recall     {pct(s['alert_recall'])}   (yes items that would be alerted)")
        print(f"alert precision  {pct(s['alert_precision'])}   (alerted items that are yes)")
    for title, key in (("YES dropped at the gate", "missed_at_gate"),
                       ("YES analysed but not hot", "missed_alert"),
                       ("NO but alerted", "false_alert")):
        rows = s.get(key) or []
        if rows:
            print(f"\n-- {title} ({len(rows)})")
            for r in rows:
                print(f"  {r['key']:>8}  rel {fmt(r['rel'])}  pri {fmt(r['pri'])}  {r['title'][:90]}")
                if r.get("why"):
                    print(f"            why: {r['why'][:200]}")
                if r.get("note"):
                    print(f"            label note: {r['note'][:200]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", default=DEFAULT_GOLD)
    ap.add_argument("--db", default=str(DATABASE_PATH))
    ap.add_argument("--live", action="store_true", help="re-run Stage 1 with the current prompt")
    ap.add_argument("--full", action="store_true", help="with --live: also Stage 2 + 3")
    ap.add_argument("--limit", type=int, default=0, help="only the first N items (cost guard)")
    ap.add_argument("--out", help="write per-item results as JSON")
    a = ap.parse_args()
    if a.full and not a.live:
        ap.error("--full needs --live")

    items = attach_db_rows(load_gold(a.gold), a.db)
    if a.limit:
        items = items[: a.limit]
    gate, hot = float(MIN_RELEVANCE_SCORE), float(HIGH_PRIORITY_THRESHOLD)

    scout = make_scout() if a.live else None
    results = []
    for i, it in enumerate(items, 1):
        r = score_live(it, scout, a.full) if a.live else score_stored(it)
        results.append({"key": it["key"], "label": it["label"], "note": it.get("note", ""),
                        "title": it["title"], **r})
        if a.live:
            print(f"  [{i}/{len(items)}] {it['key']} rel {fmt(r['rel'])} pri {fmt(r['pri'])}",
                  file=sys.stderr)

    mode = "LIVE stage 1-3" if a.full else "LIVE stage 1" if a.live else "STORED scores"
    s = summarize(results, gate, hot, scored_stage3=(a.full or not a.live))
    print_report(s, mode, gate, hot)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "results": results}, f, ensure_ascii=False, indent=1)
        print(f"\nper-item results → {a.out}")


if __name__ == "__main__":
    main()
