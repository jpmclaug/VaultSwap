"""Comprehensive test suite for VaultSwap."""

from __future__ import annotations

import io
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

import services
from app import create_app
from models import BinderItem, Card, User, db, utc_now
from providers.mightymeeple import MightyMeepleProvider
from providers.scryfall import ScryfallProvider


@pytest.fixture()
def app():
    test_app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SECRET_KEY": "test-secret-key",
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "DEV_LOGIN_ENABLED": True,
        "INVITE_ONLY": False,
        "MEEPLE_CACHE_TTL_HOURS": 24.0,
        "MEEPLE_RATE_LIMIT_MIN_SEC": 1.5,
        "MEEPLE_RATE_LIMIT_MAX_SEC": 2.0,
    })
    with test_app.app_context():
        db.create_all()
        yield test_app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def _login_user(client, email: str, display_name: str, discord_handle: str = "") -> User:
    resp = client.post(
        "/auth/dev-login",
        data={"email": email, "display_name": display_name, "discord_handle": discord_handle},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 200)
    return User.query.filter_by(email=email.lower()).one()


def _make_card(
    name: str,
    set_code: str,
    collector_number: str,
    oracle_id: str | None = None,
    scryfall_id: str | None = None,
    usd_price: float = 10.00,
    usd_foil_price: float = 15.00,
) -> Card:
    card = Card(
        scryfall_id=scryfall_id or str(uuid.uuid4()),
        oracle_id=oracle_id or str(uuid.uuid4()),
        name=name,
        set_code=set_code,
        collector_number=collector_number,
        set_name=f"Set {set_code}",
        image_uri_normal=f"https://cards.scryfall.io/normal/{set_code.lower()}/{collector_number}.jpg",
        image_uri_small=f"https://cards.scryfall.io/small/{set_code.lower()}/{collector_number}.jpg",
        mana_cost="{2}{U}",
        type_line="Enchantment",
        usd_price=usd_price,
        usd_foil_price=usd_foil_price,
    )
    db.session.add(card)
    db.session.commit()
    return card


def test_auth_required_and_google_oauth_callback(app, client):
    # Unauthenticated access redirects to /login; API returns 401
    resp = client.get("/binder")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]

    api_resp = client.get("/api/scryfall/autocomplete?q=sol")
    assert api_resp.status_code == 401

    # Mock fallback Google OAuth 2.0 flow with GOOGLE_CLIENT_ID configured
    app.config["GOOGLE_CLIENT_ID"] = "test-google-client-id"
    app.config["GOOGLE_CLIENT_SECRET"] = "test-google-client-secret"

    auth_resp = client.get("/auth/google")
    assert auth_resp.status_code == 302
    assert "accounts.google.com/o/oauth2/v2/auth" in auth_resp.headers["Location"]

    with client.session_transaction() as sess:
        oauth_state = sess["oauth_state"]

    token_mock = MagicMock(status_code=200)
    token_mock.json.return_value = {"access_token": "fake-access-token"}

    userinfo_mock = MagicMock(status_code=200)
    userinfo_mock.json.return_value = {
        "sub": "google-sub-12345",
        "email": "Jason@Example.com",
        "name": "Jason McLaughlin",
        "picture": "https://lh3.googleusercontent.com/avatar.png",
    }

    with patch("app.requests.post", return_value=token_mock) as mock_post, \
         patch("app.requests.get", return_value=userinfo_mock) as mock_get:
        cb_resp = client.get(f"/auth/google/callback?code=auth-code-xyz&state={oauth_state}")
        assert cb_resp.status_code == 302
        mock_post.assert_called_once()
        mock_get.assert_called_once()

    user = User.query.filter_by(google_sub="google-sub-12345").one()
    assert user.email == "jason@example.com"
    assert user.display_name == "Jason McLaughlin"
    assert user.avatar_url == "https://lh3.googleusercontent.com/avatar.png"


def test_row_level_scoping_between_users(app, client):
    with app.app_context():
        card_a = _make_card("Mana Crypt", "2XM", "270", usd_price=95.00)
        card_b = _make_card("Sol Ring", "CMM", "410", usd_price=1.50)

        alice = _login_user(client, "alice@pod.local", "Alice")
        alice_item = BinderItem(user_id=alice.id, scryfall_id=card_a.scryfall_id, quantity=1, status="private")
        db.session.add(alice_item)
        db.session.commit()
        alice_item_id = alice_item.id

        # Switch to Bob
        bob = _login_user(client, "bob@pod.local", "Bob")
        bob_item = BinderItem(user_id=bob.id, scryfall_id=card_b.scryfall_id, quantity=1, status="private")
        db.session.add(bob_item)
        db.session.commit()

        # Bob sees Sol Ring in his private binder, never Alice's Mana Crypt
        binder_html = client.get("/binder").get_data(as_text=True)
        assert "Sol Ring" in binder_html
        assert "Mana Crypt" not in binder_html

        # Pod feed must NOT show Alice's or Bob's private cards
        pod_html = client.get("/pod").get_data(as_text=True)
        assert "Mana Crypt" not in pod_html
        assert "Sol Ring" not in pod_html

        # Bob cannot mutate Alice's binder item
        assert client.post(f"/binder/{alice_item_id}/status", data={"status": "for_trade"}).status_code == 404
        assert client.post(f"/binder/{alice_item_id}/quantity", data={"delta": 1}).status_code == 404
        assert client.post(f"/binder/{alice_item_id}/refresh-meeple").status_code == 404
        assert client.delete(f"/binder/{alice_item_id}").status_code == 404

        # Verify Alice's item stayed untouched and private
        refreshed = db.session.get(BinderItem, alice_item_id)
        assert refreshed.status == "private"
        assert refreshed.quantity == 1


def test_import_zero_meeple_traffic_and_scryfall_batching_of_75(app, client):
    _login_user(client, "jason@pod.local", "Jason")

    # Test ManaBox CSV upload + quick paste with 0 Mighty Meeple calls
    csv_payload = (
        "Binder Name,Name,Set code,Set name,Collector number,Foil,Rarity,Quantity,Scryfall ID,Condition\n"
        "Main,Sol Ring,CMM,Commander Masters,410,foil,uncommon,2,,near_mint\n"
        "Main,Arcane Signet,CMM,Commander Masters,394,normal,common,1,,lightly_played\n"
    )

    mock_sf_responses = []

    def fake_scryfall_request(self, method, url, **kwargs):
        resp = MagicMock(status_code=200)
        if url.endswith("/cards/collection"):
            idents = kwargs["json"]["identifiers"]
            # Must never exceed 75 identifiers per POST
            assert len(idents) <= 75
            data_cards = []
            for ident in idents:
                if "set" in ident and "collector_number" in ident:
                    cname = f"Card {ident['set'].upper()}-{ident['collector_number']}"
                    data_cards.append({
                        "id": str(uuid.uuid4()),
                        "oracle_id": str(uuid.uuid4()),
                        "name": cname,
                        "set": ident["set"],
                        "set_name": f"Set {ident['set'].upper()}",
                        "collector_number": ident["collector_number"],
                        "image_uris": {"normal": "https://img/normal.jpg", "small": "https://img/small.jpg"},
                        "mana_cost": "{2}",
                        "type_line": "Artifact",
                        "prices": {"usd": "2.00", "usd_foil": "4.00"},
                    })
                elif "name" in ident:
                    data_cards.append({
                        "id": str(uuid.uuid4()),
                        "oracle_id": str(uuid.uuid4()),
                        "name": ident["name"],
                        "set": "tst",
                        "set_name": "Test Set",
                        "collector_number": "1",
                        "image_uris": {"normal": "https://img/normal.jpg", "small": "https://img/small.jpg"},
                        "mana_cost": "{1}",
                        "type_line": "Artifact",
                        "prices": {"usd": "1.25", "usd_foil": "2.50"},
                    })
            mock_sf_responses.append(len(idents))
            resp.json.return_value = {"data": data_cards, "not_found": []}
            return resp
        resp.status_code = 404
        return resp

    with patch.object(ScryfallProvider, "_request", new=fake_scryfall_request), \
         patch.object(MightyMeepleProvider, "get_card_snapshot", side_effect=AssertionError("Mighty Meeple MUST NOT be called on import")) as mm_mock:
        resp = client.post(
            "/import",
            data={"csv_file": (io.BytesIO(csv_payload.encode("utf-8")), "manabox.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert mm_mock.call_count == 0

        items = BinderItem.query.all()
        assert len(items) == 2
        assert all(i.status == "private" for i in items)
        assert all(i.mm_checked_at is None for i in items)
        assert sorted((i.finish, i.condition, i.quantity) for i in items) == [
            ("foil", "NM", 2),
            ("nonfoil", "LP", 1),
        ]

        # Now import 160 distinct cards via quick paste to verify 75-card chunking (75 + 75 + 10)
        mock_sf_responses.clear()
        lines = "\n".join(f"1 Synthetic Card {idx}" for idx in range(160))
        resp2 = client.post("/import", data={"quick_text": lines}, follow_redirects=True)
        assert resp2.status_code == 200
        assert mock_sf_responses == [75, 75, 10]
        assert mm_mock.call_count == 0


def test_htmx_status_toggle_on_tag_snapshot_and_no_requery_when_checked(app, client):
    with app.app_context():
        user = _login_user(client, "jason@pod.local", "Jason")
        card = _make_card("Rhystic Study", "WOT", "25", usd_price=42.50)
        item = BinderItem(user_id=user.id, scryfall_id=card.scryfall_id, quantity=1, finish="nonfoil", status="private")
        db.session.add(item)
        db.session.commit()
        item_id = item.id

        snapshot_payload = {
            "in_stock": True,
            "retail_price": 14.99,
            "buylist_cash": 7.50,
            "buylist_credit": 9.75,
        }

        with patch.object(MightyMeepleProvider, "get_card_snapshot", return_value=snapshot_payload) as mm_mock:
            # 1. Toggling to 'private' does not query Mighty Meeple
            resp_priv = client.post(f"/binder/{item_id}/status", data={"status": "private"})
            assert resp_priv.status_code == 200
            assert mm_mock.call_count == 0

            # 2. Toggling to 'for_trade' when mm_checked_at is None triggers Mighty Meeple ONCE
            resp_trade = client.post(f"/binder/{item_id}/status", data={"status": "for_trade"})
            assert resp_trade.status_code == 200
            assert mm_mock.call_count == 1
            html = resp_trade.get_data(as_text=True)
            assert "Meeple: In Stock ($14.99)" in html
            assert "$7.50 Cash" in html
            assert "$9.75 Credit" in html

            # 3. Switching to 'meeple_bound', back to 'private', and back to 'meeple_bound'
            #    does NOT re-query Mighty Meeple because mm_checked_at is already populated
            client.post(f"/binder/{item_id}/status", data={"status": "meeple_bound"})
            client.post(f"/binder/{item_id}/status", data={"status": "private"})
            resp_again = client.post(f"/binder/{item_id}/status", data={"status": "meeple_bound"})
            assert resp_again.status_code == 200
            assert mm_mock.call_count == 1

            # 4. Explicit manual refresh button DOES refresh live
            updated_snapshot = {
                "in_stock": False,
                "retail_price": 16.50,
                "buylist_cash": 8.00,
                "buylist_credit": 10.40,
            }
            mm_mock.return_value = updated_snapshot
            resp_refresh = client.post(f"/binder/{item_id}/refresh-meeple")
            assert resp_refresh.status_code == 200
            assert mm_mock.call_count == 2
            refresh_html = resp_refresh.get_data(as_text=True)
            assert "Meeple: Out of Stock ($16.50)" in refresh_html
            assert "$8.00 Cash" in refresh_html
            assert "$10.40 Credit" in refresh_html


def test_pod_wide_24h_meeple_cache_sharing(app, client):
    with app.app_context():
        card = _make_card("Smothering Tithe", "CMM", "57", usd_price=24.00, usd_foil_price=36.00)

        alice = _login_user(client, "alice@pod.local", "Alice")
        alice_item = BinderItem(user_id=alice.id, scryfall_id=card.scryfall_id, finish="nonfoil", status="private")
        db.session.add(alice_item)
        db.session.commit()
        alice_item_id = alice_item.id

        with patch.object(
            MightyMeepleProvider,
            "get_card_snapshot",
            return_value={
                "in_stock": True,
                "retail_price": 26.99,
                "buylist_cash": 14.00,
                "buylist_credit": 18.20,
            },
        ) as mm_mock:
            # Alice tags nonfoil -> 1 live Mighty Meeple request
            client.post(f"/binder/{alice_item_id}/status", data={"status": "for_trade"})
            assert mm_mock.call_count == 1

            # Bob tags the exact same printing & finish within 24h -> 0 new external requests!
            bob = _login_user(client, "bob@pod.local", "Bob")
            bob_item_nf = BinderItem(user_id=bob.id, scryfall_id=card.scryfall_id, finish="nonfoil", status="private")
            bob_item_foil = BinderItem(user_id=bob.id, scryfall_id=card.scryfall_id, finish="foil", status="private")
            db.session.add_all([bob_item_nf, bob_item_foil])
            db.session.commit()

            resp_bob_nf = client.post(f"/binder/{bob_item_nf.id}/status", data={"status": "meeple_bound"})
            assert resp_bob_nf.status_code == 200
            assert mm_mock.call_count == 1  # Still 1! Copied from Alice's pod cache
            bob_nf_refreshed = db.session.get(BinderItem, bob_item_nf.id)
            assert float(bob_nf_refreshed.mm_retail_price) == 26.99
            assert float(bob_nf_refreshed.mm_buylist_cash) == 14.00
            assert float(bob_nf_refreshed.mm_buylist_credit) == 18.20

            # Foil finish does NOT collide with nonfoil cache -> triggers live lookup (call #2)
            client.post(f"/binder/{bob_item_foil.id}/status", data={"status": "for_trade"})
            assert mm_mock.call_count == 2

            # Expire Alice's and Bob's nonfoil cache (> 24h ago), then Carol tags nonfoil -> triggers live lookup (call #3)
            stale_ts = utc_now() - timedelta(hours=25)
            db.session.get(BinderItem, alice_item_id).mm_checked_at = stale_ts
            db.session.get(BinderItem, bob_item_nf.id).mm_checked_at = stale_ts
            db.session.commit()

            carol = _login_user(client, "carol@pod.local", "Carol")
            carol_item = BinderItem(user_id=carol.id, scryfall_id=card.scryfall_id, finish="nonfoil", status="private")
            db.session.add(carol_item)
            db.session.commit()

            client.post(f"/binder/{carol_item.id}/status", data={"status": "for_trade"})
            assert mm_mock.call_count == 3


def test_bulk_status_worker_rate_limit_guard_and_pod_cache(app, client):
    with app.app_context():
        pod_friend = User(google_sub="sub-friend", email="friend@pod.local", display_name="Friend")
        db.session.add(pod_friend)
        c1 = _make_card("Cyclonic Rift", "RVR", "40")
        c2 = _make_card("Demonic Tutor", "CMM", "150")
        c3 = _make_card("Dockside Extortionist", "2X2", "107")
        db.session.flush()

        # Pre-seed pod cache for c1 only
        db.session.add(
            BinderItem(
                user_id=pod_friend.id,
                scryfall_id=c1.scryfall_id,
                finish="nonfoil",
                status="for_trade",
                mm_in_stock=True,
                mm_retail_price=35.00,
                mm_buylist_cash=19.00,
                mm_buylist_credit=24.70,
                mm_checked_at=utc_now(),
            )
        )
        db.session.commit()

        jason = _login_user(client, "jason@pod.local", "Jason")
        i1 = BinderItem(user_id=jason.id, scryfall_id=c1.scryfall_id, finish="nonfoil", status="private")
        i2 = BinderItem(user_id=jason.id, scryfall_id=c2.scryfall_id, finish="nonfoil", status="private")
        i3 = BinderItem(user_id=jason.id, scryfall_id=c3.scryfall_id, finish="nonfoil", status="private")
        db.session.add_all([i1, i2, i3])
        db.session.commit()
        ids = [i1.id, i2.id, i3.id]

        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("services.time.sleep", side_effect=fake_sleep), \
             patch.object(
                 MightyMeepleProvider,
                 "get_card_snapshot",
                 return_value={
                     "in_stock": True,
                     "retail_price": 40.00,
                     "buylist_cash": 21.00,
                     "buylist_credit": 27.30,
                 },
             ) as mm_mock:
            resp = client.post(
                "/binder/bulk-status",
                json={"item_ids": ids, "status": "meeple_bound"},
            )
            assert resp.status_code == 200
            payload = resp.get_json()
            assert payload["updated"] == 3
            # i1 hit pod cache synchronously, only i2 & i3 were queued
            assert payload["queued"] == 2

            assert app.meeple_worker.wait_until_idle(timeout=5.0)
            assert mm_mock.call_count == 2  # i2 and i3 only!
            # Polite 1.5s-2.0s jittered delay applied before second live request
            assert len(sleep_calls) == 1
            assert 1.4 <= sleep_calls[0] <= 2.0

            db.session.expire_all()
            for item_id in ids:
                itm = db.session.get(BinderItem, item_id)
                assert itm.status == "meeple_bound"
                assert itm.mm_checked_at is not None


def test_binder_filters_including_cross_set_oracle_dupes_and_recent(app, client):
    with app.app_context():
        user = _login_user(client, "jason@pod.local", "Jason")
        shared_oracle = str(uuid.uuid4())

        # Same oracle_id across two different printings (qty=1 each -> total 2 across 2 sets!)
        rhystic_wot = _make_card("Rhystic Study", "WOT", "25", oracle_id=shared_oracle)
        rhystic_pry = _make_card("Rhystic Study", "PRY", "45", oracle_id=shared_oracle)
        # Single row dupe (qty=3)
        sol_ring = _make_card("Sol Ring", "CMM", "410")
        # Unique old card (not recent, not dupe)
        old_card = _make_card("Mystic Remora", "ICE", "87")

        now = utc_now()
        old_ts = now - timedelta(days=14)

        b1 = BinderItem(user_id=user.id, scryfall_id=rhystic_wot.scryfall_id, quantity=1, status="private", created_at=now)
        b2 = BinderItem(user_id=user.id, scryfall_id=rhystic_pry.scryfall_id, quantity=1, status="for_trade", created_at=old_ts)
        b3 = BinderItem(user_id=user.id, scryfall_id=sol_ring.scryfall_id, quantity=3, status="meeple_bound", created_at=now)
        b4 = BinderItem(user_id=user.id, scryfall_id=old_card.scryfall_id, quantity=1, status="private", created_at=old_ts)
        db.session.add_all([b1, b2, b3, b4])
        db.session.commit()

        # Dupes filter must include both Rhystic Study printings + Sol Ring, and exclude Mystic Remora
        dupes_html = client.get("/binder?filter=dupes").get_data(as_text=True)
        assert "WOT #25" in dupes_html
        assert "PRY #45" in dupes_html
        assert "2x owned (2 sets)" in dupes_html
        assert "Sol Ring" in dupes_html
        assert "Mystic Remora" not in dupes_html

        # Recent filter (last 7 days) excludes 14-day-old items
        recent_html = client.get("/binder?filter=recent").get_data(as_text=True)
        assert "WOT #25" in recent_html
        assert "Sol Ring" in recent_html
        assert "PRY #45" not in recent_html
        assert "Mystic Remora" not in recent_html

        # Status pills
        trade_html = client.get("/binder?filter=for_trade").get_data(as_text=True)
        assert "PRY #45" in trade_html
        assert "Sol Ring" not in trade_html

        meeple_html = client.get("/binder?filter=meeple_bound").get_data(as_text=True)
        assert "Sol Ring" in meeple_html
        assert "PRY #45" not in meeple_html

        # HTMX search partial
        hx_html = client.get("/binder?q=Remora", headers={"HX-Request": "true"}).get_data(as_text=True)
        assert "Mystic Remora" in hx_html
        assert "Sol Ring" not in hx_html
        assert "<html" not in hx_html.lower()


def test_pod_feed_sorting_filtering_and_quantity_controls(app, client):
    with app.app_context():
        jason = _login_user(client, "jason@pod.local", "Jason", "@jason_mtg")
        alex = User(google_sub="dev-alex", email="alex@pod.local", display_name="Alex", discord_handle="@alex_edh")
        db.session.add(alex)
        db.session.commit()

        c_trade = _make_card("Force of Will", "DMR", "50", usd_price=75.00)
        c_meeple = _make_card("The One Ring", "LTR", "246", usd_price=69.00)
        c_private = _make_card("Black Lotus", "LEA", "232", usd_price=9999.00)

        now = utc_now()
        # Note: c_trade was updated MORE recently than c_meeple, yet meeple_bound must appear FIRST!
        i_trade = BinderItem(
            user_id=jason.id,
            scryfall_id=c_trade.scryfall_id,
            quantity=1,
            status="for_trade",
            updated_at=now,
        )
        i_meeple = BinderItem(
            user_id=alex.id,
            scryfall_id=c_meeple.scryfall_id,
            quantity=1,
            status="meeple_bound",
            mm_in_stock=False,
            mm_retail_price=72.99,
            mm_buylist_cash=42.00,
            mm_buylist_credit=54.60,
            mm_checked_at=now - timedelta(minutes=30),
            updated_at=now - timedelta(minutes=30),
        )
        i_priv = BinderItem(
            user_id=alex.id,
            scryfall_id=c_private.scryfall_id,
            quantity=1,
            status="private",
        )
        db.session.add_all([i_trade, i_meeple, i_priv])
        db.session.commit()

        pod_html = client.get("/pod").get_data(as_text=True)
        assert "Black Lotus" not in pod_html
        assert "The One Ring" in pod_html
        assert "Force of Will" in pod_html
        assert "@Alex" in pod_html
        assert "@alex_edh" in pod_html
        # Meeple Bound appears before For Trade in the feed HTML
        assert pod_html.index("The One Ring") < pod_html.index("Force of Will")

        # Filter by owner_id
        alex_only = client.get(f"/pod?owner_id={alex.id}").get_data(as_text=True)
        assert "The One Ring" in alex_only
        assert "Force of Will" not in alex_only

        # Filter by status=for_trade
        trade_only = client.get("/pod?status=for_trade").get_data(as_text=True)
        assert "Force of Will" in trade_only
        assert "The One Ring" not in trade_only

        # Filter by search query with HX-Request
        search_pod = client.get("/pod?q=Ring", headers={"HX-Request": "true"}).get_data(as_text=True)
        assert "The One Ring" in search_pod
        assert "Force of Will" not in search_pod
        assert "<html" not in search_pod.lower()

        # Quantity decrement to 0 deletes the item cleanly
        inc_resp = client.post(f"/binder/{i_trade.id}/quantity", data={"delta": 1})
        assert inc_resp.status_code == 200
        assert db.session.get(BinderItem, i_trade.id).quantity == 2

        dec1 = client.post(f"/binder/{i_trade.id}/quantity", data={"delta": -1})
        assert dec1.status_code == 200
        assert db.session.get(BinderItem, i_trade.id).quantity == 1

        dec2 = client.post(f"/binder/{i_trade.id}/quantity", data={"delta": -1})
        assert dec2.status_code == 200
        assert dec2.get_data(as_text=True) == ""
        assert db.session.get(BinderItem, i_trade.id) is None


def test_neon_env_vars_resolution(monkeypatch):
    import importlib
    import config

    try:
        # 1. Empty env falls back to SQLite
        with monkeypatch.context() as m:
            m.setenv("DATABASE_URL", "")
            m.setenv("DATABASE_URL_POOLED", "")
            m.setenv("NEON_AUTH_BASE_URL", "")
            importlib.reload(config)
            assert config._resolve_database_url() == "sqlite:///vaultswap.db"
            assert config.Config.SQLALCHEMY_DATABASE_URI == "sqlite:///vaultswap.db"
            assert config.Config.SQLALCHEMY_ENGINE_OPTIONS == {"pool_pre_ping": True}
            assert config.Config.NEON_AUTH_BASE_URL == ""

        # 2. DATABASE_URL only is used as fallback
        with monkeypatch.context() as m:
            m.setenv("DATABASE_URL", "postgres://user:pass@ep-direct.us-east-2.aws.neon.tech/neondb?sslmode=require")
            m.setenv("DATABASE_URL_POOLED", "")
            m.setenv("NEON_AUTH_BASE_URL", "https://ep-auth.neonauth.us-east-2.aws.neon.tech/neondb/auth/")
            importlib.reload(config)
            assert "ep-direct.us-east-2.aws.neon.tech" in config.Config.SQLALCHEMY_DATABASE_URI
            assert config.Config.SQLALCHEMY_DATABASE_URI.startswith("postgresql")
            assert config.Config.DATABASE_URL.startswith("postgres://")
            assert config.Config.NEON_AUTH_BASE_URL == "https://ep-auth.neonauth.us-east-2.aws.neon.tech/neondb/auth"

        # 3. DATABASE_URL_POOLED takes precedence over DATABASE_URL when both are set
        with monkeypatch.context() as m:
            m.setenv("DATABASE_URL", "postgresql://user:pass@ep-direct.us-east-2.aws.neon.tech/neondb?sslmode=require")
            m.setenv("DATABASE_URL_POOLED", "postgresql://user:pass@ep-direct-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require")
            importlib.reload(config)
            assert "ep-direct-pooler.us-east-2.aws.neon.tech" in config.Config.SQLALCHEMY_DATABASE_URI

        # 4. psycopg v3 pooled URLs disable prepared statements for PgBouncer compatibility
        pooled_opts = config._build_engine_options(
            "postgresql+psycopg://user:pass@ep-direct-pooler.us-east-2.aws.neon.tech/neondb",
            pooled_url="",
        )
        assert pooled_opts["connect_args"] == {"prepare_threshold": None}

        pooled_env_opts = config._build_engine_options(
            "postgresql+psycopg://user:pass@ep-custom.us-east-2.aws.neon.tech/neondb",
            pooled_url="postgresql://user:pass@ep-custom.us-east-2.aws.neon.tech/neondb",
        )
        assert pooled_env_opts["connect_args"] == {"prepare_threshold": None}

        # psycopg2 / unpooled URLs do not inject psycopg v3 connect_args
        psycopg2_opts = config._build_engine_options(
            "postgresql://user:pass@ep-direct-pooler.us-east-2.aws.neon.tech/neondb",
            pooled_url="postgresql://user:pass@ep-direct-pooler.us-east-2.aws.neon.tech/neondb",
        )
        assert "connect_args" not in psycopg2_opts
    finally:
        importlib.reload(config)


def test_demo_seed_removed(client):
    resp = client.post("/auth/seed-demo-pod")
    assert resp.status_code == 404

    login_html = client.get("/login").get_data(as_text=True)
    assert "Demo Pod" not in login_html
    assert "seed-demo-pod" not in login_html


def test_invite_only_login(app, client):
    app.config["INVITE_ONLY"] = True
    app.config["ADMIN_EMAILS"] = {"jpmclaug@gmail.com"}

    # 1. Unknown email is turned away with no user row created
    resp = client.post("/auth/dev-login", data={"email": "stranger@example.com"}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert User.query.filter_by(email="stranger@example.com").first() is None

    # 2. Pre-invited pending user can sign in and placeholder google_sub is replaced
    with app.app_context():
        invited = User(
            google_sub="invite:friend@example.com",
            email="friend@example.com",
            display_name="Friend",
            is_admin=False,
        )
        db.session.add(invited)
        db.session.commit()
        assert invited.is_pending is True

    resp2 = client.post("/auth/dev-login", data={"email": "friend@example.com"}, follow_redirects=False)
    assert resp2.status_code == 302
    assert "/binder" in resp2.headers["Location"] or resp2.headers["Location"].endswith("/")
    friend = User.query.filter_by(email="friend@example.com").one()
    assert friend.google_sub == "dev-friend@example.com"
    assert friend.is_pending is False
    assert friend.is_admin is False

    # 3. Bootstrap admin in ADMIN_EMAILS gets in and becomes is_admin=True
    client.get("/logout")
    resp3 = client.post("/auth/dev-login", data={"email": "jpmclaug@gmail.com"}, follow_redirects=False)
    assert resp3.status_code == 302
    admin = User.query.filter_by(email="jpmclaug@gmail.com").one()
    assert admin.is_admin is True


def test_admin_users_page_access_and_add(app, client):
    app.config["INVITE_ONLY"] = True
    app.config["ADMIN_EMAILS"] = {"jpmclaug@gmail.com"}

    # Pre-invite a non-admin user and sign them in
    with app.app_context():
        db.session.add(User(google_sub="invite:member@pod.local", email="member@pod.local", display_name="Member"))
        db.session.commit()

    _login_user(client, "member@pod.local", "Member")
    non_admin_get = client.get("/admin/users")
    assert non_admin_get.status_code == 302
    non_admin_post = client.post("/admin/users", data={"email": "x@y.com"}, content_type="application/x-www-form-urlencoded")
    assert non_admin_post.status_code == 302
    assert User.query.filter_by(email="x@y.com").first() is None

    # Switch to admin
    client.get("/logout")
    admin = _login_user(client, "jpmclaug@gmail.com", "Admin")

    # Admin GET lists users
    page_html = client.get("/admin/users").get_data(as_text=True)
    assert "member@pod.local" in page_html
    assert "jpmclaug@gmail.com" in page_html

    # Admin POST creates a pending user
    add_resp = client.post("/admin/users", data={"email": "NewFriend@Gmail.com", "display_name": "New Friend"})
    assert add_resp.status_code == 302
    new_u = User.query.filter_by(email="newfriend@gmail.com").one()
    assert new_u.is_pending is True
    assert new_u.is_admin is False

    # Duplicate email (different case) is rejected
    client.post("/admin/users", data={"email": "NEWFRIEND@gmail.com", "display_name": "Dup"})
    assert User.query.filter_by(email="newfriend@gmail.com").count() == 1

    # Cannot revoke active user or self
    member = User.query.filter_by(email="member@pod.local").one()
    client.post(f"/admin/users/{member.id}/revoke")
    assert db.session.get(User, member.id) is not None
    client.post(f"/admin/users/{admin.id}/revoke")
    assert db.session.get(User, admin.id) is not None

    # Can revoke pending invite
    client.post(f"/admin/users/{new_u.id}/revoke")
    assert db.session.get(User, new_u.id) is None


def test_parse_card_date_formats():
    from datetime import timezone
    from drive_sync import parse_card_date

    dt_iso = parse_card_date("2026-11-10T14:30:00Z")
    assert dt_iso is not None and dt_iso.tzinfo == timezone.utc and dt_iso.year == 2026 and dt_iso.hour == 14

    dt_offset = parse_card_date("2026-11-10T10:30:00-04:00")
    assert dt_offset == dt_iso

    dt_date = parse_card_date("2026-11-10")
    assert dt_date is not None and dt_date.hour == 0 and dt_date.day == 10

    dt_us = parse_card_date("11/10/2026 09:15")
    assert dt_us is not None and dt_us.month == 11 and dt_us.minute == 15

    dt_epoch_s = parse_card_date("1700000000")
    assert dt_epoch_s is not None and dt_epoch_s.year == 2023

    dt_epoch_ms = parse_card_date("1700000000000")
    assert dt_epoch_ms == dt_epoch_s

    assert parse_card_date("not-a-date") is None
    assert parse_card_date("") is None


def test_drive_incremental_import(app, client):
    from contextlib import contextmanager
    from drive_sync import run_drive_import
    from models import DriveImport

    user = _login_user(client, "drive@pod.local", "DriveUser")
    c1 = _make_card("Sol Ring", "CMM", "410")
    c2 = _make_card("Rhystic Study", "WOT", "25")
    c3 = _make_card("Cyclonic Rift", "RVR", "40")

    csv_v1 = (
        "Name,Set code,Collector number,Quantity,Foil,Condition,Date added\n"
        "Sol Ring,CMM,410,1,normal,NM,2026-11-01\n"
        "Rhystic Study,WOT,25,2,normal,NM,2026-11-05\n"
    )
    csv_v2 = (
        "Name,Set code,Collector number,Quantity,Foil,Condition,Date added\n"
        "Sol Ring,CMM,410,1,normal,NM,2026-11-01\n"
        "Rhystic Study,WOT,25,2,normal,NM,2026-11-05\n"
        "Cyclonic Rift,RVR,40,1,foil,NM,2026-11-05\n"  # same-day tie at watermark!
    )

    class FakeDriveClient:
        def __init__(self, meta, body):
            self.meta = meta
            self.body = body
            self.stream_opened = False

        def get_metadata(self, file_id, access_token):
            return self.meta

        @contextmanager
        def open_csv_stream(self, file_id, mime_type, access_token):
            self.stream_opened = True
            yield io.StringIO(self.body)

    # Run 1: imports both rows, saves file name + watermark
    imp1 = DriveImport(user_id=user.id, drive_file_id="file-1", file_name="ManaBox_Nov1.csv")
    db.session.add(imp1)
    db.session.commit()
    fc1 = FakeDriveClient({"id": "file-1", "name": "ManaBox_Nov1.csv", "mimeType": "text/csv", "md5Checksum": "md5-1", "size": 200}, csv_v1)
    run_drive_import(imp1.id, "tok", drive_client=fc1)

    db.session.refresh(imp1)
    assert imp1.status == "done"
    assert imp1.file_name == "ManaBox_Nov1.csv"
    assert imp1.rows_imported == 2
    assert imp1.cards_imported == 3
    assert imp1.max_card_created_at.strftime("%Y-%m-%d") == "2026-11-05"
    assert fc1.stream_opened is True

    # Run 2: same md5 -> unchanged, stream never opened
    imp2 = DriveImport(user_id=user.id, drive_file_id="file-1", file_name="ManaBox_Nov1.csv")
    db.session.add(imp2)
    db.session.commit()
    fc2 = FakeDriveClient({"id": "file-1", "name": "ManaBox_Nov1.csv", "mimeType": "text/csv", "md5Checksum": "md5-1", "size": 200}, csv_v1)
    run_drive_import(imp2.id, "tok", drive_client=fc2)

    db.session.refresh(imp2)
    assert imp2.status == "unchanged"
    assert fc2.stream_opened is False

    # Run 3: new file with extra row on the SAME date (tie-break via boundary_fingerprints)
    imp3 = DriveImport(user_id=user.id, drive_file_id="file-2", file_name="ManaBox_Nov5.csv")
    db.session.add(imp3)
    db.session.commit()
    fc3 = FakeDriveClient({"id": "file-2", "name": "ManaBox_Nov5.csv", "mimeType": "text/csv", "md5Checksum": "md5-2", "size": 300}, csv_v2)
    run_drive_import(imp3.id, "tok", drive_client=fc3)

    db.session.refresh(imp3)
    assert imp3.status == "done"
    assert imp3.rows_new == 1
    assert imp3.cards_imported == 1

    # Existing quantities unchanged; new card added
    assert BinderItem.query.filter_by(user_id=user.id, scryfall_id=c1.scryfall_id).one().quantity == 1
    assert BinderItem.query.filter_by(user_id=user.id, scryfall_id=c2.scryfall_id).one().quantity == 2
    assert BinderItem.query.filter_by(user_id=user.id, scryfall_id=c3.scryfall_id).one().quantity == 1


def test_drive_import_resumes_after_chunk_failure(app, client):
    from contextlib import contextmanager
    from drive_sync import run_drive_import
    from models import DriveImport

    app.config["DRIVE_IMPORT_CHUNK_ROWS"] = 2
    user = _login_user(client, "resume@pod.local", "ResumeUser")
    _make_card("Card A", "CMM", "1")
    _make_card("Card B", "CMM", "2")
    # Card C and D are NOT in local DB so Scryfall is called on chunk 2

    csv_body = (
        "Name,Set code,Collector number,Quantity,Date added\n"
        "Card A,CMM,1,1,2026-11-01\n"
        "Card B,CMM,2,1,2026-11-02\n"
        "Card C,CMM,3,1,2026-11-03\n"
        "Card D,CMM,4,1,2026-11-04\n"
    )

    class FakeDriveClient:
        def get_metadata(self, file_id, access_token):
            return {"id": file_id, "name": "Big.csv", "mimeType": "text/csv", "md5Checksum": "md5-big", "size": 300}

        @contextmanager
        def open_csv_stream(self, file_id, mime_type, access_token):
            yield io.StringIO(csv_body)

    failing_sf = MagicMock()
    failing_sf.resolve_collection_batch.side_effect = RuntimeError("Scryfall 503")

    imp1 = DriveImport(user_id=user.id, drive_file_id="f-big", file_name="Big.csv")
    db.session.add(imp1)
    db.session.commit()
    run_drive_import(imp1.id, "tok", drive_client=FakeDriveClient(), scryfall_provider=failing_sf)

    db.session.refresh(imp1)
    assert imp1.status == "failed"
    assert imp1.rows_imported == 2
    assert imp1.max_card_created_at.strftime("%Y-%m-%d") == "2026-11-02"

    # Re-run with working Scryfall: resumes from 2026-11-02, no duplicates on A or B
    ok_sf = MagicMock()
    ok_sf.resolve_collection_batch.return_value = (
        {
            0: {
                "scryfall_id": str(uuid.uuid4()),
                "oracle_id": str(uuid.uuid4()),
                "name": "Card C",
                "set_code": "CMM",
                "collector_number": "3",
            },
            1: {
                "scryfall_id": str(uuid.uuid4()),
                "oracle_id": str(uuid.uuid4()),
                "name": "Card D",
                "set_code": "CMM",
                "collector_number": "4",
            },
        },
        [],
    )
    imp2 = DriveImport(user_id=user.id, drive_file_id="f-big", file_name="Big.csv")
    db.session.add(imp2)
    db.session.commit()
    run_drive_import(imp2.id, "tok", drive_client=FakeDriveClient(), scryfall_provider=ok_sf)

    db.session.refresh(imp2)
    assert imp2.status == "done"
    assert imp2.rows_new == 2
    assert imp2.cards_imported == 2
    assert BinderItem.query.filter_by(user_id=user.id).count() == 4


def test_drive_import_missing_date_column(app, client):
    from contextlib import contextmanager
    from drive_sync import run_drive_import
    from models import DriveImport

    user = _login_user(client, "nodate@pod.local", "NoDate")

    class FakeDriveClient:
        def get_metadata(self, file_id, access_token):
            return {"id": file_id, "name": "NoDate.csv", "mimeType": "text/csv", "md5Checksum": "md5-nd", "size": 100}

        @contextmanager
        def open_csv_stream(self, file_id, mime_type, access_token):
            yield io.StringIO("Name,Set code,Collector number\nSol Ring,CMM,410\n")

    imp = DriveImport(user_id=user.id, drive_file_id="f-nd", file_name="NoDate.csv")
    db.session.add(imp)
    db.session.commit()
    run_drive_import(imp.id, "tok", drive_client=FakeDriveClient())

    db.session.refresh(imp)
    assert imp.status == "failed"
    assert "No created-date column found" in imp.error
    assert "Set code" in imp.error


def test_bulk_import_query_count(app, client):
    from sqlalchemy import event

    user = _login_user(client, "bulk@pod.local", "BulkUser")
    entries = []
    for i in range(300):
        c = _make_card(f"Bulk Card {i}", "TST", str(i + 1))
        entries.append({
            "name": c.name,
            "set_code": "TST",
            "collector_number": str(i + 1),
            "scryfall_id": c.scryfall_id,
            "quantity": 1,
            "finish": "nonfoil",
            "condition": "NM",
        })

    select_count = 0

    def count_selects(conn, cursor, statement, parameters, context, executemany):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(db.engine, "before_cursor_execute", count_selects)
    try:
        res = services.import_cards_to_binder(user.id, entries)
    finally:
        event.remove(db.engine, "before_cursor_execute", count_selects)

    assert res["imported_rows"] == 300
    assert select_count <= 10


def test_drive_routes_scoping(app, client):
    from models import DriveImport

    app.config["GOOGLE_CLIENT_ID"] = "expected-client-id"
    alice = _login_user(client, "alice_drv@pod.local", "Alice")
    imp = DriveImport(user_id=alice.id, drive_file_id="f1", file_name="Alice.csv", status="done")
    db.session.add(imp)
    db.session.commit()

    # Token aud mismatch is rejected
    bad_tok_resp = MagicMock(status_code=200)
    bad_tok_resp.json.return_value = {"aud": "other-app-client-id"}
    with patch("drive_sync.requests.Session.get", return_value=bad_tok_resp):
        r = client.post("/import/drive", json={"file_id": "f1", "access_token": "bad-tok"})
        assert r.status_code == 401

    # Bob cannot read Alice's DriveImport status
    _login_user(client, "bob_drv@pod.local", "Bob")
    assert client.get(f"/import/drive/{imp.id}/status").status_code == 404
    hist = client.get("/import/drive/history").get_json()
    assert hist["imports"] == []


def test_privacy_page_public(client):
    resp = client.get("/privacy")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Privacy Policy" in html
    assert "drive.file" in html

    login_html = client.get("/login").get_data(as_text=True)
    assert "/privacy" in login_html


def test_drive_client_open_csv_stream_handles_urllib3_eof():
    import csv
    import io
    import requests
    import urllib3
    from drive_sync import DriveClient, scan_new_rows

    csv_bytes = (
        b"\xef\xbb\xbfName,Set code,Collector number,Quantity,Foil,Condition,Date added\r\n"
        b"Sol Ring,CMM,410,1,normal,NM,2026-11-01\r\n"
        b"\"Rhystic\nStudy\",WOT,25,2,foil,NM,2026-11-05\r\n"
    )
    raw = urllib3.HTTPResponse(
        body=io.BytesIO(csv_bytes),
        headers={"Content-Length": str(len(csv_bytes))},
        preload_content=False,
    )
    resp = requests.Response()
    resp.status_code = 200
    resp.raw = raw

    session = MagicMock(spec=requests.Session)
    session.get.return_value = resp
    client = DriveClient(session=session)

    with client.open_csv_stream("file-1", "text/csv", "tok") as text_stream:
        scan = scan_new_rows(
            text_stream=text_stream,
            watermark=None,
            boundary_fps=[],
            first_import=True,
        )

    assert scan.rows_scanned == 2
    assert len(scan.new_entries) == 2
    assert scan.new_entries[0][2]["name"] == "Sol Ring"
    assert scan.new_entries[1][2]["name"] == "Rhystic\nStudy"


