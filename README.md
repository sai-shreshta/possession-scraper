# Property Scrapers

Three tools in one folder:

| Tool | What it does |
|---|---|
| `possession_scraper.py` | For every building in a sheet (CSV or Excel), finds the **possession date**, the **price**, or the **RERA completion date / extension**, each with the source URL it came from. |
| `hyd_discovery.py` | Finds **Hyderabad projects not yet on NoBroker**: from the Telangana RERA register and from 99acres / MagicBricks / Housing / Square Yards. |
| `claude_verify.py` | Optional Claude review of unsure rows (not needed; kept for reference). |

## Which command do I run?

| I want... | Run |
|---|---|
| **Possession date + price for every row** (with source URLs) | [Full mode](#full-mode-possession--price) |
| Possession date only | `python possession_scraper.py "file.xlsx" --sheet-name "tab"` |
| RERA completion date, original vs revised, extension | `python possession_scraper.py "file.xlsx" --sheet-name "tab" --mode rera` |
| Hyderabad projects that aren't on NoBroker | [Hyderabad discovery](#hyderabad-projects-not-on-nobroker) |
| Just rewrite the Excel from what's been scraped so far | add `--export-only` to the same command |
| Try something on a few rows first | add `--limit 50` |

**Stop anytime with Ctrl+C. Re-run the same command and it continues where it stopped.** Searches already made are
cached, so a re-run never pays for the same search twice.

## Setup (once)

```
pip install -r requirements.txt
```

Create a `.env` file in this folder (see `.env.example`):

```
SERPER_API_KEY=your-serper-key
```

Serper.dev gives Google results and charges **1 credit per search**. When a key runs out, the run stops, saves,
beeps and prints `SEARCH CREDITS USED UP`. Put a new key in `.env` and re-run the same command.

**Keep the laptop plugged in with the lid open.** While a run is going, the scraper keeps the screen on so Windows
doesn't put the laptop into standby (Modern Standby laptops sleep when the screen turns off). Closing the lid still
sleeps it, unless you set *Power Options → Choose what closing the lid does → Plugged in: Do nothing*. If it does
sleep, nothing is lost: it continues when it wakes up.

---

## Full mode: possession + price

Gets the **possession date** and the **price** for each row, each with **one source URL** and the exact text quoted
from that page, so every value can be checked in one click.

### Step 1: free pass (0 credits)

```
python possession_scraper.py "C:\path\to\file.csv" --mode full --max-queries 0
```

Looks every project up in PropTiger's own project data, which is free. A match is accepted only when the name matches
and **either** the RERA number equals the sheet's, **or** it's within 3 km of the sheet's coordinates, **or** within
8 km with a near-exact name and the sheet's locality in the address. Requests go one at a time, spaced out, because
PropTiger blocks fast clients. If it starts refusing, the run pauses 10 minutes and tries again. After 6 pauses it
stops and saves, so rows are never wrongly marked "not found". Expect about 40% of possession dates and about 15% of
prices from this pass.

### Step 2: paid pass with a credit cap

```
python possession_scraper.py "C:\path\to\file.csv" --mode full --retry incomplete --budget 15000
```

- `--retry incomplete` redoes only rows still missing a confirmed possession date **or** price.
- `--budget 15000` stops after 15,000 paid searches and saves (`BUDGET REACHED`). To spend more later, run the same
  command again with a new budget, e.g. `--budget 5000`. Cached searches don't count toward the budget.
- **One search per project** (`--max-queries 1`, the default in full mode). Tested: one search finds almost all of
  what three searches find.
- **Order:** projects never searched before go first, then big-city projects before small towns, then rows missing a
  possession date before rows missing only a price.
- Rows that repeat a project (e.g. one row per floor plan) are searched once.
- Measured: about **0.9 credits per project**, possession confirmed for about 80% of searched projects, price for
  about 60%.

You can skip step 1 and run step 2 directly. It still tries PropTiger first for each project and only pays when that
doesn't confirm both values.

### Step 3 (optional): re-check finished rows after a rule change

```
python possession_scraper.py "C:\path\to\file.csv" --mode full --retry complete --budget 0
```

Re-scores rows already marked complete using the cached searches. This costs no credits.

### Output

The results go to `<file>_Results_sheet.xlsx` and `.csv` next to the input file, or to a `Results - <tab>` tab for
Excel input. They're re-written every 500 rows.

| Column | Meaning |
|---|---|
| Possession Date / (YYYY-MM) / Project Status | Confirmed date only (High or Medium confidence) |
| Possession Confidence | **High**: two trusted sites agree, or RERA / site data. **Medium**: one trusted site. **Low**: only unverified sites |
| Possession Source URL / Possession Evidence | The page the date came from and the exact text |
| Price, Price Min (Rs), Price Max (Rs), Price per sq.ft | Confirmed price only |
| Price Confidence / Price Source URL / Price Evidence | Same idea for the price |
| Sheet Completion Date, Web vs Sheet (date) | Compared with the sheet's `completionDate` / possession column |
| Sheet Price, Web vs Sheet (price) | Compared with `longMinPrice` / `longMaxPrice` (within 15% = Match) |
| Unconfirmed Possession / Price (check) + Source | Values seen **only** on unverified sites. They're kept apart so they're never mistaken for confirmed ones |
| Other Dates Seen / Other Prices Seen | Conflicting values with their site |

### How it avoids wrong sources

- **Never used:** nobroker.in (your own data), social media, YouTube, Quora, JustDial, Sulekha, Quikr, OLX, Wikipedia.
- **Rejected pages:** list pages ("new projects in X", "flats for sale in X"), search results, single-unit resale listings,
  builder pages, news, blog and update articles.
- **The page must be about this one project:** the project name has to open the page title.
- **Rejected if it looks like a different project:**
  - the page names a different city;
  - it quotes a different RERA number of the same format;
  - it's a generic name ("Sai Residency") and the page doesn't mention the locality.
- **Shown only as unconfirmed:**
  - the page names a different sector (Gurgaon / Noida);
  - the page's possession date is more than a year from the confirmed one;
  - the price range is very wide (top more than 5× the bottom), which suggests mixed listings;
  - the PropTiger price hasn't been updated in over 2 years.
- **Prices on a page** count only when they're the page's own price data, or the project's **full** name comes right
  before the amount with no "similar projects / nearby / you may also like" heading in between.
- **Confirmed (High/Medium)** only from a RERA site, a major portal (99acres, MagicBricks, Housing, Square Yards,
  PropTiger, Makaan and a few others), or the project's own website. Big portals are preferred for prices because
  smaller sites often still show launch-time prices.

---

## Possession mode and RERA mode

```
# Possession date + confidence
python possession_scraper.py "C:\path\book.xlsx" --sheet-name "inactive blank"

# RERA: registered completion date, original -> revised, extension, compared with the sheet's possession_date
python possession_scraper.py "C:\path\book.xlsx" --sheet-name "inactive >2yrs" --mode rera
```

- Each tab gets its own `Results - <tab>` and `Summary - <tab>` tabs and its own progress file, so runs never mix.
- Columns are auto-detected; the mapping is printed at start. Override with `--name-col`, `--locality-col`,
  `--city-col`, `--lat-col`, `--lon-col`, `--rera-col`, `--existing-col`.
- **City:** taken from the coordinates (nearest metro) when there's no city column, else from the address.
- **Several RERA IDs in one cell:** `ID1 | ID2` and `ID1 I ID2` both work, and a trailing "dated ..." is ignored.
- **Skipped:** PG listings. Bank-auction rows are searched by the building name.
- **RERA mode columns:** RERA Original Completion, RERA Current Completion, Extension / Revision, RERA vs Sheet,
  evidence and source.
- **Credits:** about 1.3 credits per row in possession mode and about 3.7 in RERA mode. `--max-queries` lowers this.
- **Excel:** close the file in Excel while it runs, otherwise results go to a separate file. A one-time
  `<file>.backup.xlsx` is made before the first write.

---

## Hyderabad projects not on NoBroker

Finds residential and commercial projects in the Hyderabad metro area (Hyderabad, Ranga Reddy, Medchal-Malkajgiri,
Sangareddy and Yadadri Bhuvanagiri districts) within 3 km of an existing NoBroker project, that aren't on NoBroker's
buy section yet. It needs `buildings.xlsx` (NoBroker's project list) in Downloads. Run the steps in this order:

| Step | Command | What it does | Cost |
|---|---|---|---|
| 1 | `python hyd_discovery.py rera-list` | Opens the Telangana RERA site in a browser. **You solve the captcha** for each district; it then pages through the list | free |
| 2 | `python hyd_discovery.py rera-details --workers 12` | Reads each project's RERA application/certificate PDF: type, address, PIN. Run it twice; the second run retries failures | free |
| 3 | `python hyd_discovery.py listings` | Google searches restricted to 99acres / MagicBricks / Housing / Square Yards, per locality | ~1 credit per search, ~2,800 searches |
| 4 | `python hyd_discovery.py enrich --only a` | Price and possession for RERA projects that weren't on the listing sites | 1 credit per project |
| 5 | `python hyd_discovery.py build` | Writes `Downloads\Hyderabad projects not on NoBroker.xlsx` | free |
| any | `python hyd_discovery.py status` | Shows how far each step got | free |

The main tab is **All new projects**: name, builder, city, location, district, PIN, RERA ID, type, price, possession,
where it was found, link, and the nearest NoBroker project with its distance. The optional `places` step (Google Maps)
and `enrich --only b` add a separate, less reliable "B - Maps" tab.

---

## Claude review (optional, not needed)

`--claude`, `--claude-only`, `--claude-dry-run`, `--claude-model`, `--claude-batch` and `--claude-budget` let Claude
review unsure rows using the evidence the scraper saved. Put `ANTHROPIC_API_KEY` in `.env` first. The cheapest setup is
`--claude-only --claude-model claude-haiku-4-5 --claude-batch`, at about $0.0015 per row.

## Useful flags

| Flag | Use |
|---|---|
| `--limit 50` | Trial run on the first 50 rows |
| `--workers 8` | Projects in parallel (default 6) |
| `--retry missing` / `low` / `incomplete` / `complete` | Redo not-found / low-confidence / not-fully-confirmed (full mode) / re-check finished rows |
| `--budget N` | Stop after N paid searches in this run |
| `--max-queries N` | Searches per project (full: 1, possession: 3, rera: 4; 0 = free sources only) |
| `--no-proptiger` | Full mode: skip the free PropTiger lookup |
| `--exclude-domains a.com,b.com` | Extra sites to ignore (nobroker.in is always ignored) |
| `--browser` | Headless Chrome for sites that block plain requests (`playwright install chromium` once) |
| `--export-only` | Just write the Excel from saved progress |
