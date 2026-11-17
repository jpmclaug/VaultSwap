"""SQLAlchemy ORM models for VaultSwap."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Numeric, Text, Boolean, Integer, String, DateTime, ForeignKey, Index, desc, text
from sqlalchemy.types import TypeDecorator, Uuid

db = SQLAlchemy()


def utc_now() -> datetime:
    """Returns timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class StringUUID(TypeDecorator):
    """Stores UUID natively on PostgreSQL and as CHAR(32) on SQLite, returning canonical lowercase strings."""

    impl = Uuid(as_uuid=False)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(uuid.UUID(str(value).strip()))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return str(value)


class User(db.Model):
    """Pod member authenticated via Google OAuth 2.0."""

    __tablename__ = "users"

    id = db.Column(Integer, primary_key=True)
    google_sub = db.Column(String(255), unique=True, nullable=False)
    email = db.Column(String(255), unique=True, nullable=False)
    display_name = db.Column(String(100), nullable=False)
    avatar_url = db.Column(Text, nullable=True)
    discord_handle = db.Column(String(100), nullable=True)
    created_at = db.Column(DateTime(timezone=True), default=utc_now, nullable=False)

    binder_items = db.relationship(
        "BinderItem",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def handle_label(self) -> str:
        """Short handle label for mobile pod badges."""
        first = (self.display_name or "").strip().split(" ")[0]
        return f"@{first or self.email.split('@')[0]}"

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"


class Card(db.Model):
    """Canonical card printing and Scryfall metadata cache shared across all pod members."""

    __tablename__ = "cards"

    scryfall_id = db.Column(StringUUID, primary_key=True)
    oracle_id = db.Column(StringUUID, nullable=False, index=True)
    name = db.Column(String(255), nullable=False)
    set_code = db.Column(String(10), nullable=False)
    collector_number = db.Column(String(20), nullable=False)
    set_name = db.Column(String(255), nullable=True)
    image_uri_normal = db.Column(Text, nullable=True)
    image_uri_small = db.Column(Text, nullable=True)
    mana_cost = db.Column(String(100), nullable=True)
    type_line = db.Column(String(255), nullable=True)
    usd_price = db.Column(Numeric(10, 2), nullable=True)
    usd_foil_price = db.Column(Numeric(10, 2), nullable=True)
    scryfall_updated_at = db.Column(DateTime(timezone=True), default=utc_now, nullable=False)

    binder_items = db.relationship("BinderItem", back_populates="card")

    def market_price_for(self, finish: str = "nonfoil") -> Decimal | None:
        """Returns Scryfall market price corresponding to card finish, with nonfoil fallback."""
        fin = (finish or "nonfoil").lower().strip()
        if fin in ("foil", "etched"):
            return self.usd_foil_price if self.usd_foil_price is not None else self.usd_price
        return self.usd_price if self.usd_price is not None else self.usd_foil_price

    def __repr__(self) -> str:
        return f"<Card {self.name!r} [{self.set_code} #{self.collector_number}]>"


class BinderItem(db.Model):
    """User-owned card entry; private by default with selective pod trade & Mighty Meeple tagging."""

    __tablename__ = "binder_items"

    VALID_STATUSES = ("private", "for_trade", "meeple_bound")
    VALID_FINISHES = ("nonfoil", "foil", "etched")

    id = db.Column(Integer, primary_key=True)
    user_id = db.Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scryfall_id = db.Column(StringUUID, ForeignKey("cards.scryfall_id"), nullable=False)
    quantity = db.Column(Integer, nullable=False, default=1)
    finish = db.Column(String(20), nullable=False, default="nonfoil")
    condition = db.Column(String(10), nullable=True, default="NM")

    # Visibility & Trade Status
    status = db.Column(String(20), nullable=False, default="private")

    # Point-in-Time Mighty Meeple Snapshot (Populated ONLY on tag)
    mm_in_stock = db.Column(Boolean, nullable=True)
    mm_retail_price = db.Column(Numeric(10, 2), nullable=True)
    mm_buylist_cash = db.Column(Numeric(10, 2), nullable=True)
    mm_buylist_credit = db.Column(Numeric(10, 2), nullable=True)
    mm_checked_at = db.Column(DateTime(timezone=True), nullable=True)

    created_at = db.Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = db.Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    user = db.relationship("User", back_populates="binder_items")
    card = db.relationship("Card", back_populates="binder_items")

    __table_args__ = (
        Index("idx_binder_user_recent", "user_id", desc("created_at")),
        Index(
            "idx_binder_pod_feed",
            "status",
            desc("updated_at"),
            postgresql_where=text("status != 'private'"),
            sqlite_where=text("status != 'private'"),
        ),
    )

    @property
    def market_price(self) -> Decimal | None:
        if not self.card:
            return None
        return self.card.market_price_for(self.finish)

    @property
    def line_market_value(self) -> float:
        price = self.market_price
        if price is None:
            return 0.0
        return round(float(price) * (self.quantity or 1), 2)

    @property
    def is_public(self) -> bool:
        return self.status in ("for_trade", "meeple_bound")

    @property
    def has_meeple_snapshot(self) -> bool:
        return self.mm_checked_at is not None

    def __repr__(self) -> str:
        return f"<BinderItem id={self.id} user={self.user_id} status={self.status!r}>"
