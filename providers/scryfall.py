"""Scryfall API client for VaultSwap with /cards/collection batching (<=75 per POST)."""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
import requests

from card_utils import fix_mojibake, get_card_match_keys, strip_accents

logger = logging.getLogger(__name__)

SCRYFALL_BASE_URL = "https://api.scryfall.com"
DEFAULT_USER_AGENT = "VaultSwap/1.0 (MTG Pod Binder & Trade Feed; +https://vaultswap.local)"
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _to_decimal(val: Any) -> Optional[Decimal]:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


class ScryfallProvider:
    """Scryfall API client supporting batch /cards/collection, autocomplete, and named lookup."""

    _last_request_time: float = 0.0
    _card_cache: Dict[str, Dict[str, Any]] = {}

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json;q=0.9,*/*;q=0.8",
        })

    def _request(self, method: str, url: str, **kwargs):
        """Dispatches HTTP requests to Scryfall with 100ms pacing and 429 backoff."""
        elapsed = time.time() - ScryfallProvider._last_request_time
        if elapsed < 0.1:
            time.sleep(0.1 - elapsed)

        resp = None
        for attempt in range(3):
            ScryfallProvider._last_request_time = time.time()
            if method.lower() == "post":
                resp = self.session.post(url, **kwargs)
            else:
                resp = self.session.get(url, **kwargs)

            if resp.status_code == 429:
                wait_time = 1.0 * (attempt + 1)
                req_timeout = kwargs.get("timeout")
                if req_timeout is not None and wait_time >= req_timeout:
                    return resp
                logger.warning("Scryfall 429 rate limit hit for %s; sleeping %.1fs", url, wait_time)
                time.sleep(wait_time)
                continue
            return resp
        return resp

    @staticmethod
    def format_card_object(card: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes raw Scryfall card JSON into VaultSwap's canonical Card dict."""
        image_normal = None
        image_small = None
        mana_cost = card.get("mana_cost") or ""
        type_line = card.get("type_line") or ""
        oracle_id = card.get("oracle_id")

        if card.get("image_uris"):
            image_normal = card["image_uris"].get("normal")
            image_small = card["image_uris"].get("small")
        elif card.get("card_faces"):
            first_face = card["card_faces"][0]
            if first_face.get("image_uris"):
                image_normal = first_face["image_uris"].get("normal")
                image_small = first_face["image_uris"].get("small")
            if not mana_cost and first_face.get("mana_cost"):
                mana_cost = first_face["mana_cost"]
            if not type_line and first_face.get("type_line"):
                type_line = first_face["type_line"]
            if not oracle_id and first_face.get("oracle_id"):
                oracle_id = first_face["oracle_id"]

        if not oracle_id:
            oracle_id = card.get("id")

        prices = card.get("prices") or {}
        usd_price = _to_decimal(prices.get("usd"))
        usd_foil_price = _to_decimal(prices.get("usd_foil") or prices.get("usd_etched"))

        return {
            "scryfall_id": str(card.get("id") or "").lower(),
            "oracle_id": str(oracle_id or "").lower(),
            "name": card.get("name") or "",
            "set_code": (card.get("set") or "").upper(),
            "set_name": card.get("set_name") or "",
            "collector_number": str(card.get("collector_number") or ""),
            "image_uri_normal": image_normal,
            "image_uri_small": image_small,
            "mana_cost": mana_cost[:100] if mana_cost else None,
            "type_line": type_line[:255] if type_line else None,
            "usd_price": usd_price,
            "usd_foil_price": usd_foil_price,
        }

    def autocomplete(self, query: str) -> List[str]:
        """Returns up to 20 card name suggestions from Scryfall."""
        if not query or len(query.strip()) < 2:
            return []

        clean_q = fix_mojibake(str(query)).strip()
        clean_q = re.sub(r"^\s*\d+\s*[xX]?\s+", "", clean_q).strip()
        clean_q = re.sub(r"\[.*?\]|\(.*?\)", "", clean_q).strip()
        clean_q = re.sub(r"\s*#?\d+\s*$", "", clean_q).strip()
        target = clean_q or query.strip()

        try:
            resp = self._request("get", f"{SCRYFALL_BASE_URL}/cards/autocomplete", params={"q": target}, timeout=6)
            if resp is not None and resp.status_code == 200:
                suggestions = resp.json().get("data", [])
                if suggestions:
                    return suggestions
        except Exception as exc:
            logger.debug("Scryfall autocomplete error for %r: %s", target, exc)

        if len(target) >= 3:
            card = self.get_card_named(target)
            if card and card.get("name"):
                return [card["name"]]
        return []

    def get_card_named(self, card_name: str, set_code: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetches a single card via /cards/named (exact, then fuzzy)."""
        if not card_name:
            return None

        clean_name = fix_mojibake(str(card_name)).strip()
        if not clean_name:
            return None

        cache_key = f"{clean_name.lower()}|{(set_code or '').lower()}"
        if cache_key in ScryfallProvider._card_cache:
            return ScryfallProvider._card_cache[cache_key]

        candidates = [clean_name]
        ascii_name = strip_accents(clean_name)
        if ascii_name not in candidates:
            candidates.append(ascii_name)
        if " // " in clean_name:
            front = clean_name.split(" // ")[0].strip()
            if front not in candidates:
                candidates.append(front)

        url = f"{SCRYFALL_BASE_URL}/cards/named"
        for mode in ("exact", "fuzzy"):
            for cand in candidates:
                params = {mode: cand}
                if set_code:
                    params["set"] = set_code.lower().strip()
                try:
                    resp = self._request("get", url, params=params, timeout=8)
                    if resp is not None and resp.status_code == 200:
                        formatted = self.format_card_object(resp.json())
                        ScryfallProvider._card_cache[cache_key] = formatted
                        return formatted
                except Exception as exc:
                    logger.debug("Scryfall %s named lookup error for %r: %s", mode, cand, exc)
            # If set-constrained exact+fuzzy failed, try without set_code on next pass? Only if set_code given
            if set_code and mode == "fuzzy":
                for cand in candidates:
                    try:
                        resp = self._request("get", url, params={"fuzzy": cand}, timeout=8)
                        if resp is not None and resp.status_code == 200:
                            formatted = self.format_card_object(resp.json())
                            ScryfallProvider._card_cache[cache_key] = formatted
                            return formatted
                    except Exception as exc:
                        logger.debug("Scryfall fallback fuzzy lookup error for %r: %s", cand, exc)

        return None

    def resolve_collection_batch(
        self,
        entries: List[Dict[str, Any]],
        fallback_named: bool = True,
        timeout: float = 15.0,
    ) -> Tuple[Dict[int, Dict[str, Any]], List[int]]:
        """
        Resolves a list of card entry dicts via Scryfall's /cards/collection endpoint
        (max 75 identifiers per POST request).

        Each entry may specify: scryfall_id, set_code, collector_number, name.
        Returns (resolved_by_index, unresolved_indices).
        """
        if not entries:
            return {}, []

        resolved: Dict[int, Dict[str, Any]] = {}
        pending_specs: List[Tuple[int, Dict[str, str], Dict[str, Any]]] = []

        for idx, entry in enumerate(entries):
            sid = str(entry.get("scryfall_id") or "").strip().lower()
            set_code = str(entry.get("set_code") or "").strip().lower()
            col_num = str(entry.get("collector_number") or "").strip()
            name = fix_mojibake(str(entry.get("name") or "")).strip()

            if sid and UUID_PATTERN.match(sid):
                ident = {"id": sid}
            elif set_code and col_num:
                ident = {"set": set_code, "collector_number": col_num}
            elif name:
                lookup_name = name.split(" // ")[0].strip() if " // " in name else name
                ident = {"name": lookup_name}
            else:
                continue

            pending_specs.append((idx, ident, entry))

        # Helper to deduplicate identifiers in batches of 75 and dispatch POST /cards/collection
        def _dispatch_batches(specs: List[Tuple[int, Dict[str, str], Dict[str, Any]]]) -> List[Tuple[int, Dict[str, str], Dict[str, Any]]]:
            ident_to_indices: Dict[Tuple[Tuple[str, str], ...], List[int]] = {}
            unique_idents: List[Tuple[Tuple[Tuple[str, str], ...], Dict[str, str]]] = []

            for idx, ident, _entry in specs:
                key = tuple(sorted((k, str(v).lower()) for k, v in ident.items()))
                if key not in ident_to_indices:
                    ident_to_indices[key] = []
                    unique_idents.append((key, ident))
                ident_to_indices[key].append(idx)

            failed_specs: List[Tuple[int, Dict[str, str], Dict[str, Any]]] = []
            entry_by_idx = {idx: entry for idx, _ident, entry in specs}
            ident_by_idx = {idx: ident for idx, ident, _entry in specs}

            for start in range(0, len(unique_idents), 75):
                batch = unique_idents[start:start + 75]
                payload_idents = [ident for _key, ident in batch]

                try:
                    resp = self._request(
                        "post",
                        f"{SCRYFALL_BASE_URL}/cards/collection",
                        json={"identifiers": payload_idents},
                        headers={"Content-Type": "application/json"},
                        timeout=timeout,
                    )
                    if resp is None or resp.status_code != 200:
                        logger.warning(
                            "Scryfall /cards/collection failed with HTTP %s",
                            getattr(resp, "status_code", "None"),
                        )
                        for key, _ident in batch:
                            for idx in ident_to_indices[key]:
                                failed_specs.append((idx, ident_by_idx[idx], entry_by_idx[idx]))
                        continue

                    data = resp.json()
                    cards_data = data.get("data", [])

                    # Index returned cards by id, (set, collector_number), and match keys
                    by_id: Dict[str, Dict[str, Any]] = {}
                    by_set_cn: Dict[Tuple[str, str], Dict[str, Any]] = {}
                    by_name: Dict[str, Dict[str, Any]] = {}

                    for raw_card in cards_data:
                        formatted = self.format_card_object(raw_card)
                        if formatted["scryfall_id"]:
                            by_id[formatted["scryfall_id"].lower()] = formatted
                        if formatted["set_code"] and formatted["collector_number"]:
                            by_set_cn[(formatted["set_code"].lower(), formatted["collector_number"].lower())] = formatted
                        for mk in get_card_match_keys(formatted["name"]):
                            by_name[mk] = formatted

                    for key, ident in batch:
                        matched_card = None
                        if "id" in ident:
                            matched_card = by_id.get(ident["id"].lower())
                        elif "set" in ident and "collector_number" in ident:
                            matched_card = by_set_cn.get((ident["set"].lower(), str(ident["collector_number"]).lower()))
                        elif "name" in ident:
                            for mk in get_card_match_keys(ident["name"]):
                                if mk in by_name:
                                    matched_card = by_name[mk]
                                    break

                        for idx in ident_to_indices[key]:
                            if matched_card:
                                resolved[idx] = matched_card
                            else:
                                failed_specs.append((idx, ident_by_idx[idx], entry_by_idx[idx]))

                except Exception as exc:
                    logger.error("Scryfall collection batch error: %s", exc)
                    for key, _ident in batch:
                        for idx in ident_to_indices[key]:
                            failed_specs.append((idx, ident_by_idx[idx], entry_by_idx[idx]))

            return failed_specs

        # Pass 1: exact id / set+collector_number / name
        missed = _dispatch_batches(pending_specs)

        # Pass 2: if set+collector_number or id failed, retry by name (+ optional set) in batch
        retry_by_name: List[Tuple[int, Dict[str, str], Dict[str, Any]]] = []
        still_missing: List[int] = []
        for idx, ident, entry in missed:
            name = fix_mojibake(str(entry.get("name") or "")).strip()
            if name and "name" not in ident:
                lookup_name = name.split(" // ")[0].strip() if " // " in name else name
                retry_by_name.append((idx, {"name": lookup_name}, entry))
            else:
                still_missing.append(idx)

        if retry_by_name:
            missed_pass2 = _dispatch_batches(retry_by_name)
            for idx, _ident, _entry in missed_pass2:
                still_missing.append(idx)

        # Pass 3: named lookup fallback (capped at 20 cards to stay quick)
        final_unresolved: List[int] = []
        if fallback_named and still_missing:
            for idx in still_missing[:20]:
                entry = entries[idx]
                name = fix_mojibake(str(entry.get("name") or "")).strip()
                set_code = str(entry.get("set_code") or "").strip() or None
                card_obj = self.get_card_named(name, set_code=set_code)
                if card_obj:
                    resolved[idx] = card_obj
                else:
                    final_unresolved.append(idx)
            final_unresolved.extend(still_missing[20:])
        else:
            final_unresolved = still_missing

        # Include entries that lacked any usable identifier at all
        tracked = set(resolved.keys()) | set(final_unresolved)
        for idx in range(len(entries)):
            if idx not in tracked:
                final_unresolved.append(idx)

        final_unresolved.sort()
        return resolved, final_unresolved
