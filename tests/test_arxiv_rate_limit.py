import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import requests
from requests import HTTPError, Timeout

import PaperFrech_daily_keyword as paperfetch


def response(status_code: int, content: bytes = b"", retry_after: str | None = None) -> requests.Response:
    result = requests.Response()
    result.status_code = status_code
    result._content = content
    result.url = "https://export.arxiv.org/api/query"
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


def http_error(retry_after: str | None = None) -> HTTPError:
    return HTTPError(response=response(429, retry_after=retry_after))


class FixedDateTime(datetime):
    current = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


class RetryAfterTests(unittest.TestCase):
    def test_numeric_retry_after(self):
        self.assertEqual(paperfetch.retry_after_seconds(http_error("120")), 120.0)

    def test_http_date_retry_after(self):
        retry_at = FixedDateTime.current + timedelta(seconds=90)
        with patch.object(paperfetch, "datetime", FixedDateTime):
            delay = paperfetch.retry_after_seconds(http_error(format_datetime(retry_at)))
        self.assertEqual(delay, 90.0)


class RateLimitedFetchTests(unittest.TestCase):
    def fetch_with(self, side_effect, max_retries=5):
        with (
            patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
            patch.object(paperfetch.requests, "get", side_effect=side_effect) as get,
            patch.object(paperfetch.time, "sleep") as sleep,
            patch.object(paperfetch.random, "uniform", return_value=0.0),
        ):
            result = paperfetch.rate_limited_fetch("https://example.test", max_retries=max_retries)
        return result, get, sleep

    def test_429_without_header_waits_five_minutes_then_succeeds(self):
        result, get, sleep = self.fetch_with([response(429), response(200, b"ok")])
        self.assertEqual(result, b"ok")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(300.0)

    def test_429_uses_retry_after_header_then_succeeds(self):
        result, get, sleep = self.fetch_with(
            [response(429, retry_after="120"), response(200, b"ok")]
        )
        self.assertEqual(result, b"ok")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(120.0)

    def test_two_429_responses_stop_after_one_retry(self):
        with (
            patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
            patch.object(paperfetch.requests, "get", side_effect=[response(429), response(429)]) as get,
            patch.object(paperfetch.time, "sleep") as sleep,
        ):
            with self.assertRaises(paperfetch.ArxivRateLimitError):
                paperfetch.rate_limited_fetch("https://example.test")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(300.0)

    def test_retry_after_over_five_minutes_does_not_sleep_or_retry(self):
        with (
            patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
            patch.object(paperfetch.requests, "get", return_value=response(429, retry_after="301")) as get,
            patch.object(paperfetch.time, "sleep") as sleep,
        ):
            with self.assertRaises(paperfetch.ArxivRateLimitError):
                paperfetch.rate_limited_fetch("https://example.test")
        get.assert_called_once()
        sleep.assert_not_called()

    def test_timeout_uses_short_normal_backoff(self):
        result, get, sleep = self.fetch_with([Timeout("timed out"), response(200, b"ok")])
        self.assertEqual(result, b"ok")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(5.0)

    def test_connection_error_uses_short_normal_backoff(self):
        result, get, sleep = self.fetch_with(
            [requests.ConnectionError("connection failed"), response(200, b"ok")]
        )
        self.assertEqual(result, b"ok")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(5.0)

    def test_non_429_http_error_uses_short_normal_backoff(self):
        result, get, sleep = self.fetch_with([response(503), response(200, b"ok")])
        self.assertEqual(result, b"ok")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(5.0)


class EmptyCacheTests(unittest.TestCase):
    def test_empty_daily_cache_is_a_cache_hit_without_network_access(self):
        keywords = ["no matching papers"]
        categories = ["cs.AI"]

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(paperfetch, "CACHE_DIR", Path(directory)):
                _, raw_query = paperfetch.build_arxiv_url(categories, keywords, days=7, max_results=20)
                path = paperfetch.cache_path(raw_query)
                paperfetch.write_cache(path, raw_query, [])

                with patch.object(paperfetch, "rate_limited_fetch") as fetch:
                    papers = paperfetch.fetch_arxiv_papers(
                        keywords=keywords,
                        categories=categories,
                        days=7,
                        max_results=20,
                        no_cache=False,
                    )

        self.assertEqual(papers, [])
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
