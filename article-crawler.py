import csv
import json
import logging
import os
import re
import time
from dataclasses import dataclass, fields
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ARCHIVE_BASE = "https://tutorialsdojo.com/blog/"
SITE_DOMAIN = "tutorialsdojo.com"
OUTPUT_CSV = "tutorialsdojo_blog_metadata.csv"
FAILED_CSV = "failed_urls.csv"

USER_AGENT = (
    "Mozilla/5.0 (compatible; TDCrawler/1.0; +https://tutorialsdojo.com)"
)
REQUEST_DELAY = 1.5   # seconds between requests
REQUEST_TIMEOUT = 20  # seconds per request
MAX_RETRIES = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("crawler.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# ---------------------------------------------------------------------------
# Pre-compiled regex / CSS selector constants
# ---------------------------------------------------------------------------

# Grabs every JSON-LD block straight from raw HTML — no DOM traversal needed.
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Heading selectors used on both listing and article pages
_ENTRY_TITLE_SEL = (
    "h1[class*='entry-title'], h2[class*='entry-title'], "
    "h3[class*='entry-title'], h4[class*='entry-title']"
)

# Article body containers in priority order (single selector string)
_ARTICLE_BODY_SEL = (
    "div.entry-content, "
    "div.post-content, "
    "div.article-content, "
    "div[itemprop='articleBody'], "
    "article"
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ArticleMeta:
    url: str = ""
    title: str = ""
    first_published: str = ""
    last_updated: str = ""
    categories: str = ""      # pipe-separated
    keywords: str = ""        # pipe-separated
    internal_links: str = ""  # pipe-separated
    author: str = ""


CSV_FIELDNAMES = [f.name for f in fields(ArticleMeta)]


# ---------------------------------------------------------------------------
# JSON-LD — regex extraction (fast path, called once per article page)
# ---------------------------------------------------------------------------

def _extract_jsonld(raw_html: str) -> list[dict]:
    """
    Pull every JSON-LD block from raw HTML using a pre-compiled regex.
    Returns a flat list of dicts (graphs and arrays are unwrapped).
    Much faster than asking BS4 to search <script> tags after DOM parsing.
    """
    results: list[dict] = []
    for block in _JSONLD_RE.findall(raw_html):
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if isinstance(entry, dict):
                # Unwrap @graph if present
                graph = entry.get("@graph")
                if isinstance(graph, list):
                    results.extend(e for e in graph if isinstance(e, dict))
                else:
                    results.append(entry)
    return results


# ---------------------------------------------------------------------------
# fetch_page
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> tuple[Optional[BeautifulSoup], int, str, str]:
    """
    Fetch a URL and return (soup, status_code, final_url, raw_html).

    raw_html is the decoded response body (empty string on failure).
    final_url reflects any redirects that occurred.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            final_url = resp.url
            status = resp.status_code

            if status == 404:
                return None, 404, final_url, ""

            resp.raise_for_status()
            raw_html = resp.text
            # lxml is ~3–5× faster than the default html.parser
            soup = BeautifulSoup(raw_html, "lxml")
            return soup, status, final_url, raw_html

        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            log.warning("HTTP %d on attempt %d/%d: %s", code, attempt, MAX_RETRIES, url)
            if code == 404:
                return None, 404, url, ""
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY * attempt)

        except requests.RequestException as exc:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY * attempt)

    return None, 0, url, ""


# ---------------------------------------------------------------------------
# parse_listing_page
# ---------------------------------------------------------------------------

def parse_listing_page(soup: BeautifulSoup, page_url: str) -> list[str]:
    """
    Extract article URLs from a blog listing page.

    Primary target:
      <h4 class="entry-title fusion-responsive-typography-calculated">
        <a href="...">Title</a>
      </h4>

    CSS selectors replace lambda-based find_all calls throughout.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(href: str) -> None:
        absolute = urljoin(page_url, href)
        if absolute not in seen and SITE_DOMAIN in urlparse(absolute).netloc:
            seen.add(absolute)
            found.append(absolute)

    # Strategy 1: any heading (h1–h4) whose class contains "entry-title"
    for heading in soup.select(_ENTRY_TITLE_SEL):
        a = heading.select_one("a[href]")
        if a:
            _add(a["href"])

    if found:
        return found

    # Strategy 2: first heading link inside each <article>
    for article in soup.select("article"):
        a = article.select_one("h1 a[href], h2 a[href], h3 a[href], h4 a[href]")
        if a:
            _add(a["href"])

    if found:
        return found

    # Strategy 3: explicit entry-title-link anchors
    for a in soup.select("a.entry-title-link[href]"):
        _add(a["href"])

    return found


# ---------------------------------------------------------------------------
# discover_article_urls  (generator — yields one listing page at a time)
# ---------------------------------------------------------------------------

def discover_article_urls():
    """
    Walk every /blog/page/{n}/ listing page and yield article URLs page-by-page.
    Yields: (page_number, article_urls_on_this_page)

    Stops when:
      - The page returns 404 (past the last page).
      - The page redirects back to the base archive (WordPress out-of-range behaviour).
      - The page returns no article links.
    """
    page_num = 1

    while True:
        listing_url = ARCHIVE_BASE if page_num == 1 else f"{ARCHIVE_BASE}page/{page_num}/"

        log.info("Fetching listing page %d: %s", page_num, listing_url)
        soup, status, final_url, _ = fetch_page(listing_url)

        if status == 404:
            log.info("Listing page %d returned 404 — pagination end.", page_num)
            break

        if _is_redirect_to_base(listing_url, final_url):
            log.info("Listing page %d redirected to %s — pagination end.", page_num, final_url)
            break

        if soup is None:
            log.error("Could not fetch listing page %d after retries.", page_num)
            break

        article_urls = parse_listing_page(soup, listing_url)

        if not article_urls:
            log.info("Listing page %d has no article links — pagination end.", page_num)
            break

        log.info("Listing page %d: found %d article URL(s).", page_num, len(article_urls))
        yield page_num, article_urls

        page_num += 1
        time.sleep(REQUEST_DELAY)


def _is_redirect_to_base(requested: str, final: str) -> bool:
    if requested == final:
        return False
    norm = str.rstrip
    return norm(final, "/") == norm(ARCHIVE_BASE, "/")


# ---------------------------------------------------------------------------
# parse_article_page  (and per-field extractors)
# ---------------------------------------------------------------------------

def parse_article_page(
    soup: BeautifulSoup, article_url: str, raw_html: str
) -> ArticleMeta:
    """
    Extract all metadata fields from an article page.

    JSON-LD is parsed once from raw HTML (regex, no DOM traversal) and
    passed down to every sub-extractor that needs it.
    """
    jsonld = _extract_jsonld(raw_html)   # fast regex path — parsed once
    meta = ArticleMeta(url=article_url)
    meta.title = _extract_title(soup)
    meta.first_published, meta.last_updated = _extract_dates(soup, jsonld)
    meta.categories = _pipe(_extract_categories(soup, jsonld))
    meta.keywords = _pipe(_extract_keywords(soup, jsonld))
    meta.internal_links = _pipe(extract_internal_links(soup, article_url))
    meta.author = _extract_author(soup, jsonld)
    return meta


# --- title ------------------------------------------------------------------

def _extract_title(soup: BeautifulSoup) -> str:
    # 1. Any heading with class containing "entry-title" (CSS selector)
    tag = soup.select_one(_ENTRY_TITLE_SEL)
    if tag:
        return tag.get_text(strip=True)
    # 2. og:title meta
    og = soup.select_one("meta[property='og:title']")
    if og and og.get("content"):
        return og["content"].strip()
    # 3. <title> — strip "– Site Name" suffixes
    tag = soup.select_one("title")
    if tag:
        text = tag.get_text(strip=True)
        for sep in (" – ", " — ", " | ", " - "):
            if sep in text:
                return text.split(sep)[0].strip()
        return text
    return ""


# --- dates ------------------------------------------------------------------

def _extract_dates(
    soup: BeautifulSoup, jsonld: list[dict]
) -> tuple[str, str]:
    published = ""
    updated = ""

    # 1. Pre-parsed JSON-LD (regex path — no extra DOM work)
    for entry in jsonld:
        if not published:
            published = entry.get("datePublished", "")
        if not updated:
            updated = entry.get("dateModified", "")
        if published and updated:
            break

    # 2. <time> tags — CSS selectors instead of lambda find_all
    if not published:
        t = soup.select_one("time.published, time[class*='published']")
        if t:
            published = t.get("datetime") or t.get_text(strip=True)

    if not updated:
        t = soup.select_one("time.updated, time[class*='updated']")
        if t:
            updated = t.get("datetime") or t.get_text(strip=True)

    # 3. Open Graph / article meta
    if not published:
        m = soup.select_one("meta[property='article:published_time']")
        if m:
            published = m.get("content", "")
    if not updated:
        m = soup.select_one("meta[property='article:modified_time']")
        if m:
            updated = m.get("content", "")

    # 4. Any <time datetime="..."> inside .entry-meta
    if not published or not updated:
        for t in soup.select(".entry-meta time[datetime]"):
            dt = t["datetime"]
            cls = " ".join(t.get("class", []))
            if "updated" in cls and not updated:
                updated = dt
            elif not published:
                published = dt

    return published, updated or published


# --- categories -------------------------------------------------------------

def _extract_categories(
    soup: BeautifulSoup, jsonld: list[dict]
) -> list[str]:
    cats: list[str] = []

    # 1. <a rel="category tag"> — CSS attribute selector [rel~=] does word match
    for a in soup.select("a[rel~='category']"):
        text = a.get_text(strip=True)
        if text and text not in cats:
            cats.append(text)
    if cats:
        return cats

    # 2. Common WordPress category containers
    container = soup.select_one(
        "[class*='cat-links'], [class*='category-links'], [class*='post-categories']"
    )
    if container:
        for a in container.select("a"):
            text = a.get_text(strip=True)
            if text and text not in cats:
                cats.append(text)
    if cats:
        return cats

    # 3. JSON-LD articleSection (pre-parsed — no extra work)
    for entry in jsonld:
        section = entry.get("articleSection")
        if isinstance(section, list):
            cats.extend(s for s in section if s and s not in cats)
        elif isinstance(section, str) and section not in cats:
            cats.append(section)
        if cats:
            break

    return cats


# --- keywords / tags --------------------------------------------------------

def _extract_keywords(
    soup: BeautifulSoup, jsonld: list[dict]
) -> list[str]:
    tags: list[str] = []

    # 1. <a rel="tag"> — CSS word-match attribute selector
    for a in soup.select("a[rel~='tag']"):
        text = a.get_text(strip=True)
        if text and text not in tags:
            tags.append(text)
    if tags:
        return tags

    # 2. Common WordPress tag containers
    container = soup.select_one(
        "[class*='tags-links'], [class*='post-tags'], [class*='tag-links']"
    )
    if container:
        for a in container.select("a"):
            text = a.get_text(strip=True)
            if text and text not in tags:
                tags.append(text)
    if tags:
        return tags

    # 3. <meta name="keywords">
    m = soup.select_one("meta[name='keywords']")
    if m and m.get("content"):
        return [k.strip() for k in m["content"].split(",") if k.strip()]

    # 4. JSON-LD keywords (pre-parsed)
    for entry in jsonld:
        kw = entry.get("keywords")
        if isinstance(kw, list):
            return [k for k in kw if k]
        if isinstance(kw, str):
            return [k.strip() for k in kw.split(",") if k.strip()]

    return tags


# --- internal links ---------------------------------------------------------

# WordPress system path prefixes that are never blog posts
_NON_POST_PREFIXES = (
    "/category/", "/tag/", "/author/", "/page/",
    "/wp-content/", "/wp-admin/", "/wp-json/",
    "/feed/", "/search/", "/cart/", "/checkout/",
    "/my-account/", "/shop/", "/product/",
    "/blog/",   # listing/archive pages, not individual posts
    "#", "?",
)


def _is_blog_post_url(parsed) -> bool:
    """
    True only for single-slug post URLs like:
      https://tutorialsdojo.com/some-post-slug/

    Rejects multi-level paths, taxonomy pages, and system paths.
    """
    path = parsed.path.strip("/")
    # Must have exactly one path segment (no slashes in the slug)
    if not path or "/" in path:
        return False
    # Reject known WordPress non-post prefixes
    full_path = "/" + path + "/"
    return not any(full_path.startswith(pfx) for pfx in _NON_POST_PREFIXES)


def extract_internal_links(
    soup: BeautifulSoup, article_url: str
) -> list[str]:
    """Return unique blog post links found inside the article body."""
    body = soup.select_one(_ARTICLE_BODY_SEL)
    if body is None:
        return []

    seen: set[str] = set()
    links: list[str] = []
    canonical = article_url.rstrip("/")

    for a in body.select("a[href]"):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:")):
            continue
        absolute = urljoin(article_url, href)
        parsed = urlparse(absolute)
        if (
            SITE_DOMAIN in parsed.netloc
            and absolute.rstrip("/") != canonical
            and absolute not in seen
            and _is_blog_post_url(parsed)
        ):
            seen.add(absolute)
            links.append(absolute)

    return links


# --- author -----------------------------------------------------------------

def _extract_author(soup: BeautifulSoup, jsonld: list[dict]) -> str:
    # 1. <a rel="author"> — CSS word-match attribute selector
    a = soup.select_one("a[rel~='author']")
    if a:
        return a.get_text(strip=True)

    # 2. Common author containers — single selector covers all variants
    tag = soup.select_one(".author, .byline, .post-author, .entry-author")
    if tag:
        inner = tag.select_one("a")
        text = (inner or tag).get_text(strip=True)
        if text:
            return text

    # 3. JSON-LD author (pre-parsed)
    for entry in jsonld:
        author = entry.get("author")
        if isinstance(author, dict):
            name = author.get("name", "")
            if name:
                return name
        if isinstance(author, list) and author:
            name = author[0].get("name", "")
            if name:
                return name

    # 4. <meta name="author">
    m = soup.select_one("meta[name='author']")
    if m and m.get("content"):
        return m["content"].strip()

    return ""


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _pipe(values: list[str]) -> str:
    return " | ".join(v.replace("|", "/").strip() for v in values if v.strip())


def load_existing_urls(csv_path: str) -> set[str]:
    done: set[str] = set()
    if not os.path.exists(csv_path):
        return done
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("url"):
                done.add(row["url"])
    log.info("Resuming — %d article(s) already in %s.", len(done), csv_path)
    return done


def save_results_to_csv(results: list[ArticleMeta]) -> None:
    if not results:
        return
    write_header = not os.path.exists(OUTPUT_CSV)
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
        for meta in results:
            writer.writerow({fn: getattr(meta, fn) for fn in CSV_FIELDNAMES})


def save_failed_url(url: str, reason: str) -> None:
    write_header = not os.path.exists(FAILED_CSV)
    with open(FAILED_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "reason"])
        if write_header:
            writer.writeheader()
        writer.writerow({"url": url, "reason": reason})


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=== Tutorials Dojo Blog Crawler ===")

    done_urls = load_existing_urls(OUTPUT_CSV)
    total_discovered = 0
    total_saved = 0

    for page_num, article_urls in discover_article_urls():
        new_urls = [u for u in article_urls if u not in done_urls]
        total_discovered += len(article_urls)

        print(
            f"\n[Page {page_num}] {len(article_urls)} article(s) found "
            f"({len(new_urls)} new) | Total discovered so far: {total_discovered}"
        )

        batch: list[ArticleMeta] = []

        for idx, article_url in enumerate(article_urls, start=1):
            if article_url in done_urls:
                print(f"  [{idx}/{len(article_urls)}] SKIP (already done): {article_url}")
                continue

            print(f"  [{idx}/{len(article_urls)}] Scraping: {article_url}")
            log.info("Scraping article: %s", article_url)

            soup, status, final_url, raw_html = fetch_page(article_url)

            if soup is None:
                reason = f"HTTP {status}" if status else "fetch failed after retries"
                log.error("Failed (%s): %s", reason, article_url)
                save_failed_url(article_url, reason)
                done_urls.add(article_url)
                time.sleep(REQUEST_DELAY)
                continue

            meta = parse_article_page(soup, article_url, raw_html)
            batch.append(meta)
            done_urls.add(article_url)
            total_saved += 1

            log.debug("OK: %s | author=%s | cats=%s", meta.title, meta.author, meta.categories)
            time.sleep(REQUEST_DELAY)

        if batch:
            save_results_to_csv(batch)
            print(f"  Saved {len(batch)} article(s) from page {page_num}.")

    print(f"\n=== Done. {total_saved} article(s) saved to {OUTPUT_CSV} ===")
    log.info("Crawl complete. %d articles saved.", total_saved)


if __name__ == "__main__":
    main()
