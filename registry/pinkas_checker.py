"""
פנקס התובענות הייצוגיות — Registry Checker
============================================
Checks whether a similar class action has already been filed in Israel.
Uses the court.gov.il registry (or web search fallback).
"""
import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class PinkasChecker:
    """
    Check if a class action has been filed against a given company/topic.
    
    Primary: court.gov.il פנקס התובענות הייצוגיות
    Fallback: web search via Google
    """

    PINKAS_URL = "https://www.court.gov.il/NGCS.Web.Site/ClassActions/Search"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0"
        })

    def check(self, company_name: str, keywords: list[str] = None) -> dict:
        """
        Search the registry for existing class actions.
        Returns:
          {found: bool, results: [...], searched_at: datetime}
        """
        result = {
            "found": False,
            "results": [],
            "searched_at": datetime.now(timezone.utc).isoformat(),
            "method": None,
        }

        # Try primary (court.gov.il) — this often requires Playwright
        # because the site uses ASP.NET postbacks
        try:
            primary = self._search_pinkas_direct(company_name)
            if primary is not None:
                result.update(primary)
                result["method"] = "pinkas_direct"
                return result
        except Exception as e:
            logger.warning(f"Pinkas direct search failed: {e}")

        # Fallback: Google search
        try:
            fallback = self._search_google_fallback(company_name, keywords or [])
            result.update(fallback)
            result["method"] = "google_fallback"
        except Exception as e:
            logger.error(f"Google fallback also failed: {e}")
            result["error"] = str(e)

        return result

    def _search_pinkas_direct(self, company: str) -> dict | None:
        """
        Direct search on court.gov.il.
        NOTE: The site structure may change — update selectors as needed.
        Returns None if the site is unreachable or structure changed.
        """
        # The court.gov.il class action registry uses a search form.
        # This is a simplified version; for production, use Playwright.
        try:
            resp = self.session.get(
                self.PINKAS_URL,
                params={"SearchText": company},
                timeout=15,
            )
            if resp.status_code != 200:
                return None

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")

            # Look for result rows (selectors may need updating)
            rows = soup.select("tr.gridRow, tr.gridAltRow, .search-result-item")
            if not rows:
                return {"found": False, "results": []}

            results = []
            for row in rows[:10]:
                cells = row.select("td")
                if len(cells) >= 3:
                    results.append({
                        "case_number": cells[0].get_text(strip=True),
                        "title": cells[1].get_text(strip=True) if len(cells) > 1 else "",
                        "status": cells[2].get_text(strip=True) if len(cells) > 2 else "",
                    })

            return {"found": len(results) > 0, "results": results}

        except Exception as e:
            logger.warning(f"Pinkas direct error: {e}")
            return None

    def _search_google_fallback(self, company: str, keywords: list[str]) -> dict:
        """
        Google search for: תובענה ייצוגית + company name
        Parses results to check if a similar case exists.
        """
        query_parts = ["תובענה ייצוגית", company] + keywords[:3]
        query = " ".join(query_parts)

        try:
            resp = self.session.get(
                "https://www.google.com/search",
                params={"q": query, "hl": "he", "num": 10},
                timeout=15,
            )
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")

            results = []
            for item in soup.select("div.g, div[data-sokoban-container]")[:10]:
                link = item.select_one("a[href]")
                title = item.select_one("h3")
                if link and title:
                    results.append({
                        "title": title.get_text(strip=True),
                        "url": link["href"],
                    })

            # Check if results indicate an existing case
            indicators = ["אושרה", "הוגשה", "תובענה ייצוגית", "בקשת אישור", "פנקס"]
            found = any(
                any(ind in r.get("title", "") for ind in indicators)
                for r in results
            )

            return {"found": found, "results": results}

        except Exception as e:
            logger.warning(f"Google fallback error: {e}")
            return {"found": False, "results": [], "error": str(e)}
