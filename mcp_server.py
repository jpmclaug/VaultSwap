"""Model Context Protocol (MCP) server for querying VaultSwap pod trade listings and card metadata."""

from __future__ import annotations

import argparse
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from flask import Flask, has_app_context
from sqlalchemy import case, func, or_

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - compatibility fallback for mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

import services
from card_utils import normalize_condition, normalize_finish
from config import Config
from models import BinderItem, Card, User, db
from providers.scryfall import SCRYFALL_BASE_URL, ScryfallProvider

logger = logging.getLogger(__name__)

PUBLIC_TRADE_STATUSES = ("for_trade", "meeple_bound")

mcp = MCPServer(
    "VaultSwap Trades",
    instructions=(
        "Read-only MCP server for browsing MTG cards that pod members have put up for trade "
        "('for_trade' or 'meeple_bound') and inspecting their Scryfall, pricing, Mighty Meeple, "
        "and trader metadata. Private binder items are strictly excluded."
    ),
)

_mcp_flask_app: Optional[Flask] = None


def _get_mcp_app() -> Flask:
    """Lazily initializes a lightweight Flask app configured for VaultSwap DB queries."""
    global _mcp_flask_app
    if _mcp_flask_app is None:
        flask_app = Flask(__name__)
        flask_app.config.from_object(Config)
        db.init_app(flask_app)
        _mcp_flask_app = flask_app
    return _mcp_flask_app


@contextmanager
def _ensure_app_context(app_instance: Optional[Flask] = None) -> Iterator[None]:
    """Ensures a Flask application context is active for SQLAlchemy queries."""
    if app_instance is not None:
        with app_instance.app_context():
            yield
    elif has_app_context():
        yield
    else:
        with _get_mcp_app().app_context():
            yield


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _public_trade_query():
    """Returns base SQLAlchemy query strictly scoped to non-private pod trade listings."""
    return (
        BinderItem.query.join(User, BinderItem.user_id == User.id)
        .join(Card, BinderItem.scryfall_id == Card.scryfall_id)
        .options(db.joinedload(BinderItem.user), db.joinedload(BinderItem.card))
        .filter(BinderItem.status.in_(PUBLIC_TRADE_STATUSES))
    )


def _priority_order_clause():
    """Orders meeple_bound cards first, then most recently updated listings."""
    return (
        case((BinderItem.status == "meeple_bound", 0), else_=1),
        BinderItem.updated_at.desc(),
        BinderItem.id.desc(),
    )


def serialize_trade_item(
    item: BinderItem,
    live_scryfall_data: Optional[Dict[str, Any]] = None,
    fallback_meeple_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Serializes a public BinderItem into structured listing, card, owner, and Mighty Meeple metadata.
    Refuses to serialize private items to uphold VaultSwap row-level privacy.
    """
    if item.status not in PUBLIC_TRADE_STATUSES:
        raise ValueError("Cannot serialize private binder item.")

    card = item.card
    owner = item.user
    qty = int(item.quantity or 1)

    unit_price_dec = item.market_price
    unit_market_price = round(float(unit_price_dec), 2) if unit_price_dec is not None else None
    line_market_value = item.line_market_value

    if item.mm_checked_at is not None:
        has_snapshot = True
        in_stock: Optional[bool] = bool(item.mm_in_stock) if item.mm_in_stock is not None else False
        retail_price = round(float(item.mm_retail_price), 2) if item.mm_retail_price is not None else None
        buylist_cash = round(float(item.mm_buylist_cash), 2) if item.mm_buylist_cash is not None else None
        buylist_credit = round(float(item.mm_buylist_credit), 2) if item.mm_buylist_credit is not None else None
        checked_at = _iso_utc(item.mm_checked_at)
    elif fallback_meeple_snapshot is not None:
        has_snapshot = True
        raw_in_stock = fallback_meeple_snapshot.get("in_stock")
        in_stock = bool(raw_in_stock) if raw_in_stock is not None else False
        raw_retail = fallback_meeple_snapshot.get("retail_price")
        raw_cash = fallback_meeple_snapshot.get("buylist_cash")
        raw_credit = fallback_meeple_snapshot.get("buylist_credit")
        retail_price = round(float(raw_retail), 2) if raw_retail is not None else None
        buylist_cash = round(float(raw_cash), 2) if raw_cash is not None else None
        buylist_credit = round(float(raw_credit), 2) if raw_credit is not None else None
        raw_checked = fallback_meeple_snapshot.get("checked_at")
        if isinstance(raw_checked, datetime):
            checked_at = _iso_utc(raw_checked)
        elif raw_checked:
            checked_at = str(raw_checked)
        else:
            checked_at = None
    else:
        has_snapshot = False
        in_stock = None
        retail_price = None
        buylist_cash = None
        buylist_credit = None
        checked_at = None

    line_buylist_cash = round(buylist_cash * qty, 2) if buylist_cash is not None else None
    line_buylist_credit = round(buylist_credit * qty, 2) if buylist_credit is not None else None

    scryfall_uri = (
        f"https://scryfall.com/card/{card.set_code.lower()}/{card.collector_number}"
        if card and card.set_code and card.collector_number
        else None
    )

    card_payload: Dict[str, Any] = {
        "scryfall_id": str(card.scryfall_id) if card else str(item.scryfall_id),
        "oracle_id": str(card.oracle_id) if card else None,
        "name": card.name if card else None,
        "set_code": card.set_code if card else None,
        "set_name": card.set_name if card else None,
        "collector_number": card.collector_number if card else None,
        "mana_cost": card.mana_cost if card else None,
        "type_line": card.type_line if card else None,
        "usd_price": round(float(card.usd_price), 2) if card and card.usd_price is not None else None,
        "usd_foil_price": round(float(card.usd_foil_price), 2) if card and card.usd_foil_price is not None else None,
        "image_uri_normal": card.image_uri_normal if card else None,
        "image_uri_small": card.image_uri_small if card else None,
        "scryfall_uri": scryfall_uri,
        "scryfall_updated_at": _iso_utc(card.scryfall_updated_at) if card else None,
    }
    if live_scryfall_data is not None:
        card_payload["live_details"] = live_scryfall_data

    owner_payload: Dict[str, Any] = {
        "user_id": owner.id if owner else item.user_id,
        "display_name": owner.display_name if owner else None,
        "handle_label": owner.handle_label if owner else None,
        "discord_handle": owner.discord_handle if owner else None,
    }

    return {
        "listing": {
            "item_id": item.id,
            "status": item.status,
            "quantity": qty,
            "finish": item.finish,
            "condition": item.condition or "NM",
            "unit_market_price": unit_market_price,
            "line_market_value": line_market_value,
            "created_at": _iso_utc(item.created_at),
            "updated_at": _iso_utc(item.updated_at),
        },
        "card": card_payload,
        "owner": owner_payload,
        "mighty_meeple": {
            "has_snapshot": has_snapshot,
            "in_stock": in_stock,
            "retail_price": retail_price,
            "buylist_cash": buylist_cash,
            "line_buylist_cash": line_buylist_cash,
            "buylist_credit": buylist_credit,
            "line_buylist_credit": line_buylist_credit,
            "checked_at": checked_at,
        },
    }


def fetch_live_scryfall_details(
    scryfall_id: str,
    provider: Optional[ScryfallProvider] = None,
) -> Optional[Dict[str, Any]]:
    """Fetches extended card oracle, legality, and EDHREC metadata from Scryfall by printing ID."""
    clean_sid = str(scryfall_id or "").strip().lower()
    if not clean_sid:
        return None

    sf = provider or ScryfallProvider()
    try:
        resp = sf._request("get", f"{SCRYFALL_BASE_URL}/cards/{clean_sid}", timeout=8)
        if resp is None or resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch live Scryfall details for %s: %s", clean_sid, exc)
        return None

    oracle_text = data.get("oracle_text")
    colors = data.get("colors")
    faces = data.get("card_faces") or []
    if not oracle_text and faces:
        face_texts = [f.get("oracle_text") for f in faces if f.get("oracle_text")]
        oracle_text = " // ".join(face_texts) if face_texts else None
    if colors is None and faces:
        colors = faces[0].get("colors")

    raw_legalities = data.get("legalities") or {}
    legal_formats = {fmt: leg for fmt, leg in raw_legalities.items() if leg == "legal"}

    return {
        "oracle_text": oracle_text,
        "cmc": data.get("cmc"),
        "colors": colors if colors is not None else [],
        "color_identity": data.get("color_identity") or [],
        "rarity": data.get("rarity"),
        "keywords": data.get("keywords") or [],
        "legalities": legal_formats or raw_legalities,
        "edhrec_rank": data.get("edhrec_rank"),
        "artist": data.get("artist"),
        "rulings_uri": data.get("rulings_uri"),
        "scryfall_uri": data.get("scryfall_uri"),
    }


def _summarize_items(items: List[BinderItem]) -> Dict[str, Any]:
    total_qty = 0
    for_trade_qty = 0
    meeple_bound_qty = 0
    total_market = 0.0
    total_cash = 0.0
    total_credit = 0.0

    for item in items:
        qty = int(item.quantity or 1)
        total_qty += qty
        total_market += item.line_market_value
        if item.status == "for_trade":
            for_trade_qty += qty
        elif item.status == "meeple_bound":
            meeple_bound_qty += qty
        if item.mm_buylist_cash is not None:
            total_cash += float(item.mm_buylist_cash) * qty
        if item.mm_buylist_credit is not None:
            total_credit += float(item.mm_buylist_credit) * qty

    return {
        "total_quantity": total_qty,
        "for_trade_quantity": for_trade_qty,
        "meeple_bound_quantity": meeple_bound_qty,
        "total_market_value": round(total_market, 2),
        "total_meeple_cash": round(total_cash, 2),
        "total_meeple_credit": round(total_credit, 2),
    }


@mcp.tool()
def list_trade_cards(
    query: Optional[str] = None,
    status: str = "all",
    owner: Optional[str] = None,
    set_code: Optional[str] = None,
    type_line: Optional[str] = None,
    finish: Optional[str] = None,
    condition: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    meeple_in_stock: Optional[bool] = None,
    sort_by: str = "priority",
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    List and filter MTG cards currently put up for trade by pod members ('for_trade' or 'meeple_bound'),
    returning full listing, Scryfall card, trader, and Mighty Meeple metadata.
    """
    clean_status = (status or "all").strip().lower()
    if clean_status not in ("all", "for_trade", "meeple_bound"):
        return {
            "error": (
                f"Invalid status '{status}'. Only public trade statuses "
                "'all', 'for_trade', and 'meeple_bound' are supported."
            ),
            "total_matching": 0,
            "returned_count": 0,
            "listings": [],
        }

    safe_limit = max(1, min(200, int(limit)))
    safe_offset = max(0, int(offset))
    clean_sort = (sort_by or "priority").strip().lower()

    with _ensure_app_context():
        q = _public_trade_query()

        if clean_status in PUBLIC_TRADE_STATUSES:
            q = q.filter(BinderItem.status == clean_status)

        if owner is not None and str(owner).strip():
            owner_clean = str(owner).strip().lstrip("@")
            owner_clauses = [
                User.display_name.ilike(f"%{owner_clean}%"),
                User.discord_handle.ilike(f"%{owner_clean}%"),
            ]
            if owner_clean.isdigit():
                owner_clauses.append(User.id == int(owner_clean))
            q = q.filter(or_(*owner_clauses))

        if set_code and set_code.strip():
            q = q.filter(func.upper(Card.set_code) == set_code.strip().upper())

        if type_line and type_line.strip():
            q = q.filter(Card.type_line.ilike(f"%{type_line.strip()}%"))

        if finish and finish.strip():
            q = q.filter(BinderItem.finish == normalize_finish(finish))

        if condition and condition.strip():
            cond_norm = normalize_condition(condition)
            q = q.filter(func.upper(func.coalesce(BinderItem.condition, "NM")) == cond_norm.upper())

        if meeple_in_stock is not None:
            q = q.filter(BinderItem.mm_in_stock.is_(bool(meeple_in_stock)))

        if query and query.strip():
            like_term = f"%{query.strip()}%"
            q = q.filter(
                or_(
                    Card.name.ilike(like_term),
                    Card.set_code.ilike(like_term),
                    Card.set_name.ilike(like_term),
                    Card.type_line.ilike(like_term),
                    User.display_name.ilike(like_term),
                    User.discord_handle.ilike(like_term),
                )
            )

        if clean_sort == "name_asc":
            q = q.order_by(Card.name.asc(), Card.set_code.asc(), BinderItem.id.asc())
        elif clean_sort == "updated_desc":
            q = q.order_by(BinderItem.updated_at.desc(), BinderItem.id.desc())
        else:
            q = q.order_by(*_priority_order_clause())

        items = q.all()

        if min_price is not None or max_price is not None:
            filtered: List[BinderItem] = []
            for item in items:
                price_dec = item.market_price
                if price_dec is None:
                    continue
                price_val = float(price_dec)
                if min_price is not None and price_val < float(min_price):
                    continue
                if max_price is not None and price_val > float(max_price):
                    continue
                filtered.append(item)
            items = filtered

        if clean_sort == "price_desc":
            items.sort(
                key=lambda it: (
                    it.market_price is None,
                    -(float(it.market_price) if it.market_price is not None else 0.0),
                    it.id,
                )
            )
        elif clean_sort == "price_asc":
            items.sort(
                key=lambda it: (
                    it.market_price is None,
                    float(it.market_price) if it.market_price is not None else 0.0,
                    it.id,
                )
            )

        total_matching = len(items)
        summary = _summarize_items(items)
        page_items = items[safe_offset : safe_offset + safe_limit]
        serialized = [serialize_trade_item(it) for it in page_items]

        return {
            "total_matching": total_matching,
            "returned_count": len(serialized),
            "offset": safe_offset,
            "limit": safe_limit,
            "summary": summary,
            "listings": serialized,
        }


@mcp.tool()
def get_trade_card_metadata(
    item_id: Optional[int] = None,
    scryfall_id: Optional[str] = None,
    card_name: Optional[str] = None,
    include_live_scryfall: bool = False,
    include_cached_meeple: bool = True,
) -> Dict[str, Any]:
    """
    Inspect detailed metadata for specific public trade listing(s) by item_id, scryfall_id, or card_name,
    including cross-printing alternatives available in the pod, optional 24h cached Mighty Meeple quotes,
    and optional live Scryfall oracle/legality details.
    """
    clean_sid = (scryfall_id or "").strip().lower()
    clean_name = (card_name or "").strip()

    if item_id is None and not clean_sid and not clean_name:
        return {
            "found": False,
            "error": "Provide item_id, scryfall_id, or card_name.",
            "listings": [],
            "other_printings_in_pod": [],
        }

    with _ensure_app_context():
        if item_id is not None:
            matched_items = _public_trade_query().filter(BinderItem.id == int(item_id)).all()
        elif clean_sid:
            matched_items = (
                _public_trade_query()
                .filter(BinderItem.scryfall_id == clean_sid)
                .order_by(*_priority_order_clause())
                .all()
            )
        else:
            matched_items = (
                _public_trade_query()
                .filter(func.lower(Card.name) == clean_name.lower())
                .order_by(*_priority_order_clause())
                .all()
            )
            if not matched_items:
                matched_items = (
                    _public_trade_query()
                    .filter(Card.name.ilike(f"%{clean_name}%"))
                    .order_by(*_priority_order_clause())
                    .all()
                )

        if not matched_items:
            return {
                "found": False,
                "match_count": 0,
                "total_quantity_available": 0,
                "listings": [],
                "other_printings_in_pod": [],
            }

        matched_ids = {m.id for m in matched_items}
        oracle_ids = list({str(m.card.oracle_id).lower() for m in matched_items if m.card and m.card.oracle_id})

        other_printings: List[BinderItem] = []
        if oracle_ids:
            other_printings = (
                _public_trade_query()
                .filter(Card.oracle_id.in_(oracle_ids), ~BinderItem.id.in_(matched_ids))
                .order_by(*_priority_order_clause())
                .all()
            )

        live_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        if include_live_scryfall:
            for item in matched_items:
                sid = str(item.scryfall_id).lower()
                if sid not in live_cache:
                    live_cache[sid] = fetch_live_scryfall_details(sid)

        def _fallback_meeple(item: BinderItem) -> Optional[Dict[str, Any]]:
            if not include_cached_meeple or item.mm_checked_at is not None or not item.card:
                return None
            return services.find_cached_meeple_snapshot(
                card_name=item.card.name,
                set_code=item.card.set_code,
                finish=item.finish,
                scryfall_id=item.scryfall_id,
            )

        serialized_matches = [
            serialize_trade_item(
                item,
                live_scryfall_data=live_cache.get(str(item.scryfall_id).lower()) if include_live_scryfall else None,
                fallback_meeple_snapshot=_fallback_meeple(item),
            )
            for item in matched_items
        ]
        serialized_others = [
            serialize_trade_item(item, fallback_meeple_snapshot=_fallback_meeple(item))
            for item in other_printings
        ]

        return {
            "found": True,
            "match_count": len(serialized_matches),
            "total_quantity_available": sum(m["listing"]["quantity"] for m in serialized_matches),
            "listings": serialized_matches,
            "other_printings_in_pod": serialized_others,
        }


@mcp.tool()
def list_pod_traders() -> Dict[str, Any]:
    """
    List all pod members who currently have cards marked 'for_trade' or 'meeple_bound',
    with their trader profile and aggregate trade binder valuations.
    """
    with _ensure_app_context():
        items = _public_trade_query().all()
        by_user: Dict[int, Dict[str, Any]] = {}

        for item in items:
            owner = item.user
            uid = owner.id if owner else item.user_id
            if uid not in by_user:
                by_user[uid] = {
                    "user_id": uid,
                    "display_name": owner.display_name if owner else None,
                    "handle_label": owner.handle_label if owner else None,
                    "discord_handle": owner.discord_handle if owner else None,
                    "total_listings": 0,
                    "total_quantity": 0,
                    "for_trade_listings": 0,
                    "for_trade_quantity": 0,
                    "meeple_bound_listings": 0,
                    "meeple_bound_quantity": 0,
                    "total_market_value": 0.0,
                    "total_meeple_cash": 0.0,
                    "total_meeple_credit": 0.0,
                }

            entry = by_user[uid]
            qty = int(item.quantity or 1)
            entry["total_listings"] += 1
            entry["total_quantity"] += qty
            entry["total_market_value"] += item.line_market_value

            if item.status == "for_trade":
                entry["for_trade_listings"] += 1
                entry["for_trade_quantity"] += qty
            elif item.status == "meeple_bound":
                entry["meeple_bound_listings"] += 1
                entry["meeple_bound_quantity"] += qty

            if item.mm_buylist_cash is not None:
                entry["total_meeple_cash"] += float(item.mm_buylist_cash) * qty
            if item.mm_buylist_credit is not None:
                entry["total_meeple_credit"] += float(item.mm_buylist_credit) * qty

        traders = list(by_user.values())
        for t in traders:
            t["total_market_value"] = round(t["total_market_value"], 2)
            t["total_meeple_cash"] = round(t["total_meeple_cash"], 2)
            t["total_meeple_credit"] = round(t["total_meeple_credit"], 2)

        traders.sort(key=lambda t: (-t["total_quantity"], (t["display_name"] or "").lower()))
        return {
            "trader_count": len(traders),
            "traders": traders,
        }


@mcp.tool()
def get_pod_trade_summary() -> Dict[str, Any]:
    """
    Return pod-wide trade statistics, aggregate valuations, top-value trade listings,
    and cards with multiple copies available across the pod.
    """
    with _ensure_app_context():
        items = _public_trade_query().all()

        for_trade_listings = 0
        for_trade_cards = 0
        meeple_bound_listings = 0
        meeple_bound_cards = 0
        total_market = 0.0
        total_cash = 0.0
        total_credit = 0.0
        trader_ids = set()
        oracle_groups: Dict[str, Dict[str, Any]] = {}

        for item in items:
            qty = int(item.quantity or 1)
            trader_ids.add(item.user_id)
            total_market += item.line_market_value

            if item.status == "for_trade":
                for_trade_listings += 1
                for_trade_cards += qty
            elif item.status == "meeple_bound":
                meeple_bound_listings += 1
                meeple_bound_cards += qty

            if item.mm_buylist_cash is not None:
                total_cash += float(item.mm_buylist_cash) * qty
            if item.mm_buylist_credit is not None:
                total_credit += float(item.mm_buylist_credit) * qty

            if item.card and item.card.oracle_id:
                oid = str(item.card.oracle_id).lower()
                if oid not in oracle_groups:
                    oracle_groups[oid] = {
                        "oracle_id": oid,
                        "name": item.card.name,
                        "total_quantity": 0,
                        "listing_count": 0,
                        "traders": set(),
                    }
                grp = oracle_groups[oid]
                grp["total_quantity"] += qty
                grp["listing_count"] += 1
                if item.user:
                    grp["traders"].add(item.user.handle_label)

        sorted_by_price = sorted(
            items,
            key=lambda it: (
                it.market_price is None,
                -(float(it.market_price) if it.market_price is not None else 0.0),
                it.id,
            ),
        )
        top_value_listings = [serialize_trade_item(it) for it in sorted_by_price[:10]]

        multi_copy_oracles = [
            {
                "oracle_id": grp["oracle_id"],
                "name": grp["name"],
                "total_quantity": grp["total_quantity"],
                "listing_count": grp["listing_count"],
                "traders": sorted(grp["traders"]),
            }
            for grp in oracle_groups.values()
            if grp["total_quantity"] > 1
        ]
        multi_copy_oracles.sort(key=lambda g: (-g["total_quantity"], (g["name"] or "").lower()))

        return {
            "total_listings": len(items),
            "total_cards_available": for_trade_cards + meeple_bound_cards,
            "for_trade_listings": for_trade_listings,
            "for_trade_cards": for_trade_cards,
            "meeple_bound_listings": meeple_bound_listings,
            "meeple_bound_cards": meeple_bound_cards,
            "active_traders": len(trader_ids),
            "total_market_value": round(total_market, 2),
            "total_meeple_cash": round(total_cash, 2),
            "total_meeple_credit": round(total_credit, 2),
            "top_value_listings": top_value_listings,
            "multi_copy_oracles": multi_copy_oracles,
        }


@mcp.resource("vaultswap://trades/feed", mime_type="application/json")
def trade_feed_resource() -> str:
    """JSON snapshot of all active public trade and meeple-bound listings."""
    return json.dumps(list_trade_cards(limit=200), indent=2)


@mcp.resource("vaultswap://trades/summary", mime_type="application/json")
def trade_summary_resource() -> str:
    """JSON overview of pod-wide trade pool statistics and top listings."""
    return json.dumps(get_pod_trade_summary(), indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the VaultSwap Trade & Card Metadata MCP Server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help="MCP transport protocol (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for HTTP/SSE transports (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for HTTP/SSE transports (default: 8000).",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
