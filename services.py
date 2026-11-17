"""Domain services for VaultSwap: import, Scryfall batching, Mighty Meeple caching, and polite worker queue."""

from __future__ import annotations

import csv
import io
import logging
import queue
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple
from flask import current_app
from sqlalchemy import and_, func, or_

from card_utils import fix_mojibake, normalize_card_name, normalize_condition, normalize_finish
from models import BinderItem, Card, db, utc_now
from providers.mightymeeple import MightyMeepleProvider
from providers.scryfall import ScryfallProvider, UUID_PATTERN

logger = logging.getLogger(__name__)


def _to_decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:
        return None


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def apply_meeple_snapshot(item: BinderItem, snapshot: Optional[Dict[str, Any]]) -> None:
    """Applies a Mighty Meeple snapshot dict onto a BinderItem row."""
    if not snapshot:
        return
    item.mm_in_stock = bool(snapshot.get("in_stock")) if snapshot.get("in_stock") is not None else False
    item.mm_retail_price = _to_decimal_or_none(snapshot.get("retail_price"))
    item.mm_buylist_cash = _to_decimal_or_none(snapshot.get("buylist_cash"))
    item.mm_buylist_credit = _to_decimal_or_none(snapshot.get("buylist_credit"))
    item.mm_checked_at = _ensure_utc(snapshot.get("checked_at")) or utc_now()


def find_cached_meeple_snapshot(
    card_name: str,
    set_code: Optional[str],
    finish: str,
    scryfall_id: Optional[str] = None,
    cache_ttl_hours: float = 24.0,
) -> Optional[Dict[str, Any]]:
    """
    Checks across all pod members' binder_items for a Mighty Meeple snapshot fetched within
    the last `cache_ttl_hours` for the exact printing (card_name + set_code + finish, or scryfall_id + finish).
    """
    cutoff = utc_now() - timedelta(hours=cache_ttl_hours)
    finish_norm = normalize_finish(finish)
    clean_name = (card_name or "").strip().lower()
    clean_set = (set_code or "").strip().upper()

    match_clauses = []
    if scryfall_id:
        match_clauses.append(BinderItem.scryfall_id == str(scryfall_id).strip().lower())
    if clean_name and clean_set:
        match_clauses.append(
            and_(
                func.lower(Card.name) == clean_name,
                func.upper(Card.set_code) == clean_set,
            )
        )

    if not match_clauses:
        return None

    cached = (
        db.session.query(BinderItem)
        .join(Card, BinderItem.scryfall_id == Card.scryfall_id)
        .filter(
            BinderItem.mm_checked_at.isnot(None),
            BinderItem.mm_checked_at >= cutoff,
            BinderItem.finish == finish_norm,
            or_(*match_clauses),
        )
        .order_by(BinderItem.mm_checked_at.desc())
        .first()
    )

    if not cached:
        return None

    return {
        "in_stock": bool(cached.mm_in_stock) if cached.mm_in_stock is not None else False,
        "retail_price": float(cached.mm_retail_price) if cached.mm_retail_price is not None else None,
        "buylist_cash": float(cached.mm_buylist_cash) if cached.mm_buylist_cash is not None else None,
        "buylist_credit": float(cached.mm_buylist_credit) if cached.mm_buylist_credit is not None else None,
        "checked_at": _ensure_utc(cached.mm_checked_at),
        "cached": True,
    }


def get_meeple_snapshot_cached_or_live(
    card_name: str,
    set_code: Optional[str],
    finish: str,
    scryfall_id: Optional[str] = None,
    collector_number: Optional[str] = None,
    set_name: Optional[str] = None,
    condition: str = "NM",
    force_refresh: bool = False,
    cache_ttl_hours: Optional[float] = None,
    provider: Optional[MightyMeepleProvider] = None,
) -> Optional[Dict[str, Any]]:
    """
    Returns Mighty Meeple stock, retail, and buylist snapshot.
    Uses pod-wide 24-hour DB cache first unless force_refresh=True.
    """
    if cache_ttl_hours is None:
        try:
            cache_ttl_hours = float(current_app.config.get("MEEPLE_CACHE_TTL_HOURS", 24.0))
        except RuntimeError:
            cache_ttl_hours = 24.0

    if not force_refresh:
        cached = find_cached_meeple_snapshot(
            card_name=card_name,
            set_code=set_code,
            finish=finish,
            scryfall_id=scryfall_id,
            cache_ttl_hours=cache_ttl_hours,
        )
        if cached is not None:
            logger.debug("Mighty Meeple pod cache hit for %r [%s %s]", card_name, set_code, finish)
            return cached

    client = provider or MightyMeepleProvider()
    snapshot = client.get_card_snapshot(
        card_name=card_name,
        set_code=set_code,
        set_name=set_name,
        collector_number=collector_number,
        finish=finish,
        condition=condition,
    )
    if snapshot is None:
        return None

    snapshot["checked_at"] = utc_now()
    snapshot["cached"] = False
    return snapshot


class MeepleBackgroundWorker:
    """
    Sequential background worker for bulk Mighty Meeple lookups with a polite 1.5s-2.0s jittered delay
    between live external requests.
    """

    def __init__(self, app=None, provider_factory=None):
        self.app = app
        self.provider_factory = provider_factory or MightyMeepleProvider
        self._queue: queue.Queue[int] = queue.Queue()
        self._pending_ids: Set[int] = set()
        self._lock = threading.Lock()
        self._idle_cond = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._last_live_fetch_ts: float = 0.0

    def init_app(self, app) -> None:
        self.app = app

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="VaultSwap-MeepleWorker",
                    daemon=True,
                )
                self._thread.start()

    def is_pending(self, item_id: int) -> bool:
        with self._lock:
            return item_id in self._pending_ids

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending_ids)

    def enqueue(self, item_ids: List[int]) -> int:
        """Queues binder item IDs for sequential Mighty Meeple lookup."""
        added = 0
        with self._lock:
            for item_id in item_ids:
                if item_id not in self._pending_ids:
                    self._pending_ids.add(item_id)
                    self._queue.put(item_id)
                    added += 1
        if added > 0:
            self._ensure_started()
        return added

    def wait_until_idle(self, timeout: float = 10.0) -> bool:
        """Waits for all currently queued jobs to finish (primarily for tests)."""
        deadline = time.monotonic() + timeout
        with self._idle_cond:
            while self._pending_ids or not self._queue.empty():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle_cond.wait(timeout=remaining)
            return True

    def _polite_sleep_if_needed(self) -> None:
        if self._last_live_fetch_ts <= 0:
            return
        min_sec = float(self.app.config.get("MEEPLE_RATE_LIMIT_MIN_SEC", 1.5))
        max_sec = float(self.app.config.get("MEEPLE_RATE_LIMIT_MAX_SEC", 2.0))
        if max_sec <= 0:
            return
        target_delay = random.uniform(min(min_sec, max_sec), max(min_sec, max_sec))
        elapsed = time.monotonic() - self._last_live_fetch_ts
        remaining = target_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _run(self) -> None:
        provider = None
        while True:
            item_id = self._queue.get()
            try:
                if self.app is None:
                    continue
                with self.app.app_context():
                    item = db.session.get(BinderItem, item_id)
                    if not item or not item.card:
                        continue
                    if item.status not in ("for_trade", "meeple_bound") or item.mm_checked_at is not None:
                        continue

                    ttl = float(self.app.config.get("MEEPLE_CACHE_TTL_HOURS", 24.0))
                    # Check pod cache first without incurring rate-limit delay
                    cached = find_cached_meeple_snapshot(
                        card_name=item.card.name,
                        set_code=item.card.set_code,
                        finish=item.finish,
                        scryfall_id=item.scryfall_id,
                        cache_ttl_hours=ttl,
                    )
                    if cached is not None:
                        apply_meeple_snapshot(item, cached)
                        db.session.commit()
                        continue

                    # Enforce polite 1.5s-2.0s jittered delay before external Mighty Meeple request
                    self._polite_sleep_if_needed()
                    if provider is None:
                        provider = self.provider_factory()

                    snapshot = get_meeple_snapshot_cached_or_live(
                        card_name=item.card.name,
                        set_code=item.card.set_code,
                        finish=item.finish,
                        scryfall_id=item.scryfall_id,
                        collector_number=item.card.collector_number,
                        set_name=item.card.set_name,
                        condition=item.condition or "NM",
                        force_refresh=True,
                        cache_ttl_hours=ttl,
                        provider=provider,
                    )
                    self._last_live_fetch_ts = time.monotonic()
                    if snapshot:
                        apply_meeple_snapshot(item, snapshot)
                        db.session.commit()
            except Exception as exc:
                logger.error("MeepleBackgroundWorker error on item %s: %s", item_id, exc)
                try:
                    db.session.rollback()
                except Exception:
                    pass
            finally:
                with self._idle_cond:
                    self._pending_ids.discard(item_id)
                    if not self._pending_ids and self._queue.empty():
                        self._idle_cond.notify_all()
                self._queue.task_done()


# =============================================================================
# ManaBox CSV & Quick-List Parsers
# =============================================================================

HEADER_ALIASES = {
    "name": "name",
    "card name": "name",
    "card_name": "name",
    "card": "name",
    "set code": "set_code",
    "set_code": "set_code",
    "set": "set_code",
    "edition": "set_code",
    "set name": "set_name",
    "set_name": "set_name",
    "edition name": "set_name",
    "collector number": "collector_number",
    "collector_number": "collector_number",
    "card number": "collector_number",
    "card_number": "collector_number",
    "number": "collector_number",
    "quantity": "quantity",
    "qty": "quantity",
    "count": "quantity",
    "foil": "foil",
    "finish": "foil",
    "condition": "condition",
    "scryfall id": "scryfall_id",
    "scryfall_id": "scryfall_id",
    "scryfallid": "scryfall_id",
}


class ImportParseError(Exception):
    """Raised when uploaded CSV or pasted card text cannot be parsed."""


def parse_manabox_csv(csv_content: str) -> Dict[str, Any]:
    """Parses ManaBox or standard MTG collection CSV content."""
    if not csv_content or not csv_content.strip():
        raise ImportParseError("The uploaded CSV file is empty.")

    clean_content = csv_content.lstrip("\ufeff").strip()
    first_150 = clean_content[:150].lstrip().lower()
    if first_150.startswith("<") or "<html" in first_150 or "<!doctype" in first_150:
        raise ImportParseError("The uploaded content appears to be HTML/XML rather than a CSV export.")

    reader = csv.reader(io.StringIO(clean_content))
    header_row = next(reader, None)
    if not header_row:
        raise ImportParseError("CSV does not contain a header row.")

    header_map: Dict[str, int] = {}
    for idx, col in enumerate(header_row):
        col_clean = str(col).strip().lower().replace('"', "").replace("'", "")
        canonical = HEADER_ALIASES.get(col_clean)
        if canonical and canonical not in header_map:
            header_map[canonical] = idx

    if "name" not in header_map:
        recognized = [str(c).strip() for c in header_row if str(c).strip()]
        raise ImportParseError(
            f"Missing required 'Name' column in CSV headers. Found: {', '.join(recognized[:8])}"
        )

    entries: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for row_num, row in enumerate(reader, start=2):
        if not row or not any(str(cell).strip() for cell in row):
            continue

        name_idx = header_map["name"]
        if name_idx >= len(row):
            errors.append({"row": row_num, "error": "Row missing Name column."})
            continue

        raw_name = str(row[name_idx]).strip()
        norm_name = normalize_card_name(raw_name)
        if not norm_name:
            errors.append({"row": row_num, "error": "Card name is empty."})
            continue

        qty = 1
        if "quantity" in header_map and header_map["quantity"] < len(row):
            qty_str = str(row[header_map["quantity"]]).strip()
            if qty_str:
                try:
                    qty = int(qty_str)
                    if qty <= 0:
                        errors.append({"row": row_num, "error": f"Quantity must be > 0 for '{norm_name}'."})
                        continue
                except ValueError:
                    errors.append({"row": row_num, "error": f"Invalid quantity '{qty_str}' for '{norm_name}'."})
                    continue

        def _col(key: str) -> str:
            if key in header_map and header_map[key] < len(row):
                return str(row[header_map[key]]).strip()
            return ""

        entries.append({
            "name": norm_name,
            "set_code": _col("set_code").upper(),
            "set_name": _col("set_name"),
            "collector_number": _col("collector_number"),
            "scryfall_id": _col("scryfall_id").lower(),
            "quantity": qty,
            "finish": normalize_finish(_col("foil")),
            "condition": normalize_condition(_col("condition")),
        })

    if not entries and not errors:
        raise ImportParseError("No card entries found in the CSV file.")

    return {"entries": entries, "errors": errors}


QUICK_LINE_RE = re.compile(
    r"^(?:(\d+)\s*[xX]?\s+)?(.+?)(?:\s+[\[(]([A-Za-z0-9]{2,8})[\])](?:\s+([A-Za-z0-9-]+))?)?(?:\s+(\*[^*]+\*))?$"
)


def parse_quick_list(text_content: str) -> Dict[str, Any]:
    """
    Parses pasted MTG lines such as:
    - 1 Sol Ring
    - 2x Counterspell (CMM) 81 *F*
    - 1 Rhystic Study [WOT] 25
    """
    if not text_content or not text_content.strip():
        raise ImportParseError("No card text provided.")

    entries: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for line_num, raw_line in enumerate(text_content.splitlines(), start=1):
        line = fix_mojibake(raw_line).strip()
        if not line:
            continue

        lower_line = line.lower()
        if lower_line.startswith("//") or lower_line.startswith("#"):
            continue
        if lower_line in ("commander", "commanders", "deck", "main", "mainboard", "sideboard", "maybeboard"):
            continue

        finish = "nonfoil"
        if re.search(r"\*etched\*|\(etched\)|\[etched\]", lower_line):
            finish = "etched"
        elif re.search(r"\*f\*|\*foil\*|\(foil\)|\[foil\]", lower_line):
            finish = "foil"

        cleaned_line = re.sub(r"\s*(\*(?:f|foil|etched)\*|\((?:foil|etched)\)|\[(?:foil|etched)\])", "", line, flags=re.IGNORECASE).strip()

        m = QUICK_LINE_RE.match(cleaned_line)
        if not m:
            errors.append({"row": line_num, "error": f"Could not parse line: {line}"})
            continue

        qty_str, raw_name, set_code, col_num, _star = m.groups()
        qty = int(qty_str) if qty_str else 1
        if qty <= 0:
            errors.append({"row": line_num, "error": f"Quantity must be positive: {line}"})
            continue

        norm_name = normalize_card_name(raw_name)
        if not norm_name:
            errors.append({"row": line_num, "error": f"Missing card name on line {line_num}."})
            continue

        entries.append({
            "name": norm_name,
            "set_code": (set_code or "").upper().strip(),
            "set_name": "",
            "collector_number": (col_num or "").strip(),
            "scryfall_id": "",
            "quantity": qty,
            "finish": finish,
            "condition": "NM",
        })

    if not entries and not errors:
        raise ImportParseError("No valid card lines found in pasted text.")

    return {"entries": entries, "errors": errors}


def parse_import_input(csv_text: Optional[str] = None, quick_text: Optional[str] = None) -> Dict[str, Any]:
    """Parses either ManaBox CSV upload or pasted card text (auto-detecting CSV pasted into text box)."""
    if csv_text and csv_text.strip():
        return parse_manabox_csv(csv_text)

    if quick_text and quick_text.strip():
        first_line = quick_text.strip().splitlines()[0].lower()
        if "," in first_line and any(h in first_line for h in ("name", "scryfall id", "set code", "collector number")):
            return parse_manabox_csv(quick_text)
        return parse_quick_list(quick_text)

    raise ImportParseError("Provide either a ManaBox CSV file or a pasted card list.")


# =============================================================================
# Binder Import & Scryfall Canonical Cache Sync (Zero Mighty Meeple Traffic)
# =============================================================================

def _find_local_card(entry: Dict[str, Any]) -> Optional[Card]:
    sid = str(entry.get("scryfall_id") or "").strip().lower()
    if sid and UUID_PATTERN.match(sid):
        card = db.session.get(Card, sid)
        if card:
            return card

    set_code = str(entry.get("set_code") or "").strip().upper()
    col_num = str(entry.get("collector_number") or "").strip()
    if set_code and col_num:
        card = Card.query.filter(
            func.upper(Card.set_code) == set_code,
            func.lower(Card.collector_number) == col_num.lower(),
        ).first()
        if card:
            return card

    if not set_code and not col_num and entry.get("name"):
        card = Card.query.filter(
            func.lower(Card.name) == entry["name"].strip().lower()
        ).first()
        if card:
            return card

    return None


def _upsert_card_from_scryfall(card_data: Dict[str, Any]) -> Card:
    sid = str(card_data["scryfall_id"]).lower()
    card = db.session.get(Card, sid)
    now = utc_now()
    if card is None:
        card = Card(
            scryfall_id=sid,
            oracle_id=str(card_data["oracle_id"]).lower(),
            name=card_data["name"],
            set_code=card_data["set_code"],
            collector_number=card_data["collector_number"],
            set_name=card_data.get("set_name") or None,
            image_uri_normal=card_data.get("image_uri_normal"),
            image_uri_small=card_data.get("image_uri_small"),
            mana_cost=card_data.get("mana_cost"),
            type_line=card_data.get("type_line"),
            usd_price=card_data.get("usd_price"),
            usd_foil_price=card_data.get("usd_foil_price"),
            scryfall_updated_at=now,
        )
        db.session.add(card)
    else:
        card.oracle_id = str(card_data["oracle_id"]).lower()
        card.name = card_data["name"]
        card.set_code = card_data["set_code"]
        card.collector_number = card_data["collector_number"]
        if card_data.get("set_name"):
            card.set_name = card_data["set_name"]
        if card_data.get("image_uri_normal"):
            card.image_uri_normal = card_data["image_uri_normal"]
        if card_data.get("image_uri_small"):
            card.image_uri_small = card_data["image_uri_small"]
        if card_data.get("mana_cost") is not None:
            card.mana_cost = card_data["mana_cost"]
        if card_data.get("type_line") is not None:
            card.type_line = card_data["type_line"]
        if card_data.get("usd_price") is not None:
            card.usd_price = card_data["usd_price"]
        if card_data.get("usd_foil_price") is not None:
            card.usd_foil_price = card_data["usd_foil_price"]
        card.scryfall_updated_at = now
    return card


def import_cards_to_binder(
    user_id: int,
    parsed_entries: List[Dict[str, Any]],
    scryfall_provider: Optional[ScryfallProvider] = None,
) -> Dict[str, Any]:
    """
    Imports parsed cards into `user_id`'s private binder using local `cards` cache +
    Scryfall /cards/collection (<=75 per POST).
    Strictly ZERO Mighty Meeple requests are executed on import/add.
    """
    if not parsed_entries:
        return {"imported_rows": 0, "total_quantity": 0, "unresolved": []}

    resolved_cards: Dict[int, Card] = {}
    missing_indices: List[int] = []

    for idx, entry in enumerate(parsed_entries):
        local = _find_local_card(entry)
        if local is not None:
            resolved_cards[idx] = local
        else:
            missing_indices.append(idx)

    unresolved_names: List[str] = []
    if missing_indices:
        sf = scryfall_provider or ScryfallProvider()
        missing_entries = [parsed_entries[i] for i in missing_indices]
        sf_resolved, sf_unresolved = sf.resolve_collection_batch(missing_entries, fallback_named=True)

        # Deduplicate upserts by scryfall_id within the same batch
        batch_cards: Dict[str, Card] = {}
        for local_idx, card_dict in sf_resolved.items():
            orig_idx = missing_indices[local_idx]
            sid = str(card_dict["scryfall_id"]).lower()
            if sid in batch_cards:
                resolved_cards[orig_idx] = batch_cards[sid]
            else:
                card_obj = _upsert_card_from_scryfall(card_dict)
                batch_cards[sid] = card_obj
                resolved_cards[orig_idx] = card_obj

        for local_idx in sf_unresolved:
            orig_idx = missing_indices[local_idx]
            unresolved_names.append(parsed_entries[orig_idx].get("name") or "Unknown")

        db.session.flush()

    now = utc_now()
    imported_rows = 0
    total_qty = 0

    for idx, entry in enumerate(parsed_entries):
        card_obj = resolved_cards.get(idx)
        if card_obj is None:
            continue

        qty = max(1, int(entry.get("quantity") or 1))
        finish = normalize_finish(entry.get("finish"))
        condition = normalize_condition(entry.get("condition"))

        # Merge into an existing private row for the exact printing/finish/condition if present
        existing = BinderItem.query.filter_by(
            user_id=user_id,
            scryfall_id=card_obj.scryfall_id,
            finish=finish,
            condition=condition,
            status="private",
        ).first()

        if existing:
            existing.quantity = (existing.quantity or 0) + qty
            existing.updated_at = now
        else:
            item = BinderItem(
                user_id=user_id,
                scryfall_id=card_obj.scryfall_id,
                quantity=qty,
                finish=finish,
                condition=condition,
                status="private",
                mm_checked_at=None,
                created_at=now,
                updated_at=now,
            )
            db.session.add(item)

        imported_rows += 1
        total_qty += qty

    db.session.commit()
    return {
        "imported_rows": imported_rows,
        "total_quantity": total_qty,
        "unresolved": unresolved_names,
    }
