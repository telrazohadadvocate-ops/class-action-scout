#!/usr/bin/env python3
"""
Class Action Scout — Main Pipeline
====================================
Usage:
  python main.py --run-now                          # Full daily pipeline
  python main.py --run-now --sources classaction_org # Specific source only
  python main.py --run-now --skip-pinkas            # Skip registry check
  python main.py --report --days 7                  # Generate report
  python main.py --report --days 30 --format html   # HTML report
"""
import os
import re
import json
import logging
import argparse
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

# Ensure UTF-8 on Windows
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler("scout.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("scout")

from config.settings import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, DATABASE_URL, DATABASE_PATH,
    MIN_RELEVANCE_SCORE, HIGH_PRIORITY_THRESHOLD,
    SCRAPE_DELAY_SECONDS, SOURCES, FIRM_EXPERTISE, KNOWN_CASES,
    VOYAGE_API_KEY, DEDUP_THRESHOLD, AUTO_MERGE_THRESHOLD, REVIEW_THRESHOLD,
)
from database.models import (
    init_database, get_session, commit_with_retry, Lead, RawSource, ScrapeLog,
)
from scrapers.scrapers import build_scrapers
from analysis.claude_analyzer import ClaudeAnalyzer, AnalysisError
from analysis.dedup import SemanticDeduplicator, title_match_key
from registry.pinkas_checker import PinkasChecker

# Stage-1 technical failures are stored as relevance_score=NULL + stage1_error
# and retried on later runs, at most this many classification attempts in total.
MAX_STAGE1_ATTEMPTS = 3
STAGE1_RETRY_BATCH = 50      # bound the retry spend per run

# Words that say nothing about WHICH matter a known firm case is. Matching on
# them flagged every Meta article containing "class action" and every Google
# article (the word "google") as the firm's own case — and the flag hides a
# lead from alerts.
_KNOWN_CASE_GENERIC = {
    "class", "action", "actions", "settlement", "approved", "lawsuit", "suit",
    "claim", "claims", "case", "violations", "parallel", "exclusion",
}


def _known_case_topic_words(topic: str, company: str) -> list:
    """Distinctive topic words of a KNOWN_CASES entry (punctuation stripped,
    generic legal words and the company's own name removed)."""
    company_words = set(re.findall(r"[a-z0-9]+", company.lower()))
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9\-]*", topic.lower()):
        if len(w) >= 3 and w not in _KNOWN_CASE_GENERIC and w not in company_words and w not in out:
            out.append(w)
    return out


class ClassActionScout:
    """Main orchestrator — runs the full discovery pipeline."""

    def __init__(self):
        # Ensure data directory exists
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Initializing database...")
        init_database(DATABASE_URL)
        self.db = get_session(DATABASE_URL)

        logger.info("Initializing Claude analyzer...")
        self.analyzer = ClaudeAnalyzer(api_key=ANTHROPIC_API_KEY, model=CLAUDE_MODEL)

        self.pinkas = PinkasChecker()
        self.scrapers = build_scrapers(SOURCES, SCRAPE_DELAY_SECONDS)
        # Base threshold = REVIEW (0.82); the pipeline tiers matches above it into
        # auto-merge (>= AUTO_MERGE) vs suspected-duplicate review [REVIEW, AUTO).
        self.deduplicator = SemanticDeduplicator(api_key=VOYAGE_API_KEY, threshold=REVIEW_THRESHOLD)
        logger.info(f"Ready. Scrapers: {list(self.scrapers.keys())}")

    # ── Full pipeline ──────────────────────────────────

    def run(self, sources: list[str] = None, skip_pinkas: bool = False):
        """
        Full pipeline:
        1. Scrape sources → raw items
        2. Deduplicate against DB
        3. Stage 1: Claude classification
        4. Stage 2: Deep analysis (high relevance only)
        5. Check פנקס (unless skipped)
        6. Check against known firm cases
        7. Store results
        """
        run_start = datetime.now(timezone.utc)
        logger.info(f"{'='*60}")
        logger.info(f"PIPELINE START — {run_start.isoformat()}")
        logger.info(f"{'='*60}")

        # Per-stage counts for this run — logged at the end even if the run
        # crashes, so "a stage received input and produced nothing" is visible.
        stats = {
            "scrape": {"found": 0, "new": 0, "resumed": 0},
            "stage1": {"classified": 0, "passed": 0, "below_gate": 0, "error": 0, "retried": 0},
            "dedup": {"in": 0, "unique": 0, "merged": 0, "review": 0},
            "stage2": {"in": 0, "ok": 0, "failed": 0},
            "stage3": {"in": 0, "ok": 0, "failed": 0},
        }
        self.last_run_stats = stats

        try:
            # 1. SCRAPE
            all_items = []
            seen_urls = set()
            active_scrapers = {
                k: v for k, v in self.scrapers.items()
                if sources is None or k in sources
            }

            for name, scraper in active_scrapers.items():
                log = ScrapeLog(source_name=name, started_at=datetime.now(timezone.utc))
                try:
                    items = scraper.scrape()
                    log.items_found = len(items)
                    stats["scrape"]["found"] += len(items)

                    # Deduplicate by URL. A source that was saved but never got a
                    # lead (a run died between scraping and the Stage-1 commit) is
                    # resumed instead of being skipped forever.
                    new_items = []
                    n_resumed = 0
                    for item in items:
                        if item.url in seen_urls:
                            continue
                        seen_urls.add(item.url)
                        exists = self.db.query(RawSource).filter_by(url=item.url).first()
                        if not exists:
                            raw = RawSource(
                                source_name=item.source_name,
                                url=item.url,
                                title=item.title,
                                content=item.content,
                                date_published=item.date,
                            )
                            self.db.add(raw)
                            new_items.append((item, raw))
                        elif self._raw_needs_stage1(exists):
                            new_items.append((item, exists))
                            n_resumed += 1

                    log.items_new = len(new_items) - n_resumed
                    log.success = True
                    stats["scrape"]["new"] += len(new_items) - n_resumed
                    stats["scrape"]["resumed"] += n_resumed
                    all_items.extend(new_items)
                    logger.info(
                        f"[{name}] {len(items)} found, {len(new_items) - n_resumed} new"
                        + (f", {n_resumed} resumed (saved earlier, never classified)" if n_resumed else "")
                    )

                except Exception as e:
                    log.success = False
                    log.errors = str(e)
                    logger.error(f"[{name}] scrape failed: {e}")
                finally:
                    log.completed_at = datetime.now(timezone.utc)
                    self.db.add(log)

            commit_with_retry(self.db)
            retry_leads = self._stage1_retry_candidates()
            logger.info(
                f"Total new items to analyze: {len(all_items)}; "
                f"Stage-1 retries due: {len(retry_leads)}"
            )

            if not all_items and not retry_leads:
                logger.info("No new items. Pipeline complete.")
                return

            # 2. STAGE 1 — CLASSIFICATION
            #
            # Three outcomes, kept apart: pass (>= gate), below gate (a real low
            # verdict), and stage1 error (no verdict — NULL score, retried later).
            logger.info("Stage 1: Classification...")
            leads_for_deep = []
            run_leads = []          # every lead touched by Stage 1 in this run

            for item, raw in all_items:
                source_type = SOURCES.get(item.source_name, {}).get("type", "unknown")
                classification = self._classify_safely(item.title, item.content, source_type)
                lead = Lead(
                    title=item.title,
                    source_name=item.source_name,
                    source_url=item.url,
                    source_type=source_type,
                    raw_content=item.content,
                )
                decision = self._apply_stage1(lead, classification)
                # Link through the relationship, not raw.lead_id = lead.id — the
                # id does not exist until flush, so that assignment stored NULL.
                raw.lead = lead
                self.db.add(lead)
                run_leads.append(lead)
                self._count_stage1(stats, decision)
                self._log_stage1(f"raw#{raw.id}", lead, decision)
                if decision == "pass":
                    leads_for_deep.append((lead, item, classification))

            for lead in retry_leads:
                item = SimpleNamespace(
                    title=lead.title, content=lead.raw_content or "",
                    url=lead.source_url, source_name=lead.source_name,
                )
                classification = self._classify_safely(
                    lead.title, lead.raw_content or "", lead.source_type or "unknown"
                )
                decision = self._apply_stage1(lead, classification)
                run_leads.append(lead)
                stats["stage1"]["retried"] += 1
                self._count_stage1(stats, decision)
                self._log_stage1(f"#{lead.id} (retry {lead.stage1_attempts}/{MAX_STAGE1_ATTEMPTS})", lead, decision)
                if decision == "pass":
                    leads_for_deep.append((lead, item, classification))

            commit_with_retry(self.db)
            run_ids = {l.id for l in run_leads}
            logger.info(
                f"Stage 1 → {stats['stage1']['classified']} classified: "
                f"{stats['stage1']['passed']} passed, {stats['stage1']['below_gate']} below gate, "
                f"{stats['stage1']['error']} error; passed ids: "
                + (", ".join(f"#{l.id}" for l, _, _ in leads_for_deep) or "none")
            )

            # 2.5. SEMANTIC DEDUP — never compares a lead with itself: every lead
            # touched in this run is excluded from the stored-lead indexes, and
            # in-batch duplicates are caught against earlier leads of the batch.
            leads_for_analysis = self._semantic_dedup(
                leads_for_deep, exclude_ids=run_ids, stats=stats["dedup"]
            )

            # 3. STAGE 2 — DEEP ANALYSIS
            #
            # Only the leads that come out of here go on to Stage 3. A lead whose
            # legal analysis failed has no strength_score, and scoring it anyway
            # would blend a certification component of 0 into a number that is
            # then stored and displayed like any other verdict.
            leads_scored = []
            if leads_for_analysis:
                logger.info("Stage 2: Deep legal analysis...")
                stage2_failed = 0
                stats["stage2"]["in"] = len(leads_for_analysis)
                for lead, item, classification in leads_for_analysis:
                    try:
                        analysis = self.analyzer.analyze(
                            title=item.title,
                            content=item.content,
                            classification=classification,
                        )
                    except AnalysisError as e:
                        # Leave strength_score/priority NULL so reanalyze_pending
                        # picks this lead up later. Writing a default here would
                        # look like a real verdict and never be revisited.
                        stage2_failed += 1
                        stats["stage2"]["failed"] += 1
                        logger.error(f"  ✗ #{lead.id} Stage 2 failed — left unscored: {e}")
                        continue

                    self._apply_stage2(lead, analysis)
                    leads_scored.append(lead)
                    stats["stage2"]["ok"] += 1

                    priority_icon = "🔴" if lead.priority == "high" else "🟡" if lead.priority == "medium" else "⚪"
                    logger.info(f"  {priority_icon} #{lead.id} [{lead.strength_score}/10] {lead.title[:60]}")

                if stage2_failed:
                    logger.warning(
                        f"  ⚠ Stage 2 failed on {stage2_failed}/{len(leads_for_analysis)} "
                        f"lead(s) — left unscored, and skipped in Stage 3 so no "
                        f"priority_score is written for them"
                    )
                commit_with_retry(self.db)

            # 3. STAGE 3 — VALUE ESTIMATION
            # Runs over leads_scored, not leads_for_analysis: a lead that failed
            # Stage 2 must come out of this run with priority_score still NULL, so
            # it reads as "not analysed" and reanalyze_pending picks it up again.
            if leads_scored:
                logger.info("Stage 3: Value estimation...")
                from analysis.value_estimator import estimate_value
                stage3_failed = 0
                stats["stage3"]["in"] = len(leads_scored)
                for lead in leads_scored:
                    try:
                        estimate_value(lead, self.analyzer.client, self.analyzer.model)
                        stats["stage3"]["ok"] += 1
                    except Exception as e:
                        stage3_failed += 1
                        stats["stage3"]["failed"] += 1
                        logger.warning(f"  value estimation error for lead {lead.id}: {e}")
                if stage3_failed:
                    logger.warning(
                        f"  ⚠ Stage 3 failed on {stage3_failed}/{len(leads_scored)} "
                        f"lead(s) — priority_score left NULL rather than partial"
                    )
                commit_with_retry(self.db)

            # 3.5. STAGE 3.5 — PACER ENRICHMENT
            if leads_for_analysis:
                logger.info("Stage 3.5: PACER Enrichment...")
                try:
                    from scrapers.pacer_monitor import PacerMonitorClient
                    pacer = PacerMonitorClient()
                    if pacer.login():
                        for lead, _, _ in leads_for_analysis:
                            if lead.strength_score and lead.strength_score >= 5 and lead.company:
                                logger.info(f"  PACER lookup: {lead.company}")
                                try:
                                    # Fetch the full article body — case numbers like
                                    # "2:26-cv-1674" appear deep in the text, not in
                                    # the summary that the scraper captured.
                                    full_text = self._fetch_article_text(lead.source_url)
                                    time.sleep(2)  # polite delay after HTTP fetch
                                    if full_text and len(full_text) > len(lead.raw_content or ""):
                                        lead.raw_content = full_text  # cache for future use
                                    search_text = (lead.raw_content or "") + " " + lead.title
                                    case_num = self._extract_case_number(search_text)
                                    if case_num:
                                        pacer_url = self._find_pacer_url(case_num, pacer)
                                        if pacer_url:
                                            details = pacer.get_case_details(pacer_url)
                                            if details:
                                                # Fallback: numeric PacerMonitor ID from URL path
                                                _parts = pacer_url.split('/case/')
                                                pm_id = _parts[1].split('/')[0] if len(_parts) > 1 else ""
                                                # Structured columns — queryable / filterable
                                                lead.pacer_case_number = details.case_number or pm_id
                                                lead.pacer_dismissal_type = details.dismissal_type
                                                lead.pacer_docket_count = len(details.docket_entries)
                                                lead.pacer_url = pacer_url
                                                # Human-readable summary appended to notes
                                                note = (
                                                    f"PACER: {details.case_number} | {details.title}\n"
                                                    f"Dismissal: {details.dismissal_type}\n"
                                                    f"Docket entries: {len(details.docket_entries)}"
                                                )
                                                if details.dismissal_type == "voluntary":
                                                    note += "\n⚠ VOLUNTARY DISMISSAL — plaintiff withdrew, weak case signal"
                                                elif details.dismissal_type == "with_prejudice":
                                                    note += "\n❌ DISMISSED WITH PREJUDICE — case dead on merits"
                                                elif details.dismissal_type == "without_prejudice":
                                                    note += "\n🔍 DISMISSED WITHOUT PREJUDICE — can be refiled, needs review"
                                                elif details.dismissal_type == "settled":
                                                    note += "\n⚠ SETTLEMENT — check if Israeli consumers included or excluded"
                                                else:
                                                    note += "\n✅ CASE ACTIVE — litigation ongoing in US"
                                                lead.notes = (
                                                    (lead.notes + "\n\n" if lead.notes else "") + note
                                                )
                                                logger.info(f"    Found: {details.case_number} [{details.dismissal_type}]")
                                            else:
                                                logger.info(f"    Case page not accessible")
                                        else:
                                            logger.info(f"    No PacerMonitor URL found for {case_num}")
                                    else:
                                        logger.info(f"    No case number found in article")
                                except Exception as e:
                                    logger.warning(f"    PACER error: {e}")
                                time.sleep(2)  # polite delay between PACER page loads
                        pacer.close()
                    else:
                        logger.warning("  PACER login failed — skipping. Run login_interactive() to refresh cookies.")
                except ImportError:
                    logger.info("  PACER module not available — install playwright to enable")
                except Exception as e:
                    logger.warning(f"  PACER stage error: {e}")

                commit_with_retry(self.db)

            # 4. STAGE 4 — PINKAS CHECK
            if not skip_pinkas and leads_for_analysis:
                logger.info("Stage 4: פנקס check...")
                for lead, _, _ in leads_for_analysis:
                    if lead.company:
                        result = self.pinkas.check(lead.company)
                        lead.pinkas_checked = True
                        lead.pinkas_exists = result.get("found", False)
                        lead.pinkas_details = json.dumps(
                            result.get("results", [])[:5], ensure_ascii=False
                        )
                        if lead.pinkas_exists:
                            logger.warning(f"  ⚠ Existing case found for: {lead.company}")
                commit_with_retry(self.db)

            # 5. SUMMARY
            elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
            high_priority = [l for l, _, _ in leads_for_analysis if l.priority == "high"]

            logger.info(f"\n{'='*60}")
            logger.info(f"PIPELINE COMPLETE — {elapsed:.0f}s")
            logger.info(f"  Scraped: {len(all_items)} new items")
            logger.info(f"  Analyzed: {len(leads_for_analysis)} leads (after dedup)")
            logger.info(f"  High priority: {len(high_priority)}")
            # Dedup health — embedded vs total among analysis-eligible canonical leads
            _elig = self.db.query(Lead).filter(
                Lead.relevance_score >= MIN_RELEVANCE_SCORE,
                (Lead.is_duplicate_of_known.is_(None)) | (Lead.is_duplicate_of_known == False),
            )
            _elig_total = _elig.count()
            _elig_embedded = _elig.filter(Lead.embedding.isnot(None)).count()
            _cov = (_elig_embedded / _elig_total) if _elig_total else 1.0
            logger.info(f"  Dedup coverage: {_elig_embedded}/{_elig_total} embedded ({_cov:.0%})")
            if not self.deduplicator.enabled:
                logger.warning("  ⚠ Dedup DISABLED (no VOYAGE_API_KEY) — new leads are not clustered; duplicates will accumulate")
            elif _cov < 0.9:
                logger.warning(f"  ⚠ Dedup coverage {_cov:.0%} < 90% — some eligible leads are unembedded; duplicates may appear")
            logger.info(f"{'='*60}\n")

            # Print high-priority leads
            if high_priority:
                logger.info("🔴 HIGH PRIORITY LEADS:")
                for lead in high_priority:
                    logger.info(f"  • {lead.title}")
                    logger.info(f"    Company: {lead.company}")
                    logger.info(f"    Score: {lead.strength_score}/10")
                    logger.info(f"    Action: {lead.recommended_action}")
                    logger.info("")

        finally:
            self._log_stage_counts(stats)
            # 6. EMAIL ALERTS — in a finally block on purpose: a crash in PACER
            # enrichment, pinkas or the summary must not swallow the alert for
            # leads that already crossed the bar earlier in this run.
            self._send_run_alerts()

    # ── Re-analyze pending leads ────────────────────────

    def reanalyze_pending(self) -> dict:
        """
        Re-run Stage 2 deep analysis on leads that have a relevance score
        but are missing strength_score or priority (i.e. Stage 2 never ran
        or was interrupted).  Called by POST /api/reanalyze.
        """
        pending = (
            self.db.query(Lead)
            .filter(
                Lead.relevance_score.isnot(None),
                (Lead.strength_score.is_(None) | Lead.priority.is_(None)),
            )
            .all()
        )
        logger.info(f"Re-analyzing {len(pending)} pending leads...")
        done = 0
        for lead in pending:
            try:
                # Reconstruct the Stage-1 classification dict from stored columns
                classification = {
                    "relevance_score": lead.relevance_score,
                    "company": lead.company or "",
                    "sector": lead.sector or "",
                    "operates_in_israel": lead.operates_in_israel,
                    "israeli_law_basis": lead.israeli_law_basis or "",
                    "estimated_class_size": lead.estimated_class_size or "",
                }
                analysis = self.analyzer.analyze(
                    title=lead.title,
                    content=lead.raw_content or "",
                    classification=classification,
                )
                lead.strength_score = analysis.get("strength_score", 0)
                lead.priority = analysis.get("priority", "low")
                lead.legal_analysis = analysis.get("legal_analysis", "")
                lead.recommended_action = analysis.get("recommended_action", "")
                lead.comparable_cases = json.dumps(
                    analysis.get("comparable_cases", []), ensure_ascii=False
                )
                # Already-filed detection (conservative — see legal_analysis prompt)
                lead.already_filed_il = bool(analysis.get("already_filed_il", False))
                lead.already_filed_details = analysis.get("already_filed_details", "") or ""
                lead.is_duplicate_of_known = self._check_known_cases(lead)
                lead.matches_expertise = self._check_expertise(lead)
                from analysis.value_estimator import estimate_value
                estimate_value(lead, self.analyzer.client, self.analyzer.model)
                done += 1
                logger.info(f"  [{done}/{len(pending)}] {lead.title[:60]}")
            except Exception as e:
                logger.warning(f"  reanalyze error for lead {lead.id}: {e}")

        commit_with_retry(self.db)
        return {"reanalyzed": done, "total": len(pending)}

    # ── On-demand PACER enrichment ─────────────────────

    def run_pacer_enrichment(self, min_strength: float = 5.0) -> dict:
        """
        Standalone PACER enrichment pass — runs without the full pipeline.
        Queries all leads with strength_score >= min_strength and enriches any
        that don't yet have a pacer_url.  Called by POST /api/run-pacer.
        """
        leads = (
            self.db.query(Lead)
            .filter(Lead.strength_score >= min_strength, Lead.company.isnot(None))
            .all()
        )
        enriched = 0
        try:
            from scrapers.pacer_monitor import PacerMonitorClient
            pacer = PacerMonitorClient()
            if not pacer.login():
                return {"error": "PACER login failed — refresh cookies via login_interactive()"}

            for lead in leads:
                try:
                    full_text = self._fetch_article_text(lead.source_url)
                    time.sleep(2)
                    if full_text and len(full_text) > len(lead.raw_content or ""):
                        lead.raw_content = full_text
                    search_text = (lead.raw_content or "") + " " + lead.title
                    case_num = self._extract_case_number(search_text)
                    if not case_num:
                        continue
                    pacer_url = self._find_pacer_url(case_num, pacer)
                    if not pacer_url:
                        continue
                    details = pacer.get_case_details(pacer_url)
                    if not details:
                        continue
                    _parts = pacer_url.split('/case/')
                    pm_id = _parts[1].split('/')[0] if len(_parts) > 1 else ""
                    lead.pacer_case_number = details.case_number or pm_id
                    lead.pacer_dismissal_type = details.dismissal_type
                    lead.pacer_docket_count = len(details.docket_entries)
                    lead.pacer_url = pacer_url
                    note = (
                        f"PACER: {details.case_number} | {details.title}\n"
                        f"Dismissal: {details.dismissal_type}\n"
                        f"Docket entries: {len(details.docket_entries)}"
                    )
                    if details.dismissal_type == "voluntary":
                        note += "\n⚠ VOLUNTARY DISMISSAL — plaintiff withdrew, weak case signal"
                    elif details.dismissal_type == "with_prejudice":
                        note += "\n❌ DISMISSED WITH PREJUDICE — case dead on merits"
                    elif details.dismissal_type == "without_prejudice":
                        note += "\n🔍 DISMISSED WITHOUT PREJUDICE — can be refiled, needs review"
                    elif details.dismissal_type == "settled":
                        note += "\n⚠ SETTLEMENT — check if Israeli consumers included or excluded"
                    else:
                        note += "\n✅ CASE ACTIVE — litigation ongoing in US"
                    lead.notes = (lead.notes + "\n\n" if lead.notes else "") + note
                    enriched += 1
                    logger.info(f"PACER enriched: {lead.company} → {details.case_number}")
                except Exception as e:
                    logger.warning(f"PACER enrichment error ({lead.company}): {e}")
                time.sleep(2)

            pacer.close()
            commit_with_retry(self.db)
        except ImportError:
            return {"error": "playwright not installed — run: pip install playwright && playwright install chromium"}
        except Exception as e:
            logger.error(f"run_pacer_enrichment failed: {e}")
            return {"error": str(e)}

        return {"enriched": enriched, "total": len(leads)}

    # ── Report generation ──────────────────────────────

    def print_report(self, days: int = 7, format: str = "text"):
        """Generate and print a report of recent leads."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        leads = (
            self.db.query(Lead)
            .filter(Lead.scraped_at >= since)
            .order_by(Lead.strength_score.desc().nullslast())
            .all()
        )

        if format == "text":
            self._print_text_report(leads, days)
        elif format == "html":
            return self._generate_html_report(leads, days)

    def _print_text_report(self, leads, days):
        print(f"\n{'='*60}")
        print(f"CLASS ACTION SCOUT — דו\"ח {days} ימים אחרונים")
        print(f"Generated: {datetime.now().isoformat()}")
        print(f"Total leads: {len(leads)}")
        print(f"{'='*60}\n")

        high = [l for l in leads if l.priority == "high"]
        medium = [l for l in leads if l.priority == "medium"]

        if high:
            print("🔴 HIGH PRIORITY")
            print("-" * 40)
            for l in high:
                self._print_lead(l)

        if medium:
            print("\n🟡 MEDIUM PRIORITY")
            print("-" * 40)
            for l in medium:
                self._print_lead(l)

        if not high and not medium:
            print("אין ממצאים בעדיפות גבוהה או בינונית.")

    def _print_lead(self, lead):
        dup = " [DUPLICATE]" if lead.is_duplicate_of_known else ""
        pinkas = " [EXISTS IN PINKAS]" if lead.pinkas_exists else ""
        print(f"\n  📌 {lead.title}{dup}{pinkas}")
        print(f"     Company: {lead.company}")
        print(f"     Source: {lead.source_name}")
        print(f"     Relevance: {lead.relevance_score}/10 | Strength: {lead.strength_score}/10")
        if lead.legal_analysis:
            # Print first 200 chars of analysis
            print(f"     Analysis: {lead.legal_analysis[:200]}...")
        if lead.recommended_action:
            print(f"     Action: {lead.recommended_action}")
        print(f"     URL: {lead.source_url}")

    def _generate_html_report(self, leads, days):
        """Generate HTML report (for email or dashboard)."""
        analyzed = [l for l in leads if l.relevance_score and l.relevance_score >= 4]
        unanalyzed = [l for l in leads if not l.relevance_score or l.relevance_score < 4]

        html = f"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head><meta charset="UTF-8"><title>Class Action Scout Report</title>
<style>
body {{ font-family: David, Arial, sans-serif; direction: rtl; padding: 20px; max-width: 900px; margin: 0 auto; background: #f9f9f9; }}
h1 {{ color: #1a365d; border-bottom: 3px solid #2c5282; padding-bottom: 10px; }}
.lead {{ background: white; border-radius: 8px; padding: 16px; margin: 12px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.high {{ border-right: 5px solid #e53e3e; }}
.medium {{ border-right: 5px solid #ecc94b; }}
.low {{ border-right: 5px solid #a0aec0; }}
.score {{ display: inline-block; background: #2c5282; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; }}
.tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; margin-left: 4px; }}
.tag-high {{ background: #fed7d7; color: #9b2c2c; }}
.tag-medium {{ background: #fefcbf; color: #975a16; }}
.tag-low {{ background: #e2e8f0; color: #4a5568; }}
.meta {{ color: #718096; font-size: 0.9em; margin: 4px 0; }}
.analysis {{ background: #f7fafc; padding: 12px; border-radius: 4px; margin-top: 8px; line-height: 1.6; }}
.action {{ background: #ebf8ff; padding: 10px; border-radius: 4px; margin-top: 8px; font-weight: bold; }}
a {{ color: #2c5282; }}
.summary {{ background: #ebf8ff; padding: 16px; border-radius: 8px; margin-bottom: 20px; }}
</style>
</head>
<body>
<h1>דו"ח סוכן תובענות ייצוגיות — {days} ימים אחרונים</h1>
<div class="summary">
<p>נוצר: {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
<p>סה"כ פריטים שנסרקו: {len(leads)} | נותחו לעומק: {len(analyzed)} | לא רלוונטיים: {len(unanalyzed)}</p>
</div>
"""
        if not analyzed:
            html += "<p>לא נמצאו ממצאים רלוונטיים בתקופה זו.</p>"
        else:
            for lead in analyzed:
                priority = lead.priority or "low"
                css = priority if priority in ("high", "medium", "low") else "low"
                tag_css = f"tag-{css}"
                priority_he = {"high": "גבוהה", "medium": "בינונית", "low": "נמוכה"}.get(priority, priority)

                dup_badge = ' <span class="tag" style="background:#fed7d7;color:#9b2c2c">כפילות — תיק קיים</span>' if lead.is_duplicate_of_known else ""
                pinkas_badge = ' <span class="tag" style="background:#fefcbf;color:#975a16">קיים בפנקס</span>' if lead.pinkas_exists else ""
                expertise_badge = f' <span class="tag" style="background:#c6f6d5;color:#276749">{lead.expertise_area}</span>' if lead.matches_expertise else ""

                html += f"""
<div class="lead {css}">
  <h3>{lead.title}{dup_badge}{pinkas_badge}</h3>
  <p>
    <span class="score">רלוונטיות: {lead.relevance_score}/10</span>
    <span class="score">חוזק: {lead.strength_score or 0}/10</span>
    <span class="tag {tag_css}">עדיפות: {priority_he}</span>
    {expertise_badge}
  </p>
  <p class="meta"><strong>חברה:</strong> {lead.company or 'לא זוהתה'} | <strong>מקור:</strong> {lead.source_name} | <strong>סקטור:</strong> {lead.sector or ''}</p>
  <p class="meta"><strong>עילה משפטית:</strong> {lead.israeli_law_basis or 'לא זוהתה'} | <strong>גודל קבוצה:</strong> {lead.estimated_class_size or ''}</p>
"""
                if lead.legal_analysis:
                    html += f'  <div class="analysis">{lead.legal_analysis}</div>\n'
                if lead.recommended_action:
                    html += f'  <div class="action">המלצה: {lead.recommended_action}</div>\n'
                html += f'  <p class="meta"><a href="{lead.source_url}" target="_blank">קישור למקור</a></p>\n'
                html += "</div>\n"

        html += "</body></html>"
        report_path = Path("reports") / f"report_{datetime.now():%Y%m%d_%H%M}.html"
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(html, encoding="utf-8")
        logger.info(f"HTML report saved: {report_path}")
        print(f"Report saved: {report_path}")
        return html

    # ── Internal helpers ───────────────────────────────

    def _send_run_alerts(self) -> None:
        """
        Email a digest of leads that crossed HIGH_PRIORITY_THRESHOLD and have not
        been alerted yet.

        The send condition is per-lead state, not a time window: a lead qualifies
        when priority_score >= HIGH_PRIORITY_THRESHOLD and alerted_at IS NULL.
        alerted_at is stamped only after a successful send, so a lead scored in a
        run that later crashed — or one rescored by a backfill — is picked up by
        the next run instead of being lost, and can never be alerted twice.
        """
        from alerts.email_sender import send_alert_email
        from database.models import AlertLog

        try:
            pending = (
                self.db.query(Lead)
                .filter(
                    Lead.priority_score >= HIGH_PRIORITY_THRESHOLD,
                    Lead.alerted_at.is_(None),
                    (Lead.is_duplicate_of_known.is_(None)) | (Lead.is_duplicate_of_known == False),
                )
                .order_by(Lead.priority_score.desc())
                .all()
            )

            if not pending:
                logger.info("No un-alerted leads over the threshold — no email sent")
                return

            lead_dicts = [
                {
                    "id": l.id,
                    "title": l.title,
                    "company": l.company or "",
                    "source_name": l.source_name or "",
                    "recommended_action": l.recommended_action or "",
                    "strength_score": l.strength_score,
                    "priority_score": l.priority_score,
                }
                for l in pending
            ]

            ok = send_alert_email(lead_dicts)
            if ok:
                # Stamp only on success — a failed send leaves alerted_at NULL so
                # the next run retries these same leads.
                stamped = datetime.utcnow()
                for l in pending:
                    l.alerted_at = stamped
            self.db.add(AlertLog(lead_count=len(pending), status="sent" if ok else "error"))
            commit_with_retry(self.db)

            if ok:
                logger.info(f"Sent alert email with {len(pending)} lead(s) over {HIGH_PRIORITY_THRESHOLD}")
            else:
                logger.warning("Alert email failed — leads stay un-alerted and retry next run")

        except Exception as e:
            logger.error(f"_send_run_alerts error: {e}")

    # ── Stage-1 helpers ────────────────────────────────

    def _raw_needs_stage1(self, raw: RawSource) -> bool:
        """
        A saved source that never produced a lead. Checked by URL against leads
        as well as raw.lead_id, because lead_id was never populated before this
        fix — relying on it alone would re-classify the whole history.
        """
        if raw.lead_id is not None:
            return False
        return self.db.query(Lead.id).filter(Lead.source_url == raw.url).first() is None

    def _stage1_retry_candidates(self) -> list:
        """Leads whose Stage 1 failed technically and still have attempts left."""
        from sqlalchemy import func
        return (
            self.db.query(Lead)
            .filter(
                Lead.relevance_score.is_(None),
                Lead.stage1_error.isnot(None),
                func.coalesce(Lead.stage1_attempts, 0) < MAX_STAGE1_ATTEMPTS,
            )
            .order_by(Lead.id)
            .limit(STAGE1_RETRY_BATCH)
            .all()
        )

    def _classify_safely(self, title: str, content: str, source_type: str) -> dict:
        """
        One item's classification failing must not take the run down with it.
        Any ordinary exception becomes a Stage-1 error (NULL, retried).
        """
        try:
            return self.analyzer.classify(title=title, content=content, source_type=source_type)
        except Exception as e:
            logger.error(f"  Stage 1 raised for '{title[:50]}': {e}")
            return {"relevance_score": None, "stage1_error": f"exception: {e}"[:500]}

    @staticmethod
    def _apply_stage1(lead: Lead, c: dict) -> str:
        """Write a Stage-1 result onto the lead. Returns pass | below_gate | error."""
        lead.stage1_attempts = (lead.stage1_attempts or 0) + 1
        score = c.get("relevance_score")
        if c.get("stage1_error") or score is None:
            lead.relevance_score = None
            lead.stage1_error = c.get("stage1_error") or "no relevance_score returned"
            return "error"
        lead.stage1_error = None
        lead.relevance_score = float(score)
        lead.company = c.get("company", "") or ""
        lead.sector = c.get("sector", "") or ""
        lead.relevance_reasoning = c.get("reasoning", "") or ""
        lead.operates_in_israel = c.get("operates_in_israel")
        lead.product_available_in_israel = c.get("product_available_in_israel")
        lead.israeli_class_size_estimate = c.get("israeli_class_size_estimate", "") or ""
        lead.israeli_law_basis = c.get("israeli_law_basis", "") or ""
        lead.estimated_class_size = c.get("estimated_class_size", "") or ""
        return "pass" if lead.relevance_score >= MIN_RELEVANCE_SCORE else "below_gate"

    @staticmethod
    def _count_stage1(stats: dict, decision: str) -> None:
        stats["stage1"]["classified"] += 1
        key = {"pass": "passed", "below_gate": "below_gate", "error": "error"}[decision]
        stats["stage1"][key] += 1

    @staticmethod
    def _log_stage1(ref: str, lead: Lead, decision: str) -> None:
        title = (lead.title or "")[:60]
        if decision == "pass":
            logger.info(f"  ✓ {ref} [{lead.relevance_score}/10] {title} — pass (>= {MIN_RELEVANCE_SCORE})")
        elif decision == "below_gate":
            logger.info(f"  ✗ {ref} [{lead.relevance_score}/10] {title} — below gate (< {MIN_RELEVANCE_SCORE})")
        else:
            logger.warning(f"  ⚠ {ref} {title} — stage1 error, stored NULL for retry: {lead.stage1_error}")

    @staticmethod
    def _log_stage_counts(stats: dict) -> None:
        st = stats
        logger.info(
            "STAGE COUNTS — "
            f"scrape found={st['scrape']['found']} new={st['scrape']['new']} resumed={st['scrape']['resumed']} | "
            f"stage1 classified={st['stage1']['classified']} passed={st['stage1']['passed']} "
            f"below_gate={st['stage1']['below_gate']} error={st['stage1']['error']} retried={st['stage1']['retried']} | "
            f"dedup in={st['dedup']['in']} unique={st['dedup']['unique']} merged={st['dedup']['merged']} "
            f"review={st['dedup']['review']} | "
            f"stage2 in={st['stage2']['in']} ok={st['stage2']['ok']} failed={st['stage2']['failed']} | "
            f"stage3 in={st['stage3']['in']} ok={st['stage3']['ok']} failed={st['stage3']['failed']}"
        )
        if st["dedup"]["in"] and not st["dedup"]["unique"]:
            logger.warning(
                f"  ⚠ every lead that passed Stage 1 ({st['dedup']['in']}) was marked a duplicate "
                f"— nothing reached Stage 2. Check the dedup decisions above."
            )

    # ── Semantic dedup ─────────────────────────────────

    def _semantic_dedup(self, leads_for_deep: list, exclude_ids=None, stats: dict = None,
                        commit: bool = True) -> list:
        """
        Decide, for each lead that passed Stage 1, whether it duplicates another
        record. Returns only the leads that go on to Stage 2.

        A lead is never compared with itself. Stage 1 commits its leads before
        this runs, so the stored-lead indexes are built without `exclude_ids`
        (every lead touched in this run); in-batch duplicates are caught against
        earlier leads of the same batch, which are added to the indexes as they
        are accepted.

        Only leads that passed the relevance gate can absorb a newcomer — a
        lead that was never analysed is not a canonical record.
        """
        exclude_ids = set(exclude_ids or ()) | {l.id for l, _, _ in leads_for_deep if l.id is not None}
        stats = stats if stats is not None else {}
        for k in ("in", "unique", "merged", "review"):
            stats.setdefault(k, 0)
        stats["in"] += len(leads_for_deep)
        if not leads_for_deep:
            return []

        def _not_self(q):
            return q.filter(Lead.id.notin_(exclude_ids)) if exclude_ids else q

        enabled = self.deduplicator.enabled
        if not enabled:
            logger.warning(
                "Semantic dedup DISABLED (no VOYAGE_API_KEY / voyageai missing) — "
                "identical-title duplicates are still caught, but new leads are "
                "not embedded. Set VOYAGE_API_KEY on the web service to enable."
            )

        # Stored embeddings (gate-passing, not this run) → vectorized index.
        # A stored DUPLICATE whose group is one of the leads being decided here
        # is skipped: matching it would make the lead a duplicate of its own
        # duplicate — a loop with no canonical, so both disappear.
        excluded_groups = {str(i) for i in exclude_ids}
        existing_with_embs = []
        if enabled:
            for db_lead in _not_self(self.db.query(Lead).filter(
                Lead.embedding.isnot(None),
                Lead.relevance_score >= MIN_RELEVANCE_SCORE,
            )).all():
                if db_lead.is_duplicate_of_known and (db_lead.dedup_group_id or "") in excluded_groups:
                    continue
                try:
                    existing_with_embs.append((db_lead, json.loads(db_lead.embedding)))
                except Exception:
                    pass
        existing_index = self.deduplicator.build_index(existing_with_embs) if existing_with_embs else None

        # Exact-title index over stored canonical, gate-passing leads (not this run)
        title_index = {}
        for db_lead in _not_self(self.db.query(Lead).filter(
            (Lead.is_duplicate_of_known.is_(None)) | (Lead.is_duplicate_of_known == False),
            Lead.relevance_score >= MIN_RELEVANCE_SCORE,
        )).all():
            k = title_match_key(db_lead.title, db_lead.company)
            if k and k not in title_index:
                title_index[k] = db_lead

        # Batch-embed all new leads up front (one Voyage call per ~100)
        texts = [
            f"{(l.company or '')} | {l.title} | {(l.israeli_law_basis or '')}"[:500]
            for l, _, _ in leads_for_deep
        ]
        new_embs = self._embed_batched(texts) if enabled else [[] for _ in texts]

        unique = []
        batch_embs = []  # (lead, emb) for accepted leads of this batch
        n_dup = 0
        n_dup_title = 0
        n_review = 0

        def _accept(lead, tkey):
            if tkey and tkey not in title_index:
                title_index[tkey] = lead

        for (lead, item, classification), emb in zip(leads_for_deep, new_embs):
            # Tier 0 — same headline, same company. Text-only, so it works with
            # Voyage off or failing too.
            tkey = title_match_key(lead.title, lead.company)
            tmatch = title_index.get(tkey) if tkey else None
            if tmatch is not None and tmatch.id == lead.id:
                tmatch = None     # belt and braces: a record is never its own duplicate
            if tmatch is not None:
                lead.is_duplicate_of_known = True
                lead.dedup_group_id = tmatch.dedup_group_id or str(tmatch.id)
                lead.known_case_ref = tmatch.title
                if emb:
                    lead.embedding = json.dumps(emb)
                note = f"🔁 כפילות של ליד #{tmatch.id} (כותרת זהה)"
                lead.notes = (lead.notes + "\n" if lead.notes else "") + note
                n_dup += 1
                n_dup_title += 1
                logger.info(f"  [DEDUP] #{lead.id} duplicate of #{tmatch.id} — identical title + company")
                continue

            if not emb:
                # No embedding (Voyage off or failed) — keep it, unclustered, but
                # still let later identical titles in this batch merge into it.
                unique.append((lead, item, classification))
                _accept(lead, tkey)
                logger.info(f"  [DEDUP] #{lead.id} unique (no embedding — unclustered)")
                continue

            # Best match against stored leads (vectorized) and this batch.
            # deduplicator.threshold == REVIEW_THRESHOLD; tiered below.
            candidates = []
            if existing_index is not None:
                m, sc = self.deduplicator.find_duplicate_indexed(emb, existing_index)
            else:
                # No numpy -> pure-Python scan. Slower, but never silently skipped.
                m, sc = self.deduplicator.find_duplicate(emb, existing_with_embs)
            if m is not None and m.id != lead.id:
                candidates.append((sc, m))
            mb, sb = self.deduplicator.find_duplicate(emb, batch_embs)
            if mb is not None and mb.id != lead.id:
                candidates.append((sb, mb))
            score, match = max(candidates, key=lambda c: c[0]) if candidates else (0.0, None)

            if match is not None and score >= AUTO_MERGE_THRESHOLD:
                lead.is_duplicate_of_known = True
                lead.dedup_group_id = match.dedup_group_id or str(match.id)
                lead.known_case_ref = match.title
                note = f"🔁 כפילות של ליד #{match.id} (דמיון {score:.0%})"
                lead.notes = (lead.notes + "\n" if lead.notes else "") + note
                n_dup += 1
                logger.info(f"  [DEDUP] #{lead.id} duplicate of #{match.id} — similarity {score:.0%}")
            elif match is not None and score >= REVIEW_THRESHOLD:
                lead.embedding = json.dumps(emb)
                lead.dedup_group_id = str(lead.id)
                lead.suspected_dup_of = match.id
                lead.suspected_dup_score = round(score, 4)
                lead.dup_review = "pending"
                unique.append((lead, item, classification))
                batch_embs.append((lead, emb))
                _accept(lead, tkey)
                n_review += 1
                logger.info(f"  [DEDUP] #{lead.id} kept, flagged for review vs #{match.id} — similarity {score:.0%}")
            else:
                lead.embedding = json.dumps(emb)
                lead.dedup_group_id = str(lead.id)
                unique.append((lead, item, classification))
                batch_embs.append((lead, emb))
                _accept(lead, tkey)
                logger.info(f"  [DEDUP] #{lead.id} unique")

        stats["unique"] += len(unique)
        stats["merged"] += n_dup
        stats["review"] += n_review
        if commit:
            commit_with_retry(self.db)
        n_embedded = sum(1 for e in new_embs if e)
        logger.info(
            f"Dedup: {len(unique)} unique, {n_dup} auto-merged "
            f"({n_dup_title} by identical title, {n_dup - n_dup_title} by embedding), "
            f"{n_review} flagged for review; "
            f"embedded {n_embedded}/{len(new_embs)} new leads"
        )
        if n_embedded < len(new_embs):
            logger.warning(
                f"  ⚠ {len(new_embs) - n_embedded} new leads failed to embed "
                f"(Voyage error/rate limit) — they remain unclustered"
            )
        return unique

    def _embed_batched(self, texts: list, chunk: int = 100, max_retries: int = 3) -> list:
        """
        Embed texts in chunks with rate-limit retry. Returns a list aligned 1:1
        with `texts`; any chunk that ultimately fails yields [] for its members
        (so those leads stay unclustered rather than crashing the scan).
        """
        if not texts:
            return []
        out = []
        chunks = [texts[i:i + chunk] for i in range(0, len(texts), chunk)]
        for ci, ch in enumerate(chunks):
            embs = [[] for _ in ch]
            for attempt in range(1, max_retries + 1):
                try:
                    embs = self.deduplicator.compute_embeddings_batch(ch)
                    break
                except Exception as e:
                    rate = "ratelimit" in type(e).__name__.lower() or "rate limit" in str(e).lower()
                    if attempt == max_retries:
                        logger.warning(f"  [DEDUP] embed chunk failed after {attempt} tries: {e}")
                        embs = [[] for _ in ch]
                        break
                    wait = 15 * attempt
                    logger.info(
                        f"  [DEDUP] Voyage {'rate limit' if rate else 'error'}: "
                        f"retry in {wait}s ({attempt}/{max_retries})"
                    )
                    time.sleep(wait)
            out.extend(embs)
            if ci < len(chunks) - 1:
                time.sleep(2)  # small politeness gap between chunks
        return out

    def _apply_stage2(self, lead: Lead, analysis: dict) -> None:
        """Write a Stage-2 result onto the lead (shared by run() and the restore script)."""
        lead.legal_analysis = analysis.get("legal_analysis", "")
        lead.strength_score = analysis.get("strength_score", 0)
        lead.priority = analysis.get("priority", "low")
        lead.recommended_action = analysis.get("recommended_action", "")
        lead.comparable_cases = json.dumps(
            analysis.get("comparable_cases", []), ensure_ascii=False
        )
        # Already-filed detection (conservative — see legal_analysis prompt)
        lead.already_filed_il = bool(analysis.get("already_filed_il", False))
        lead.already_filed_details = analysis.get("already_filed_details", "") or ""
        # Firm known cases, then expertise
        lead.is_duplicate_of_known = self._check_known_cases(lead)
        if lead.is_duplicate_of_known:
            logger.info(f"  [KNOWN] #{lead.id} matches firm case: {lead.known_case_ref}")
        lead.matches_expertise = self._check_expertise(lead)

    def _check_known_cases(self, lead: Lead) -> bool:
        """
        Check if this lead matches a case the firm already knows about.
        Matches on BOTH company name AND topic — so Amazon Fire TV ≠ Amazon Buy Box.
        """
        if not lead.company:
            return False
        company_lower = lead.company.lower()
        lead_text = f"{lead.title} {lead.raw_content or ''}".lower()

        for known in KNOWN_CASES:
            known_lower = known["name"].lower()
            # Extract company name from known case (before the dash)
            known_company = known_lower.split("—")[0].strip() if "—" in known_lower else known_lower
            # Extract topic keywords from known case (after the dash)
            known_topic = known_lower.split("—")[1].strip() if "—" in known_lower else ""

            # Company must match
            if known_company not in company_lower and company_lower not in known_company:
                continue

            if known_topic:
                words = _known_case_topic_words(known_topic, known_company)
                hits = [w for w in words if re.search(rf"\b{re.escape(w)}\b", lead_text)]
                need = min(2, len(words))
                if words and len(hits) >= need:
                    lead.known_case_ref = known["name"]
                    return True
                # Company matches but topic is different — NOT a duplicate
                # (e.g. Amazon Fire TV vs Amazon Buy Box)
            else:
                # No topic in known case — match on company alone
                lead.known_case_ref = known["name"]
                return True

        return False

    def _check_expertise(self, lead: Lead) -> bool:
        """Check if the lead matches firm expertise areas."""
        text = f"{lead.title} {lead.sector} {lead.israeli_law_basis}".lower()
        for area in FIRM_EXPERTISE:
            keywords = area.lower().split(" / ")
            if any(kw in text for kw in keywords):
                lead.expertise_area = area
                return True
        return False

    @staticmethod
    def _fetch_article_text(url: str) -> str:
        """
        Fetch the full article body from url using requests + BeautifulSoup.

        Tries common article-body selectors in order; falls back to the entire
        <body> (with nav/header/footer/script/style stripped) if none match.
        Returns an empty string on any network or parse error so the caller
        can safely fall back to lead.raw_content.
        """
        if not url:
            return ""
        try:
            import requests
            from bs4 import BeautifulSoup
            resp = requests.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    )
                },
                timeout=15,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for selector in (
                ".article-body",
                ".entry-content",
                "article",
                ".post-content",
                "main",
            ):
                el = soup.select_one(selector)
                if el:
                    return el.get_text(separator=" ", strip=True)
            # Fallback: whole body minus chrome elements
            for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)
        except Exception as e:
            logger.debug("[fetch_article] %s — %s", url, e)
            return ""

    @staticmethod
    def _extract_case_number(text: str) -> str:
        """
        Extract US federal case number from text.
        Patterns: 3:25-md-03166, 1:26-cv-03847, 2:24-cv-02391, etc.
        """
        import re
        # Match only the case types relevant to class/mass actions
        _CORE = r'\d:\d{2}-(?:md|cv|mc|ml)-\d{4,6}'
        patterns = [
            r'Case\s+' + _CORE,   # Case 3:25-md-03166
            r'No\.\s*'  + _CORE,  # No. 3:25-md-03166
            _CORE,                # bare 3:25-cv-02391
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                core = re.search(_CORE, match.group(), re.IGNORECASE)
                if core:
                    return core.group()
        return ""

    @staticmethod
    def _find_pacer_url(case_number: str, pacer_client) -> str:
        """
        Find the PacerMonitor case page URL using the authenticated Playwright
        session by submitting the site's own search form.

        The ?q= URL parameter approach returns 0 results — the site requires
        a form POST / JS submit to trigger the search backend.
        """
        try:
            page = pacer_client._page
            if page is None:
                logger.warning("_find_pacer_url: browser page is not open")
                return ""

            page.goto("https://www.pacermonitor.com/search", timeout=20000)

            # The visible main-form input has class="input-lg"; the hidden
            # header bar shares name="querystring" but lacks that class.
            page.wait_for_selector("input.input-lg[name='querystring']", timeout=10000)
            search_input = page.locator("input.input-lg[name='querystring']")
            search_input.fill(case_number)
            search_input.press("Enter")
            time.sleep(4)  # wait for JS-rendered results

            for link in page.query_selector_all("a[href*='/case/']"):
                href = link.get_attribute("href") or ""
                if "/case/" in href:
                    if not href.startswith("http"):
                        href = f"https://www.pacermonitor.com{href}"
                    return href

        except Exception as e:
            logger.warning("PacerMonitor search for %s failed: %s", case_number, e)
        return ""


# ── CLI ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Class Action Scout 🔍⚖️")
    parser.add_argument("--run-now", action="store_true", help="Run full pipeline now")
    parser.add_argument("--report", action="store_true", help="Generate report")
    parser.add_argument("--days", type=int, default=7, help="Report period in days")
    parser.add_argument("--format", choices=["text", "html"], default="text", help="Report format")
    parser.add_argument("--sources", nargs="+", help="Specific sources to scrape")
    parser.add_argument("--skip-pinkas", action="store_true", help="Skip פנקס check")

    args = parser.parse_args()
    scout = ClassActionScout()

    if args.run_now:
        scout.run(sources=args.sources, skip_pinkas=args.skip_pinkas)
        scout.print_report(days=1)
    elif args.report:
        scout.print_report(days=args.days, format=args.format)
    else:
        parser.print_help()
        print("\nExamples:")
        print("  python main.py --run-now")
        print("  python main.py --run-now --sources classaction_org topclassactions")
        print("  python main.py --run-now --skip-pinkas")
        print("  python main.py --report --days 30 --format html")


if __name__ == "__main__":
    main()
