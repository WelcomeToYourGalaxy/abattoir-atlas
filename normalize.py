"""
Normalization used by the matcher. Nothing here touches the stored record --
normalized forms are derived on the fly and used only for comparison, so the
published spelling of every name and address survives into the output.
"""

from __future__ import annotations

import re
import unicodedata

# Corporate suffixes across the jurisdictions in scope. Stripped for comparison
# only: "Smithfield Packaged Meats Corp." and "Smithfield Packaged Meats" are
# the same plant, but the output keeps whichever the registry printed.
LEGAL_SUFFIXES = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "plc", "lp", "llp", "pty", "pte",
    "gmbh", "mbh", "ag", "kg", "ohg", "eg", "se",
    "sa", "sas", "sarl", "sprl", "nv", "bv", "cv", "vof",
    "spa", "srl", "snc", "sas",
    "ab", "as", "asa", "oy", "oyj", "aps", "a/s",
    "sl", "slu", "sau", "scl", "coop", "sccl",
    "ltda", "sa de cv", "eireli", "me", "epp",
    "as", "ao", "ooo", "zao", "pao",
    "sp z oo", "spzoo", "sro", "as", "kft", "zrt", "bt",
    "kk", "yk",
}

# Words that appear in facility names so often they carry no discriminating
# power. Kept out of the token set used for similarity scoring.
STOPWORDS = {
    "the", "and", "of", "de", "del", "la", "le", "les", "el", "los", "das",
    "dos", "da", "do", "der", "die", "und", "van", "von",
    "meat", "meats", "foods", "food", "packing", "packers", "abattoir",
    "slaughterhouse", "slaughter", "processing", "processors", "plant",
    "matadero", "frigorifico", "schlachthof", "abattoirs", "macello",
}

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_HOUSE_NUM = re.compile(r"\b(\d{1,6})\s*[a-zA-Z]?\b")


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    s = strip_accents(s).lower()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def norm_name(s: str | None) -> str:
    """Lowercased, accent-folded, legal suffix stripped."""
    base = norm_text(s)
    if not base:
        return ""
    toks = [t for t in base.split() if t not in LEGAL_SUFFIXES]
    return " ".join(toks)


def name_tokens(s: str | None) -> frozenset[str]:
    """Discriminating tokens only, for set-similarity scoring."""
    toks = {t for t in norm_name(s).split() if t not in STOPWORDS and len(t) > 1}
    return frozenset(toks)


def token_set_ratio(a: str | None, b: str | None) -> float:
    """Jaccard over discriminating tokens. 0.0-1.0.

    Chosen over edit distance because facility names differ far more by word
    order and by which corporate layer the registry recorded than by typo.
    """
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def norm_id(scheme: str | None, value: str | None) -> str | None:
    """Canonical form of an establishment number.

    Registries print the same number half a dozen ways: `M-1234`, `M 1234`,
    `EST. 1234`, `1234 M`. CIFER stores the foreign number with the country
    prefix attached. All of those have to land on one string or the ID tier of
    the matcher does nothing.
    """
    if not value:
        return None
    v = strip_accents(str(value)).upper()
    v = re.sub(r"\b(EST|ESTAB|ESTABLISHMENT|NO|N|NUM|NUMERO|APPROVAL)\b\.?", "", v)
    # A slash between two parts is part of the number, not decoration: Belgium's
    # "1/31" and "13/1" are two plants, and with every mark stripped both read
    # "131" and were joined. Spaces, dots and hyphens are still dropped, since
    # "M-1234", "M 1234" and "M1234" are one number printed three ways.
    v = re.sub(r"\s*/\s*", "/", v)
    v = re.sub(r"[^A-Z0-9/]", "", v).strip("/")
    if not v:
        return None
    if scheme:
        return f"{scheme.upper()}:{v}"
    return v


def norm_postcode(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", strip_accents(str(s)).upper())


def house_number(address: str | None) -> str:
    """First standalone number in an address line.

    Weak signal on its own, strong in combination with postcode -- two plants
    at the same postcode with different street numbers are two plants.
    """
    if not address:
        return ""
    m = _HOUSE_NUM.search(address)
    return m.group(1) if m else ""


def addr_key(address: str | None, postcode: str | None) -> str:
    """Composite address comparison key. Empty string means unusable."""
    pc = norm_postcode(postcode)
    hn = house_number(address)
    if pc and hn:
        return f"{pc}#{hn}"
    if pc:
        return f"{pc}#"
    return ""
