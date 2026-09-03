"""
Fetch recent arXiv papers by keyword/category and send one daily Markdown digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from requests import HTTPError, RequestException, Timeout

CONFIG_DIR = Path("./config")
CACHE_DIR = Path("./cache")
EMAIL_CONFIG = CONFIG_DIR / "MyEmail.yaml"
ARXIV_API_URL = "https://export.arxiv.org/api/query"

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
DAYS = 7
MAX_RESULTS = 100
REQUEST_TIMEOUT = (10, 60)
MIN_ARXIV_INTERVAL_SECONDS = 10.0
KEYWORD_BATCH_SIZE = 30
BATCH_SLEEP_SECONDS = 120
MAX_ARXIV_RETRIES = 3
NORMAL_RETRY_DELAYS_SECONDS = (10.0, 30.0)
DEFAULT_ARXIV_BUSY_WAIT_SECONDS = 300.0
MAX_ARXIV_BUSY_WAIT_SECONDS = 300.0
DEFAULT_ARXIV_COOLDOWN_SECONDS = 1800.0
CACHE_FALLBACK_MAX_AGE_DAYS = 7
USER_AGENT = "PaperFetch/1.0 contact: oymk66666@outlook.com"
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

LAST_ARXIV_REQUEST_TS = 0.0
LOGGER = logging.getLogger("paperfetch")


class ArxivFetchError(RuntimeError):
    """Base exception for arXiv fetch failures."""


class ArxivServiceBusyError(ArxivFetchError):
    """Raised when arXiv reports a capacity or rate-limit failure."""

    def __init__(self, message: str, status_sequence: list[int] | None = None):
        super().__init__(message)
        self.status_sequence = list(status_sequence or [])


class ArxivRateLimitError(ArxivServiceBusyError):
    """Raised when arXiv returns HTTP 429 rate limit."""


class ArxivRateLimitCooldownError(ArxivServiceBusyError):
    """Raised when a recorded arXiv service-busy cooldown is active."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch arXiv papers and send a daily digest.")
    parser.add_argument("--days", type=int, default=DAYS, help="Recent days to query.")
    parser.add_argument("--max-results", type=int, default=MAX_RESULTS, help="Maximum arXiv results to request.")
    parser.add_argument("--dry-run", action="store_true", help="Run fully but do not send email.")
    parser.add_argument("--no-email", action="store_true", help="Generate the report without sending email.")
    parser.add_argument("--no-cache", action="store_true", help="Do not read or write the daily arXiv cache.")
    parser.add_argument("--no-cache-fallback", action="store_true", help="Do not send a cached digest if arXiv fails.")
    parser.add_argument(
        "--cache-fallback-max-age-days",
        type=int,
        default=CACHE_FALLBACK_MAX_AGE_DAYS,
        help="Maximum age in days for the latest successful digest cache.",
    )
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


def build_arxiv_request(
    categories: list[str],
    keywords: list[str],
    days: int,
    max_results: int,
) -> tuple[str, dict[str, str | int], str]:
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
    params: dict[str, str | int] = {
        "search_query": raw_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    return ARXIV_API_URL, params, raw_query


def chunked(items: list[str], size: int) -> list[list[str]]:
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [items[index : index + size] for index in range(0, len(items), size)]


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
    response = getattr(exc, "response", None)
    value = response.headers.get("Retry-After") if response is not None else None
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


def backoff_seconds(retry_index: int) -> float:
    return NORMAL_RETRY_DELAYS_SECONDS[
        min(retry_index, len(NORMAL_RETRY_DELAYS_SECONDS) - 1)
    ]


def arxiv_rate_limit_state_path() -> Path:
    return CACHE_DIR / "arxiv_rate_limit_state.json"


def load_arxiv_rate_limit_state() -> dict[str, Any] | None:
    path = arxiv_rate_limit_state_path()
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("ignoring invalid arXiv rate-limit state path=%s error=%r", path, exc)
        return None
    if not isinstance(payload, dict):
        LOGGER.warning("ignoring invalid arXiv rate-limit state without an object: %s", path)
        return None
    return payload


def response_body_hint(response: requests.Response, limit: int = 200) -> str:
    text = response.content.decode(response.encoding or "utf-8", errors="replace")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def log_arxiv_http_failure(response: requests.Response, elapsed_seconds: float) -> str:
    hint = response_body_hint(response)
    LOGGER.warning(
        "arXiv HTTP failure status=%s elapsed=%.2fs retry_after=%r x_cache=%r "
        "via=%r date=%r body=%r",
        response.status_code,
        elapsed_seconds,
        response.headers.get("Retry-After"),
        response.headers.get("X-Cache"),
        response.headers.get("Via"),
        response.headers.get("Date"),
        hint,
    )
    return hint


def write_arxiv_rate_limit_state(
    exc: HTTPError,
    *,
    fallback_cooldown_seconds: float = DEFAULT_ARXIV_COOLDOWN_SECONDS,
    response_hint: str | None = None,
) -> dict[str, Any]:
    path = arxiv_rate_limit_state_path()
    now = datetime.now(timezone.utc)
    retry_after = retry_after_seconds(exc)
    response = getattr(exc, "response", None)
    status_code = response.status_code if response is not None else None
    cooldown_seconds = retry_after if retry_after is not None else fallback_cooldown_seconds
    payload: dict[str, Any] = {
        "last_status": status_code,
        "last_failure_at": now.isoformat(),
        "retry_after_until": (now + timedelta(seconds=cooldown_seconds)).isoformat(),
        "response_hint": response_hint or "",
        "wait_source": "Retry-After" if retry_after is not None else "default-cooldown",
    }

    temporary_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
        temporary_path.replace(path)
    except OSError as write_error:
        LOGGER.warning("failed to persist arXiv rate-limit state path=%s error=%r", path, write_error)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("failed to remove temporary arXiv rate-limit state: %s", temporary_path)

    LOGGER.warning(
        "recorded arXiv service-busy state path=%s status=%s wait_source=%s "
        "retry_after_until=%s",
        path,
        payload["last_status"],
        payload["wait_source"],
        payload["retry_after_until"],
    )
    return payload


def clear_arxiv_rate_limit_state() -> None:
    path = arxiv_rate_limit_state_path()
    try:
        if path.exists():
            path.unlink()
            LOGGER.info("cleared arXiv service-busy state: %s", path)
    except OSError as exc:
        LOGGER.warning("failed to clear arXiv rate-limit state path=%s error=%r", path, exc)


def active_arxiv_rate_limit_cooldown(
    state: dict[str, Any],
    now: datetime | None = None,
) -> str | None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    retry_after_until_value = state.get("retry_after_until")
    if retry_after_until_value:
        retry_after_until = parse_cache_datetime(retry_after_until_value)
        if retry_after_until is not None and retry_after_until > current:
            source = state.get("wait_source", "stored deadline")
            return f"service-busy cooldown ({source}) is active until {retry_after_until.isoformat()}"
        return None

    # Migrate the previous format without preserving its incorrect UTC-day block.
    legacy_failure_at = parse_cache_datetime(state.get("last_429_at", ""))
    if legacy_failure_at is not None:
        legacy_deadline = legacy_failure_at + timedelta(seconds=DEFAULT_ARXIV_COOLDOWN_SECONDS)
        if legacy_deadline > current:
            return f"legacy service-busy cooldown is active until {legacy_deadline.isoformat()}"
    return None


def enforce_arxiv_rate_limit_cooldown() -> None:
    state = load_arxiv_rate_limit_state()
    if state is None:
        return
    reason = active_arxiv_rate_limit_cooldown(state)
    if reason is None:
        clear_arxiv_rate_limit_state()
        return
    LOGGER.warning("skipping arXiv request because %s", reason)
    raise ArxivRateLimitCooldownError(
        f"Skipped the arXiv request because {reason}. Use cached results if available."
    )


def rate_limited_fetch(
    endpoint: str,
    params: dict[str, str | int],
    max_retries: int = MAX_ARXIV_RETRIES,
) -> bytes:
    enforce_arxiv_rate_limit_cooldown()
    last_error: Exception | None = None
    status_sequence: list[int] = []
    busy_retry_used = False
    normal_retry_index = 0

    for attempt in range(max_retries):
        try:
            LOGGER.info("arXiv GET request attempt=%s max_attempts=%s", attempt + 1, max_retries)
            wait_for_arxiv_rate_limit()
            request_started = time.monotonic()
            response = requests.get(
                endpoint,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            clear_arxiv_rate_limit_state()
            return response.content
        except HTTPError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code is not None:
                status_sequence.append(status_code)
            response_hint = ""
            if exc.response is not None:
                response_hint = log_arxiv_http_failure(
                    exc.response,
                    time.monotonic() - request_started,
                )

            if status_code in (429, 503):
                retry_after = retry_after_seconds(exc)
                retry_wait = (
                    retry_after
                    if retry_after is not None
                    else DEFAULT_ARXIV_BUSY_WAIT_SECONDS
                )
                may_retry = (
                    not busy_retry_used
                    and attempt < max_retries - 1
                    and retry_wait <= MAX_ARXIV_BUSY_WAIT_SECONDS
                )

                if may_retry:
                    busy_retry_used = True
                    write_arxiv_rate_limit_state(
                        exc,
                        fallback_cooldown_seconds=retry_wait,
                        response_hint=response_hint,
                    )
                    LOGGER.warning(
                        "arXiv service busy status=%s; wait %.2fs before the only "
                        "capacity retry response_sequence=%s",
                        status_code,
                        retry_wait,
                        " -> ".join(str(value) for value in status_sequence),
                    )
                    time.sleep(retry_wait)
                    continue

                state = write_arxiv_rate_limit_state(
                    exc,
                    response_hint=response_hint,
                )
                retry_detail = state["retry_after_until"]
                sequence = " -> ".join(str(value) for value in status_sequence)
                LOGGER.warning(
                    "arXiv remains unavailable; stop this run and use cache "
                    "response_sequence=%s",
                    sequence,
                )
                error_type = ArxivRateLimitError if status_code == 429 else ArxivServiceBusyError
                raise error_type(
                    "arXiv is temporarily unavailable due to service capacity or rate limiting. "
                    f"Response sequence: {sequence}. Next request is allowed after "
                    f"{retry_detail}. Use cached results if available.",
                    status_sequence,
                ) from exc

            if status_code is not None and 400 <= status_code < 500:
                raise ArxivFetchError(
                    f"arXiv returned non-retryable HTTP {status_code} for the API request."
                ) from exc

            LOGGER.warning("recoverable arXiv HTTP failure: %r", exc)

        except Timeout as exc:
            last_error = exc
            LOGGER.warning("timeout while requesting arXiv")

            if attempt >= max_retries - 1:
                break

        except RequestException as exc:
            last_error = exc
            LOGGER.warning("recoverable arXiv request failure: %r", exc)

            if attempt >= max_retries - 1:
                break

        if attempt >= max_retries - 1:
            break

        sleep_for = backoff_seconds(normal_retry_index)
        normal_retry_index += 1
        LOGGER.info("retry after %.2fs", sleep_for)
        time.sleep(sleep_for)

    raise ArxivFetchError(
        f"Failed to fetch arXiv feed after {max_retries} attempts at endpoint={endpoint}. "
        f"Response sequence: {' -> '.join(str(value) for value in status_sequence) or 'none'}. "
        f"Last error: {last_error!r}"
    ) from last_error


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


def latest_cache_path(keywords: list[str], categories: list[str], days: int, max_results: int) -> Path:
    signature = {
        "categories": categories,
        "keywords": keywords,
        "days": days,
        "max_results": min(max_results, 100),
    }
    encoded = json.dumps(signature, ensure_ascii=False, sort_keys=True)
    query_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return CACHE_DIR / f"arxiv_latest_{query_hash}.json"


def parse_cache_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_cache(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    papers = payload.get("papers")
    if not isinstance(papers, list):
        LOGGER.warning("ignoring invalid arXiv cache without a paper list: %s", path)
        return None
    LOGGER.info("loaded arXiv cache: %s paper_count=%s", path, len(papers))
    return papers


def load_latest_cache(
    keywords: list[str],
    categories: list[str],
    days: int,
    max_results: int,
    max_age_days: int,
) -> dict[str, Any] | None:
    if max_age_days < 1:
        LOGGER.info("latest cache fallback disabled by max_age_days=%s", max_age_days)
        return None

    path = latest_cache_path(keywords, categories, days, max_results)
    if not path.exists():
        LOGGER.info("no latest arXiv cache found: %s", path)
        return None

    with path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)

    papers = payload.get("papers")
    if not isinstance(papers, list):
        LOGGER.info("ignoring latest arXiv cache without a paper list: %s", path)
        return None

    created_at = parse_cache_datetime(payload.get("created_at", ""))
    if created_at is None:
        LOGGER.warning("ignoring latest arXiv cache with invalid created_at: %s", path)
        return None

    age = datetime.now(timezone.utc) - created_at
    if age > timedelta(days=max_age_days):
        LOGGER.warning(
            "latest arXiv cache is too old: path=%s age_days=%.2f max_age_days=%s",
            path,
            age.total_seconds() / 86400,
            max_age_days,
        )
        return None

    LOGGER.info("loaded latest arXiv cache: %s created_at=%s", path, created_at.isoformat())
    payload["path"] = str(path)
    return payload


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


def write_latest_cache(
    keywords: list[str],
    categories: list[str],
    days: int,
    max_results: int,
    papers: list[dict[str, Any]],
) -> None:
    path = latest_cache_path(keywords, categories, days, max_results)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "query": {
            "categories": categories,
            "keywords": keywords,
            "days": days,
            "max_results": min(max_results, 100),
        },
        "papers": papers,
    }
    with path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    LOGGER.info("wrote latest arXiv cache: %s", path)


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
    batches = chunked(keywords, KEYWORD_BATCH_SIZE)
    LOGGER.info(
        "arXiv keyword batching: keyword_count=%s batch_size=%s batch_count=%s",
        len(keywords),
        KEYWORD_BATCH_SIZE,
        len(batches),
    )
    papers_by_id: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    successful_batches = 0

    for index, batch in enumerate(batches, 1):
        endpoint, request_params, raw_query = build_arxiv_request(categories, batch, days, max_results)
        path = cache_path(raw_query)
        LOGGER.info("arXiv keyword batch %s/%s size=%s", index, len(batches), len(batch))
        LOGGER.info(
            "arXiv request method=GET endpoint=%s query_chars=%s max_results=%s",
            endpoint,
            len(raw_query),
            request_params["max_results"],
        )

        if not no_cache:
            cached = load_cache(path)
            if cached is not None:
                successful_batches += 1
                for paper in cached:
                    papers_by_id[paper["arxiv_id"]] = paper
                continue

        try:
            data = rate_limited_fetch(endpoint, request_params)
            feed = parse_feed(data)
            batch_papers = papers_from_feed(feed, batch)
            successful_batches += 1
            LOGGER.info("batch %s matched paper count=%s", index, len(batch_papers))
            for paper in batch_papers:
                papers_by_id[paper["arxiv_id"]] = paper

            if not no_cache:
                write_cache(path, raw_query, batch_papers)
        except ArxivServiceBusyError:
            LOGGER.exception(
                "arXiv service busy at keyword batch %s/%s; stop remaining batches",
                index,
                len(batches),
            )
            raise
        except Exception as exc:
            message = f"batch {index}/{len(batches)} failed: {exc!r}"
            failures.append(message)
            LOGGER.exception("arXiv keyword batch failed: %s", message)

        if index < len(batches):
            LOGGER.info("sleep %.2fs before next keyword batch", BATCH_SLEEP_SECONDS)
            time.sleep(BATCH_SLEEP_SECONDS)

    if successful_batches == 0:
        detail = "; ".join(failures) if failures else "no keyword batches completed"
        raise RuntimeError(f"Failed to fetch arXiv feed for all keyword batches: {detail}")

    if failures:
        LOGGER.warning("arXiv completed with %s failed keyword batch(es)", len(failures))

    papers = list(papers_by_id.values())
    papers.sort(key=lambda item: item["published"], reverse=True)
    LOGGER.info("final matched paper count=%s", len(papers))
    return papers


def build_email_subject(papers: list[dict[str, Any]], days: int) -> str:
    if papers:
        return f"PaperFetch: found {len(papers)} related paper(s) in the last {days} days"
    return f"PaperFetch: no matching papers in the last {days} days"


def build_failure_subject(days: int) -> str:
    return f"PaperFetch: arXiv temporarily unavailable (last {days} days)"


def build_cache_fallback_subject(days: int) -> str:
    return f"PaperFetch: arXiv temporarily unavailable, using cached results (last {days} days)"


def build_empty_report(keywords: list[str], categories: list[str], days: int) -> str:
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return "\n".join(
        [
            "# PaperFetch Search Result",
            "",
            "No matching papers were found in this run.",
            "",
            f"- Search window: last {days} days",
            f"- arXiv categories: {', '.join(categories)}",
            f"- Keywords: {', '.join(keywords)}",
            "- Matched paper count: 0",
            f"- Run time: {run_time}",
            "",
            "The program ran successfully. This is an empty match result, not a failure.",
        ]
    )


def arxiv_failure_context(error: Exception) -> tuple[str, str]:
    if isinstance(error, ArxivRateLimitCooldownError):
        return (
            "PaperFetch skipped the arXiv request because a recorded service-busy cooldown is still active.",
            "No live arXiv request was made in this run; the cooldown prevents repeated requests while arXiv recovers.",
        )
    if isinstance(error, ArxivServiceBusyError):
        return (
            "arXiv was temporarily unavailable during this run because it returned HTTP 429 or 503.",
            "This can reflect arXiv-wide service capacity, a shared outbound IP, or caller traffic; it does not by itself prove this task ran too frequently.",
        )
    return (
        "PaperFetch attempted the arXiv request, but no fresh paper results were produced.",
        "Likely causes include a non-retryable API response, network timeout, or temporary connectivity trouble.",
    )


def arxiv_response_sequence(error: Exception) -> str | None:
    sequence = getattr(error, "status_sequence", None)
    if not sequence:
        return None
    return " -> ".join(str(status) for status in sequence)


def build_failure_report(
    error: Exception,
    keywords: list[str],
    categories: list[str],
    days: int,
    max_results: int,
    cache_checked: bool = False,
    cache_hit: bool = False,
    cache_created_at: str | None = None,
) -> str:
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    request_summary, cause = arxiv_failure_context(error)
    response_sequence = arxiv_response_sequence(error) or "not available"

    cache_status = "not checked"
    if cache_checked:
        cache_status = "hit" if cache_hit else "miss"
        if cache_created_at:
            cache_status += f" (created_at: {cache_created_at})"
    return "\n".join(
        [
            "# PaperFetch Run Failed",
            "",
            request_summary,
            "",
            f"- Error type: {type(error).__name__}",
            f"- Error message: {error}",
            f"- arXiv response sequence: {response_sequence}",
            f"- Search window: last {days} days",
            f"- max_results: {min(max_results, 100)}",
            f"- arXiv categories: {', '.join(categories)}",
            f"- Keyword count: {len(keywords)}",
            f"- Cache fallback: {cache_status}",
            f"- Run time: {run_time}",
            "",
            cause,
            "",
            "Check log/run.log for the response diagnostics and confirm cron has only one PaperFetch entry. It is not an email configuration error if you received this message.",
        ]
    )


def build_cache_fallback_report(
    error: Exception,
    cached_payload: dict[str, Any],
    keywords: list[str],
    categories: list[str],
    days: int,
) -> str:
    cached_created_at = cached_payload.get("created_at", "unknown")
    papers = cached_payload.get("papers", [])
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    request_summary, _ = arxiv_failure_context(error)
    response_sequence = arxiv_response_sequence(error) or "not available"
    if papers:
        cached_body = generate_markdown(papers, keywords, categories, days)
    else:
        cached_body = "\n".join(
            [
                "# Cached PaperFetch Search Result",
                "",
                "The cached successful run had no matching papers.",
                "",
                f"- Search window: last {days} days",
                f"- arXiv categories: {', '.join(categories)}",
                f"- Keywords: {', '.join(keywords)}",
                "- Cached matched paper count: 0",
            ]
        )
    header = "\n".join(
        [
            "# PaperFetch Cached Digest",
            "",
            f"{request_summary} This email uses the latest successful cached results.",
            "",
            f"- Error type: {type(error).__name__}",
            f"- Error message: {error}",
            f"- arXiv response sequence: {response_sequence}",
            f"- Cache created_at: {cached_created_at}",
            f"- Search window: last {days} days",
            f"- arXiv categories: {', '.join(categories)}",
            f"- Keyword count: {len(keywords)}",
            f"- Cached paper count: {len(papers)}",
            f"- Run time: {run_time}",
            "",
            "These results are not from a fresh arXiv request today.",
            "",
        ]
    )
    return f"{header}\n{cached_body}"


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

    should_send_email = not args.dry_run and not args.no_email
    cache_fallback_enabled = not args.no_cache_fallback

    try:
        papers = fetch_arxiv_papers(
            keywords=keywords,
            categories=categories,
            days=args.days,
            max_results=args.max_results,
            no_cache=args.no_cache,
        )
    except Exception as exc:
        LOGGER.exception("Failed to fetch arXiv papers")

        cached_payload = None
        if cache_fallback_enabled:
            cached_payload = load_latest_cache(
                keywords=keywords,
                categories=categories,
                days=args.days,
                max_results=args.max_results,
                max_age_days=args.cache_fallback_max_age_days,
            )

        if cached_payload is not None:
            LOGGER.warning(
                "using latest successful cache after arXiv failure created_at=%s path=%s",
                cached_payload.get("created_at", "unknown"),
                cached_payload.get("path", "unknown"),
            )
            subject = build_cache_fallback_subject(args.days)
            report = build_cache_fallback_report(exc, cached_payload, keywords, categories, args.days)
            if should_send_email:
                try:
                    send_email(subject, report)
                    LOGGER.info("cache fallback email sent to configured receiver")
                except Exception:
                    LOGGER.exception("Failed to send cache fallback email")
                    return 1
            else:
                LOGGER.info("cache fallback email not sent because dry_run=%s no_email=%s", args.dry_run, args.no_email)
                LOGGER.debug("generated cache fallback report:\n%s", report)
            return 0

        LOGGER.warning("no eligible latest cache available after arXiv failure")
        subject = build_failure_subject(args.days)
        report = build_failure_report(
            exc,
            keywords,
            categories,
            args.days,
            args.max_results,
            cache_checked=cache_fallback_enabled,
            cache_hit=False,
        )
        if should_send_email:
            try:
                send_email(subject, report)
                LOGGER.info("failure email sent to configured receiver")
            except Exception:
                LOGGER.exception("Failed to send failure notification email")
        else:
            LOGGER.info("failure email not sent because dry_run=%s no_email=%s", args.dry_run, args.no_email)
            LOGGER.debug("generated failure report:\n%s", report)
        return 1

    LOGGER.info("final paper count=%s", len(papers))
    try:
        write_latest_cache(keywords, categories, args.days, args.max_results, papers)
    except Exception:
        LOGGER.exception("Failed to write latest arXiv cache")

    if not papers:
        LOGGER.info("No matched papers found, sending empty report email.")

    report = generate_markdown(papers, keywords, categories, args.days)
    subject = build_email_subject(papers, args.days)
    LOGGER.info("send_email=%s dry_run=%s no_email=%s", should_send_email, args.dry_run, args.no_email)

    if should_send_email:
        send_email(subject, report)
    else:
        LOGGER.info("email not sent")
        LOGGER.debug("generated report:\n%s", report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
