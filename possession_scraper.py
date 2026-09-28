"""Bulk possession-date scraper.

    python possession_scraper.py "C:\\path\\to\\buildings.xlsx"

Reads building name / locality / city / lat / long from the sheet, searches the web for each
building's possession date, and writes a "Possession Dates" tab back into the same workbook.
Progress is saved in a SQLite file next to the sheet, so it can be stopped and resumed anytime.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd

import extractor as ex
from search_backends import (AllBackendsDisabled, CreditsExhausted, BrowserFetcher, PageFetcher, SearchManager,
                             SearchResult, build_backends, headers)

log = logging.getLogger("scraper")

COL_HINTS = {
    "name": ["building name", "project name", "society name", "property name", "building", "project",
             "society", "name", "complex", "apartment"],
    "locality": ["locality", "sub locality", "sublocality", "micro market", "neighbourhood", "neighborhood",
                 "area", "location", "sector", "region"],
    "city": ["city", "town", "district"],
    "lat": ["latitude", "lat"],
    "lon": ["longitude", "lng", "lon", "long"],
    "rera": ["rera id", "rera number", "rera no", "rera registration", "rera"],
    "existing": ["possession date", "possession"],
}

OUT_COLS = {
    "display": "Possession Date",
    "iso": "Possession (YYYY-MM)",
    "precision": "Date Precision",
    "status": "Project Status",
    "label": "Confidence",
    "confidence": "Confidence Score",
    "sources": "Sources Agreeing",
    "rera_date": "RERA Date",
    "best_url": "Best Source",
    "evidence": "Evidence",
    "alternatives": "Other Dates Seen",
    "queries": "Queries Run",
    "note": "Scrape Note",
}


# ----------------------------------------------------------------------------- storage

class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS results(row_idx INTEGER PRIMARY KEY, rkey TEXT, data TEXT,
                found INTEGER, label TEXT, updated TEXT);
            CREATE INDEX IF NOT EXISTS results_key ON results(rkey);
            CREATE TABLE IF NOT EXISTS search_cache(q TEXT PRIMARY KEY, backend TEXT, results TEXT, ts TEXT);
            CREATE TABLE IF NOT EXISTS geo(latlon TEXT PRIMARY KEY, locality TEXT, city TEXT);
            CREATE TABLE IF NOT EXISTS claude(row_idx INTEGER PRIMARY KEY, rkey TEXT, data TEXT, updated TEXT);
            CREATE INDEX IF NOT EXISTS claude_key ON claude(rkey);
        """)

    def claude_done(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT row_idx FROM claude")}

    def claude_by_key(self, rkey: str) -> dict | None:
        r = self.db.execute("SELECT data FROM claude WHERE rkey=? LIMIT 1", (rkey,)).fetchone()
        return json.loads(r[0]) if r else None

    def save_claude(self, idx: int, rkey: str, data: dict):
        self.db.execute("INSERT OR REPLACE INTO claude VALUES(?,?,?,?)",
                        (idx, rkey, json.dumps(data, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def claude_all(self) -> dict[int, dict]:
        return {i: json.loads(d) for i, d in self.db.execute("SELECT row_idx, data FROM claude")}

    def rkey(self, idx: int) -> str | None:
        r = self.db.execute("SELECT rkey FROM results WHERE row_idx=?", (idx,)).fetchone()
        return r[0] if r else None

    def done_rows(self, retry: str | None) -> set[int]:
        q = "SELECT row_idx FROM results"
        if retry == "missing":
            q += " WHERE found=1"
        elif retry == "low":
            q += " WHERE found=1 AND label IN ('High','Medium')"
        return {r[0] for r in self.db.execute(q)}

    def forget_domain(self, domain: str) -> int:
        """Drop results (and Claude verdicts) whose evidence came from `domain`, so they get redone."""
        drop = []
        for idx, d in self.db.execute("SELECT row_idx, data FROM results"):
            d = json.loads(d)
            urls = [d.get("best_url") or ""] + [x.get("url", "") for x in (d.get("pack") or {}).get("dates", [])]
            if any(domain in u for u in urls):
                drop.append((idx,))
        self.db.executemany("DELETE FROM results WHERE row_idx=?", drop)
        self.db.executemany("DELETE FROM claude WHERE row_idx=?", drop)
        self.db.commit()
        return len(drop)

    def by_key(self, rkey: str) -> dict | None:
        r = self.db.execute("SELECT data FROM results WHERE rkey=? AND found=1 ORDER BY updated DESC LIMIT 1",
                            (rkey,)).fetchone()
        return json.loads(r[0]) if r else None

    def save(self, idx: int, rkey: str, data: dict):
        self.db.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?)",
                        (idx, rkey, json.dumps(data, ensure_ascii=False), int(bool(data.get("found"))),
                         data.get("label", ""), datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def all_results(self) -> dict[int, dict]:
        return {i: json.loads(d) for i, d in self.db.execute("SELECT row_idx, data FROM results")}

    def get_search(self, q: str) -> list[SearchResult] | None:
        r = self.db.execute("SELECT results FROM search_cache WHERE q=?", (q,)).fetchone()
        return [SearchResult(**x) for x in json.loads(r[0])] if r else None

    def put_search(self, q: str, backend: str, res: list[SearchResult]):
        self.db.execute("INSERT OR REPLACE INTO search_cache VALUES(?,?,?,?)",
                        (q, backend, json.dumps([r.__dict__ for r in res], ensure_ascii=False),
                         datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def get_geo(self, key: str):
        return self.db.execute("SELECT locality, city FROM geo WHERE latlon=?", (key,)).fetchone()

    def put_geo(self, key: str, loc: str, city: str):
        self.db.execute("INSERT OR REPLACE INTO geo VALUES(?,?,?)", (key, loc, city))
        self.db.commit()


# ----------------------------------------------------------------------------- sheet helpers

def _clean_col(c) -> str:
    return re.sub(r"[^a-z]+", " ", str(c).lower()).strip()


def detect_columns(cols: list, overrides: dict) -> dict:
    mapping = {}
    cleaned = {c: _clean_col(c) for c in cols}
    for key, hints in COL_HINTS.items():
        if overrides.get(key):
            want = overrides[key].strip().lower()
            hit = next((c for c in cols if str(c).strip().lower() == want), None)
            if hit is None:
                sys.exit(f"Column '{overrides[key]}' not found. Columns are: {list(cols)}")
            mapping[key] = hit
            continue
        used = set(mapping.values())
        hit = next((c for h in hints for c in cols if c not in used and cleaned[c] == h), None)
        if hit is None:
            hit = next((c for h in hints for c in cols
                        if c not in used and set(h.split()) <= set(cleaned[c].split())), None)
        mapping[key] = hit
    if mapping["name"] is None:
        sys.exit(f"Couldn't find the building-name column; pass --name-col. Columns are: {list(cols)}")
    return mapping


def cell(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "null", "-", "na", "n/a") else s


def row_key(name: str, loc: str, city: str) -> str:
    return "|".join(ex.norm(x) for x in (name, loc, city))


_STATES = {"maharashtra", "karnataka", "telangana", "tamil nadu", "kerala", "gujarat", "haryana", "uttar pradesh",
           "delhi", "new delhi", "west bengal", "andhra pradesh", "rajasthan", "madhya pradesh", "punjab", "goa",
           "odisha", "bihar", "uttarakhand", "chandigarh", "jharkhand", "chhattisgarh", "assam", "india", "ncr",
           "delhi ncr"}
_CITY_LIKE = {"navi mumbai", "thane", "pimpri chinchwad", "greater noida", "noida", "gurugram", "gurgaon",
              "ghaziabad", "faridabad", "mira bhayandar", "kalyan dombivli", "vasai virar", "secunderabad",
              "new town", "howrah", "mumbai suburban", "bengaluru urban", "bangalore urban", "rangareddy",
              "ranga reddy", "medchal malkajgiri", "sangareddy", "palghar", "raigad", "chengalpattu", "kancheepuram",
              "tiruvallur", "pune district", "hyderabad district"}
_ADDR_NOISE = re.compile(r"\b(road|rd|cross|main|street|st|lane|marg|highway|hwy|near|opp|opposite|behind|floor"
                         r"|plot|survey|sy|off)\b", re.I)


def short_locality(address: str, city: str) -> str:
    """'2nd Cross Rd, Lokhandwala Complex, Andheri West, Mumbai, Maharashtra 400047' -> 'Andheri West'."""
    if not address or "," not in address:
        return address[:60] if address else ""
    c = ex.norm(city)
    city_names = {c, ex.CITY_ALIASES.get(c, "")} | {k for k, v in ex.CITY_ALIASES.items() if v == c}
    keep = []
    for part in address.split(","):
        p = part.strip()
        n = ex.norm(p)
        if not n or n in _STATES or n in city_names:
            continue
        if re.search(r"\d", p) and not re.fullmatch(r"(?i)sector[\s\-]*\d+[a-z]?", p):
            continue  # PIN codes, plus codes, house/plot numbers
        if _ADDR_NOISE.search(p):
            continue
        keep.append(p)
    # Municipalities that sit between locality and city ("Ravet, Pimpri-Chinchwad") are too broad to search on.
    specific = [p for p in keep if ex.norm(p) not in _CITY_LIKE]
    return (specific or keep)[-1] if keep else ""


def parse_coord(v: str) -> str:
    """'77.38914312° E' -> '77.38914312', '12.5° S' -> '-12.5'."""
    m = re.search(r"-?\d+(?:\.\d+)?", v or "")
    if not m:
        return ""
    num = float(m.group())
    if re.search(r"[SW]\s*$", v.strip(), re.I):
        num = -abs(num)
    return str(num)


_PLUS_CODE = re.compile(r"\b[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}\b", re.I)


def city_from_address(address: str) -> str:
    """'…, Sector 78, Noida, Uttar Pradesh 201305, India' -> 'Noida'. The part just before the state/PIN."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    for i, p in enumerate(parts):
        n = ex.norm(re.sub(r"\d", " ", p))
        if n in _STATES and n != "india" and n != "delhi":
            for prev in reversed(parts[:i]):
                cand = _PLUS_CODE.sub("", prev).strip()
                if cand and not re.fullmatch(r"[\d\s\-/]+", cand):
                    return re.sub(r"\s*\d{6}$", "", cand)
            return ""
    return ""


def row_fields(df: pd.DataFrame, cols: dict, i: int) -> dict:
    """Everything the scraper needs from one sheet row, normalised."""
    def val(key):
        return cell(df.iat[i, df.columns.get_loc(cols[key])]) if cols.get(key) is not None else ""
    address = val("locality")
    city = val("city") or city_from_address(address)
    rera_raw = val("rera")
    return {"name": val("name"), "address": address, "loc": short_locality(address, city), "city": city,
            "lat": parse_coord(val("lat")), "lon": parse_coord(val("lon")),
            "rera": rera_raw, "rera_ids": split_rera(rera_raw)}


def split_rera(v: str) -> list[str]:
    """Some rows hold several IDs: 'UPRERAPRJ9689 | UPRERAPRJ9214'. Karnataka IDs contain slashes, so no '/' split."""
    return [x.strip() for x in re.split(r"[|,;\n]+", v or "") if len(x.strip()) >= 5]


def build_rera_queries(name: str, loc: str, city: str, rera_ids: list[str]) -> list[str]:
    place = " ".join(x for x in (loc, city) if x)
    qs = []
    if rera_ids:
        qs.append(f"\"{rera_ids[0]}\"")
    qs.append(f"{name} {place} rera possession date extension")
    for rid in rera_ids[:2]:
        qs.append(f"{rid} revised completion date extension")
    if len(rera_ids) > 1:
        qs.append(f"\"{rera_ids[1]}\"")
    qs.append(f"{name} {city} rera revised completion date")
    out, seen = [], set()
    for q in qs:
        q = re.sub(r"\s+", " ", q).strip()
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def build_queries(name: str, loc: str, city: str, rera: str = "") -> list[str]:
    place = " ".join(x for x in (loc, city) if x)
    qs = [
        f"{name} {place} possession date",
        f"{rera} possession date" if rera else "",
        f"\"{name}\" {city} possession",
        f"{name} {city} rera completion date",
        f"{name} {place} ready to move year built",
        f"{name} {city} site:housing.com OR site:99acres.com OR site:magicbricks.com OR site:squareyards.com",
        f"{name} {place} project details possession status",
    ]
    out, seen = [], set()
    for q in qs:
        q = re.sub(r"\s+", " ", q.replace('""', "")).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


# ----------------------------------------------------------------------------- scraping

class Ctx:
    def __init__(self, args, store, client, search, fetcher):
        self.args, self.store, self.client, self.search, self.fetcher = args, store, client, search, fetcher
        self.excluded = [d.strip().lower() for d in (args.exclude_domains or "").split(",") if d.strip()]
        self.geo_lock = asyncio.Lock()
        self.geo_last = 0.0


def pick_pages(results: list[SearchResult], m: ex.NameMatcher, seen: set, k: int) -> list[SearchResult]:
    scored = []
    for r in results:
        if r.url in seen or PageFetcher.skippable(r.url):
            continue
        match = m.score(f"{r.title} {r.snippet}")
        if match < 0.5:
            continue
        scored.append((ex.domain_weight(r.url)[0] + match, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in scored[:k]]


def evidence_pack(m: ex.NameMatcher, results: list[SearchResult], cands: list[ex.Candidate]) -> dict:
    """Compact record of what the scraper saw, kept for the Claude review pass."""
    snippets, seen = [], set()
    for r in results:
        if r.url not in seen and m.score(f"{r.title} {r.snippet}") >= 0.45:
            seen.add(r.url)
            snippets.append({"title": r.title[:150], "url": r.url, "snippet": r.snippet[:300]})
    ranked = sorted(cands, key=lambda c: c.score, reverse=True)
    rera = [c for c in ranked if c.is_rera][:10]
    top = rera + [c for c in ranked if not c.is_rera][: max(15 - len(rera), 5)]
    return {"snippets": snippets[:12],
            "dates": [{"date": c.key, "kind": c.kind, "score": round(c.score, 2), "url": c.url,
                       "text": c.context[:250]} for c in top]}


async def scrape_building(ctx: Ctx, name: str, loc: str, city: str,
                          rera_ids: list[str]) -> tuple[ex.Verdict, dict, dict]:
    a = ctx.args
    rera_mode = a.mode == "rera"
    m = ex.NameMatcher(name, loc, city, tuple(rera_ids))
    cands, statuses, seen, all_results = [], ex.Counter(), set(), []
    pairs: list | None = [] if rera_mode else None
    verdict, n_q, pages_left = ex.Verdict(), 0, a.max_pages
    queries = (build_rera_queries(name, loc, city, rera_ids) if rera_mode
               else build_queries(name, loc, city, rera_ids[0] if rera_ids else ""))
    for q in queries[: a.max_queries]:
        results = [r for r in await ctx.search.search(ctx.client, q)
                   if not any(dom in r.url.lower() for dom in ctx.excluded)]
        all_results += results
        n_q += 1
        for r in results:
            c, s = ex.from_snippet(r.title, r.snippet, r.url, m, pairs)
            cands += c
            statuses.update(s)
        for r in pick_pages(results, m, seen, min(a.pages_per_query, pages_left)):
            seen.add(r.url)
            pages_left -= 1
            html = await ctx.fetcher.fetch(r.url)
            if html:
                c, s = await asyncio.to_thread(ex.from_page, html, r.url, m, pairs)
                cands += c
                statuses.update(s)
        verdict = ex.aggregate(cands, statuses)
        # RERA mode keeps searching: the latest extension is often only on a later result.
        if verdict.label == "High" and not rera_mode:
            break
    verdict.queries = n_q
    return verdict, evidence_pack(m, all_results, cands), (ex.rera_summary(cands, pairs) if rera_mode else {})


async def reverse_geocode(ctx: Ctx, lat: str, lon: str) -> tuple[str, str]:
    """Only used when a row has neither locality nor city. OpenStreetMap Nominatim, 1 req/sec."""
    try:
        la, lo = round(float(lat), 4), round(float(lon), 4)
    except ValueError:
        return "", ""
    key = f"{la},{lo}"
    hit = ctx.store.get_geo(key)
    if hit:
        return hit
    async with ctx.geo_lock:
        wait = ctx.geo_last + 1.2 - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        ctx.geo_last = time.monotonic()
        try:
            r = await ctx.client.get("https://nominatim.openstreetmap.org/reverse",
                                     params={"lat": la, "lon": lo, "format": "jsonv2", "zoom": 16},
                                     headers={"User-Agent": "possession-date-scraper/1.0"})
            addr = r.json().get("address", {}) if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            addr = {}
    loc = addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter") or addr.get("village") or ""
    city = addr.get("city") or addr.get("town") or addr.get("state_district") or addr.get("county") or ""
    ctx.store.put_geo(key, loc, city)
    return loc, city


class Progress:
    def __init__(self, total: int):
        self.total, self.done, self.found, self.t0 = total, 0, 0, time.monotonic()
        self.recent: list[float] = []

    def tick(self, found: bool) -> str:
        # Speed over the last 300 rows, so a pause (sleep, network drop) doesn't skew the ETA for hours.
        now = time.monotonic()
        self.recent = (self.recent + [now])[-300:]
        self.done += 1
        self.found += int(found)
        span = now - (self.recent[0] if len(self.recent) > 1 else self.t0)
        per_sec = (len(self.recent) - 1) / span if len(self.recent) > 1 and span > 0 else 0
        eta = (self.total - self.done) / per_sec if per_sec else 0
        return (f"[{self.done}/{self.total}] found {self.found} ({self.found * 100 // self.done}%) "
                f"| {per_sec * 60:.1f} rows/min | ETA {eta / 3600:.1f}h")


def notify_user():
    """Audible heads-up that the run needs attention."""
    if sys.platform == "win32":
        import winsound
        for _ in range(3):
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            time.sleep(0.6)


def keep_awake():
    """Stop Windows from sleeping while the scraper runs (closing the lid still sleeps)."""
    if sys.platform == "win32":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log.info("Keeping the PC awake while this runs")


async def worker(ctx: Ctx, queue: asyncio.Queue, prog: Progress, export_cb):
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return
        idx, name, loc, city, lat, lon, rera = item
        try:
            if not name:
                data = ex.Verdict(note="empty building name").as_dict()
            else:
                if not loc and not city and lat and lon:
                    loc, city = await reverse_geocode(ctx, lat, lon)
                rkey = row_key(name, loc, city)
                data = ctx.store.by_key(rkey)
                if data:
                    data = {**data, "note": "same building as another row"}
                else:
                    verdict, pack, rera_info = await scrape_building(ctx, name, loc, city, rera)
                    data = {**verdict.as_dict(), **rera_info, "pack": pack, "locality_used": loc, "city_used": city}
            ctx.store.save(idx, row_key(name, loc, city), data)
            log.info("%s | %s (%s) -> %s [%s]", prog.tick(bool(data.get("found"))), name,
                     ", ".join(x for x in (loc, city) if x), data.get("display") or "not found",
                     data.get("label"))
            await export_cb(prog.done)
        except (AllBackendsDisabled, CreditsExhausted):
            raise  # this row is not saved, so it is redone on the next run
        except Exception as e:
            log.exception("row %s (%s) failed: %s", idx, name, e)
            prog.tick(False)
        finally:
            queue.task_done()


# ----------------------------------------------------------------------------- export

CLAUDE_COLS = {
    "display": "Claude Date", "confidence": "Claude Confidence", "status": "Claude Status",
    "phase_note": "Claude Phase Note", "reasoning": "Claude Reasoning", "source_url": "Claude Source",
}


def parse_sheet_date(v: str) -> tuple[int, int] | None:
    """Existing possession date in the user's sheet -> (year, month). Handles datetimes, text and Excel serials."""
    v = cell(v)
    if not v:
        return None
    if re.fullmatch(r"\d{5}(\.\d+)?", v):
        ts = pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(v))
    else:
        pd_ = ex.parse_first_date(v)
        if pd_ and pd_.month:
            return pd_.year, pd_.month
        ts = pd.to_datetime(v, errors="coerce", dayfirst=True)
        if pd.isna(ts):
            return (pd_.year, 12) if pd_ else None
    return (ts.year, ts.month) if MIN_SHEET_YEAR <= ts.year <= 2100 else None


MIN_SHEET_YEAR = 1950


def compare_dates(sheet: tuple[int, int] | None, web_iso: str) -> tuple[str, str, int | str]:
    """Returns (sheet date display, verdict, web-minus-sheet months)."""
    shown = f"{ex.MONTH_ABBR[sheet[1]]} {sheet[0]}" if sheet else ""
    if not web_iso:
        return shown, ("No web date" if sheet else ""), ""
    if not sheet:
        return shown, "No sheet date", ""
    if len(web_iso) == 4:
        return shown, ("Same year" if int(web_iso) == sheet[0] else "Different year"), ""
    diff = (int(web_iso[:4]) * 12 + int(web_iso[5:7])) - (sheet[0] * 12 + sheet[1])
    if diff == 0:
        verdict = "Match"
    elif abs(diff) <= 3:
        verdict = "Close (within 3 months)"
    else:
        verdict = f"Web is {abs(diff)} months {'later' if diff > 0 else 'earlier'}"
    return shown, verdict, diff


def export(src: Path, sheet, df: pd.DataFrame, results: dict[int, dict], out_sheet: str, backup_done: list,
           claude: dict[int, dict] | None = None, existing_col=None, mode: str = "possession",
           summary_sheet: str = "Possession Summary"):
    claude = claude or {}
    rows, verdicts, rera_verdicts = [], [], []
    for i in range(len(df)):
        d = results.get(i)
        if d is None:
            r = {v: "" for v in OUT_COLS.values()}
        else:
            r = {OUT_COLS[k]: d.get(k, "") for k in OUT_COLS}
            if not d.get("found"):
                r["Possession Date"] = "Not found"
        if claude:
            c = claude.get(i)
            r.update({v: (c or {}).get(k, "") for k, v in CLAUDE_COLS.items()})
            if c:
                r["Final Possession Date"] = c["display"] or "Not found"
                r["Final Source"] = "Claude review"
            elif d is not None:
                r["Final Possession Date"] = r["Possession Date"]
                r["Final Source"] = "Scraper"
            else:
                r["Final Possession Date"] = r["Final Source"] = ""
        if existing_col is not None:
            if d is None:
                r.update({"Sheet Possession Date": "", "Web vs Sheet": "", "Difference (months)": ""})
            else:
                c = claude.get(i)
                web_iso = c.get("iso", "") if c else (d.get("iso", "") if d.get("found") else "")
                shown, verdict, diff = compare_dates(parse_sheet_date(df.iat[i, df.columns.get_loc(existing_col)]),
                                                     web_iso)
                r.update({"Sheet Possession Date": shown, "Web vs Sheet": verdict, "Difference (months)": diff})
                verdicts.append(verdict)
        if mode == "rera":
            dd = d or {}
            r.update({"RERA Original Completion": dd.get("rera_orig_display", ""),
                      "RERA Current Completion": dd.get("rera_current_display", ""),
                      "Extension / Revision": dd.get("extension_found", "") if d else ""})
            if d is not None and existing_col is not None:
                _, rv, rdiff = compare_dates(parse_sheet_date(df.iat[i, df.columns.get_loc(existing_col)]),
                                             dd.get("rera_current_iso", ""))
                rv = rv.replace("Web is", "RERA is").replace("No web date", "No RERA date found")
                r.update({"RERA vs Sheet": rv, "RERA Difference (months)": rdiff})
                rera_verdicts.append(rv)
            r.update({"RERA Evidence": dd.get("rera_evidence", ""), "RERA Source": dd.get("rera_url", ""),
                      "Other RERA Dates Seen": dd.get("rera_alternatives", "")})
        rows.append(r)
    out = pd.concat([df.reset_index(drop=True), pd.DataFrame(rows)], axis=1)

    labels = [d.get("label", "") for d in results.values()]
    metrics = [("Rows in sheet", len(df)), ("Rows processed", len(results)),
               ("Date found", sum(1 for d in results.values() if d.get("found"))),
               ("High confidence", labels.count("High")), ("Medium confidence", labels.count("Medium")),
               ("Low confidence", labels.count("Low")), ("Not found", labels.count("Not found"))]
    if claude:
        cc = [c.get("confidence") for c in claude.values()]
        metrics += [("Claude reviewed", len(claude)), ("Claude gave a date", sum(1 for c in claude.values() if c.get("iso"))),
                    ("Claude high", cc.count("high")), ("Claude medium", cc.count("medium")),
                    ("Claude low", cc.count("low")), ("Claude could not tell", cc.count("none"))]
    if verdicts:
        metrics += [("Web vs sheet: match", verdicts.count("Match")),
                    ("Web vs sheet: within 3 months", verdicts.count("Close (within 3 months)")),
                    ("Web vs sheet: differs by more than 3 months",
                     sum(1 for v in verdicts if v.startswith("Web is") or v == "Different year")),
                    ("Web vs sheet: same year (web has year only)", verdicts.count("Same year")),
                    ("Web found a date where sheet has none", verdicts.count("No sheet date")),
                    ("Sheet has a date the web didn't find", verdicts.count("No web date"))]
    if mode == "rera":
        metrics += [("RERA date found", sum(1 for d in results.values() if d.get("rera_current_iso"))),
                    ("RERA extension found", sum(1 for d in results.values()
                                                 if str(d.get("extension_found", "")).startswith("Yes"))),
                    ("RERA later than sheet", sum(1 for v in rera_verdicts if "later" in v)),
                    ("RERA earlier than sheet", sum(1 for v in rera_verdicts if "earlier" in v)),
                    ("RERA matches sheet (within 3 months)",
                     sum(1 for v in rera_verdicts if v in ("Match", "Close (within 3 months)")))]
    metrics.append(("Last updated", datetime.now().strftime("%Y-%m-%d %H:%M")))
    summary = pd.DataFrame(metrics, columns=["Metric", "Value"])

    targets = []
    if src.suffix.lower() in (".xlsx", ".xlsm"):
        if not backup_done:
            bk = src.with_name(f"{src.stem}.backup{src.suffix}")
            if not bk.exists():
                shutil.copy2(src, bk)
                log.info("Backed up original workbook to %s", bk.name)
            backup_done.append(True)
        targets.append((src, "a"))
    tag = re.sub(r"[^A-Za-z0-9]+", "_", out_sheet).strip("_")
    targets.append((src.with_name(f"{src.stem}_{tag}.xlsx"), "w"))

    for path, mode in targets:
        try:
            kw = {"if_sheet_exists": "replace"} if mode == "a" else {}
            with pd.ExcelWriter(path, engine="openpyxl", mode=mode, **kw) as xw:
                out.to_excel(xw, sheet_name=out_sheet, index=False)
                summary.to_excel(xw, sheet_name=summary_sheet, index=False)
                ws = xw.sheets[out_sheet]
                ws.freeze_panes = "B2"
                ws.auto_filter.ref = ws.dimensions
                from openpyxl.styles import Font, PatternFill
                first_new = len(df.columns) + 1
                for col_i, col in enumerate(out.columns, 1):
                    c = ws.cell(row=1, column=col_i)
                    c.font = Font(bold=True)
                    if col_i >= first_new:
                        c.fill = PatternFill("solid", fgColor="FFF2CC")
                    width = 14 if col_i < first_new else {"Evidence": 60, "Best Source": 45,
                                                          "Other Dates Seen": 35, "Claude Reasoning": 60,
                                                          "Claude Source": 45, "Claude Phase Note": 30,
                                                          "Extension / Revision": 40, "RERA Evidence": 60,
                                                          "RERA Source": 45}.get(col, 18)
                    ws.column_dimensions[c.column_letter].width = width
            log.info("Wrote %d rows to '%s' in %s", len(out), out_sheet, path.name)
            break
        except PermissionError:
            log.warning("%s is open in another program (close Excel?) - writing a separate file instead", path.name)
        except Exception as e:
            log.warning("Could not write to %s: %s", path.name, e)
    out.to_csv(src.with_name(f"{src.stem}_{tag}.csv"), index=False, encoding="utf-8-sig")


# ----------------------------------------------------------------------------- main

def store_path(src: Path, sheet: str | None, mode: str) -> Path:
    """One progress file per tab (and mode), so two tabs of the same workbook never mix rows.
    Falls back to the older single-file name when that exists, so earlier runs keep resuming."""
    legacy = src.with_name(f"{src.stem}.possession_cache.sqlite")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{sheet or 'sheet'}_{mode}").strip("_")
    per_tab = src.with_name(f"{src.stem}.{slug}.possession_cache.sqlite")
    if legacy.exists() and not per_tab.exists() and mode == "possession":
        db = sqlite3.connect(legacy)
        try:
            owner = db.execute("SELECT value FROM meta WHERE key='sheet'").fetchone()
        except sqlite3.OperationalError:
            owner = None
        finally:
            db.close()
        if owner and owner[0] == (sheet or ""):
            return legacy
    return per_tab


def load_env(path: Path):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_args():
    p = argparse.ArgumentParser(description="Scrape possession dates for every building in a sheet.")
    p.add_argument("sheet", help="Path to .xlsx / .xlsm / .csv")
    p.add_argument("--sheet-name", help="Tab to read (default: first tab)")
    p.add_argument("--name-col"), p.add_argument("--locality-col"), p.add_argument("--city-col")
    p.add_argument("--lat-col"), p.add_argument("--lon-col"), p.add_argument("--rera-col")
    p.add_argument("--existing-col", help="Column holding possession dates you already have, to compare against")
    p.add_argument("--out-sheet", help="Tab to write results into (default: 'Results - <input tab>')")
    p.add_argument("--mode", choices=["possession", "rera"], default="possession",
                   help="possession = find possession date; rera = check RERA completion date / extensions by RERA ID")
    p.add_argument("--workers", type=int, default=6, help="Buildings processed in parallel")
    p.add_argument("--max-queries", type=int,
                   help="Max searches per building (default 3; 4 in rera mode). Possession mode stops early when sure")
    p.add_argument("--pages-per-query", type=int, default=3, help="Result pages opened per search")
    p.add_argument("--max-pages", type=int, default=8, help="Max pages opened per building")
    p.add_argument("--start", type=int, default=0, help="First data row (0-based) to process")
    p.add_argument("--limit", type=int, help="Only process this many rows (for a test run)")
    p.add_argument("--retry", choices=["missing", "low"],
                   help="Re-scrape rows already done: 'missing' = not found, 'low' = not found or Low")
    p.add_argument("--browser", action="store_true", help="Use headless Chromium for pages that block scrapers")
    p.add_argument("--fallback-free", action="store_true",
                   help="When the paid search key runs out, carry on with free engines instead of stopping")
    p.add_argument("--backends", help="Comma list to restrict engines, e.g. bing,duckduckgo,serper")
    p.add_argument("--save-every", type=int, default=500, help="Write the sheet every N rows")
    p.add_argument("--export-only", action="store_true", help="Just write what's been scraped so far into the sheet")
    p.add_argument("--exclude-domains", default="",
                   help="Comma list of sites to ignore as evidence, e.g. your own site: nobroker.in")
    p.add_argument("--redo-domain", help="Re-scrape rows already done whose evidence came from this site")

    c = p.add_argument_group("Claude review (needs ANTHROPIC_API_KEY in .env)")
    c.add_argument("--claude", action="store_true", help="After scraping, have Claude review unsure rows")
    c.add_argument("--claude-only", action="store_true", help="Skip scraping; only run the Claude review")
    c.add_argument("--claude-dry-run", action="store_true", help="Show an example prompt + cost estimate, send nothing")
    c.add_argument("--claude-scope", choices=["unsure", "notfound", "all"], default="unsure",
                   help="unsure = Medium/Low/Not found (default), notfound = only Not found, all = every row")
    c.add_argument("--claude-model", default="claude-opus-5",
                   help="claude-opus-5 (default), claude-sonnet-5 (cheaper), claude-haiku-4-5 (cheapest)")
    c.add_argument("--claude-effort", default="medium", choices=["low", "medium", "high"])
    c.add_argument("--claude-web", action="store_true", help="Let Claude run its own web searches (extra cost)")
    c.add_argument("--claude-web-searches", type=int, default=3, help="Max Claude web searches per row")
    c.add_argument("--claude-budget", type=float, default=20.0, help="Stop the review after spending this many USD")
    c.add_argument("--claude-workers", type=int, default=8, help="Parallel Claude requests")
    c.add_argument("--claude-limit", type=int, help="Only review this many rows (for a test)")
    c.add_argument("--claude-batch", action="store_true",
                   help="Use the Batch API: half price, results in minutes to a few hours")
    return p.parse_args()


async def run(args, src: Path, df: pd.DataFrame, cols: dict, store: Store, backup_done: list):
    todo_idx = [i for i in range(args.start, len(df)) if i not in store.done_rows(args.retry)]
    if args.limit:
        todo_idx = todo_idx[: args.limit]
    log.info("%d rows to scrape (%d already done)", len(todo_idx), len(store.done_rows(None)))
    if not todo_idx:
        return

    limits = httpx.Limits(max_connections=60, max_keepalive_connections=30)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), follow_redirects=True,
                                 limits=limits, headers=headers()) as client:
        browser = None
        if args.browser:
            browser = BrowserFetcher()
            await browser.start()
        backends = build_backends(args.backends.split(",") if args.backends else None, args.fallback_free)
        log.info("Search engines: %s", ", ".join(b.name for b in backends))
        search = SearchManager(backends, store, stop_on_exhausted=not args.fallback_free)
        ctx = Ctx(args, store, client, search, PageFetcher(client, browser))
        prog = Progress(len(todo_idx))
        export_lock = asyncio.Lock()

        async def export_cb(done: int):
            if done % args.save_every == 0 and not export_lock.locked():
                async with export_lock:
                    res = store.all_results()
                    await asyncio.to_thread(export, src, args.sheet_name, df, res, args.out_sheet, backup_done,
                                            store.claude_all(), cols.get("existing"), args.mode,
                                            args.summary_sheet)
                    log.info("Engines so far: %s", search.stats())

        queue: asyncio.Queue = asyncio.Queue()
        for i in todo_idx:
            f = row_fields(df, cols, i)
            queue.put_nowait((i, f["name"], f["loc"], f["city"], f["lat"], f["lon"], f["rera_ids"]))
        for _ in range(args.workers):
            queue.put_nowait(None)
        tasks = [asyncio.create_task(worker(ctx, queue, prog, export_cb)) for _ in range(args.workers)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            log.info("Engines: %s", search.stats())
            if browser:
                await browser.close()


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    src = Path(args.sheet).expanduser().resolve()
    if not src.exists():
        sys.exit(f"File not found: {src}")
    load_env(Path(__file__).with_name(".env"))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(src.with_name(f"{src.stem}.possession_scrape.log"),
                                                      encoding="utf-8")])
    for noisy in ("httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if src.suffix.lower() == ".csv":
        df = pd.read_csv(src, dtype=str, keep_default_na=False)
    else:
        xl = pd.ExcelFile(src, engine="openpyxl")
        args.sheet_name = args.sheet_name or xl.sheet_names[0]
        df = xl.parse(args.sheet_name, dtype=str, keep_default_na=False)
    if args.max_queries is None:
        args.max_queries = 4 if args.mode == "rera" else 3
    spath = store_path(src, args.sheet_name, args.mode)
    legacy = spath.name == f"{src.stem}.possession_cache.sqlite"
    args.out_sheet = (args.out_sheet or ("Possession Dates" if legacy else f"Results - {args.sheet_name or 'sheet'}"))[:31]
    args.summary_sheet = ("Possession Summary" if legacy else f"Summary - {args.sheet_name or 'sheet'}")[:31]
    if args.sheet_name in (args.out_sheet, args.summary_sheet):
        sys.exit("--out-sheet must be different from the input tab")

    cols = detect_columns(list(df.columns), {"name": args.name_col, "locality": args.locality_col,
                                             "city": args.city_col, "lat": args.lat_col, "lon": args.lon_col,
                                             "rera": args.rera_col, "existing": args.existing_col})
    log.info("Loaded %d rows from %s [%s]", len(df), src.name, args.sheet_name or "csv")
    log.info("Column mapping: %s", {k: v for k, v in cols.items()})

    store = Store(spath)
    store.db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    store.db.execute("INSERT OR IGNORE INTO meta VALUES('sheet', ?)", (args.sheet_name or "",))
    store.db.commit()
    backup_done: list = []
    if args.redo_domain:
        log.info("Will redo %d rows whose evidence came from %s", store.forget_domain(args.redo_domain.lower()),
                 args.redo_domain)
    if not args.export_only:
        keep_awake()
    if not args.export_only and not args.claude_only:
        try:
            asyncio.run(run(args, src, df, cols, store, backup_done))
        except KeyboardInterrupt:
            log.warning("Stopped by user - saving progress (re-run the same command to resume)")
        except AllBackendsDisabled as e:
            log.error("%s", e)
        except CreditsExhausted as e:
            done = len(store.all_results())
            log.error("=" * 70)
            log.error("SEARCH CREDITS USED UP - stopped and saved (%d of %d rows done).", done, len(df))
            log.error("  %s", e)
            log.error("  Put a new SERPER_API_KEY in .env, then re-run the SAME command to continue.")
            log.error("=" * 70)
            notify_user()
    if not args.export_only and (args.claude or args.claude_only or args.claude_dry_run):
        try:
            run_claude(args, df, cols, store)
        except KeyboardInterrupt:
            log.warning("Stopped by user - Claude verdicts so far are saved")
    if args.claude_dry_run:
        return
    export(src, args.sheet_name, df, store.all_results(), args.out_sheet, backup_done, store.claude_all(),
           cols.get("existing"), args.mode, args.summary_sheet)


def run_claude(args, df: pd.DataFrame, cols: dict, store: Store):
    import claude_verify

    results = store.all_results()
    rows = {}
    for i in claude_verify.select_rows(results, args.claude_scope):
        if i >= len(df):
            continue
        d = results[i]
        f = row_fields(df, cols, i)
        loc, city = d.get("locality_used", f["loc"]), d.get("city_used", f["city"])
        rows[i] = {**f, "loc": loc, "city": city, "rkey": store.rkey(i) or row_key(f["name"], loc, city),
                   "scraped": d}

    def get_pack(i: int) -> dict:
        pack = results[i].get("pack")
        if pack:
            return pack
        # Rows scraped before evidence was saved: rebuild from cached search results.
        r = rows[i]
        m = ex.NameMatcher(r["name"], r["loc"], r["city"])
        res = []
        for q in build_queries(r["name"], r["loc"], r["city"], r["rera"]):
            res += store.get_search(q) or []
        return evidence_pack(m, res, [])

    asyncio.run(claude_verify.run(args, rows, store, get_pack))


if __name__ == "__main__":
    main()
