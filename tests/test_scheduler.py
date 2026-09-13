import json
import smtplib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
import PaperFrech_daily_keyword as pf
import paperfetch_scheduler as scheduler


class Clock(datetime):
    current = datetime(2026, 9, 13, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


def response(status, retry=None):
    r = requests.Response()
    r.status_code = status
    r._content = b"Rate exceeded."
    r.url = pf.ARXIV_API_URL
    if retry is not None:
        r.headers["Retry-After"] = retry
    return r


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        Clock.current = datetime(2026, 9, 13, 1, tzinfo=timezone.utc)
        self.args = SimpleNamespace(days=7, max_results=100, no_cache=False,
                                    no_cache_fallback=False, cache_fallback_max_age_days=7,
                                    dry_run=False, no_email=False)
        self.keywords, self.categories = ["alignment"], ["cs.CV"]
        for target, name, value in ((pf, "CACHE_DIR", self.directory), (pf, "datetime", Clock),
                                    (scheduler, "now", lambda: Clock.current)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.real_fetch = pf.fetch_arxiv_papers
        self.fetch = self.start_patch("fetch_arxiv_papers", return_value=[])
        self.send = self.start_patch("send_email")

    def start_patch(self, name, **kwargs):
        p = patch.object(pf, name, **kwargs)
        result = p.start()
        self.addCleanup(p.stop)
        return result

    def run_tick(self):
        return scheduler.run_scheduled(self.args, self.keywords, self.categories)

    def state(self):
        return scheduler.load_state(scheduler.state_path(
            self.args, self.keywords, self.categories, Clock.current))

    def advance(self, minutes):
        Clock.current += timedelta(minutes=minutes)

    def test_failure_once_then_recovery_once_and_no_more_requests(self):
        self.fetch.side_effect = [pf.ArxivRateLimitError("busy", [429]), []]
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.state()["status"], "pending")
        self.assertEqual(self.send.call_count, 1)
        self.advance(15)
        self.run_tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.advance(15)
        self.run_tick()
        self.assertEqual(self.state()["status"], "success")
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("recovered", self.send.call_args.args[0])
        self.advance(30)
        self.run_tick()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.send.call_count, 2)

    def test_backoff_and_eight_attempt_cap(self):
        self.fetch.side_effect = pf.ArxivServiceBusyError("busy", [503])
        for index, minutes in enumerate((30, 60, 120, 120, 120, 120, 120)):
            self.run_tick()
            self.assertEqual(self.state()["attempts"], index + 1)
            self.assertEqual(pf.parse_cache_datetime(self.state()["next_attempt"]),
                             Clock.current + timedelta(minutes=minutes))
            self.advance(minutes)
        # Eight attempts fit in the window when responses are instantaneous.
        self.run_tick()
        self.assertEqual(self.fetch.call_count, 8)
        self.assertEqual(self.send.call_count, 1)
        Clock.current = Clock.current.replace(hour=12, minute=0)
        path = scheduler.state_path(self.args, self.keywords, self.categories, Clock.current)
        state = self.state()
        state["attempts"], state["next_attempt"] = 8, None
        scheduler.atomic_write(path, state)
        self.run_tick()
        self.assertEqual(self.fetch.call_count, 8)

    def test_permanent_client_error_stops(self):
        self.fetch.side_effect = pf.ArxivPermanentError("400")
        self.run_tick()
        self.advance(120)
        self.run_tick()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.state()["status"], "permanent")

    def test_safe_smtp_failure_retries_only_delivery(self):
        self.send.side_effect = [smtplib.SMTPDataError(451, b"try later"), None]
        self.assertEqual(self.run_tick(), 1)
        self.assertEqual(self.state()["messages"]["fresh"]["status"], "pending")
        self.advance(30)
        self.assertEqual(self.run_tick(), 0)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.send.call_count, 2)

    def test_uncertain_smtp_failure_not_automatically_resent(self):
        self.send.side_effect = smtplib.SMTPServerDisconnected("acceptance unknown")
        self.assertEqual(self.run_tick(), 1)
        self.advance(30)
        self.assertEqual(self.run_tick(), 1)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.state()["messages"]["fresh"]["status"], "uncertain")

    def test_crash_during_send_is_not_resent(self):
        self.send.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_tick()
        self.assertEqual(self.state()["messages"]["fresh"]["status"], "sending")
        self.send.side_effect = None
        self.assertEqual(self.run_tick(), 1)
        self.assertEqual(self.send.call_count, 1)

    def test_corrupted_state_fails_closed(self):
        path = scheduler.state_path(self.args, self.keywords, self.categories, Clock.current)
        for bad in ("{", "{}", '[]'):
            path.write_text(bad, encoding="utf-8")
            self.assertEqual(self.run_tick(), 1)
        self.fetch.assert_not_called()
        self.send.assert_not_called()

    def test_preview_does_not_mutate_or_contact_services(self):
        for option in ("dry_run", "no_email"):
            setattr(self.args, option, True)
            self.assertEqual(self.run_tick(), 0)
            setattr(self.args, option, False)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.fetch.assert_not_called()
        self.send.assert_not_called()

    def test_concurrent_lock_skips(self):
        with scheduler.scheduler_lock() as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.run_tick(), 0)
        self.fetch.assert_not_called()

    def test_cross_day_server_cooldown_preserved(self):
        deadline = Clock.current + timedelta(days=1, hours=2)
        scheduler.atomic_write(pf.arxiv_rate_limit_state_path(), {
            "retry_after_until": deadline.isoformat(), "wait_source": "Retry-After"})
        self.run_tick()
        self.assertEqual(self.state()["attempts"], 0)
        self.advance(24 * 60)
        self.run_tick()
        self.assertEqual(self.state()["attempts"], 0)
        self.fetch.assert_not_called()
        self.advance(120)
        self.run_tick()
        self.fetch.assert_called_once()

    def test_time_window_and_daily_reset(self):
        Clock.current = Clock.current.replace(hour=0, minute=30)
        self.run_tick()
        self.fetch.assert_not_called()
        self.advance(30)
        self.run_tick()
        self.advance(24 * 60)
        self.run_tick()
        self.assertEqual(self.fetch.call_count, 2)
        Clock.current = Clock.current.replace(hour=13, minute=30)
        self.run_tick()
        self.assertEqual(self.fetch.call_count, 2)

    def test_cached_failure_keeps_original_timestamp(self):
        cached_time = (Clock.current - timedelta(days=2)).isoformat()
        cache = pf.latest_cache_path(self.keywords, self.categories, 7, 100)
        scheduler.atomic_write(cache, {"created_at": cached_time, "papers": []})
        original = cache.read_bytes()
        self.fetch.side_effect = pf.ArxivRateLimitError("busy", [429])
        self.run_tick()
        self.assertIn("cached results", self.send.call_args.args[0])
        self.assertIn("48.0 hours", self.send.call_args.args[1])
        self.assertEqual(cache.read_bytes(), original)
        self.assertIsNone(self.state()["fetched_at"])

    def test_expired_cache_not_sent(self):
        cache = pf.latest_cache_path(self.keywords, self.categories, 7, 100)
        scheduler.atomic_write(cache, {"created_at": (Clock.current - timedelta(days=8)).isoformat(),
                                       "papers": []})
        self.fetch.side_effect = pf.ArxivRateLimitError("busy", [429])
        self.run_tick()
        self.assertNotIn("cached results", self.send.call_args.args[0])
        self.assertIn("Run Failed", self.send.call_args.args[1])

    def test_real_http_capacity_path_defers_without_sleep(self):
        # Exercise the HTTP layer, independent of the mocked scheduler fetch.
        for status, retry in ((429, None), (503, "7200"),
                              (429, format_datetime(Clock.current + timedelta(hours=3)))):
            pf.clear_arxiv_rate_limit_state()
            with patch.object(pf.requests, "get", return_value=response(status, retry)) as get, \
                    patch.object(pf, "wait_for_arxiv_rate_limit"), patch.object(pf.time, "sleep") as sleep:
                with self.assertRaises(pf.ArxivServiceBusyError):
                    pf.rate_limited_fetch(pf.ARXIV_API_URL, {}, deferred_backoff=1800)
            get.assert_called_once()
            sleep.assert_not_called()
            deadline = pf.parse_cache_datetime(pf.load_arxiv_rate_limit_state()["retry_after_until"])
            self.assertGreaterEqual(deadline, Clock.current + timedelta(minutes=30))

    def test_http_to_valid_empty_feed_recovery_integration(self):
        # Remove the fetch stub so request, feed parsing, cache and scheduler all run.
        good = response(200)
        good._content = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>arXiv</title></feed>'
        with patch.object(pf, "fetch_arxiv_papers", self.real_fetch), \
                patch.object(pf.requests, "get", side_effect=[response(429), good]) as get, \
                patch.object(pf, "wait_for_arxiv_rate_limit"), patch.object(pf.time, "sleep") as sleep:
            self.run_tick()
            self.advance(30)
            self.run_tick()
        self.assertEqual(get.call_count, 2)
        sleep.assert_not_called()
        self.assertEqual(self.state()["status"], "success")
        self.assertEqual(self.send.call_count, 2)
        self.assertIsNotNone(pf.load_latest_cache(self.keywords, self.categories, 7, 100, 7))


class CacheAndBatchTests(unittest.TestCase):
    def test_http_200_non_feed_does_not_become_zero_match_success(self):
        with self.assertRaises(pf.ArxivFetchError):
            pf.parse_feed(b"<html><body>Service temporarily unavailable</body></html>")

    def test_api_error_feed_is_permanent_failure(self):
        with self.assertRaises(pf.ArxivPermanentError):
            pf.parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                          b'<id>http://arxiv.org/api/errors#incorrect_id_format</id>'
                          b'<summary>Invalid query</summary></entry></feed>')

    def test_daily_cache_does_not_become_new_latest_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(pf, "CACHE_DIR", Path(directory)):
            _, _, query = pf.build_arxiv_request(["cs.CV"], ["alignment"], 7, 100)
            timestamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            scheduler.atomic_write(pf.cache_path(query), {"created_at": timestamp, "papers": []})
            with patch.object(pf.requests, "get") as get:
                papers = pf.fetch_arxiv_papers(["alignment"], ["cs.CV"], 7, 100, False)
            pf.write_latest_cache(["alignment"], ["cs.CV"], 7, 100, papers)
            value = json.loads(pf.latest_cache_path(["alignment"], ["cs.CV"], 7, 100).read_text())
            self.assertEqual(value["created_at"], timestamp)
            get.assert_not_called()

    def test_partial_batch_is_failure(self):
        with patch.object(pf, "KEYWORD_BATCH_SIZE", 1), \
                patch.object(pf, "rate_limited_fetch", side_effect=[b"ok", pf.ArxivFetchError("timeout")]), \
                patch.object(pf, "parse_feed"), patch.object(pf, "papers_from_feed", return_value=[]), \
                patch.object(pf.time, "sleep"):
            with self.assertRaisesRegex(pf.ArxivFetchError, "Incomplete"):
                pf.fetch_arxiv_papers(["a", "b"], ["cs.CV"], 7, 100, True)


if __name__ == "__main__":
    unittest.main()
