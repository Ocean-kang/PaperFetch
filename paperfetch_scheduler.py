"""Persistent, bounded daily recovery. All production state is protected by one lock."""
from __future__ import annotations

import json
import logging
import os
import smtplib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import PaperFrech_daily_keyword as pf

LOG = logging.getLogger("paperfetch")
BEIJING = timezone(timedelta(hours=8))


def now():
    return datetime.now(timezone.utc)


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def scheduler_lock():
    pf.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with (pf.CACHE_DIR / "scheduler.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        try:
            yield True
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def state_path(args, keywords, categories, current):
    signature = pf.latest_cache_path(keywords, categories, args.days, args.max_results).stem
    return pf.CACHE_DIR / f"schedule_{current.astimezone(BEIJING):%Y-%m-%d}_{signature}.json"


def load_state(path):
    if not path.exists():
        return {"version": 1, "attempts": 0, "status": "pending", "next_attempt": None,
                "fetched_at": None, "messages": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("version") != 1
            or type(value.get("attempts")) is not int or not 0 <= value["attempts"] <= 8
            or value.get("status") not in ("pending", "success", "permanent")
            or not isinstance(value.get("messages"), dict)
            or "next_attempt" not in value or "fetched_at" not in value):
        raise ValueError(f"Invalid scheduler state: {path}; inspect it before retrying")
    for field in ("next_attempt", "fetched_at"):
        if value[field] is not None and pf.parse_cache_datetime(value[field]) is None:
            raise ValueError(f"Invalid {field} in {path}")
    for kind, message in value["messages"].items():
        if (kind not in ("initial", "fresh") or not isinstance(message, dict)
                or message.get("status") not in ("pending", "sending", "sent", "uncertain")
                or not isinstance(message.get("subject"), str)
                or not isinstance(message.get("body"), str)):
            raise ValueError(f"Invalid message in {path}")
    if value["status"] == "success" and (not value["fetched_at"] or "fresh" not in value["messages"]):
        raise ValueError(f"Incomplete successful state: {path}")
    return value


def deliver(path, state):
    ok = True
    for message in state["messages"].values():
        if message["status"] in ("sending", "uncertain"):
            message["status"] = "uncertain"
            atomic_write(path, state)
            LOG.error("delivery_uncertain: inspect SMTP/mailbox and scheduler state %s", path)
            ok = False
            continue
        if message["status"] == "sent":
            continue
        message["status"] = "sending"
        atomic_write(path, state)
        try:
            pf.send_email(message["subject"], message["body"])
        except Exception as exc:
            # Explicit SMTP rejection/configuration failure is safe to retry. A dropped
            # connection may occur after acceptance: never silently send it again.
            safe = isinstance(exc, (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused,
                                    FileNotFoundError, ConnectionRefusedError))
            message["status"] = "pending" if safe else "uncertain"
            LOG.exception("delivery_failed retry_safe=%s", safe)
            ok = False
        else:
            message["status"] = "sent"
            LOG.info("delivery_sent")
        atomic_write(path, state)
    return ok


def queued(subject, body):
    return {"status": "pending", "subject": subject, "body": body}


def run_scheduled(args, keywords, categories):
    if args.days < 1 or args.max_results < 1:
        LOG.error("days and max-results must be positive")
        return 1
    if args.dry_run or args.no_email:
        LOG.info("scheduled_preview: no network, email or production state changes")
        try:
            path = state_path(args, keywords, categories, now())
            state = load_state(path)
            LOG.info("scheduler status=%s attempts=%s next_attempt=%s fetched_at=%s messages=%s",
                     state["status"], state["attempts"], state["next_attempt"], state["fetched_at"],
                     {kind: message["status"] for kind, message in state["messages"].items()})
            return 0
        except (OSError, ValueError):
            LOG.exception("invalid scheduler state")
            return 1
    try:
        with scheduler_lock() as acquired:
            if not acquired:
                LOG.info("scheduler_locked: skip")
                return 0
            return tick(args, keywords, categories)
    except (OSError, ValueError):
        LOG.exception("scheduler_state_error: inspect state before retrying")
        return 1


def tick(args, keywords, categories):
    current = now()
    local = current.astimezone(BEIJING)
    if local.hour < 9 or local.hour > 21 or (local.hour == 21 and local.minute > 0):
        LOG.info("outside_daily_window")
        return 0
    path = state_path(args, keywords, categories, current)
    state = load_state(path)
    # Pending SMTP work gets its own wakeup, without another arXiv request.
    if any(m["status"] == "pending" for m in state["messages"].values()):
        return 0 if deliver(path, state) else 1
    if state["status"] != "pending" or state["attempts"] >= 8:
        LOG.info("daily_fetch_stopped status=%s attempts=%s", state["status"], state["attempts"])
        return 0 if deliver(path, state) else 1
    deadline = pf.parse_cache_datetime(state["next_attempt"])
    if deadline and current < deadline:
        LOG.info("waiting_recovery next_attempt_beijing=%s", deadline.astimezone(BEIJING).isoformat())
        return 0
    backoff = 1800 * 2 ** min(state["attempts"], 2)
    try:
        # Enforce cross-day server deadlines before consuming a daily attempt.
        pf.enforce_arxiv_rate_limit_cooldown()
        state["attempts"] += 1
        state["next_attempt"] = (current + timedelta(seconds=backoff)).isoformat()
        atomic_write(path, state)  # Reserve attempt before a crash/network operation.
        papers = pf.fetch_arxiv_papers(keywords, categories, args.days, args.max_results,
                                      no_cache=True, deferred_backoff=backoff)
    except Exception as exc:
        if isinstance(exc, pf.ArxivPermanentError):
            state["status"] = "permanent"
        cooldown = pf.load_arxiv_rate_limit_state() or {}
        server_deadline = pf.parse_cache_datetime(cooldown.get("retry_after_until"))
        deadline = max(now() + timedelta(seconds=backoff), server_deadline or now())
        state["next_attempt"] = deadline.isoformat()
        LOG.warning("fetch_degraded attempts=%s next_attempt_beijing=%s error=%s",
                    state["attempts"], deadline.astimezone(BEIJING).isoformat(), exc)
        if "initial" not in state["messages"]:
            cached = None
            if not args.no_cache_fallback:
                try:
                    cached = pf.load_latest_cache(keywords, categories, args.days, args.max_results,
                                                  args.cache_fallback_max_age_days)
                except (OSError, ValueError, AttributeError):
                    LOG.exception("invalid fallback cache")
            if cached is not None:
                subject = pf.build_cache_fallback_subject(args.days)
                body = pf.build_cache_fallback_report(exc, cached, keywords, categories, args.days)
            else:
                subject = pf.build_failure_subject(args.days)
                body = pf.build_failure_report(exc, keywords, categories, args.days, args.max_results)
            state["messages"]["initial"] = queued(subject, body)
        atomic_write(path, state)
        return 0 if deliver(path, state) else 1
    state["fetched_at"] = now().isoformat()
    state["status"] = "success"
    state["next_attempt"] = None
    subject = pf.build_email_subject(papers, args.days)
    if "initial" in state["messages"]:
        subject = "PaperFetch recovered: " + subject
    state["messages"]["fresh"] = queued(
        subject, pf.generate_email_html(papers, keywords, categories, args.days))
    fetched_label = "Fetched at (Beijing): " + now().astimezone(BEIJING).isoformat()
    body = state["messages"]["fresh"]["body"]
    if papers:
        body = pf.generate_email_html(papers, keywords, categories, args.days,
                                      notice_html=f"<p>{fetched_label}</p>")
    else:
        body += "\n\n" + fetched_label
    state["messages"]["fresh"]["body"] = body
    atomic_write(path, state)
    if not args.no_cache:
        try:
            pf.write_latest_cache(keywords, categories, args.days, args.max_results, papers)
        except OSError:
            LOG.exception("fresh result persisted in scheduler; latest cache write failed")
    LOG.info("fetch_success fetched_at=%s paper_count=%s", state["fetched_at"], len(papers))
    return 0 if deliver(path, state) else 1
