"""Mighty Meeple storefront & BinderPOS buylist client for VaultSwap."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
import urllib.parse
from typing import Any, Dict, List, Optional
import requests

logger = logging.getLogger(__name__)

MIGHTY_MEEPLE_BASE = "https://mightymeeple.com"
BINDERPOS_PORTAL_BASE = "https://portal.binderpos.com"
MIGHTY_MEEPLE_STORE_ID = "7b044554-df1f-4771-a58e-39092834f726"
DEFAULT_USER_AGENT = "VaultSwap/1.0 (MTG Pod Binder Snapshot; +https://vaultswap.local)"

JUNK_TERMS = [
    "art card",
    "art series",
    "artist card",
    "token",
    "playmat",
    "sleeve",
    "sleeves",
    "deck box",
    "binder",
    "booster pack",
    "booster box",
    "collector booster",
    "bundle",
    "display",
    "case",
    "draft pack",
]


def normalize_card_text(text: str) -> str:
    """Normalizes card names and titles to lowercase ASCII with uniform whitespace."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("utf-8")
    cleaned = re.sub(r"[^\w\s]", " ", norm.lower())
    return " ".join(cleaned.split())


def extract_base_name_from_title(title: str) -> str:
    """Extracts the base card name from Mighty Meeple product titles."""
    t = re.sub(r"\[.*?\]", "", title)
    t = re.sub(r"\(.*?\)", "", t)
    return t.strip()


class MightyMeepleProvider:
    """Polite live stock, retail price, and buylist snapshot scanner for Mighty Meeple."""

    _variant_cache: Dict[str, List[Dict[str, Any]]] = {}

    def __init__(self, session=None, user_agent: str = DEFAULT_USER_AGENT):
        self.is_cffi = False
        if session is not None:
            self.session = session
        else:
            try:
                from curl_cffi import requests as cffi_requests
                self.session = cffi_requests.Session(impersonate="chrome120")
                self.is_cffi = True
            except ImportError:
                self.session = requests.Session()
                self.is_cffi = False

        if not self.is_cffi and hasattr(self.session, "headers"):
            self.session.headers.update({
                "User-Agent": user_agent,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
            })

    def get_card_snapshot(
        self,
        card_name: str,
        set_code: Optional[str] = None,
        set_name: Optional[str] = None,
        collector_number: Optional[str] = None,
        finish: str = "nonfoil",
        condition: str = "LP",
    ) -> Dict[str, Any]:
        """
        Fetches a point-in-time Mighty Meeple retail + buylist snapshot for a single card.
        Returns:
        {
            "in_stock": bool,
            "retail_price": float | None,
            "buylist_cash": float | None,
            "buylist_credit": float | None,
            "product_url": str | None,
        }
        """
        retail = self.search_card(
            card_name=card_name,
            set_name=set_name,
            set_code=set_code,
            collector_number=collector_number,
            finish=finish,
        )

        primary_name = (card_name or "").split(" // ")[0].strip()
        buylist_res = self.search_buylist(query=primary_name, set_name=set_name, limit=20)
        items = buylist_res.get("items", [])

        # Fallback without strict set_name filter if Mighty Meeple's set naming differs from Scryfall
        if not items and set_name:
            buylist_res = self.search_buylist(query=primary_name, set_name=None, limit=25)
            items = buylist_res.get("items", [])

        buylist_cash: Optional[float] = None
        buylist_credit: Optional[float] = None
        buylist_sell: Optional[float] = None

        matching_items = [
            itm for itm in items
            if self._is_card_name_match(itm.get("card_name", ""), card_name)
            or (primary_name and self._is_card_name_match(itm.get("card_name", ""), primary_name))
        ]

        if matching_items:
            best_item = matching_items[0]
            if set_name:
                target_set_norm = normalize_card_text(set_name)
                for candidate in matching_items:
                    if target_set_norm and target_set_norm in normalize_card_text(candidate.get("set_name", "")):
                        best_item = candidate
                        break

            quote = self._find_variant_quote(
                best_item.get("variants", []),
                condition="LP",
                finish=finish or "nonfoil",
            )
            if quote:
                cash_val = float(quote.get("cash_price") or 0.0)
                credit_val = float(quote.get("credit_price") or 0.0)
                sell_val = float(quote.get("store_sell_price") or 0.0)
                if cash_val > 0 or credit_val > 0:
                    buylist_cash = round(cash_val, 2)
                    buylist_credit = round(credit_val, 2)
                if sell_val > 0:
                    buylist_sell = round(sell_val, 2)

        raw_retail_price = float(retail.get("price") or 0.0)
        retail_price: Optional[float] = round(raw_retail_price, 2) if raw_retail_price > 0 else buylist_sell

        return {
            "in_stock": bool(retail.get("in_stock", False)),
            "retail_price": retail_price,
            "buylist_cash": buylist_cash,
            "buylist_credit": buylist_credit,
            "product_url": retail.get("product_url"),
        }

    def search_card(
        self,
        card_name: str,
        set_name: Optional[str] = None,
        set_code: Optional[str] = None,
        collector_number: Optional[str] = None,
        finish: str = "nonfoil",
    ) -> Dict[str, Any]:
        """Queries Mighty Meeple Shopify search & variants for retail stock and price."""
        if not card_name:
            return self._empty_result(card_name)

        clean_name = unicodedata.normalize("NFKD", card_name).encode("ASCII", "ignore").decode("utf-8").strip()
        primary_name = clean_name.split(" // ")[0].strip()
        fallback_search_url = f"{MIGHTY_MEEPLE_BASE}/search?q={urllib.parse.quote_plus(clean_name)}&type=product"

        try:
            quoted_q = f'"{primary_name}"'
            suggest_url_quoted = (
                f"{MIGHTY_MEEPLE_BASE}/search/suggest.json"
                f"?q={urllib.parse.quote_plus(quoted_q)}&resources[type]=product&resources[limit]=25"
            )
            suggest_url_unquoted = (
                f"{MIGHTY_MEEPLE_BASE}/search/suggest.json"
                f"?q={urllib.parse.quote_plus(primary_name)}&resources[type]=product&resources[limit]=25"
            )

            raw_prods_quoted = self._fetch_suggest_products(suggest_url_quoted)
            raw_prods_unquoted = self._fetch_suggest_products(suggest_url_unquoted)

            seen_handles = set()
            products = []
            for p in raw_prods_quoted + raw_prods_unquoted:
                h = p.get("handle")
                if h and h not in seen_handles:
                    seen_handles.add(h)
                    products.append(p)

            if not products:
                return self._empty_result(card_name, fallback_search_url)

            candidate_products = self._filter_products(
                products=products,
                card_name=card_name,
                set_name=set_name,
                set_code=set_code,
                collector_number=collector_number,
            )

            if collector_number and len(candidate_products) > 1:
                target_cn = str(collector_number).strip().lower()
                base_cn = target_cn.rstrip("ps")

                def product_matches_target_sku(prod_item: Dict[str, Any]) -> bool:
                    p_handle = prod_item.get("handle", "")
                    for v in self._get_product_variants(p_handle):
                        sku = (v.get("sku") or "").lower()
                        if target_cn.endswith("p") and (f"-{base_cn}-promo" in sku or f"-{target_cn}-" in sku):
                            return True
                        if target_cn.endswith("s") and (f"-{base_cn}-prerelease" in sku or f"-{target_cn}-" in sku):
                            return True
                        if f"-{target_cn}-" in sku or sku.endswith(f"-{target_cn}"):
                            return True
                    return False

                sku_candidates = [p for p in candidate_products if product_matches_target_sku(p)]
                if sku_candidates:
                    candidate_products = sku_candidates

            if not candidate_products:
                return self._empty_result(card_name, fallback_search_url)

            in_stock_matches = []
            out_of_stock_matches = []
            finish_clean = (finish or "nonfoil").lower().strip()
            is_foil_target = None if finish_clean == "any" else finish_clean in ("foil", "etched")

            for idx, prod in enumerate(candidate_products):
                is_prod_avail = bool(prod.get("available", False))
                if in_stock_matches and not is_prod_avail:
                    break
                if not in_stock_matches and not is_prod_avail and len(out_of_stock_matches) >= 3:
                    break

                handle = prod.get("handle")
                if idx > 0 and handle and handle not in MightyMeepleProvider._variant_cache:
                    time.sleep(0.25)

                prod_url = prod.get("url") or f"/products/{handle}"
                if not prod_url.startswith("http"):
                    prod_url = f"{MIGHTY_MEEPLE_BASE}{prod_url}"

                variants = self._get_product_variants(handle)
                if not variants:
                    continue

                matched = self._match_variant(variants, is_foil_target)
                if matched:
                    matched["product_url"] = prod_url
                    if matched.get("in_stock") and matched.get("price", 0) > 0:
                        in_stock_matches.append(matched)
                    else:
                        out_of_stock_matches.append(matched)

            if in_stock_matches:
                in_stock_matches.sort(key=lambda m: m["price"])
                return in_stock_matches[0]

            if out_of_stock_matches:
                priced_oos = [m for m in out_of_stock_matches if m.get("price", 0) > 0]
                if priced_oos:
                    priced_oos.sort(key=lambda m: m["price"])
                    return priced_oos[0]
                return out_of_stock_matches[0]

            first_prod = candidate_products[0]
            first_url = first_prod.get("url") or f"/products/{first_prod.get('handle', '')}"
            if not first_url.startswith("http"):
                first_url = f"{MIGHTY_MEEPLE_BASE}{first_url}"

            raw_p = first_prod.get("price") or first_prod.get("price_min") or "0.00"
            try:
                p_val = float(raw_p)
            except (ValueError, TypeError):
                p_val = 0.0

            return {
                "vendor_name": "Mighty Meeple",
                "price": round(p_val, 2),
                "condition": "NM",
                "in_stock": False,
                "product_url": first_url,
            }

        except Exception as exc:
            logger.error("Error checking Mighty Meeple for %r: %s", card_name, exc)
            return self._empty_result(card_name, fallback_search_url)

    def _fetch_suggest_products(self, url: str) -> List[Dict[str, Any]]:
        try:
            resp = self.session.get(url, timeout=8)
            if self._is_cloudflare_challenge(resp):
                logger.warning("Cloudflare/bot challenge on suggest query: %s", url)
                return []
            if resp.status_code == 200:
                data = resp.json()
                if "resources" in data and "results" in data["resources"]:
                    return data["resources"]["results"].get("products", [])
                if "products" in data:
                    return data["products"]
        except Exception as exc:
            logger.debug("Suggest query failed for %s: %s", url, exc)
        return []

    @staticmethod
    def _is_cloudflare_challenge(resp) -> bool:
        if resp is None:
            return True
        if resp.status_code in (403, 429, 503):
            return True
        if hasattr(resp, "headers") and "cf-mitigated" in resp.headers:
            return True
        text = getattr(resp, "text", "") or ""
        return (
            "Just a moment..." in text
            or "challenge-platform" in text
            or "cf-chl-widget" in text
            or "Verifying your connection..." in text
        )

    def _get_product_variants(self, handle: Optional[str]) -> List[Dict[str, Any]]:
        if not handle:
            return []
        if handle in MightyMeepleProvider._variant_cache:
            return MightyMeepleProvider._variant_cache[handle]

        for attempt in range(3):
            try:
                url = f"{MIGHTY_MEEPLE_BASE}/products/{handle}.js"
                r = self.session.get(url, timeout=8)
                if self._is_cloudflare_challenge(r):
                    retry_sec = 2.0
                    if hasattr(r, "headers") and "Retry-After" in r.headers:
                        try:
                            retry_sec = max(2.0, float(r.headers["Retry-After"]))
                        except (ValueError, TypeError):
                            pass
                    time.sleep(retry_sec * (attempt + 1))
                    continue

                if r.status_code == 200:
                    variants = r.json().get("variants", [])
                    if variants:
                        MightyMeepleProvider._variant_cache[handle] = variants
                        return variants
                elif r.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
            except Exception as exc:
                logger.debug("Variant fetch attempt %d failed for %s: %s", attempt + 1, handle, exc)
                time.sleep(0.5)
        return []

    def _is_card_name_match(self, title: str, card_name: str) -> bool:
        norm_target = normalize_card_text(card_name)
        if not norm_target:
            return False

        norm_title = normalize_card_text(title)
        for jt in JUNK_TERMS:
            jt_norm = normalize_card_text(jt)
            if re.search(r"\b" + re.escape(jt_norm) + r"\b", norm_title):
                if not re.search(r"\b" + re.escape(jt_norm) + r"\b", norm_target):
                    return False

        base = extract_base_name_from_title(title)
        if not base:
            return False
        norm_base = normalize_card_text(base)

        if norm_base == norm_target:
            return True

        if any(sep in base for sep in (" - ", " — ", " – ")):
            parts = [normalize_card_text(p) for p in re.split(r"\s+[-—–]\s+", base) if p.strip()]
            if norm_target in parts:
                return True

        title_faces = [normalize_card_text(f) for f in re.split(r"/+", base) if f.strip()]
        if norm_target in title_faces:
            return True

        if "/" in card_name:
            target_faces = [normalize_card_text(f) for f in re.split(r"/+", card_name) if f.strip()]
            if target_faces == title_faces:
                return True
            if len(target_faces) > 1 and len(title_faces) == 1 and title_faces[0] == target_faces[0]:
                return True

        return False

    def _filter_products(
        self,
        products: List[Dict[str, Any]],
        card_name: str,
        set_name: Optional[str],
        set_code: Optional[str],
        collector_number: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        primary_name = card_name.split(" // ")[0].strip()

        valid_name_products = [
            p for p in products
            if self._is_card_name_match(p.get("title", ""), card_name)
            or self._is_card_name_match(p.get("title", ""), primary_name)
        ]
        if not valid_name_products:
            return []

        is_any = not set_code or set_code.strip().upper() in ("ANY", "")
        if is_any:
            def _any_sort_key(p):
                avail = bool(p.get("available", False))
                title = (p.get("title") or "").lower()
                is_special = any(k in title for k in ("serialized", "poster", "oversized", "art card", "showcase"))
                return (not avail, is_special)
            return sorted(valid_name_products, key=_any_sort_key)

        target_set_code = (set_code or "").upper().strip()
        target_coll_num = str(collector_number or "").lower().strip() if collector_number else ""
        target_coll_digits = re.sub(r"[^\d]", "", target_coll_num)
        target_set_name = (set_name or "").lower().strip()

        if target_set_name or target_set_code:
            def _set_match_priority(p):
                title = (p.get("title") or "").lower()
                handle = (p.get("handle") or "").lower()
                matches_name = bool(target_set_name and (target_set_name in title or target_set_name.replace(" ", "-") in handle))
                matches_code = bool(
                    target_set_code and (
                        f"[{target_set_code.lower()}]" in title
                        or f"-{target_set_code.lower()}-" in handle
                        or handle.endswith(f"-{target_set_code.lower()}")
                    )
                )
                return (not (matches_name or matches_code), not matches_name)
            valid_name_products = sorted(valid_name_products, key=_set_match_priority)

        if target_set_code and target_coll_num:
            for idx, p in enumerate(valid_name_products):
                handle = p.get("handle")
                if idx > 0 and handle and handle not in MightyMeepleProvider._variant_cache:
                    time.sleep(0.25)
                variants = self._get_product_variants(handle)
                title_lower = p.get("title", "").lower()
                for v in variants:
                    sku = (v.get("sku") or "").upper()
                    if not sku:
                        continue
                    sku_lower = sku.lower()
                    sku_parts = sku.split("-")
                    if len(sku_parts) >= 2:
                        sku_set = sku_parts[0]
                        sku_num = sku_parts[1].lower()
                        set_matches = (
                            sku_set == target_set_code
                            or (sku_set.startswith("P") and sku_set[1:] == target_set_code)
                            or (target_set_code.startswith("P") and target_set_code[1:] == sku_set)
                        )
                        if set_matches:
                            if sku_num == target_coll_num:
                                if "promo pack" in title_lower and target_coll_num.endswith("s"):
                                    continue
                                if "prerelease" in title_lower and target_coll_num.endswith("p"):
                                    continue
                                return [p]
                            if target_coll_digits and sku_num == target_coll_digits:
                                if target_coll_num.endswith("p") and ("promo-pack" in sku_lower or "promo pack" in title_lower):
                                    return [p]
                                if target_coll_num.endswith("s") and ("prerelease" in sku_lower or "prerelease" in title_lower):
                                    return [p]
                                if not target_coll_num.endswith("p") and not target_coll_num.endswith("s"):
                                    return [p]

        if target_coll_num:
            for p in valid_name_products:
                desc = (p.get("description") or "").lower()
                if f"<td>{target_coll_num}</td>" in desc or f"<td>#{target_coll_num}</td>" in desc:
                    return [p]

        target_set_terms = []
        if target_set_name:
            target_set_terms.append(target_set_name)
        if target_set_code:
            target_set_terms.append(f"[{target_set_code.lower()}]")
            target_set_terms.append(target_set_code.lower())

        set_matched = [
            p for p in valid_name_products
            if any(st in p.get("title", "").lower() for st in target_set_terms)
        ]
        return set_matched or valid_name_products

    def _match_variant(self, variants: List[Dict[str, Any]], is_foil_target: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        if not variants:
            return None

        in_stock_variants = []
        out_of_stock_variants = []

        for v in variants:
            title = v.get("title", "") or v.get("name", "")
            title_lower = title.lower()
            is_foil = "foil" in title_lower
            is_available = bool(v.get("available", False))

            if is_foil_target is not None and is_foil_target != is_foil:
                continue

            price_cents = v.get("price", 0)
            price_dollars = price_cents / 100.0 if isinstance(price_cents, (int, float)) else 0.0

            if "damaged" in title_lower or "dmg" in title_lower:
                cond_rank, cond_label = 0, "Damaged"
            elif "heavily played" in title_lower or "hp" in title_lower:
                cond_rank, cond_label = 1, "HP"
            elif "moderately played" in title_lower or "mp" in title_lower:
                cond_rank, cond_label = 2, "MP"
            elif "lightly played" in title_lower or "lp" in title_lower:
                cond_rank, cond_label = 3, "LP"
            elif "near mint" in title_lower or "nm" in title_lower:
                cond_rank, cond_label = 4, "NM"
            else:
                cond_rank, cond_label = 2, "Played"

            finish_label = "Foil" if is_foil else ""
            full_cond = f"{cond_label} {finish_label}".strip()

            entry = {
                "price": price_dollars,
                "condition": full_cond,
                "cond_rank": cond_rank,
                "in_stock": is_available,
            }

            if is_available and price_dollars > 0:
                in_stock_variants.append(entry)
            else:
                out_of_stock_variants.append(entry)

        if in_stock_variants:
            in_stock_variants.sort(key=lambda item: (item["cond_rank"], item["price"]))
            best = in_stock_variants[0]
            return {
                "vendor_name": "Mighty Meeple",
                "price": round(best["price"], 2),
                "condition": best["condition"],
                "in_stock": True,
            }

        if out_of_stock_variants:
            priced_oos = [item for item in out_of_stock_variants if item["price"] > 0]
            if priced_oos:
                nm_oos = [item for item in priced_oos if item["cond_rank"] == 4]
                if nm_oos:
                    nm_oos.sort(key=lambda item: item["price"])
                    best = nm_oos[0]
                else:
                    priced_oos.sort(key=lambda item: item["price"])
                    best = priced_oos[0]
            else:
                best = out_of_stock_variants[0]

            return {
                "vendor_name": "Mighty Meeple",
                "price": round(best["price"], 2),
                "condition": best["condition"],
                "in_stock": False,
            }

        return None

    @staticmethod
    def _empty_result(card_name: str, url: Optional[str] = None) -> Dict[str, Any]:
        encoded = urllib.parse.quote_plus(card_name) if card_name else ""
        return {
            "vendor_name": "Mighty Meeple",
            "price": 0.0,
            "condition": "NM",
            "in_stock": False,
            "product_url": url or f"{MIGHTY_MEEPLE_BASE}/search?q={encoded}&type=product",
        }

    def search_buylist(
        self,
        query: str,
        set_name: Optional[str] = None,
        game: str = "mtg",
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Queries Mighty Meeple's live BinderPOS buylist API."""
        if not query or not str(query).strip():
            return {"items": [], "total": 0}

        clean_query = str(query).strip()
        url = f"{BINDERPOS_PORTAL_BASE}/external/shopify/{MIGHTY_MEEPLE_STORE_ID}/cards/{game or 'mtg'}"
        params: Dict[str, Any] = {
            "keyword": clean_query,
            "limit": max(1, min(50, limit)),
            "offset": max(0, offset),
        }
        if set_name and str(set_name).strip():
            params["setName"] = str(set_name).strip()

        try:
            resp = self.session.get(url, params=params, timeout=12)
            if resp.status_code != 200:
                return {"items": [], "total": 0, "error": f"HTTP {resp.status_code}"}

            raw_list = resp.json()
            if not isinstance(raw_list, list):
                return {"items": [], "total": 0}

            items = []
            for raw in raw_list:
                parsed_variants = []
                for v in raw.get("variants", []):
                    cond_name = v.get("variantName", "Near Mint")
                    for bt in v.get("cardBuylistTypes", []):
                        finish_type = bt.get("type") or bt.get("legacyType") or "Normal"
                        parsed_variants.append({
                            "variant_id": bt.get("productVariantId"),
                            "condition": cond_name,
                            "finish": finish_type,
                            "store_sell_price": round(float(bt.get("storeSellPrice", 0.0) or 0.0), 2),
                            "cash_price": round(float(bt.get("buyPrice", 0.0) or 0.0), 2),
                            "credit_price": round(float(bt.get("creditBuyPrice", 0.0) or 0.0), 2),
                            "max_quantity": int(bt.get("maxPurchaseQuantity", 0) or 0),
                        })

                items.append({
                    "id": raw.get("id"),
                    "card_name": raw.get("cardName") or clean_query,
                    "set_name": raw.get("setName") or "Unknown Set",
                    "variants": parsed_variants,
                })

            return {"items": items, "total": len(items)}
        except Exception as exc:
            logger.error("Error querying Mighty Meeple buylist for %r: %s", query, exc)
            return {"items": [], "total": 0, "error": str(exc)}

    @staticmethod
    def _find_variant_quote(
        variants: List[Dict[str, Any]],
        condition: str = "LP",
        finish: str = "nonfoil",
    ) -> Optional[Dict[str, Any]]:
        if not variants:
            return None

        cond_clean = (condition or "LP").lower().strip()
        cond_map = {
            "nm": "near",
            "near mint": "near",
            "lp": "light",
            "lightly played": "light",
            "mp": "moderate",
            "moderately played": "moderate",
            "hp": "heavily",
            "heavily played": "heavily",
            "dmg": "damaged",
            "damaged": "damaged",
        }
        cond_token = cond_map.get(cond_clean, cond_clean)
        is_foil_target = (finish or "nonfoil").lower().strip() in ("foil", "etched")

        for v in variants:
            v_cond = v.get("condition", "").lower()
            v_fin = v.get("finish", "").lower()
            v_foil = "foil" in v_fin or "etched" in v_fin
            if cond_token in v_cond and v_foil == is_foil_target:
                return v

        for v in variants:
            v_fin = v.get("finish", "").lower()
            v_foil = "foil" in v_fin or "etched" in v_fin
            if v_foil == is_foil_target and (v.get("credit_price", 0) > 0 or v.get("cash_price", 0) > 0):
                return v

        for v in variants:
            if cond_token in v.get("condition", "").lower():
                return v

        for v in variants:
            if v.get("credit_price", 0) > 0 or v.get("cash_price", 0) > 0:
                return v

        return variants[0]
