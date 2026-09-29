"""Find Hyderabad projects that are not yet on NoBroker.

Set A - Telangana RERA register (you solve one captcha per district search; everything else is automatic)
Set B - Google Maps via Serper (apartment complexes / gated communities / commercial buildings, per locality)

    python hyd_discovery.py rera-list       # browser opens; type the captcha + Search for each district
    python hyd_discovery.py rera-details    # project pages, coordinates, 3 km filter, RERA certificates
    python hyd_discovery.py places          # Serper Maps search per NoBroker locality (1 credit per search)
    python hyd_discovery.py enrich          # optional: 1 web search per unmatched Set B building (builder/RERA/possession)
    python hyd_discovery.py build           # write the Excel output
    python hyd_discovery.py status          # progress so far

Progress is kept in hyd_discovery.sqlite next to the output file, so every step can be stopped and resumed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import math
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import numpy as np
import pandas as pd
import truststore
from bs4 import BeautifulSoup

import extractor as ex
from possession_scraper import keep_awake, load_env, notify_user, parse_coord, short_locality

log = logging.getLogger("hyd")

RERA_BASE = "https://rerait.telangana.gov.in"
SEARCH_URL = f"{RERA_BASE}/SearchList/Search"
# Districts that fall within greater Hyderabad; the 3 km NoBroker filter trims the rural parts.
DISTRICTS = [("25", "Hyderabad"), ("24", "Ranga Reddy"), ("22", "Medchal-Malkajgiri"), ("11", "Sangareddy"),
             ("20", "Yadadri Bhuvanagiri")]
KEEP_TYPES = ("residential", "commercial", "mixed")
PLACE_TERMS = ["apartment complex", "gated community", "commercial building"]
PLACE_CATEGORIES = re.compile(r"apartment|housing|condominium|residential|villa|gated|township|commercial building"
                              r"|business park|office building|it park|business center|tech park", re.I)
RERA_ID_RE = re.compile(r"\bP0\d{10}\b")
HYD_BOX = (16.9, 18.0, 77.9, 79.1)  # lat_min, lat_max, lon_min, lon_max


# ----------------------------------------------------------------------------- storage

class DB:
    def __init__(self, path: Path):
        self.c = sqlite3.connect(path, timeout=60, check_same_thread=False)
        self.lock = threading.RLock()
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript("""
            CREATE TABLE IF NOT EXISTS rera_list(key TEXT PRIMARY KEY, district TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS district_done(district TEXT PRIMARY KEY, total INTEGER);
            CREATE TABLE IF NOT EXISTS rera_detail(key TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS rera_cert(key TEXT PRIMARY KEY, rera_no TEXT);
            CREATE TABLE IF NOT EXISTS geo(q TEXT PRIMARY KEY, lat REAL, lon REAL);
            CREATE TABLE IF NOT EXISTS place_query(q TEXT PRIMARY KEY, n INTEGER);
            CREATE TABLE IF NOT EXISTS places(cid TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS enrich(cid TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS listing_query(q TEXT PRIMARY KEY, n INTEGER);
            CREATE TABLE IF NOT EXISTS listings(cid TEXT PRIMARY KEY, data TEXT);
        """)

    def put(self, table: str, key: str, data, col="data"):
        with self.lock:
            self.c.execute(f"INSERT OR REPLACE INTO {table}(" + ("key" if table.startswith("rera") else "cid") +
                           f", {col}) VALUES(?,?)",
                           (key, json.dumps(data, ensure_ascii=False) if col == "data" else data))
            self.c.commit()

    def execute(self, sql: str, params=()):
        with self.lock:
            cur = self.c.execute(sql, params)
            rows = cur.fetchall()
            self.c.commit()
            return rows

    def all(self, table: str) -> dict:
        kcol = "key" if table.startswith("rera") else "cid"
        return {k: json.loads(v) for k, v in self.c.execute(f"SELECT {kcol}, data FROM {table}")}


def ask(msg: str):
    log.info("")
    log.info(">>> %s", msg)
    notify_user()


# ----------------------------------------------------------------------------- NoBroker reference

def load_nobroker(path: str, sheet: str) -> pd.DataFrame:
    d = pd.read_excel(path, sheet_name=sheet, dtype=str, keep_default_na=False)
    d = d[d["city"].str.strip().str.lower().isin(["hyderabad", "secunderabad"])].copy()
    lat = pd.to_numeric(d["latitude"].map(parse_coord), errors="coerce")
    lon = pd.to_numeric(d["longitude"].map(parse_coord), errors="coerce")
    swapped = (lat > 70) & (lon < 30)
    lat, lon = lat.where(~swapped, lon), lon.where(~swapped, lat)
    ok = lat.between(HYD_BOX[0], HYD_BOX[1]) & lon.between(HYD_BOX[2], HYD_BOX[3])
    d = d.assign(lat=lat, lon=lon)[ok]
    d["rera_norm"] = d["rera_id"].str.upper().str.extract(r"(P0\d{10})", expand=False).fillna("")
    log.info("NoBroker Hyderabad reference: %d projects with usable coordinates (%d with RERA IDs)",
             len(d), (d["rera_norm"] != "").sum())
    return d.reset_index(drop=True)


def km_to_all(lat: float, lon: float, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    p1, p2 = np.radians(lat), np.radians(lats)
    dphi, dl = p2 - p1, np.radians(lons - lon)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 6371 * 2 * np.arcsin(np.sqrt(a))


_NAME_GENERIC = ex.GENERIC | ex.STOPWORDS | {"by", "m", "s", "ms", "pvt", "private", "ltd", "llp", "infra",
                                             "developers", "constructions", "projects", "group", "the"}


def name_tokens(n: str) -> list[str]:
    return [t for t in ex.norm(n).split() if t not in _NAME_GENERIC]


def name_sim(a: str, b: str) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = " ".join(ta), " ".join(tb)
    jac = len(set(ta) & set(tb)) / len(set(ta) | set(tb))
    seq = SequenceMatcher(None, sa, sb).ratio()
    contain = 0.9 if min(len(sa), len(sb)) >= 5 and (sa in sb or sb in sa) else 0.0
    return max(jac, seq, contain)


class Footprint:
    def __init__(self, nb: pd.DataFrame):
        self.nb = nb
        self.lats, self.lons = nb["lat"].to_numpy(float), nb["lon"].to_numpy(float)
        self.rera = set(nb["rera_norm"]) - {""}
        self.index = NameIndex([{"name": n} for n in nb["building_name"]])

    def check(self, name: str, lat: float | None, lon: float | None, rera_no: str = "",
              match_km: float = 2.0) -> dict:
        """Nearest NoBroker project, distance, and whether this looks already onboarded."""
        out = {"nearest_nb": "", "nearest_km": None, "onboarded": "", "onboarded_as": ""}
        if rera_no and rera_no in self.rera:
            hit = self.nb[self.nb["rera_norm"] == rera_no].iloc[0]
            out.update(onboarded="Yes (same RERA number)", onboarded_as=hit["building_name"])
        if lat is None or lon is None:
            if not out["onboarded"]:  # no location to compare: accept only a near-identical name
                hit = self.index.best(name, None, None, km=0, min_sim=0.9)
                if hit:
                    out.update(onboarded="Yes (same name, location unknown)", onboarded_as=hit["name"])
            return out
        dist = km_to_all(lat, lon, self.lats, self.lons)
        i = int(dist.argmin())
        out.update(nearest_nb=self.nb.at[i, "building_name"], nearest_km=round(float(dist[i]), 2))
        if not out["onboarded"]:
            for j in np.where(dist <= match_km)[0]:
                s = name_sim(name, self.nb.at[j, "building_name"])
                if s >= 0.8:
                    out.update(onboarded=f"Yes (name match {s:.2f}, {dist[j]:.1f} km)",
                               onboarded_as=self.nb.at[j, "building_name"])
                    break
        return out


# ----------------------------------------------------------------------------- Set A: RERA list (browser)

def parse_list(html: str, district: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    if not tables:
        return []
    table = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = []
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        cert = tr.find("a", onclick=re.compile(r"showFile\("))
        detail = tr.find("a", href=re.compile(r"PrintPreview"))
        qstr = cert.get("data-qstr", "") if cert else ""
        app = ""
        if qstr:
            try:
                app = parse_qs(base64.b64decode(qstr + "=" * (-len(qstr) % 4)).decode()).get("AppID", [""])[0]
            except (ValueError, UnicodeDecodeError):
                pass
        name, promoter = tds[1].get_text(" ", strip=True), tds[2].get_text(" ", strip=True)
        html_row = str(tr)
        m_map = re.search(r"view_on_map\('([^']+)'\)", html_row)
        m_dir = re.search(r"direction_on_map\('([\d.]+)','([\d.]+)'\)", html_row)
        key = f"app{app}" if app else "h" + base64.b16encode(f"{district}|{name}|{promoter}".encode()).decode()[:40]
        rows.append({"key": key, "district": district, "name": name, "promoter": promoter,
                     "last_modified": tds[4].get_text(" ", strip=True)[:10],
                     "detail_url": RERA_BASE + detail["href"] if detail else "", "cert_qstr": qstr,
                     "extension_cert": bool(tr.find("a", onclick=re.compile(r"showExtensionFile"))),
                     "map_id": m_map.group(1) if m_map else "",
                     "dir_lat": float(m_dir.group(1)) if m_dir else None,
                     "dir_lon": float(m_dir.group(2)) if m_dir else None})
    return rows


def page_info(page) -> tuple[int, int, int] | None:
    try:
        t = page.inner_text("body", timeout=3000)
    except Exception:
        return None
    m = re.search(r"Total Records:\s*(\d+).*?Page:\s*(\d+)\s*of\s*(\d+)", t, re.S)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def wait_for_page(page, want: int, timeout: float) -> tuple[int, int, int] | None:
    end = time.time() + timeout
    while time.time() < end:
        page.wait_for_timeout(1000)
        info = page_info(page)
        if info and info[1] == want:
            return info
    return None


def cmd_rera_list(args, db: DB):
    from playwright.sync_api import sync_playwright
    done = {d for (d,) in db.c.execute("SELECT district FROM district_done")}
    todo = [(i, n) for i, n in DISTRICTS if n not in done]
    if not todo:
        log.info("All districts already collected. Next: python hyd_discovery.py rera-details")
        return
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        for did, dname in todo:
            page.goto(SEARCH_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            try:
                page.click("#btnAdvance", timeout=5000)
                page.wait_for_timeout(800)
                page.select_option("select[name=District]", did, timeout=5000)
                page.evaluate("() => { const e = document.getElementById('PageSize'); if (e) e.value = '1000'; }")
                ask(f"[{dname}] District is selected. Type the captcha in the browser and click Search.")
            except Exception:
                ask(f"[{dname}] Open 'Advanced Search', pick District = {dname}, type the captcha, click Search.")
            info = wait_for_page(page, 1, timeout=3600)
            if not info:
                log.error("No results for %s within 60 minutes - skipping it for now (re-run to retry).", dname)
                continue
            total, _, last = info
            log.info("[%s] %d projects over %d page(s)", dname, total, last)
            got = 0
            while True:
                rows = parse_list(page.content(), dname)
                for r in rows:
                    db.put("rera_list", r["key"], r)
                got += len(rows)
                cur = info[1]
                log.info("[%s] page %d/%d saved (%d rows so far)", dname, cur, last, got)
                if cur >= last:
                    break
                page.click("#btnNext")
                nxt = wait_for_page(page, cur + 1, timeout=25)
                if not nxt:
                    ask(f"[{dname}] The portal wants the captcha again for page {cur + 1}: type it and click Next.")
                    nxt = wait_for_page(page, cur + 1, timeout=900)
                if not nxt:
                    log.error("[%s] stuck on page %d - re-run to redo this district.", dname, cur)
                    break
                info = nxt
            if info[1] >= last:
                db.c.execute("INSERT OR REPLACE INTO district_done VALUES(?,?)", (dname, total))
                db.c.commit()
                log.info("[%s] complete: %d projects listed", dname, got)
        browser.close()
    log.info("Next: python hyd_discovery.py rera-details")


# ----------------------------------------------------------------------------- Set A: details, coords, certificates

def _field(text: str, label: str, nxt: str) -> str:
    m = re.search(rf"{label}\s+(.*?)\s+{nxt}", text)
    v = m.group(1).strip() if m else ""
    if re.match(r"(Street|Locality|Pin Code|Mandal|Village|Land ?mark)\b", v):  # the field itself was blank
        v = ""
    return "" if v.lower() in ("na", "n/a", "-") or len(v) > 80 else v


def parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    proj = text[text.find("Project Information"):] if "Project Information" in text else text
    addr_start = proj.find("Address Details")
    addr = proj[addr_start: addr_start + 400] if addr_start >= 0 else ""
    date = r"(\d{2}/\d{2}/\d{4})"
    g = lambda rx: (re.search(rx, proj) or [None, ""])[1]
    return {
        "project_type": _field(proj, "Project Type", r"(?:Are there|Project Status|Is the)"),
        "status": _field(proj, "Project Status", r"(?:Approved Date|Proposed Date)"),
        "approved": g(rf"Approved Date\s+{date}"),
        "proposed_completion": g(rf"Proposed Date of Completion\s+{date}"),
        "revised_completion": g(rf"Revised Proposed Date of Completion\s+{date}"),
        "district": _field(addr, "District", "Mandal"),
        "mandal": _field(addr, "Mandal", "Village/City/Town"),
        "village": _field(addr, "Village/City/Town", "Street"),
        "street": _field(addr, "Street", "Locality"),
        "locality": _field(addr, "Locality", "Pin Code"),
        "pin": g(r"Address Details.{0,300}?Pin Code\s+(\d{6})"),
        "extension_reg": g(r"Registration No\s*:\s*(EXT\d+)"),
    }


def parse_application(txt: str) -> dict:
    """Fields from the RERA application PDF. The builder's office address comes first; the project's own
    address follows the land details (Boundaries / Mortgage Area), so it is read from there."""
    txt = re.sub(r"\s+", " ", txt.replace("\xad", "-"))
    date = r"(\d{2}/\d{2}/\d{4})"
    g = lambda rx, src=txt: (re.search(rx, src) or [None, ""])[1]
    after_land = txt[txt.find("Boundaries"):] if "Boundaries" in txt else ""
    m = re.search(r"State\s+Telangana\s+District.{0,300}?Pin Code\s+\d{6}", after_land)
    addr = m.group(0) if m else ""
    return {
        "project_type": g(r"Project Type\s+(Residential|Commercial|Plotted Development|Mixed Development"
                          r"(?:\s*\(Residential\s*&(?:amp;)?\s*Commercial\))?|Mixed)"),
        "status": _field(txt, "Project Status", r"(?:Approved Date|Proposed Date)"),
        "approved": g(rf"Approved Date\s+{date}"),
        "proposed_completion": g(rf"Proposed Date of Completion\s+{date}"),
        "revised_completion": g(rf"Revised Proposed Date of Completion\s+{date}"),
        "district": _field(addr, "District", "Mandal"),
        "mandal": _field(addr, "Mandal", "Village/City/Town"),
        "village": _field(addr, "Village/City/Town", r"(?:Street|Locality|Pin Code)"),
        "street": _field(addr, "Street", r"(?:Locality|Pin Code)"),
        "locality": _field(addr, "Locality", "Pin Code"),
        "pin": g(r"Pin Code\s+(\d{6})", addr),
        "extension_reg": g(r"Registration No\s*:\s*(EXT\d+)"),
    }


def fetch_application(client: httpx.Client, qstr: str) -> dict:
    """The application PDF is addressed by the certificate's ID, which (unlike the 'View' link) does not expire."""
    from pypdf import PdfReader
    t = client.post(f"{RERA_BASE}/SearchList/showFileApplicationPreviewIframe", json={"ID": qstr, "Preview": "1"}).text
    m = re.search(r'src="([^"]+)"', t)
    if not m:
        raise ValueError("no application preview")
    pdf = client.get(RERA_BASE + m.group(1)).content
    if not pdf.startswith(b"%PDF"):
        raise ValueError("application preview is not a PDF")
    pages = PdfReader(io.BytesIO(pdf)).pages
    d = parse_application(" ".join(pg.extract_text() or "" for pg in pages[:5]))
    if not d["pin"] and len(pages) > 5:  # long applications: the project address sits further in
        d = parse_application(" ".join(pg.extract_text() or "" for pg in pages[:10]))
    if not d["project_type"]:
        raise ValueError("could not read the project type")
    return d


def geocode(client: httpx.Client, db: DB, parts: list[str], pin: str, last: list) -> tuple[float, float, str] | None:
    """OpenStreetMap Nominatim (free, max 1 request/second). Locality first, then PIN code."""
    tries = []
    locality, district = (parts + ["", ""])[:2]
    if locality:
        if district:
            tries.append(("locality", {"q": f"{locality}, {district}, Telangana, India"}))
        tries.append(("locality", {"q": f"{locality}, Hyderabad, Telangana, India"}))
    if pin:
        tries.append(("PIN code", {"postalcode": pin, "country": "India"}))
    for label, params in tries:
        key = json.dumps(params, sort_keys=True)
        found = db.execute("SELECT lat, lon FROM geo WHERE q=?", (key,))
        hit = found[0] if found else None
        if hit is None:
            wait = last[0] + 1.1 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last[0] = time.monotonic()
            try:
                r = client.get("https://nominatim.openstreetmap.org/search",
                               params={**params, "format": "jsonv2", "limit": 1},
                               headers={"User-Agent": "hyd-project-discovery/1.0"})
                j = r.json() if r.status_code == 200 else []
            except (httpx.HTTPError, ValueError):
                j = []
            hit = (float(j[0]["lat"]), float(j[0]["lon"])) if j else (None, None)
            db.execute("INSERT OR REPLACE INTO geo VALUES(?,?,?)", (key, *hit))
        if hit[0] is not None and HYD_BOX[0] <= hit[0] <= HYD_BOX[1] and HYD_BOX[2] <= hit[1] <= HYD_BOX[3]:
            return hit[0], hit[1], label
    return None


def cert_rera_no(client: httpx.Client, qstr: str) -> str:
    from pypdf import PdfReader
    r = client.post(f"{RERA_BASE}/SearchList/ShowCertificateIframe", json={"ID": qstr})
    m = re.search(r'src="([^"]+GetShowCertificateFileContent[^"]+)"', r.text)
    if not m:
        return ""
    pdf = client.get(RERA_BASE + m.group(1)).content
    if not pdf.startswith(b"%PDF"):
        return ""
    txt = " ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages[:2])
    ids = RERA_ID_RE.findall(txt.replace(" ", ""))
    return ids[0] if ids else ""


def cmd_rera_details(args, db: DB, fp: Footprint):
    """Project pages in parallel; map lookups (1/sec, the free service's limit) run alongside; then certificates."""
    import queue
    listed = db.all("rera_list")
    details = db.all("rera_detail")
    todo = [(k, r) for k, r in listed.items() if k not in details and r.get("cert_qstr")]
    log.info("%d projects listed, %d already have details, %d to fetch with %d parallel workers",
             len(listed), len(details), len(todo), args.workers)
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    rc = httpx.Client(verify=ctx, headers={"User-Agent": "Mozilla/5.0"}, timeout=90, follow_redirects=True,
                      limits=httpx.Limits(max_connections=args.workers + 2))
    gc = httpx.Client(timeout=30)
    rc.get(SEARCH_URL)
    geo_q: queue.Queue = queue.Queue()
    counts = {"fetched": 0, "geocoded": 0}
    count_lock = threading.Lock()

    def finish(key, row, d):
        chk = fp.check(row["name"], d.get("lat"), d.get("lon"))
        d["nearest_km"] = chk["nearest_km"]
        d["in_footprint"] = chk["nearest_km"] is not None and chk["nearest_km"] <= args.radius_km
        db.put("rera_detail", key, d)

    def fetch(item):
        key, row = item
        if not row.get("cert_qstr"):
            return
        try:
            d = fetch_application(rc, row["cert_qstr"])
        except Exception as e:  # network error or unreadable PDF: nothing saved, retried on the next run
            log.warning("%s: %s - will retry next run", row["name"], e)
            return
        d["type_ok"] = any(t in d.get("project_type", "").lower() for t in KEEP_TYPES)
        if not d["type_ok"]:
            db.put("rera_detail", key, d)
        elif row.get("dir_lat"):
            d.update(lat=row["dir_lat"], lon=row["dir_lon"], coord_source="RERA map pin")
            finish(key, row, d)
        else:
            geo_q.put((key, row, d))
        with count_lock:
            counts["fetched"] += 1
            if counts["fetched"] % 100 == 0:
                log.info("pages %d/%d fetched | map lookups waiting: %d", counts["fetched"], len(todo), geo_q.qsize())

    def geocoder():
        last = [0.0]
        while True:
            item = geo_q.get()
            if item is None:
                return
            key, row, d = item
            g = geocode(gc, db, [d.get("locality") or d.get("village") or d.get("street") or "",
                                 d.get("district") or ""], d.get("pin", ""), last)
            if g:
                d.update(lat=g[0], lon=g[1], coord_source=f"approx. from {g[2]}")
            finish(key, row, d)
            counts["geocoded"] += 1
            if counts["geocoded"] % 100 == 0:
                log.info("map lookups done: %d | still waiting: %d", counts["geocoded"], geo_q.qsize())

    gt = threading.Thread(target=geocoder, daemon=True)
    gt.start()
    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(fetch, todo))
    log.info("All pages fetched. Finishing %d map lookups (about 1 per second)...", geo_q.qsize())
    geo_q.put(None)
    gt.join()

    certs = {k for (k,) in db.execute("SELECT key FROM rera_cert")}
    need = [(k, listed[k]) for k, d in db.all("rera_detail").items()
            if d.get("in_footprint") and k not in certs and listed.get(k, {}).get("cert_qstr")]
    log.info("Reading RERA numbers from %d certificates...", len(need))

    def cert(item):
        k, row = item
        try:
            rn = cert_rera_no(rc, row["cert_qstr"])
        except Exception as e:  # network errors and unreadable PDFs alike: retry on the next run
            log.warning("certificate for %s: %s", row["name"], e)
            return
        db.execute("INSERT OR REPLACE INTO rera_cert VALUES(?,?)", (k, rn))

    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(cert, need))
    rc.close()
    gc.close()
    log.info("Details done. Next: python hyd_discovery.py places   (or build)")


# ----------------------------------------------------------------------------- Set B: Serper Maps

class CreditsOut(Exception):
    pass


class SerperFailed(Exception):
    """Serper could not answer this one request (server error / timeout) after retries."""


def serper(client: httpx.Client, endpoint: str, payload: dict) -> dict:
    last = ""
    for attempt in range(2):
        try:
            r = client.post(f"https://google.serper.dev/{endpoint}", json=payload,
                            headers={"X-API-KEY": os.environ["SERPER_API_KEY"], "Content-Type": "application/json"})
        except httpx.HTTPError as e:
            last = str(e)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            last = "rate limited"
            continue
        if r.status_code in (401, 402, 403) or (r.status_code == 400 and "credit" in r.text.lower()):
            raise CreditsOut(f"HTTP {r.status_code}: {r.text[:150]}")
        if r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise SerperFailed(f"HTTP {r.status_code}: {r.text[:150]}")
        return r.json()
    raise SerperFailed(last)


def stop_for_credits(e: Exception):
    log.error("=" * 70)
    log.error("SEARCH CREDITS USED UP - progress saved. (%s)", e)
    log.error("  Put a new SERPER_API_KEY in .env, then re-run the SAME command to continue.")
    log.error("=" * 70)
    notify_user()


def localities(nb: pd.DataFrame, db: DB) -> list[str]:
    seen, out = set(), []
    names = [short_locality(a, "hyderabad") for a in nb["locality"]]
    names += [d.get("locality", "") for d in db.all("rera_detail").values() if d.get("in_footprint")]
    for n in names:
        k = ex.norm(n)
        if k and k not in seen and k not in ("hyderabad", "secunderabad", "telangana") and len(k) > 2:
            seen.add(k)
            out.append(n.strip())
    return out


def cmd_places(args, db: DB, nb: pd.DataFrame):
    """Serper Maps search per locality and term. Serper's page 2 for Maps is unreliable (slow, then HTTP 500),
    so only first pages (10 places) are fetched unless --pages says otherwise."""
    if not os.getenv("SERPER_API_KEY"):
        sys.exit("SERPER_API_KEY missing in .env")
    locs = localities(nb, db)
    done = {q for (q,) in db.execute("SELECT q FROM place_query")}
    queries = [f"{t} in {l} Hyderabad" for l in locs for t in args.terms]
    todo = [q for q in queries if q not in done]
    log.info("%d localities x %d terms = %d searches (%d done, %d to go, ~%d credits), %d at a time",
             len(locs), len(args.terms), len(queries), len(queries) - len(todo), len(todo),
             len(todo) * args.pages, args.search_workers)
    stop = threading.Event()
    state = {"n": 0, "fails": 0, "credits_error": None}
    lock = threading.Lock()
    client = httpx.Client(timeout=40, limits=httpx.Limits(max_connections=args.search_workers + 2))

    def one(q: str):
        if stop.is_set():
            return
        got = 0
        for page in range(1, args.pages + 1):
            try:
                j = serper(client, "places", {"q": q, "gl": "in", "hl": "en", "page": page})
            except CreditsOut as e:
                state["credits_error"] = e
                stop.set()
                return
            except SerperFailed as e:
                with lock:
                    state["fails"] += 1
                    too_many = state["fails"] >= 25
                log.warning("Serper couldn't answer %r page %d (%s) - skipped", q, page, e)
                if too_many:
                    stop.set()
                if page == 1:
                    return  # not marked done, so the next run tries it again
                break
            items = j.get("places", [])
            with db.lock:
                db.c.executemany("INSERT OR REPLACE INTO places(cid, data) VALUES(?,?)",
                                 [(str(p["cid"]), json.dumps({**p, "query": q}, ensure_ascii=False))
                                  for p in items if p.get("cid")])
                db.c.commit()
            got += len(items)
            if len(items) < 10:
                break
        db.execute("INSERT OR REPLACE INTO place_query VALUES(?,?)", (q, got))
        with lock:
            state["n"] += 1
            if state["n"] % 50 == 0:
                total = db.execute("SELECT COUNT(*) FROM places")[0][0]
                log.info("places searches %d/%d | %d unique places so far", state["n"], len(todo), total)

    try:
        with ThreadPoolExecutor(args.search_workers) as pool:
            list(pool.map(one, todo))
    finally:
        client.close()
    if state["credits_error"]:
        stop_for_credits(state["credits_error"])
        return
    if stop.is_set():
        log.error("Serper failed 25 times - it seems to be having problems. Progress saved; re-run later.")
        notify_user()
        return
    log.info("Places done: %d unique places. Next: python hyd_discovery.py enrich (optional) or build",
             db.execute("SELECT COUNT(*) FROM places")[0][0])


def set_a_rows(db: DB, fp: Footprint, radius: float) -> list[dict]:
    listed, details = db.all("rera_list"), db.all("rera_detail")
    certs = dict(db.c.execute("SELECT key, rera_no FROM rera_cert"))
    enrich = db.all("enrich")
    rows = []
    for key, r in listed.items():
        d = details.get(key, {})
        if not d.get("type_ok") or not d.get("in_footprint"):
            continue
        rn = certs.get(key, "")
        chk = fp.check(r["name"], d.get("lat"), d.get("lon"), rn)
        e = enrich.get("a:" + key, {})
        rows.append({"key": key, "name": r["name"], "builder": r["promoter"], "rera_no": rn, **d, **chk,
                     "extension_cert": "Yes" if r.get("extension_cert") else "No", "last_modified": r["last_modified"],
                     "detail_url": r["detail_url"], "price": e.get("price", ""), "price_sqft": e.get("price_sqft", ""),
                     "market_possession": e.get("possession", "")})
    return rows


def match_a(name: str, lat: float, lon: float, a_rows: list[dict], km: float = 1.5) -> dict | None:
    best, best_s = None, 0.0
    for a in a_rows:
        if a.get("lat") is None:
            continue
        if math.dist((lat, lon), (a["lat"], a["lon"])) * 111 > km:
            continue
        s = name_sim(name, a["name"])
        if s > best_s:
            best, best_s = a, s
    return best if best_s >= 0.75 else None


def set_b_rows(db: DB, fp: Footprint, a_rows: list[dict], radius: float) -> list[dict]:
    enrich = db.all("enrich")
    rows = []
    for cid, p in db.all("places").items():
        if not PLACE_CATEGORIES.search(p.get("category", "")):
            continue
        lat, lon = p.get("latitude"), p.get("longitude")
        chk = fp.check(p["title"], lat, lon, match_km=1.0)
        if chk["nearest_km"] is None or chk["nearest_km"] > radius:
            continue
        row = {"cid": cid, "name": p["title"], "category": p.get("category", ""), "address": p.get("address", ""),
               "lat": lat, "lon": lon, "rating": p.get("rating"), "reviews": p.get("ratingCount"), **chk,
               "maps_url": f"https://maps.google.com/?cid={cid}", "found_via": p.get("query", "")}
        a = match_a(p["title"], lat, lon, a_rows)
        if a:
            row.update(builder=a["builder"], rera_no=a["rera_no"], possession=a.get("revised_completion") or
                       a.get("proposed_completion", ""), project_type=a.get("project_type", ""),
                       details_from=f"RERA register ({a['name']})", in_set_a="Yes",
                       price=a.get("price") or enrich.get(cid, {}).get("price", ""),
                       price_sqft=a.get("price_sqft") or enrich.get(cid, {}).get("price_sqft", ""))
            if a.get("onboarded") and not row["onboarded"]:
                row.update(onboarded=a["onboarded"], onboarded_as=a["onboarded_as"])
        elif cid in enrich:
            e = enrich[cid]
            row.update(builder=e.get("builder", ""), rera_no=e.get("rera_no", ""), possession=e.get("possession", ""),
                       details_from="web search", in_set_a="No", price=e.get("price", ""),
                       price_sqft=e.get("price_sqft", ""))
            if e.get("rera_no") and e["rera_no"] in fp.rera and not row["onboarded"]:
                row.update(onboarded="Yes (same RERA number)")
        else:
            row["in_set_a"] = "No"
        rows.append(row)
    return rows


_UNITS = r"cr|crores?|l|lacs?|lakhs?"
_PRICE_RANGE = re.compile(rf"(?:₹|rs\.?|inr)\s*(\d+(?:\.\d+)?)\s*({_UNITS})?\s*(?:-|–|to)\s*"
                          rf"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*({_UNITS})\b", re.I)
_PRICE_ONE = re.compile(rf"(?:₹|rs\.?|inr)\s*(\d+(?:\.\d+)?)\s*({_UNITS})\b", re.I)
_PER_SQFT = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]{3,7})\s*(?:/|per)\s*sq\.?\s*ft", re.I)


def _amt(num: str, unit: str) -> tuple[float, str]:
    u = "Cr" if unit.lower().startswith("c") else "L"
    return float(num) * (100 if u == "Cr" else 1), f"₹{num} {u}"


def extract_price(texts: list[str]) -> tuple[str, str]:
    """('₹1.08 Cr - ₹2.14 Cr', '₹7,500/sq.ft') from listing snippets; empty strings when absent."""
    rng, singles, sqft = "", [], []
    for t in texts:
        if not rng:
            m = _PRICE_RANGE.search(t)
            if m:
                lo = _amt(m.group(1), m.group(2) or m.group(4))
                hi = _amt(m.group(3), m.group(4))
                if 5 <= lo[0] <= hi[0] <= 50000:
                    rng = f"{lo[1]} - {hi[1]}"
        for m in _PRICE_ONE.finditer(t):
            a = _amt(m.group(1), m.group(2))
            if 5 <= a[0] <= 50000:
                singles.append(a)
        for m in _PER_SQFT.finditer(t):
            v = int(m.group(1).replace(",", ""))
            if 1500 <= v <= 60000:
                sqft.append(v)
    if not rng and singles:
        lo, hi = min(singles), max(singles)
        rng = f"{lo[1]} - {hi[1]}" if hi[0] > lo[0] * 1.05 else f"{lo[1]} onwards"
    per = f"₹{sorted(sqft)[len(sqft) // 2]:,}/sq.ft" if sqft else ""
    return rng, per


def web_lookup(c: httpx.Client, name: str, area: str) -> dict:
    """One Serper web search: builder, RERA number, possession date and price from matching snippets."""
    j = serper(c, "search", {"q": f"{name} {area} Hyderabad price possession RERA", "gl": "in", "num": 10})
    m = ex.NameMatcher(name, area, "Hyderabad")
    cands, rera, builder, texts = [], "", "", []
    for o in j.get("organic", []):
        title, snip, url = o.get("title", ""), o.get("snippet", ""), o.get("link", "")
        if "nobroker.in" in url or m.score(f"{title} {snip}") < 0.6:
            continue
        texts.append(f"{title} . {snip}")
        cands += ex.from_snippet(title, snip, url, m)[0]
        rera = rera or next(iter(RERA_ID_RE.findall(f"{title} {snip}")), "")
        b = re.search(r"\bby\s+([A-Z][\w&.'\- ]{2,40}?)(?=\s*(?:[|\-,:(]|in\b|at\b|$))", title)
        if b and not _MONTH_START.match(b.group(1)):
            builder = builder or b.group(1).strip()
    v = ex.aggregate(cands, ex.Counter())
    price, per_sqft = extract_price(texts)
    return {"builder": builder, "rera_no": rera, "possession": v.display if v.found else "",
            "confidence": v.label, "price": price, "price_sqft": per_sqft}


def cmd_enrich(args, db: DB, fp: Footprint):
    """1 credit per project: price for Set A (the register has none); builder/RERA/possession/price for Set B."""
    if not os.getenv("SERPER_API_KEY"):
        sys.exit("SERPER_API_KEY missing in .env")
    done = set(db.all("enrich"))
    a_rows = set_a_rows(db, fp, args.radius_km)
    todo = []
    if args.only in ("a", "both"):
        set_c_rows(db, fp, a_rows, args.radius_km)  # fills listing_price on RERA projects found on listing sites
        todo += [("a:" + r["key"], r["name"], r.get("locality") or r.get("village") or r.get("mandal") or "")
                 for r in a_rows if not r["onboarded"] and not r.get("listing_price")]
    if args.only in ("b", "both"):
        todo += [(r["cid"], r["name"], r["address"] or r["found_via"].split(" in ", 1)[-1].replace(" Hyderabad", ""))
                 for r in set_b_rows(db, fp, a_rows, args.radius_km)
                 if not r["onboarded"] and r.get("in_set_a") == "No"]
    todo = [t for t in todo if t[0] not in done]
    log.info("%d projects to look up on the web (1 credit each)", len(todo))
    with httpx.Client(timeout=30) as c:
        try:
            for n, (key, name, area) in enumerate(todo, 1):
                try:
                    db.put("enrich", key, web_lookup(c, name, area))
                except SerperFailed as e:
                    log.warning("lookup failed for %s (%s) - will retry next run", name, e)
                    continue
                if n % 25 == 0:
                    log.info("enriched %d/%d", n, len(todo))
        except CreditsOut as e:
            stop_for_credits(e)
            return
    log.info("Enrichment done. Next: python hyd_discovery.py build")


# ----------------------------------------------------------------------------- Set C: listing sites via Google

LISTING_SITES = {
    "99acres": ("99acres.com", re.compile(r"99acres\.com/(?P<slug>[^/?#]+?)-npxid-r\d+")),
    "magicbricks": ("magicbricks.com", re.compile(r"magicbricks\.com/(?P<slug>[^/?#]+?)-pdpid-")),
    "housing": ("housing.com", re.compile(r"housing\.com/in/buy/projects/page/\d+-(?P<slug>[^/?#]+)")),
    "squareyards": ("squareyards.com",
                    re.compile(r"squareyards\.com/hyderabad-(?:residential|commercial)-property/(?P<slug>[^/]+)/\d+/project")),
}
_TITLE_NOISE = re.compile(r"\b(FAQs?|Price List|Prices?|Reviews?|Floor Plans?|Brochure|Resale|Photos|Overview|Rera)\b.*$",
                          re.I)


_MONTH_START = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(\s|$)|\d", re.I)


def _nice(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip(" -|:,")
    return t.title() if t.isupper() or t.islower() else t


def parse_listing(site: str, o: dict, known_locs: dict) -> dict | None:
    """One Google result -> project record, or None when it isn't an individual project page."""
    url, title, snip = o.get("link", ""), o.get("title", ""), o.get("snippet", "")
    m = LISTING_SITES[site][1].search(url)
    if not m:
        return None
    slug = m.group("slug")
    name = loc = builder = ""
    if site == "housing":
        parts = slug.split("-by-", 1)
        name = parts[0].replace("-", " ")
        if len(parts) > 1:
            builder = re.sub(r"-in-[a-z0-9-]+$", "", parts[1]).replace("-", " ")
        mt = re.search(r"\bin\s+([^,|-]+),\s*Hyderabad", title)
        loc = mt.group(1) if mt else ""
    elif site == "squareyards":
        bits = [b.strip() for b in title.split(" - ")[0].split(",")]
        name = bits[0]
        loc = bits[1] if len(bits) >= 3 else ""
    elif site == "magicbricks":
        mt = re.match(r"(.+?)\s+in\s+([^,|]+),\s*Hyderabad", title)
        name, loc = (mt.group(1), mt.group(2)) if mt else (title.split(",")[0], "")
    else:  # 99acres: "Prestige Beverly Hills Kokapet, Hyderabad" - locality is glued on the end
        head = _TITLE_NOISE.sub("", title.split(",")[0]).strip(" -")
        words = head.split()
        for k in (3, 2, 1):
            tail = ex.norm(" ".join(words[-k:])) if len(words) > k else ""
            if tail and tail in known_locs:
                name, loc = " ".join(words[:-k]), known_locs[tail]
                break
        else:
            name = head
    name = _TITLE_NOISE.sub("", name).strip()
    if len(ex.norm(name)) < 3:
        return None
    text = f"{title} . {snip}"
    b = re.search(r"\bby\s+([A-Z][\w&.'\- ]{2,40}?)(?=\s*(?:[|\-,:(]|in\b|at\b|$))", text)
    if b and not _MONTH_START.match(b.group(1)):
        builder = builder or b.group(1)
    matcher = ex.NameMatcher(name, loc, "Hyderabad")
    v = ex.aggregate(ex.from_snippet(title, snip, url, matcher)[0], ex.Counter())
    price, per_sqft = extract_price([text])
    return {"site": site, "name": _nice(name), "locality": _nice(loc), "builder": _nice(builder),
            "rera_no": next(iter(RERA_ID_RE.findall(text)), ""), "price": price, "price_sqft": per_sqft,
            "possession": v.display if v.found else "", "url": url}


def cmd_listings(args, db: DB, nb: pd.DataFrame):
    """Google search restricted to each listing site, per locality; keeps only individual project pages."""
    if not os.getenv("SERPER_API_KEY"):
        sys.exit("SERPER_API_KEY missing in .env")
    locs = localities(nb, db)
    known = {ex.norm(l): l for l in locs}
    done = {q for (q,) in db.execute("SELECT q FROM listing_query")}
    queries = [(site, l, f"site:{LISTING_SITES[site][0]} {l} Hyderabad project price possession")
               for l in locs for site in args.sites]
    todo = [t for t in queries if t[2] not in done]
    log.info("%d localities x %d sites = %d searches (%d done, %d to go, ~%d credits), %d at a time",
             len(locs), len(args.sites), len(queries), len(queries) - len(todo), len(todo), len(todo),
             args.search_workers)
    stop = threading.Event()
    state = {"n": 0, "fails": 0, "credits_error": None}
    lock = threading.Lock()
    client = httpx.Client(timeout=40, limits=httpx.Limits(max_connections=args.search_workers + 2))

    def one(item):
        site, loc, q = item
        if stop.is_set():
            return
        try:
            j = serper(client, "search", {"q": q, "gl": "in", "hl": "en", "num": 10})
        except CreditsOut as e:
            state["credits_error"] = e
            stop.set()
            return
        except SerperFailed as e:
            with lock:
                state["fails"] += 1
                if state["fails"] >= 25:
                    stop.set()
            log.warning("search failed %r (%s) - will retry next run", q, e)
            return
        recs = [r for r in (parse_listing(site, o, known) for o in j.get("organic", [])) if r]
        for r in recs:
            r["locality"] = r["locality"] or loc
            r["query_locality"] = loc
        with db.lock:
            db.c.executemany("INSERT OR REPLACE INTO listings(cid, data) VALUES(?,?)",
                             [(r["url"], json.dumps(r, ensure_ascii=False)) for r in recs])
            db.c.execute("INSERT OR REPLACE INTO listing_query VALUES(?,?)", (q, len(recs)))
            db.c.commit()
        with lock:
            state["n"] += 1
            if state["n"] % 50 == 0:
                log.info("listing searches %d/%d | %d project pages so far", state["n"], len(todo),
                         db.execute("SELECT COUNT(*) FROM listings")[0][0])

    try:
        with ThreadPoolExecutor(args.search_workers) as pool:
            list(pool.map(one, todo))
    finally:
        client.close()
    if state["credits_error"]:
        stop_for_credits(state["credits_error"])
        return
    if stop.is_set():
        log.error("Serper failed 25 times - it seems to be having problems. Progress saved; re-run later.")
        notify_user()
        return
    # Approximate area coordinates for each listing locality (only used for the 3 km NoBroker check).
    need = sorted({d["locality"] for d in db.all("listings").values() if d.get("locality")})
    log.info("Locating %d listing localities (free map lookup, ~1 per second, cached)...", len(need))
    last = [0.0]
    with httpx.Client(timeout=30) as gc:
        for l in need:
            geocode(gc, db, [l, ""], "", last)
    log.info("Listings done: %d project pages. Next: python hyd_discovery.py build",
             db.execute("SELECT COUNT(*) FROM listings")[0][0])


def locality_coords(db: DB, loc: str) -> tuple[float, float] | tuple[None, None]:
    """Cached result of geocode(locality) - no network here."""
    key = json.dumps({"q": f"{loc}, Hyderabad, Telangana, India"}, sort_keys=True)
    hit = db.execute("SELECT lat, lon FROM geo WHERE q=?", (key,))
    if hit and hit[0][0] is not None and HYD_BOX[0] <= hit[0][0] <= HYD_BOX[1] and HYD_BOX[2] <= hit[0][1] <= HYD_BOX[3]:
        return hit[0]
    return None, None


class NameIndex:
    """Find same-named projects quickly: candidates share at least one distinctive name word."""

    def __init__(self, rows: list[dict]):
        self.rows, self.by_tok = rows, {}
        for i, r in enumerate(rows):
            for t in set(name_tokens(r["name"])):
                self.by_tok.setdefault(t, []).append(i)

    def best(self, name: str, lat, lon, km: float, min_sim: float = 0.8) -> dict | None:
        cands = {i for t in set(name_tokens(name)) for i in self.by_tok.get(t, [])}
        best, best_s = None, 0.0
        for i in cands:
            r = self.rows[i]
            if lat is not None and r.get("lat") is not None and math.dist((lat, lon), (r["lat"], r["lon"])) * 111 > km:
                continue
            sim = name_sim(name, r["name"])
            if sim > best_s:
                best, best_s = r, sim
        return best if best_s >= min_sim else None


def set_c_rows(db: DB, fp: Footprint, a_rows: list[dict], radius: float) -> list[dict]:
    """Listing-site projects merged across sites, checked against NoBroker and matched to RERA (Set A)."""
    merged: dict[str, dict] = {}
    for d in db.all("listings").values():
        key = ex.norm(d["name"]) + "|" + ex.norm(d.get("locality", ""))
        m = merged.setdefault(key, {"name": d["name"], "locality": d.get("locality", ""), "sites": set(), "urls": []})
        m["sites"].add(d["site"])
        m["urls"].append(d["url"])
        for f in ("builder", "rera_no", "price", "price_sqft", "possession"):
            if d.get(f) and not m.get(f):
                m[f] = d[f]
    a_index = NameIndex(a_rows)
    rows = []
    for m in merged.values():
        lat, lon = locality_coords(db, m["locality"]) if m["locality"] else (None, None)
        chk = fp.check(m["name"], lat, lon, m.get("rera_no", ""), match_km=3.0)
        if chk["nearest_km"] is not None and chk["nearest_km"] > radius + 2:  # locality centre, so allow slack
            continue
        a = a_index.best(m["name"], lat, lon, km=3.0)
        row = {**m, "lat": lat, "lon": lon, **chk, "sites": ", ".join(sorted(m["sites"])), "url": m["urls"][0],
               "in_set_a": "Yes" if a else "No"}
        if a:
            row.update(rera_no=row.get("rera_no") or a["rera_no"], builder=row.get("builder") or a["builder"],
                       rera_possession=a.get("revised_completion") or a.get("proposed_completion", ""),
                       a_key=a["key"])
            if a.get("onboarded") and not row["onboarded"]:
                row.update(onboarded=a["onboarded"], onboarded_as=a["onboarded_as"])
            a.setdefault("listing_price", row.get("price", ""))
            a.setdefault("listing_price_sqft", row.get("price_sqft", ""))
            a.setdefault("listing_possession", row.get("possession", ""))
            a.setdefault("listing_url", row["url"])
        rows.append(row)
    return rows


# ----------------------------------------------------------------------------- output

def cmd_build(args, db: DB, fp: Footprint):
    a_rows = set_a_rows(db, fp, args.radius_km)
    c_rows = set_c_rows(db, fp, a_rows, args.radius_km)  # also copies listing price/possession onto RERA projects
    b_rows = set_b_rows(db, fp, a_rows, args.radius_km)

    # One list of every new project: RERA projects, plus listing-site projects that aren't in RERA.
    combined = []
    for a in a_rows:
        if a["onboarded"]:
            continue
        combined.append({
            "Project Name": a["name"], "Builder": a["builder"], "City": "Hyderabad",
            "Location": ", ".join(x for x in (a.get("locality"), a.get("village"), a.get("mandal")) if x),
            "District": a.get("district", ""), "PIN": a.get("pin", ""), "RERA ID": a["rera_no"],
            "Project Type": a.get("project_type", ""),
            "Price": a.get("listing_price") or a.get("price", ""),
            "Price per sq.ft": a.get("listing_price_sqft") or a.get("price_sqft", ""),
            "Possession (RERA)": a.get("revised_completion") or a.get("proposed_completion", ""),
            "Possession (listing sites)": a.get("listing_possession") or a.get("market_possession", ""),
            "Found In": "RERA register" + (" + listing sites" if a.get("listing_url") else ""),
            "Link": a.get("listing_url") or a.get("detail_url", ""),
            "Nearest NoBroker Project": a.get("nearest_nb", ""), "Distance (km)": a.get("nearest_km", "")})
    for c in c_rows:
        if c["onboarded"] or c["in_set_a"] == "Yes":
            continue
        combined.append({
            "Project Name": c["name"], "Builder": c.get("builder", ""), "City": "Hyderabad",
            "Location": c.get("locality", ""), "District": "", "PIN": "", "RERA ID": c.get("rera_no", ""),
            "Project Type": "", "Price": c.get("price", ""), "Price per sq.ft": c.get("price_sqft", ""),
            "Possession (RERA)": "", "Possession (listing sites)": c.get("possession", ""),
            "Found In": f"listing sites ({c['sites']})", "Link": c["url"],
            "Nearest NoBroker Project": c.get("nearest_nb", ""), "Distance (km)": c.get("nearest_km", "")})
    ALL = pd.DataFrame(combined)

    a_cols = {"name": "Project Name", "builder": "Builder (Promoter)", "rera_no": "RERA Number",
              "project_type": "Project Type", "status": "RERA Status", "locality": "Locality", "village": "Village/Town",
              "mandal": "Mandal", "district": "District", "pin": "PIN",
              "proposed_completion": "Proposed Completion", "revised_completion": "Revised Completion (Possession)",
              "extension_cert": "Extension Certificate", "listing_price": "Price (listing sites)",
              "listing_possession": "Possession (listing sites)", "price": "Price (web lookup)",
              "nearest_nb": "Nearest NoBroker Project", "nearest_km": "Distance (km)",
              "onboarded_as": "Matches NoBroker Project", "onboarded": "Already on NoBroker", "detail_url": "RERA Page"}
    c_cols = {"name": "Project Name", "builder": "Builder", "locality": "Location", "rera_no": "RERA ID",
              "price": "Price", "price_sqft": "Price per sq.ft", "possession": "Possession (listing sites)",
              "rera_possession": "Possession (RERA)", "sites": "Listed On", "in_set_a": "In RERA Register",
              "nearest_nb": "Nearest NoBroker Project", "nearest_km": "Distance (km)",
              "onboarded_as": "Matches NoBroker Project", "onboarded": "Already on NoBroker", "url": "Link"}
    b_cols = {"name": "Building Name", "category": "Google Category", "address": "Address", "builder": "Builder",
              "rera_no": "RERA Number", "possession": "Possession / Completion", "price": "Price",
              "details_from": "Details From", "nearest_nb": "Nearest NoBroker Project", "nearest_km": "Distance (km)",
              "onboarded_as": "Matches NoBroker Project", "onboarded": "Already on NoBroker", "maps_url": "Google Maps"}
    frame = lambda rows, cols: pd.DataFrame([{v: r.get(k, "") for k, v in cols.items()} for r in rows],
                                            columns=list(cols.values()))
    A, C, B = frame(a_rows, a_cols), frame(c_rows, c_cols), frame(b_rows, b_cols)
    new = lambda df: df[df["Already on NoBroker"] == ""]
    old = lambda df, tag: df[df["Already on NoBroker"] != ""].assign(Set=tag)
    summary = pd.DataFrame([
        ("ALL NEW PROJECTS (not on NoBroker)", len(ALL)),
        ("  from RERA register", int(ALL["Found In"].str.startswith("RERA").sum()) if len(ALL) else 0),
        ("  only on listing sites (not in RERA)", int(ALL["Found In"].str.startswith("listing").sum()) if len(ALL) else 0),
        ("  with a price", int((ALL["Price"] != "").sum()) if len(ALL) else 0),
        ("  with a RERA ID", int((ALL["RERA ID"] != "").sum()) if len(ALL) else 0),
        ("RERA projects listed (all districts)", db.execute("SELECT COUNT(*) FROM rera_list")[0][0]),
        (f"RERA residential/commercial within {args.radius_km:.0f} km of NoBroker", len(A)),
        ("RERA already on NoBroker", int((A["Already on NoBroker"] != "").sum())),
        ("Listing-site projects found", len(C)), ("Listing-site projects already on NoBroker",
                                                  int((C["Already on NoBroker"] != "").sum())),
        ("Districts collected", ", ".join(d for (d,) in db.execute("SELECT district FROM district_done"))),
    ], columns=["Metric", "Value"])
    out = Path(args.out)
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        ALL.to_excel(xw, sheet_name="All new projects", index=False)
        summary.to_excel(xw, sheet_name="Summary", index=False)
        new(A).to_excel(xw, sheet_name="A - RERA not on NoBroker", index=False)
        new(C).to_excel(xw, sheet_name="C - Listings not on NoBroker", index=False)
        if len(B):
            new(B).to_excel(xw, sheet_name="B - Maps not on NoBroker", index=False)
        pd.concat([old(A, "A - RERA"), old(C, "C - Listings"),
                   old(B.rename(columns={"Building Name": "Project Name"}), "B - Maps")],
                  ignore_index=True).to_excel(xw, sheet_name="Already on NoBroker", index=False)
        from openpyxl.styles import Font
        for ws in xw.sheets.values():
            ws.freeze_panes = "B2"
            ws.auto_filter.ref = ws.dimensions
            for c in ws[1]:
                c.font = Font(bold=True)
                ws.column_dimensions[c.column_letter].width = 22
    log.info("Wrote %s | all new projects: %d (RERA %d, listing-only %d)", out, len(ALL),
             int(ALL["Found In"].str.startswith("RERA").sum()) if len(ALL) else 0,
             int(ALL["Found In"].str.startswith("listing").sum()) if len(ALL) else 0)


def cmd_status(args, db: DB):
    q = lambda s: db.c.execute(s).fetchone()[0]
    log.info("Districts done: %s", [d for (d,) in db.c.execute("SELECT district FROM district_done")] or "none")
    log.info("RERA listed: %d | details: %d | in footprint: %d | certificates read: %d",
             q("SELECT COUNT(*) FROM rera_list"), q("SELECT COUNT(*) FROM rera_detail"),
             sum(1 for d in db.all("rera_detail").values() if d.get("in_footprint")), q("SELECT COUNT(*) FROM rera_cert"))
    log.info("Listing searches done: %d | project pages: %d", q("SELECT COUNT(*) FROM listing_query"),
             q("SELECT COUNT(*) FROM listings"))
    log.info("Maps searches done: %d | unique places: %d | enriched: %d", q("SELECT COUNT(*) FROM place_query"),
             q("SELECT COUNT(*) FROM places"), q("SELECT COUNT(*) FROM enrich"))


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=["rera-list", "rera-details", "listings", "places", "enrich", "build", "status"])
    p.add_argument("--sites", nargs="+", default=list(LISTING_SITES), choices=list(LISTING_SITES),
                   help="listings: which sites to search")
    p.add_argument("--nobroker", default=r"C:\Users\shres\Downloads\buildings.xlsx", help="NoBroker projects workbook")
    p.add_argument("--nobroker-sheet", default="build l")
    p.add_argument("--out", default=r"C:\Users\shres\Downloads\Hyderabad projects not on NoBroker.xlsx")
    p.add_argument("--radius-km", type=float, default=3.0, help="Keep projects within this distance of a NoBroker project")
    p.add_argument("--terms", nargs="+", default=PLACE_TERMS, help="Google Maps search terms per locality")
    p.add_argument("--pages", type=int, default=1, help="Maps result pages per search (10 places each)")
    p.add_argument("--search-workers", type=int, default=4, help="places/enrich: Serper searches in parallel")
    p.add_argument("--workers", type=int, default=6, help="rera-details: project pages fetched in parallel")
    p.add_argument("--only", choices=["a", "b", "both"], default="both", help="enrich: which set to look up")
    args = p.parse_args()

    out = Path(args.out)
    load_env(Path(__file__).with_name(".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(out.with_name("hyd_discovery.log"), encoding="utf-8")])
    for noisy in ("httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    db = DB(out.with_name("hyd_discovery.sqlite"))
    if args.step not in ("status", "build"):
        keep_awake()
    if args.step == "status":
        return cmd_status(args, db)
    if args.step == "rera-list":
        return cmd_rera_list(args, db)
    nb = load_nobroker(args.nobroker, args.nobroker_sheet)
    fp = Footprint(nb)
    try:
        {"rera-details": lambda: cmd_rera_details(args, db, fp), "places": lambda: cmd_places(args, db, nb),
         "listings": lambda: cmd_listings(args, db, nb),
         "enrich": lambda: cmd_enrich(args, db, fp), "build": lambda: cmd_build(args, db, fp)}[args.step]()
    except KeyboardInterrupt:
        log.warning("Stopped by you - progress is saved; run the same command to continue.")
    except Exception:
        log.exception("%s crashed - progress is saved; send this log to get it fixed", args.step)
        raise


if __name__ == "__main__":
    main()
