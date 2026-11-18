"""Flask application factory and routes for VaultSwap."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import timedelta
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple
from flask import (
    Flask,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
import requests
from sqlalchemy import case, func, inspect, or_, text

import drive_sync
import services
from config import Config
from drive_sync import DriveClient, DriveImportWorker, DriveSyncError
from models import ActivityLog, BinderItem, Card, DriveImport, User, db, utc_now
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
    color_filter: str = "all",
    type_filter: str = "all",
    finish_filter: str = "all",
    sort_mode: str = "default",
) -> Tuple[List[BinderItem], Dict[str, Dict[str, int]]]:
    """Queries user's private binder strictly scoped to `user_id` with Chimera-style filters and sorting."""
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

    # Active status filter
    if active_filter == "recent":
        cutoff = utc_now() - timedelta(days=7)
        query = query.filter(BinderItem.created_at >= cutoff)
    elif active_filter == "dupes":
        dupe_oracles = [
            oid for oid, info in oracle_dupe_map.items() if info["total_qty"] > 1
        ]
        if dupe_oracles:
            query = query.filter(
                or_(BinderItem.quantity > 1, Card.oracle_id.in_(dupe_oracles))
            )
        else:
            query = query.filter(BinderItem.quantity > 1)
    elif active_filter == "for_trade":
        query = query.filter(BinderItem.status == "for_trade")
    elif active_filter == "meeple_bound":
        query = query.filter(BinderItem.status == "meeple_bound")

    # Finish filter
    finish_norm = finish_filter.strip().lower()
    if finish_norm in ("foil", "etched"):
        query = query.filter(BinderItem.finish == finish_norm)
    elif finish_norm in ("nonfoil", "normal"):
        query = query.filter(BinderItem.finish == "nonfoil")
    elif finish_norm == "special":
        query = query.filter(BinderItem.finish.in_(["foil", "etched"]))

    # Card Type filter (creature, instant, sorcery, artifact, enchantment, planeswalker, land, other)
    t_clean = type_filter.strip().lower()
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

    # Note: color_bucket can also be filtered server-side or in-memory, but let's query first or apply SQL:
    c_clean = color_filter.strip().lower()

    # Sorting
    s_clean = sort_mode.strip().lower()
    if s_clean == "price_desc":
        query = query.order_by(func.coalesce(Card.usd_price, 0).desc(), Card.name.asc())
    elif s_clean == "price_asc":
        query = query.order_by(func.coalesce(Card.usd_price, 999999).asc(), Card.name.asc())
    elif s_clean == "total_value_desc":
        query = query.order_by((func.coalesce(Card.usd_price, 0) * BinderItem.quantity).desc(), Card.name.asc())
    elif s_clean == "meeple_credit_desc":
        query = query.order_by(func.coalesce(BinderItem.mm_buylist_credit, 0).desc(), Card.name.asc())
    elif s_clean == "name_asc":
        query = query.order_by(Card.name.asc(), Card.set_code.asc())
    elif s_clean == "name_desc":
        query = query.order_by(Card.name.desc(), Card.set_code.asc())
    elif s_clean == "cmc_asc":
        query = query.order_by(func.coalesce(Card.cmc, 0).asc(), Card.name.asc())
    elif s_clean == "qty_desc":
        query = query.order_by(BinderItem.quantity.desc(), Card.name.asc())
    elif s_clean == "set_asc":
        query = query.order_by(Card.set_code.asc(), Card.collector_number.asc())
    else:
        # Default sort
        if active_filter == "recent":
            query = query.order_by(BinderItem.created_at.desc(), BinderItem.id.desc())
        elif active_filter == "dupes":
            query = query.order_by(Card.name.asc(), Card.set_code.asc(), BinderItem.id.asc())
        elif active_filter in ("for_trade", "meeple_bound"):
            query = query.order_by(BinderItem.updated_at.desc(), BinderItem.id.desc())
        else:
            query = query.order_by(BinderItem.created_at.desc(), BinderItem.id.desc())

    items = query.all()

    # Python-level filter for color_bucket (ensures land, hybrid, multi, colorless match accurately)
    if c_clean and c_clean != "all":
        items = [item for item in items if item.color_bucket.lower() == c_clean]

    return items, oracle_dupe_map


def create_app(test_config: Optional[Dict[str, Any]] = None) -> Flask:
    """Application factory for VaultSwap."""
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
        if "SQLALCHEMY_DATABASE_URI" in test_config and "SQLALCHEMY_ENGINE_OPTIONS" not in test_config:
            from config import _build_engine_options
            app.config["SQLALCHEMY_ENGINE_OPTIONS"] = _build_engine_options(
                test_config["SQLALCHEMY_DATABASE_URI"],
                str(test_config.get("DATABASE_URL_POOLED", "")),
            )

    db.init_app(app)

    meeple_worker = services.MeepleBackgroundWorker()
    meeple_worker.init_app(app)
    app.meeple_worker = meeple_worker  # type: ignore[attr-defined]

    discord_worker = services.DiscordNotificationWorker()
    discord_worker.init_app(app)
    app.discord_worker = discord_worker  # type: ignore[attr-defined]

    drive_worker = DriveImportWorker()
    drive_worker.init_app(app)
    app.drive_worker = drive_worker  # type: ignore[attr-defined]

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

    def _apply_lightweight_migrations() -> None:
        user_cols = {c["name"] for c in inspect(db.engine).get_columns("users")}
        if "is_admin" not in user_cols:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE"))
        if "discord_webhook_url" not in user_cols:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN discord_webhook_url TEXT"))
        card_cols = {c["name"] for c in inspect(db.engine).get_columns("cards")}
        if "colors" not in card_cols:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE cards ADD COLUMN colors VARCHAR(50)"))
        if "color_identity" not in card_cols:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE cards ADD COLUMN color_identity VARCHAR(50)"))
        if "cmc" not in card_cols:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE cards ADD COLUMN cmc NUMERIC(6, 2)"))
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE binder_items SET condition = 'LP' WHERE status = 'meeple_bound'"))

    def log_activity(
        action: str,
        user: Optional[User] = None,
        user_id: Optional[int] = None,
        user_email: Optional[str] = None,
        user_name: Optional[str] = None,
        details: Optional[str] = None,
    ) -> ActivityLog:
        """Helper to record user logins, card uploads, and admin actions."""
        try:
            u_id = user.id if user else user_id
            u_email = (user.email if user else user_email) or ""
            u_name = (user.display_name if user else user_name) or ""
            ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
            if ip and "," in ip:
                ip = ip.split(",")[0].strip()

            entry = ActivityLog(
                user_id=u_id,
                user_email=u_email[:255] if u_email else None,
                user_name=u_name[:100] if u_name else None,
                action=action[:100],
                details=details,
                ip_address=(ip[:64] if ip else None),
                created_at=utc_now(),
            )
            db.session.add(entry)
            db.session.commit()
            return entry
        except Exception as exc:
            logger.warning("Failed to record ActivityLog %s: %s", action, exc)
            try:
                db.session.rollback()
            except Exception:
                pass

    with app.app_context():
        db.create_all()
        _apply_lightweight_migrations()

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

    def admin_required(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            user = get_current_user()
            if not user or not user.is_admin:
                if request.path.startswith("/api/") or request.is_json:
                    return jsonify({"error": "Admin access required."}), 403
                flash("Admin access is required to view that page.", "error")
                return redirect(url_for("binder_view"))
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

    class NotInvitedError(Exception):
        """Raised when a non-invited user attempts to sign in while INVITE_ONLY is enabled."""

    def _upsert_google_user(
        google_sub: str,
        email: str,
        display_name: str,
        avatar_url: Optional[str] = None,
    ) -> User:
        clean_email = email.strip().lower()
        admin_emails = {str(e).strip().lower() for e in (app.config.get("ADMIN_EMAILS") or set())}
        is_bootstrap_admin = clean_email in admin_emails

        user = User.query.filter_by(google_sub=google_sub).first()
        if user is None:
            user = User.query.filter(func.lower(User.email) == clean_email).first()

        if user is None:
            if app.config.get("INVITE_ONLY", True) and not is_bootstrap_admin:
                raise NotInvitedError(clean_email)
            user = User(
                google_sub=google_sub,
                email=clean_email,
                display_name=display_name.strip()[:100] or clean_email.split("@")[0],
                avatar_url=avatar_url,
                is_admin=is_bootstrap_admin,
            )
            db.session.add(user)
        else:
            user.google_sub = google_sub
            user.email = clean_email
            if display_name and display_name.strip():
                user.display_name = display_name.strip()[:100]
            if avatar_url:
                user.avatar_url = avatar_url
            if is_bootstrap_admin:
                user.is_admin = True

        db.session.commit()
        return user

    @app.route("/manifest.webmanifest")
    @app.route("/manifest.json")
    def pwa_manifest():
        """Serve Progressive Web App manifest with canonical MIME type."""
        response = make_response(send_from_directory("static", "manifest.webmanifest"))
        response.headers["Content-Type"] = "application/manifest+json; charset=utf-8"
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    @app.route("/sw.js")
    def service_worker():
        """Serve Service Worker from app root so default scope covers entire site."""
        response = make_response(send_from_directory("static", "sw.js"))
        response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.route("/offline")
    def offline_page():
        """Offline fallback page for Service Worker cache."""
        return render_template("offline.html")

    @app.route("/apple-touch-icon.png")
    @app.route("/apple-touch-icon-precomposed.png")
    def apple_touch_icon():
        return send_from_directory("static/img/icons", "apple-touch-icon.png", mimetype="image/png")

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory("static", "favicon.ico", mimetype="image/vnd.microsoft.icon")

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

    @app.route("/privacy")
    def privacy_policy():
        return render_template("privacy.html")

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        u = get_current_user()
        if u:
            log_activity(
                action="LOGOUT",
                user=u,
                details=f"Signed out ({u.email})",
            )
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
            log_activity(
                action="LOGIN_GOOGLE",
                user=user,
                details=f"Signed in via Google OAuth ({user.email})",
            )
            return redirect(next_url or url_for("binder_view"))
        except NotInvitedError as exc:
            flash(f"{exc} hasn't been invited to this pod yet. Ask an admin to add you.", "error")
            return redirect(url_for("login_page"))
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
        try:
            user = _upsert_google_user(
                google_sub=f"dev-{email}",
                email=email,
                display_name=display_name,
                avatar_url=None,
            )
        except NotInvitedError as exc:
            flash(f"{exc} hasn't been invited to this pod yet. Ask an admin to add you.", "error")
            return redirect(url_for("login_page"))
        if data.get("discord_handle"):
            user.discord_handle = str(data["discord_handle"]).strip()[:100]
            db.session.commit()

        session.permanent = True
        session["user_id"] = user.id
        log_activity(
            action="LOGIN_DEV",
            user=user,
            details=f"Local dev sign-in ({user.email})",
        )
        return redirect(url_for("binder_view"))

    # -------------------------------------------------------------------------
    # Admin User Management (Invite-Only Allowlist)
    # -------------------------------------------------------------------------

    _EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    @app.route("/admin/users", methods=["GET", "POST"])
    @admin_required
    def admin_users():
        if request.method == "POST":
            raw_email = (request.form.get("email") or "").strip().lower()
            display_name = (request.form.get("display_name") or "").strip()
            make_admin = str(request.form.get("is_admin") or "").strip().lower() in ("1", "true", "on", "yes")

            if not raw_email or not _EMAIL_RE.match(raw_email):
                flash("Please enter a valid email address.", "error")
                return redirect(url_for("admin_users"))

            existing = User.query.filter(func.lower(User.email) == raw_email).first()
            if existing is not None:
                flash(f"{raw_email} is already in the pod.", "error")
                return redirect(url_for("admin_users"))

            new_user = User(
                google_sub=f"invite:{raw_email}",
                email=raw_email,
                display_name=(display_name[:100] if display_name else raw_email.split("@")[0]),
                is_admin=make_admin,
            )
            db.session.add(new_user)
            db.session.commit()
            log_activity(
                action="ADMIN_INVITE_USER",
                user=get_current_user(),
                details=f"Invited {raw_email} (Admin: {make_admin})",
            )
            flash(f"Invited {raw_email} to VaultSwap.", "success")
            return redirect(url_for("admin_users"))

        users = User.query.order_by(User.created_at.desc(), User.id.desc()).all()
        card_counts = dict(
            db.session.query(BinderItem.user_id, func.coalesce(func.sum(BinderItem.quantity), 0))
            .group_by(BinderItem.user_id)
            .all()
        )

        # Activity log query with action/user filters
        action_filter = (request.args.get("action") or "all").strip()
        user_filter_raw = (request.args.get("user_id") or "").strip()
        log_query = ActivityLog.query.order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())

        if action_filter and action_filter != "all":
            if action_filter == "logins":
                log_query = log_query.filter(ActivityLog.action.in_(["LOGIN_GOOGLE", "LOGIN_DEV", "LOGOUT"]))
            elif action_filter == "uploads":
                log_query = log_query.filter(ActivityLog.action.in_(["UPLOAD_CSV", "UPLOAD_QUICK_LIST", "UPLOAD_DRIVE_SYNC", "ADD_SINGLE"]))
            elif action_filter == "admin":
                log_query = log_query.filter(ActivityLog.action.in_(["ADMIN_INVITE_USER", "ADMIN_REVOKE_INVITE"]))
            else:
                log_query = log_query.filter(ActivityLog.action == action_filter)

        if user_filter_raw:
            try:
                u_id_val = int(user_filter_raw)
                log_query = log_query.filter(ActivityLog.user_id == u_id_val)
            except (ValueError, TypeError):
                pass

        activity_logs = log_query.limit(100).all()

        return render_template(
            "admin_users.html",
            users=users,
            card_counts=card_counts,
            activity_logs=activity_logs,
            action_filter=action_filter,
            selected_user_id=user_filter_raw,
            active_tab="admin",
        )

    @app.route("/admin/users/<int:user_id>/revoke", methods=["POST"])
    @admin_required
    def admin_revoke_user(user_id: int):
        me = get_current_user()
        target = db.session.get(User, user_id)
        if target is None:
            flash("User not found.", "error")
            return redirect(url_for("admin_users"))
        if me and target.id == me.id:
            flash("You cannot revoke your own account.", "error")
            return redirect(url_for("admin_users"))
        if not target.is_pending:
            flash("Only pending invites can be revoked.", "error")
            return redirect(url_for("admin_users"))

        email = target.email
        db.session.delete(target)
        db.session.commit()
        log_activity(
            action="ADMIN_REVOKE_INVITE",
            user=me,
            details=f"Revoked pending invite for {email}",
        )
        flash(f"Revoked pending invite for {email}.", "success")
        return redirect(url_for("admin_users"))

    @app.route("/profile", methods=["POST"])
    @login_required
    def update_profile():
        user = get_current_user()
        display_name = (request.form.get("display_name") or "").strip()
        discord_handle = (request.form.get("discord_handle") or "").strip()
        raw_webhook = request.form.get("discord_webhook_url")

        if display_name:
            user.display_name = display_name[:100]
        user.discord_handle = discord_handle[:100] or None

        if raw_webhook is not None:
            try:
                user.discord_webhook_url = services.validate_discord_webhook_url(raw_webhook)
            except ValueError as exc:
                db.session.commit()
                flash(str(exc), "error")
                return redirect(request.referrer or url_for("binder_view"))

        db.session.commit()
        flash("Pod profile updated.", "success")
        return redirect(request.referrer or url_for("binder_view"))

    @app.route("/profile/test-webhook", methods=["POST"])
    @login_required
    def test_discord_webhook():
        user = get_current_user()
        raw_url = (request.form.get("discord_webhook_url") or user.discord_webhook_url or "").strip()
        try:
            webhook_url = services.validate_discord_webhook_url(raw_url)
        except ValueError as exc:
            if request.is_json:
                return jsonify({"error": str(exc)}), 400
            flash(str(exc), "error")
            return redirect(request.referrer or url_for("binder_view"))

        if not webhook_url:
            msg = "Configure and save a Discord Webhook URL first."
            if request.is_json:
                return jsonify({"error": msg}), 400
            flash(msg, "error")
            return redirect(request.referrer or url_for("binder_view"))

        payload = {
            "username": "VaultSwap",
            "content": (
                f"✅ **VaultSwap Discord Webhook Connected!**\n"
                f"Configured by **{user.display_name}** — new **For Trade** and **Meeple Bound** cards "
                f"will be batched for 5 minutes and posted here."
            ),
        }
        ok = discord_worker.sender_fn(webhook_url, payload)
        if not ok:
            err_msg = "Could not deliver test message to Discord webhook. Check the URL and channel permissions."
            if request.is_json:
                return jsonify({"error": err_msg}), 502
            flash(err_msg, "error")
            return redirect(request.referrer or url_for("binder_view"))

        if request.is_json:
            return jsonify({"ok": True})
        flash("Test notification sent to your Discord webhook!", "success")
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
        color_filter = (request.args.get("color") or "all").strip().lower()
        type_filter = (request.args.get("type") or "all").strip().lower()
        finish_filter = (request.args.get("finish") or "all").strip().lower()
        sort_mode = (request.args.get("sort") or "default").strip().lower()
        group_by = (request.args.get("group") or "none").strip().lower()
        view_mode = (request.args.get("view") or "rows").strip().lower()

        items, oracle_dupe_map = _query_user_binder(
            user.id,
            active_filter=active_filter,
            search_q=search_q,
            color_filter=color_filter,
            type_filter=type_filter,
            finish_filter=finish_filter,
            sort_mode=sort_mode,
        )
        stats = _compute_binder_stats(user.id)

        context = {
            "items": items,
            "oracle_dupe_map": oracle_dupe_map,
            "stats": stats,
            "active_filter": active_filter,
            "search_q": search_q,
            "color_filter": color_filter,
            "type_filter": type_filter,
            "finish_filter": finish_filter,
            "sort_mode": sort_mode,
            "group_by": group_by,
            "view_mode": view_mode,
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
        previous_status = item.status
        item.status = new_status
        if new_status == "meeple_bound":
            item.condition = "LP"
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
                condition=item.condition or "LP",
            )
            if mm_data:
                services.apply_meeple_snapshot(item, mm_data)

        db.session.commit()
        if new_status in ("for_trade", "meeple_bound") and previous_status != new_status:
            discord_worker.enqueue([item.id])
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
            condition=item.condition or "LP",
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
        discord_to_queue: List[int] = []

        for item in items:
            prev_status = item.status
            item.status = new_status
            if new_status == "meeple_bound":
                item.condition = "LP"
            item.updated_at = now
            if new_status in ("for_trade", "meeple_bound") and prev_status != new_status:
                discord_to_queue.append(item.id)
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
        if discord_to_queue:
            discord_worker.enqueue(discord_to_queue)

        if request.is_json:
            return jsonify({"updated": len(items), "queued": queued_count})

        active_filter = (request.form.get("filter") or "all").strip().lower()
        search_q = (request.form.get("q") or "").strip()
        color_filter = (request.form.get("color") or "all").strip().lower()
        type_filter = (request.form.get("type") or "all").strip().lower()
        finish_filter = (request.form.get("finish") or "all").strip().lower()
        sort_mode = (request.form.get("sort") or "default").strip().lower()
        group_by = (request.form.get("group") or "none").strip().lower()
        view_mode = (request.form.get("view") or "rows").strip().lower()

        if request.headers.get("HX-Request"):
            updated_items, oracle_dupe_map = _query_user_binder(
                user.id,
                active_filter=active_filter,
                search_q=search_q,
                color_filter=color_filter,
                type_filter=type_filter,
                finish_filter=finish_filter,
                sort_mode=sort_mode,
            )
            return render_template(
                "partials/binder_list.html",
                items=updated_items,
                oracle_dupe_map=oracle_dupe_map,
                active_filter=active_filter,
                search_q=search_q,
                color_filter=color_filter,
                type_filter=type_filter,
                finish_filter=finish_filter,
                sort_mode=sort_mode,
                group_by=group_by,
                view_mode=view_mode,
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
        color_filter = (request.args.get("color") or "all").strip().lower()
        type_filter = (request.args.get("type") or "all").strip().lower()
        finish_filter = (request.args.get("finish") or "all").strip().lower()
        sort_mode = (request.args.get("sort") or "default").strip().lower()
        group_by = (request.args.get("group") or "none").strip().lower()
        view_mode = (request.args.get("view") or "rows").strip().lower()

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
                    Card.type_line.ilike(like_term),
                    User.display_name.ilike(like_term),
                )
            )

        # Finish filter
        finish_norm = finish_filter.strip().lower()
        if finish_norm in ("foil", "etched"):
            query = query.filter(BinderItem.finish == finish_norm)
        elif finish_norm in ("nonfoil", "normal"):
            query = query.filter(BinderItem.finish == "nonfoil")
        elif finish_norm == "special":
            query = query.filter(BinderItem.finish.in_(["foil", "etched"]))

        # Card Type filter
        t_clean = type_filter.strip().lower()
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

        # Sorting
        s_clean = sort_mode.strip().lower()
        if s_clean == "price_desc":
            query = query.order_by(func.coalesce(Card.usd_price, 0).desc(), Card.name.asc())
        elif s_clean == "price_asc":
            query = query.order_by(func.coalesce(Card.usd_price, 999999).asc(), Card.name.asc())
        elif s_clean == "total_value_desc":
            query = query.order_by((func.coalesce(Card.usd_price, 0) * BinderItem.quantity).desc(), Card.name.asc())
        elif s_clean == "meeple_credit_desc":
            query = query.order_by(func.coalesce(BinderItem.mm_buylist_credit, 0).desc(), Card.name.asc())
        elif s_clean == "name_asc":
            query = query.order_by(Card.name.asc(), Card.set_code.asc())
        elif s_clean == "name_desc":
            query = query.order_by(Card.name.desc(), Card.set_code.asc())
        elif s_clean == "cmc_asc":
            query = query.order_by(func.coalesce(Card.cmc, 0).asc(), Card.name.asc())
        elif s_clean == "qty_desc":
            query = query.order_by(BinderItem.quantity.desc(), Card.name.asc())
        elif s_clean == "set_asc":
            query = query.order_by(Card.set_code.asc(), Card.collector_number.asc())
        else:
            # Default: Put Meeple Bound cards first because they are about to leave the playgroup
            priority_order = case((BinderItem.status == "meeple_bound", 0), else_=1)
            query = query.order_by(priority_order, BinderItem.updated_at.desc(), BinderItem.id.desc())

        items = query.all()

        # Color filter (python-level)
        c_clean = color_filter.strip().lower()
        if c_clean and c_clean != "all":
            items = [item for item in items if item.color_bucket.lower() == c_clean]

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
            "color_filter": color_filter,
            "type_filter": type_filter,
            "finish_filter": finish_filter,
            "sort_mode": sort_mode,
            "group_by": group_by,
            "view_mode": view_mode,
            "active_tab": "pod",
        }
        if request.headers.get("HX-Request"):
            return render_template("partials/pod_feed_list.html", **context)
        return render_template("pod_feed.html", **context)

    # -------------------------------------------------------------------------
    # Import / Add (Zero Mighty Meeple Traffic — Scryfall Batch Only)
    # -------------------------------------------------------------------------

    def _drive_import_context(user_id: int) -> Dict[str, Any]:
        client_id = app.config.get("GOOGLE_CLIENT_ID", "")
        api_key = app.config.get("GOOGLE_API_KEY", "")
        project_num = app.config.get("GOOGLE_CLOUD_PROJECT_NUMBER", "")
        last_drive_import = (
            DriveImport.query.filter_by(user_id=user_id)
            .filter(DriveImport.status.in_(["done", "unchanged"]))
            .order_by(DriveImport.created_at.desc(), DriveImport.id.desc())
            .first()
        )
        active_drive_import = (
            DriveImport.query.filter_by(user_id=user_id)
            .filter(DriveImport.status.in_(["queued", "running"]))
            .order_by(DriveImport.id.desc())
            .first()
        )
        return {
            "drive_configured": bool(client_id and api_key and project_num),
            "google_client_id": client_id,
            "google_api_key": api_key,
            "google_project_number": project_num,
            "last_drive_import": last_drive_import,
            "active_drive_import": active_drive_import,
        }

    @app.route("/import", methods=["GET", "POST"])
    @login_required
    def import_cards():
        user = get_current_user()
        if request.method == "GET":
            return render_template("import.html", active_tab="import", **_drive_import_context(user.id))

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
            return (
                render_template(
                    "import.html",
                    active_tab="import",
                    quick_text=quick_text,
                    **_drive_import_context(user.id),
                ),
                400,
            )

        if single_cond:
            for entry in parsed["entries"]:
                entry["condition"] = single_cond

        result = services.import_cards_to_binder(user.id, parsed["entries"])

        # Determine upload source
        if uploaded_file and uploaded_file.filename:
            upload_source = f"CSV Upload ({uploaded_file.filename})"
            action_name = "UPLOAD_CSV"
        elif single_card:
            upload_source = f"Single Add ({single_card})"
            action_name = "ADD_SINGLE"
        else:
            upload_source = "Quick Text Paste"
            action_name = "UPLOAD_QUICK_LIST"

        log_activity(
            action=action_name,
            user=user,
            details=f"{upload_source}: Added {result['total_quantity']} card(s) across {result['imported_rows']} row(s).",
        )

        msg = f"Added {result['total_quantity']} card(s) across {result['imported_rows']} line(s) to your Private Binder."
        if result["unresolved"]:
            sample_unres = ", ".join(result["unresolved"][:5])
            msg += f" Could not match on Scryfall: {sample_unres}."
        if parsed["errors"]:
            msg += f" Skipped {len(parsed['errors'])} malformed row(s)."

        flash(msg, "success" if result["imported_rows"] > 0 else "error")
        return redirect(url_for("binder_view"))

    def _parse_unresolved(imp: DriveImport) -> List[str]:
        try:
            data = json.loads(imp.unresolved or "[]")
            return data if isinstance(data, list) else []
        except Exception:
            return []

    @app.route("/import/drive", methods=["POST"])
    @login_required
    def import_from_drive():
        user = get_current_user()
        payload = request.get_json(silent=True) or request.form or {}
        access_token = str(payload.get("access_token") or "").strip()
        reuse_last = str(payload.get("reuse_last") or "").strip().lower() in ("1", "true", "yes")
        file_id = str(payload.get("file_id") or "").strip()
        file_name = str(payload.get("file_name") or "").strip()

        active = (
            DriveImport.query.filter_by(user_id=user.id)
            .filter(DriveImport.status.in_(["queued", "running"]))
            .first()
        )
        if active is not None:
            return jsonify({"error": "A Google Drive import is already in progress."}), 409

        if reuse_last and not file_id:
            last = (
                DriveImport.query.filter_by(user_id=user.id)
                .filter(DriveImport.status.in_(["done", "unchanged"]))
                .order_by(DriveImport.created_at.desc(), DriveImport.id.desc())
                .first()
            )
            if last is None:
                return jsonify({"error": "No previous Drive file found to re-sync."}), 400
            file_id = last.drive_file_id
            file_name = last.file_name

        if not file_id:
            return jsonify({"error": "Missing Google Drive file ID."}), 400

        try:
            DriveClient().verify_token(access_token, app.config.get("GOOGLE_CLIENT_ID", ""))
        except DriveSyncError as exc:
            return jsonify({"error": str(exc)}), 401

        imp = DriveImport(
            user_id=user.id,
            drive_file_id=file_id[:128],
            file_name=(file_name or "Drive CSV")[:255],
            status="queued",
            phase="downloading",
        )
        db.session.add(imp)
        db.session.commit()

        drive_worker.enqueue(imp.id, access_token)
        return render_template(
            "partials/drive_import_status.html",
            imp=imp,
            unresolved_list=[],
        )

    @app.route("/import/drive/<int:import_id>/status", methods=["GET"])
    @login_required
    def drive_import_status(import_id: int):
        user = get_current_user()
        imp = DriveImport.query.filter_by(id=import_id, user_id=user.id).first_or_404()
        return render_template(
            "partials/drive_import_status.html",
            imp=imp,
            unresolved_list=_parse_unresolved(imp),
        )

    @app.route("/import/drive/history", methods=["GET"])
    @login_required
    def drive_import_history():
        user = get_current_user()
        imports = (
            DriveImport.query.filter_by(user_id=user.id)
            .order_by(DriveImport.created_at.desc(), DriveImport.id.desc())
            .limit(10)
            .all()
        )
        return jsonify({
            "imports": [
                {
                    "id": imp.id,
                    "file_name": imp.file_name,
                    "status": imp.status,
                    "max_card_created_at": imp.max_card_created_at.isoformat() if imp.max_card_created_at else None,
                    "rows_scanned": imp.rows_scanned,
                    "rows_new": imp.rows_new,
                    "rows_imported": imp.rows_imported,
                    "cards_imported": imp.cards_imported,
                    "created_at": imp.created_at.isoformat() if imp.created_at else None,
                }
                for imp in imports
            ]
        })

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
