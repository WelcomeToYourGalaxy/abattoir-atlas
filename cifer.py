#!/usr/bin/env python3
"""
CIFER retrieval — China GACC registered overseas food establishments.

CIFER is the single most valuable source in this pipeline and the only one with
no bulk download. It matters because each record carries the *home authority's*
establishment number alongside the Chinese one, which is what lets a Chinese
registration collapse onto the USDA, DAFF, SIF or EU record for the same plant
instead of becoming a second pin.

  ONE THING YOU MUST DO FIRST
  ---------------------------
  The request shape below is NOT confirmed. The sandbox this was written in had
  no route to ciferquery.singlewindow.cn, so the endpoint path, parameter names
  and response field names in ENDPOINT are placeholders in the right shape, not
  observed fact. Run `python cifer.py discover` for the ninety seconds of
  devtools work that replaces them with the real ones.

  Everything downstream — pagination, checkpointing, retry, normalization,
  output — is real and does not change once you fill that in.

Usage:
    python cifer.py discover
    python cifer.py probe   --country USA          # 1 page, prints raw JSON
    python cifer.py harvest --categories meat --out raw/cifer.jsonl
    python cifer.py harvest --resume               # picks up where it stopped
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# UNVERIFIED — replace from devtools. See `discover`.
# ---------------------------------------------------------------------------

ENDPOINT = {
    "base": "https://ciferquery.singlewindow.cn",
    "path": "/enterprise/query",          # UNVERIFIED
    "method": "POST",                     # UNVERIFIED
    "content_type": "application/json",

    # Request parameter names, as the site sends them.
    "params": {
        "page": "pageNum",                # UNVERIFIED
        "page_size": "pageSize",          # UNVERIFIED
        "country": "countryCode",         # UNVERIFIED
        "category": "productType",        # UNVERIFIED
        "keyword": "keyword",             # UNVERIFIED
    },

    # Where the rows and the total live in the response body.
    "rows_path": ["data", "list"],        # UNVERIFIED
    "total_path": ["data", "total"],      # UNVERIFIED

    "page_size": 50,                      # raise only if the site allows it
}

# Response field names, mapped onto what parsers.parse_cifer reads. Several
# plausible spellings are tried per field, so minor differences in the real
# response survive without a code change.
FIELD_ALIASES = {
    "registerNo":        ["registerNo", "registNo", "cnRegisterNo", "regNo", "code"],
    "foreignRegisterNo": ["foreignRegisterNo", "foreignNo", "originalNo",
                          "countryRegisterNo", "abroadRegisterNo"],
    "enName":            ["enName", "enterpriseNameEn", "nameEn", "englishName"],
    "name":              ["name", "enterpriseName", "cnName"],
    "countryCode":       ["countryCode", "country", "nationCode", "countryEn"],
    "address":           ["address", "addr", "enterpriseAddress"],
    "enAddress":         ["enAddress", "addressEn", "enterpriseAddressEn"],
    "productCategory":   ["productCategory", "productType", "products",
                          "categoryList", "productName"],
    "validDate":         ["validDate", "expiryDate", "validTo", "endDate"],
}

# ISO3 -> the numeric code the site uses, read off its own country dropdown.
# Both forms work in the country field; the numeric one is what the request
# carries. Anything absent here is simply not harvested -- a visible gap.
COUNTRY_CODES: dict[str, str] = {
    "ARG": "402",
    "AUS": "601",
    "AUT": "315",
    "BEL": "301",
    "BRA": "410",
    "CAN": "501",
    "CHL": "412",
    "CHN": "142",
    "COL": "413",
    "CRI": "415",
    "CZE": "352",
    "DEU": "304",
    "DNK": "302",
    "ESP": "312",
    "EST": "334",
    "FIN": "318",
    "FRA": "305",
    "GBR": "303",
    "GRC": "310",
    "HRV": "351",
    "HUN": "321",
    "IND": "111",
    "IRL": "306",
    "ISL": "322",
    "ITA": "307",
    "JPN": "116",
    "KOR": "133",
    "LTU": "336",
    "LVA": "335",
    "MEX": "429",
    "MNG": "124",
    "NAM": "234",
    "NLD": "309",
    "NOR": "326",
    "NZL": "609",
    "PAN": "432",
    "PER": "434",
    "POL": "327",
    "PRT": "311",
    "PRY": "433",
    "ROU": "328",
    "RUS": "344",
    "SRB": "358",
    "SVK": "353",
    "SVN": "350",
    "SWE": "330",
    "THA": "136",
    "TUR": "137",
    "UKR": "347",
    "URY": "444",
    "USA": "502",
    "VNM": "141",
    "ZAF": "244",
    "BLR": "340",
    "BGR": "316",
    "MDA": "343",
    "MKD": "354",
    "BIH": "355",
    "MNE": "359",
    "ALB": "313",
    "KAZ": "145",
    "IDN": "112",
    "MYS": "122",
    "PHL": "129",
    "SGP": "132",
    "LKA": "134",
    "PAK": "127",
    "BGD": "103",
    "NPL": "125",
    "MAR": "232",
    "EGY": "215",
    "TUN": "249",
    "KEN": "224",
    "ETH": "217",
    "NGA": "236",
    "SDN": "246",
    "ZWE": "716",
    "BOL": "408",
    "ECU": "419",
    "GTM": "423",
    "NIC": "431",
    "SLV": "440",
    "HND": "426",
    "DOM": "418",
    "CUB": "416",
    "JAM": "427",
    "TTO": "442",
    "SUR": "441",
    "GUY": "424",
    "VEN": "445",
    "CHE": "331",
    "LUX": "308",
    "MLT": "324",
    "CYP": "108",
    "ISR": "115",
    "ARE": "138",
    "SAU": "131",
    "QAT": "130",
    "OMN": "126",
    "KWT": "118",
    "JOR": "117",
    "LBN": "120",
    "IRN": "113",
    "IRQ": "114",
    "GEO": "150",
    "ARM": "151",
    "AZE": "152",
    "UZB": "149",
    "TKM": "148",
    "TJK": "147",
    "KGZ": "146",
    "MMR": "106",
    "KHM": "107",
    "LAO": "119",
    "PRK": "109",
    "FRO": "357",
    "GRL": "503",
}

# Product category codes, taken from the site's own code table. The top-level
# code covers its children, so "01" pulls every meat subcategory in one sweep;
# the subcodes are listed because species is the field this atlas needs and
# querying them separately is what attaches species to each plant.
CATEGORIES = {
    "01": "meat and meat products",
    "0101": "beef",         "0102": "pork",        "0103": "mutton",
    "0104": "horse, donkey, mule",                 "0105": "poultry",
    "0106": "rabbit",       "0107": "venison",     "0108": "camel",
    "0109": "bear",         "0110": "kangaroo",    "0111": "dog",
    "0112": "other meat",
    "02": "casings",
    "0201": "pig casings",  "0202": "sheep casings", "0203": "cattle casings",
    "0204": "deer casings", "0205": "other casings",
    "05": "eggs and egg products",
    "17": "dairy",
    "18": "aquatic products",
}

# Which species group each meat subcode implies. This is the reason to query by
# subcode rather than pulling "01" wholesale: the response does not name a
# species, the query does.
CATEGORY_SPECIES = {
    "0101": ["bovine"], "0102": ["porcine"], "0103": ["ovine", "caprine"],
    "0104": ["equine"], "0105": ["poultry"], "0106": ["lagomorph"],
    "0107": ["cervid"], "0108": ["other"],   "0109": ["wild_game"],
    "0110": ["wild_game"], "0111": ["other"], "0112": ["other"],
    "0201": ["porcine"], "0202": ["ovine"],  "0203": ["bovine"],
    "0204": ["cervid"],  "0205": ["other"],
}

CATEGORY_SETS = {
    "meat": ["0101", "0102", "0103", "0104", "0105", "0106", "0107",
             "0108", "0109", "0110", "0111", "0112"],
    "meat_and_casings": ["0101", "0102", "0103", "0104", "0105", "0106",
                         "0107", "0108", "0109", "0110", "0111", "0112",
                         "0201", "0202", "0203", "0204", "0205"],
    "all_animal": ["01", "02", "05", "17", "18"],
}

PAUSE = (1.4, 2.6)        # seconds between requests, randomized
MAX_RETRIES = 5
CHECKPOINT = Path("work/cifer_checkpoint.json")


# ---------------------------------------------------------------------------

def _dig(obj, path: list[str]):
    for k in path:
        if not isinstance(obj, dict) or k not in obj:
            return None
        obj = obj[k]
    return obj


def _alias(row: dict, canonical: str):
    for k in FIELD_ALIASES.get(canonical, [canonical]):
        if k in row and row[k] not in (None, ""):
            return row[k]
    # case-insensitive second pass
    low = {k.lower(): v for k, v in row.items()}
    for k in FIELD_ALIASES.get(canonical, [canonical]):
        v = low.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def normalize_row(row: dict) -> dict:
    """Map a raw response row onto the keys parsers.parse_cifer expects.

    The full original row is kept under `_raw` so nothing the site published is
    lost to a mapping I guessed wrong.
    """
    cats = _alias(row, "productCategory")
    if isinstance(cats, str):
        cats = [cats]
    elif isinstance(cats, list):
        cats = [c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                for c in cats]
    else:
        cats = []

    return {
        "registerNo": _alias(row, "registerNo"),
        "foreignRegisterNo": _alias(row, "foreignRegisterNo"),
        "enName": _alias(row, "enName"),
        "name": _alias(row, "name"),
        "countryCode": _alias(row, "countryCode"),
        "address": _alias(row, "address"),
        "enAddress": _alias(row, "enAddress"),
        "productCategory": cats,
        "validDate": _alias(row, "validDate"),
        "_raw": row,
    }


# ---------------------------------------------------------------------------

def request_page(page: int, *, country: str | None = None,
                 category: str | None = None, timeout: int = 40) -> dict:
    """One page. Retries on transient failure with exponential backoff."""
    P = ENDPOINT["params"]
    payload = {P["page"]: page, P["page_size"]: ENDPOINT["page_size"]}
    if country:
        payload[P["country"]] = country
    if category:
        payload[P["category"]] = category

    url = ENDPOINT["base"] + ENDPOINT["path"]
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en,zh-CN;q=0.8",
        "User-Agent": "Mozilla/5.0 (compatible; abattoir-atlas/1.0)",
        "Referer": ENDPOINT["base"] + "/",
    }

    if ENDPOINT["method"].upper() == "GET":
        req = urllib.request.Request(
            url + "?" + urllib.parse.urlencode(payload), headers=headers)
    else:
        headers["Content-Type"] = ENDPOINT["content_type"]
        body = (json.dumps(payload).encode()
                if "json" in ENDPOINT["content_type"]
                else urllib.parse.urlencode(payload).encode())
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    last = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code in (400, 403, 404):
                raise RuntimeError(
                    f"{last} on {url}. The endpoint config is probably wrong — "
                    f"run `python cifer.py discover`.") from exc
        except Exception as exc:
            last = str(exc)
        wait = (2 ** attempt) + random.random() * 2
        print(f"    retry {attempt+1}/{MAX_RETRIES} after {last}; "
              f"waiting {wait:.1f}s", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"gave up after {MAX_RETRIES} attempts: {last}")


def harvest(out_path: Path, *, countries: list[str] | None,
            categories: list[str], resume: bool) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)

    state = {"done": [], "rows": 0}
    if resume and CHECKPOINT.exists():
        state = json.loads(CHECKPOINT.read_text())
        print(f"resuming: {len(state['done'])} slices done, "
              f"{state['rows']:,} rows already written")

    seen: set[str] = set()
    if resume and out_path.exists():
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    seen.add(str(json.loads(line).get("registerNo")))
                except Exception:
                    pass

    targets = [(c, cat) for cat in categories
               for c in (countries or [None])]

    mode = "a" if resume and out_path.exists() else "w"
    with open(out_path, mode, encoding="utf-8") as fh:
        for country, category in targets:
            slice_id = f"{country or 'ALL'}|{category}"
            if slice_id in state["done"]:
                continue
            print(f"[{slice_id}]")

            page, total, got = 1, None, 0
            while True:
                data = request_page(page, country=country, category=category)
                rows = _dig(data, ENDPOINT["rows_path"])
                if rows is None:
                    print(f"  no rows at {ENDPOINT['rows_path']} — response keys: "
                          f"{list(data.keys())}", file=sys.stderr)
                    print("  run `python cifer.py probe` and fix rows_path.",
                          file=sys.stderr)
                    sys.exit(1)
                if total is None:
                    total = _dig(data, ENDPOINT["total_path"])
                    print(f"  {total if total is not None else '?'} rows reported")

                if not rows:
                    break

                for raw in rows:
                    rec = normalize_row(raw)
                    key = str(rec.get("registerNo"))
                    if key and key in seen:
                        continue
                    seen.add(key)
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    got += 1
                    state["rows"] += 1
                fh.flush()

                print(f"  page {page}: +{len(rows)} (slice total {got:,})")
                if total is not None and page * ENDPOINT["page_size"] >= total:
                    break
                if len(rows) < ENDPOINT["page_size"]:
                    break
                page += 1
                time.sleep(random.uniform(*PAUSE))

            state["done"].append(slice_id)
            CHECKPOINT.write_text(json.dumps(state))

    print(f"\n{state['rows']:,} rows in {out_path}")
    print(f"next: python run.py parse --source cifer_china --file {out_path.name}")


# ---------------------------------------------------------------------------

DISCOVER = """
Ninety seconds of devtools, once. What you are replacing is the ENDPOINT dict
at the top of this file.

1.  Open https://ciferquery.singlewindow.cn in Chrome or Firefox.
2.  F12, Network tab, filter to Fetch/XHR, tick "Preserve log".
3.  Run a search on the page. Pick a country and the meat category, then hit
    search, then click through to page 2 — page 2 is what reveals the
    pagination parameter.
4.  In the Network list, find the request that returns the establishment rows.
    Ignore the ones returning dropdown options and translations.

    From that request, read off:

      path          the URL after the host              -> ENDPOINT["path"]
      method        GET or POST                         -> ENDPOINT["method"]
      Payload tab   the parameter names it sends        -> ENDPOINT["params"]
                    (which key changed between page 1 and page 2 is your
                     "page" param; the one holding 10 or 20 is "page_size")

5.  Open the Response tab and read off:

      where the array of rows sits, e.g. data.list      -> rows_path
      where the row count sits, e.g. data.total         -> total_path
      the field names on one row                        -> FIELD_ALIASES,
                                                            if they are not
                                                            already listed

6.  Copy the country dropdown's option values into COUNTRY_CODES, keyed by
    ISO3. Anything you leave out is simply not harvested.

7.  Check:   python cifer.py probe --country <one country code>

    That prints one raw page. If you can see establishment names in it, the
    config is right and `harvest` will run.

Two notes. The site is a government portal with no published rate limit, so
PAUSE is set to a request every second and a half — leave it there. And the
response may be gzipped or wrapped in an envelope with a status code; if probe
returns something that is not the row array, the Response tab tells you the
real shape in about five seconds.
"""


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover")

    pr = sub.add_parser("probe")
    pr.add_argument("--country"); pr.add_argument("--category")
    pr.add_argument("--page", type=int, default=1)

    h = sub.add_parser("harvest")
    h.add_argument("--out", default="raw/cifer.jsonl")
    h.add_argument("--categories", default="meat", choices=list(CATEGORY_SETS))
    h.add_argument("--countries", default=None,
                   help="comma-separated site country codes; omit to query all "
                        "countries in one sweep if the site allows it")
    h.add_argument("--resume", action="store_true")

    a = p.parse_args()

    if a.cmd == "discover":
        print(DISCOVER)
        return

    if a.cmd == "probe":
        data = request_page(a.page, country=a.country, category=a.category)
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000])
        rows = _dig(data, ENDPOINT["rows_path"])
        if rows:
            print(f"\n--- rows_path {ENDPOINT['rows_path']} holds "
                  f"{len(rows)} rows. First one normalized:\n")
            print(json.dumps(normalize_row(rows[0]), ensure_ascii=False, indent=2))
        else:
            print(f"\n--- nothing at rows_path {ENDPOINT['rows_path']}. "
                  f"Top-level keys: {list(data.keys())}")
        return

    countries = a.countries.split(",") if a.countries else None
    harvest(Path(a.out), countries=countries,
            categories=CATEGORY_SETS[a.categories], resume=a.resume)


if __name__ == "__main__":
    main()
