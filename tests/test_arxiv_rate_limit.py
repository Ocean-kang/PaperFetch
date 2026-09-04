import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from lxml import html as lxml_html
from requests import HTTPError, Timeout

import PaperFrech_daily_keyword as paperfetch


def response(
    status_code: int,
    content: bytes = b"",
    retry_after: str | None = None,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    result = requests.Response()
    result.status_code = status_code
    result._content = content
    result.url = "https://export.arxiv.org/api/query"
    result.headers.update(headers or {})
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
    def test_builds_get_parameters_without_embedding_query_in_endpoint(self):
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

    def fetch_with(self, side_effect, max_retries=3):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "datetime", FixedDateTime),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "get", side_effect=side_effect) as get,
                patch.object(paperfetch.time, "sleep") as sleep,
            ):
                result = paperfetch.rate_limited_fetch(
                    paperfetch.ARXIV_API_URL,
                    self.params,
                    max_retries=max_retries,
                )
        return result, get, sleep

    def test_success_uses_get_parameters_and_user_agent(self):
        result, get, sleep = self.fetch_with([response(200, b"ok")])

        self.assertEqual(result, b"ok")
        get.assert_called_once_with(
            paperfetch.ARXIV_API_URL,
            params=self.params,
            headers={"User-Agent": paperfetch.USER_AGENT},
            timeout=paperfetch.REQUEST_TIMEOUT,
        )
        sleep.assert_not_called()

    def test_503_waits_five_minutes_once_then_succeeds(self):
        result, get, sleep = self.fetch_with([response(503), response(200, b"ok")])

        self.assertEqual(result, b"ok")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(300.0)

    def test_short_retry_after_controls_the_only_capacity_retry(self):
        result, get, sleep = self.fetch_with(
            [response(429, retry_after="120"), response(200, b"ok")]
        )

        self.assertEqual(result, b"ok")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(120.0)

    def test_503_then_429_stops_and_records_thirty_minute_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "datetime", FixedDateTime),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(
                    paperfetch.requests,
                    "get",
                    side_effect=[response(503, b"Service Unavailable"), response(429, b"Rate exceeded.")],
                ) as get,
                patch.object(paperfetch.time, "sleep") as sleep,
            ):
                with self.assertRaises(paperfetch.ArxivRateLimitError) as raised:
                    paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

                state = paperfetch.load_arxiv_rate_limit_state()
                self.assertEqual(raised.exception.status_sequence, [503, 429])
                self.assertEqual(state["last_status"], 429)
                self.assertEqual(state["last_failure_at"], FixedDateTime.current.isoformat())
                self.assertEqual(state["response_hint"], "Rate exceeded.")
                self.assertEqual(
                    paperfetch.parse_cache_datetime(state["retry_after_until"]),
                    FixedDateTime.current + timedelta(minutes=30),
                )
                self.assertEqual(get.call_count, 2)
                sleep.assert_called_once_with(300.0)

    def test_429_retries_once_then_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "datetime", FixedDateTime),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "get", return_value=response(429)) as get,
                patch.object(paperfetch.time, "sleep") as sleep,
            ):
                with self.assertRaises(paperfetch.ArxivRateLimitError) as raised:
                    paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

            self.assertEqual(raised.exception.status_sequence, [429, 429])
            self.assertEqual(get.call_count, 2)
            sleep.assert_called_once_with(300.0)

    def test_long_retry_after_persists_exact_deadline_without_sleep(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "datetime", FixedDateTime),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(
                    paperfetch.requests,
                    "get",
                    return_value=response(429, retry_after="600"),
                ) as get,
                patch.object(paperfetch.time, "sleep") as sleep,
            ):
                with self.assertRaises(paperfetch.ArxivRateLimitError):
                    paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

                state = paperfetch.load_arxiv_rate_limit_state()
                expected = FixedDateTime.current + timedelta(seconds=600)
                self.assertEqual(
                    paperfetch.parse_cache_datetime(state["retry_after_until"]),
                    expected,
                )
                self.assertIsNotNone(
                    paperfetch.active_arxiv_rate_limit_cooldown(
                        state,
                        now=FixedDateTime.current + timedelta(seconds=599),
                    )
                )
                self.assertIsNone(
                    paperfetch.active_arxiv_rate_limit_cooldown(
                        state,
                        now=FixedDateTime.current + timedelta(seconds=601),
                    )
                )
                get.assert_called_once()
                sleep.assert_not_called()

    def test_active_cooldown_blocks_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "arxiv_rate_limit_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "last_status": 503,
                        "last_failure_at": FixedDateTime.current.isoformat(),
                        "retry_after_until": (
                            FixedDateTime.current + timedelta(minutes=10)
                        ).isoformat(),
                        "response_hint": "busy",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "datetime", FixedDateTime),
                patch.object(paperfetch.requests, "get") as get,
            ):
                with self.assertRaises(paperfetch.ArxivRateLimitCooldownError):
                    paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

            get.assert_not_called()

    def test_legacy_utc_day_state_expires_after_thirty_minutes(self):
        state = {"last_429_at": FixedDateTime.current.isoformat(), "wait_source": "utc-day"}

        self.assertIsNotNone(
            paperfetch.active_arxiv_rate_limit_cooldown(
                state,
                now=FixedDateTime.current + timedelta(minutes=29),
            )
        )
        self.assertIsNone(
            paperfetch.active_arxiv_rate_limit_cooldown(
                state,
                now=FixedDateTime.current + timedelta(minutes=31),
            )
        )

    def test_success_clears_expired_rate_limit_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "arxiv_rate_limit_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "last_429_at": (
                            FixedDateTime.current - timedelta(minutes=31)
                        ).isoformat(),
                        "wait_source": "utc-day",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "datetime", FixedDateTime),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "get", return_value=response(200, b"ok")),
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
                patch.object(paperfetch.requests, "get", return_value=response(200, b"ok")),
            ):
                result = paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

            self.assertEqual(result, b"ok")
            self.assertFalse(state_path.exists())

    def test_non_429_client_error_fails_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "get", return_value=response(400)) as get,
                patch.object(paperfetch.time, "sleep") as sleep,
            ):
                with self.assertRaises(paperfetch.ArxivFetchError):
                    paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

        get.assert_called_once()
        sleep.assert_not_called()

    def test_normal_server_errors_use_ten_and_thirty_second_backoff(self):
        result, get, sleep = self.fetch_with(
            [response(500), response(502), response(200, b"ok")]
        )
        self.assertEqual(result, b"ok")
        self.assertEqual(get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [10.0, 30.0])

    def test_normal_server_errors_stop_after_three_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "get", return_value=response(500)) as get,
                patch.object(paperfetch.time, "sleep") as sleep,
            ):
                with self.assertRaises(paperfetch.ArxivFetchError):
                    paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

        self.assertEqual(get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [10.0, 30.0])

    def test_timeout_uses_short_normal_backoff(self):
        result, get, sleep = self.fetch_with([Timeout("timed out"), response(200, b"ok")])
        self.assertEqual(result, b"ok")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(10.0)

    def test_connection_error_uses_short_normal_backoff(self):
        result, get, sleep = self.fetch_with(
            [requests.ConnectionError("connection failed"), response(200, b"ok")]
        )
        self.assertEqual(result, b"ok")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(10.0)

    def test_http_diagnostics_include_capacity_headers_and_body(self):
        failure = response(
            400,
            b"Rate exceeded.",
            headers={"X-Cache": "MISS", "Via": "1.1 varnish", "Date": "today"},
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(paperfetch, "CACHE_DIR", Path(directory)),
                patch.object(paperfetch, "wait_for_arxiv_rate_limit"),
                patch.object(paperfetch.requests, "get", return_value=failure),
                self.assertLogs("paperfetch", level="WARNING") as logs,
            ):
                with self.assertRaises(paperfetch.ArxivFetchError):
                    paperfetch.rate_limited_fetch(paperfetch.ARXIV_API_URL, self.params)

        combined = "\n".join(logs.output)
        self.assertIn("x_cache='MISS'", combined)
        self.assertIn("via='1.1 varnish'", combined)
        self.assertIn("body='Rate exceeded.'", combined)


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

    def test_service_busy_with_latest_cache_sends_cached_digest_and_returns_success(self):
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
                side_effect=paperfetch.ArxivServiceBusyError(
                    "arXiv temporarily unavailable",
                    [503, 429],
                ),
            ),
            patch.object(paperfetch, "load_latest_cache", return_value=cached_payload),
            patch.object(paperfetch, "send_email") as send_email,
        ):
            result = paperfetch.main()

        self.assertEqual(result, 0)
        send_email.assert_called_once()
        subject, body = send_email.call_args.args
        self.assertIn("using cached results", subject)
        self.assertIn("arXiv was temporarily unavailable", body)
        self.assertIn("arXiv response sequence: 503 -> 429", body)
        self.assertIn("Cache created_at: 2026-08-11T01:00:00+00:00", body)

    def test_service_busy_without_latest_cache_sends_failure_and_returns_error(self):
        with (
            patch.object(paperfetch, "parse_args", return_value=self.args()),
            patch.object(paperfetch, "configure_logging"),
            patch.object(
                paperfetch,
                "fetch_arxiv_papers",
                side_effect=paperfetch.ArxivServiceBusyError(
                    "arXiv temporarily unavailable",
                    [503, 429],
                ),
            ),
            patch.object(paperfetch, "load_latest_cache", return_value=None),
            patch.object(paperfetch, "send_email") as send_email,
        ):
            result = paperfetch.main()

        self.assertEqual(result, 1)
        send_email.assert_called_once()
        subject, body = send_email.call_args.args
        self.assertIn("arXiv temporarily unavailable", subject)
        self.assertIn("arXiv was temporarily unavailable", body)
        self.assertIn("arXiv response sequence: 503 -> 429", body)

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


class TopicDigestTests(unittest.TestCase):
    @staticmethod
    def paper(
        arxiv_id: str,
        title: str,
        summary: str,
        published: str,
        link: str | None = None,
    ):
        return {
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": 'Alice & Bob <team@example.com>',
            "summary": summary,
            "published": published,
            "category": "cs.CV, cs.AI",
            "link": link or f"https://arxiv.org/abs/{arxiv_id}",
        }

    def test_topic_configuration_preserves_all_twenty_nine_keywords_in_order(self):
        expected = [
            "open vocabulary semantic segmentation",
            "open-vocabulary semantic segmentation",
            "open vocabulary segmentation",
            "open-vocabulary segmentation",
            "vision-language alignment",
            "vision language alignment",
            "image-text alignment",
            "image text alignment",
            "cross-modal alignment",
            "cross modal alignment",
            "multimodal alignment",
            "multi-modal alignment",
            "unsupervised alignment",
            "unsupervised embedding alignment",
            "unsupervised representation alignment",
            "unsupervised cross-modal alignment",
            "unpaired alignment",
            "unpaired image-text alignment",
            "unpaired vision-language alignment",
            "unpaired multimodal alignment",
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

        self.assertEqual(paperfetch.KEYWORDS, expected)
        self.assertEqual(len(paperfetch.TOPICS), 4)

    def test_matching_topics_supports_single_multiple_and_no_match(self):
        single = self.paper("1", "An open-vocabulary segmentation method", "", "2026-09-01")
        multiple = self.paper(
            "2",
            "Open vocabulary segmentation with vision-language alignment",
            "Uses optimal transport alignment.",
            "2026-09-02",
        )
        unmatched = self.paper("3", "A general machine learning paper", "No configured terms.", "2026-09-03")

        self.assertEqual(paperfetch.matching_topics(single), ["开放词汇分割"])
        self.assertEqual(
            paperfetch.matching_topics(multiple),
            ["开放词汇分割", "视觉语言与多模态对齐", "分布、几何与表征空间对齐"],
        )
        self.assertEqual(paperfetch.matching_topics(unmatched), [])

        grouped = paperfetch.group_papers_by_topic([single, multiple, unmatched])
        self.assertEqual(len(grouped["开放词汇分割"]), 2)
        self.assertEqual(grouped["未分类"], [unmatched])

    def test_html_has_complete_summary_safe_content_and_unique_sorted_details(self):
        older = self.paper(
            "old&1",
            '<Open vocabulary segmentation & vision-language alignment>',
            '<script>alert("unsafe")</script>',
            "2026-09-01",
            "https://arxiv.org/abs/old?x=1&y=2",
        )
        newer = self.paper(
            "new-2",
            "Optimal transport alignment for model spaces",
            "A newer paper.",
            "2026-09-03",
        )

        report = paperfetch.generate_email_html(
            [older, newer],
            paperfetch.KEYWORDS,
            paperfetch.CATEGORIES,
            7,
        )
        document = lxml_html.fromstring(report)
        rows = document.xpath("//table[@id='topic-summary']/tbody/tr")
        row_by_topic = {row.get("data-topic"): row for row in rows}

        self.assertTrue(report.lower().startswith("<!doctype html>"))
        self.assertEqual(list(row_by_topic), list(paperfetch.TOPICS))
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            row_by_topic["开放词汇分割"].xpath(".//td[contains(@class, 'topic-count')]")[0].text_content().strip(),
            "1",
        )
        self.assertEqual(
            row_by_topic["视觉语言与多模态对齐"].xpath(".//td[contains(@class, 'topic-count')]")[0].text_content().strip(),
            "1",
        )
        self.assertIn("No matching papers", row_by_topic["无监督与非配对对齐"].text_content())
        self.assertEqual(report.count("&lt;script&gt;"), 1)
        self.assertNotIn("<script>alert", report)
        self.assertIn("Alice &amp; Bob &lt;team@example.com&gt;", report)

        detail_ids = document.xpath("//table[contains(@class, 'paper-detail')]/@data-arxiv-id")
        self.assertEqual(detail_ids, ["new-2", "old&1"])
        self.assertEqual(detail_ids.count("old&1"), 1)
        self.assertEqual(
            len(document.xpath("//a[@href='https://arxiv.org/abs/old?x=1&y=2']")),
            3,
        )

    def test_unclassified_summary_row_only_appears_when_needed(self):
        unmatched = self.paper("3", "A general machine learning paper", "No configured terms.", "2026-09-03")
        report = paperfetch.generate_email_html(
            [unmatched],
            paperfetch.KEYWORDS,
            paperfetch.CATEGORIES,
            7,
        )
        document = lxml_html.fromstring(report)
        topics = document.xpath("//table[@id='topic-summary']/tbody/tr/@data-topic")

        self.assertEqual(topics, [*paperfetch.TOPICS, "未分类"])

    def test_cached_papers_use_html_digest_with_warning_banner(self):
        cached_paper = self.paper(
            "cached-1",
            "Vision-language alignment from cache",
            "Cached abstract.",
            "2026-09-01",
        )
        report = paperfetch.build_cache_fallback_report(
            paperfetch.ArxivServiceBusyError("busy", [503, 429]),
            {"created_at": "2026-09-02T01:00:00+00:00", "papers": [cached_paper]},
            paperfetch.KEYWORDS,
            paperfetch.CATEGORIES,
            7,
        )

        self.assertTrue(report.lower().startswith("<!doctype html>"))
        self.assertIn("Cached results:", report)
        self.assertIn("503 -&gt; 429", report)
        self.assertIn('id="topic-summary"', report)
        self.assertIn("Vision-language alignment from cache", report)


if __name__ == "__main__":
    unittest.main()
