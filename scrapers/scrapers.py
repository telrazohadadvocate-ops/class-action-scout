"""
Scrapers for Class Action Scout
================================
Each scraper inherits from BaseScraper and implements scrape().
"""
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}


@dataclass
class ScrapedItem:
    title: str
    url: str
    source_name: str
    content: str = ""
    date: Optional[datetime] = None
    company: str = ""
    category: str = ""
    metadata: dict = field(default_factory=dict)


class BaseScraper(ABC):
    def __init__(self, source_name: str, base_url: str, delay: float = 3.0):
        self.source_name = source_name
        self.base_url = base_url
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _get(self, url: str) -> Optional[BeautifulSoup]:
        try:
            time.sleep(self.delay)
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            logger.error(f"[{self.source_name}] Failed to fetch {url}: {e}")
            return None

    @abstractmethod
    def scrape(self, max_items: int = 20) -> list[ScrapedItem]:
        ...


class ClassActionOrgScraper(BaseScraper):
    """Scrapes classaction.org/news for recent filings"""

    def __init__(self, delay: float = 3.0):
        super().__init__("classaction_org", "https://www.classaction.org/news", delay)

    def scrape(self, max_items: int = 20) -> list[ScrapedItem]:
        items = []
        soup = self._get(self.base_url)
        if not soup:
            return items

        # CSS selectors — UPDATE if site structure changes
        articles = soup.select("article, .node--type-article, .views-row")[:max_items]
        if not articles:
            # Fallback: try generic link patterns
            articles = soup.select("a[href*='/news/']")[:max_items]

        for art in articles:
            try:
                link_tag = art if art.name == "a" else art.select_one("a[href]")
                if not link_tag or not link_tag.get("href"):
                    continue

                href = link_tag["href"]
                if not href.startswith("http"):
                    href = f"https://www.classaction.org{href}"

                title = (
                    art.select_one("h2, h3, .field--name-title")
                    or link_tag
                )
                title_text = title.get_text(strip=True)
                if not title_text:
                    continue

                summary_el = art.select_one("p, .field--name-body, .summary")
                summary = summary_el.get_text(strip=True) if summary_el else ""

                items.append(ScrapedItem(
                    title=title_text,
                    url=href,
                    source_name=self.source_name,
                    content=summary,
                ))
            except Exception as e:
                logger.warning(f"[classaction_org] parse error: {e}")
                continue

        logger.info(f"[classaction_org] scraped {len(items)} items")
        return items


class TopClassActionsScraper(BaseScraper):
    """Scrapes topclassactions.com for open settlements/lawsuits"""

    def __init__(self, delay: float = 3.0):
        super().__init__(
            "topclassactions",
            "https://topclassactions.com/category/lawsuit-settlements/open-lawsuit-settlements/",
            delay,
        )

    def scrape(self, max_items: int = 20) -> list[ScrapedItem]:
        items = []
        soup = self._get(self.base_url)
        if not soup:
            return items

        articles = soup.select("article, .post, .entry")[:max_items]
        for art in articles:
            try:
                link = art.select_one("a[href]")
                if not link:
                    continue
                title = art.select_one("h2, h3, .entry-title")
                summary = art.select_one("p, .entry-summary, .excerpt")
                items.append(ScrapedItem(
                    title=title.get_text(strip=True) if title else link.get_text(strip=True),
                    url=link["href"],
                    source_name=self.source_name,
                    content=summary.get_text(strip=True) if summary else "",
                ))
            except Exception as e:
                logger.warning(f"[topclassactions] parse error: {e}")
        logger.info(f"[topclassactions] scraped {len(items)} items")
        return items


class IsraeliNewsScraper(BaseScraper):
    """Generic scraper for Israeli business news sites (TheMarker, Globes, Calcalist)"""

    def __init__(self, source_name: str, base_url: str, delay: float = 3.0):
        super().__init__(source_name, base_url, delay)

    def scrape(self, max_items: int = 15) -> list[ScrapedItem]:
        items = []
        soup = self._get(self.base_url)
        if not soup:
            return items

        # Generic selectors that work across Israeli news sites
        articles = soup.select("article, .teaser, .slotView, .feedItem, .item")[:max_items]
        if not articles:
            articles = soup.select("a[href*='article'], a[href*='כתבה']")[:max_items]

        for art in articles:
            try:
                link = art if art.name == "a" else art.select_one("a[href]")
                if not link or not link.get("href"):
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    # Build absolute URL
                    from urllib.parse import urljoin
                    href = urljoin(self.base_url, href)

                title_el = art.select_one("h2, h3, h4, .title, .headline")
                title_text = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
                if not title_text or len(title_text) < 5:
                    continue

                items.append(ScrapedItem(
                    title=title_text,
                    url=href,
                    source_name=self.source_name,
                    content="",  # full content fetched on demand
                ))
            except Exception as e:
                logger.warning(f"[{self.source_name}] parse error: {e}")

        logger.info(f"[{self.source_name}] scraped {len(items)} items")
        return items


class Law360Scraper(BaseScraper):
    """
    Scrapes Law360 class action / consumer protection headlines.
    NOTE: Full articles require subscription. Scrapes free headlines + summaries.
    """

    def __init__(self, source_name: str, base_url: str, delay: float = 3.0):
        super().__init__(source_name, base_url, delay)

    def scrape(self, max_items: int = 20) -> list[ScrapedItem]:
        items = []
        soup = self._get(self.base_url)
        if not soup:
            return items

        articles = soup.select(
            "article, .hnews, .news-item, div[data-article-id], "
            ".sc-dTSzeu, li.article"
        )[:max_items]
        if not articles:
            articles = soup.select("a[href*='/articles/']")[:max_items]

        for art in articles:
            try:
                link = art if art.name == "a" else art.select_one("a[href*='/articles/']")
                if not link or not link.get("href"):
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    href = f"https://www.law360.com{href}"
                title = art.select_one("h3, h2, .hed, .headline") or link
                title_text = title.get_text(strip=True)
                if not title_text or len(title_text) < 10:
                    continue
                summary_el = art.select_one("p, .dek, .summary, .blurb")
                summary = summary_el.get_text(strip=True) if summary_el else ""
                items.append(ScrapedItem(
                    title=title_text, url=href,
                    source_name=self.source_name, content=summary,
                ))
            except Exception as e:
                logger.warning(f"[{self.source_name}] parse error: {e}")

        logger.info(f"[{self.source_name}] scraped {len(items)} items")
        return items


class FTCScraper(BaseScraper):
    """Scrapes FTC press releases for enforcement actions"""

    def __init__(self, delay: float = 3.0):
        super().__init__(
            "ftc_enforcement",
            "https://www.ftc.gov/news-events/news/press-releases",
            delay,
        )

    def scrape(self, max_items: int = 15) -> list[ScrapedItem]:
        items = []
        soup = self._get(self.base_url)
        if not soup:
            return items

        articles = soup.select(
            ".views-row, article, .node--type-press-release, li.news-item"
        )[:max_items]

        for art in articles:
            try:
                link = art.select_one("a[href]")
                if not link:
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    href = f"https://www.ftc.gov{href}"
                title = art.select_one("h2, h3, .field--name-title") or link
                summary_el = art.select_one("p, .field--name-body")
                items.append(ScrapedItem(
                    title=title.get_text(strip=True),
                    url=href,
                    source_name=self.source_name,
                    content=summary_el.get_text(strip=True) if summary_el else "",
                ))
            except Exception as e:
                logger.warning(f"[ftc] parse error: {e}")

        logger.info(f"[ftc_enforcement] scraped {len(items)} items")
        return items


class CFPBScraper(BaseScraper):
    """Scrapes CFPB enforcement actions"""

    def __init__(self, delay: float = 3.0):
        super().__init__(
            "cfpb_enforcement",
            "https://www.consumerfinance.gov/enforcement/actions/",
            delay,
        )

    def scrape(self, max_items: int = 15) -> list[ScrapedItem]:
        items = []
        soup = self._get(self.base_url)
        if not soup:
            return items

        articles = soup.select(
            ".o-post-preview, article, .m-list_item, li.o-post-preview"
        )[:max_items]

        for art in articles:
            try:
                link = art.select_one("a[href]")
                if not link:
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    href = f"https://www.consumerfinance.gov{href}"
                title = art.select_one("h3, h2, .o-post-preview_title") or link
                items.append(ScrapedItem(
                    title=title.get_text(strip=True),
                    url=href,
                    source_name=self.source_name,
                    content="",
                ))
            except Exception as e:
                logger.warning(f"[cfpb] parse error: {e}")

        logger.info(f"[cfpb_enforcement] scraped {len(items)} items")
        return items


class UKCATScraper(BaseScraper):
    """Scrapes UK Competition Appeal Tribunal — collective proceedings / class actions"""

    def __init__(self, delay: float = 3.0):
        super().__init__(
            "uk_cat",
            "https://www.catribunal.org.uk/cases",
            delay,
        )

    def scrape(self, max_items: int = 15) -> list[ScrapedItem]:
        items = []
        soup = self._get(self.base_url)
        if not soup:
            return items

        rows = soup.select(
            "tr, .views-row, .case-row, article, .node--type-case"
        )[:max_items]

        for row in rows:
            try:
                link = row.select_one("a[href]")
                if not link:
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    href = f"https://www.catribunal.org.uk{href}"
                title_text = link.get_text(strip=True)
                if not title_text or len(title_text) < 5:
                    continue
                items.append(ScrapedItem(
                    title=title_text, url=href,
                    source_name=self.source_name, content="",
                ))
            except Exception as e:
                logger.warning(f"[uk_cat] parse error: {e}")

        logger.info(f"[uk_cat] scraped {len(items)} items")
        return items


class FXPForumScraper(BaseScraper):
    """
    Scrapes FXP consumer complaints forum.
    NOTE: FXP uses JS rendering; this basic version gets what it can.
    For full support, use Playwright (see notes).
    """

    def __init__(self, delay: float = 3.0):
        super().__init__(
            "fxp_consumer",
            "https://www.fxp.co.il/forumdisplay.php?f=5765",
            delay,
        )

    def scrape(self, max_items: int = 20) -> list[ScrapedItem]:
        items = []
        soup = self._get(self.base_url)
        if not soup:
            logger.warning("[fxp] Could not fetch — may need Playwright for JS rendering")
            return items

        threads = soup.select(
            "li.threadbit, .thread, tr[id^='thread_'], .threadTitle, "
            "a[href*='showthread']"
        )[:max_items]

        for thread in threads:
            try:
                link = thread if thread.name == "a" else thread.select_one("a[href*='showthread']")
                if not link or not link.get("href"):
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    href = f"https://www.fxp.co.il/{href}"
                title_text = link.get_text(strip=True)
                if not title_text:
                    continue
                items.append(ScrapedItem(
                    title=title_text, url=href,
                    source_name=self.source_name, content="",
                ))
            except Exception as e:
                logger.warning(f"[fxp] parse error: {e}")

        logger.info(f"[fxp_consumer] scraped {len(items)} items")
        return items


class TrustpilotILScraper(BaseScraper):
    """Scrapes Trustpilot Israel for low-rated companies with complaint patterns"""

    def __init__(self, delay: float = 3.0):
        super().__init__(
            "trustpilot_il",
            "https://il.trustpilot.com/categories",
            delay,
        )

    def scrape(self, max_items: int = 15) -> list[ScrapedItem]:
        items = []
        # Trustpilot has API restrictions; scrape category pages
        soup = self._get(self.base_url)
        if not soup:
            return items

        links = soup.select("a[href*='/review/'], a[href*='/categories/']")[:max_items]
        for link in links:
            try:
                href = link["href"]
                if not href.startswith("http"):
                    href = f"https://il.trustpilot.com{href}"
                title_text = link.get_text(strip=True)
                if title_text and len(title_text) > 3:
                    items.append(ScrapedItem(
                        title=title_text, url=href,
                        source_name=self.source_name, content="",
                    ))
            except Exception:
                continue

        logger.info(f"[trustpilot_il] scraped {len(items)} items")
        return items


class GovernmentSiteScraper(BaseScraper):
    """
    Generic scraper for Israeli government/regulatory websites.
    Covers: רשות הגנת הצרכן, רשות התחרות, רשות שוק ההון, משרד התקשורת
    """

    def __init__(self, source_name: str, base_url: str, delay: float = 3.0):
        super().__init__(source_name, base_url, delay)

    def scrape(self, max_items: int = 15) -> list[ScrapedItem]:
        items = []
        soup = self._get(self.base_url)
        if not soup:
            return items

        # gov.il sites use common patterns
        articles = soup.select(
            ".gov-page-items .item, .gov-updates-item, "
            "article, .views-row, .news-item, .content-item, "
            "li.list-item, div.card, .govil-content-item"
        )[:max_items]
        if not articles:
            articles = soup.select("a[href*='govil'], a[href*='news']")[:max_items]

        for art in articles:
            try:
                link = art if art.name == "a" else art.select_one("a[href]")
                if not link or not link.get("href"):
                    continue
                href = link["href"]
                if not href.startswith("http"):
                    from urllib.parse import urljoin
                    href = urljoin(self.base_url, href)
                title_el = art.select_one("h2, h3, h4, .title, .headline, span")
                title_text = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
                if not title_text or len(title_text) < 5:
                    continue
                items.append(ScrapedItem(
                    title=title_text, url=href,
                    source_name=self.source_name, content="",
                ))
            except Exception as e:
                logger.warning(f"[{self.source_name}] parse error: {e}")

        logger.info(f"[{self.source_name}] scraped {len(items)} items")
        return items


class GoogleAlertsRSSScraper(BaseScraper):
    """
    Parses Google Alerts RSS feeds.
    Setup: go to google.com/alerts → create alert → delivery = RSS → paste URL in settings.
    """

    def __init__(self, source_name: str, feed_url: str, delay: float = 1.0):
        super().__init__(source_name, feed_url, delay)

    def scrape(self, max_items: int = 20) -> list[ScrapedItem]:
        items = []
        try:
            import xml.etree.ElementTree as ET
            time.sleep(self.delay)
            resp = self.session.get(self.base_url, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)

            # RSS 2.0 format
            for entry in root.findall(".//item")[:max_items]:
                title = entry.findtext("title", "")
                link = entry.findtext("link", "")
                desc = entry.findtext("description", "")
                if title and link:
                    # Strip HTML from description
                    clean_desc = BeautifulSoup(desc, "html.parser").get_text(strip=True)
                    items.append(ScrapedItem(
                        title=title, url=link,
                        source_name=self.source_name, content=clean_desc,
                    ))

            # Atom format (Google Alerts sometimes uses this)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall(".//atom:entry", ns)[:max_items]:
                title = entry.findtext("atom:title", "", ns)
                link_el = entry.find("atom:link", ns)
                link = link_el.get("href", "") if link_el is not None else ""
                content = entry.findtext("atom:content", "", ns)
                if title and link:
                    clean = BeautifulSoup(content, "html.parser").get_text(strip=True)
                    items.append(ScrapedItem(
                        title=title, url=link,
                        source_name=self.source_name, content=clean,
                    ))

        except Exception as e:
            logger.error(f"[{self.source_name}] RSS parse error: {e}")

        logger.info(f"[{self.source_name}] scraped {len(items)} items from RSS")
        return items


# ── Factory ────────────────────────────────────────────

def build_scrapers(sources: dict, delay: float) -> dict[str, BaseScraper]:
    """Build scraper instances from config"""
    scrapers = {}
    for name, cfg in sources.items():
        if not cfg.get("enabled"):
            continue

        url = cfg["url"]
        src_type = cfg.get("type", "unknown")

        if name == "outlook_law360":
            from scrapers.outlook_law360 import OutlookLaw360Scraper
            scrapers[name] = OutlookLaw360Scraper()
        elif name == "classaction_org":
            scrapers[name] = ClassActionOrgScraper(delay=delay)
        elif name == "topclassactions":
            scrapers[name] = TopClassActionsScraper(delay=delay)
        elif name.startswith("law360"):
            scrapers[name] = Law360Scraper(name, url, delay=delay)
        elif name == "ftc_enforcement":
            scrapers[name] = FTCScraper(delay=delay)
        elif name == "cfpb_enforcement":
            scrapers[name] = CFPBScraper(delay=delay)
        elif name == "uk_cat":
            scrapers[name] = UKCATScraper(delay=delay)
        elif name == "fxp_consumer":
            scrapers[name] = FXPForumScraper(delay=delay)
        elif name == "trustpilot_il":
            scrapers[name] = TrustpilotILScraper(delay=delay)
        elif src_type == "alerts" and url:
            scrapers[name] = GoogleAlertsRSSScraper(name, url, delay=delay)
        elif src_type in ("regulatory_il", "regulatory"):
            scrapers[name] = GovernmentSiteScraper(name, url, delay=delay)
        else:
            # Israeli news / generic
            scrapers[name] = IsraeliNewsScraper(name, url, delay=delay)

    return scrapers
