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

from cifer import CATEGORIES, CATEGORY_SETS, CATEGORY_SPECIES, COUNTRY_CODES

URL = "https://ciferquery.singlewindow.cn"
CHECKPOINT = Path("work/cifer_checkpoint.json")
PAUSE = (1.2, 2.4)


# ---------------------------------------------------------------------------
# Page interaction
#
# Selectors are resolved by what the control *is* rather than by a generated
# class name, and every one of them has a fallback. A government portal rebuild
# will still break this -- when it does, run `probe --headed` and watch where it
# stops.
# ---------------------------------------------------------------------------

SEL = {
    "country": ["input[placeholder*='国家']", "input[placeholder*='Country']",
                ".country input", "#country"],
    "category": ["input[placeholder*='产品']", "input[placeholder*='Category']",
                 ".category input", "#productType"],
    "search": ["button:has-text('查询')", "button:has-text('Search')",
               ".search-btn", "button[type='submit']"],
    "rows": ["table tbody tr", ".el-table__body tbody tr", ".ant-table-tbody tr"],
    "next": ["button:has-text('下一页')", ".btn-next", "li.next",
             "button[aria-label='Next page']"],
    "total": [".el-pagination__total", ".total", "span:has-text('共')"],
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
    page = ctx.new_page()
    # The page fires `debugger` in a loop to frustrate inspection. Playwright
    # never attaches a debugger, so the statement is a no-op -- but neutralise
    # it anyway so a future variant that throws cannot stall the run.
    page.add_init_script("window.eval = new Proxy(window.eval, {apply:(t,s,a)=>"
                         "a[0] && /debugger/.test(String(a[0])) ? undefined : "
                         "Reflect.apply(t,s,a)});")
    page.goto(URL, wait_until="networkidle", timeout=90_000)
    return browser, ctx, page


def run_query(page, country_code: str | None, category_code: str | None):
    """Fill the form and search. Both fields are autocompletes, so the value has
    to be typed and then chosen from the dropdown rather than just set."""
    if country_code:
        box = _first(page, "country")
        box.click(); box.fill(""); box.type(country_code, delay=90)
        page.wait_for_timeout(900)
        page.keyboard.press("ArrowDown"); page.keyboard.press("Enter")

    if category_code:
        try:
            box = _first(page, "category", timeout=5000)
            box.click(); box.fill(""); box.type(category_code, delay=90)
            page.wait_for_timeout(900)
            page.keyboard.press("ArrowDown"); page.keyboard.press("Enter")
        except RuntimeError as exc:
            # Better to harvest the country unfiltered than to abort. The cost
            # is species: without the category filter the rows carry no species
            # at all, so the caller is told rather than left to assume.
            print(f"  category field not found, querying country only "
                  f"({exc.__class__.__name__})", file=sys.stderr)
            return False

    _first(page, "search").click()
    page.wait_for_timeout(2200)
    return True


def read_page(page) -> list[dict]:
    """Read the rendered table. Column order varies by locale, so columns are
    identified from the header text and the raw cells are kept alongside."""
    rows = _rows(page)
    if rows is None or rows.count() == 0:
        return []

    headers = [h.strip() for h in
               page.locator("table thead th, .el-table__header th").all_inner_texts()]

    def col(*needles):
        for i, h in enumerate(headers):
            low = h.lower()
            if any(n in low or n in h for n in needles):
                return i
        return None

    i_cn = col("china registration", "中国注册", "registration no")
    i_fo = col("overseas", "所在国家（地区）注册", "country register")
    i_nm = col("name", "企业名称")
    i_ct = col("country", "国家")
    i_ad = col("address", "地址")
    i_pd = col("product", "产品")
    i_vd = col("valid", "有效期")

    out = []
    for r in range(rows.count()):
        cells = [c.strip() for c in rows.nth(r).locator("td").all_inner_texts()]
        if not any(cells):
            continue
        pick = lambda i: cells[i] if i is not None and i < len(cells) else None
        out.append({
            "registerNo": pick(i_cn),
            "foreignRegisterNo": pick(i_fo),
            "enName": pick(i_nm),
            "countryCode": pick(i_ct),
            "address": pick(i_ad),
            "productCategory": [pick(i_pd)] if pick(i_pd) else [],
            "validDate": pick(i_vd),
            "_headers": headers,
            "_cells": cells,
        })
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
            page.wait_for_timeout(1800)
            return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------

def harvest(out_path: Path, countries: list[str], categories: list[str],
            resume: bool, max_pages: int, headed: bool):
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
            for country in countries:
                for cat in categories:
                    slice_id = f"{country}|{cat}"
                    if slice_id in state["done"]:
                        continue
                    label = CATEGORIES.get(cat, cat)
                    print(f"[{country} / {label}]", flush=True)

                    try:
                        page.goto(URL, wait_until="networkidle", timeout=90_000)
                        run_query(page, COUNTRY_CODES.get(country, country), cat)
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
                            r["_country_iso3"] = country
                            r["_category_code"] = cat
                            r["_species"] = CATEGORY_SPECIES.get(cat, [])
                            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                            got += 1
                            state["rows"] += 1
                        fh.flush()
                        pages += 1
                        print(f"  page {pages}: +{len(rows)} (slice {got:,})", flush=True)
                        if not next_page(page):
                            break
                        time.sleep(random.uniform(*PAUSE))

                    state["done"].append(slice_id)
                    CHECKPOINT.write_text(json.dumps(state))
                    time.sleep(random.uniform(*PAUSE))
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
    h.add_argument("--countries", default=None,
                   help="comma-separated ISO3; default is every country in the table")
    h.add_argument("--resume", action="store_true")
    h.add_argument("--max-pages", type=int, default=200)
    h.add_argument("--headed", action="store_true")

    a = p.parse_args()
    if a.cmd == "form":
        dump_form(a.headed)
    elif a.cmd == "probe":
        probe(a.country, a.category, a.headed)
    else:
        countries = (a.countries.split(",") if a.countries
                     else sorted(COUNTRY_CODES))
        harvest(Path(a.out), countries, CATEGORY_SETS[a.categories],
                a.resume, a.max_pages, a.headed)


if __name__ == "__main__":
    main()
