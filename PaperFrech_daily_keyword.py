"""
Fetch recent arXiv papers by keyword/category and send one daily Markdown digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

CONFIG_DIR = Path("./config")
CACHE_DIR = Path("./cache")
EMAIL_CONFIG = CONFIG_DIR / "MyEmail.yaml"

CATEGORIES = ["cs.CV", "cs.CL", "cs.AI"]
KEYWORDS = [
    # open-vocabulary segmentation
    "open vocabulary semantic segmentation",
    "open-vocabulary semantic segmentation",
    "open vocabulary segmentation",
    "open-vocabulary segmentation",

    # vision-language / multimodal alignment
    "vision-language alignment",
    "vision language alignment",
    "image-text alignment",
    "image text alignment",
    "cross-modal alignment",
    "cross modal alignment",
    "multimodal alignment",
    "multi-modal alignment",

    # unsupervised / unpaired alignment
    "unsupervised alignment",
    "unsupervised embedding alignment",
    "unsupervised representation alignment",
    "unsupervised cross-modal alignment",
    "unpaired alignment",
    "unpaired image-text alignment",
    "unpaired vision-language alignment",
    "unpaired multimodal alignment",

    # distribution / geometry / translator
    "distribution matching",
    "embedding distribution alignment",
    "embedding translation",
    "embedding translator",
    "vector space alignment",
    "representation alignment",
    "manifold alignment",
    "optimal transport alignment",
    "adversarial alignment",
]
DAYS = 20
MAX_RESULTS = 100
REQUEST_TIMEOUT = 30
MIN_ARXIV_INTERVAL_SECONDS = 3.5
USER_AGENT = "PaperFetch/1.0 contact: oymk66666@outlook.com"
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

LAST_ARXIV_REQUEST_TS = 0.0
LOGGER = logging.getLogger("paperfetch")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch arXiv papers and send a daily digest.")
    parser.add_argument("--days", type=int, default=DAYS, help="Recent days to query.")
    parser.add_argument("--max-results", type=int, default=MAX_RESULTS, help="Maximum arXiv results to request.")
    parser.add_argument("--dry-run", action="store_true", help="Run fully but do not send email.")
    parser.add_argument("--no-email", action="store_true", help="Generate the report without sending email.")
    parser.add_argument("--no-cache", action="store_true", help="Do not read or write the daily arXiv cache.")
    parser.add_argument("--log-level", default="INFO", help="Python logging level.")
    return parser.parse_args()


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def normalize_keywords(keywords: list[str] | tuple[str, ...] | str) -> list[str]:
    if isinstance(keywords, str):
        parts = re.split(r"\s*,\s*|\s+OR\s+|\s*\|\|\s*", keywords.strip(), flags=re.IGNORECASE)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(keywords, (list, tuple)):
        return [str(k).strip() for k in keywords if str(k).strip()]
    raise ValueError("keywords must be a list or a string")


def make_keyword_clause(keywords: list[str]) -> str:
    parts = []
    for keyword in keywords:
        escaped = keyword.replace('"', '\\"')
        if " " in escaped or "-" in escaped:
            parts.append(f'all:"{escaped}"')
        else:
            parts.append(f"all:{escaped}")
    return " OR ".join(parts)


def make_category_clause(categories: list[str]) -> str:
    return " OR ".join(f"cat:{category}" for category in categories)


def submitted_date_range(days: int) -> tuple[str, str]:
    if days < 1:
        raise ValueError("--days must be >= 1")
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).strftime("%Y%m%d0000")
    end = now.strftime("%Y%m%d2359")
    return start, end


def build_arxiv_url(categories: list[str], keywords: list[str], days: int, max_results: int) -> tuple[str, str]:
    if not categories:
        raise ValueError("categories cannot be empty")
    if not keywords:
        raise ValueError("keywords cannot be empty")
    if max_results < 1:
        raise ValueError("--max-results must be >= 1")

    max_results = min(max_results, 100)
    start_date, end_date = submitted_date_range(days)
    raw_query = (
        f"({make_category_clause(categories)}) "
        f"AND ({make_keyword_clause(keywords)}) "
        f"AND submittedDate:[{start_date} TO {end_date}]"
    )
    encoded_query = quote_plus(raw_query)
    url = (
        "https://export.arxiv.org/api/query?"
        f"search_query={encoded_query}"
        f"&start=0&max_results={max_results}"
        "&sortBy=submittedDate&sortOrder=descending"
    )
    return url, raw_query


def wait_for_arxiv_rate_limit() -> None:
    global LAST_ARXIV_REQUEST_TS
    now = time.monotonic()
    elapsed = now - LAST_ARXIV_REQUEST_TS
    if elapsed < MIN_ARXIV_INTERVAL_SECONDS:
        sleep_for = MIN_ARXIV_INTERVAL_SECONDS - elapsed
        LOGGER.info("arXiv rate limit sleep %.2fs", sleep_for)
        time.sleep(sleep_for)
    LAST_ARXIV_REQUEST_TS = time.monotonic()


def retry_after_seconds(exc: HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


def backoff_seconds(attempt: int, exc: Exception) -> float:
    if isinstance(exc, HTTPError):
        retry_after = retry_after_seconds(exc)
        if retry_after is not None:
            return min(retry_after, 300.0)
    schedule = [30, 60, 120, 240, 300]
    base = schedule[min(attempt, len(schedule) - 1)]
    return base + random.uniform(0, 10)


def rate_limited_fetch(url: str, max_retries: int = 5) -> bytes:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            LOGGER.info("arXiv request attempt=%s max_retries=%s", attempt + 1, max_retries)
            wait_for_arxiv_rate_limit()
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError, socket.timeout) as exc:
            last_error = exc
            if isinstance(exc, HTTPError) and exc.code == 429:
                LOGGER.warning("HTTP 429 from arXiv")
            elif isinstance(exc, (TimeoutError, socket.timeout)):
                LOGGER.warning("timeout while requesting arXiv")
            else:
                LOGGER.warning("recoverable arXiv request failure: %r", exc)

            if attempt >= max_retries - 1:
                break
            sleep_for = backoff_seconds(attempt, exc)
            LOGGER.info("retry after %.2fs", sleep_for)
            time.sleep(sleep_for)
    raise RuntimeError(f"Failed to fetch arXiv feed after {max_retries} retries: {last_error}")


def parse_feed(data: bytes) -> Any:
    import feedparser

    feed = feedparser.parse(data)
    if getattr(feed, "bozo", False):
        raise RuntimeError(f"arXiv feed parse failed: {getattr(feed, 'bozo_exception', 'unknown error')}")
    return feed


def cache_path(raw_query: str) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    query_hash = hashlib.sha256(raw_query.encode("utf-8")).hexdigest()[:12]
    return CACHE_DIR / f"arxiv_{today}_{query_hash}.json"


def load_cache(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    LOGGER.info("loaded arXiv cache: %s", path)
    return payload.get("papers", [])


def write_cache(path: Path, raw_query: str, papers: list[dict[str, Any]]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "query": raw_query,
        "papers": papers,
    }
    with path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    LOGGER.info("wrote arXiv cache: %s", path)


def keyword_in_text(text: str, keyword: str) -> bool:
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(keyword.lower()) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, text.lower()) is not None


def keyword_match(entry: Any, keywords: list[str]) -> bool:
    text = f"{entry.get('title', '')} {entry.get('summary', '')}"
    return any(keyword_in_text(text, keyword) for keyword in keywords)


def entry_recent_time(entry: Any) -> datetime | None:
    candidates = []
    for key in ("published", "updated"):
        value = entry.get(key)
        if not value:
            continue
        try:
            candidates.append(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    return max(candidates) if candidates else None


def arxiv_id(entry: Any) -> str:
    raw = entry.get("id") or entry.get("link") or ""
    match = re.search(r"abs/([^v?#]+)(?:v\d+)?", raw)
    if match:
        return match.group(1)
    return raw.rsplit("/", 1)[-1].split("v")[0] or raw


def entry_categories(entry: Any) -> str:
    tags = entry.get("tags", [])
    terms = [tag.get("term") for tag in tags if tag.get("term")]
    return ", ".join(terms)


def entry_authors(entry: Any) -> str:
    authors = entry.get("authors", [])
    names = []
    for author in authors:
        if isinstance(author, dict):
            names.append(author.get("name", ""))
        else:
            names.append(getattr(author, "name", ""))
    return ", ".join(name for name in names if name)


def papers_from_feed(feed: Any, keywords: list[str]) -> list[dict[str, Any]]:
    papers_by_id: dict[str, dict[str, Any]] = {}
    for entry in feed.entries:
        if not keyword_match(entry, keywords):
            continue
        recent_time = entry_recent_time(entry)
        if recent_time is None:
            continue
        paper_id = arxiv_id(entry)
        title = entry.get("title", "").strip().replace("\n", " ")
        papers_by_id[paper_id] = {
            "arxiv_id": paper_id,
            "title": title,
            "authors": entry_authors(entry),
            "summary": re.sub(r"\s+", " ", entry.get("summary", "").strip()),
            "published": recent_time.strftime("%Y-%m-%d"),
            "category": entry_categories(entry),
            "link": entry.get("link", ""),
        }
    papers = list(papers_by_id.values())
    papers.sort(key=lambda item: item["published"], reverse=True)
    return papers


def fetch_arxiv_papers(
    keywords: list[str],
    categories: list[str],
    days: int,
    max_results: int,
    no_cache: bool,
) -> list[dict[str, Any]]:
    url, raw_query = build_arxiv_url(categories, keywords, days, max_results)
    path = cache_path(raw_query)
    LOGGER.info("arXiv query: %s", raw_query)
    LOGGER.info("arXiv query URL: %s", url[:300])

    if not no_cache:
        cached = load_cache(path)
        if cached is not None:
            return cached

    try:
        data = rate_limited_fetch(url)
        feed = parse_feed(data)
        papers = papers_from_feed(feed, keywords)
        LOGGER.info("final matched paper count=%s", len(papers))
        if not no_cache and papers:
            write_cache(path, raw_query, papers)
        elif not no_cache:
            LOGGER.info("skip writing empty arXiv cache")
        return papers
    except Exception:
        if not no_cache:
            cached = load_cache(path)
            if cached is not None:
                LOGGER.warning("WARNING: arXiv fetch failed, fallback to cache")
                return cached
        raise


def build_email_subject(papers: list[dict[str, Any]], days: int) -> str:
    if papers:
        return f"PaperFetch: 最近 {days} 天找到 {len(papers)} 篇相关论文"
    return f"PaperFetch: 最近 {days} 天未找到匹配论文"


def build_empty_report(keywords: list[str], categories: list[str], days: int) -> str:
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return "\n".join(
        [
            "# PaperFetch 检索结果",
            "",
            "本次没有找到符合条件的论文。",
            "",
            f"- 检索范围：最近 {days} 天",
            f"- arXiv categories: {', '.join(categories)}",
            f"- 当前关键词：{', '.join(keywords)}",
            "- 匹配论文数量：0",
            f"- 运行时间：{run_time}",
            "",
            "这表示程序运行成功，但本次查询没有匹配结果；这不是程序错误。",
        ]
    )


def generate_markdown(papers: list[dict[str, Any]], keywords: list[str], categories: list[str], days: int) -> str:
    if not papers:
        return build_empty_report(keywords, categories, days)

    lines = ["# Daily arXiv Digest\n"]
    for index, paper in enumerate(papers, 1):
        lines.append(f"### {index}. [{paper['title']}]({paper['link']})")
        lines.append(f"- **arXiv ID:** `{paper['arxiv_id']}`")
        lines.append(f"- **Authors:** {paper['authors']}")
        lines.append(f"- **Category:** `{paper['category']}`")
        lines.append(f"- **Published:** {paper['published']}\n")
        lines.append(f"{paper['summary']}\n")
    return "\n".join(lines)


def load_email_config() -> Any:
    from omegaconf import OmegaConf

    if not EMAIL_CONFIG.exists():
        raise FileNotFoundError(f"email config not found: {EMAIL_CONFIG}")
    cfg = OmegaConf.load(EMAIL_CONFIG)
    required = ["sender_email", "sender_pass", "receiver_email"]
    missing = [name for name in required if not getattr(cfg, name, None)]
    if missing:
        raise ValueError(f"email config missing required fields: {', '.join(missing)}")
    return cfg


def send_email(subject: str, markdown_content: str) -> None:
    import yagmail

    cfg = load_email_config()
    yag = yagmail.SMTP(
        user=cfg.sender_email,
        password=cfg.sender_pass,
        host=SMTP_HOST,
        port=SMTP_PORT,
        smtp_ssl=True,
    )
    yag.send(to=cfg.receiver_email, subject=subject, contents=[markdown_content])
    LOGGER.info("email sent to %s", cfg.receiver_email)


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)

    keywords = normalize_keywords(KEYWORDS)
    categories = [category.strip() for category in CATEGORIES if category.strip()]
    LOGGER.info("starting PaperFetch days=%s max_results=%s", args.days, min(args.max_results, 100))

    papers = fetch_arxiv_papers(
        keywords=keywords,
        categories=categories,
        days=args.days,
        max_results=args.max_results,
        no_cache=args.no_cache,
    )
    LOGGER.info("final paper count=%s", len(papers))

    if not papers:
        LOGGER.info("No matched papers found, sending empty report email.")

    report = generate_markdown(papers, keywords, categories, args.days)
    subject = build_email_subject(papers, args.days)
    should_send_email = not args.dry_run and not args.no_email
    LOGGER.info("send_email=%s dry_run=%s no_email=%s", should_send_email, args.dry_run, args.no_email)

    if should_send_email:
        send_email(subject, report)
    else:
        LOGGER.info("email not sent")
        LOGGER.debug("generated report:\n%s", report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
