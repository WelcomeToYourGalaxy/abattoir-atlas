#!/usr/bin/env python3
"""
CIFER harvest by driving the site's own browser.

Why this and not an HTTP client
-------------------------------
CIFER returns its result rows as a base64 blob encrypted with a modified SM4 --
the S-box differs from GB/T 32907-2016 in two positions and the data round
swaps two output bytes, so no standard library decrypts it. The two keys in the
page bundle do not decrypt a captured response either, which means the key is
issued per session. Reimplementing that is a losing race against whatever they
change next.

So don't. Load the real page, let it decrypt its own responses, and read the
table it renders. The crypto becomes somebody else's problem, and the
anti-debugger script that breaks devtools is irrelevant here because there are
no devtools to detect.

Setup (once, and the workflow does it for you):
    pip install playwright
    playwright install chromium

    python cifer_browser.py probe   --country USA --category 0102
    python cifer_browser.py harvest --categories meat --out raw/cifer.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

from cifer import (CATEGORIES, CATEGORY_SETS, CATEGORY_SPECIES,
                   COUNTRY_CODES, iso3_from_name)

URL = "https://ciferquery.singlewindow.cn"
CHECKPOINT = Path("work/cifer_checkpoint.json")
PAUSE = (1.2, 2.4)   # overridden by --pause


# ---------------------------------------------------------------------------
# Page interaction
#
# Selectors are resolved by what the control *is* rather than by a generated
# class name, and every one of them has a fallback. A government portal rebuild
# will still break this -- when it does, run `probe --headed` and watch where it
# stops.
# ---------------------------------------------------------------------------

# Real element IDs, read off the page's own DOM via `python cifer_browser.py
# form`. Not guessed -- and worth noting why guessing failed: the country box
# carries a generic placeholder, while #orgNo (overseas registration number)
# is the field whose placeholder contains 国家. A placeholder match for
# "country" hits the wrong input and silently searches on nonsense.
SEL = {
    "category": ["#registerTypeName", "input[name='registerTypeName']"],
    "country":  ["#countryName", "input[name='countryName']"],
    "orgno":    ["#orgNo"],
    "status":   ["#status", "select[name='status']"],
    "search":   ["button:has-text('查询')", ".btn-primary:has-text('查询')",
                 "button.btn-primary"],
    "rows":     ["table tbody tr", ".table tbody tr", "#dataTable tbody tr"],
    "next":     ["a:has-text('下一页')", "button:has-text('下一页')",
                 ".pagination li:not(.disabled) a:has-text('»')",
                 ".pagination .next:not(.disabled) a", "a[rel='next']"],
    # The autocompleters are the jQuery "autocompleter" plugin: type, press
    # space to fire the lookup, then pick from the rendered list.
    "ac_item":  [".autocompleter li", ".autocompleter-item",
                 "ul.autocompleter-list li", ".autocompleter-hint li"],
}


def _first(page, keys, timeout=8000):
    """Try each candidate selector, return the first that resolves."""
    last = None
    for sel in SEL[keys]:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=timeout)
            return el
        except Exception as exc:
            last = exc
    raise RuntimeError(f"no selector matched for '{keys}'. Tried {SEL[keys]}. "
                       f"Last error: {last}")


def _rows(page):
    for sel in SEL["rows"]:
        loc = page.locator(sel)
        if loc.count():
            return loc
    return None


def fill_autocomplete(page, field: str, value: str) -> bool:
    """Fill one of the two autocompleter boxes.

    The placeholder on both reads "press space to search, fuzzy matching
    supported", so the space keypress is what fires the lookup -- typing alone
    leaves the dropdown closed and the underlying hidden value unset, which is
    the difference between a filtered query and a silently unfiltered one.
    """
    box = _first(page, field, timeout=10000)
    box.click()
    box.fill("")
    box.type(value, delay=60)
    page.keyboard.press("Space")
    page.wait_for_timeout(800)

    for sel in SEL["ac_item"]:
        items = page.locator(sel)
        try:
            if items.count() and items.first.is_visible():
                items.first.click()
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue

    # No list rendered. Fall back to keyboard selection, then verify the box
    # actually holds something -- an empty box means the filter did not apply.
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    return bool((box.input_value() or "").strip())


def open_site(pw, headed: bool = False):
    browser = pw.chromium.launch(
        headless=not headed,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx = browser.new_context(
        locale="en-US",
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"),
        viewport={"width": 1440, "height": 900},
    )
    # Images, fonts, stylesheets and analytics are pure latency for a table
    # scrape. Blocking them roughly halves each page load.
    ctx.route("**/*", lambda route: (
        route.abort() if route.request.resource_type in
        ("image", "font", "media", "stylesheet") else route.continue_()))

    page = ctx.new_page()
    # The page fires `debugger` in a loop to frustrate inspection. Playwright
    # never attaches a debugger, so the statement is a no-op -- but neutralise
    # it anyway so a future variant that throws cannot stall the run.
    page.add_init_script("window.eval = new Proxy(window.eval, {apply:(t,s,a)=>"
                         "a[0] && /debugger/.test(String(a[0])) ? undefined : "
                         "Reflect.apply(t,s,a)});")
    page.goto(URL, wait_until="networkidle", timeout=90_000)
    return browser, ctx, page


def run_query(page, country_code: str | None, category_code: str | None) -> bool:
    """Fill the form and search. Returns whether the category filter applied."""
    # Status: include suspended registrations as well as active ones. Which
    # plants to count is your call, not the scraper's, so it does not narrow
    # the set here.
    try:
        page.select_option(SEL["status"][0], "ALL")
    except Exception:
        pass

    if country_code:
        if not fill_autocomplete(page, "country", country_code):
            raise RuntimeError(f"country {country_code!r} did not resolve in the "
                               f"autocompleter")

    applied = True
    if category_code:
        applied = fill_autocomplete(page, "category", category_code)
        if not applied:
            print(f"  category {category_code} did not resolve; querying the "
                  f"country unfiltered", file=sys.stderr)

    _first(page, "search").click()
    page.wait_for_timeout(1600)
    set_page_size(page)
    return applied


def set_page_size(page, want: int = 50) -> None:
    """Raise rows-per-page from the default 10. Best effort: if the control is
    not there the harvest just pages more often."""
    try:
        tog = page.locator(".dropdown-toggle:has-text('10')").first
        if tog.count() == 0:
            return
        tog.click()
        page.wait_for_timeout(400)
        for n in (str(want), "100", "50", "20"):
            opt = page.locator(f".dropdown-menu li:has-text('{n}'), "
                               f".dropdown-menu a:has-text('{n}')").first
            if opt.count():
                opt.click()
                page.wait_for_timeout(1600)
                return
    except Exception:
        pass


def read_page(page) -> list[dict]:
    """Read the results table.

    The page stacks several tables -- results, import-suspension history, HS
    codes -- and an unscoped header scan concatenates all of their headers, so
    column indices land in the wrong table. Each table is scored on how many of
    the expected result headers it carries and only the winner is read.
    """
    tables = page.locator("table")
    best, best_score, best_headers = None, 0, []

    for i in range(min(tables.count(), 12)):
        tbl = tables.nth(i)
        try:
            hdrs = [h.strip() for h in tbl.locator("thead th, tr:first-child th")
                    .all_inner_texts()]
        except Exception:
            continue
        if not hdrs:
            continue
        joined = " ".join(hdrs).lower()
        score = sum(k in joined for k in
                    ("企业名称", "name", "在华注册编号", "china reg",
                     "所在国家", "country", "生产场所地址", "address"))
        if score > best_score:
            best, best_score, best_headers = tbl, score, hdrs

    if best is None or best_score < 3:
        return []

    rows = best.locator("tbody tr")
    if rows.count() == 0:
        rows = best.locator("tr")

    def col(*needles):
        for i, h in enumerate(best_headers):
            low = h.lower()
            if any(n in low or n in h for n in needles):
                return i
        return None

    # Header cells carry the Chinese label, a newline, then the English one.
    i_cn = col("china reg", "在华注册编号")
    i_fo = col("overseas reg", "所在国家（地区）注册编号")
    i_nm = col("企业名称", "name")
    i_ct = col("country(region)", "国家（地区）")
    i_ad = col("生产场所地址", "address")
    i_pd = col("产品类别", "category")
    i_vd = col("exp. date", "注册有效期", "state", "状态")

    out = []
    for r in range(rows.count()):
        cells = [c.strip() for c in rows.nth(r).locator("td").all_inner_texts()]
        if not any(cells):
            continue
        pick = lambda i: (cells[i] if i is not None and i < len(cells) else None) or None
        rec = {
            "registerNo": pick(i_cn),
            "foreignRegisterNo": pick(i_fo),
            "enName": pick(i_nm),
            "countryCode": pick(i_ct),
            "address": pick(i_ad),
            "productCategory": [pick(i_pd)] if pick(i_pd) else [],
            "validDate": pick(i_vd),
            "_headers": best_headers,
            "_cells": cells,
        }
        # A row with no identifier at all means the column scan is still wrong;
        # keep it, but the caller can count them and notice.
        out.append(rec)
    return out


def next_page(page) -> bool:
    for sel in SEL["next"]:
        btn = page.locator(sel).first
        try:
            if btn.count() == 0:
                continue
            cls = (btn.get_attribute("class") or "") + (btn.get_attribute("aria-disabled") or "")
            if "disabled" in cls or btn.is_disabled():
                return False
            btn.click()
            page.wait_for_timeout(1100)
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------

def harvest(out_path: Path, countries: list[str] | None, categories: list[str],
            resume: bool, max_pages: int, headed: bool, pause: float = 0.8):
    """countries=None means a global sweep: query each category with no country
    filter, which is twelve queries instead of twelve times a hundred and
    seventeen. The country then comes out of the results table rather than
    going into the form."""
    from playwright.sync_api import sync_playwright

    out_path.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)

    state = {"done": [], "rows": 0}
    if resume and CHECKPOINT.exists():
        state = json.loads(CHECKPOINT.read_text())
        print(f"resuming: {len(state['done'])} slices done, {state['rows']:,} rows")

    seen: set[str] = set()
    if resume and out_path.exists():
        for line in open(out_path, encoding="utf-8"):
            try:
                seen.add(str(json.loads(line).get("registerNo")))
            except Exception:
                pass

    with sync_playwright() as pw:
        browser, ctx, page = open_site(pw, headed)
        mode = "a" if resume and out_path.exists() else "w"
        fh = open(out_path, mode, encoding="utf-8")
        try:
            for country in (countries if countries is not None else [None]):
                for cat in categories:
                    slice_id = f"{country or 'GLOBAL'}|{cat}"
                    if slice_id in state["done"]:
                        continue
                    label = CATEGORIES.get(cat, cat)
                    print(f"[{country or 'all countries'} / {label}]", flush=True)

                    try:
                        page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
                        page.wait_for_timeout(1200)
                        applied = run_query(
                            page,
                            COUNTRY_CODES.get(country, country) if country else None,
                            cat)
                        if not applied:
                            print(f"  !! category {cat} did not apply -- these "
                                  f"rows are NOT species-tagged and may not be "
                                  f"meat at all", file=sys.stderr)
                    except Exception as exc:
                        print(f"  query failed: {exc}", file=sys.stderr)
                        continue

                    got, pages = 0, 0
                    while pages < max_pages:
                        rows = read_page(page)
                        if not rows:
                            break
                        for r in rows:
                            key = str(r.get("registerNo"))
                            if key and key in seen:
                                continue
                            seen.add(key)
                            r["_country_iso3"] = country or iso3_from_name(
                                r.get("countryCode"))
                            r["_category_code"] = cat
                            r["_species"] = CATEGORY_SPECIES.get(cat, []) if applied else []
                            r["_category_applied"] = applied
                            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                            got += 1
                            state["rows"] += 1
                        fh.flush()
                        pages += 1
                        noid = sum(1 for r in rows if not r.get("registerNo")
                                   and not r.get("foreignRegisterNo"))
                        print(f"  page {pages}: +{len(rows)} (slice {got:,})"
                              + (f"  [{noid} rows with no registration number -- "
                                 f"column scan may be off]" if noid else ""),
                              flush=True)
                        if not next_page(page):
                            break
                        time.sleep(pause * random.uniform(0.7, 1.3))

                    state["done"].append(slice_id)
                    CHECKPOINT.write_text(json.dumps(state))
                    time.sleep(pause)
        finally:
            fh.close()
            ctx.close(); browser.close()

    print(f"\n{state['rows']:,} rows in {out_path}")
    print(f"next: python run.py parse --source cifer_china --file {out_path.name}")


FORM_JS = """() => {
  const out = {inputs: [], selects: [], buttons: [], labels: []};
  const near = el => {
    let n = el.closest('.el-form-item, .form-item, label, td, div');
    return n ? (n.innerText || '').trim().slice(0, 80) : '';
  };
  document.querySelectorAll('input, [role=combobox], [contenteditable=true]')
    .forEach(el => out.inputs.push({
      tag: el.tagName, type: el.type || '', id: el.id || '',
      name: el.name || '', cls: (el.className || '').toString().slice(0, 90),
      placeholder: el.placeholder || '',
      aria: el.getAttribute('aria-label') || '',
      visible: !!(el.offsetWidth || el.offsetHeight),
      context: near(el)
    }));
  document.querySelectorAll('select').forEach(el => out.selects.push({
    id: el.id, name: el.name, cls: (el.className||'').toString().slice(0,90),
    options: [...el.options].slice(0, 12).map(o => o.value + '|' + o.text)
  }));
  document.querySelectorAll('button, .el-button, [role=button]').forEach(el => {
    const txt = (el.innerText || '').trim();
    if (txt) out.buttons.push({text: txt.slice(0, 40),
                               cls: (el.className||'').toString().slice(0, 70)});
  });
  document.querySelectorAll('label, .el-form-item__label').forEach(el => {
    const txt = (el.innerText || '').trim();
    if (txt) out.labels.push(txt.slice(0, 60));
  });
  return out;
}"""


def dump_form(headed: bool = False):
    """Print every form control on the page.

    This exists because guessing selectors against a portal you cannot open in
    devtools is a waste of runs. One of these tells you the real answer.
    """
    from playwright.sync_api import sync_playwright
    Path("work").mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser, ctx, page = open_site(pw, headed)
        try:
            page.wait_for_timeout(3000)
            info = page.evaluate(FORM_JS)

            print("=== visible inputs ===")
            for i in info["inputs"]:
                if not i["visible"]:
                    continue
                print(f"  <{i['tag']} type={i['type']!r} id={i['id']!r} "
                      f"name={i['name']!r}")
                print(f"     placeholder={i['placeholder']!r} aria={i['aria']!r}")
                print(f"     class={i['cls']!r}")
                print(f"     context={i['context']!r}")
            hidden = sum(1 for i in info["inputs"] if not i["visible"])
            print(f"\n({hidden} hidden inputs not shown)")

            print("\n=== selects ===")
            for s in info["selects"]:
                print(f"  {s}")

            print("\n=== buttons ===")
            for b in info["buttons"][:25]:
                print(f"  {b['text']!r}  class={b['cls']!r}")

            print("\n=== labels ===")
            print("  " + " | ".join(dict.fromkeys(info["labels"]))[:1200])

            page.screenshot(path="work/cifer_form.png", full_page=True)
            Path("work/cifer_form.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
            print("\nwrote work/cifer_form.png and work/cifer_form.json")
        finally:
            ctx.close(); browser.close()


def probe(country: str, category: str, headed: bool):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, ctx, page = open_site(pw, headed)
        Path("work").mkdir(exist_ok=True)
        try:
            filtered = run_query(page, COUNTRY_CODES.get(country, country), category)
            if not filtered:
                print("note: category filter was not applied")
            rows = read_page(page)
            page.screenshot(path="work/cifer_probe.png", full_page=True)
            print(f"{len(rows)} rows on page 1\n")
            if rows:
                print("headers:", rows[0]["_headers"])
                print("\nfirst row mapped:")
                print(json.dumps({k: v for k, v in rows[0].items()
                                  if not k.startswith("_")},
                                 ensure_ascii=False, indent=2))
                print("\nraw cells:", rows[0]["_cells"])
            else:
                print("no rows found. Re-run with --headed and watch the page.")
                page.screenshot(path="work/cifer_probe.png", full_page=True)
                print("screenshot: work/cifer_probe.png")
        finally:
            ctx.close(); browser.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    fd = sub.add_parser("form", help="dump every form control on the page")
    fd.add_argument("--headed", action="store_true")

    pr = sub.add_parser("probe")
    pr.add_argument("--country", default="USA")
    pr.add_argument("--category", default="0102")
    pr.add_argument("--headed", action="store_true")

    h = sub.add_parser("harvest")
    h.add_argument("--out", default="raw/cifer.jsonl")
    h.add_argument("--categories", default="meat", choices=list(CATEGORY_SETS))
    h.add_argument("--scope", default="global", choices=["global", "country"],
                   help="global: one query per category, country read from the "
                        "results (fast). country: one query per country per "
                        "category (slow, but shardable)")
    h.add_argument("--countries", default=None,
                   help="comma-separated ISO3; only used with --scope country")
    h.add_argument("--pause", type=float, default=0.8,
                   help="seconds between page clicks")
    h.add_argument("--resume", action="store_true")
    h.add_argument("--max-pages", type=int, default=200)
    h.add_argument("--headed", action="store_true")

    a = p.parse_args()
    if a.cmd == "form":
        dump_form(a.headed)
    elif a.cmd == "probe":
        probe(a.country, a.category, a.headed)
    else:
        if a.scope == "global":
            countries = None
        else:
            countries = (a.countries.split(",") if a.countries
                         else sorted(COUNTRY_CODES))
        harvest(Path(a.out), countries, CATEGORY_SETS[a.categories],
                a.resume, a.max_pages, a.headed, a.pause)


if __name__ == "__main__":
    main()
