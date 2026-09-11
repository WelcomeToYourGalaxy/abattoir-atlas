# Running this on GitHub — repo layout and setup

Everything runs as Actions workflows. You upload files through the GitHub web
interface and click "Run workflow"; nothing needs a terminal except one
ninety-second step for CIFER, described at the bottom.

## Where each file goes

Create a repository — `WelcomeToYourGalaxy/abattoir-atlas` — and put the files
at exactly these paths. The Python files sit at the repo root, not in a
subfolder; `run.py` resolves `raw/`, `work/` and `out/` relative to itself, so
moving them breaks the paths.

```
abattoir-atlas/
├── .github/
│   └── workflows/
│       ├── 1-selftest.yml
│       ├── 2-fetch.yml
│       ├── 3-parse-dedup.yml
│       ├── 4-geocode.yml
│       ├── 5-build-publish.yml
│       └── 6-cifer.yml
├── build_map.py
├── cifer.py
├── dedup.py
├── fetch.py
├── geocode.py
├── normalize.py
├── parse_all.sh
├── parsers.py
├── run.py
├── schema.py
├── sources.yml
├── .gitignore
├── README.md
├── REPO-SETUP.md          ← this file
├── raw/                   ← registry downloads land here
│   └── .gitkeep
├── work/                  ← the geocode cache lives here, committed by the bot
│   └── .gitkeep
├── out/                   ← dataset, review queue, report
│   └── smoketest_synthetic.html
└── docs/                  ← created by workflow 5; GitHub Pages serves this
```

To upload through the web interface: **Add file → Upload files**, drag the whole
set in at once. For the workflows, the folder path has to exist first — either
drag a folder that already contains `.github/workflows/`, or use **Create new
file** and type `.github/workflows/1-selftest.yml` in the name box, which
creates both folders as you type the slashes.

## Three settings to change before the first run

1. **Settings → Actions → General → Workflow permissions**
   Select **Read and write permissions**. Every workflow commits its results
   back to the repo, and without this they all fail at the push step.

2. **Settings → Pages → Build and deployment → Source**
   Select **GitHub Actions**. Workflow 5 deploys the map there.

3. **Settings → Secrets and variables → Actions → New repository secret**
   Name it `CONTACT`, value a real email address or `https://welcometoyourgalaxy.com`.
   Nominatim's usage policy requires a contact string in the User-Agent and they
   block requests without one. Nothing else needs a secret or an API key.

## Run order

Each is **Actions → pick the workflow → Run workflow**.

| | Workflow | Takes | What it does |
|---|---|---|---|
| 1 | Self test | 30 s | Proves the matcher and renderer work. Also runs automatically on every push. |
| 2 | Fetch registries | 5–20 min | Downloads FSIS and OpenStreetMap into `raw/` and commits them. Re-runs itself weekly. |
| 3 | Parse and deduplicate | 2–10 min | Reads everything in `raw/`, clusters it, commits `out/facilities.json.gz`, `review_queue.csv`, `dedup_report.md`. Runs automatically whenever `raw/` changes. |
| 4 | Geocode | hours, repeatedly | See below. |
| 5 | Build and publish | 5 min | Re-clusters with coordinates live, builds the map, publishes to Pages. Runs automatically after each geocode round. |
| 6 | Harvest CIFER | up to 5 h | Only after the discovery step below. |

Run 1, then 2, then 3, and you already have a working map of the US plus
whatever OSM holds — that's a real deliverable before any of the slow parts.

## Why geocoding is a loop, not a step

Nominatim allows one request per second. Sixty thousand addresses is roughly
seventeen hours. A GitHub Actions job is killed at six hours.

So workflow 4 does a slice — ten thousand fresh lookups, about three hours —
writes the cache to `work/geocache.json.gz`, and commits it. The next run loads
that cache and continues from where it stopped. It's on a six-hour cron, so once
you start it, it works through the backlog on its own over a day or two and then
stops: the workflow checks `run.py status` first and exits immediately when
nothing is outstanding.

The cache is the point. Those seventeen hours happen once, not once per rebuild.
Don't delete `work/geocache.json.gz`.

If you'd rather not wait, swap the provider in `geocode.py` for a paid bulk
geocoder — the `nominatim()` function is the only thing that has to change, and
`geocode_records(provider=...)` takes the replacement.

**Watch the free-tier budget.** Private repos get 2,000 Actions minutes a month;
a full geocode run burns most of that. Make the repo **public** and Actions
minutes are unlimited — which also suits the project, since the point is
publishing this.

## Sources that still need you

Three can't be automated from a runner, and `python fetch.py list` prints this
same summary.

**EU third-country lists** — per-country files behind an index at
`food.ec.europa.eu`. Download the meat sections (Annex III Sections I–IV),
convert each to CSV, upload into `raw/eu_traces_third_country/`. Column names
don't need to match anything; the parser resolves them by looking for candidates
and tells you the headers it found if it can't.

**EU member state lists** — one list per country, one layout per country.
Convert to CSV into `raw/eu_member_states/`. Start with DE, FR, ES, PL, IT, NL,
DK; that's most of the EU's throughput.

**CIFER** — the one step that needs a browser. Run `python cifer.py discover`
on your own machine and follow it: open the site, F12, capture the search
request, read off the endpoint path and parameter names. Edit the `ENDPOINT`
block at the top of `cifer.py` with what you saw, upload the edited file, then
workflow 6 runs it unattended with resume and checkpointing.

It's worth the ninety seconds. CIFER is what carries each plant's *home*
establishment number alongside the Chinese one, and that's the field that
collapses a Chinese registration onto the USDA or SIF record instead of
producing a second pin.

Anything you leave out simply isn't in the atlas — a visible gap rather than a
silent one.

## Where the map ends up

`https://welcometoyourgalaxy.github.io/abattoir-atlas/`

To embed it on the Weebly page, drop in an Embed Code element:

```html
<iframe src="https://welcometoyourgalaxy.github.io/abattoir-atlas/"
        style="width:100%;height:82vh;border:0" loading="lazy"
        title="Global abattoir atlas"></iframe>
```

The map is one self-contained file, around 6.7 MB at full scale and 1.7 MB over
the wire since Pages gzips. That loads fine directly; it's Weebly's own file
uploader that chokes on it, which is why it's hosted on Pages and framed in
rather than uploaded.

## Reviewing the merges

`out/review_queue.csv` holds every pair the matcher wouldn't decide on its own —
mostly identically-named plants with no usable address. Open it in a spreadsheet,
fill the `decision` column with `merge`, `distinct`, or `unsure`, and commit it
back. Nothing in it has been merged, so leaving it untouched is a safe default;
it just means those pairs stay as separate facilities.

## If a workflow fails

Click the failed run and read the step that's red. The two you'll hit first:

- **"no .csv links on the FSIS page"** — FSIS restructured their page. The
  fetcher deliberately refuses to guess at that point. Download the two CSVs by
  hand into `raw/` and skip workflow 2.
- **`HeaderError: none of (...) found. Headers present: [...]`** — a registry
  renamed a column. The message lists the actual headers; add the new name to
  the candidate list in `parsers.py`. This is a loud failure on purpose: a
  silent column shift would mislabel thousands of plants.
