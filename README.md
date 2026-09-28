# Possession Date Scraper

Reads a sheet of buildings (name, locality, city, lat, long), searches the web for each building's
possession date, and writes a **Possession Dates** tab (plus a **Possession Summary** tab) back into the same workbook.

## Run

```
pip install -r requirements.txt
python possession_scraper.py "C:\path\to\buildings.xlsx"
```

Columns are auto-detected (it prints the mapping at start). Override if needed:
`--name-col "Project Name" --locality-col Area --city-col City --lat-col Lat --lon-col Lng`.
Pick a tab with `--sheet-name`. Do a trial first: `--limit 50`.

**Stop anytime (Ctrl+C) and re-run the same command to resume.** Progress lives in
`<sheet>.possession_cache.sqlite` next to your file. The sheet is re-written every 500 rows and at the end.

- Close the file in Excel while it runs. If it's open, results go to `<sheet>_possession_results.xlsx` instead.
- A one-time backup `<sheet>.backup.xlsx` is made before the first write. openpyxl drops charts and images when it re-saves a workbook.
- A CSV copy is always written: `<sheet>_possession_results.csv`.

## Output columns

| Column | Meaning |
|---|---|
| Possession Date | Best date found (e.g. `Dec 2025`), or `Not found` |
| Possession (YYYY-MM) | Same date, sortable |
| Project Status | Ready to Move / Under Construction (from the date) |
| Confidence | **High**: multiple sites agree, or RERA or site data. **Medium**: likely. **Low**: weak, verify manually |
| Sources Agreeing | Number of different websites giving that date |
| RERA Date | RERA or official completion date, if seen (often later than the builder's date) |
| Best Source / Evidence | URL and the exact text the date came from |
| Other Dates Seen | Conflicting dates (phases or towers often differ) |

### Comparing with dates you already have

If the sheet has a `possession_date` column (or pass `--existing-col "Column Name"`), three more columns are added:
**Sheet Possession Date**, **Web vs Sheet** (Match / Close (within 3 months) / Web is N months later or earlier /
No web date / No sheet date), and **Difference (months)**. It compares against the Final date when Claude reviewed the
row, otherwise the scraper's date. The totals appear on the Possession Summary tab.

## Speed and scale

With free engines only, expect about 10–20 buildings/min, so 25k rows is roughly 1–2 days of running. Engines that
rate-limit are paused and retried automatically. For much faster and more reliable runs, add a search API key in `.env`
(see `.env.example`). Serper.dev (Google results) works best. About 2–3 searches per building means roughly 60k queries for 25k rows.

## Two modes, one tab at a time

```
# Possession date + confidence
python possession_scraper.py "C:\path\book.xlsx" --sheet-name "inactive blank" --exclude-domains nobroker.in

# RERA check: registered completion date, original -> revised, extension, compared with your possession_date
python possession_scraper.py "C:\path\book.xlsx" --sheet-name "inactive >2yrs" --mode rera --exclude-domains nobroker.in
```

- Each tab gets its own `Results - <tab>` and `Summary - <tab>` tabs, and its own progress file, so the two runs never mix.
- The city is taken from the address when there's no city column. Coordinates like `77.38° E` are fine.
  Several RERA IDs in one cell (`ID1 | ID2`) are each searched.
- RERA mode columns: **RERA Original Completion**, **RERA Current Completion**, **Extension / Revision**
  ("Yes - extended X -> Y" or "No - revised date same as original"), **RERA vs Sheet**, the evidence and source.
  Pages that quote the project's RERA ID are preferred over name matches.

### Search credits and resuming

With `SERPER_API_KEY` set, only Serper is used. When the key runs out of credits (or is rejected), the run **stops,
saves, beeps and prints `SEARCH CREDITS USED UP`**. Put a new key in `.env` and run the same command again: it continues
where it stopped, and searches already made are reused without spending credits.
Measured usage: about 1.3 credits per row in possession mode, about 3.7 in RERA mode (`--max-queries 3` lowers this).
Add `--fallback-free` to switch to the free engines instead of stopping.

## Claude review (optional second pass)

Claude reads the evidence the scraper collected for rows that aren't High confidence and decides the right date.
It catches same-name projects elsewhere, phase/tower splits, and RERA-vs-builder dates.

1. Put `ANTHROPIC_API_KEY=sk-ant-...` in a `.env` file in this folder (see `.env.example`).
2. Preview the prompt and cost without spending anything:
   `python possession_scraper.py "C:\path\to\sheet.xlsx" --claude-only --claude-dry-run`
3. Test on 20 rows: `... --claude-only --claude-limit 20`
4. Full review: `... --claude-only --claude-budget 150` (or add `--claude` to a scraping run to review right after).

Adds these columns: Claude Date / Confidence / Status / Phase Note / Reasoning / Source, plus **Final Possession Date**
(Claude's answer where it reviewed the row, otherwise the scraper's).

| Flag | Default | |
|---|---|---|
| `--claude-model` | `claude-opus-5` | `claude-sonnet-5` is ~2.5x cheaper, `claude-haiku-4-5` ~5x cheaper |
| `--claude-scope` | `unsure` | `unsure` = Medium/Low/Not found, `notfound`, or `all` |
| `--claude-budget` | `20` | Stops once this many USD are spent. Re-run with a higher value to continue |
| `--claude-web` | off | Lets Claude do up to 3 of its own web searches per row (~$0.01 each + tokens) |

**Cheapest setup: Haiku + Batch API (half price, about $0.0015 per row measured):**
```
python possession_scraper.py "C:\path\to\sheet.xlsx" --claude-only --claude-model claude-haiku-4-5 --claude-batch
```
Batches finish in minutes to a few hours. You can close the window while it waits; re-run the same command to collect.

Rough cost per reviewed row without web search, not using batch: Opus 5 about $0.04, Sonnet 5 about $0.017, Haiku 4.5 about $0.008.
Reviews are saved as they go; re-running skips rows already reviewed.

Useful flags:
- `--workers 8`: more parallel buildings (default 6)
- `--browser`: headless Chrome for sites that block plain requests (run `playwright install chromium` once)
- `--retry missing` / `--retry low`: second pass over not-found / low-confidence rows
- `--export-only`: write current progress into the sheet without scraping
