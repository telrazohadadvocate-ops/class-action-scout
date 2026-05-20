"""
PacerMonitor Integration — Playwright Version
================================================
Uses a real browser (Chromium) to connect to PacerMonitor.
First run: opens browser for manual login (with reCAPTCHA).
Subsequent runs: uses saved cookies.

Setup:
  pip install playwright
  playwright install chromium

Usage:
  # First time — manual login:
  python -c "from scrapers.pacer_monitor import PacerMonitorClient; p = PacerMonitorClient(); p.login_interactive()"

  # After that — automatic:
  python -c "from scrapers.pacer_monitor import PacerMonitorClient; p = PacerMonitorClient(); p.login(); results = p.search_cases('Amazon Fire TV'); print(results)"
"""
import json
import re
import time
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:
    from config.settings import PACER_COOKIES_PATH as COOKIES_PATH
except Exception:
    COOKIES_PATH = Path(__file__).parent.parent / "data" / "pacer_cookies.json"


def _pacer_id_from_url(url: str) -> str:
    """Extract the numeric PacerMonitor case ID from a URL like /case/61789239/..."""
    m = re.search(r"/case/(\d+)", url)
    return m.group(1) if m else ""


@dataclass
class PacerCase:
    case_number: str
    title: str
    court: str
    filed_date: str = ""
    status: str = ""
    cause: str = ""
    nature_of_suit: str = ""
    url: str = ""
    docket_entries: list = None
    complaint_text: str = ""
    last_activity: str = ""
    dismissal_type: str = ""

    def __post_init__(self):
        if self.docket_entries is None:
            self.docket_entries = []


class PacerMonitorClient:
    BASE_URL = "https://www.pacermonitor.com"

    def __init__(self):
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._logged_in = False

    def login_interactive(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("Playwright not installed. Run:")
            print("  pip install playwright")
            print("  playwright install chromium")
            return False

        print("Opening PacerMonitor login page...")
        print("Please log in manually (complete the reCAPTCHA).")
        print("After login, the cookies will be saved automatically.")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"{self.BASE_URL}/login")

            try:
                page.wait_for_url(
                    lambda url: "/login" not in url,
                    timeout=300000
                )
                print("Login detected!")
                cookies = context.cookies()
                COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
                COOKIES_PATH.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
                print(f"Cookies saved to {COOKIES_PATH}")
                print("From now on, the scout will use these cookies automatically.")
                self._logged_in = True
                return True
            except Exception as e:
                print(f"Login timeout or error: {e}")
                return False
            finally:
                browser.close()

    def login(self) -> bool:
        if not COOKIES_PATH.exists():
            logger.warning("No saved cookies. Run login_interactive() first.")
            return False

        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context()

            cookies = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
            self._context.add_cookies(cookies)
            self._page = self._context.new_page()

            self._page.goto(f"{self.BASE_URL}/user/account", timeout=15000)
            time.sleep(2)

            if "/login" in self._page.url:
                logger.warning("Cookies expired. Run login_interactive() again.")
                self._cleanup()
                return False

            self._logged_in = True
            logger.info("PacerMonitor login successful (cookies)")
            return True
        except Exception as e:
            logger.error(f"PacerMonitor login failed: {e}")
            self._cleanup()
            return False

    def _cleanup(self):
        try:
            if self._page: self._page.close()
            if self._context: self._context.close()
            if self._browser: self._browser.close()
            if self._pw: self._pw.stop()
        except Exception:
            pass
        self._page = self._context = self._browser = self._pw = None
        self._logged_in = False

    def close(self):
        self._cleanup()

    def search_cases(self, query: str, max_results: int = 10) -> list[PacerCase]:
        if not self._logged_in and not self.login():
            return []

        cases = []
        try:
            self._page.goto(
                f"{self.BASE_URL}/public/case?q={query}&sort=date_filed&order=desc",
                timeout=20000
            )
            time.sleep(3)

            links = self._page.query_selector_all("a[href*='/public/case/']")
            for link in links[:max_results]:
                try:
                    href = link.get_attribute("href") or ""
                    title = link.inner_text().strip()
                    if title and len(title) > 5:
                        cases.append(PacerCase(
                            case_number="",
                            title=title,
                            court="",
                            url=href if href.startswith("http") else f"{self.BASE_URL}{href}",
                        ))
                except Exception:
                    continue

            logger.info(f"PacerMonitor search '{query}': {len(cases)} cases")
        except Exception as e:
            logger.error(f"PacerMonitor search failed: {e}")

        return cases

    def get_case_details(self, case_url: str) -> Optional[PacerCase]:
        if not self._logged_in and not self.login():
            return None

        try:
            if not case_url.startswith("http"):
                case_url = f"{self.BASE_URL}{case_url}"

            self._page.goto(case_url, timeout=20000)
            time.sleep(3)

            # ── Debug: log page state so selectors can be refined ──────────
            logger.debug("PACER detail page title : %r", self._page.title())
            logger.debug("PACER detail page url   : %r", self._page.url)
            logger.debug("PACER detail page html (first 500): %s",
                         self._page.content()[:500])
            for probe in ("h1", "h2", "h3",
                          ".case-number", ".docket-number", ".case-title",
                          ".case-name", ".case-header", ".case-info",
                          "table tr", "tbody tr", ".docket-entry", ".docket-row"):
                n = len(self._page.query_selector_all(probe))
                if n:
                    logger.debug("  selector %-40r → %d elements", probe, n)

            # ── Extract case_number ─────────────────────────────────────────
            # Try HTML selectors first; fall back to the numeric PacerMonitor
            # case ID embedded in the URL path (/case/61789239/...).
            case_number = self._get_text(
                ".case-no, .case-number, .docket-number, "
                "[class*='case-no'], [class*='docket-no'], "
                ".case-header__number, .caseNumber"
            ) or _pacer_id_from_url(case_url)

            title = (
                self._get_text("h1")
                or self._get_text(".case-name, .case-title, .case-header__title, h2")
                or self._page.title()
            )

            court = self._get_text(
                ".court, .court-name, .jurisdiction, "
                "[class*='court'], [class*='district'], .case-header__court"
            )

            case = PacerCase(
                case_number=case_number,
                title=title,
                court=court,
                url=case_url,
            )

            case.docket_entries = self._extract_docket_entries()
            case.dismissal_type = self._detect_dismissal(case)

            if case.docket_entries:
                case.last_activity = case.docket_entries[0].get("description", "")

            logger.debug(
                "PACER extracted: case_number=%r  docket_entries=%d  dismissal=%r",
                case.case_number, len(case.docket_entries), case.dismissal_type,
            )
            return case
        except Exception as e:
            logger.error("Failed to get case details from %s: %s", case_url, e)
            return None

    def check_case_status(self, case_url: str) -> dict:
        case = self.get_case_details(case_url)
        if not case:
            return {"error": "Could not fetch case"}

        return {
            "case_number": case.case_number,
            "title": case.title,
            "status": case.status,
            "dismissal_type": case.dismissal_type,
            "last_activity": case.last_activity,
            "is_active": case.dismissal_type == "none" and "closed" not in (case.status or "").lower(),
            "docket_count": len(case.docket_entries),
        }

    def enrich_lead(self, company: str, title: str) -> dict:
        result = {
            "pacer_found": False, "cases": [],
            "has_dismissal": False, "dismissal_type": None,
            "has_settlement": False,
        }

        cases = self.search_cases(f"{company} class action")
        if not cases:
            return result

        result["pacer_found"] = True

        for case in cases[:3]:
            if case.url:
                details = self.get_case_details(case.url)
                if details:
                    result["cases"].append({
                        "case_number": details.case_number,
                        "title": details.title,
                        "court": details.court,
                        "dismissal_type": details.dismissal_type,
                        "docket_count": len(details.docket_entries),
                    })
                    if details.dismissal_type in ("voluntary", "without_prejudice"):
                        result["has_dismissal"] = True
                        result["dismissal_type"] = details.dismissal_type
                    elif details.dismissal_type == "settled":
                        result["has_settlement"] = True
            time.sleep(2)

        return result

    def _extract_docket_entries(self) -> list[dict]:
        # Try progressively broader selectors until we find rows
        selector_attempts = [
            ".docket-entry",
            ".docket-row",
            "tr.entry",
            "table.docket tbody tr",
            "#docket-table tbody tr",
            "tbody tr",
            "tr",
        ]
        rows = []
        for sel in selector_attempts:
            rows = self._page.query_selector_all(sel)
            if rows:
                logger.debug("_extract_docket_entries: selector %r → %d rows", sel, len(rows))
                break

        entries = []
        for row in rows[:50]:
            try:
                entry = {}
                desc_el = row.query_selector(
                    ".description, .entry-text, .docket-desc, "
                    "td:nth-child(3), td:nth-child(2)"
                )
                if desc_el:
                    entry["description"] = desc_el.inner_text().strip()
                date_el = row.query_selector(
                    ".date, time, [class*='date'], "
                    "td:first-child, td:nth-child(1)"
                )
                if date_el:
                    entry["date"] = date_el.inner_text().strip()
                link = row.query_selector("a[href*='doc'], a[href*='pdf'], a[href]")
                if link:
                    entry["url"] = link.get_attribute("href") or ""
                if entry.get("description"):
                    entries.append(entry)
            except Exception:
                continue
        return entries

    def _detect_dismissal(self, case: PacerCase) -> str:
        dismissal_keywords = {
            "voluntary": ["voluntary dismissal", "voluntarily dismissed", "notice of voluntary"],
            "without_prejudice": ["without prejudice", "dismissed without prejudice"],
            "with_prejudice": ["with prejudice", "dismissed with prejudice", "judgment on the merits"],
            "settled": ["settlement", "consent decree", "final approval", "settlement approved", "preliminary approval"],
        }

        all_text = " ".join(e.get("description", "").lower() for e in case.docket_entries)
        all_text += " " + (case.status or "").lower()

        for dtype, keywords in dismissal_keywords.items():
            if any(kw in all_text for kw in keywords):
                return dtype
        return "none"

    def _get_text(self, selector: str) -> str:
        el = self._page.query_selector(selector)
        return el.inner_text().strip() if el else ""

    def __del__(self):
        self._cleanup()
