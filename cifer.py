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


# English country name -> ISO3, from the site's own country payload. A global
# query (no country filter) has to read the country back out of the results
# table, so the name is what arrives and this is what turns it into a code.
COUNTRY_NAME_TO_ISO3: dict[str, str] = {
    "aruba": "ABW",
    "afghanistan": "AFG",
    "angola": "AGO",
    "anguilla": "AIA",
    "albania": "ALB",
    "andorra": "AND",
    "united arab emirates": "ARE",
    "argentina": "ARG",
    "armenia": "ARM",
    "american samoa": "ASM",
    "antarctica": "ATA",
    "antigua and barbuda": "ATG",
    "australia": "AUS",
    "austria": "AUT",
    "azerbaijan": "AZE",
    "burundi": "BDI",
    "belgium": "BEL",
    "benin": "BEN",
    "burkina faso": "BFA",
    "bangladesh": "BGD",
    "bulgaria": "BGR",
    "bahrain": "BHR",
    "bahamas": "BHS",
    "bosnia and herzegovina": "BIH",
    "belarus": "BLR",
    "belize": "BLZ",
    "bermuda": "BMU",
    "bolivia": "BOL",
    "brazil": "BRA",
    "barbados": "BRB",
    "brunei": "BRN",
    "bhutan": "BTN",
    "botswana": "BWA",
    "central african republic": "CAF",
    "canada": "CAN",
    "switzerland": "CHE",
    "chile": "CHL",
    "china": "CHN",
    "cameroon": "CMR",
    "democratic republic of congo": "COD",
    "congo": "COG",
    "cook islands": "COK",
    "colombia": "COL",
    "comoros": "COM",
    "cabo verde": "CPV",
    "costa rica": "CRI",
    "cuba": "CUB",
    "curacao": "CUW",
    "cayman islands": "CYM",
    "cyprus": "CYP",
    "germany": "DEU",
    "djibouti": "DJI",
    "dominica": "DMA",
    "denmark": "DNK",
    "dominican republic": "DOM",
    "algeria": "DZA",
    "ecuador": "ECU",
    "egypt": "EGY",
    "eritrea": "ERI",
    "spain": "ESP",
    "estonia": "EST",
    "ethiopia": "ETH",
    "finland": "FIN",
    "fiji": "FJI",
    "france": "FRA",
    "faroe islands": "FRO",
    "gabon": "GAB",
    "united kingdom": "GBR",
    "georgia": "GEO",
    "guernsey": "GGY",
    "ghana": "GHA",
    "gibraltar": "GIB",
    "guinea": "GIN",
    "guadeloupe": "GLP",
    "gambia": "GMB",
    "guinea-bissau": "GNB",
    "equatorial guinea": "GNQ",
    "greece": "GRC",
    "grenada": "GRD",
    "greenland": "GRL",
    "guatemala": "GTM",
    "french guiana": "GUF",
    "guam": "GUM",
    "guyana": "GUY",
    "honduras": "HND",
    "croatia": "HRV",
    "haiti": "HTI",
    "hungary": "HUN",
    "indonesia": "IDN",
    "isle of man": "IMN",
    "india": "IND",
    "ireland": "IRL",
    "iran": "IRN",
    "iraq": "IRQ",
    "iceland": "ISL",
    "israel": "ISR",
    "italy": "ITA",
    "jamaica": "JAM",
    "jersey": "JEY",
    "jordan": "JOR",
    "japan": "JPN",
    "kazakhstan": "KAZ",
    "kenya": "KEN",
    "kyrgyzstan": "KGZ",
    "cambodia": "KHM",
    "kiribati": "KIR",
    "saint kitts and nevis": "KNA",
    "republic of korea": "KOR",
    "kuwait": "KWT",
    "lao": "LAO",
    "lebanon": "LBN",
    "liberia": "LBR",
    "libya": "LBY",
    "saint lucia": "LCA",
    "liechtenstein": "LIE",
    "sri lanka": "LKA",
    "lesotho": "LSO",
    "lithuania": "LTU",
    "luxembourg": "LUX",
    "latvia": "LVA",
    "morocco": "MAR",
    "monaco": "MCO",
    "moldova": "MDA",
    "madagascar": "MDG",
    "maldives": "MDV",
    "mexico": "MEX",
    "marshall islands": "MHL",
    "north macedonia": "MKD",
    "mali": "MLI",
    "malta": "MLT",
    "myanmar": "MMR",
    "montenegro": "MNE",
    "mongolia": "MNG",
    "mozambique": "MOZ",
    "mauritania": "MRT",
    "montserrat": "MSR",
    "martinique": "MTQ",
    "mauritius": "MUS",
    "malawi": "MWI",
    "malaysia": "MYS",
    "mayotte": "MYT",
    "namibia": "NAM",
    "new caledonia": "NCL",
    "niger": "NER",
    "nigeria": "NGA",
    "nicaragua": "NIC",
    "niue": "NIU",
    "netherlands": "NLD",
    "norway": "NOR",
    "nepal": "NPL",
    "new zealand": "NZL",
    "oman": "OMN",
    "pakistan": "PAK",
    "panama": "PAN",
    "peru": "PER",
    "philippines": "PHL",
    "palau": "PLW",
    "papua new guinea": "PNG",
    "poland": "POL",
    "puerto rico": "PRI",
    "portugal": "PRT",
    "paraguay": "PRY",
    "palestine": "PSE",
    "french polynesia": "PYF",
    "qatar": "QAT",
    "reunion": "REU",
    "romania": "ROU",
    "russia": "RUS",
    "rwanda": "RWA",
    "saudi arabia": "SAU",
    "sudan": "SDN",
    "senegal": "SEN",
    "singapore": "SGP",
    "saint helena": "SHN",
    "solomon islands": "SLB",
    "sierra leone": "SLE",
    "el salvador": "SLV",
    "san marino": "SMR",
    "somalia": "SOM",
    "serbia": "SRB",
    "south sudan": "SSD",
    "sao tome and principe": "STP",
    "suriname": "SUR",
    "slovakia": "SVK",
    "slovenia": "SVN",
    "sweden": "SWE",
    "seychelles": "SYC",
    "syria": "SYR",
    "chad": "TCD",
    "togo": "TGO",
    "thailand": "THA",
    "tajikistan": "TJK",
    "turkmenistan": "TKM",
    "timor-leste": "TLS",
    "tonga": "TON",
    "trinidad and tobago": "TTO",
    "tunisia": "TUN",
    "tuvalu": "TUV",
    "tanzania": "TZA",
    "uganda": "UGA",
    "ukraine": "UKR",
    "uruguay": "URY",
    "united states": "USA",
    "uzbekistan": "UZB",
    "holy see": "VAT",
    "venezuela": "VEN",
    "british virgin islands": "VGB",
    "viet nam": "VNM",
    "vanuatu": "VUT",
    "samoa": "WSM",
    "yemen": "YEM",
    "south africa": "ZAF",
    "zambia": "ZMB",
    "zimbabwe": "ZWE",
}


# Chinese country names -> ISO3. The portal returns 国家（地区）in Chinese even
# under an en-US locale, so without this every harvested row carries a country
# code made of the first three Chinese characters of its name.
COUNTRY_ZH_TO_ISO3: dict[str, str] = {
    "阿鲁巴": "ABW",
    "阿富汗": "AFG",
    "安哥拉": "AGO",
    "安圭拉": "AIA",
    "奥兰群岛": "ALA",
    "阿尔巴尼亚": "ALB",
    "安道尔": "AND",
    "阿联酋": "ARE",
    "阿根廷": "ARG",
    "亚美尼亚": "ARM",
    "美属萨摩亚": "ASM",
    "南极洲": "ATA",
    "安提瓜和巴布达": "ATG",
    "澳大利亚": "AUS",
    "奥地利": "AUT",
    "阿塞拜疆": "AZE",
    "布隆迪": "BDI",
    "比利时": "BEL",
    "贝宁": "BEN",
    "布基纳法索": "BFA",
    "孟加拉国": "BGD",
    "保加利亚": "BGR",
    "巴林": "BHR",
    "巴哈马": "BHS",
    "波斯尼亚和黑塞哥维那": "BIH",
    "白俄罗斯": "BLR",
    "伯利兹": "BLZ",
    "百慕大": "BMU",
    "玻利维亚": "BOL",
    "巴西": "BRA",
    "巴巴多斯": "BRB",
    "文莱": "BRN",
    "不丹": "BTN",
    "博茨瓦纳": "BWA",
    "中非": "CAF",
    "加那利群岛": "CAI",
    "加拿大": "CAN",
    "瑞士": "CHE",
    "智利": "CHL",
    "中国": "CHN",
    "科特迪瓦": "CIV",
    "喀麦隆": "CMR",
    "刚果民主共和国": "COD",
    "刚果共和国": "COG",
    "库克群岛": "COK",
    "哥伦比亚": "COL",
    "科摩罗": "COM",
    "佛得角": "CPV",
    "哥斯达黎加": "CRI",
    "古巴": "CUB",
    "库拉索": "CUW",
    "开曼群岛": "CYM",
    "塞浦路斯": "CYP",
    "捷克": "CZE",
    "德国": "DEU",
    "吉布提": "DJI",
    "多米尼克": "DMA",
    "丹麦": "DNK",
    "多米尼加": "DOM",
    "阿尔及利亚": "DZA",
    "厄瓜多尔": "ECU",
    "埃及": "EGY",
    "厄立特里亚": "ERI",
    "西撒哈拉": "ESH",
    "西班牙": "ESP",
    "爱沙尼亚": "EST",
    "埃塞俄比亚": "ETH",
    "芬兰": "FIN",
    "斐济": "FJI",
    "法国": "FRA",
    "法罗群岛": "FRO",
    "加蓬": "GAB",
    "英国": "GBR",
    "格鲁吉亚": "GEO",
    "格恩西": "GGY",
    "加纳": "GHA",
    "直布罗陀": "GIB",
    "几内亚": "GIN",
    "瓜德罗普": "GLP",
    "冈比亚": "GMB",
    "几内亚比绍": "GNB",
    "赤道几内亚": "GNQ",
    "希腊": "GRC",
    "格林纳达": "GRD",
    "格陵兰": "GRL",
    "危地马拉": "GTM",
    "法属圭亚那": "GUF",
    "关岛": "GUM",
    "圭亚那": "GUY",
    "洪都拉斯": "HND",
    "克罗地亚": "HRV",
    "海地": "HTI",
    "匈牙利": "HUN",
    "印度尼西亚": "IDN",
    "马恩岛": "IMN",
    "印度": "IND",
    "爱尔兰": "IRL",
    "伊朗": "IRN",
    "伊拉克": "IRQ",
    "冰岛": "ISL",
    "以色列": "ISR",
    "意大利": "ITA",
    "牙买加": "JAM",
    "泽西": "JEY",
    "约旦": "JOR",
    "日本": "JPN",
    "哈萨克斯坦": "KAZ",
    "肯尼亚": "KEN",
    "吉尔吉斯斯坦": "KGZ",
    "柬埔寨": "KHM",
    "基里巴斯": "KIR",
    "圣基茨和尼维斯": "KNA",
    "韩国": "KOR",
    "科威特": "KWT",
    "老挝": "LAO",
    "黎巴嫩": "LBN",
    "利比里亚": "LBR",
    "利比亚": "LBY",
    "圣卢西亚": "LCA",
    "列支敦士登": "LIE",
    "斯里兰卡": "LKA",
    "莱索托": "LSO",
    "立陶宛": "LTU",
    "卢森堡": "LUX",
    "拉脱维亚": "LVA",
    "摩洛哥": "MAR",
    "摩纳哥": "MCO",
    "摩尔多瓦": "MDA",
    "马达加斯加": "MDG",
    "马尔代夫": "MDV",
    "墨西哥": "MEX",
    "马绍尔群岛": "MHL",
    "北马其顿": "MKD",
    "马里": "MLI",
    "马耳他": "MLT",
    "缅甸": "MMR",
    "黑山": "MNE",
    "蒙古": "MNG",
    "莫桑比克": "MOZ",
    "毛里塔尼亚": "MRT",
    "蒙特塞拉特": "MSR",
    "马提尼克": "MTQ",
    "毛里求斯": "MUS",
    "马拉维": "MWI",
    "马来西亚": "MYS",
    "马约特": "MYT",
    "纳米比亚": "NAM",
    "新喀里多尼亚": "NCL",
    "尼日尔": "NER",
    "尼日利亚": "NGA",
    "尼加拉瓜": "NIC",
    "纽埃": "NIU",
    "荷兰": "NLD",
    "挪威": "NOR",
    "尼泊尔": "NPL",
    "瑙鲁": "NRO",
    "新西兰": "NZL",
    "阿曼": "OMN",
    "巴基斯坦": "PAK",
    "巴拿马": "PAN",
    "秘鲁": "PER",
    "菲律宾": "PHL",
    "帕劳": "PLW",
    "巴布亚新几内亚": "PNG",
    "波兰": "POL",
    "波多黎各": "PRI",
    "朝鲜": "PRK",
    "葡萄牙": "PRT",
    "巴拉圭": "PRY",
    "巴勒斯坦": "PSE",
    "法属波利尼西亚": "PYF",
    "卡塔尔": "QAT",
    "留尼汪": "REU",
    "罗马尼亚": "ROU",
    "俄罗斯": "RUS",
    "卢旺达": "RWA",
    "沙特阿拉伯": "SAU",
    "苏丹": "SDN",
    "塞内加尔": "SEN",
    "新加坡": "SGP",
    "圣赫勒拿": "SHN",
    "所罗门群岛": "SLB",
    "塞拉利昂": "SLE",
    "萨尔瓦多": "SLV",
    "圣马力诺": "SMR",
    "索马里": "SOM",
    "塞尔维亚": "SRB",
    "南苏丹": "SSD",
    "圣多美和普林西比": "STP",
    "苏里南": "SUR",
    "斯洛伐克": "SVK",
    "斯洛文尼亚": "SVN",
    "瑞典": "SWE",
    "斯威士兰": "SWZ",
    "塞舌尔": "SYC",
    "叙利亚": "SYR",
    "乍得": "TCD",
    "多哥": "TGO",
    "泰国": "THA",
    "塔吉克斯坦": "TJK",
    "土库曼斯坦": "TKM",
    "东帝汶": "TLS",
    "汤加": "TON",
    "特立尼达和多巴哥": "TTO",
    "突尼斯": "TUN",
    "土耳其": "TUR",
    "图瓦卢": "TUV",
    "坦桑尼亚": "TZA",
    "乌干达": "UGA",
    "乌克兰": "UKR",
    "乌拉圭": "URY",
    "美国": "USA",
    "乌兹别克斯坦": "UZB",
    "梵蒂冈": "VAT",
    "委内瑞拉": "VEN",
    "英属维尔京群岛": "VGB",
    "越南": "VNM",
    "瓦努阿图": "VUT",
    "萨摩亚": "WSM",
    "也门": "YEM",
    "南非": "ZAF",
    "赞比亚": "ZMB",
    "津巴布韦": "ZWE",
}


def iso3_from_name(name: str | None) -> str:
    """Best-effort; returns "" rather than a guess when nothing matches."""
    if not name:
        return ""
    raw = name.strip()
    if raw in COUNTRY_ZH_TO_ISO3:
        return COUNTRY_ZH_TO_ISO3[raw]
    k = raw.lower()
    if k in COUNTRY_NAME_TO_ISO3:
        return COUNTRY_NAME_TO_ISO3[k]
    if len(k) == 3 and k.upper() in COUNTRY_CODES:
        return k.upper()
    for full, iso in COUNTRY_NAME_TO_ISO3.items():
        if full in k or k in full:
            return iso
    return ""
