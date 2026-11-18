"""Shared query builder, SQL grouping, pagination, and aggregates for VaultSwap."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import case, func, or_

from models import BinderItem, Card, User, db, utc_now

COLOR_MAP = {
    "w": "W",
    "u": "U",
    "b": "B",
    "r": "R",
    "g": "G",
    "multicolor": "Multicolor",
    "colorless": "Colorless",
    "land": "Land",
}

VALID_BINDER_FILTERS = ("all", "recent", "dupes", "for_trade", "meeple_bound")
VALID_POD_FILTERS = ("all", "meeple_bound", "for_trade")
VALID_VIEWS = ("rows", "art", "checklist")
VALID_GROUPS = ("none", "color", "type", "alpha", "set", "status", "owner")
VALID_SORTS = (
    "default",
    "price_desc",
    "price_asc",
    "total_value_desc",
    "meeple_credit_desc",
    "name_asc",
    "name_desc",
    "cmc_asc",
    "qty_desc",
    "set_asc",
)


@dataclass
class CardFilters:
    status: str = "all"
    q: str = ""
    color: str = "all"
    type: str = "all"
    finish: str = "all"
    sort: str = "default"
    group: str = "none"
    view: str = "rows"
    owner_id: Optional[int] = None
    page: int = 1
    prev_group: str = ""

    @classmethod
    def from_request(cls, src: Any, scope: str = "binder") -> "CardFilters":
        key = "status" if scope == "pod" else "filter"
        raw_status = (src.get(key) or "all").strip().lower()
        valid_statuses = VALID_POD_FILTERS if scope == "pod" else VALID_BINDER_FILTERS
        status = raw_status if raw_status in valid_statuses else "all"

        q = (src.get("q") or "").strip()
        color = (src.get("color") or "all").strip().lower()
        if color != "all" and color not in COLOR_MAP:
            color = "all"

        type_f = (src.get("type") or "all").strip().lower()
        finish = (src.get("finish") or "all").strip().lower()
        sort = (src.get("sort") or "default").strip().lower()
        if sort not in VALID_SORTS:
            sort = "default"

        group = (src.get("group") or "none").strip().lower()
        if group not in VALID_GROUPS:
            group = "none"

        view = (src.get("view") or "rows").strip().lower()
        if view not in VALID_VIEWS:
            view = "rows"

        owner_id: Optional[int] = None
        owner_raw = (src.get("owner_id") or "").strip()
        if owner_raw:
            try:
                owner_id = int(owner_raw)
            except (ValueError, TypeError):
                owner_id = None

        page = 1
        try:
            page = max(1, int(src.get("page") or 1))
        except (ValueError, TypeError):
            page = 1

        prev_group = (src.get("prev_group") or "").strip()

        return cls(
            status=status,
            q=q,
            color=color,
            type=type_f,
            finish=finish,
            sort=sort,
            group=group,
            view=view,
            owner_id=owner_id,
            page=page,
            prev_group=prev_group,
        )

    def to_query_params(self, scope: str = "binder", **overrides: Any) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            ("status" if scope == "pod" else "filter"): self.status,
            "q": self.q,
            "color": self.color,
            "type": self.type,
            "finish": self.finish,
            "sort": self.sort,
            "group": self.group,
            "view": self.view,
        }
        if scope == "pod" and self.owner_id is not None:
            params["owner_id"] = self.owner_id
        params.update(overrides)
        return {k: v for k, v in params.items() if v not in ("", None)}


def _market_price_expr():
    return case(
        (
            BinderItem.finish.in_(["foil", "etched"]),
            func.coalesce(Card.usd_foil_price, Card.usd_price, 0),
        ),
        else_=func.coalesce(Card.usd_price, Card.usd_foil_price, 0),
    )


def _group_order_expr(group: str):
    if group == "color":
        color_rank = case(
            (Card.color_bucket_col == "W", 0),
            (Card.color_bucket_col == "U", 1),
            (Card.color_bucket_col == "B", 2),
            (Card.color_bucket_col == "R", 3),
            (Card.color_bucket_col == "G", 4),
            (Card.color_bucket_col == "Multicolor", 5),
            (Card.color_bucket_col == "Colorless", 6),
            (Card.color_bucket_col == "Land", 7),
            else_=8,
        )
        return [color_rank.asc()]
    if group == "type":
        return [func.coalesce(Card.primary_type_col, "Other").asc()]
    if group == "alpha":
        return [func.upper(func.substr(Card.name, 1, 1)).asc()]
    if group == "set":
        return [func.upper(Card.set_code).asc()]
    if group == "status":
        status_rank = case(
            (BinderItem.status == "meeple_bound", 0),
            (BinderItem.status == "for_trade", 1),
            else_=2,
        )
        return [status_rank.asc()]
    if group == "owner":
        return [func.lower(User.display_name).asc()]
    return []


def group_key_and_label(item: BinderItem, group: str) -> Tuple[str, str]:
    """Returns (canonical_key, human_label) for rendering group headers."""
    if group == "color":
        cb = item.color_bucket or "Colorless"
        labels = {
            "W": "White (W)",
            "U": "Blue (U)",
            "B": "Black (B)",
            "R": "Red (R)",
            "G": "Green (G)",
            "Multicolor": "Multicolor",
            "Colorless": "Colorless",
            "Land": "Lands",
        }
        return cb, labels.get(cb, cb)
    if group == "type":
        pt = item.primary_card_type or "Other"
        return pt, pt
    if group == "alpha":
        ch = (item.card.name[0].upper() if item.card and item.card.name else "#")
        return ch, ch
    if group == "set":
        sc = (item.card.set_code or "").upper() if item.card else ""
        sn = item.card.set_name if item.card and item.card.set_name else sc
        return sc, f"{sc} — {sn}" if sn != sc else sc
    if group == "status":
        st = item.status or "private"
        labels = {
            "meeple_bound": "Meeple Bound",
            "for_trade": "For Trade",
            "private": "Private",
        }
        return st, labels.get(st, st)
    if group == "owner":
        name = item.user.display_name if item.user else "Unknown"
        return name, name
    return "", ""


def apply_card_filters(
    query,
    f: CardFilters,
    *,
    scope: str = "binder",
    user_id: Optional[int] = None,
    apply_order: bool = True,
):
    """Applies shared filtering and sorting to a BinderItem+Card(+User) SQLAlchemy query."""
    if scope == "binder":
        if user_id is not None:
            query = query.filter(BinderItem.user_id == user_id)
        if f.status == "recent":
            cutoff = utc_now() - timedelta(days=7)
            query = query.filter(BinderItem.created_at >= cutoff)
        elif f.status == "dupes":
            if user_id is not None:
                dupe_oracle_subq = (
                    db.session.query(Card.oracle_id)
                    .join(BinderItem, BinderItem.scryfall_id == Card.scryfall_id)
                    .filter(BinderItem.user_id == user_id)
                    .group_by(Card.oracle_id)
                    .having(func.coalesce(func.sum(BinderItem.quantity), 0) > 1)
                )
                query = query.filter(
                    or_(BinderItem.quantity > 1, Card.oracle_id.in_(dupe_oracle_subq))
                )
            else:
                query = query.filter(BinderItem.quantity > 1)
        elif f.status in ("for_trade", "meeple_bound"):
            query = query.filter(BinderItem.status == f.status)
    else:
        query = query.filter(BinderItem.status.in_(["for_trade", "meeple_bound"]))
        if f.status in ("meeple_bound", "for_trade"):
            query = query.filter(BinderItem.status == f.status)
        if f.owner_id is not None:
            query = query.filter(BinderItem.user_id == f.owner_id)

    if f.q:
        like_term = f"%{f.q}%"
        if scope == "pod":
            query = query.filter(
                or_(
                    Card.name.ilike(like_term),
                    Card.set_code.ilike(like_term),
                    Card.type_line.ilike(like_term),
                    User.display_name.ilike(like_term),
                )
            )
        else:
            query = query.filter(
                or_(
                    Card.name.ilike(like_term),
                    Card.set_code.ilike(like_term),
                    Card.type_line.ilike(like_term),
                )
            )

    if f.color != "all" and f.color in COLOR_MAP:
        query = query.filter(Card.color_bucket_col == COLOR_MAP[f.color])

    if f.finish in ("foil", "etched"):
        query = query.filter(BinderItem.finish == f.finish)
    elif f.finish in ("nonfoil", "normal"):
        query = query.filter(BinderItem.finish == "nonfoil")
    elif f.finish == "special":
        query = query.filter(BinderItem.finish.in_(["foil", "etched"]))

    t_clean = f.type.strip().lower()
    if t_clean and t_clean != "all":
        if t_clean in ("creature", "instant", "sorcery", "artifact", "enchantment", "planeswalker", "land"):
            query = query.filter(Card.type_line.ilike(f"%{t_clean}%"))
        elif t_clean == "other":
            query = query.filter(
                ~Card.type_line.ilike("%creature%"),
                ~Card.type_line.ilike("%instant%"),
                ~Card.type_line.ilike("%sorcery%"),
                ~Card.type_line.ilike("%artifact%"),
                ~Card.type_line.ilike("%enchantment%"),
                ~Card.type_line.ilike("%planeswalker%"),
                ~Card.type_line.ilike("%land%"),
            )

    if not apply_order:
        return query

    order_clauses: List[Any] = []
    if f.group != "none":
        order_clauses.extend(_group_order_expr(f.group))

    mkt_expr = _market_price_expr()
    if f.sort == "price_desc":
        order_clauses.extend([mkt_expr.desc(), Card.name.asc(), BinderItem.id.asc()])
    elif f.sort == "price_asc":
        asc_price = case(
            (
                BinderItem.finish.in_(["foil", "etched"]),
                func.coalesce(Card.usd_foil_price, Card.usd_price, 999999),
            ),
            else_=func.coalesce(Card.usd_price, Card.usd_foil_price, 999999),
        )
        order_clauses.extend([asc_price.asc(), Card.name.asc(), BinderItem.id.asc()])
    elif f.sort == "total_value_desc":
        order_clauses.extend([(mkt_expr * BinderItem.quantity).desc(), Card.name.asc(), BinderItem.id.asc()])
    elif f.sort == "meeple_credit_desc":
        order_clauses.extend([func.coalesce(BinderItem.mm_buylist_credit, 0).desc(), Card.name.asc(), BinderItem.id.asc()])
    elif f.sort == "name_asc":
        order_clauses.extend([Card.name.asc(), Card.set_code.asc(), BinderItem.id.asc()])
    elif f.sort == "name_desc":
        order_clauses.extend([Card.name.desc(), Card.set_code.asc(), BinderItem.id.asc()])
    elif f.sort == "cmc_asc":
        order_clauses.extend([func.coalesce(Card.cmc, 0).asc(), Card.name.asc(), BinderItem.id.asc()])
    elif f.sort == "qty_desc":
        order_clauses.extend([BinderItem.quantity.desc(), Card.name.asc(), BinderItem.id.asc()])
    elif f.sort == "set_asc":
        order_clauses.extend([Card.set_code.asc(), Card.collector_number.asc(), BinderItem.id.asc()])
    else:
        if scope == "pod":
            priority_order = case((BinderItem.status == "meeple_bound", 0), else_=1)
            order_clauses.extend([priority_order, BinderItem.updated_at.desc(), BinderItem.id.desc()])
        elif f.status == "dupes":
            order_clauses.extend([Card.name.asc(), Card.set_code.asc(), BinderItem.id.asc()])
        elif f.status in ("for_trade", "meeple_bound"):
            order_clauses.extend([BinderItem.updated_at.desc(), BinderItem.id.desc()])
        else:
            order_clauses.extend([BinderItem.created_at.desc(), BinderItem.id.desc()])

    return query.order_by(*order_clauses)


def paginate(query, page: int, per_page: int = 60) -> Tuple[List[BinderItem], bool]:
    """Fetches one page plus 1 lookahead row to detect whether more pages exist."""
    safe_page = max(1, int(page))
    offset = (safe_page - 1) * per_page
    rows = query.offset(offset).limit(per_page + 1).all()
    has_more = len(rows) > per_page
    return rows[:per_page], has_more


def compute_binder_stats_sql(user_id: int) -> Dict[str, Any]:
    """Computes summary counters and valuations for a user's binder in a single SQL query."""
    qty_expr = func.coalesce(BinderItem.quantity, 1)
    mkt_expr = _market_price_expr()

    row = (
        db.session.query(
            func.count(BinderItem.id),
            func.coalesce(func.sum(qty_expr), 0),
            func.coalesce(func.sum(case((BinderItem.status == "for_trade", qty_expr), else_=0)), 0),
            func.coalesce(func.sum(case((BinderItem.status == "meeple_bound", qty_expr), else_=0)), 0),
            func.coalesce(func.sum(mkt_expr * qty_expr), 0),
            func.coalesce(
                func.sum(
                    case(
                        (BinderItem.status == "meeple_bound", func.coalesce(BinderItem.mm_buylist_cash, 0) * qty_expr),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (BinderItem.status == "meeple_bound", func.coalesce(BinderItem.mm_buylist_credit, 0) * qty_expr),
                        else_=0,
                    )
                ),
                0,
            ),
        )
        .join(Card, BinderItem.scryfall_id == Card.scryfall_id)
        .filter(BinderItem.user_id == user_id)
        .one()
    )

    return {
        "unique_rows": int(row[0] or 0),
        "total_cards": int(row[1] or 0),
        "for_trade_qty": int(row[2] or 0),
        "meeple_qty": int(row[3] or 0),
        "total_market_value": round(float(row[4] or 0.0), 2),
        "meeple_cash_total": round(float(row[5] or 0.0), 2),
        "meeple_credit_total": round(float(row[6] or 0.0), 2),
    }


def compute_oracle_dupe_map(
    user_id: int,
    oracle_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Dict[str, int]]:
    """
    Groups a user's binder items by Card.oracle_id so cross-printing duplicates are identified.
    When `oracle_ids` is provided, scopes the query to only those oracle IDs (O(page_size)).
    """
    if oracle_ids is not None:
        oid_list = list({str(oid) for oid in oracle_ids if oid})
        if not oid_list:
            return {}
    else:
        oid_list = None

    query = (
        db.session.query(
            Card.oracle_id,
            func.coalesce(func.sum(BinderItem.quantity), 0),
            func.count(BinderItem.id),
        )
        .join(BinderItem, BinderItem.scryfall_id == Card.scryfall_id)
        .filter(BinderItem.user_id == user_id)
    )
    if oid_list is not None:
        query = query.filter(Card.oracle_id.in_(oid_list))

    rows = query.group_by(Card.oracle_id).all()
    return {
        str(oracle_id): {"total_qty": int(total_qty or 0), "printings": int(printing_count or 0)}
        for oracle_id, total_qty, printing_count in rows
    }
