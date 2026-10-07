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
    "rera": ["rera id", "reraid", "rera number", "rera no", "rera registration", "rera"],
    "existing": ["possession date", "possession", "completiondate", "completion date"],
    "min_price": ["longminprice", "min price", "minprice"],
    "max_price": ["longmaxprice", "max price", "maxprice"],
}

# Offline city from coordinates (most rows have no city column). Nearest centre relative to its radius wins,
# so Thane / Navi Mumbai points don't get labelled Mumbai.
CITY_CENTERS = [
    ("Bangalore", 12.9716, 77.5946, 45), ("Hyderabad", 17.3850, 78.4867, 45), ("Chennai", 13.0827, 80.2707, 45),
    ("Mumbai", 19.0760, 72.8777, 25), ("Thane", 19.2183, 72.9781, 14), ("Navi Mumbai", 19.0330, 73.0297, 16),
    ("Mira Bhayandar", 19.2952, 72.8544, 7), ("Vasai Virar", 19.4259, 72.8225, 13),
    ("Kalyan Dombivli", 19.2350, 73.1300, 12), ("Panvel", 18.9894, 73.1175, 11), ("Pune", 18.5204, 73.8567, 35),
    ("Gurgaon", 28.4595, 77.0266, 20), ("Noida", 28.5355, 77.3910, 13), ("Greater Noida", 28.4744, 77.5040, 15),
    ("Ghaziabad", 28.6692, 77.4538, 14), ("Faridabad", 28.4089, 77.3178, 15), ("Delhi", 28.6139, 77.2090, 22),
    ("Kolkata", 22.5726, 88.3639, 30), ("Ahmedabad", 23.0225, 72.5714, 30), ("Gandhinagar", 23.2156, 72.6369, 12),
    ("Chandigarh", 30.7333, 76.7794, 20), ("Coimbatore", 11.0168, 76.9558, 25), ("Kochi", 9.9312, 76.2673, 25),
    ("Jaipur", 26.9124, 75.7873, 25), ("Lucknow", 26.8467, 80.9462, 25), ("Indore", 22.7196, 75.8577, 20),
    ("Nagpur", 21.1458, 79.0882, 20), ("Nashik", 19.9975, 73.7898, 15), ("Surat", 21.1702, 72.8311, 20),
    ("Vadodara", 22.3072, 73.1812, 18), ("Mysore", 12.2958, 76.6394, 15), ("Visakhapatnam", 17.6868, 83.2185, 20),
    ("Bhubaneswar", 20.2961, 85.8245, 18), ("Goa", 15.4909, 73.8278, 40),
]


def city_from_coords(lat: str, lon: str) -> str:
    import math
    try:
        la, lo = float(lat), float(lon)
    except ValueError:
        return ""
    if not (6 <= la <= 37 and 68 <= lo <= 98) and (6 <= lo <= 37 and 68 <= la <= 98):
        la, lo = lo, la  # swapped columns
    best, best_ratio = "", 1.0
    for name, cla, clo, r in CITY_CENTERS:
        dy = (la - cla) * 111.0
        dx = (lo - clo) * 111.0 * math.cos(math.radians(cla))
        ratio = math.hypot(dx, dy) / r
        if ratio <= best_ratio:
            best, best_ratio = name, ratio
    return best

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
            CREATE TABLE IF NOT EXISTS api_cache(k TEXT PRIMARY KEY, data TEXT);
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
        elif retry == "skipped-only":
            return {i for i, d in self.db.execute("SELECT row_idx, data FROM results")
                    if json.loads(d).get("label") == "Skipped"}
        elif retry == "incomplete":
            # full mode: done = both possession and price confirmed (or a row skipped as not-a-project)
            out = set()
            for idx, d in self.db.execute("SELECT row_idx, data FROM results"):
                d = json.loads(d)
                if d.get("label") == "Skipped" or (
                        d.get("found") and d.get("label") in ("High", "Medium")
                        and d.get("price_label") in ("High", "Medium")):
                    out.add(idx)
            return out
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

    def get_api(self, k: str):
        r = self.db.execute("SELECT data FROM api_cache WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def put_api(self, k: str, data):
        self.db.execute("INSERT OR REPLACE INTO api_cache VALUES(?,?)", (k, json.dumps(data, ensure_ascii=False)))
        self.db.commit()

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


_NOT_A_PROJECT = [
    ("PG / paying-guest listing", re.compile(r"^[0-9A-F]{16,}\b|\bPG for\b|\bpaying guest\b", re.I)),
]
_NAME_NOISE = re.compile(r"\s*[-(]?\s*\bsite\s+visit\b\s*\)?\s*$", re.I)
# "Bank Auction Property - Anika Apartment" -> search the building itself: "Anika Apartment"
_NAME_PREFIX = re.compile(r"^\s*(?:bank\s+)?auction\s+(?:propert(?:y|ies)|bazaar)\s*[-:|]\s*", re.I)


def not_a_project(name: str) -> str:
    """Reason a row is a listing rather than a building (nothing to search), else ''."""
    return next((why for why, rx in _NOT_A_PROJECT if rx.search(name or "")), "")


def row_fields(df: pd.DataFrame, cols: dict, i: int) -> dict:
    """Everything the scraper needs from one sheet row, normalised."""
    def val(key):
        return cell(df.iat[i, df.columns.get_loc(cols[key])]) if cols.get(key) is not None else ""
    address = val("locality")
    # Coordinates first: the part of an address before the state is often a village ("Thornahalli").
    city = val("city") or city_from_coords(parse_coord(val("lat")), parse_coord(val("lon"))) \
        or city_from_address(address)
    rera_raw = val("rera")
    return {"name": _NAME_PREFIX.sub("", _NAME_NOISE.sub("", val("name"))).strip(), "address": address,
            "loc": short_locality(address, city), "city": city,
            "lat": parse_coord(val("lat")), "lon": parse_coord(val("lon")),
            "rera": rera_raw, "rera_ids": split_rera(rera_raw)}


def split_rera(v: str) -> list[str]:
    """Some rows hold several IDs: 'UPRERAPRJ9689 | UPRERAPRJ9214'. Karnataka IDs contain slashes, so no '/' split."""
    v = re.sub(r"\s+dated\b[^|,;\n]*", "", v or "", flags=re.I)  # 'TN/01/Layout/506/2021 dated 14/12/2021'
    return [x.strip() for x in re.split(r"[|,;\n]+|\s+(?:I|l|and|&)\s+", v) if len(x.strip()) >= 5]


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


PORTAL_SITES = "site:99acres.com OR site:magicbricks.com OR site:housing.com OR site:squareyards.com"


def build_full_queries(name: str, loc: str, city: str, rera_ids: list[str]) -> list[str]:
    """Possession + price. Portal project pages carry both, so the first two searches aim at them."""
    place = " ".join(x for x in (loc, city) if x)
    qs = [f"{name} {place} price possession",
          f"{name} {place} {PORTAL_SITES}",
          f"\"{rera_ids[0]}\"" if rera_ids else f"\"{name}\" {city} possession date price",
          f"{name} {city} project price per sq.ft possession"]
    out, seen = [], set()
    for q in qs:
        q = re.sub(r"\s+", " ", q).strip()
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


# ----------------------------------------------------------------------------- scraping

class Ctx:
    def __init__(self, args, store, client, search, fetcher):
        self.args, self.store, self.client, self.search, self.fetcher = args, store, client, search, fetcher
        self.excluded = [d.strip().lower() for d in (args.exclude_domains or "").split(",") if d.strip()]
        self.geo_lock = asyncio.Lock()
        self.paid = 0
        self.pt_lock = asyncio.Lock()
        self.pt_last, self.pt_blocks, self.pt_fail_streak = 0.0, 0, 0
        self.geo_last = 0.0


def pick_pages(results: list[SearchResult], m: ex.NameMatcher, seen: set, k: int) -> list[SearchResult]:
    scored = []
    for r in results:
        if r.url in seen or PageFetcher.skippable(r.url) or ex.url_rejected(r.url):
            continue
        if ex.city_conflict(f"{r.title} {r.url}", m.city):
            continue
        match = 1.0 if m.id_in(f"{r.title} {r.snippet}") else m.score(r.title)
        if match < 0.6:
            continue
        trusted = ex.source_tier(r.url, m) in ex.TRUSTED_TIERS
        scored.append((trusted + ex.domain_weight(r.url)[0] + match, r))
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


def namesake_check(verdict: ex.Verdict, cands: list, prices: list) -> list:
    """A page whose possession date is over a year away from the confirmed one is about a namesake
    project (e.g. same name, other locality) - its price is shown only as unconfirmed."""
    if not (verdict.found and verdict.label in ("High", "Medium") and len(verdict.iso) == 7):
        return prices
    want = int(verdict.iso[:4]) * 12 + int(verdict.iso[5:])
    by_url: dict[str, list[int]] = {}
    for c in cands:
        if c.month:
            by_url.setdefault(c.url, []).append(abs(c.year * 12 + c.month - want))
    off = {u for u, gaps in by_url.items() if min(gaps) > 12}
    for p in prices:
        if p.url in off and p.trusted:
            p.trusted = False
            p.context = f"(possession on this page differs - maybe another project) {p.context}"
    return prices


PT_TYPEAHEAD = "https://www.proptiger.com/columbus/app/v6/typeahead"
PT_DETAIL = "https://www.proptiger.com/app/v4/project-detail/{}"
PT_MAX_KM = 3.0
PT_INTERVAL = 0.8      # seconds between PropTiger requests
PT_PAUSE = 600         # pause when PropTiger starts refusing
PT_MAX_PAUSES = 6


class BudgetReached(Exception):
    """--budget paid searches used; the row is left unsaved so a later run continues from it."""


class PropTigerBlocked(Exception):
    """PropTiger keeps refusing; the row is left unsaved so a later run redoes it."""
PT_STALE_MONTHS = 24


def _km(lat1, lon1, lat2, lon2) -> float:
    import math
    dy = (lat1 - lat2) * 111.0
    dx = (lon1 - lon2) * 111.0 * math.cos(math.radians(lat2))
    return math.hypot(dx, dy)


async def _pt_get(ctx: Ctx, url: str, params: dict | None = None):
    k = url + "?" + json.dumps(params or {}, sort_keys=True)
    hit = ctx.store.get_api(k)
    if hit is not None:
        return hit
    # One request at a time, spaced out: PropTiger blocks an IP that sends ~10 requests a second.
    async with ctx.pt_lock:
        attempt = 0
        while True:
            wait = ctx.pt_last + PT_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            ctx.pt_last = time.monotonic()
            try:
                r = await ctx.client.get(url, params=params)
            except httpx.HTTPError:
                r = None
            if r is not None and r.status_code == 200:
                ctx.pt_blocks = ctx.pt_fail_streak = 0
                try:
                    data = r.json()
                except ValueError:
                    data = {}
                ctx.store.put_api(k, data)
                return data
            if r is not None and r.status_code in (404, 400):
                ctx.store.put_api(k, {})
                return {}
            if r is not None and r.status_code in (403, 429):
                ctx.pt_blocks += 1
                if ctx.pt_blocks > PT_MAX_PAUSES:
                    raise PropTigerBlocked(f"HTTP {r.status_code} after {PT_MAX_PAUSES} pauses")
                log.warning("PropTiger is refusing requests (HTTP %d) - pausing %d min before trying again (%d/%d). "
                            "Nothing is marked 'not found' meanwhile.", r.status_code, PT_PAUSE // 60,
                            ctx.pt_blocks, PT_MAX_PAUSES)
                await asyncio.sleep(PT_PAUSE)
                continue
            attempt += 1
            if attempt >= 3:
                # One project's page erroring (HTTP 500) is normal - skip it (the paid pass can still find it).
                # Only many failures in a row mean PropTiger itself is down.
                ctx.pt_fail_streak += 1
                if ctx.pt_fail_streak >= 15:
                    raise PropTigerBlocked(f"no answer for 15 projects in a row "
                                           f"(last: {r.status_code if r is not None else 'network error'})")
                return {}
            await asyncio.sleep(3 * attempt)


async def proptiger_lookup(ctx: Ctx, m: ex.NameMatcher, name: str, city: str, lat: str, lon: str,
                           rera_ids: list[str]) -> tuple[list, list, str]:
    """Free: PropTiger's own project search + project data. A result is accepted when the name matches AND
      - its RERA number equals the sheet's (then up to 60 km off - sheet coordinates are often area centres), or
      - it is within 3 km of the sheet's coordinates, or
      - it is within 8 km, the name matches closely and the sheet's locality is in PropTiger's address.
    A different RERA number of the same format always rejects it. Returns (date cands, price cands, note)."""
    try:
        la, lo = float(lat), float(lon)
    except ValueError:
        la = lo = None
    mine = {re.sub(r"[^a-z0-9]", "", r.lower()) for r in rera_ids}

    def options(j) -> list:
        out = []
        for d in (j or {}).get("data") or []:
            if d.get("type") != "PROJECT" or not d.get("entityId"):
                continue
            label = f"{d.get('displayText', '')} {d.get('redirectUrl', '')}"
            s = max(m.score(d.get("displayText", "")), m.score(f"{d.get('builderName', '')} {d.get('entityName', '')}"))
            if s < 0.8 or ex.sector_conflict(label, m):
                continue
            if la is not None and d.get("latitude") and d.get("longitude"):
                km = _km(la, lo, float(d["latitude"]), float(d["longitude"]))
            elif ex.norm(d.get("city", "")) in ex.city_names_for(city):
                km = PT_MAX_KM
            else:
                continue
            if km <= 60:
                out.append((s, km, d))
        return sorted(out, key=lambda t: (-t[0], t[1]))[:2]

    q = {"typeAheadType": "project", "rows": 6, "sourceDomain": "Proptiger", "view": "buyer", "category": "buy"}
    found = options(await _pt_get(ctx, PT_TYPEAHEAD, {**q, "query": name}))
    rera_only = False
    base = re.sub(r"\s*\b(?:phase|ph|tower|wing|block|stage)\s*[-.]?\s*(?:[ivx]+|\d+[a-z]?)\b\.?\s*$", "", name,
                  flags=re.I).strip()
    if not found and base and base != name and mine:
        # "X Phase 2" unknown to PropTiger: try "X", but then only an exact RERA match will do.
        found, rera_only = options(await _pt_get(ctx, PT_TYPEAHEAD, {**q, "query": base})), True
    pick = None
    for s, km, d in found:
        det = ((await _pt_get(ctx, PT_DETAIL.format(d["entityId"]))) or {}).get("data") or {}
        if not det:
            continue
        rera = str(det.get("reraRegistrationNumber") or "")
        rera_n = re.sub(r"[^a-z0-9]", "", rera.lower())
        same_rera = bool(mine) and len(rera_n) >= 6 and any(rera_n in x or x in rera_n for x in mine)
        if mine and re.search(r"\d{4}", rera) and not same_rera and \
                any(ex._id_shape(r).fullmatch(rera.strip()) for r in rera_ids):
            continue  # another project / phase
        near_ok = km <= PT_MAX_KM or (km <= 8 and s >= 0.9 and m.place_in(d.get("displayText", "")))
        if same_rera or (near_ok and not rera_only):
            pick = (s, km, d, det, rera, same_rera)
            break
    if not pick:
        return [], [], ""
    s, km, d, det, rera, same_rera = pick
    url = "https://www.proptiger.com/" + d.get("redirectUrl", "").lstrip("/")
    where = d.get("displayText", "") + (f" ({km:.1f} km from sheet location)" if la is not None else "") + \
        (" - RERA number matches the sheet" if same_rera else "")
    updated = det.get("lastUpdatedDate")
    upd_txt = datetime.fromtimestamp(updated / 1000).strftime("%b %Y") if updated else "unknown"
    stale = bool(updated) and (time.time() - updated / 1000) / (30.4 * 86400) > PT_STALE_MONTHS
    cands, prices = [], []
    pos = det.get("possessionDate") or det.get("currentPhaseCompletionDate")
    if pos:
        dt = datetime.fromtimestamp(pos / 1000)
        ctxt = (f"PropTiger project data: possession {dt:%b %Y}, status {det.get('projectStatus', '')}, "
                f"RERA {rera or '-'}, listing updated {upd_txt} - {where}")
        cands.append(ex.Candidate(dt.year, dt.month, "month", "structured", 0.85, url, "proptiger.com", ctxt,
                                  False, True, "PropTiger data"))
    if det.get("shouldDisplayPrice", True):
        p_lo = det.get("minAgreementPrice") or det.get("minPrice")
        p_hi = det.get("maxAgreementPrice") or det.get("maxPrice") or p_lo
        per = int(det.get("minPricePerUnitArea") or 0)
        if p_lo and ex.MIN_PRICE <= p_lo <= p_hi <= ex.MAX_PRICE:
            note = f" - price not updated since {upd_txt}, may be old" if stale else ""
            prices.append(ex.PriceCand(float(p_lo), float(p_hi), "page data", url, "proptiger.com", not stale, 0.85,
                                       "PropTiger data", f"PropTiger builder price, updated {upd_txt}{note} - {where}",
                                       per if 1000 <= per <= 150000 else 0))
    return cands, prices, ""


async def scrape_building(ctx: Ctx, name: str, loc: str, city: str, rera_ids: list[str],
                          lat: str = "", lon: str = "") -> tuple[ex.Verdict, dict, dict]:
    a = ctx.args
    rera_mode, full_mode = a.mode == "rera", a.mode == "full"
    m = ex.NameMatcher(name, loc, city, tuple(rera_ids))
    cands, statuses, seen, all_results = [], ex.Counter(), set(), []
    pairs: list | None = [] if rera_mode else None
    prices: list | None = [] if full_mode else None
    verdict, n_q, pages_left, price = ex.Verdict(), 0, a.max_pages, {}
    if rera_mode:
        queries = build_rera_queries(name, loc, city, rera_ids)
    elif full_mode:
        queries = build_full_queries(name, loc, city, rera_ids)
    else:
        queries = build_queries(name, loc, city, rera_ids[0] if rera_ids else "")
    if full_mode and not a.no_proptiger:
        c, p, note = await proptiger_lookup(ctx, m, name, city, lat, lon, rera_ids)
        cands += c
        prices += p
        if c or p:
            verdict = ex.aggregate(cands, statuses)
            price = ex.aggregate_price(prices)
            if verdict.label in ("High", "Medium") and price["price_label"] in ("High", "Medium") \
                    and price["price_min"]:
                verdict.queries = 0
                return verdict, evidence_pack(m, [], cands), price
    for q in queries[: a.max_queries]:
        if ctx.store.get_search(q) is None:  # a cached search costs nothing
            if a.budget is not None and ctx.paid >= a.budget:
                if a.retry == "complete":
                    break  # a re-check never searches anew; it keeps what free/cached sources give
                raise BudgetReached
            ctx.paid += 1
        results = [r for r in await ctx.search.search(ctx.client, q)
                   if not any(dom in r.url.lower() for dom in ctx.excluded)]
        all_results += results
        n_q += 1
        for r in results:
            c, s = ex.from_snippet(r.title, r.snippet, r.url, m, pairs, prices)
            cands += c
            statuses.update(s)
        for r in pick_pages(results, m, seen, min(a.pages_per_query, pages_left)):
            seen.add(r.url)
            pages_left -= 1
            html = await ctx.fetcher.fetch(r.url)
            if html:
                c, s = await asyncio.to_thread(ex.from_page, html, r.url, m, pairs, prices)
                cands += c
                statuses.update(s)
        verdict = ex.aggregate(cands, statuses)
        if full_mode:
            price = ex.aggregate_price(namesake_check(verdict, cands, prices))
            # Both answers confirmed by a trusted site - no need to spend another search.
            if verdict.label == "High" and price["price_label"] in ("High", "Medium") and price["price_min"]:
                break
        # RERA mode keeps searching: the latest extension is often only on a later result.
        elif verdict.label == "High" and not rera_mode:
            break
    verdict.queries = n_q
    extra = ex.rera_summary(cands, pairs) if rera_mode else price
    return verdict, evidence_pack(m, all_results, cands), extra


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
    # "Mumbai Suburban District" / "Bengaluru Urban" -> "Mumbai" / "Bengaluru"
    city = re.sub(r"\s+(suburban|urban|rural|district|division)\b", "", city, flags=re.I).strip()
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
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
        # Laptops with Modern Standby go to sleep when the screen times out, whatever ES_SYSTEM_REQUIRED says;
        # keeping the display on is what holds them awake.
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        log.info("Keeping the PC awake while this runs (screen stays on; closing the lid still sleeps it)")


async def worker(ctx: Ctx, queue: asyncio.Queue, prog: Progress, export_cb):
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return
        idxs, name, loc, city, lat, lon, rera = item
        idx = idxs[0]
        try:
            if not name:
                data = ex.Verdict(note="empty building name").as_dict()
            elif not_a_project(name):
                data = {**ex.Verdict(note=f"Skipped: {not_a_project(name)}").as_dict(), "label": "Skipped"}
            else:
                if not city and lat and lon:
                    # Short addresses ("Thane West") carry no city; the coordinates do.
                    g_loc, city = await reverse_geocode(ctx, lat, lon)
                    loc = loc or g_loc
                rkey = row_key(name, loc, city)
                # Reuse an earlier run's result for the same project - except when retrying, where that
                # earlier result is exactly what is being redone.
                data = None if ctx.args.retry else ctx.store.by_key(rkey)
                if data:
                    data = {**data, "note": "same building as another row"}
                else:
                    verdict, pack, extra = await scrape_building(ctx, name, loc, city, rera, lat, lon)
                    data = {**verdict.as_dict(), **extra, "pack": pack, "locality_used": loc, "city_used": city}
            for i, n in enumerate(idxs):
                ctx.store.save(n, row_key(name, loc, city), data if i == 0 else
                               {**data, "note": (data.get("note") or "same project as row %d" % (idx + 2))})
            price = f" | price {data.get('price_display') or data.get('price_sqft') or '-'} [{data.get('price_label')}]" \
                if ctx.args.mode == "full" else ""
            prog.done += len(idxs) - 1  # repeats are done without a search
            prog.found += (len(idxs) - 1) * bool(data.get("found"))
            log.info("%s | %s (%s)%s -> %s [%s]%s", prog.tick(bool(data.get("found"))), name,
                     ", ".join(x for x in (loc, city) if x), f" x{len(idxs)}" if len(idxs) > 1 else "",
                     data.get("display") or "not found", data.get("label"), price)
            await export_cb(prog.done)
        except (AllBackendsDisabled, CreditsExhausted, PropTigerBlocked, BudgetReached):
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
    if re.fullmatch(r"\d{12,13}|\d{9,10}", v):
        ts = pd.Timestamp(int(v) // (1000 if len(v) >= 12 else 1), unit="s")  # epoch (ms) from an export
    elif re.fullmatch(r"\d{5}(\.\d+)?", v):
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


def sheet_price(v) -> float | None:
    """Price already in the sheet -> rupees. Handles plain rupees (8500000), '85 L', '1.2 Cr'."""
    v = cell(v).replace(",", "")
    if not v:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*(cr|crores?|l|lacs?|lakhs?))?", v.strip(), re.I)
    if not m:
        return None
    num, unit = float(m.group(1)), (m.group(2) or "").lower()
    if unit:
        num *= 1e7 if unit.startswith("c") else 1e5
    return num if num >= 1e5 else None


def compare_price(sheet_lo: float | None, sheet_hi: float | None, web_lo, web_hi) -> str:
    if not web_lo:
        return "No web price" if (sheet_lo or sheet_hi) else ""
    if not (sheet_lo or sheet_hi):
        return "No sheet price"
    s, w = (sheet_lo or sheet_hi), float(web_lo)
    diff = (w - s) / s * 100
    if abs(diff) <= 15:
        return "Match (within 15%)"
    return f"Web {'higher' if diff > 0 else 'lower'} by {abs(diff):.0f}%"


def full_row(d: dict | None, sheet_date, sheet_lo, sheet_hi) -> dict:
    """--mode full: possession + price, each with exactly one source URL. Low-confidence answers (only seen
    on unverified sites) are kept apart so they are never mistaken for confirmed ones."""
    d = d or {}
    done = bool(d)
    pos_ok = d.get("found") and d.get("label") in ("High", "Medium")
    pr_ok = d.get("price_label") in ("High", "Medium")
    pos_low = d.get("found") and d.get("label") == "Low"
    pr_low = d.get("price_label") == "Low"
    if d.get("label") == "Skipped":
        pos_txt = "Skipped - not a project"
    else:
        pos_txt = d.get("display") if pos_ok else ("Not found" if done else "")
    shown, verdict, diff = compare_dates(sheet_date, d.get("iso", "") if pos_ok else "")
    return {
        "Possession Date": pos_txt,
        "Possession (YYYY-MM)": d.get("iso", "") if pos_ok else "",
        "Project Status": d.get("status", "") if pos_ok else "",
        "Possession Confidence": d.get("label", "") if done else "",
        "Possession Source URL": d.get("best_url", "") if pos_ok else "",
        "Possession Evidence": d.get("evidence", "") if pos_ok else "",
        "RERA Date": d.get("rera_date", "") if pos_ok else "",
        "Price": d.get("price_display", "") if pr_ok else ("Not found" if done and not pr_low else ""),
        "Price Min (Rs)": d.get("price_min", "") if pr_ok else "",
        "Price Max (Rs)": d.get("price_max", "") if pr_ok else "",
        "Price per sq.ft": d.get("price_sqft", "") if pr_ok else "",
        "Price Confidence": d.get("price_label", "") if done else "",
        "Price Source URL": d.get("price_url", "") if pr_ok else "",
        "Price Evidence": d.get("price_evidence", "") if pr_ok else "",
        "Sheet Completion Date": shown,
        "Web vs Sheet (date)": verdict,
        "Date Difference (months)": diff,
        "Sheet Price": " - ".join(ex.fmt_inr(x) for x in dict.fromkeys(p for p in (sheet_lo, sheet_hi) if p)),
        "Web vs Sheet (price)": compare_price(sheet_lo, sheet_hi, d.get("price_min") if pr_ok else "",
                                              d.get("price_max") if pr_ok else ""),
        "Unconfirmed Possession (check)": d.get("display", "") if pos_low else "",
        "Unconfirmed Possession Source": d.get("best_url", "") if pos_low else "",
        "Unconfirmed Price (check)": (d.get("price_display") or d.get("price_sqft", "")) if pr_low else "",
        "Unconfirmed Price Source": d.get("price_url", "") if pr_low else "",
        "Other Dates Seen": d.get("alternatives", ""),
        "Other Prices Seen": d.get("price_alternatives", ""),
        "Searches Used": d.get("queries", ""),
        "Note": d.get("note", ""),
    }


def export(src: Path, sheet, df: pd.DataFrame, results: dict[int, dict], out_sheet: str, backup_done: list,
           claude: dict[int, dict] | None = None, existing_col=None, mode: str = "possession",
           summary_sheet: str = "Possession Summary", price_cols: tuple = (None, None)):
    claude = claude or {}
    rows, verdicts, rera_verdicts = [], [], []
    for i in range(len(df)):
        d = results.get(i)
        if mode == "full":
            def col(c):
                return df.iat[i, df.columns.get_loc(c)] if c is not None else ""
            r = full_row(d, parse_sheet_date(col(existing_col)), sheet_price(col(price_cols[0])),
                         sheet_price(col(price_cols[1])))
            if d is not None:
                verdicts.append(r["Web vs Sheet (date)"])
            rows.append(r)
            continue
        if d is None:
            r = {v: "" for v in OUT_COLS.values()}
        else:
            r = {OUT_COLS[k]: d.get(k, "") for k in OUT_COLS}
            if d.get("label") == "Skipped":
                r["Possession Date"] = "Skipped - not a project"
            elif not d.get("found"):
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
               ("Low confidence", labels.count("Low")), ("Not found", labels.count("Not found")),
               ("Skipped (PG / auction listings)", labels.count("Skipped"))]
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
    if mode == "full":
        vals = list(results.values())
        pl = [d.get("price_label", "") for d in vals]
        metrics[2:8] = [
            ("Possession confirmed (High/Medium)", sum(1 for d in vals if d.get("found")
                                                       and d.get("label") in ("High", "Medium"))),
            ("  of which High", labels.count("High")), ("  of which Medium", labels.count("Medium")),
            ("Possession unconfirmed (only unverified sites - check)", labels.count("Low")),
            ("Possession not found", labels.count("Not found")),
            ("Price confirmed (High/Medium)", pl.count("High") + pl.count("Medium")),
            ("  of which High (2+ sites agree)", pl.count("High")),
            ("Price unconfirmed (check)", pl.count("Low")), ("Price not found", pl.count("Not found")),
            ("Skipped (PG listings)", labels.count("Skipped")),
            ("Rule", "Confirmed = from RERA, 99acres/MagicBricks/Housing/Square Yards/PropTiger/Makaan etc. or the "
                     "project's own site, on a page about this one project in this city. NoBroker never used."),
        ]
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
                                                          "RERA Source": 45, "Possession Source URL": 45,
                                                          "Possession Evidence": 50, "Price Source URL": 45,
                                                          "Price Evidence": 50, "Other Prices Seen": 35,
                                                          "Unconfirmed Possession Source": 40,
                                                          "Unconfirmed Price Source": 40, "Note": 30}.get(col, 18)
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
    p.add_argument("--mode", choices=["possession", "rera", "full"], default="possession",
                   help="possession = find possession date; rera = check RERA completion date / extensions by RERA ID; "
                        "full = possession date AND price, each with its source URL")
    p.add_argument("--workers", type=int, default=6, help="Buildings processed in parallel")
    p.add_argument("--max-queries", type=int,
                   help="Max searches per building (default 3; 4 in rera mode). Possession mode stops early when sure")
    p.add_argument("--pages-per-query", type=int, default=3, help="Result pages opened per search")
    p.add_argument("--max-pages", type=int, default=8, help="Max pages opened per building")
    p.add_argument("--start", type=int, default=0, help="First data row (0-based) to process")
    p.add_argument("--limit", type=int, help="Only process this many rows (for a test run)")
    p.add_argument("--retry", choices=["missing", "low", "incomplete", "complete"],
                   help="Re-scrape rows already done: 'missing' = not found, 'low' = not found or Low, "
                        "'incomplete' (full mode) = possession or price not confirmed yet")
    p.add_argument("--browser", action="store_true", help="Use headless Chromium for pages that block scrapers")
    p.add_argument("--fallback-free", action="store_true",
                   help="When the paid search key runs out, carry on with free engines instead of stopping")
    p.add_argument("--backends", help="Comma list to restrict engines, e.g. bing,duckduckgo,serper")
    p.add_argument("--save-every", type=int, default=500, help="Write the sheet every N rows")
    p.add_argument("--export-only", action="store_true", help="Just write what's been scraped so far into the sheet")
    p.add_argument("--budget", type=int,
                   help="Stop after this many paid searches (credits) in this run; progress is saved")
    p.add_argument("--no-proptiger", action="store_true",
                   help="full mode: skip the free PropTiger lookup that runs before any paid search")
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
    if args.retry == "complete":
        # Re-check rows already marked complete with the current rules (cached searches: no credits).
        todo_idx = sorted(store.done_rows("incomplete") - store.done_rows("skipped-only"))
    else:
        done = store.done_rows(args.retry)  # once - it parses every saved result
        todo_idx = [i for i in range(args.start, len(df)) if i not in done]
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
                                            args.summary_sheet, (cols.get("min_price"), cols.get("max_price")))
                    log.info("Engines so far: %s", search.stats())

        # One search per distinct project: exports often repeat a project once per floor plan.
        groups: dict[str, list] = {}
        for i in todo_idx:
            f = row_fields(df, cols, i)
            k = row_key(f["name"], f["loc"], f["city"]) + "|" + ",".join(f["rera_ids"])
            if k in groups:
                groups[k][0].append(i)
            else:
                groups[k] = [[i], f["name"], f["loc"], f["city"], f["lat"], f["lon"], f["rera_ids"]]
        log.info("%d distinct projects to search (%d rows are repeats of one of them)",
                 len(groups), len(todo_idx) - len(groups))
        order = list(groups.values())
        if args.mode == "full" and args.max_queries:
            # Spend credits where they pay off most: big-city projects (Google has portal pages for them)
            # before small towns, and projects still missing a possession date before ones missing only a price.
            prev = store.all_results()

            def priority(g):
                d = prev.get(g[0][0]) or {}
                has_pos = bool(d.get("found") and d.get("label") in ("High", "Medium"))
                metro = bool(city_from_coords(g[4], g[5]))
                searched = bool(d.get("queries"))  # already had its paid search - redoing it finds nothing new
                return (searched, not metro, has_pos)
            order.sort(key=priority)
            n_metro_pos = sum(1 for g in order if priority(g) == (False, False, False))
            log.info("Paid-search order: %d big-city projects missing possession first, then the rest%s",
                     n_metro_pos, f" | budget {args.budget} credits" if args.budget is not None else "")
        queue: asyncio.Queue = asyncio.Queue()
        for g in order:
            queue.put_nowait(tuple(g))
        for _ in range(args.workers):
            queue.put_nowait(None)
        tasks = [asyncio.create_task(worker(ctx, queue, prog, export_cb)) for _ in range(args.workers)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            log.info("Engines: %s | paid searches this run: %d", search.stats(), ctx.paid)
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
        args.max_queries = {"rera": 4, "full": 1}.get(args.mode, 3)
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
        except BudgetReached:
            log.error("=" * 70)
            log.error("BUDGET REACHED: %d paid searches used - stopped and saved (%d of %d rows done).",
                      args.budget, len(store.all_results()), len(df))
            log.error("  To spend more, re-run the same command (each run gets a fresh --budget).")
            log.error("=" * 70)
            notify_user()
        except PropTigerBlocked as e:
            log.error("=" * 70)
            log.error("PROPTIGER IS BLOCKING THIS CONNECTION (%s) - stopped and saved (%d of %d rows done).",
                      e, len(store.all_results()), len(df))
            log.error("  Wait an hour (or switch network / hotspot), then re-run the SAME command to continue.")
            log.error("=" * 70)
            notify_user()
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
           cols.get("existing"), args.mode, args.summary_sheet, (cols.get("min_price"), cols.get("max_price")))


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
