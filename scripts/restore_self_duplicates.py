#!/usr/bin/env python3
"""
Restore leads that were marked as duplicates OF THEMSELVES (the 2026-08-14
dedup bug) and push them through the pipeline they never reached.

For each self-duplicate (identified by record ids, exactly as
scripts/report_blocked_leads.py does — never by known_case_ref == title alone;
manual merge/separate decisions are left alone):

  1. If a different, gate-passing lead has the same headline  -> re-point to it
  2. Otherwise unmerge: clear the self-duplicate flag, group and note
  3. Run real dedup against other records (title + Voyage). A restored lead is
     excluded from the stored-lead indexes, as is every other self-duplicate,
     so nothing is matched against itself or against another broken record.
     Restored leads that duplicate each other are still caught in-batch.
  4. Stage 2 (legal) + Stage 3 (value) for the ones that are still unique
  5. Optional alert cap so the next scan does not email the whole backlog

Resumable: each restored lead is tagged in notes with RESTORE_MARK and
committed per lead after analysis. A re-run picks up tagged leads still
missing strength_score, and never re-analyses finished ones.

Usage (Render Shell):
  python scripts/restore_self_duplicates.py --dry-run          # no writes, no Claude calls
  python scripts/restore_self_duplicates.py --limit 3 --alert-cap 15
  python scripts/restore_self_duplicates.py --alert-cap 15     # the rest
"""
import os
import re
import sys
import argparse
import importlib.util
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from config.settings import (
    DATABASE_URL, ANTHROPIC_API_KEY, CLAUDE_MODEL, VOYAGE_API_KEY,
    MIN_RELEVANCE_SCORE, REVIEW_THRESHOLD,
)
from database.models import init_database, get_session, commit_with_retry, Lead
from analysis.claude_analyzer import ClaudeAnalyzer, AnalysisError
from analysis.dedup import SemanticDeduplicator, title_match_key
import analysis.value_estimator as ve
import main as M
from report_blocked_leads import classify_row

RESTORE_MARK = "♻ שוחזר מכפילות עצמית"
_NOTE_ID = re.compile(r"כפילות של ליד #(\d+)")


def _load_cap_first_alert():
    spec = importlib.util.spec_from_file_location(
        "reanalyze_leads", os.path.join(ROOT, "scripts", "reanalyze_leads.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.cap_first_alert


def _row(lead):
    return {k: getattr(lead, k, None) for k in (
        "id", "title", "company", "relevance_score", "is_duplicate_of_known",
        "known_case_ref", "dedup_group_id", "dup_review", "notes", "raw_content")}


def find_self_duplicates(db):
    """All self-duplicates, oldest first, with the re-point target if any."""
    leads = db.query(Lead).order_by(Lead.id).all()
    canon = {}
    for l in leads:
        if not l.is_duplicate_of_known and (l.relevance_score or 0) >= MIN_RELEVANCE_SCORE:
            k = title_match_key(l.title, l.company)
            if k and k not in canon:
                canon[k] = l.id
    out = []
    for l in leads:
        if not l.is_duplicate_of_known:
            continue
        cat, _, proposal = classify_row(_row(l), canon)
        if cat != "self_duplicate":
            continue
        m = re.search(r"re-point to #(\d+)", proposal)
        out.append((l, int(m.group(1)) if m else None))
    return out


def _drop_self_note(notes, lead_id):
    keep = [ln for ln in (notes or "").splitlines()
            if str(lead_id) not in _NOTE_ID.findall(ln)]
    return "\n".join(keep)


def _stamp(notes, text):
    return (notes + "\n" if notes else "") + text


def unmerge(lead, today):
    lead.is_duplicate_of_known = False
    lead.dedup_group_id = None
    if lead.known_case_ref == lead.title:
        lead.known_case_ref = None
    lead.notes = _stamp(_drop_self_note(lead.notes, lead.id), f"{RESTORE_MARK} {today}")


def repoint(lead, target, today):
    lead.is_duplicate_of_known = True
    lead.dedup_group_id = target.dedup_group_id or str(target.id)
    lead.known_case_ref = target.title
    lead.notes = _stamp(_drop_self_note(lead.notes, lead.id),
                        f"🔁 כפילות של ליד #{target.id} (כותרת זהה) — {RESTORE_MARK}, הופנה מחדש {today}")


def _classification(lead):
    return {
        "relevance_score": lead.relevance_score, "company": lead.company or "",
        "sector": lead.sector or "", "operates_in_israel": lead.operates_in_israel,
        "israeli_law_basis": lead.israeli_law_basis or "",
        "estimated_class_size": lead.estimated_class_size or "",
    }


def pending_analysis(db):
    """Restored, unique, gate-passing leads still without a Stage-2 result."""
    return (db.query(Lead)
            .filter(Lead.notes.like(f"%{RESTORE_MARK}%"),
                    Lead.strength_score.is_(None),
                    (Lead.is_duplicate_of_known.is_(None)) | (Lead.is_duplicate_of_known == False),
                    Lead.relevance_score >= MIN_RELEVANCE_SCORE)
            .order_by(Lead.id).all())


def run(db, analyzer, deduplicator, limit=None, dry_run=False, alert_cap=None, log=print):
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    scout = M.ClassActionScout.__new__(M.ClassActionScout)
    scout.db, scout.analyzer, scout.deduplicator = db, analyzer, deduplicator

    s = {"self_duplicates": 0, "selected": 0, "repointed": 0, "unmerged_gate": 0,
         "unmerged_below_gate": 0, "dedup": {}, "stage2_ok": 0, "stage2_failed": 0,
         "stage3_ok": 0, "stage3_failed": 0, "known_case": [], "resumed": 0,
         "alert_kept": None, "alert_suppressed": None}

    found = find_self_duplicates(db)
    all_self_ids = {l.id for l, _ in found}
    s["self_duplicates"] = len(found)
    batch = found[:limit] if limit else found
    s["selected"] = len(batch)
    log(f"Self-duplicates in DB: {len(found)}; processing {len(batch)}"
        + (" (DRY RUN — nothing is written, no Claude calls)" if dry_run else ""))

    to_dedup = []
    for lead, target_id in batch:
        if target_id is not None:
            target = db.get(Lead, target_id)
            repoint(lead, target, today)
            s["repointed"] += 1
            log(f"  #{lead.id} re-pointed to #{target_id} (genuine identical-title lead)")
            continue
        unmerge(lead, today)
        if (lead.relevance_score or 0) >= MIN_RELEVANCE_SCORE:
            s["unmerged_gate"] += 1
            to_dedup.append((lead, SimpleNamespace(title=lead.title, content=lead.raw_content or ""),
                             _classification(lead)))
        else:
            s["unmerged_below_gate"] += 1
            log(f"  #{lead.id} unmerged only (below gate)")

    unique = scout._semantic_dedup(to_dedup, exclude_ids=all_self_ids,
                                   stats=s["dedup"], commit=not dry_run)
    unique_ids = [l.id for l, _, _ in unique]
    log(f"Dedup: {s['dedup']}  → to analyse: " + (", ".join(f"#{i}" for i in unique_ids) or "none"))

    if dry_run:
        db.rollback()
        log("DRY RUN complete — rolled back.")
        return s

    commit_with_retry(db)

    queue = pending_analysis(db)
    s["resumed"] = sum(1 for l in queue if l.id not in unique_ids)
    if limit:
        queue = queue[:limit]
    for lead in queue:
        try:
            analysis = analyzer.analyze(title=lead.title, content=lead.raw_content or "",
                                        classification=_classification(lead))
        except AnalysisError as e:
            s["stage2_failed"] += 1
            log(f"  ✗ #{lead.id} Stage 2 failed — left for the next run: {e}")
            continue
        scout._apply_stage2(lead, analysis)
        s["stage2_ok"] += 1
        if lead.is_duplicate_of_known:
            s["known_case"].append((lead.id, lead.known_case_ref))
        try:
            ve.estimate_value(lead, analyzer.client, analyzer.model)
            s["stage3_ok"] += 1
        except Exception as e:
            s["stage3_failed"] += 1
            log(f"  ⚠ #{lead.id} Stage 3 failed — priority_score left NULL: {e}")
        commit_with_retry(db)
        log(f"  ✓ #{lead.id} strength={lead.strength_score} priority_score={lead.priority_score} "
            f"{(lead.title or '')[:50]}")

    if alert_cap is not None:
        kept, suppressed = _load_cap_first_alert()(db, alert_cap)
        s["alert_kept"], s["alert_suppressed"] = kept, suppressed
        log(f"Alert cap {alert_cap}: {kept} left for the next email, {suppressed} marked as alerted")

    log("SUMMARY " + str({k: v for k, v in s.items() if k != "known_case"}))
    for lid, ref in s["known_case"]:
        log(f"  [KNOWN] #{lid} flagged as firm case after Stage 2: {ref}")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--alert-cap", type=int, default=None, metavar="N")
    args = ap.parse_args()

    init_database(DATABASE_URL)
    db = get_session(DATABASE_URL)
    analyzer = None if args.dry_run else ClaudeAnalyzer(api_key=ANTHROPIC_API_KEY, model=CLAUDE_MODEL)
    dedup = SemanticDeduplicator(api_key=VOYAGE_API_KEY, threshold=REVIEW_THRESHOLD)
    run(db, analyzer, dedup, limit=args.limit, dry_run=args.dry_run, alert_cap=args.alert_cap)


if __name__ == "__main__":
    main()
