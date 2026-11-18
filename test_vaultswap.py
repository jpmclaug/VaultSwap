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
        "WTF_CSRF_ENABLED": False,
        "JOB_DISPATCHER": "inline",
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
            # Mighty Meeple trade-in / credit listed before cash
            assert html.index("$9.75 Credit") < html.index("$7.50 Cash")

            # 3. Switching to 'meeple_bound', back to 'private', and back to 'meeple_bound'
            #    does NOT re-query Mighty Meeple because mm_checked_at is already populated
            client.post(f"/binder/{item_id}/status", data={"status": "meeple_bound"})
            item_mb = db.session.get(BinderItem, item_id)
            assert item_mb.status == "meeple_bound"
            assert item_mb.condition == "LP"
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
            assert refresh_html.index("$10.40 Credit") < refresh_html.index("$8.00 Cash")


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

        with patch("jobs.time.sleep", side_effect=fake_sleep), \
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

            processed = app.job_queue.run_due("meeple_lookup")
            assert processed == 2
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


def test_activity_logging_for_login_and_uploads(app, client):
    from models import ActivityLog

    app.config["INVITE_ONLY"] = True
    app.config["ADMIN_EMAILS"] = {"admin@pod.local"}

    # 1. Admin login generates an ActivityLog record
    client.post("/auth/dev-login", data={"email": "admin@pod.local", "display_name": "Admin Boss"})
    admin_login_log = ActivityLog.query.filter_by(action="LOGIN_DEV").first()
    assert admin_login_log is not None
    assert admin_login_log.user_email == "admin@pod.local"
    assert "Admin Boss" in (admin_login_log.user_name or "")

    # 2. Uploading a single card logs an upload event
    client.post("/import", data={"single_card_name": "Sol Ring", "single_quantity": "1"})
    upload_log = ActivityLog.query.filter_by(action="ADD_SINGLE").first()
    assert upload_log is not None
    assert upload_log.user_email == "admin@pod.local"
    assert "Sol Ring" in upload_log.details

    # 3. Uploading quick text list logs an upload event
    client.post("/import", data={"quick_text": "1 Lightning Bolt"})
    quick_log = ActivityLog.query.filter_by(action="UPLOAD_QUICK_LIST").first()
    assert quick_log is not None
    assert "Quick Text Paste" in quick_log.details

    # 4. Admin users page renders the activity logs section and filter tabs
    admin_page = client.get("/admin/users").get_data(as_text=True)
    assert "User Action &amp; Audit Log" in admin_page
    assert "LOGIN_DEV" in admin_page
    assert "ADD_SINGLE" in admin_page
    assert "admin@pod.local" in admin_page

    # 5. Filtering activity logs by action=uploads only returns upload logs
    uploads_filter_page = client.get("/admin/users?action=uploads").get_data(as_text=True)
    assert "ADD_SINGLE" in uploads_filter_page
    assert "LOGIN_DEV" not in uploads_filter_page

    # 6. Logout logs an event
    client.get("/logout")
    logout_log = ActivityLog.query.filter_by(action="LOGOUT").first()
    assert logout_log is not None
    assert logout_log.user_email == "admin@pod.local"



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


def test_mcp_privacy_boundary_excludes_private_cards(app, client):
    import mcp_server

    alice = _login_user(client, "alice_mcp@pod.local", "Alice Johnson", discord_handle="@alice_j")
    bob = _login_user(client, "bob_mcp@pod.local", "Bob Smith", discord_handle="@bob_s")

    mana_crypt = _make_card("Mana Crypt", "2XM", "270", usd_price=95.00)
    sol_ring = _make_card("Sol Ring", "CMM", "410", usd_price=2.50)
    rhystic = _make_card("Rhystic Study", "WOT", "25", usd_price=40.00)
    fow = _make_card("Force of Will", "DMR", "50", usd_price=75.00)

    alice_priv = BinderItem(user_id=alice.id, scryfall_id=mana_crypt.scryfall_id, quantity=1, status="private")
    alice_trade = BinderItem(user_id=alice.id, scryfall_id=sol_ring.scryfall_id, quantity=2, status="for_trade")
    bob_meeple = BinderItem(user_id=bob.id, scryfall_id=rhystic.scryfall_id, quantity=1, status="meeple_bound")
    bob_priv = BinderItem(user_id=bob.id, scryfall_id=fow.scryfall_id, quantity=1, status="private")
    db.session.add_all([alice_priv, alice_trade, bob_meeple, bob_priv])
    db.session.commit()

    feed = mcp_server.list_trade_cards()
    assert feed["total_matching"] == 2
    names = {entry["card"]["name"] for entry in feed["listings"]}
    assert names == {"Sol Ring", "Rhystic Study"}

    for entry in feed["listings"]:
        assert "email" not in entry["owner"]
        assert "google_sub" not in entry["owner"]

    # Rejects status='private'
    priv_query = mcp_server.list_trade_cards(status="private")
    assert "error" in priv_query
    assert priv_query["listings"] == []

    # Direct lookup by private item_id, scryfall_id, or card_name returns nothing
    assert mcp_server.get_trade_card_metadata(item_id=alice_priv.id)["found"] is False
    assert mcp_server.get_trade_card_metadata(scryfall_id=mana_crypt.scryfall_id)["found"] is False
    assert mcp_server.get_trade_card_metadata(card_name="Mana Crypt")["found"] is False

    with pytest.raises(ValueError):
        mcp_server.serialize_trade_item(alice_priv)

    traders = mcp_server.list_pod_traders()
    assert traders["trader_count"] == 2
    by_uid = {t["user_id"]: t for t in traders["traders"]}
    assert by_uid[alice.id]["total_quantity"] == 2
    assert by_uid[bob.id]["total_quantity"] == 1

    summary = mcp_server.get_pod_trade_summary()
    assert summary["total_listings"] == 2
    assert summary["total_cards_available"] == 3


def test_mcp_list_trade_cards_filters_sorting_and_metadata(app, client):
    import mcp_server

    alice = _login_user(client, "alice_f@pod.local", "Alice Mercer", discord_handle="@alice_mtg")
    bob = _login_user(client, "bob_f@pod.local", "Bob Vance", discord_handle="@bob_mtg")

    c_ring = Card(
        scryfall_id=str(uuid.uuid4()),
        oracle_id=str(uuid.uuid4()),
        name="Sol Ring",
        set_code="CMM",
        collector_number="410",
        set_name="Commander Masters",
        image_uri_normal="https://cards.scryfall.io/normal/cmm/410.jpg",
        image_uri_small="https://cards.scryfall.io/small/cmm/410.jpg",
        mana_cost="{1}",
        type_line="Artifact",
        usd_price=2.00,
        usd_foil_price=12.00,
    )
    c_tutor = Card(
        scryfall_id=str(uuid.uuid4()),
        oracle_id=str(uuid.uuid4()),
        name="Demonic Tutor",
        set_code="CMM",
        collector_number="150",
        set_name="Commander Masters",
        mana_cost="{1}{B}",
        type_line="Sorcery",
        usd_price=35.00,
        usd_foil_price=55.00,
    )
    c_etched = Card(
        scryfall_id=str(uuid.uuid4()),
        oracle_id=str(uuid.uuid4()),
        name="Urza's Saga",
        set_code="MH2",
        collector_number="259",
        set_name="Modern Horizons 2",
        mana_cost="",
        type_line="Enchantment Land — Urza's Saga",
        usd_price=38.00,
        usd_foil_price=48.00,
    )
    db.session.add_all([c_ring, c_tutor, c_etched])
    db.session.flush()

    now = utc_now()
    item_ring = BinderItem(
        user_id=alice.id,
        scryfall_id=c_ring.scryfall_id,
        quantity=2,
        finish="foil",
        condition="NM",
        status="for_trade",
        mm_in_stock=True,
        mm_retail_price=14.00,
        mm_buylist_cash=6.50,
        mm_buylist_credit=8.50,
        mm_checked_at=now,
    )
    item_tutor = BinderItem(
        user_id=bob.id,
        scryfall_id=c_tutor.scryfall_id,
        quantity=1,
        finish="nonfoil",
        condition="LP",
        status="meeple_bound",
        mm_in_stock=False,
        mm_retail_price=35.00,
        mm_buylist_cash=18.00,
        mm_buylist_credit=23.00,
        mm_checked_at=now - timedelta(minutes=5),
    )
    item_saga = BinderItem(
        user_id=alice.id,
        scryfall_id=c_etched.scryfall_id,
        quantity=1,
        finish="etched",
        condition="NM",
        status="for_trade",
        mm_checked_at=None,
    )
    db.session.add_all([item_ring, item_tutor, item_saga])
    db.session.commit()

    # Priority sort puts meeple_bound first
    all_res = mcp_server.list_trade_cards(sort_by="priority")
    assert all_res["total_matching"] == 3
    assert all_res["listings"][0]["listing"]["status"] == "meeple_bound"
    assert all_res["listings"][0]["card"]["name"] == "Demonic Tutor"

    # Verify full 4-block metadata on foil item_ring
    ring_res = mcp_server.list_trade_cards(query="Sol Ring")
    assert ring_res["total_matching"] == 1
    ring_meta = ring_res["listings"][0]
    assert ring_meta["listing"]["item_id"] == item_ring.id
    assert ring_meta["listing"]["unit_market_price"] == 12.00
    assert ring_meta["listing"]["line_market_value"] == 24.00
    assert ring_meta["card"]["set_code"] == "CMM"
    assert ring_meta["card"]["scryfall_uri"] == "https://scryfall.com/card/cmm/410"
    assert ring_meta["owner"]["handle_label"] == "@Alice"
    assert ring_meta["owner"]["discord_handle"] == "@alice_mtg"
    assert ring_meta["mighty_meeple"]["has_snapshot"] is True
    assert ring_meta["mighty_meeple"]["in_stock"] is True
    assert ring_meta["mighty_meeple"]["buylist_cash"] == 6.50
    assert ring_meta["mighty_meeple"]["line_buylist_cash"] == 13.00
    assert ring_meta["mighty_meeple"]["buylist_credit"] == 8.50
    assert ring_meta["mighty_meeple"]["line_buylist_credit"] == 17.00

    # Filters
    assert mcp_server.list_trade_cards(status="for_trade")["total_matching"] == 2
    assert mcp_server.list_trade_cards(status="meeple_bound")["total_matching"] == 1
    assert mcp_server.list_trade_cards(owner="@Alice")["total_matching"] == 2
    assert mcp_server.list_trade_cards(owner="bob_mtg")["total_matching"] == 1
    assert mcp_server.list_trade_cards(owner=str(bob.id))["total_matching"] == 1
    assert mcp_server.list_trade_cards(set_code="cmm")["total_matching"] == 2
    assert mcp_server.list_trade_cards(type_line="Artifact")["total_matching"] == 1
    assert mcp_server.list_trade_cards(finish="etched")["total_matching"] == 1
    assert mcp_server.list_trade_cards(condition="lightly played")["total_matching"] == 1
    assert mcp_server.list_trade_cards(min_price=10.0, max_price=40.0)["total_matching"] == 2
    assert mcp_server.list_trade_cards(meeple_in_stock=True)["total_matching"] == 1
    assert mcp_server.list_trade_cards(meeple_in_stock=False)["total_matching"] == 1

    # Price & name sorting
    desc_names = [x["card"]["name"] for x in mcp_server.list_trade_cards(sort_by="price_desc")["listings"]]
    assert desc_names == ["Urza's Saga", "Demonic Tutor", "Sol Ring"]
    asc_names = [x["card"]["name"] for x in mcp_server.list_trade_cards(sort_by="price_asc")["listings"]]
    assert asc_names == ["Sol Ring", "Demonic Tutor", "Urza's Saga"]
    alpha_names = [x["card"]["name"] for x in mcp_server.list_trade_cards(sort_by="name_asc")["listings"]]
    assert alpha_names == ["Demonic Tutor", "Sol Ring", "Urza's Saga"]


def test_mcp_get_trade_card_metadata_cross_printing_and_live_scryfall(app, client):
    import mcp_server

    alice = _login_user(client, "alice_x@pod.local", "Alice")
    bob = _login_user(client, "bob_x@pod.local", "Bob")
    charlie = _login_user(client, "charlie_x@pod.local", "Charlie")

    shared_oracle = str(uuid.uuid4())
    rhystic_wot = _make_card("Rhystic Study", "WOT", "25", oracle_id=shared_oracle, usd_price=42.00)
    rhystic_pcy = _make_card("Rhystic Study", "PCY", "45", oracle_id=shared_oracle, usd_price=38.00)
    rhystic_cmm = _make_card("Rhystic Study", "CMM", "114", oracle_id=shared_oracle, usd_price=39.00)

    alice_wot = BinderItem(
        user_id=alice.id,
        scryfall_id=rhystic_wot.scryfall_id,
        quantity=1,
        finish="nonfoil",
        status="for_trade",
        mm_checked_at=None,
    )
    bob_pcy = BinderItem(
        user_id=bob.id,
        scryfall_id=rhystic_pcy.scryfall_id,
        quantity=2,
        finish="nonfoil",
        status="meeple_bound",
    )
    # Private copy in CMM must NOT leak into other_printings_in_pod
    charlie_priv_cmm = BinderItem(
        user_id=charlie.id,
        scryfall_id=rhystic_cmm.scryfall_id,
        quantity=1,
        finish="nonfoil",
        status="private",
    )
    # A 24h pod-cached Mighty Meeple snapshot on the exact WOT printing
    charlie_wot_cache = BinderItem(
        user_id=charlie.id,
        scryfall_id=rhystic_wot.scryfall_id,
        quantity=1,
        finish="nonfoil",
        status="private",
        mm_in_stock=True,
        mm_retail_price=44.00,
        mm_buylist_cash=24.00,
        mm_buylist_credit=31.00,
        mm_checked_at=utc_now() - timedelta(hours=2),
    )
    db.session.add_all([alice_wot, bob_pcy, charlie_priv_cmm, charlie_wot_cache])
    db.session.commit()

    sf_resp = MagicMock(status_code=200)
    sf_resp.json.return_value = {
        "oracle_text": "Whenever an opponent casts a spell, you may draw a card unless that player pays {1}.",
        "cmc": 3.0,
        "colors": ["U"],
        "color_identity": ["U"],
        "rarity": "mythic",
        "keywords": [],
        "legalities": {"commander": "legal", "legacy": "legal", "standard": "not_legal"},
        "edhrec_rank": 3,
        "artist": "Fay Jones",
        "rulings_uri": "https://api.scryfall.com/cards/wot/25/rulings",
        "scryfall_uri": "https://scryfall.com/card/wot/25/rhystic-study",
    }

    with patch.object(ScryfallProvider, "_request", return_value=sf_resp) as mock_sf:
        meta = mcp_server.get_trade_card_metadata(
            item_id=alice_wot.id,
            include_live_scryfall=True,
            include_cached_meeple=True,
        )
        assert mock_sf.call_count == 1

    assert meta["found"] is True
    assert meta["match_count"] == 1
    assert meta["total_quantity_available"] == 1

    primary = meta["listings"][0]
    assert primary["card"]["set_code"] == "WOT"
    assert primary["card"]["live_details"]["edhrec_rank"] == 3
    assert primary["card"]["live_details"]["color_identity"] == ["U"]
    assert "commander" in primary["card"]["live_details"]["legalities"]
    assert primary["mighty_meeple"]["has_snapshot"] is True
    assert primary["mighty_meeple"]["buylist_cash"] == 24.00
    assert primary["mighty_meeple"]["buylist_credit"] == 31.00

    # Cross-printing pod availability should include ONLY Bob's PCY copy
    others = meta["other_printings_in_pod"]
    assert len(others) == 1
    assert others[0]["listing"]["item_id"] == bob_pcy.id
    assert others[0]["card"]["set_code"] == "PCY"
    assert others[0]["owner"]["handle_label"] == "@Bob"


def test_mcp_traders_and_summary_and_resources(app, client):
    import json
    import anyio
    import mcp_server

    alice = _login_user(client, "alice_s@pod.local", "Alice", discord_handle="@alice")
    bob = _login_user(client, "bob_s@pod.local", "Bob", discord_handle="@bob")

    oracle_shared = str(uuid.uuid4())
    card1 = _make_card("Cyclonic Rift", "CMM", "84", oracle_id=oracle_shared, usd_price=30.00)
    card2 = _make_card("Cyclonic Rift", "RTR", "35", oracle_id=oracle_shared, usd_price=32.00)
    card3 = _make_card("Lightning Bolt", "2X2", "117", usd_price=1.50)

    db.session.add_all([
        BinderItem(
            user_id=alice.id,
            scryfall_id=card1.scryfall_id,
            quantity=1,
            status="meeple_bound",
            mm_buylist_cash=16.00,
            mm_buylist_credit=21.00,
            mm_checked_at=utc_now(),
        ),
        BinderItem(
            user_id=bob.id,
            scryfall_id=card2.scryfall_id,
            quantity=2,
            status="for_trade",
        ),
        BinderItem(
            user_id=bob.id,
            scryfall_id=card3.scryfall_id,
            quantity=4,
            status="for_trade",
        ),
    ])
    db.session.commit()

    traders = mcp_server.list_pod_traders()
    assert traders["trader_count"] == 2
    assert traders["traders"][0]["display_name"] == "Bob"
    assert traders["traders"][0]["total_quantity"] == 6
    assert traders["traders"][0]["total_market_value"] == 70.00
    assert traders["traders"][1]["display_name"] == "Alice"
    assert traders["traders"][1]["meeple_bound_quantity"] == 1
    assert traders["traders"][1]["total_meeple_cash"] == 16.00

    summary = mcp_server.get_pod_trade_summary()
    assert summary["total_listings"] == 3
    assert summary["total_cards_available"] == 7
    assert summary["for_trade_cards"] == 6
    assert summary["meeple_bound_cards"] == 1
    assert summary["active_traders"] == 2
    assert summary["total_market_value"] == 100.00
    assert summary["top_value_listings"][0]["card"]["name"] == "Cyclonic Rift"
    assert len(summary["multi_copy_oracles"]) == 2
    assert summary["multi_copy_oracles"][0]["name"] == "Lightning Bolt"
    assert summary["multi_copy_oracles"][1]["name"] == "Cyclonic Rift"
    assert summary["multi_copy_oracles"][1]["traders"] == ["@Alice", "@Bob"]

    # Verify MCP resources return valid JSON
    feed_json = json.loads(mcp_server.trade_feed_resource())
    assert feed_json["total_matching"] == 3
    summary_json = json.loads(mcp_server.trade_summary_resource())
    assert summary_json["total_cards_available"] == 7

    # Verify MCP tool and resource registrations on the MCPServer instance
    async def _check_mcp_registry():
        tool_names = {t.name for t in await mcp_server.mcp.list_tools()}
        resource_uris = {str(r.uri) for r in await mcp_server.mcp.list_resources()}
        assert {
            "list_trade_cards",
            "get_trade_card_metadata",
            "list_pod_traders",
            "get_pod_trade_summary",
        }.issubset(tool_names)
        assert {
            "vaultswap://trades/feed",
            "vaultswap://trades/summary",
        }.issubset(resource_uris)

    anyio.run(_check_mcp_registry)


def test_unified_asgi_app_routing_and_mcp_endpoint(app, client):
    import hashlib
    from starlette.testclient import TestClient
    import asgi
    from models import ApiToken

    with app.app_context():
        u = _login_user(client, "asgi_mcp@pod.local", "AsgiUser")
        raw_tok = "vs_test_asgi_token_12345"
        db.session.add(
            ApiToken(
                user_id=u.id,
                label="Test",
                token_hash=hashlib.sha256(raw_tok.encode("utf-8")).hexdigest(),
                token_prefix=raw_tok[:10],
            )
        )
        db.session.commit()

    wrapped = asgi.create_asgi_app(app)
    with TestClient(wrapped) as tc:
        # Flask web UI route still functions normally
        res_flask = tc.get("/login")
        assert res_flask.status_code == 200

        # MCP Streamable HTTP endpoint rejects unauthenticated requests
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "GeminiMobileApp", "version": "1.0"},
            },
        }
        assert tc.post("/mcp", json=init_payload).status_code == 401

        # Authenticated MCP request succeeds
        res_mcp = tc.post(
            "/mcp",
            json=init_payload,
            headers={"Authorization": f"Bearer {raw_tok}"},
        )
        assert res_mcp.status_code == 200
        assert "capabilities" in res_mcp.text



def test_buylist_tradein_first_and_lightly_played_condition(app, client):
    """Verifies buylist displays trade-in/credit first then cash, and always marks card as lightly played."""
    with app.app_context():
        card = _make_card("Cyclonic Rift", "RTR", "35", usd_price=35.00)
        alice = _login_user(client, "alice_bl@pod.local", "Alice BL")

        # 1. BinderItem creation with meeple_bound automatically sets condition to LP
        item1 = BinderItem(
            user_id=alice.id,
            scryfall_id=card.scryfall_id,
            quantity=1,
            finish="nonfoil",
            status="meeple_bound",
            condition="NM",  # Should be overridden to LP
        )
        db.session.add(item1)
        db.session.commit()
        assert item1.condition == "LP"

        # 2. Updating item status to meeple_bound marks condition as LP
        item2 = BinderItem(
            user_id=alice.id,
            scryfall_id=card.scryfall_id,
            quantity=1,
            finish="nonfoil",
            status="private",
            condition="NM",
        )
        db.session.add(item2)
        db.session.commit()
        assert item2.condition == "NM"

        client.post(f"/binder/{item2.id}/status", data={"status": "meeple_bound"})
        item2_refreshed = db.session.get(BinderItem, item2.id)
        assert item2_refreshed.status == "meeple_bound"
        assert item2_refreshed.condition == "LP"

        # 3. Bulk status update to meeple_bound sets condition to LP
        item3 = BinderItem(
            user_id=alice.id,
            scryfall_id=card.scryfall_id,
            quantity=1,
            finish="nonfoil",
            status="private",
            condition="NM",
        )
        db.session.add(item3)
        db.session.commit()
        client.post("/binder/bulk-status", json={"item_ids": [item3.id], "status": "meeple_bound"})
        item3_refreshed = db.session.get(BinderItem, item3.id)
        assert item3_refreshed.condition == "LP"

        # 4. MightyMeepleProvider.get_card_snapshot defaults and uses LP for buylist quote
        provider = MightyMeepleProvider()
        fake_variants = [
            {"condition": "Near Mint", "finish": "Non-foil", "buyPrice": 12.00, "creditBuyPrice": 15.00},
            {"condition": "Lightly Played", "finish": "Non-foil", "buyPrice": 10.00, "creditBuyPrice": 13.00},
        ]
        chosen = provider._find_variant_quote(fake_variants, condition="LP", finish="nonfoil")
        assert chosen["condition"] == "Lightly Played"
        assert chosen["buyPrice"] == 10.00
        assert chosen["creditBuyPrice"] == 13.00

        # 5. Pod feed and binder row render credit first then cash
        item1.mm_buylist_cash = 10.00
        item1.mm_buylist_credit = 13.00
        item1.mm_checked_at = utc_now()
        db.session.commit()

        # Binder row check
        binder_html = client.get("/binder?filter=meeple_bound").get_data(as_text=True)
        assert "$13.00 Credit" in binder_html
        assert "$10.00 Cash" in binder_html
        assert binder_html.index("$13.00 Credit") < binder_html.index("$10.00 Cash")

        # Pod feed check
        feed_html = client.get("/pod").get_data(as_text=True)
        assert "$13.00 Credit" in feed_html
        assert "$10.00 Cash" in feed_html
        assert feed_html.index("$13.00 Credit") < feed_html.index("$10.00 Cash")


def test_pwa_manifest_route_and_structure(client):
    """Verifies manifest route responds with application/manifest+json and valid PWA config."""
    # Both .webmanifest and .json routes work
    for path in ["/manifest.webmanifest", "/manifest.json"]:
        res = client.get(path)
        assert res.status_code == 200
        assert "application/manifest+json" in res.content_type
        data = res.get_json()
        assert data["name"] == "VaultSwap // MTG Pod Binder"
        assert data["short_name"] == "VaultSwap"
        assert data["display"] == "standalone"
        assert data["start_url"] == "/?source=pwa"
        assert data["theme_color"] == "#020617"
        assert data["background_color"] == "#020617"
        assert len(data["icons"]) >= 2
        # Verify icon URLs
        icon_sizes = [i["sizes"] for i in data["icons"]]
        assert "192x192" in icon_sizes
        assert "512x512" in icon_sizes


def test_pwa_service_worker_route_and_headers(client):
    """Verifies /sw.js is served from root with Service-Worker-Allowed header."""
    res = client.get("/sw.js")
    assert res.status_code == 200
    assert "application/javascript" in res.content_type
    assert res.headers.get("Service-Worker-Allowed") == "/"
    content = res.get_data(as_text=True)
    assert "vaultswap-v1" in content
    assert "addEventListener('install'" in content
    assert "addEventListener('fetch'" in content


def test_pwa_offline_page(client):
    """Verifies /offline route renders the offline fallback template."""
    res = client.get("/offline")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "You're Currently Offline" in html
    assert "Retry Connection" in html


def test_pwa_icons_and_favicons(client):
    """Verifies touch icons and favicon endpoints return 200."""
    res_apple = client.get("/apple-touch-icon.png")
    assert res_apple.status_code == 200
    assert res_apple.content_type == "image/png"

    res_favicon = client.get("/favicon.ico")
    assert res_favicon.status_code == 200


def test_pwa_mobile_meta_tags_in_templates(client):
    """Verifies base template contains required PWA and Apple mobile meta tags."""
    res = client.get("/login")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'rel="manifest"' in html
    assert 'name="apple-mobile-web-app-capable" content="yes"' in html
    assert 'name="apple-mobile-web-app-status-bar-style" content="black-translucent"' in html
    assert 'name="apple-mobile-web-app-title" content="VaultSwap"' in html
    assert 'rel="apple-touch-icon"' in html
    assert 'triggerPwaInstall' in html
    assert 'serviceWorker' in html
    assert 'install-guide-modal' in html


def test_discord_webhook_profile_configuration_and_validation(app, client):
    """Verifies users can configure, test, and clear a Discord webhook URL in their profile."""
    with app.app_context():
        user = _login_user(client, "jason@pod.local", "Jason")
        valid_hook = "https://discord.com/api/webhooks/123456789012345678/abcdef123456_token-xyz"

        # Save valid Discord webhook URL
        resp = client.post(
            "/profile",
            data={
                "display_name": "Jason M",
                "discord_handle": "@jason_mtg",
                "discord_webhook_url": valid_hook,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        updated = db.session.get(User, user.id)
        assert updated.display_name == "Jason M"
        assert updated.discord_handle == "@jason_mtg"
        assert updated.discord_webhook_url == valid_hook

        # Reject invalid non-Discord URL without overwriting existing valid webhook
        bad_resp = client.post(
            "/profile",
            data={
                "display_name": "Jason M",
                "discord_handle": "@jason_mtg",
                "discord_webhook_url": "https://example.com/not-a-discord-webhook",
            },
            follow_redirects=True,
        )
        assert bad_resp.status_code == 200
        assert "Please enter a valid Discord Webhook URL" in bad_resp.get_data(as_text=True)
        assert db.session.get(User, user.id).discord_webhook_url == valid_hook

        # Test webhook endpoint
        sent_calls = []
        app.discord_sender_fn = lambda url, payload: (sent_calls.append((url, payload)) or True)
        test_resp = client.post("/profile/test-webhook", follow_redirects=True)
        assert test_resp.status_code == 200
        assert len(sent_calls) == 1
        assert sent_calls[0][0] == valid_hook
        assert "VaultSwap Discord Webhook Connected" in sent_calls[0][1]["content"]

        # Clear webhook URL
        clear_resp = client.post(
            "/profile",
            data={
                "display_name": "Jason M",
                "discord_handle": "@jason_mtg",
                "discord_webhook_url": "",
            },
            follow_redirects=True,
        )
        assert clear_resp.status_code == 200
        assert db.session.get(User, user.id).discord_webhook_url is None


def test_discord_webhook_batches_and_posts_after_delay(app, client):
    """
    Verifies that when anyone lists cards for trade or meeple, the worker waits
    for the configured delay window, batches newly added cards, excludes any card
    reverted back to private during the wait, and posts to configured Discord webhooks.
    """
    with app.app_context():
        # Default config must be 300s (5 minutes)
        assert app.config["DISCORD_WEBHOOK_DELAY_SEC"] == 300.0
        # Use short delay for test execution speed while testing timer wait + batching
        app.config["DISCORD_WEBHOOK_DELAY_SEC"] = 0.15

        hook_alice = "https://discord.com/api/webhooks/1111111111/token_alice"
        hook_bob = "https://discordapp.com/api/webhooks/2222222222/token_bob"

        alice = _login_user(client, "alice_hook@pod.local", "Alice", discord_handle="@alice")
        alice.discord_webhook_url = hook_alice
        db.session.commit()

        # Bob and Carol share hook_bob (to verify deduplication of identical webhook URLs)
        bob = User(
            google_sub="sub-bob-hook",
            email="bob_hook@pod.local",
            display_name="Bob",
            discord_handle="@bob",
            discord_webhook_url=hook_bob,
        )
        carol = User(
            google_sub="sub-carol-hook",
            email="carol_hook@pod.local",
            display_name="Carol",
            discord_webhook_url=hook_bob,
        )
        db.session.add_all([bob, carol])

        c1 = _make_card("Rhystic Study", "WOT", "25", usd_price=42.00)
        c2 = _make_card("Smothering Tithe", "CMM", "57", usd_price=25.00)
        c3 = _make_card("Sol Ring", "CMM", "410", usd_price=1.50)

        i1 = BinderItem(
            user_id=alice.id,
            scryfall_id=c1.scryfall_id,
            quantity=1,
            finish="nonfoil",
            status="private",
            mm_in_stock=True,
            mm_retail_price=45.00,
            mm_buylist_cash=22.00,
            mm_buylist_credit=28.60,
            mm_checked_at=utc_now(),
        )
        i2 = BinderItem(
            user_id=alice.id,
            scryfall_id=c2.scryfall_id,
            quantity=2,
            finish="foil",
            status="private",
            mm_in_stock=True,
            mm_retail_price=38.00,
            mm_buylist_cash=18.00,
            mm_buylist_credit=23.40,
            mm_checked_at=utc_now(),
        )
        i3 = BinderItem(
            user_id=alice.id,
            scryfall_id=c3.scryfall_id,
            quantity=1,
            finish="nonfoil",
            status="private",
            mm_in_stock=True,
            mm_retail_price=2.00,
            mm_buylist_cash=0.50,
            mm_buylist_credit=0.65,
            mm_checked_at=utc_now(),
        )
        db.session.add_all([i1, i2, i3])
        db.session.commit()

        dispatched = []

        def fake_sender(url, payload):
            dispatched.append((url, payload))
            return True

        app.discord_sender_fn = fake_sender
        # 1. Alice lists Rhystic Study for trade
        resp1 = client.post(f"/binder/{i1.id}/status", data={"status": "for_trade"})
        assert resp1.status_code == 200

        # 2. Alice bulk-lists Smothering Tithe and Sol Ring for meeple_bound
        resp2 = client.post(
            "/binder/bulk-status",
            json={"status": "meeple_bound", "item_ids": [i2.id, i3.id]},
        )
        assert resp2.status_code == 200

        # 3. Before the 5-minute window elapses, Alice reverts Sol Ring back to private
        resp3 = client.post(f"/binder/{i3.id}/status", data={"status": "private"})
        assert resp3.status_code == 200

        # Should still be pending before timer expires
        assert len(dispatched) == 0
        assert app.job_queue.pending_count("discord_notify") == 3
        assert app.job_queue.run_due("discord_notify") == 0
        assert len(dispatched) == 0

        # Wait for timer window to elapse and dispatch
        import time as _time
        _time.sleep(0.2)
        assert app.job_queue.run_due("discord_notify") == 3

        # Delivered once to hook_alice and once to hook_bob (deduplicated across Bob & Carol)
        assert len(dispatched) == 2
        urls_notified = [u for u, _ in dispatched]
        assert urls_notified == [hook_alice, hook_bob]

        payload = dispatched[0][1]
        assert "Alice (@alice)" in payload["content"]
        assert "**1** For Trade" in payload["content"]
        assert "**2** Meeple Bound" in payload["content"]
        embed_desc = payload["embeds"][0]["description"]
        assert "Rhystic Study" in embed_desc
        assert "Smothering Tithe" in embed_desc
        assert "Foil" in embed_desc
        assert "$23.40 Cr" in embed_desc
        # Sol Ring was reverted to private before timer fired, so it must NOT be posted
        assert "Sol Ring" not in embed_desc


# =============================================================================
# Security, Durable Job Queue, and 22k-Card Pagination Tests (S1-S4, U1)
# =============================================================================

def test_s1_mcp_api_token_auth_and_lifecycle(app, client):
    """Verifies /mcp requires a valid non-revoked ApiToken (Bearer or ?key=) and tokens are hashed."""
    from starlette.testclient import TestClient
    import asgi
    from models import ApiToken

    with app.app_context():
        _login_user(client, "mcp_owner@pod.local", "McpOwner")
        tok_resp = client.post("/profile/api-tokens", json={"label": "Phone"})
        assert tok_resp.status_code == 200
        tok_data = tok_resp.get_json()
        raw_token = tok_data["token"]
        tok_id = tok_data["id"]

        # Plaintext must not be stored in DB
        db_tok = db.session.get(ApiToken, tok_id)
        assert db_tok is not None
        assert db_tok.token_hash != raw_token
        assert raw_token not in repr(db_tok)

    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "GeminiMobileApp", "version": "1.0"},
        },
    }

    wrapped = asgi.create_asgi_app(app)
    with TestClient(wrapped) as tc:
        # Missing / bad token -> 401
        assert tc.post("/mcp", json=init_payload).status_code == 401
        assert tc.post("/mcp", json=init_payload, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert tc.post("/mcp?key=wrong", json=init_payload).status_code == 401

        # Valid Bearer -> 200
        res_bearer = tc.post("/mcp", json=init_payload, headers={"Authorization": f"Bearer {raw_token}"})
        assert res_bearer.status_code == 200

        # Valid ?key= -> 200 with Cache-Control: no-store
        res_key = tc.post(f"/mcp?key={raw_token}", json=init_payload)
        assert res_key.status_code == 200
        assert res_key.headers.get("cache-control") == "no-store"

        # Revoke token -> 401
        rev_resp = client.post(f"/profile/api-tokens/{tok_id}/revoke", json={})
        assert rev_resp.status_code == 200
        assert tc.post("/mcp", json=init_payload, headers={"Authorization": f"Bearer {raw_token}"}).status_code == 401


def test_s2_csrf_protection_and_htmx_headers():
    """Verifies Flask-WTF CSRF blocks raw POSTs without a token and allows HTMX X-CSRFToken."""
    import re

    csrf_app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SECRET_KEY": "csrf-test-secret",
        "GOOGLE_CLIENT_ID": "",
        "DEV_LOGIN_ENABLED": True,
        "INVITE_ONLY": False,
        "WTF_CSRF_ENABLED": True,
        "JOB_DISPATCHER": "inline",
    })
    with csrf_app.app_context():
        db.create_all()
        c = csrf_app.test_client()

        # Login page has CSRF token
        login_html = c.get("/login").get_data(as_text=True)
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_html)
        assert m is not None
        csrf_tok = m.group(1)

        # POST without CSRF token fails with 400
        bad = c.post("/auth/dev-login", data={"email": "csrf@pod.local", "display_name": "CsrfUser"})
        assert bad.status_code == 400

        # POST with CSRF token succeeds
        ok = c.post(
            "/auth/dev-login",
            data={"email": "csrf@pod.local", "display_name": "CsrfUser", "csrf_token": csrf_tok},
            follow_redirects=False,
        )
        assert ok.status_code == 302

        # Rendered base.html has meta tag and hx-headers
        binder_html = c.get("/binder").get_data(as_text=True)
        assert '<meta name="csrf-token"' in binder_html
        assert 'hx-headers=' in binder_html

        # HTMX POST with X-CSRFToken succeeds
        user = User.query.filter_by(email="csrf@pod.local").one()
        card = _make_card("Opt", "XLN", "65")
        item = BinderItem(user_id=user.id, scryfall_id=card.scryfall_id, status="private")
        db.session.add(item)
        db.session.commit()

        m2 = re.search(r'<meta name="csrf-token" content="([^"]+)"', binder_html)
        assert m2 is not None
        page_csrf = m2.group(1)

        assert c.post(f"/binder/{item.id}/status", data={"status": "for_trade"}).status_code == 400
        htmx_ok = c.post(
            f"/binder/{item.id}/status",
            data={"status": "for_trade"},
            headers={"X-CSRFToken": page_csrf},
        )
        assert htmx_ok.status_code == 200
        db.drop_all()


def test_s3_secret_key_required_in_production():
    """Verifies create_app refuses to start without SECRET_KEY when dev login is disabled."""
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({
            "TESTING": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SECRET_KEY": "",
            "GOOGLE_CLIENT_ID": "real-client-id",
            "DEV_LOGIN_ENABLED": False,
        })


def test_s4_durable_jobs_retry_stale_recovery_token_wipe_and_tasks_endpoint(app, client):
    """Verifies stale recovery, retry/fail backoff, Drive token wipe, /tasks/run auth, and CloudTasks dedupe."""
    from models import DriveImport, Job
    import jobs as jobs_mod

    with app.app_context():
        u = _login_user(client, "jobs@pod.local", "JobsUser")
        c1 = _make_card("Force of Will", "DMR", "50")
        item = BinderItem(user_id=u.id, scryfall_id=c1.scryfall_id, status="for_trade")
        db.session.add(item)
        db.session.commit()

        # 1. Stale recovery
        stale_job = Job(
            kind="meeple_lookup",
            ref_id=item.id,
            status="running",
            attempts=1,
            max_attempts=3,
            locked_at=utc_now() - timedelta(minutes=45),
        )
        db.session.add(stale_job)
        db.session.commit()
        assert app.job_queue.recover_stale(stale_minutes=35) == 1
        assert db.session.get(Job, stale_job.id).status == "pending"

        # 2. Retry with backoff then mark failed after max_attempts
        stale_job.max_attempts = 2
        db.session.commit()
        app.meeple_provider_factory = lambda: MagicMock(get_card_snapshot=MagicMock(side_effect=RuntimeError("boom")))
        app.job_queue.run_due("meeple_lookup")
        j = db.session.get(Job, stale_job.id)
        assert j.status == "failed"
        assert j.attempts == 2
        assert "boom" in (j.last_error or "")

        # 3. Drive import wipes access_token payload on completion
        imp = DriveImport(user_id=u.id, drive_file_id="f1", file_name="test.csv", status="queued")
        db.session.add(imp)
        db.session.commit()
        drive_job = app.job_queue.enqueue("drive_import", ref_id=imp.id, payload={"access_token": "secret-tok"}, deduplicate=False)
        assert drive_job.payload is not None
        with patch("drive_sync.run_drive_import", return_value=None):
            assert app.job_queue.run_due("drive_import") == 1
        assert db.session.get(Job, drive_job.id).status == "done"
        assert db.session.get(Job, drive_job.id).payload is None

        # 4. /tasks/run rejects unauthenticated, accepts valid OIDC or shared secret
        assert client.post("/tasks/run").status_code == 401
        app.config["TASKS_SHARED_SECRET"] = "local-secret"
        assert client.post("/tasks/run", headers={"X-Tasks-Secret": "local-secret"}).status_code == 200

        app.config["TASKS_SHARED_SECRET"] = ""
        app.config["TASKS_INVOKER_SA"] = "invoker@proj.iam.gserviceaccount.com"
        app.config["APP_BASE_URL"] = "https://vaultswap.run.app"
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value={"email": "invoker@proj.iam.gserviceaccount.com", "email_verified": True}):
            res_oidc = client.post("/tasks/run", headers={"Authorization": "Bearer fake-oidc"})
            assert res_oidc.status_code == 200

        # 5. CloudTasksDispatcher deduplicates task names and ignores AlreadyExists
        class AlreadyExistsErr(Exception):
            pass

        mock_client = MagicMock()
        mock_client.create_task.side_effect = [None, AlreadyExistsErr("AlreadyExists")]
        disp = jobs_mod.CloudTasksDispatcher(
            queue_path="projects/p/locations/us-east1/queues/q",
            target_url="https://vaultswap.run.app/tasks/run",
            invoker_sa="invoker@proj.iam.gserviceaccount.com",
            client_factory=lambda: mock_client,
        )
        now = utc_now()
        disp.notify("meeple_lookup", now)
        disp.notify("meeple_lookup", now)
        assert mock_client.create_task.call_count == 2
        name1 = mock_client.create_task.call_args_list[0].kwargs["task"]["name"]
        name2 = mock_client.create_task.call_args_list[1].kwargs["task"]["name"]
        assert name1 == name2


def test_u1_pagination_sql_color_grouping_views_stats_and_select_all(app, client):
    """Verifies 60-item pagination, SQL color filter/grouping across pages, single-view render, SQL stats, and select_all."""
    import queries
    from sqlalchemy import event as sa_event

    with app.app_context():
        alice = _login_user(client, "u1_alice@pod.local", "Alice")
        bob = User(google_sub="sub-u1-bob", email="u1_bob@pod.local", display_name="Bob")
        db.session.add(bob)
        db.session.flush()

        # Seed 500 items for Alice (250 White, 250 Blue) and 5 for Bob
        cards = []
        for idx in range(500):
            is_white = idx < 250
            c = Card(
                scryfall_id=str(uuid.uuid4()),
                oracle_id=str(uuid.uuid4()),
                name=f"Card {idx:03d}",
                set_code="TST",
                collector_number=str(idx + 1),
                type_line="Creature — Human" if is_white else "Instant",
                mana_cost="{W}" if is_white else "{U}",
                colors="W" if is_white else "U",
                usd_price=2.00,
                usd_foil_price=5.00,
            )
            cards.append(c)
        db.session.add_all(cards)
        db.session.flush()

        items = []
        for idx, c in enumerate(cards):
            items.append(
                BinderItem(
                    user_id=alice.id,
                    scryfall_id=c.scryfall_id,
                    quantity=2 if idx % 10 == 0 else 1,
                    finish="foil" if idx % 5 == 0 else "nonfoil",
                    status="for_trade" if idx == 0 else "private",
                )
            )
        # Bob's card must never be touched by Alice's select_all
        bob_item = BinderItem(user_id=bob.id, scryfall_id=cards[0].scryfall_id, status="private")
        db.session.add_all(items + [bob_item])
        db.session.commit()

        # 1. Page 1 returns 60 items + sentinel; Page 9 returns 20 items and no sentinel
        p1 = client.get("/binder?page=1")
        assert p1.status_code == 200
        html1 = p1.get_data(as_text=True)
        assert html1.count('id="binder-row-') == 60
        assert 'hx-trigger="revealed"' in html1
        assert "Showing 60 of 500 card(s)" in html1
        # Only active view ('rows') rendered — no art grid or checklist
        assert 'id="binder-art-grid"' not in html1
        assert 'id="binder-checklist"' not in html1

        p9 = client.get("/binder?page=9", headers={"HX-Request": "true"})
        assert p9.status_code == 200
        html9 = p9.get_data(as_text=True)
        assert html9.count('id="binder-row-') == 20
        assert 'hx-trigger="revealed"' not in html9

        # 2. Active view switcher renders only requested view
        art_html = client.get("/binder?view=art").get_data(as_text=True)
        assert 'id="binder-art-grid"' in art_html
        assert 'id="binder-rows-container"' not in art_html

        # 3. SQL color filter + cross-page color grouping (prev_group avoids duplicate header)
        w_html = client.get("/binder?color=w").get_data(as_text=True)
        assert "Showing 60 of 250 card(s)" in w_html

        g1_html = client.get("/binder?group=color&page=1").get_data(as_text=True)
        assert "White (W)" in g1_html
        g2_html = client.get("/binder?group=color&page=2&prev_group=W", headers={"HX-Request": "true"}).get_data(as_text=True)
        assert "White (W)" not in g2_html

        # 4. SQL stats match expected values
        stats = queries.compute_binder_stats_sql(alice.id)
        assert stats["unique_rows"] == 500
        assert stats["total_cards"] == 550  # 500 + 50 extra qty
        assert stats["for_trade_qty"] == 2

        # 5. Constant query count per page (no N+1)
        q_count = 0

        def _count_q(*args, **kwargs):
            nonlocal q_count
            q_count += 1

        sa_event.listen(db.engine, "before_cursor_execute", _count_q)
        try:
            client.get("/binder?page=2", headers={"HX-Request": "true"})
        finally:
            sa_event.remove(db.engine, "before_cursor_execute", _count_q)
        assert q_count <= 8

        # 6. select_all=1 bulk updates only Alice's matching filtered cards
        bulk_resp = client.post(
            "/binder/bulk-status",
            json={"select_all": "1", "color": "u", "status": "for_trade"},
        )
        assert bulk_resp.status_code == 200
        assert bulk_resp.get_json()["updated"] == 250
        assert db.session.get(BinderItem, bob_item.id).status == "private"







