"""Flask application factory and routes for VaultSwap."""

from __future__ import annotations

import hashlib
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
from flask_wtf.csrf import CSRFProtect
import requests
from sqlalchemy import case, func, inspect, or_, text

import drive_sync
import jobs
import queries
import services
from config import Config
from drive_sync import DriveClient, DriveSyncError
from models import ActivityLog, ApiToken, BinderItem, Card, DriveImport, Job, User, db, utc_now
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

    def dev_login_allowed() -> bool:
        return bool(
            app.config.get("TESTING")
            or app.config.get("DEV_LOGIN_ENABLED")
            or not app.config.get("GOOGLE_CLIENT_ID")
        )

    if not app.config.get("SECRET_KEY"):
        if not dev_login_allowed():
            raise RuntimeError("SECRET_KEY environment variable must be set when dev login is disabled.")
        app.config["SECRET_KEY"] = "vaultswap-dev-secret-key-mtg"

    csrf = CSRFProtect(app)
    db.init_app(app)

    job_queue = jobs.JobQueue()
    job_queue.init_app(app)
    app.job_queue = job_queue  # type: ignore[attr-defined]

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
        if "color_bucket" not in card_cols:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE cards ADD COLUMN color_bucket VARCHAR(12)"))
        if "primary_type" not in card_cols:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE cards ADD COLUMN primary_type VARCHAR(15)"))

        card_indexes = {idx["name"] for idx in inspect(db.engine).get_indexes("cards")}
        if "ix_cards_color_bucket" not in card_indexes:
            with db.engine.begin() as conn:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_cards_color_bucket ON cards (color_bucket)"))
        if "ix_cards_primary_type" not in card_indexes:
            with db.engine.begin() as conn:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_cards_primary_type ON cards (primary_type)"))

        # One-time backfill for persisted color_bucket / primary_type in batches of 1,000
        while True:
            batch = (
                Card.query.filter(or_(Card.color_bucket_col.is_(None), Card.primary_type_col.is_(None)))
                .limit(1000)
                .all()
            )
            if not batch:
                break
            for card_row in batch:
                card_row.color_bucket_col = card_row.compute_color_bucket()
                card_row.primary_type_col = card_row.compute_primary_card_type()
            db.session.commit()

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
        user = get_current_user()
        user_api_tokens: List[ApiToken] = []
        if user is not None:
            user_api_tokens = (
                ApiToken.query.filter_by(user_id=user.id, revoked_at=None)
                .order_by(ApiToken.created_at.desc())
                .all()
            )
        return {
            "current_user": user,
            "dev_login_allowed": dev_login_allowed(),
            "pending_meeple_count": job_queue.pending_count("meeple_lookup"),
            "user_api_tokens": user_api_tokens,
            "new_mcp_token": session.pop("new_mcp_token", None),
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
        sender_fn = getattr(app, "discord_sender_fn", None) or services.send_discord_webhook
        ok = sender_fn(webhook_url, payload)
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

    @app.route("/profile/api-tokens", methods=["POST"])
    @login_required
    def create_api_token():
        user = get_current_user()
        label = (request.form.get("label") or (request.get_json(silent=True) or {}).get("label") or "Gemini").strip()[:100]
        raw_token = f"vs_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

        tok = ApiToken(
            user_id=user.id,
            label=label or "Gemini",
            token_hash=token_hash,
            token_prefix=raw_token[:10],
        )
        db.session.add(tok)
        db.session.commit()
        log_activity(action="API_TOKEN_CREATED", user=user, details=f"Created MCP API token ({tok.token_prefix}...)")

        if request.is_json:
            return jsonify({"id": tok.id, "token": raw_token, "prefix": tok.token_prefix, "label": tok.label})

        session["new_mcp_token"] = raw_token
        base_url = (app.config.get("APP_BASE_URL") or request.host_url.rstrip("/")).rstrip("/")
        flash(f"MCP key created (shown once): {base_url}/mcp?key={raw_token}", "success")
        return redirect(request.referrer or url_for("binder_view"))

    @app.route("/profile/api-tokens/<int:token_id>/revoke", methods=["POST"])
    @login_required
    def revoke_api_token(token_id: int):
        user = get_current_user()
        tok = ApiToken.query.filter_by(id=token_id, user_id=user.id).first_or_404()
        if tok.revoked_at is None:
            tok.revoked_at = utc_now()
            db.session.commit()
            log_activity(action="API_TOKEN_REVOKED", user=user, details=f"Revoked MCP API token ({tok.token_prefix}...)")
        if request.is_json:
            return jsonify({"ok": True})
        flash("MCP API key revoked.", "success")
        return redirect(request.referrer or url_for("binder_view"))

    # -------------------------------------------------------------------------
    # Durable Job Queue Runner (Google Cloud Tasks Target)
    # -------------------------------------------------------------------------

    def _verify_tasks_caller() -> bool:
        shared_secret = (app.config.get("TASKS_SHARED_SECRET") or "").strip()
        if shared_secret:
            hdr_secret = (request.headers.get("X-Tasks-Secret") or "").strip()
            if hdr_secret and secrets.compare_digest(hdr_secret, shared_secret):
                return True

        auth_header = (request.headers.get("Authorization") or "").strip()
        if not auth_header.lower().startswith("bearer "):
            return False
        id_token_str = auth_header[7:].strip()
        if not id_token_str:
            return False

        base_url = (app.config.get("APP_BASE_URL") or "").strip().rstrip("/")
        expected_audience = f"{base_url}/tasks/run" if base_url else request.base_url
        expected_sa = (app.config.get("TASKS_INVOKER_SA") or "").strip().lower()

        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token as google_id_token

            claims = google_id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                audience=expected_audience,
            )
            if expected_sa:
                email = str(claims.get("email") or "").strip().lower()
                if not email or not claims.get("email_verified") or email != expected_sa:
                    return False
            return True
        except Exception as exc:
            logger.warning("Failed to verify /tasks/run OIDC token: %s", exc)
            return False

    @app.route("/tasks/run", methods=["POST"])
    @csrf.exempt
    def run_queued_tasks():
        if not _verify_tasks_caller():
            return jsonify({"error": "Unauthorized task caller."}), 401

        kind = (request.args.get("kind") or (request.get_json(silent=True) or {}).get("kind") or "").strip()
        kinds_to_run = [kind] if kind in Job.KINDS else list(Job.KINDS)

        job_queue.recover_stale()
        processed = 0
        for k in kinds_to_run:
            processed += job_queue.run_due(k, time_budget_sec=500.0)

        return jsonify({"ok": True, "processed": processed})

    # -------------------------------------------------------------------------
    # My Binder (Private, Row-Level Scoped) & HTMX Status Routes
    # -------------------------------------------------------------------------

    @app.route("/")
    @app.route("/binder")
    @login_required
    def binder_view():
        user = get_current_user()
        f = queries.CardFilters.from_request(request.args, scope="binder")

        base_query = (
            BinderItem.query.join(Card, BinderItem.scryfall_id == Card.scryfall_id)
            .options(db.joinedload(BinderItem.card))
        )
        filtered_query = queries.apply_card_filters(base_query, f, scope="binder", user_id=user.id)
        items, has_more = queries.paginate(filtered_query, f.page, per_page=60)

        page_oracle_ids = [item.card.oracle_id for item in items if item.card]
        oracle_dupe_map = queries.compute_oracle_dupe_map(user.id, oracle_ids=page_oracle_ids)
        pending_ids = job_queue.pending_ref_ids("meeple_lookup", [item.id for item in items])

        last_group_key = ""
        if items and f.group != "none":
            last_group_key = queries.group_key_and_label(items[-1], f.group)[0]

        next_page_url = (
            url_for("binder_view", **f.to_query_params("binder", page=f.page + 1, prev_group=last_group_key))
            if has_more
            else ""
        )

        is_hx = bool(request.headers.get("HX-Request"))
        if is_hx and f.page > 1:
            return render_template(
                "partials/binder_list.html",
                items=items,
                oracle_dupe_map=oracle_dupe_map,
                active_filter=f.status,
                search_q=f.q,
                color_filter=f.color,
                type_filter=f.type,
                finish_filter=f.finish,
                sort_mode=f.sort,
                group_by=f.group,
                view_mode=f.view,
                has_more=has_more,
                next_page_url=next_page_url,
                prev_group=f.prev_group,
                page_only=True,
                group_key_and_label_fn=queries.group_key_and_label,
                is_pending_fn=lambda item_id: item_id in pending_ids,
            )

        stats = queries.compute_binder_stats_sql(user.id)
        if f.status == "all" and not f.q and f.color == "all" and f.type == "all" and f.finish == "all":
            total_matches = stats["unique_rows"]
        else:
            count_q = queries.apply_card_filters(
                db.session.query(func.count(BinderItem.id)).join(Card, BinderItem.scryfall_id == Card.scryfall_id),
                f,
                scope="binder",
                user_id=user.id,
                apply_order=False,
            )
            total_matches = int(count_q.scalar() or 0)

        context = {
            "items": items,
            "oracle_dupe_map": oracle_dupe_map,
            "stats": stats,
            "total_matches": total_matches,
            "active_filter": f.status,
            "search_q": f.q,
            "color_filter": f.color,
            "type_filter": f.type,
            "finish_filter": f.finish,
            "sort_mode": f.sort,
            "group_by": f.group,
            "view_mode": f.view,
            "has_more": has_more,
            "next_page_url": next_page_url,
            "prev_group": "",
            "page_only": False,
            "group_key_and_label_fn": queries.group_key_and_label,
            "is_pending_fn": lambda item_id: item_id in pending_ids,
            "active_tab": "binder",
        }
        if is_hx:
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
            delay_sec = float(app.config.get("DISCORD_WEBHOOK_DELAY_SEC", 300))
            job_queue.enqueue("discord_notify", ref_id=item.id, delay_sec=delay_sec)
        oracle_dupe_map = queries.compute_oracle_dupe_map(
            user.id,
            oracle_ids=[item.card.oracle_id] if item.card else [],
        )
        return render_template(
            "partials/binder_card_row.html",
            item=item,
            oracle_dupe_map=oracle_dupe_map,
            is_pending=job_queue.is_pending("meeple_lookup", item.id),
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

        oracle_dupe_map = queries.compute_oracle_dupe_map(
            user.id,
            oracle_ids=[item.card.oracle_id] if item.card else [],
        )
        return render_template(
            "partials/binder_card_row.html",
            item=item,
            oracle_dupe_map=oracle_dupe_map,
            is_pending=job_queue.is_pending("meeple_lookup", item.id),
        )

    @app.route("/binder/<int:item_id>/row", methods=["GET"])
    @login_required
    def get_item_row(item_id: int):
        user = get_current_user()
        item = BinderItem.query.filter_by(id=item_id, user_id=user.id).first_or_404()
        oracle_dupe_map = queries.compute_oracle_dupe_map(
            user.id,
            oracle_ids=[item.card.oracle_id] if item.card else [],
        )
        return render_template(
            "partials/binder_card_row.html",
            item=item,
            oracle_dupe_map=oracle_dupe_map,
            is_pending=job_queue.is_pending("meeple_lookup", item.id),
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

        oracle_dupe_map = queries.compute_oracle_dupe_map(
            user.id,
            oracle_ids=[item.card.oracle_id] if item.card else [],
        )
        return render_template(
            "partials/binder_card_row.html",
            item=item,
            oracle_dupe_map=oracle_dupe_map,
            is_pending=job_queue.is_pending("meeple_lookup", item.id),
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

        select_all_raw = str(request.form.get("select_all") or json_data.get("select_all") or "").strip().lower()
        select_all = select_all_raw in ("1", "true", "yes", "on")

        if select_all:
            f = queries.CardFilters.from_request(request.form if not request.is_json else json_data, scope="binder")
            items_query = queries.apply_card_filters(
                BinderItem.query.join(Card, BinderItem.scryfall_id == Card.scryfall_id)
                .options(db.joinedload(BinderItem.card)),
                f,
                scope="binder",
                user_id=user.id,
                apply_order=False,
            )
            items = items_query.all()
        else:
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

        queued_count = job_queue.enqueue_many("meeple_lookup", to_queue) if to_queue else 0
        if discord_to_queue:
            delay_sec = float(app.config.get("DISCORD_WEBHOOK_DELAY_SEC", 300))
            job_queue.enqueue_many("discord_notify", discord_to_queue, delay_sec=delay_sec)

        if request.is_json:
            return jsonify({"updated": len(items), "queued": queued_count})

        f = queries.CardFilters.from_request(request.form, scope="binder")

        if request.headers.get("HX-Request"):
            filtered_query = queries.apply_card_filters(
                BinderItem.query.join(Card, BinderItem.scryfall_id == Card.scryfall_id)
                .options(db.joinedload(BinderItem.card)),
                f,
                scope="binder",
                user_id=user.id,
            )
            updated_items, has_more = queries.paginate(filtered_query, 1, per_page=60)
            page_oracle_ids = [item.card.oracle_id for item in updated_items if item.card]
            oracle_dupe_map = queries.compute_oracle_dupe_map(user.id, oracle_ids=page_oracle_ids)
            pending_ids = job_queue.pending_ref_ids("meeple_lookup", [item.id for item in updated_items])
            last_group_key = (
                queries.group_key_and_label(updated_items[-1], f.group)[0]
                if updated_items and f.group != "none"
                else ""
            )
            next_page_url = (
                url_for("binder_view", **f.to_query_params("binder", page=2, prev_group=last_group_key))
                if has_more
                else ""
            )
            count_q = queries.apply_card_filters(
                db.session.query(func.count(BinderItem.id)).join(Card, BinderItem.scryfall_id == Card.scryfall_id),
                f,
                scope="binder",
                user_id=user.id,
                apply_order=False,
            )
            total_matches = int(count_q.scalar() or 0)
            return render_template(
                "partials/binder_list.html",
                items=updated_items,
                oracle_dupe_map=oracle_dupe_map,
                total_matches=total_matches,
                active_filter=f.status,
                search_q=f.q,
                color_filter=f.color,
                type_filter=f.type,
                finish_filter=f.finish,
                sort_mode=f.sort,
                group_by=f.group,
                view_mode=f.view,
                has_more=has_more,
                next_page_url=next_page_url,
                prev_group="",
                page_only=False,
                group_key_and_label_fn=queries.group_key_and_label,
                is_pending_fn=lambda item_id: item_id in pending_ids,
            )

        msg = f"Updated {len(items)} card(s) to {new_status.replace('_', ' ').title()}."
        if queued_count:
            msg += f" Queued {queued_count} Mighty Meeple lookup(s) with polite pacing."
        flash(msg, "success")
        return redirect(url_for("binder_view", filter=f.status, q=f.q))

    # -------------------------------------------------------------------------
    # Pod Feed (Shared across Pod Members — Public Items Only)
    # -------------------------------------------------------------------------

    @app.route("/pod")
    @login_required
    def pod_feed():
        f = queries.CardFilters.from_request(request.args, scope="pod")

        base_query = (
            BinderItem.query.join(User, BinderItem.user_id == User.id)
            .join(Card, BinderItem.scryfall_id == Card.scryfall_id)
            .options(db.joinedload(BinderItem.user), db.joinedload(BinderItem.card))
        )
        filtered_query = queries.apply_card_filters(base_query, f, scope="pod")
        items, has_more = queries.paginate(filtered_query, f.page, per_page=60)

        last_group_key = ""
        if items and f.group != "none":
            last_group_key = queries.group_key_and_label(items[-1], f.group)[0]

        next_page_url = (
            url_for("pod_feed", **f.to_query_params("pod", page=f.page + 1, prev_group=last_group_key))
            if has_more
            else ""
        )

        is_hx = bool(request.headers.get("HX-Request"))
        if is_hx and f.page > 1:
            return render_template(
                "partials/pod_feed_list.html",
                items=items,
                status_filter=f.status,
                selected_owner_id=f.owner_id,
                search_q=f.q,
                color_filter=f.color,
                type_filter=f.type,
                finish_filter=f.finish,
                sort_mode=f.sort,
                group_by=f.group,
                view_mode=f.view,
                has_more=has_more,
                next_page_url=next_page_url,
                prev_group=f.prev_group,
                page_only=True,
                group_key_and_label_fn=queries.group_key_and_label,
            )

        count_q = queries.apply_card_filters(
            db.session.query(func.count(BinderItem.id))
            .join(User, BinderItem.user_id == User.id)
            .join(Card, BinderItem.scryfall_id == Card.scryfall_id),
            f,
            scope="pod",
            apply_order=False,
        )
        total_matches = int(count_q.scalar() or 0)

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
            "total_matches": total_matches,
            "status_filter": f.status,
            "selected_owner_id": f.owner_id,
            "search_q": f.q,
            "color_filter": f.color,
            "type_filter": f.type,
            "finish_filter": f.finish,
            "sort_mode": f.sort,
            "group_by": f.group,
            "view_mode": f.view,
            "has_more": has_more,
            "next_page_url": next_page_url,
            "prev_group": "",
            "page_only": False,
            "group_key_and_label_fn": queries.group_key_and_label,
            "active_tab": "pod",
        }
        if is_hx:
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

        job_queue.enqueue(
            "drive_import",
            ref_id=imp.id,
            payload={"access_token": access_token},
            deduplicate=False,
        )
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
