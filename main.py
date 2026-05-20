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
import json
import logging
import argparse
import time
from datetime import datetime, timedelta, timezone
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
)
from database.models import init_database, get_session, Lead, RawSource, ScrapeLog
from scrapers.scrapers import build_scrapers
from analysis.claude_analyzer import ClaudeAnalyzer
from registry.pinkas_checker import PinkasChecker


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

        # 1. SCRAPE
        all_items = []
        active_scrapers = {
            k: v for k, v in self.scrapers.items()
            if sources is None or k in sources
        }

        for name, scraper in active_scrapers.items():
            log = ScrapeLog(source_name=name, started_at=datetime.now(timezone.utc))
            try:
                items = scraper.scrape()
                log.items_found = len(items)

                # Deduplicate
                new_items = []
                for item in items:
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

                log.items_new = len(new_items)
                log.success = True
                all_items.extend(new_items)
                logger.info(f"[{name}] {len(items)} found, {len(new_items)} new")

            except Exception as e:
                log.success = False
                log.errors = str(e)
                logger.error(f"[{name}] scrape failed: {e}")
            finally:
                log.completed_at = datetime.now(timezone.utc)
                self.db.add(log)

        self.db.commit()
        logger.info(f"Total new items to analyze: {len(all_items)}")

        if not all_items:
            logger.info("No new items. Pipeline complete.")
            return

        # 2. STAGE 1 — CLASSIFICATION
        logger.info("Stage 1: Classification...")
        leads_for_deep = []

        for item, raw in all_items:
            source_type = SOURCES.get(item.source_name, {}).get("type", "unknown")
            classification = self.analyzer.classify(
                title=item.title,
                content=item.content,
                source_type=source_type,
            )

            score = classification.get("relevance_score", 0)
            lead = Lead(
                title=item.title,
                source_name=item.source_name,
                source_url=item.url,
                source_type=source_type,
                company=classification.get("company", ""),
                sector=classification.get("sector", ""),
                raw_content=item.content,
                relevance_score=score,
                relevance_reasoning=classification.get("reasoning", ""),
                operates_in_israel=classification.get("operates_in_israel"),
                israeli_law_basis=classification.get("israeli_law_basis", ""),
                estimated_class_size=classification.get("estimated_class_size", ""),
            )

            # Link to raw source
            raw.lead_id = lead.id
            self.db.add(lead)

            if score >= MIN_RELEVANCE_SCORE:
                leads_for_deep.append((lead, item, classification))
                logger.info(f"  ✓ [{score}/10] {item.title[:60]}")
            else:
                logger.info(f"  ✗ [{score}/10] {item.title[:60]} — skipped")

        self.db.commit()
        logger.info(f"Leads for deep analysis: {len(leads_for_deep)}")

        # 3. STAGE 2 — DEEP ANALYSIS
        if leads_for_deep:
            logger.info("Stage 2: Deep legal analysis...")
            for lead, item, classification in leads_for_deep:
                analysis = self.analyzer.analyze(
                    title=item.title,
                    content=item.content,
                    classification=classification,
                )

                lead.legal_analysis = analysis.get("legal_analysis", "")
                lead.strength_score = analysis.get("strength_score", 0)
                lead.priority = analysis.get("priority", "low")
                lead.recommended_action = analysis.get("recommended_action", "")
                lead.comparable_cases = json.dumps(
                    analysis.get("comparable_cases", []), ensure_ascii=False
                )

                # Check against known cases
                lead.is_duplicate_of_known = self._check_known_cases(lead)

                # Check expertise match
                lead.matches_expertise = self._check_expertise(lead)

                priority_icon = "🔴" if lead.priority == "high" else "🟡" if lead.priority == "medium" else "⚪"
                logger.info(f"  {priority_icon} [{lead.strength_score}/10] {lead.title[:60]}")

            self.db.commit()

        # 3.5. STAGE 3.5 — PACER ENRICHMENT
        if leads_for_deep:
            logger.info("Stage 3.5: PACER Enrichment...")
            try:
                from scrapers.pacer_monitor import PacerMonitorClient
                pacer = PacerMonitorClient()
                if pacer.login():
                    for lead, _, _ in leads_for_deep:
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
                                            if details.dismissal_type in ("voluntary", "without_prejudice"):
                                                note += "\n⚡ VOLUNTARY DISMISSAL — case alive for IL filing"
                                            elif details.dismissal_type == "settled":
                                                note += "\n⚠ SETTLEMENT — check if IL consumers included/excluded"
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

            self.db.commit()

        # 4. STAGE 4 — PINKAS CHECK
        if not skip_pinkas and leads_for_deep:
            logger.info("Stage 4: פנקס check...")
            for lead, _, _ in leads_for_deep:
                if lead.company:
                    result = self.pinkas.check(lead.company)
                    lead.pinkas_checked = True
                    lead.pinkas_exists = result.get("found", False)
                    lead.pinkas_details = json.dumps(
                        result.get("results", [])[:5], ensure_ascii=False
                    )
                    if lead.pinkas_exists:
                        logger.warning(f"  ⚠ Existing case found for: {lead.company}")
            self.db.commit()

        # 5. SUMMARY
        elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
        high_priority = [l for l, _, _ in leads_for_deep if l.priority == "high"]

        logger.info(f"\n{'='*60}")
        logger.info(f"PIPELINE COMPLETE — {elapsed:.0f}s")
        logger.info(f"  Scraped: {len(all_items)} new items")
        logger.info(f"  Analyzed: {len(leads_for_deep)} leads")
        logger.info(f"  High priority: {len(high_priority)}")
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
                lead.is_duplicate_of_known = self._check_known_cases(lead)
                lead.matches_expertise = self._check_expertise(lead)
                done += 1
                logger.info(f"  [{done}/{len(pending)}] {lead.title[:60]}")
            except Exception as e:
                logger.warning(f"  reanalyze error for lead {lead.id}: {e}")

        self.db.commit()
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
                    if details.dismissal_type in ("voluntary", "without_prejudice"):
                        note += "\n⚡ VOLUNTARY DISMISSAL — case alive for IL filing"
                    elif details.dismissal_type == "settled":
                        note += "\n⚠ SETTLEMENT — check if IL consumers included/excluded"
                    lead.notes = (lead.notes + "\n\n" if lead.notes else "") + note
                    enriched += 1
                    logger.info(f"PACER enriched: {lead.company} → {details.case_number}")
                except Exception as e:
                    logger.warning(f"PACER enrichment error ({lead.company}): {e}")
                time.sleep(2)

            pacer.close()
            self.db.commit()
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

            # If company matches, check if topic also overlaps
            if known_topic:
                topic_words = [w for w in known_topic.split() if len(w) > 3]
                topic_match = any(w in lead_text for w in topic_words)
                if topic_match:
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
