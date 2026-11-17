"""Card name, finish, and condition normalization utilities for VaultSwap."""

from __future__ import annotations

import html
import re
import unicodedata
from functools import lru_cache
from typing import FrozenSet, Optional, Set


def fix_mojibake(text: Optional[str]) -> str:
    """Repairs UTF-8 bytes mistakenly decoded as Latin-1 or CP1252."""
    if not text:
        return ""

    clean = str(text)
    if any(marker in clean for marker in ("\u00c3", "\u00c2", "\u00e2\u20ac")):
        for enc in ("latin-1", "cp1252"):
            try:
                candidate = clean.encode(enc).decode("utf-8")
                if candidate and "\ufffd" not in candidate:
                    clean = candidate
                    break
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

    return clean


@lru_cache(maxsize=32768)
def strip_accents(text: Optional[str]) -> str:
    """Removes combining diacritics via NFKD Unicode normalization."""
    if not text:
        return ""
    clean = fix_mojibake(str(text))
    nfkd = unicodedata.normalize("NFKD", clean)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


@lru_cache(maxsize=32768)
def normalize_card_name(name: Optional[str], strip_diacritics: bool = False) -> str:
    """Cleans card names (mojibake, HTML, curly quotes, foil markers, DFC slashes)."""
    if not name:
        return ""

    clean = fix_mojibake(str(name))
    clean = re.sub(r"<[^>]+>", "", clean)
    clean = html.unescape(clean).strip("\"'").strip()

    clean = clean.replace("\u2019", "'").replace("\u2018", "'")
    clean = clean.replace("\u201C", '"').replace("\u201D", '"')
    clean = clean.replace("\u00B4", "'").replace("`", "'")

    clean = re.sub(r"\s*\*.*?\*\s*$", "", clean)

    if "/" in clean:
        parts = [p.strip() for p in re.split(r"\s*/+\s*", clean) if p.strip()]
        if len(parts) >= 2:
            clean = " // ".join(parts)

    if strip_diacritics:
        clean = strip_accents(clean)

    return clean.strip().strip("\"'").strip()


@lru_cache(maxsize=32768)
def _get_card_match_keys_cached(card_name: Optional[str]) -> FrozenSet[str]:
    if not card_name:
        return frozenset()

    clean = normalize_card_name(card_name)
    if not clean:
        return frozenset()

    keys = set()
    full_lower = clean.lower()
    keys.add(full_lower)
    keys.add(strip_accents(full_lower))

    if " // " in clean:
        front_lower = clean.split(" // ")[0].strip().lower()
        keys.add(front_lower)
        keys.add(strip_accents(front_lower))

    return frozenset(keys)


def get_card_match_keys(card_name: Optional[str]) -> Set[str]:
    """Returns canonical lowercase and unaccented match keys (including DFC front face)."""
    return set(_get_card_match_keys_cached(card_name))


def card_names_match(name1: Optional[str], name2: Optional[str]) -> bool:
    """Compares two card names accounting for accents, DFC front faces, and mojibake."""
    if not name1 or not name2:
        return False
    return bool(get_card_match_keys(name1).intersection(get_card_match_keys(name2)))


def normalize_finish(raw_finish: Optional[str]) -> str:
    """Normalizes ManaBox / user finish strings to 'nonfoil', 'foil', or 'etched'."""
    if not raw_finish:
        return "nonfoil"
    clean = str(raw_finish).strip().lower()
    if clean in ("etched", "foil etched", "e", "*e*"):
        return "etched"
    if clean in ("foil", "true", "1", "yes", "f", "*f*", "surge", "halo", "textured"):
        return "foil"
    return "nonfoil"


def normalize_condition(raw_condition: Optional[str]) -> str:
    """Normalizes condition strings to compact abbreviations ('NM', 'LP', 'MP', 'HP', 'DMG')."""
    if not raw_condition:
        return "NM"
    clean = str(raw_condition).strip().lower()
    if clean in ("nm", "near mint", "near_mint", "mint", "m"):
        return "NM"
    if clean in ("lp", "lightly played", "lightly_played", "sp", "slightly played", "excellent", "ex"):
        return "LP"
    if clean in ("mp", "moderately played", "moderately_played", "good", "gd", "played", "pl"):
        return "MP"
    if clean in ("hp", "heavily played", "heavily_played", "poor"):
        return "HP"
    if clean in ("dmg", "damaged", "d"):
        return "DMG"
    return str(raw_condition).strip().upper()[:10] or "NM"
