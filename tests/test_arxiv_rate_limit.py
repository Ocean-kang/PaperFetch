import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
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


class RequestConstructionTests(unittest.TestCase):
    def test_builds_post_parameters_without_a_long_query_url(self):
        endpoint, params, raw_query = paperfetch.build_arxiv_request(
            ["cs.CV", "cs.AI"],
            ["vision-language alignment", "multimodal alignment"],
            days=7,
            max_results=250,
        )

        self.assertEqual(endpoint, paperfetch.ARXIV_API_URL)
        self.assertNotIn("search_query=", endpoint)
        self.assertEqual(params["search_query"], raw_query)
        self.assertEqual(params["start"], 0)
        self.assertEqual(params["max_results"], 100)
        self.assertEqual(params["sortBy"], "submittedDate")
        self.assertEqual(params["sortOrder"], "descending")


class RateLimitedFetchTests(unittest.TestCase):
    params = {
        "search_query": "all:test",
        "start": 0,
        "max_results": 1,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }

    def fetch_with(self, side_effect, max_retries=5):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "datetime", FixedDateTime),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "post", side_effect=side_effect) as post,
                patch.object(paperfetch.time, "sleep") as sleep,
                patch.object(paperfetch.random, "uniform", return_value=0.0),
            ):
                result = paperfetch.rate_limited_fetch(
                    paperfetch.ARXIV_API_URL,
                    self.params,
                    max_retries=max_retries,
                )
        return result, post, sleep

    def test_success_uses_post_parameters_and_user_agent(self):
        result, post, sleep = self.fetch_with([response(200, b"ok")])

        self.assertEqual(result, b"ok")
        post.assert_called_once_with(
            paperfetch.ARXIV_API_URL,
            data=self.params,
            headers={"User-Agent": paperfetch.USER_AGENT},
            timeout=paperfetch.REQUEST_TIMEOUT,
        )
        sleep.assert_not_called()

    def test_429_without_header_stops_and_blocks_same_utc_day(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "datetime", FixedDateTime),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "post", return_value=response(429)) as post,
                patch.object(paperfetch.time, "sleep") as sleep,
            ):
                with self.assertRaises(paperfetch.ArxivRateLimitError):
                    paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

                state = paperfetch.load_arxiv_rate_limit_state()
                self.assertEqual(state["wait_source"], "utc-day")
                self.assertNotIn("retry_after_until", state)
                post.assert_called_once()
                sleep.assert_not_called()

                with patch.object(paperfetch.requests, "post") as blocked_post:
                    with self.assertRaises(paperfetch.ArxivRateLimitCooldownError):
                        paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)
                blocked_post.assert_not_called()

    def test_429_retry_after_persists_exact_deadline_without_sleep(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "datetime", FixedDateTime),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(
                    paperfetch.requests,
                    "post",
                    return_value=response(429, retry_after="120"),
                ) as post,
                patch.object(paperfetch.time, "sleep") as sleep,
            ):
                with self.assertRaises(paperfetch.ArxivRateLimitError):
                    paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

                state = paperfetch.load_arxiv_rate_limit_state()
                expected = FixedDateTime.current + timedelta(seconds=120)
                self.assertEqual(
                    paperfetch.parse_cache_datetime(state["retry_after_until"]),
                    expected,
                )
                self.assertIsNotNone(
                    paperfetch.active_arxiv_rate_limit_cooldown(
                        state,
                        now=FixedDateTime.current + timedelta(seconds=119),
                    )
                )
                self.assertIsNone(
                    paperfetch.active_arxiv_rate_limit_cooldown(
                        state,
                        now=FixedDateTime.current + timedelta(seconds=121),
                    )
                )
                post.assert_called_once()
                sleep.assert_not_called()

    def test_success_clears_expired_rate_limit_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "arxiv_rate_limit_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "last_429_at": FixedDateTime.current.isoformat(),
                        "retry_after_until": (
                            FixedDateTime.current - timedelta(seconds=1)
                        ).isoformat(),
                        "wait_source": "Retry-After",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "datetime", FixedDateTime),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "post", return_value=response(200, b"ok")),
            ):
                result = paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

            self.assertEqual(result, b"ok")
            self.assertFalse(state_path.exists())

    def test_invalid_state_is_ignored_and_cleared_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "arxiv_rate_limit_state.json"
            state_path.write_text("{", encoding="utf-8")
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "post", return_value=response(200, b"ok")),
            ):
                result = paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

            self.assertEqual(result, b"ok")
            self.assertFalse(state_path.exists())

    def test_non_429_client_error_fails_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "post", return_value=response(400)) as post,
                patch.object(paperfetch.time, "sleep") as sleep,
            ):
                with self.assertRaises(paperfetch.ArxivFetchError):
                    paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

        post.assert_called_once()
        sleep.assert_not_called()

    def test_server_error_uses_short_normal_backoff(self):
        result, post, sleep = self.fetch_with([response(503), response(200, b"ok")])
        self.assertEqual(result, b"ok")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5.0)

    def test_timeout_uses_short_normal_backoff(self):
        result, post, sleep = self.fetch_with([Timeout("timed out"), response(200, b"ok")])
        self.assertEqual(result, b"ok")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5.0)

    def test_connection_error_uses_short_normal_backoff(self):
        result, post, sleep = self.fetch_with(
            [requests.ConnectionError("connection failed"), response(200, b"ok")]
        )
        self.assertEqual(result, b"ok")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5.0)


class EmptyCacheTests(unittest.TestCase):
    def test_empty_daily_cache_is_a_cache_hit_without_network_access(self):
        keywords = ["no matching papers"]
        categories = ["cs.AI"]

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(paperfetch, "CACHE_DIR", Path(directory)):
                _, _, raw_query = paperfetch.build_arxiv_request(
                    categories,
                    keywords,
                    days=7,
                    max_results=20,
                )
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


class CacheFallbackMainTests(unittest.TestCase):
    @staticmethod
    def args():
        return SimpleNamespace(
            days=7,
            max_results=100,
            dry_run=False,
            no_email=False,
            no_cache=False,
            no_cache_fallback=False,
            cache_fallback_max_age_days=7,
            log_level="INFO",
        )

    def test_429_with_latest_cache_sends_cached_digest_and_returns_success(self):
        cached_payload = {
            "created_at": "2026-08-11T01:00:00+00:00",
            "papers": [],
            "path": "cache/arxiv_latest_test.json",
        }
        with (
            patch.object(paperfetch, "parse_args", return_value=self.args()),
            patch.object(paperfetch, "configure_logging"),
            patch.object(
                paperfetch,
                "fetch_arxiv_papers",
                side_effect=paperfetch.ArxivRateLimitError("live HTTP 429"),
            ),
            patch.object(paperfetch, "load_latest_cache", return_value=cached_payload),
            patch.object(paperfetch, "send_email") as send_email,
        ):
            result = paperfetch.main()

        self.assertEqual(result, 0)
        send_email.assert_called_once()
        subject, body = send_email.call_args.args
        self.assertIn("using cached results", subject)
        self.assertIn("arXiv rejected this run's request with HTTP 429", body)

    def test_429_without_latest_cache_sends_failure_and_returns_error(self):
        with (
            patch.object(paperfetch, "parse_args", return_value=self.args()),
            patch.object(paperfetch, "configure_logging"),
            patch.object(
                paperfetch,
                "fetch_arxiv_papers",
                side_effect=paperfetch.ArxivRateLimitError("live HTTP 429"),
            ),
            patch.object(paperfetch, "load_latest_cache", return_value=None),
            patch.object(paperfetch, "send_email") as send_email,
        ):
            result = paperfetch.main()

        self.assertEqual(result, 1)
        send_email.assert_called_once()
        subject, body = send_email.call_args.args
        self.assertIn("arXiv request failed", subject)
        self.assertIn("arXiv rejected this run's request with HTTP 429", body)

    def test_cooldown_report_says_no_live_request_was_made(self):
        report = paperfetch.build_cache_fallback_report(
            paperfetch.ArxivRateLimitCooldownError("cooldown"),
            {
                "created_at": "2026-08-11T01:00:00+00:00",
                "papers": [],
            },
            ["keyword"],
            ["cs.AI"],
            7,
        )
        self.assertIn("skipped the arXiv request", report)
        self.assertNotIn("could not reach arXiv", report)


if __name__ == "__main__":
    unittest.main()
