"""
Outlook / Law360 newsletter scraper
====================================
Uses MSAL (Microsoft Authentication Library) to fetch emails from
news-alt@law360.com via Microsoft Graph API, then parses the HTML
newsletter body to extract article headlines, URLs, and summaries.

Authentication:
  - First run: device-code flow (run scripts/setup_outlook.py)
  - Subsequent runs: silent refresh from data/outlook_token.json

Required env vars:
  OUTLOOK_CLIENT_ID   — Azure app registration client ID
  OUTLOOK_TENANT_ID   — tenant ID or "common" for personal Microsoft accounts
  OUTLOOK_USER_EMAIL  — mailbox to search (defaults to "me")
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from scrapers.scrapers import BaseScraper, ScrapedItem

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read", "User.Read"]
LAW360_SENDER = "news-alt@law360.com"
try:
    from config.settings import OUTLOOK_TOKEN_PATH as TOKEN_PATH
except Exception:
    TOKEN_PATH = Path(__file__).resolve().parent.parent / "data" / "outlook_token.json"


class OutlookTokenManager:
    """
    Wraps MSAL PublicClientApplication with a file-backed SerializableTokenCache.
    Supports silent refresh; falls back to device-code flow when the cache is cold.
    """

    def __init__(self, client_id: str, tenant_id: str, token_path: Path = TOKEN_PATH):
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.token_path = token_path

    def _load_cache(self):
        try:
            import msal
        except ImportError:
            raise RuntimeError("msal is not installed. Run: pip install 'msal>=1.28'")

        cache = msal.SerializableTokenCache()
        if self.token_path.exists():
            cache.deserialize(self.token_path.read_text(encoding="utf-8"))
        return cache

    def _build_app(self, cache):
        import msal
        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        return msal.PublicClientApplication(
            self.client_id,
            authority=authority,
            token_cache=cache,
        )

    def _persist(self, cache) -> None:
        if cache.has_state_changed:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(cache.serialize(), encoding="utf-8")

    def acquire_token_silent(self) -> Optional[str]:
        """Return a valid token using the cached refresh token. Returns None on miss."""
        cache = self._load_cache()
        app = self._build_app(cache)
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            self._persist(cache)
            return result["access_token"]
        return None

    def acquire_token_device_flow(self) -> str:
        """
        Interactive device-code flow. Prints the one-time-code URL to stdout
        and blocks until the user completes auth in a browser.
        """
        cache = self._load_cache()
        app = self._build_app(cache)
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow initiation failed: {flow}")

        print("\n" + "═" * 60)
        print(flow["message"])
        print("═" * 60 + "\n")

        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(
                "Token acquisition failed: "
                + result.get("error_description", str(result))
            )
        self._persist(cache)
        logger.info("[outlook_law360] Token saved to %s", self.token_path)
        return result["access_token"]

    def acquire_token(self) -> str:
        """Return a valid token, falling back to device-code if needed."""
        token = self.acquire_token_silent()
        if token:
            return token
        return self.acquire_token_device_flow()


class OutlookLaw360Scraper(BaseScraper):
    """
    Scrapes Law360 newsletters delivered to an Outlook/Exchange mailbox.

    Searches for emails from news-alt@law360.com in the past `days_back` days,
    fetches each HTML body via Microsoft Graph, and extracts article headlines,
    law360.com article URLs, and summary text.

    Gracefully returns [] when credentials are absent or the token file has not
    yet been created — the run continues without crashing.
    """

    def __init__(
        self,
        client_id: str = "",
        tenant_id: str = "",
        user_email: str = "",
        days_back: int = 7,
        delay: float = 0.5,
    ):
        super().__init__("outlook_law360", GRAPH_BASE, delay=delay)
        self.client_id = client_id or os.getenv("OUTLOOK_CLIENT_ID", "")
        self.tenant_id = tenant_id or os.getenv("OUTLOOK_TENANT_ID", "common")
        self.user_email = user_email or os.getenv("OUTLOOK_USER_EMAIL", "me")
        self.days_back = days_back

    # ── Graph helpers ─────────────────────────────────────

    def _graph(self, token: str, path: str, params: dict = None) -> Optional[dict]:
        from urllib.parse import quote
        url = f"{GRAPH_BASE}{path}"
        if params:
            # requests.get(params=...) uses quote_plus, which encodes spaces as '+'
            # and '$' as '%24'. Build the query string manually so that:
            #   - OData $-prefixed key names are preserved verbatim
            #   - spaces in filter expressions are encoded as %20 (not +)
            qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
            url = f"{url}?{qs}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            resp = self.session.get(url, headers=headers, timeout=30)
            if not resp.ok:
                logger.error(
                    "[outlook_law360] GET %s → %s: %s",
                    path, resp.status_code, resp.text[:400],
                )
                return None
            return resp.json()
        except Exception as e:
            logger.error("[outlook_law360] GET %s failed: %s", path, e)
            return None

    def _search_emails(self, token: str) -> list[dict]:
        """Return message stubs (id + subject + receivedDateTime) for Law360 emails."""
        # Use midnight of the cutoff day so the value is a clean ISO 8601 datetime
        # with Z suffix — required by Graph OData for DateTimeOffset comparisons.
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.days_back)
        since = cutoff.strftime("%Y-%m-%d") + "T00:00:00Z"

        # Do not combine $filter with $search (Graph rejects both together).
        # Do not add $orderby on a different property than the $filter predicate —
        # Exchange Online returns 400 when $orderby and $filter target different
        # non-indexed properties in the same request.
        odata_filter = (
            f"from/emailAddress/address eq '{LAW360_SENDER}'"
            f" and receivedDateTime ge {since}"
        )
        data = self._graph(
            token,
            "/me/messages",
            params={
                "$filter": odata_filter,
                "$select": "id,subject,receivedDateTime",
                "$top": "50",
            },
        )
        return data.get("value", []) if data else []

    def _fetch_body(self, token: str, msg_id: str) -> tuple[str, str, str]:
        """Return (html_body, subject, receivedDateTime) for one message."""
        data = self._graph(
            token,
            f"/me/messages/{msg_id}",
            params={"$select": "body,subject,receivedDateTime"},
        )
        if not data:
            return "", "", ""
        return (
            data.get("body", {}).get("content", ""),
            data.get("subject", "Law360 Newsletter"),
            data.get("receivedDateTime", ""),
        )

    # ── Newsletter parser ─────────────────────────────────

    def _parse_newsletter(
        self, html: str, subject: str, received: str
    ) -> list[ScrapedItem]:
        """
        Parse a single Law360 newsletter HTML body.

        Law360 newsletters are table-layout HTML emails. Each article block
        contains an <a href="https://www.law360.com/articles/NNNNNN/..."> for
        the headline; summary text sits in the same or next table cell.
        """
        if not html:
            return []

        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as e:
            logger.warning("[outlook_law360] HTML parse error: %s", e)
            return []

        received_dt = _parse_dt(received)
        items: list[ScrapedItem] = []
        seen: set[str] = set()

        article_links = [
            a for a in soup.find_all("a", href=True)
            if re.search(r"law360\.com/articles/\d+", a["href"], re.I)
        ]

        for link in article_links:
            # Strip query-string tracking params to get the canonical URL
            raw_href = link["href"].strip()
            clean_url = re.sub(r"\?.*$", "", raw_href)
            if clean_url in seen:
                continue
            seen.add(clean_url)

            title = link.get_text(separator=" ", strip=True)
            if not title or len(title) < 10:
                continue

            summary = _extract_summary(link)

            items.append(ScrapedItem(
                title=title,
                url=clean_url,
                source_name="outlook_law360",
                content=summary,
                date=received_dt,
                category="newsletter",
                metadata={"email_subject": subject, "sender": LAW360_SENDER},
            ))

        logger.debug(
            "[outlook_law360] '%s' → %d articles", subject, len(items)
        )
        return items

    # ── Public scrape() ───────────────────────────────────

    def scrape(self, max_items: int = 60) -> list[ScrapedItem]:
        if not self.client_id:
            logger.warning(
                "[outlook_law360] OUTLOOK_CLIENT_ID not configured. "
                "Run scripts/setup_outlook.py first."
            )
            return []

        # Require a pre-existing token; device flow is only in setup_outlook.py
        tm = OutlookTokenManager(self.client_id, self.tenant_id)
        token = tm.acquire_token_silent()
        if not token:
            logger.warning(
                "[outlook_law360] No cached token found. "
                "Run scripts/setup_outlook.py to authenticate."
            )
            return []

        stubs = self._search_emails(token)
        if not stubs:
            logger.info(
                "[outlook_law360] No Law360 emails in the last %d days.", self.days_back
            )
            return []

        all_items: list[ScrapedItem] = []
        for stub in stubs:
            if len(all_items) >= max_items:
                break
            html, subject, received = self._fetch_body(token, stub["id"])
            all_items.extend(self._parse_newsletter(html, subject, received))

        all_items = all_items[:max_items]
        logger.info("[outlook_law360] Scraped %d articles total.", len(all_items))
        return all_items


# ── Utility functions ─────────────────────────────────────

def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_summary(link_tag) -> str:
    """
    Heuristic: find the descriptive blurb that follows an article link.

    Law360 newsletters usually put the summary in:
    1. The same <td> as the link (minus the link text itself)
    2. The next sibling element
    3. The next <tr> in the same table
    """
    link_text = link_tag.get_text(strip=True)

    # 1. Same <td>
    td = link_tag.find_parent("td")
    if td:
        full = td.get_text(separator=" ", strip=True)
        candidate = full.replace(link_text, "", 1).strip(" ·|—-–")
        if len(candidate) > 20:
            return _clean(candidate[:500])

    # 2. Next sibling element
    sib = link_tag.find_next_sibling()
    if sib:
        text = sib.get_text(strip=True)
        if len(text) > 20:
            return _clean(text[:500])

    # 3. Next <tr>
    tr = link_tag.find_parent("tr")
    if tr:
        nxt = tr.find_next_sibling("tr")
        if nxt:
            text = nxt.get_text(strip=True)
            if len(text) > 20:
                return _clean(text[:500])

    return ""


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    # Strip trailing "View More" / "Read More" newsletter chrome
    text = re.sub(r"(?i)\s*(view more|read more|click here|subscribe|unsubscribe).*$", "", text).strip()
    return text
