"""Flask application factory and routes for VaultSwap."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple
from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
import requests
from sqlalchemy import case, func, or_

import services
from config import Config
from models import BinderItem, Card, User, db, utc_now
from providers.scryfall import ScryfallProvider

try:
    from authlib.integrations.flask_client import OAuth
except ImportError:
    OAuth = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _compute_oracle_dupe_map(user_id: int) -> Dict[str, Dict[str, int]]:
    """
    Groups a user's binder items by Card.oracle_id so cross-printing duplicates
    (e.g. 3 copies of Rhystic Study across different sets) are identified.
    """
    rows = (
        db.session.query(
            Card.oracle_id,
            func.coalesce(func.sum(BinderItem.quantity), 0),
            func.count(BinderItem.id),
        )
        .join(BinderItem, BinderItem.scryfall_id == Card.scryfall_id)
        .filter(BinderItem.user_id == user_id)
        .group_by(Card.oracle_id)
        .all()
    )
    return {
        str(oracle_id): {"total_qty": int(total_qty or 0), "printings": int(printing_count or 0)}
        for oracle_id, total_qty, printing_count in rows
    }


def _compute_binder_stats(user_id: int) -> Dict[str, Any]:
    """Computes summary counters and valuations for a user's private binder."""
    items = (
        BinderItem.query.options(db.joinedload(BinderItem.card))
        .filter(BinderItem.user_id == user_id)
        .all()
    )
    total_cards = 0
    for_trade_qty = 0
    meeple_qty = 0
    total_market_value = 0.0
    meeple_cash_total = 0.0
    meeple_credit_total = 0.0

    for item in items:
        qty = int(item.quantity or 1)
        total_cards += qty
        total_market_value += item.line_market_value
        if item.status == "for_trade":
            for_trade_qty += qty
        elif item.status == "meeple_bound":
            meeple_qty += qty
            if item.mm_buylist_cash is not None:
                meeple_cash_total += float(item.mm_buylist_cash) * qty
            if item.mm_buylist_credit is not None:
                meeple_credit_total += float(item.mm_buylist_credit) * qty

    return {
        "unique_rows": len(items),
        "total_cards": total_cards,
        "for_trade_qty": for_trade_qty,
        "meeple_qty": meeple_qty,
        "total_market_value": round(total_market_value, 2),
        "meeple_cash_total": round(meeple_cash_total, 2),
        "meeple_credit_total": round(meeple_credit_total, 2),
    }


def _query_user_binder(
    user_id: int,
    active_filter: str = "all",
    search_q: str = "",
) -> Tuple[List[BinderItem], Dict[str, Dict[str, int]]]:
    """Queries user's private binder strictly scoped to `user_id`."""
    oracle_dupe_map = _compute_oracle_dupe_map(user_id)

    query = (
        BinderItem.query.join(Card, BinderItem.scryfall_id == Card.scryfall_id)
        .options(db.joinedload(BinderItem.card))
        .filter(BinderItem.user_id == user_id)
    )

    if search_q:
        like_term = f"%{search_q.strip()}%"
        query = query.filter(
            or_(
                Card.name.ilike(like_term),
                Card.set_code.ilike(like_term),
                Card.type_line.ilike(like_term),
            )
        )

    if active_filter == "recent":
        cutoff = utc_now() - timedelta(days=7)
        query = query.filter(BinderItem.created_at >= cutoff).order_by(
            BinderItem.created_at.desc(), BinderItem.id.desc()
        )
    elif active_filter == "dupes":
        dupe_oracles = [
            oid for oid, info in oracle_dupe_map.items() if info["total_qty"] > 1
        ]
        if dupe_oracles:
            query = query.filter(
                or_(BinderItem.quantity > 1, Card.oracle_id.in_(dupe_oracles))
            ).order_by(Card.name.asc(), Card.set_code.asc(), BinderItem.id.asc())
        else:
            query = query.filter(BinderItem.quantity > 1)
    elif active_filter == "for_trade":
        query = query.filter(BinderItem.status == "for_trade").order_by(
            BinderItem.updated_at.desc(), BinderItem.id.desc()
        )
    elif active_filter == "meeple_bound":
        query = query.filter(BinderItem.status == "meeple_bound").order_by(
            BinderItem.updated_at.desc(), BinderItem.id.desc()
        )
    else:
        query = query.order_by(BinderItem.created_at.desc(), BinderItem.id.desc())

    return query.all(), oracle_dupe_map


def create_app(test_config: Optional[Dict[str, Any]] = None) -> Flask:
    """Application factory for VaultSwap."""
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)

    db.init_app(app)

    meeple_worker = services.MeepleBackgroundWorker()
    meeple_worker.init_app(app)
    app.meeple_worker = meeple_worker  # type: ignore[attr-defined]

    # Configure Google OAuth 2.0 via Authlib if available
    google_oauth_client = None
    if OAuth is not None and app.config.get("GOOGLE_CLIENT_ID") and app.config.get("GOOGLE_CLIENT_SECRET"):
        oauth = OAuth(app)
        google_oauth_client = oauth.register(
            name="google",
            client_id=app.config["GOOGLE_CLIENT_ID"],
            client_secret=app.config["GOOGLE_CLIENT_SECRET"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    with app.app_context():
        db.create_all()

    def get_current_user() -> Optional[User]:
        user_id = session.get("user_id")
        if not user_id:
            return None
        if not hasattr(g, "current_user") or g.current_user is None or g.current_user.id != user_id:
            g.current_user = db.session.get(User, user_id)
        return g.current_user

    def dev_login_allowed() -> bool:
        return bool(
            app.config.get("TESTING")
            or app.config.get("DEV_LOGIN_ENABLED")
            or not app.config.get("GOOGLE_CLIENT_ID")
        )

    def login_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = get_current_user()
            if not user:
                session.clear()
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Authentication required."}), 401
                return redirect(url_for("login_page", next=request.url))
            return fn(*args, **kwargs)

        return wrapper

    @app.context_processor
    def inject_template_globals():
        return {
            "current_user": get_current_user(),
            "dev_login_allowed": dev_login_allowed(),
            "pending_meeple_count": meeple_worker.pending_count(),
        }

    # -------------------------------------------------------------------------
    # Authentication & Profile Routes
    # -------------------------------------------------------------------------

    def _upsert_google_user(
        google_sub: str,
        email: str,
        display_name: str,
        avatar_url: Optional[str] = None,
    ) -> User:
        clean_email = email.strip().lower()
        user = User.query.filter_by(google_sub=google_sub).first()
        if user is None:
            user = User.query.filter(func.lower(User.email) == clean_email).first()

        if user is None:
            user = User(
                google_sub=google_sub,
                email=clean_email,
                display_name=display_name.strip()[:100] or clean_email.split("@")[0],
                avatar_url=avatar_url,
            )
            db.session.add(user)
        else:
            user.google_sub = google_sub
            user.email = clean_email
            if display_name and display_name.strip():
                user.display_name = display_name.strip()[:100]
            if avatar_url:
                user.avatar_url = avatar_url

        db.session.commit()
        return user

    @app.route("/login")
    def login_page():
        if get_current_user():
            return redirect(url_for("binder_view"))
        return render_template(
            "login.html",
            next_url=request.args.get("next", "/"),
            google_configured=bool(app.config.get("GOOGLE_CLIENT_ID")),
            dev_login_allowed=dev_login_allowed(),
        )

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect(url_for("login_page"))

    def _google_redirect_uri() -> str:
        base_url = app.config.get("APP_BASE_URL")
        if base_url:
            return f"{base_url}/auth/google/callback"
        is_https = request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure
        return url_for("google_auth_callback", _external=True, _scheme="https" if is_https else "http")

    @app.route("/auth/google")
    def google_auth():
        client_id = app.config.get("GOOGLE_CLIENT_ID")
        if not client_id:
            flash("Google OAuth is not configured on this server.", "error")
            return redirect(url_for("login_page"))

        redirect_uri = _google_redirect_uri()
        session["oauth_next"] = request.args.get("next", "/")
        session["oauth_redirect_uri"] = redirect_uri

        if google_oauth_client is not None:
            return google_oauth_client.authorize_redirect(redirect_uri)

        state = secrets.token_urlsafe(32)
        session["oauth_state"] = state
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "prompt": "select_account",
        }
        prep = requests.Request("GET", "https://accounts.google.com/o/oauth2/v2/auth", params=params).prepare()
        return redirect(prep.url)

    @app.route("/auth/google/callback")
    def google_auth_callback():
        next_url = session.pop("oauth_next", "/")
        redirect_uri = session.pop("oauth_redirect_uri", None) or _google_redirect_uri()

        try:
            if google_oauth_client is not None:
                token = google_oauth_client.authorize_access_token()
                profile = token.get("userinfo") or google_oauth_client.userinfo()
            else:
                state_received = request.args.get("state")
                state_expected = session.pop("oauth_state", None)
                if not state_received or not state_expected or state_received != state_expected:
                    flash("Invalid OAuth state token. Please try signing in again.", "error")
                    return redirect(url_for("login_page"))

                code = request.args.get("code")
                if not code:
                    flash("Google sign-in was cancelled or denied.", "error")
                    return redirect(url_for("login_page"))

                token_resp = requests.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "code": code,
                        "client_id": app.config.get("GOOGLE_CLIENT_ID"),
                        "client_secret": app.config.get("GOOGLE_CLIENT_SECRET"),
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                    },
                    timeout=10,
                )
                if token_resp.status_code != 200:
                    flash("Failed to exchange Google OAuth authorization code.", "error")
                    return redirect(url_for("login_page"))

                access_token = token_resp.json().get("access_token")
                userinfo_resp = requests.get(
                    "https://www.googleapis.com/oauth2/v3/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=10,
                )
                if userinfo_resp.status_code != 200:
                    flash("Could not retrieve Google account profile.", "error")
                    return redirect(url_for("login_page"))
                profile = userinfo_resp.json()

            email = (profile.get("email") or "").strip().lower()
            google_sub = str(profile.get("sub") or email).strip()
            if not email or not google_sub:
                flash("Google account did not return an email address.", "error")
                return redirect(url_for("login_page"))

            user = _upsert_google_user(
                google_sub=google_sub,
                email=email,
                display_name=profile.get("name") or email.split("@")[0],
                avatar_url=profile.get("picture"),
            )
            session.permanent = True
            session["user_id"] = user.id
            return redirect(next_url or url_for("binder_view"))
        except Exception as exc:
            logger.error("Google OAuth callback error: %s", exc, exc_info=True)
            flash("Authentication handshake failed. Please retry.", "error")
            return redirect(url_for("login_page"))

    @app.route("/auth/dev-login", methods=["POST"])
    def dev_login():
        if not dev_login_allowed():
            return jsonify({"error": "Dev login is disabled in production."}), 403

        data = request.get_json(silent=True) or request.form or {}
        email = (data.get("email") or "").strip().lower()
        if not email:
            flash("Email is required for local dev sign-in.", "error")
            return redirect(url_for("login_page"))

        display_name = (data.get("display_name") or email.split("@")[0].title()).strip()
        user = _upsert_google_user(
            google_sub=f"dev-{email}",
            email=email,
            display_name=display_name,
            avatar_url=None,
        )
        if data.get("discord_handle"):
            user.discord_handle = str(data["discord_handle"]).strip()[:100]
            db.session.commit()

        session.permanent = True
        session["user_id"] = user.id
        return redirect(url_for("binder_view"))

    @app.route("/auth/seed-demo-pod", methods=["POST"])
    def seed_demo_pod():
        """Seeds deterministic offline sample cards for local mobile UI exploration."""
        if not dev_login_allowed():
            return jsonify({"error": "Demo seeding disabled in production."}), 403

        jason = _upsert_google_user("dev-jason@vaultswap.local", "jason@vaultswap.local", "Jason", None)
        if not jason.discord_handle:
            jason.discord_handle = "@jason_mtg"
        alex = _upsert_google_user("dev-alex@vaultswap.local", "alex@vaultswap.local", "Alex", None)
        if not alex.discord_handle:
            alex.discord_handle = "@alex_edh"

        sample_cards = [
            {
                "scryfall_id": "11111111-1111-4111-8111-111111111101",
                "oracle_id": "aaaaaaa1-1111-4111-8111-111111111101",
                "name": "Rhystic Study",
                "set_code": "WOT",
                "set_name": "Wilds of Eldraine: Enchanting Tales",
                "collector_number": "25",
                "image_uri_normal": "https://cards.scryfall.io/normal/front/d/6/d6914dba-0d27-4055-ac34-b3ebf5802221.jpg",
                "image_uri_small": "https://cards.scryfall.io/small/front/d/6/d6914dba-0d27-4055-ac34-b3ebf5802221.jpg",
                "mana_cost": "{2}{U}",
                "type_line": "Enchantment",
                "usd_price": 42.50,
                "usd_foil_price": 68.00,
            },
            {
                "scryfall_id": "11111111-1111-4111-8111-111111111102",
                "oracle_id": "aaaaaaa1-1111-4111-8111-111111111101",
                "name": "Rhystic Study",
                "set_code": "PRY",
                "set_name": "Prophecy",
                "collector_number": "45",
                "image_uri_normal": "https://cards.scryfall.io/normal/front/d/6/d6914dba-0d27-4055-ac34-b3ebf5802221.jpg",
                "image_uri_small": "https://cards.scryfall.io/small/front/d/6/d6914dba-0d27-4055-ac34-b3ebf5802221.jpg",
                "mana_cost": "{2}{U}",
                "type_line": "Enchantment",
                "usd_price": 44.00,
                "usd_foil_price": 190.00,
            },
            {
                "scryfall_id": "11111111-1111-4111-8111-111111111103",
                "oracle_id": "aaaaaaa2-1111-4111-8111-111111111102",
                "name": "Sol Ring",
                "set_code": "CMM",
                "set_name": "Commander Masters",
                "collector_number": "410",
                "image_uri_normal": "https://cards.scryfall.io/normal/front/e/e/ee6e5a35-fe21-4dee-b0ef-a8f2841511ad.jpg",
                "image_uri_small": "https://cards.scryfall.io/small/front/e/e/ee6e5a35-fe21-4dee-b0ef-a8f2841511ad.jpg",
                "mana_cost": "{1}",
                "type_line": "Artifact",
                "usd_price": 1.85,
                "usd_foil_price": 4.20,
            },
            {
                "scryfall_id": "11111111-1111-4111-8111-111111111104",
                "oracle_id": "aaaaaaa3-1111-4111-8111-111111111103",
                "name": "The One Ring",
                "set_code": "LTR",
                "set_name": "The Lord of the Rings: Tales of Middle-earth",
                "collector_number": "246",
                "image_uri_normal": "https://cards.scryfall.io/normal/front/d/5/d5806e68-1054-458e-866d-1f2470f682b2.jpg",
                "image_uri_small": "https://cards.scryfall.io/small/front/d/5/d5806e68-1054-458e-866d-1f2470f682b2.jpg",
                "mana_cost": "{4}",
                "type_line": "Legendary Artifact",
                "usd_price": 69.00,
                "usd_foil_price": 88.00,
            },
            {
                "scryfall_id": "11111111-1111-4111-8111-111111111105",
                "oracle_id": "aaaaaaa4-1111-4111-8111-111111111104",
                "name": "Cyclonic Rift",
                "set_code": "RVR",
                "set_name": "Ravnica Remastered",
                "collector_number": "40",
                "image_uri_normal": "https://cards.scryfall.io/normal/front/d/f/dfb7c4b9-f2f4-4d4e-baf2-86551c8150fe.jpg",
                "image_uri_small": "https://cards.scryfall.io/small/front/d/f/dfb7c4b9-f2f4-4d4e-baf2-86551c8150fe.jpg",
                "mana_cost": "{1}{U}",
                "type_line": "Instant",
                "usd_price": 31.25,
                "usd_foil_price": 41.00,
            },
        ]

        for sc in sample_cards:
            if not db.session.get(Card, sc["scryfall_id"]):
                db.session.add(Card(**sc))
        db.session.flush()

        now = utc_now()
        if BinderItem.query.filter_by(user_id=jason.id).count() == 0:
            db.session.add_all([
                BinderItem(
                    user_id=jason.id,
                    scryfall_id="11111111-1111-4111-8111-111111111101",
                    quantity=2,
                    finish="nonfoil",
                    condition="NM",
                    status="for_trade",
                    mm_in_stock=True,
                    mm_retail_price=44.99,
                    mm_buylist_cash=24.00,
                    mm_buylist_credit=31.20,
                    mm_checked_at=now,
                ),
                BinderItem(
                    user_id=jason.id,
                    scryfall_id="11111111-1111-4111-8111-111111111102",
                    quantity=1,
                    finish="nonfoil",
                    condition="LP",
                    status="private",
                ),
                BinderItem(
                    user_id=jason.id,
                    scryfall_id="11111111-1111-4111-8111-111111111103",
                    quantity=3,
                    finish="foil",
                    condition="NM",
                    status="private",
                ),
            ])

        if BinderItem.query.filter_by(user_id=alex.id).count() == 0:
            db.session.add_all([
                BinderItem(
                    user_id=alex.id,
                    scryfall_id="11111111-1111-4111-8111-111111111104",
                    quantity=1,
                    finish="nonfoil",
                    condition="NM",
                    status="meeple_bound",
                    mm_in_stock=False,
                    mm_retail_price=72.99,
                    mm_buylist_cash=42.00,
                    mm_buylist_credit=54.60,
                    mm_checked_at=now,
                ),
                BinderItem(
                    user_id=alex.id,
                    scryfall_id="11111111-1111-4111-8111-111111111105",
                    quantity=1,
                    finish="foil",
                    condition="NM",
                    status="for_trade",
                    mm_in_stock=True,
                    mm_retail_price=39.99,
                    mm_buylist_cash=20.00,
                    mm_buylist_credit=26.00,
                    mm_checked_at=now,
                ),
            ])

        db.session.commit()
        session.permanent = True
        session["user_id"] = jason.id
        flash("Demo pod seeded and signed in as Jason.", "success")
        return redirect(url_for("binder_view"))

    @app.route("/profile", methods=["POST"])
    @login_required
    def update_profile():
        user = get_current_user()
        display_name = (request.form.get("display_name") or "").strip()
        discord_handle = (request.form.get("discord_handle") or "").strip()
        if display_name:
            user.display_name = display_name[:100]
        user.discord_handle = discord_handle[:100] or None
        db.session.commit()
        flash("Pod profile updated.", "success")
        return redirect(request.referrer or url_for("binder_view"))

    # -------------------------------------------------------------------------
    # My Binder (Private, Row-Level Scoped) & HTMX Status Routes
    # -------------------------------------------------------------------------

    @app.route("/")
    @app.route("/binder")
    @login_required
    def binder_view():
        user = get_current_user()
        active_filter = (request.args.get("filter") or "all").strip().lower()
        if active_filter not in ("all", "recent", "dupes", "for_trade", "meeple_bound"):
            active_filter = "all"
        search_q = (request.args.get("q") or "").strip()

        items, oracle_dupe_map = _query_user_binder(user.id, active_filter=active_filter, search_q=search_q)
        stats = _compute_binder_stats(user.id)

        context = {
            "items": items,
            "oracle_dupe_map": oracle_dupe_map,
            "stats": stats,
            "active_filter": active_filter,
            "search_q": search_q,
            "is_pending_fn": meeple_worker.is_pending,
            "active_tab": "binder",
        }
        if request.headers.get("HX-Request"):
            return render_template("partials/binder_list.html", **context)
        return render_template("binder.html", **context)

    @app.route("/binder/<int:item_id>/status", methods=["POST"])
    @login_required
    def update_item_status(item_id: int):
        user = get_current_user()
        new_status = (request.form.get("status") or (request.get_json(silent=True) or {}).get("status") or "").strip()
        if new_status not in BinderItem.VALID_STATUSES:
            return jsonify({"error": f"Invalid status '{new_status}'."}), 400

        item = BinderItem.query.filter_by(id=item_id, user_id=user.id).first_or_404()
        item.status = new_status
        item.updated_at = utc_now()

        # Only query Mighty Meeple if tagging for trade/buylist AND never checked before
        if new_status in ("for_trade", "meeple_bound") and item.mm_checked_at is None:
            mm_data = services.get_meeple_snapshot_cached_or_live(
                card_name=item.card.name,
                set_code=item.card.set_code,
                finish=item.finish,
                scryfall_id=item.scryfall_id,
                collector_number=item.card.collector_number,
                set_name=item.card.set_name,
                condition=item.condition or "NM",
            )
            if mm_data:
                services.apply_meeple_snapshot(item, mm_data)

        db.session.commit()
        oracle_dupe_map = _compute_oracle_dupe_map(user.id)
        return render_template(
            "partials/binder_card_row.html",
            item=item,
            oracle_dupe_map=oracle_dupe_map,
            is_pending=meeple_worker.is_pending(item.id),
        )

    @app.route("/binder/<int:item_id>/refresh-meeple", methods=["POST"])
    @login_required
    def refresh_item_meeple(item_id: int):
        user = get_current_user()
        item = BinderItem.query.filter_by(id=item_id, user_id=user.id).first_or_404()

        mm_data = services.get_meeple_snapshot_cached_or_live(
            card_name=item.card.name,
            set_code=item.card.set_code,
            finish=item.finish,
            scryfall_id=item.scryfall_id,
            collector_number=item.card.collector_number,
            set_name=item.card.set_name,
            condition=item.condition or "NM",
            force_refresh=True,
        )
        if mm_data:
            services.apply_meeple_snapshot(item, mm_data)
            item.updated_at = utc_now()
            db.session.commit()

        oracle_dupe_map = _compute_oracle_dupe_map(user.id)
        return render_template(
            "partials/binder_card_row.html",
            item=item,
            oracle_dupe_map=oracle_dupe_map,
            is_pending=meeple_worker.is_pending(item.id),
        )

    @app.route("/binder/<int:item_id>/row", methods=["GET"])
    @login_required
    def get_item_row(item_id: int):
        user = get_current_user()
        item = BinderItem.query.filter_by(id=item_id, user_id=user.id).first_or_404()
        oracle_dupe_map = _compute_oracle_dupe_map(user.id)
        return render_template(
            "partials/binder_card_row.html",
            item=item,
            oracle_dupe_map=oracle_dupe_map,
            is_pending=meeple_worker.is_pending(item.id),
        )

    @app.route("/binder/<int:item_id>/quantity", methods=["POST"])
    @login_required
    def update_item_quantity(item_id: int):
        user = get_current_user()
        item = BinderItem.query.filter_by(id=item_id, user_id=user.id).first_or_404()

        payload = request.get_json(silent=True) or request.form
        if "delta" in payload:
            try:
                delta = int(payload.get("delta", 0))
            except (ValueError, TypeError):
                delta = 0
            new_qty = (item.quantity or 1) + delta
        elif "quantity" in payload:
            try:
                new_qty = int(payload.get("quantity", 1))
            except (ValueError, TypeError):
                new_qty = item.quantity or 1
        else:
            new_qty = item.quantity or 1

        if new_qty <= 0:
            db.session.delete(item)
            db.session.commit()
            return ""

        item.quantity = new_qty
        item.updated_at = utc_now()
        db.session.commit()

        oracle_dupe_map = _compute_oracle_dupe_map(user.id)
        return render_template(
            "partials/binder_card_row.html",
            item=item,
            oracle_dupe_map=oracle_dupe_map,
            is_pending=meeple_worker.is_pending(item.id),
        )

    @app.route("/binder/<int:item_id>", methods=["DELETE"])
    @app.route("/binder/<int:item_id>/delete", methods=["POST"])
    @login_required
    def delete_binder_item(item_id: int):
        user = get_current_user()
        item = BinderItem.query.filter_by(id=item_id, user_id=user.id).first_or_404()
        db.session.delete(item)
        db.session.commit()
        if request.headers.get("HX-Request") or request.method == "DELETE":
            return ""
        flash("Card removed from your binder.", "success")
        return redirect(url_for("binder_view"))

    @app.route("/binder/bulk-status", methods=["POST"])
    @login_required
    def bulk_update_status():
        user = get_current_user()
        json_data = request.get_json(silent=True) or {}
        new_status = (request.form.get("status") or json_data.get("status") or "").strip()
        if new_status not in BinderItem.VALID_STATUSES:
            return jsonify({"error": f"Invalid status '{new_status}'."}), 400

        raw_ids = request.form.getlist("item_ids") or json_data.get("item_ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [x.strip() for x in raw_ids.split(",") if x.strip()]
        item_ids: List[int] = []
        for val in raw_ids:
            try:
                item_ids.append(int(val))
            except (ValueError, TypeError):
                pass

        if not item_ids:
            if request.is_json:
                return jsonify({"updated": 0, "queued": 0})
            flash("Select at least one card first.", "error")
            return redirect(url_for("binder_view"))

        items = (
            BinderItem.query.options(db.joinedload(BinderItem.card))
            .filter(BinderItem.user_id == user.id, BinderItem.id.in_(item_ids))
            .all()
        )

        now = utc_now()
        ttl = float(app.config.get("MEEPLE_CACHE_TTL_HOURS", 24.0))
        to_queue: List[int] = []

        for item in items:
            item.status = new_status
            item.updated_at = now
            if new_status in ("for_trade", "meeple_bound") and item.mm_checked_at is None:
                cached = services.find_cached_meeple_snapshot(
                    card_name=item.card.name,
                    set_code=item.card.set_code,
                    finish=item.finish,
                    scryfall_id=item.scryfall_id,
                    cache_ttl_hours=ttl,
                )
                if cached is not None:
                    services.apply_meeple_snapshot(item, cached)
                else:
                    to_queue.append(item.id)

        db.session.commit()

        queued_count = meeple_worker.enqueue(to_queue) if to_queue else 0

        if request.is_json:
            return jsonify({"updated": len(items), "queued": queued_count})

        active_filter = (request.form.get("filter") or "all").strip().lower()
        search_q = (request.form.get("q") or "").strip()

        if request.headers.get("HX-Request"):
            updated_items, oracle_dupe_map = _query_user_binder(user.id, active_filter=active_filter, search_q=search_q)
            return render_template(
                "partials/binder_list.html",
                items=updated_items,
                oracle_dupe_map=oracle_dupe_map,
                active_filter=active_filter,
                search_q=search_q,
                is_pending_fn=meeple_worker.is_pending,
            )

        msg = f"Updated {len(items)} card(s) to {new_status.replace('_', ' ').title()}."
        if queued_count:
            msg += f" Queued {queued_count} Mighty Meeple lookup(s) with polite pacing."
        flash(msg, "success")
        return redirect(url_for("binder_view", filter=active_filter, q=search_q))

    # -------------------------------------------------------------------------
    # Pod Feed (Shared across Pod Members — Public Items Only)
    # -------------------------------------------------------------------------

    @app.route("/pod")
    @login_required
    def pod_feed():
        status_filter = (request.args.get("status") or "all").strip().lower()
        if status_filter not in ("all", "meeple_bound", "for_trade"):
            status_filter = "all"

        owner_id_raw = request.args.get("owner_id", "").strip()
        owner_id: Optional[int] = None
        if owner_id_raw:
            try:
                owner_id = int(owner_id_raw)
            except (ValueError, TypeError):
                owner_id = None

        search_q = (request.args.get("q") or "").strip()

        query = (
            BinderItem.query.join(User, BinderItem.user_id == User.id)
            .join(Card, BinderItem.scryfall_id == Card.scryfall_id)
            .options(db.joinedload(BinderItem.user), db.joinedload(BinderItem.card))
            .filter(BinderItem.status.in_(["for_trade", "meeple_bound"]))
        )

        if status_filter in ("meeple_bound", "for_trade"):
            query = query.filter(BinderItem.status == status_filter)
        if owner_id is not None:
            query = query.filter(BinderItem.user_id == owner_id)
        if search_q:
            like_term = f"%{search_q}%"
            query = query.filter(
                or_(
                    Card.name.ilike(like_term),
                    Card.set_code.ilike(like_term),
                    User.display_name.ilike(like_term),
                )
            )

        # Put Meeple Bound cards first because they are about to leave the playgroup
        priority_order = case((BinderItem.status == "meeple_bound", 0), else_=1)
        items = query.order_by(priority_order, BinderItem.updated_at.desc(), BinderItem.id.desc()).all()

        pod_owners = (
            db.session.query(User)
            .join(BinderItem, BinderItem.user_id == User.id)
            .filter(BinderItem.status.in_(["for_trade", "meeple_bound"]))
            .distinct()
            .order_by(User.display_name.asc())
            .all()
        )

        context = {
            "items": items,
            "pod_owners": pod_owners,
            "status_filter": status_filter,
            "selected_owner_id": owner_id,
            "search_q": search_q,
            "active_tab": "pod",
        }
        if request.headers.get("HX-Request"):
            return render_template("partials/pod_feed_list.html", **context)
        return render_template("pod_feed.html", **context)

    # -------------------------------------------------------------------------
    # Import / Add (Zero Mighty Meeple Traffic — Scryfall Batch Only)
    # -------------------------------------------------------------------------

    @app.route("/import", methods=["GET", "POST"])
    @login_required
    def import_cards():
        user = get_current_user()
        if request.method == "GET":
            return render_template("import.html", active_tab="import")

        csv_text = None
        uploaded_file = request.files.get("csv_file")
        if uploaded_file and uploaded_file.filename:
            raw_bytes = uploaded_file.read()
            csv_text = raw_bytes.decode("utf-8-sig", errors="replace")

        quick_text = request.form.get("quick_text", "")
        single_card = (request.form.get("single_card_name") or "").strip()
        if not csv_text and not quick_text.strip() and single_card:
            qty = request.form.get("single_quantity", "1").strip() or "1"
            finish = (request.form.get("single_finish") or "nonfoil").strip().lower()
            suffix = " *Etched*" if finish == "etched" else (" *F*" if finish == "foil" else "")
            quick_text = f"{qty} {single_card}{suffix}"
            single_cond = (request.form.get("single_condition") or "NM").strip().upper()
        else:
            single_cond = None

        try:
            parsed = services.parse_import_input(csv_text=csv_text, quick_text=quick_text)
        except services.ImportParseError as exc:
            flash(str(exc), "error")
            return render_template("import.html", active_tab="import", quick_text=quick_text), 400

        if single_cond:
            for entry in parsed["entries"]:
                entry["condition"] = single_cond

        result = services.import_cards_to_binder(user.id, parsed["entries"])

        msg = f"Added {result['total_quantity']} card(s) across {result['imported_rows']} line(s) to your Private Binder."
        if result["unresolved"]:
            sample_unres = ", ".join(result["unresolved"][:5])
            msg += f" Could not match on Scryfall: {sample_unres}."
        if parsed["errors"]:
            msg += f" Skipped {len(parsed['errors'])} malformed row(s)."

        flash(msg, "success" if result["imported_rows"] > 0 else "error")
        return redirect(url_for("binder_view"))

    @app.route("/api/scryfall/autocomplete", methods=["GET"])
    @login_required
    def scryfall_autocomplete():
        q = (request.args.get("q") or "").strip()
        if len(q) < 2:
            return jsonify({"suggestions": []})
        suggestions = ScryfallProvider().autocomplete(q)
        return jsonify({"suggestions": suggestions})

    return app


app = create_app()

if __name__ == "__main__":
    import threading
    import time
    import webbrowser

    port = int(app.config.get("PORT", 5055))
    url = f"http://localhost:{port}"

    def _open_browser():
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # When Flask debug mode is active, Werkzeug spawns a child process.
    # WERKZEUG_RUN_MAIN is set to "true" in the child process, so we only open the browser once.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        threading.Thread(target=_open_browser, daemon=True).start()

    print(f"Starting VaultSwap on {url}")
    app.run(host="0.0.0.0", port=port, debug=True)
