"""Google Drive Picker incremental CSV import service and background worker for VaultSwap."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import queue
import threading
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Iterator, List, Optional, TextIO, Tuple

import requests
from flask import current_app

import services
from models import DriveImport, db, utc_now

logger = logging.getLogger(__name__)

DATE_HEADER_ALIASES = (
    "date added",
    "added",
    "added at",
    "added on",
    "date_added",
    "created",
    "created at",
    "created date",
    "date created",
    "created_at",
    "date",
)

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%m/%d/%y %H:%M:%S",
    "%m/%d/%y %H:%M",
    "%m/%d/%y",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
)


class DriveSyncError(Exception):
    """Raised when a Google Drive sync operation fails with a user-actionable error."""


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@lru_cache(maxsize=65536)
def parse_card_date(raw: Optional[str]) -> Optional[datetime]:
    """Parses common ManaBox / spreadsheet date strings or epoch timestamps into UTC datetime."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # Epoch seconds or milliseconds
    if s.isdigit():
        try:
            val = int(s)
            if val > 10_000_000_000:  # milliseconds
                val = val / 1000.0
            if 946_684_800 <= val <= 4_102_444_800:  # 2000-01-01 .. 2100-01-01
                return datetime.fromtimestamp(val, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            pass

    # ISO-8601 (handles 'Z', offsets, 'T' separator)
    iso_candidate = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso_candidate)
        return _ensure_utc(dt)
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def compute_row_fingerprint(entry: Dict[str, Any], raw_date: str) -> str:
    """Stable short hash for tie-breaking rows that share the exact watermark timestamp."""
    payload = "|".join([
        str(entry.get("name") or ""),
        str(entry.get("set_code") or ""),
        str(entry.get("collector_number") or ""),
        str(entry.get("scryfall_id") or ""),
        str(entry.get("finish") or ""),
        str(entry.get("condition") or ""),
        str(entry.get("quantity") or 1),
        str(raw_date or "").strip(),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class _IterContentStream(io.RawIOBase):
    """Adapts a `requests.Response.iter_content` iterator into a readable binary stream.

    Avoids wrapping `urllib3.HTTPResponse` (`resp.raw`) directly in `io.TextIOWrapper`,
    because `urllib3` closes `resp.raw` at EOF and subsequent `TextIOWrapper` reads raise
    `ValueError: I/O operation on closed file.`.
    """

    def __init__(self, chunks: Iterator[bytes]):
        self._chunks = iter(chunks)
        self._buf = bytearray()

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray | memoryview) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file.")
        while not self._buf:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                return 0
            if chunk:
                self._buf.extend(chunk)
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        del self._buf[:n]
        return n


class DriveClient:
    """Thin HTTP client for Google Drive v3 API and OAuth2 token verification."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()

    def verify_token(self, access_token: str, expected_aud: str) -> None:
        """Verifies the GIS access token is valid and issued for our Google Client ID."""
        if not access_token:
            raise DriveSyncError("Missing Google Drive access token.")
        resp = self.session.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"access_token": access_token},
            timeout=10,
        )
        if resp.status_code != 200:
            raise DriveSyncError("Google Drive authorization expired or invalid. Please pick the file again.")
        info = resp.json()
        aud = str(info.get("aud") or "").strip()
        if expected_aud and aud != expected_aud:
            raise DriveSyncError("Google access token audience mismatch.")

    def get_metadata(self, file_id: str, access_token: str) -> Dict[str, Any]:
        """Fetches lightweight file metadata (name, mimeType, md5Checksum, modifiedTime, size)."""
        resp = self.session.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "id,name,mimeType,md5Checksum,modifiedTime,size"},
            timeout=15,
        )
        if resp.status_code == 404:
            raise DriveSyncError("Drive file not found or access was not granted.")
        if resp.status_code == 401 or resp.status_code == 403:
            raise DriveSyncError("Google Drive permission denied. Please re-select the file.")
        if resp.status_code != 200:
            raise DriveSyncError(f"Google Drive metadata error ({resp.status_code}).")
        return resp.json()

    @contextmanager
    def open_csv_stream(
        self,
        file_id: str,
        mime_type: Optional[str],
        access_token: str,
    ) -> Iterator[TextIO]:
        """Streams a CSV file (or exports a Google Sheet to CSV) as a decoded text stream."""
        is_sheet = (mime_type or "").strip().lower() == "application/vnd.google-apps.spreadsheet"
        if is_sheet:
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export"
            params = {"mimeType": "text/csv"}
        else:
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
            params = {"alt": "media"}

        with self.session.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            stream=True,
            timeout=(15, 120),
        ) as resp:
            if resp.status_code != 200:
                if is_sheet and resp.status_code == 403:
                    raise DriveSyncError(
                        "Google Sheet export failed (Sheets exports are capped at 10 MB; upload as .csv for larger files)."
                    )
                raise DriveSyncError(f"Failed to download Drive file ({resp.status_code}).")
            buffered = io.BufferedReader(
                _IterContentStream(resp.iter_content(chunk_size=65536)),
                buffer_size=65536,
            )
            text_stream = io.TextIOWrapper(buffered, encoding="utf-8-sig", newline="", errors="replace")
            try:
                yield text_stream
            finally:
                try:
                    text_stream.close()
                except Exception:
                    pass


@dataclass
class ScanResult:
    date_column: str
    rows_scanned: int
    new_entries: List[Tuple[Optional[datetime], str, Dict[str, Any]]]
    skipped_no_date: int


def _find_date_column(header_row: List[str]) -> Tuple[Optional[int], Optional[str]]:
    cleaned = [str(c).strip().replace('"', "").replace("'", "") for c in header_row]
    lowered = [c.lower() for c in cleaned]
    for alias in DATE_HEADER_ALIASES:
        for idx, col in enumerate(lowered):
            if col == alias:
                return idx, cleaned[idx]
    return None, None


def scan_new_rows(
    text_stream: TextIO,
    watermark: Optional[datetime],
    boundary_fps: List[str],
    first_import: bool,
) -> ScanResult:
    """
    Streams CSV rows and filters to only cards newer than `watermark`
    (with fingerprint tie-breaking at `d == watermark`).
    """
    reader = csv.reader(text_stream)
    header_row = next(reader, None)
    if not header_row:
        raise DriveSyncError("CSV file is empty or missing a header row.")

    header_map = services.build_header_map(header_row)
    if "name" not in header_map:
        recognized = [str(c).strip() for c in header_row if str(c).strip()]
        raise DriveSyncError(
            f"Missing required 'Name' column in CSV headers. Found: {', '.join(recognized[:8])}"
        )

    date_idx, date_col_name = _find_date_column(header_row)
    if date_idx is None or date_col_name is None:
        recognized = [str(c).strip() for c in header_row if str(c).strip()]
        raise DriveSyncError(
            "No created-date column found (expected e.g. 'Date added', 'Added', or 'Created'). "
            f"Found headers: {', '.join(recognized[:12])}"
        )

    watermark_utc = _ensure_utc(watermark)
    boundary_counter = Counter(boundary_fps or [])

    rows_scanned = 0
    skipped_no_date = 0
    new_entries: List[Tuple[Optional[datetime], str, Dict[str, Any]]] = []

    for row_num, row in enumerate(reader, start=2):
        entry, err = services.parse_csv_row(row, header_map, row_num)
        if entry is None:
            continue
        rows_scanned += 1

        raw_date = str(row[date_idx]).strip() if date_idx < len(row) else ""
        card_dt = parse_card_date(raw_date)

        if card_dt is None:
            if first_import:
                fp = compute_row_fingerprint(entry, raw_date)
                new_entries.append((None, fp, entry))
            else:
                skipped_no_date += 1
            continue

        fp = compute_row_fingerprint(entry, raw_date)
        if watermark_utc is None or card_dt > watermark_utc:
            new_entries.append((card_dt, fp, entry))
        elif card_dt == watermark_utc:
            if boundary_counter[fp] > 0:
                boundary_counter[fp] -= 1
            else:
                new_entries.append((card_dt, fp, entry))
        # else card_dt < watermark_utc -> skip

    # Sort ascending by card_dt (None dates first) so chunked commits advance watermark monotonically
    min_dt = datetime.min.replace(tzinfo=timezone.utc)
    new_entries.sort(key=lambda item: item[0] or min_dt)

    return ScanResult(
        date_column=date_col_name,
        rows_scanned=rows_scanned,
        new_entries=new_entries,
        skipped_no_date=skipped_no_date,
    )


def _iter_timestamp_safe_chunks(
    items: List[Tuple[Optional[datetime], str, Dict[str, Any]]],
    chunk_size: int,
) -> Iterator[List[Tuple[Optional[datetime], str, Dict[str, Any]]]]:
    """Splits sorted items into chunks of ~`chunk_size` without splitting a timestamp group."""
    if not items:
        return
    size = max(1, int(chunk_size))
    n = len(items)
    start = 0
    while start < n:
        end = min(start + size, n)
        if end < n:
            boundary_dt = items[end - 1][0]
            if boundary_dt is not None:
                while end < n and items[end][0] == boundary_dt:
                    end += 1
        yield items[start:end]
        start = end


def _get_user_watermark(user_id: int) -> Tuple[Optional[datetime], List[str], bool]:
    """Returns (watermark, boundary_fingerprints, is_first_import) for `user_id`."""
    prev_with_wm = (
        DriveImport.query.filter(
            DriveImport.user_id == user_id,
            DriveImport.max_card_created_at.isnot(None),
        )
        .order_by(DriveImport.max_card_created_at.desc(), DriveImport.id.desc())
        .first()
    )
    if prev_with_wm is not None:
        try:
            fps = json.loads(prev_with_wm.boundary_fingerprints or "[]")
            if not isinstance(fps, list):
                fps = []
        except Exception:
            fps = []
        return _ensure_utc(prev_with_wm.max_card_created_at), fps, False

    any_done = DriveImport.query.filter(
        DriveImport.user_id == user_id,
        DriveImport.status.in_(["done", "unchanged"]),
    ).first()
    return None, [], (any_done is None)


def run_drive_import(
    import_id: int,
    access_token: str,
    drive_client: Optional[DriveClient] = None,
    scryfall_provider: Optional[Any] = None,
) -> None:
    """Executes a DriveImport job synchronously (called by DriveImportWorker or directly in tests)."""
    imp = db.session.get(DriveImport, import_id)
    if imp is None:
        return

    client = drive_client or DriveClient()
    try:
        imp.status = "running"
        imp.phase = "downloading"
        db.session.commit()

        meta = client.get_metadata(imp.drive_file_id, access_token)
        if meta.get("name"):
            imp.file_name = str(meta["name"])[:255]
        if meta.get("mimeType"):
            imp.mime_type = str(meta["mimeType"])[:100]
        md5 = (meta.get("md5Checksum") or "").strip() or None
        imp.drive_md5 = md5
        if meta.get("modifiedTime"):
            imp.drive_modified_time = parse_card_date(meta["modifiedTime"])

        max_mb = int(current_app.config.get("DRIVE_MAX_FILE_MB", 500))
        if meta.get("size") is not None:
            try:
                size_bytes = int(meta["size"])
                if size_bytes > max_mb * 1024 * 1024:
                    raise DriveSyncError(f"File exceeds the {max_mb} MB limit ({size_bytes // (1024 * 1024)} MB).")
            except (ValueError, TypeError):
                pass

        # Skip unchanged files without downloading
        if md5:
            same_md5 = (
                DriveImport.query.filter(
                    DriveImport.user_id == imp.user_id,
                    DriveImport.id != imp.id,
                    DriveImport.drive_md5 == md5,
                    DriveImport.status.in_(["done", "unchanged"]),
                )
                .first()
            )
            if same_md5 is not None:
                imp.status = "unchanged"
                imp.phase = None
                imp.finished_at = utc_now()
                db.session.commit()
                return

        watermark, boundary_fps, first_import = _get_user_watermark(imp.user_id)

        with client.open_csv_stream(imp.drive_file_id, imp.mime_type, access_token) as text_stream:
            scan = scan_new_rows(
                text_stream=text_stream,
                watermark=watermark,
                boundary_fps=boundary_fps,
                first_import=first_import,
            )

        imp.date_column = scan.date_column[:100]
        imp.rows_scanned = scan.rows_scanned
        imp.rows_new = len(scan.new_entries)
        imp.rows_skipped_no_date = scan.skipped_no_date
        db.session.commit()

        chunk_size = int(current_app.config.get("DRIVE_IMPORT_CHUNK_ROWS", 1000))
        unresolved_acc: List[str] = []
        current_wm = watermark
        current_fps: List[str] = list(boundary_fps) if watermark is not None else []

        for chunk in _iter_timestamp_safe_chunks(scan.new_entries, chunk_size):
            imp.phase = "resolving"
            db.session.commit()

            entries = [item[2] for item in chunk]
            res = services.import_cards_to_binder(
                imp.user_id,
                entries,
                scryfall_provider=scryfall_provider,
                commit=False,
            )
            imp.phase = "importing"
            imp.rows_imported = (imp.rows_imported or 0) + res["imported_rows"]
            imp.cards_imported = (imp.cards_imported or 0) + res["total_quantity"]
            if res["unresolved"] and len(unresolved_acc) < 200:
                remaining = 200 - len(unresolved_acc)
                unresolved_acc.extend(res["unresolved"][:remaining])
                imp.unresolved = json.dumps(unresolved_acc)

            # Advance watermark & boundary fingerprints in the same transaction
            dated = [(dt, fp) for dt, fp, _ in chunk if dt is not None]
            if dated:
                chunk_max_dt = max(dt for dt, _ in dated)
                if current_wm is None or chunk_max_dt > current_wm:
                    current_wm = chunk_max_dt
                    current_fps = [fp for dt, fp in dated if dt == chunk_max_dt]
                elif chunk_max_dt == current_wm:
                    current_fps.extend(fp for dt, fp in dated if dt == chunk_max_dt)
                imp.max_card_created_at = current_wm
                imp.boundary_fingerprints = json.dumps(current_fps)

            db.session.commit()

        imp.status = "done"
        imp.phase = None
        imp.finished_at = utc_now()
        db.session.commit()

        # Log activity
        try:
            from models import ActivityLog, User
            user_obj = db.session.get(User, imp.user_id)
            log_entry = ActivityLog(
                user_id=imp.user_id,
                user_email=user_obj.email if user_obj else None,
                user_name=user_obj.display_name if user_obj else None,
                action="UPLOAD_DRIVE_SYNC",
                details=f"Google Drive Sync ({imp.file_name}): Imported {imp.cards_imported or 0} card(s) across {imp.rows_imported or 0} row(s).",
                created_at=utc_now(),
            )
            db.session.add(log_entry)
            db.session.commit()
        except Exception as log_err:
            logger.debug("Failed to log drive import activity: %s", log_err)
    except Exception as exc:
        logger.error("DriveImport %s failed: %s", import_id, exc, exc_info=True)
        try:
            db.session.rollback()
        except Exception:
            pass
        imp = db.session.get(DriveImport, import_id)
        if imp is not None:
            imp.status = "failed"
            imp.phase = None
            imp.error = str(exc)[:1000]
            imp.finished_at = utc_now()
            db.session.commit()


class DriveImportWorker:
    """Sequential background worker for Google Drive CSV imports."""

    def __init__(self, app=None, drive_client_factory=None, scryfall_factory=None):
        self.app = app
        self.drive_client_factory = drive_client_factory or DriveClient
        self.scryfall_factory = scryfall_factory
        self._queue: queue.Queue[Tuple[int, str]] = queue.Queue()
        self._pending_ids: set[int] = set()
        self._lock = threading.Lock()
        self._idle_cond = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None

    def init_app(self, app) -> None:
        self.app = app

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="VaultSwap-DriveWorker",
                    daemon=True,
                )
                self._thread.start()

    def enqueue(self, import_id: int, access_token: str) -> bool:
        with self._lock:
            if import_id in self._pending_ids:
                return False
            self._pending_ids.add(import_id)
            self._queue.put((import_id, access_token))
        self._ensure_started()
        return True

    def wait_until_idle(self, timeout: float = 10.0) -> bool:
        import time

        deadline = time.monotonic() + timeout
        with self._idle_cond:
            while self._pending_ids or not self._queue.empty():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle_cond.wait(timeout=remaining)
            return True

    def _run(self) -> None:
        while True:
            import_id, access_token = self._queue.get()
            try:
                if self.app is not None:
                    with self.app.app_context():
                        sf = self.scryfall_factory() if self.scryfall_factory else None
                        run_drive_import(
                            import_id=import_id,
                            access_token=access_token,
                            drive_client=self.drive_client_factory(),
                            scryfall_provider=sf,
                        )
            except Exception as exc:
                logger.error("DriveImportWorker error on import %s: %s", import_id, exc)
            finally:
                with self._idle_cond:
                    self._pending_ids.discard(import_id)
                    if not self._pending_ids and self._queue.empty():
                        self._idle_cond.notify_all()
                self._queue.task_done()
