"""Durable DB-backed job queue and dispatchers (Cloud Tasks, local thread, inline test)."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import Flask
from sqlalchemy import func

from models import BinderItem, Job, User, db, utc_now

logger = logging.getLogger(__name__)


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class BaseDispatcher:
    def notify(self, kind: str, run_after: datetime) -> None:
        raise NotImplementedError


class InlineDispatcher(BaseDispatcher):
    """No-op dispatcher for tests; callers invoke run_due() explicitly."""

    def notify(self, kind: str, run_after: datetime) -> None:
        return


class CloudTasksDispatcher(BaseDispatcher):
    """Dispatches due job notifications via Google Cloud Tasks with OIDC auth."""

    def __init__(self, queue_path: str, target_url: str, invoker_sa: str, client_factory: Optional[Callable[[], Any]] = None):
        self.queue_path = queue_path
        self.target_url = target_url
        self.invoker_sa = invoker_sa
        self._client_factory = client_factory
        self._client = None

    def _get_client(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                from google.cloud import tasks_v2
                self._client = tasks_v2.CloudTasksClient()
        return self._client

    def notify(self, kind: str, run_after: datetime) -> None:
        run_utc = _ensure_utc(run_after) or utc_now()
        bucket = int(run_utc.timestamp() // 10)
        task_name = f"{self.queue_path}/tasks/vs-{kind}-{bucket}"
        url = f"{self.target_url}?kind={kind}"

        from google.protobuf import timestamp_pb2

        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(run_utc)

        http_request: Dict[str, Any] = {
            "http_method": "POST",
            "url": url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"kind": kind}).encode("utf-8"),
        }
        if self.invoker_sa:
            http_request["oidc_token"] = {
                "service_account_email": self.invoker_sa,
                "audience": self.target_url,
            }

        task = {
            "name": task_name,
            "http_request": http_request,
            "schedule_time": ts,
        }

        try:
            client = self._get_client()
            client.create_task(parent=self.queue_path, task=task)
        except Exception as exc:
            # Ignore AlreadyExists (deduplicated task within the 10-second bucket)
            exc_name = type(exc).__name__
            if exc_name == "AlreadyExists" or "AlreadyExists" in str(exc):
                return
            logger.warning("CloudTasksDispatcher failed to enqueue task %s: %s", task_name, exc)


class ThreadDispatcher(BaseDispatcher):
    """Local-dev background thread dispatcher that wakes when jobs are due."""

    def __init__(self, job_queue: "JobQueue"):
        self.job_queue = job_queue
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._woken = False

    def _ensure_started(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run,
                name="VaultSwap-JobDispatcher",
                daemon=True,
            )
            self._thread.start()

    def notify(self, kind: str, run_after: datetime) -> None:
        with self._cond:
            self._woken = True
            self._ensure_started()
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            app = self.job_queue.app
            if app is None:
                time.sleep(0.5)
                continue

            next_due: Optional[datetime] = None
            with app.app_context():
                try:
                    self.job_queue.recover_stale()
                    now = utc_now()
                    for kind in Job.KINDS:
                        self.job_queue.run_due(kind, time_budget_sec=300.0)
                    earliest = (
                        db.session.query(func.min(Job.run_after))
                        .filter(Job.status == "pending")
                        .scalar()
                    )
                    next_due = _ensure_utc(earliest)
                except Exception as exc:
                    logger.error("ThreadDispatcher loop error: %s", exc)
                    try:
                        db.session.rollback()
                    except Exception:
                        pass

            with self._cond:
                self._woken = False
                if next_due is None:
                    self._cond.wait(timeout=60.0)
                else:
                    delay = (next_due - utc_now()).total_seconds()
                    if delay > 0:
                        self._cond.wait(timeout=min(delay, 60.0))


class JobQueue:
    """Manages durable Job rows, claiming, execution, retries, and dispatching."""

    def __init__(self, app: Optional[Flask] = None):
        self.app = app
        self.dispatcher: BaseDispatcher = InlineDispatcher()
        self._last_meeple_fetch_ts: float = 0.0

    def init_app(self, app: Flask) -> None:
        self.app = app
        mode = (app.config.get("JOB_DISPATCHER") or "auto").strip().lower()
        queue_path = (app.config.get("CLOUD_TASKS_QUEUE") or "").strip()
        base_url = (app.config.get("APP_BASE_URL") or "").strip().rstrip("/")
        invoker_sa = (app.config.get("TASKS_INVOKER_SA") or "").strip()

        if mode == "inline" or (mode == "auto" and app.config.get("TESTING")):
            self.dispatcher = InlineDispatcher()
        elif mode == "cloudtasks" or (mode == "auto" and queue_path and base_url):
            self.dispatcher = CloudTasksDispatcher(
                queue_path=queue_path,
                target_url=f"{base_url}/tasks/run",
                invoker_sa=invoker_sa,
            )
        else:
            self.dispatcher = ThreadDispatcher(self)

    def enqueue(
        self,
        kind: str,
        ref_id: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
        delay_sec: float = 0.0,
        deduplicate: bool = True,
        notify: bool = True,
    ) -> Job:
        """Inserts a durable job into the DB and notifies the configured dispatcher."""
        if kind not in Job.KINDS:
            raise ValueError(f"Unknown job kind: {kind!r}")

        now = utc_now()
        run_after = now + timedelta(seconds=max(0.0, float(delay_sec)))

        if deduplicate and ref_id is not None:
            existing = (
                Job.query.filter(
                    Job.kind == kind,
                    Job.ref_id == ref_id,
                    Job.status.in_(["pending", "running"]),
                )
                .first()
            )
            if existing is not None:
                if kind == "discord_notify" and existing.status == "pending":
                    existing.run_after = run_after
                    db.session.commit()
                    if notify:
                        self.dispatcher.notify(kind, run_after)
                return existing

        job = Job(
            kind=kind,
            ref_id=ref_id,
            payload=json.dumps(payload) if payload is not None else None,
            status="pending",
            run_after=run_after,
            attempts=0,
            max_attempts=3,
            created_at=now,
        )
        db.session.add(job)
        db.session.commit()

        if notify:
            self.dispatcher.notify(kind, run_after)
        return job

    def enqueue_many(
        self,
        kind: str,
        ref_ids: List[int],
        delay_sec: float = 0.0,
    ) -> int:
        """Bulk-inserts deduplicated jobs for a list of ref_ids and dispatches once."""
        if not ref_ids:
            return 0
        if kind not in Job.KINDS:
            raise ValueError(f"Unknown job kind: {kind!r}")

        clean_ids = list(dict.fromkeys(int(i) for i in ref_ids if i is not None))
        if not clean_ids:
            return 0

        now = utc_now()
        run_after = now + timedelta(seconds=max(0.0, float(delay_sec)))

        existing_rows = (
            Job.query.filter(
                Job.kind == kind,
                Job.ref_id.in_(clean_ids),
                Job.status.in_(["pending", "running"]),
            )
            .all()
        )
        existing_ids = {row.ref_id for row in existing_rows}

        if kind == "discord_notify":
            for row in existing_rows:
                if row.status == "pending":
                    row.run_after = run_after

        added = 0
        for rid in clean_ids:
            if rid in existing_ids:
                continue
            db.session.add(
                Job(
                    kind=kind,
                    ref_id=rid,
                    status="pending",
                    run_after=run_after,
                    attempts=0,
                    max_attempts=3,
                    created_at=now,
                )
            )
            added += 1

        db.session.commit()
        if added > 0 or (kind == "discord_notify" and existing_rows):
            self.dispatcher.notify(kind, run_after)
        return added

    def pending_count(self, kind: str) -> int:
        return Job.query.filter(Job.kind == kind, Job.status.in_(["pending", "running"])).count()

    def is_pending(self, kind: str, ref_id: int) -> bool:
        return (
            Job.query.filter(
                Job.kind == kind,
                Job.ref_id == ref_id,
                Job.status.in_(["pending", "running"]),
            )
            .first()
            is not None
        )

    def pending_ref_ids(self, kind: str, ref_ids: List[int]) -> set[int]:
        id_list = [int(i) for i in ref_ids if i is not None]
        if not id_list:
            return set()
        rows = (
            db.session.query(Job.ref_id)
            .filter(
                Job.kind == kind,
                Job.ref_id.in_(id_list),
                Job.status.in_(["pending", "running"]),
            )
            .all()
        )
        return {int(r[0]) for r in rows if r[0] is not None}

    def recover_stale(self, stale_minutes: int = 35) -> int:
        """Resets jobs stuck in 'running' longer than stale_minutes."""
        now = utc_now()
        cutoff = now - timedelta(minutes=stale_minutes)
        stale_jobs = Job.query.filter(Job.status == "running", Job.locked_at < cutoff).all()
        recovered = 0
        for job in stale_jobs:
            if (job.attempts or 0) < (job.max_attempts or 3):
                job.status = "pending"
                job.locked_at = None
                job.run_after = now
            else:
                job.status = "failed"
                job.locked_at = None
                job.finished_at = now
                job.last_error = (job.last_error or "Timed out while running")[:1000]
                if job.kind == "drive_import":
                    job.payload = None
            recovered += 1
        if recovered:
            db.session.commit()
        return recovered

    def claim(self, kind: str, limit: int = 1, ignore_schedule: bool = False) -> List[Job]:
        """Atomically claims up to `limit` due pending jobs of `kind`."""
        now = utc_now()
        q = Job.query.filter(Job.kind == kind, Job.status == "pending")
        if not ignore_schedule:
            q = q.filter(Job.run_after <= now)
        q = q.order_by(Job.run_after.asc(), Job.id.asc()).limit(limit)

        if db.engine.dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)

        jobs = q.all()
        if not jobs:
            return []

        for job in jobs:
            job.status = "running"
            job.locked_at = now
            job.attempts = (job.attempts or 0) + 1
        db.session.commit()
        return jobs

    def _mark_done(self, job: Job) -> None:
        job.status = "done"
        job.locked_at = None
        job.finished_at = utc_now()
        if job.kind == "drive_import":
            job.payload = None
        db.session.commit()

    def _mark_error(self, job: Job, exc: Exception) -> None:
        try:
            db.session.rollback()
        except Exception:
            pass
        refreshed = db.session.get(Job, job.id)
        if refreshed is None:
            return
        refreshed.last_error = str(exc)[:1000]
        refreshed.locked_at = None
        if (refreshed.attempts or 0) < (refreshed.max_attempts or 3):
            backoff_sec = 30 * (2 ** max(0, (refreshed.attempts or 1) - 1))
            refreshed.status = "pending"
            refreshed.run_after = utc_now() + timedelta(seconds=backoff_sec)
        else:
            refreshed.status = "failed"
            refreshed.finished_at = utc_now()
            if refreshed.kind == "drive_import":
                refreshed.payload = None
        db.session.commit()

    def run_due(
        self,
        kind: str,
        time_budget_sec: float = 500.0,
        ignore_schedule: bool = False,
    ) -> int:
        """Executes due jobs of `kind` within `time_budget_sec`. Returns number of jobs handled."""
        if kind == "discord_notify":
            return self._run_discord_batch(ignore_schedule=ignore_schedule)
        if kind == "meeple_lookup":
            return self._run_meeple_loop(time_budget_sec=time_budget_sec, ignore_schedule=ignore_schedule)
        if kind == "drive_import":
            return self._run_drive_loop(time_budget_sec=time_budget_sec, ignore_schedule=ignore_schedule)
        raise ValueError(f"Unknown job kind: {kind!r}")

    def _polite_sleep_if_needed(self) -> None:
        if self._last_meeple_fetch_ts <= 0 or self.app is None:
            return
        min_sec = float(self.app.config.get("MEEPLE_RATE_LIMIT_MIN_SEC", 1.5))
        max_sec = float(self.app.config.get("MEEPLE_RATE_LIMIT_MAX_SEC", 2.0))
        if max_sec <= 0:
            return
        target_delay = random.uniform(min(min_sec, max_sec), max(min_sec, max_sec))
        elapsed = time.monotonic() - self._last_meeple_fetch_ts
        remaining = target_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _run_meeple_loop(self, time_budget_sec: float, ignore_schedule: bool = False) -> int:
        import services
        from providers.mightymeeple import MightyMeepleProvider

        start_ts = time.monotonic()
        processed = 0
        provider = None

        while (time.monotonic() - start_ts) < time_budget_sec:
            claimed = self.claim("meeple_lookup", limit=1, ignore_schedule=ignore_schedule)
            if not claimed:
                break
            job = claimed[0]
            try:
                item = db.session.get(BinderItem, job.ref_id) if job.ref_id else None
                if (
                    item is None
                    or not item.card
                    or item.status not in ("for_trade", "meeple_bound")
                    or item.mm_checked_at is not None
                ):
                    self._mark_done(job)
                    processed += 1
                    continue

                ttl = float(self.app.config.get("MEEPLE_CACHE_TTL_HOURS", 24.0)) if self.app else 24.0
                cached = services.find_cached_meeple_snapshot(
                    card_name=item.card.name,
                    set_code=item.card.set_code,
                    finish=item.finish,
                    scryfall_id=item.scryfall_id,
                    cache_ttl_hours=ttl,
                )
                if cached is not None:
                    services.apply_meeple_snapshot(item, cached)
                    self._mark_done(job)
                    processed += 1
                    continue

                self._polite_sleep_if_needed()
                if provider is None:
                    factory = getattr(self.app, "meeple_provider_factory", None) or MightyMeepleProvider
                    provider = factory()

                snapshot = services.get_meeple_snapshot_cached_or_live(
                    card_name=item.card.name,
                    set_code=item.card.set_code,
                    finish=item.finish,
                    scryfall_id=item.scryfall_id,
                    collector_number=item.card.collector_number,
                    set_name=item.card.set_name,
                    condition=item.condition or "LP",
                    force_refresh=True,
                    cache_ttl_hours=ttl,
                    provider=provider,
                )
                self._last_meeple_fetch_ts = time.monotonic()
                if snapshot:
                    services.apply_meeple_snapshot(item, snapshot)
                self._mark_done(job)
                processed += 1
            except Exception as exc:
                logger.error("meeple_lookup job %s failed: %s", job.id, exc)
                self._mark_error(job, exc)

        # If time budget expired and more due jobs remain, schedule continuation
        remaining_due = Job.query.filter(
            Job.kind == "meeple_lookup",
            Job.status == "pending",
            Job.run_after <= utc_now(),
        ).first()
        if remaining_due is not None:
            self.dispatcher.notify("meeple_lookup", utc_now())

        return processed

    def _run_discord_batch(self, ignore_schedule: bool = False) -> int:
        import services

        now = utc_now()
        if not ignore_schedule:
            max_run_after = (
                db.session.query(func.max(Job.run_after))
                .filter(Job.kind == "discord_notify", Job.status == "pending")
                .scalar()
            )
            max_utc = _ensure_utc(max_run_after)
            if max_utc is None or max_utc > now:
                # Quiet window after the newest listed card hasn't elapsed yet
                return 0

        claimed = self.claim("discord_notify", limit=500, ignore_schedule=True)
        if not claimed:
            return 0

        try:
            batch_ids = [j.ref_id for j in claimed if j.ref_id is not None]
            if batch_ids:
                users_with_webhooks = (
                    User.query.filter(
                        User.discord_webhook_url.isnot(None),
                        User.discord_webhook_url != "",
                    )
                    .all()
                )
                webhook_urls: List[str] = []
                for u in users_with_webhooks:
                    url = (u.discord_webhook_url or "").strip()
                    if url and url not in webhook_urls:
                        webhook_urls.append(url)

                if webhook_urls:
                    items = (
                        BinderItem.query.options(
                            db.joinedload(BinderItem.card),
                            db.joinedload(BinderItem.user),
                        )
                        .filter(
                            BinderItem.id.in_(batch_ids),
                            BinderItem.status.in_(["for_trade", "meeple_bound"]),
                        )
                        .order_by(BinderItem.updated_at.asc(), BinderItem.id.asc())
                        .all()
                    )
                    if items:
                        base_url = str(self.app.config.get("APP_BASE_URL") or "").strip() if self.app else ""
                        payload = services.build_discord_notification_payload(items, app_base_url=base_url)
                        sender_fn = getattr(self.app, "discord_sender_fn", None) or services.send_discord_webhook
                        for url in webhook_urls:
                            sender_fn(url, payload)

            done_at = utc_now()
            for j in claimed:
                j.status = "done"
                j.locked_at = None
                j.finished_at = done_at
            db.session.commit()
            return len(claimed)
        except Exception as exc:
            logger.error("discord_notify batch failed: %s", exc)
            for j in claimed:
                self._mark_error(j, exc)
            return 0

    def _run_drive_loop(self, time_budget_sec: float, ignore_schedule: bool = False) -> int:
        import drive_sync

        start_ts = time.monotonic()
        processed = 0

        while (time.monotonic() - start_ts) < time_budget_sec:
            claimed = self.claim("drive_import", limit=1, ignore_schedule=ignore_schedule)
            if not claimed:
                break
            job = claimed[0]
            try:
                data = json.loads(job.payload or "{}")
                access_token = str(data.get("access_token") or "")
                client_factory = getattr(self.app, "drive_client_factory", None) or drive_sync.DriveClient
                sf_factory = getattr(self.app, "drive_scryfall_factory", None)
                sf_provider = sf_factory() if sf_factory else None

                if job.ref_id is not None:
                    drive_sync.run_drive_import(
                        import_id=job.ref_id,
                        access_token=access_token,
                        drive_client=client_factory(),
                        scryfall_provider=sf_provider,
                    )
                self._mark_done(job)
                processed += 1
            except Exception as exc:
                logger.error("drive_import job %s failed: %s", job.id, exc)
                self._mark_error(job, exc)

        return processed
