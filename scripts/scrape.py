# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "trafilatura>=2.0",
#   "markdownify>=0.13",
#   "beautifulsoup4>=4.12",
#   "lxml>=5",
# ]
# ///
"""Scrape a documentation site into one markdown file per page.

Usage (run from repo root):
    uv run scripts/scrape.py --url https://argo-cd.readthedocs.io/en/stable/ --out corpora/argocd/raw
    uv run scripts/scrape.py --url ... --out ... --dry-run          # discover only, print count
    uv run scripts/scrape.py --url ... --out ... --max-pages 800

Discovery strategies, tried in order unless --strategy pins one:
    llms     <origin>/llms.txt, links under the prefix used as seeds
    sitemap  robots.txt Sitemap: lines, <origin>/sitemap.xml, <prefix>sitemap.xml
    crawl    breadth-first link following from the prefix URL, same origin, under prefix

Content: if the site serves native markdown at <url>.md it is used as-is.
Otherwise HTML goes through trafilatura, with markdownify as a fallback.

Every page gets frontmatter (source_url, title, fetched_at) and the run
writes pages.json next to <out> plus a JSON summary on stdout. Progress goes to stderr.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from markdownify import markdownify

USER_AGENT = "docs-graph-tool/0.1 (local documentation indexing; contact: platform team)"
SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf", ".zip", ".tar",
    ".gz", ".tgz", ".css", ".js", ".json", ".xml", ".txt", ".yaml", ".yml", ".mp4",
    ".woff", ".woff2", ".ttf", ".map", ".rss", ".atom",
}
ANCHOR_RE = re.compile(r"\[¶\]\(#[^)]*\)|\[#\]\(#[^)]*\)|\s*¶")
BLANKS_RE = re.compile(r"\n{3,}")
NATIVE_MD_BANNER_RE = re.compile(
    r"^>?\s*For the complete documentation index[^\n]*\n(?:\s*Markdown versions of all docs pages[^\n]*\n)?\n*",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def normalize(url: str) -> str:
    """Canonical form for dedupe: no fragment, no query, no trailing index.html."""
    p = urlparse(url)
    path = p.path or "/"
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    return urlunparse((p.scheme, p.netloc.lower(), path, "", "", ""))


def key(url: str) -> str:
    return normalize(url).rstrip("/")


def under_prefix(url: str, prefix: str) -> bool:
    return key(url).startswith(key(prefix)) and urlparse(url).netloc.lower() == urlparse(prefix).netloc.lower()


def looks_like_page(url: str) -> bool:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix in ("", ".html", ".htm")


class Fetcher:
    def __init__(self, delay: float, timeout: float = 30.0):
        self.delay = delay
        self.client = httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            http2=False,
        )
        self._last = 0.0

    def get(self, url: str, retries: int = 2) -> httpx.Response | None:
        for attempt in range(retries + 1):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.client.get(url)
                self._last = time.monotonic()
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                return r
            except httpx.HTTPError as e:
                self._last = time.monotonic()
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                log(f"  ! fetch error {url}: {e}")
                return None
        return None


# ---------- discovery ----------

def discover_llms(f: Fetcher, prefix: str) -> list[str]:
    origin = "{0.scheme}://{0.netloc}".format(urlparse(prefix))
    r = f.get(urljoin(origin, "/llms.txt"))
    if not r or r.status_code != 200 or "text" not in r.headers.get("content-type", ""):
        return []
    links = re.findall(r"\((https?://[^)\s]+)\)", r.text) + re.findall(r"^\s*-\s*(https?://\S+)", r.text, re.M)
    urls = [u for u in links if under_prefix(u, prefix) and looks_like_page(u)]
    return sorted({key(u): u for u in urls}.values())


def _parse_sitemap(f: Fetcher, url: str, base: str, seen: set[str], depth: int = 0) -> list[str]:
    if url in seen or depth > 3:
        return []
    seen.add(url)
    r = f.get(url)
    if not r or r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "xml")
    out: list[str] = []
    if soup.find("sitemapindex"):
        for loc in soup.find_all("loc"):
            out.extend(_parse_sitemap(f, urljoin(base, loc.text.strip()), base, seen, depth + 1))
        return out
    for loc in soup.find_all("loc"):
        out.append(urljoin(base, loc.text.strip()))
    return out


def discover_sitemap(f: Fetcher, prefix: str) -> list[str]:
    origin = "{0.scheme}://{0.netloc}".format(urlparse(prefix))
    candidates: list[str] = []
    r = f.get(urljoin(origin, "/robots.txt"))
    if r and r.status_code == 200:
        candidates += [urljoin(origin, m.strip()) for m in re.findall(r"(?im)^sitemap:\s*(\S+)", r.text)]
    candidates += [urljoin(origin, "/sitemap.xml"), urljoin(prefix if prefix.endswith("/") else prefix + "/", "sitemap.xml")]
    seen: set[str] = set()
    urls: list[str] = []
    for c in candidates:
        urls.extend(_parse_sitemap(f, c, origin, seen))
    urls = [u for u in urls if under_prefix(u, prefix) and looks_like_page(u)]
    return sorted({key(u): u for u in urls}.values())


def extract_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "javascript:", "tel:")):
            continue
        out.append(urljoin(base_url, href))
    return out


# ---------- content ----------

def probe_native_markdown(f: Fetcher, seeds: list[str]) -> bool:
    """Try the deepest few seed URLs: section roots often lack a .md twin."""
    sample = sorted(seeds, key=lambda u: -u.count("/"))[:3]
    for u in sample:
        r = f.get(key(u) + ".md")
        if r and r.status_code == 200 and "markdown" in r.headers.get("content-type", ""):
            return True
    return False


def clean_markdown(md: str) -> str:
    md = ANCHOR_RE.sub("", md)
    md = NATIVE_MD_BANNER_RE.sub("", md)
    md = BLANKS_RE.sub("\n\n", md)
    return md.strip() + "\n"


def html_title(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return ANCHOR_RE.sub("", h1.get_text(" ", strip=True)).strip()
    if soup.title and soup.title.string:
        return soup.title.string.split("|")[0].split(" - ")[0].strip()
    return None


def html_to_markdown(html: str) -> tuple[str | None, str | None]:
    md = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_formatting=True,
        include_images=False,
        favor_precision=False,
    )
    title = None
    meta = trafilatura.extract_metadata(html)
    if meta and meta.title:
        title = ANCHOR_RE.sub("", meta.title).strip()
    if not md or len(md) < 200:
        soup = BeautifulSoup(html, "lxml")
        node = soup.find("main") or soup.find("article") or soup.find(role="main") or soup.body
        if node:
            for t in node.find_all(["nav", "script", "style", "footer", "aside"]):
                t.decompose()
            md = markdownify(str(node), heading_style="ATX", strip=["img"])
    if not title:
        title = html_title(html)
    return (md, title)


def md_title(md: str) -> str | None:
    m = re.search(r"^#\s+(.+)$", md, re.M)
    return m.group(1).strip() if m else None


def file_for(url: str, prefix: str) -> Path:
    rel = key(url)[len(key(prefix)):].strip("/")
    if rel.endswith(".html") or rel.endswith(".htm"):
        rel = rel.rsplit(".", 1)[0]
    if not rel:
        rel = "index"
    parts = [re.sub(r"[^A-Za-z0-9._-]+", "-", p).strip("-") or "page" for p in rel.split("/")]
    return Path(*parts).with_suffix(".md")


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="URL prefix. Only pages under it are scraped.")
    ap.add_argument("--out", required=True, help="Output directory, e.g. corpora/<slug>/raw. Replaced in full.")
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--strategy", choices=["auto", "llms", "sitemap", "crawl"], default="auto")
    ap.add_argument("--follow-links", action="store_true", help="Also follow links found on pages (always on for crawl).")
    ap.add_argument("--delay", type=float, default=0.25, help="Seconds between requests.")
    ap.add_argument("--no-native-md", action="store_true", help="Never use <url>.md even if the site serves it.")
    ap.add_argument("--dry-run", action="store_true", help="Discover URLs, print the count and a sample, fetch nothing.")
    args = ap.parse_args()

    prefix = args.url if args.url.endswith("/") else args.url + "/"
    f = Fetcher(delay=args.delay)

    strategy = args.strategy
    seeds: list[str] = []
    if strategy in ("auto", "llms"):
        seeds = discover_llms(f, prefix)
        if seeds:
            strategy = "llms"
            log(f"llms.txt: {len(seeds)} page links under prefix")
        elif args.strategy == "llms":
            log("llms.txt gave no links under the prefix")
            return 2
    if not seeds and strategy in ("auto", "sitemap"):
        seeds = discover_sitemap(f, prefix)
        if seeds:
            strategy = "sitemap"
            log(f"sitemap: {len(seeds)} page URLs under prefix")
        elif args.strategy == "sitemap":
            log("no sitemap found")
            return 2
    if not seeds:
        strategy = "crawl"
        seeds = [prefix]
        log("crawl: no seed list, following links from the prefix")
    follow = args.follow_links or strategy == "crawl"

    if args.dry_run:
        summary = {
            "strategy": strategy,
            "discovered": len(seeds),
            "max_pages": args.max_pages,
            "over_cap": len(seeds) > args.max_pages,
            "sample": seeds[:10],
        }
        if strategy == "crawl":
            summary["note"] = "crawl mode discovers while fetching, so the count is unknown until a real run"
        print(json.dumps(summary, indent=2))
        return 0

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    native_md = False
    if not args.no_native_md:
        native_md = probe_native_markdown(f, seeds)
        log(f"native markdown endpoint: {'yes' if native_md else 'no'}")

    queue = deque(seeds)
    queued = {key(u) for u in seeds}
    pages: list[dict] = []
    errors: list[dict] = []
    skipped = 0
    capped = False
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    while queue:
        if len(pages) >= args.max_pages:
            capped = True
            break
        url = queue.popleft()
        if not looks_like_page(url):
            skipped += 1
            continue
        md: str | None = None
        title: str | None = None
        html: str | None = None
        if native_md:
            r = f.get(key(url) + ".md")
            if r and r.status_code == 200 and "markdown" in r.headers.get("content-type", ""):
                md = r.text
                title = md_title(md)
        if md is None or follow:
            r = f.get(url)
            if not r:
                errors.append({"url": url, "error": "fetch failed"})
                continue
            if r.status_code != 200:
                errors.append({"url": url, "error": f"http {r.status_code}"})
                continue
            if "html" not in r.headers.get("content-type", ""):
                skipped += 1
                continue
            html = r.text
            if md is None:
                md, title = html_to_markdown(html)
            elif not title:
                title = html_title(html)
        if follow and html:
            for link in extract_links(html, url):
                k = key(link)
                if k not in queued and under_prefix(link, prefix) and looks_like_page(link):
                    queued.add(k)
                    queue.append(normalize(link))
        if not md or len(md.strip()) < 50:
            skipped += 1
            log(f"  - empty: {url}")
            continue
        md = clean_markdown(md)
        title = title or md_title(md) or key(url).rsplit("/", 1)[-1] or "index"
        rel = file_for(url, prefix)
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            "---\n"
            f"source_url: {normalize(url)}\n"
            f"title: {json.dumps(title)}\n"
            f"fetched_at: {fetched_at}\n"
            "---\n\n"
        )
        path.write_text(fm + md, encoding="utf-8")
        pages.append({
            "file": str(rel),
            "url": normalize(url),
            "title": title,
            "chars": len(md),
            "sha256": hashlib.sha256(md.encode("utf-8")).hexdigest()[:16],
        })
        if len(pages) % 25 == 0:
            log(f"  {len(pages)} pages, {len(queue)} queued")

    (out.parent / "pages.json").write_text(json.dumps(pages, indent=2), encoding="utf-8")
    summary = {
        "strategy": strategy,
        "native_markdown": native_md,
        "pages": len(pages),
        "skipped": skipped,
        "errors": len(errors),
        "capped": capped,
        "remaining_in_queue": len(queue),
        "out": str(out),
        "fetched_at": fetched_at,
    }
    if errors:
        (out.parent / "errors.json").write_text(json.dumps(errors, indent=2), encoding="utf-8")
    log(f"done: {len(pages)} pages, {skipped} skipped, {len(errors)} errors{', CAPPED' if capped else ''}")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
