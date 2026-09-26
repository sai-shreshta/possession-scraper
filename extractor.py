"""Find possession / completion dates in text and decide the most likely one."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

TODAY = datetime.now()
MIN_YEAR, MAX_YEAR = 1950, TODAY.year + 12

MONTH_NUM = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
        r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_YEAR = r"(19[5-9]\d|20[0-4]\d)"

_DATE_RES = [
    ("dmy_name", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s\-/.,]*{_MON}[\s\-/.,']*{_YEAR}\b", re.I)),
    ("my_name", re.compile(rf"\b{_MON}[\s\-/.,']*{_YEAR}\b", re.I)),
    ("my_short", re.compile(rf"\b{_MON}\s*['’`]\s*(\d{{2}})\b", re.I)),
    ("dmy_num", re.compile(rf"\b(\d{{1,2}})[/\-.](\d{{1,2}})[/\-.]{_YEAR}\b")),
    ("ymd_num", re.compile(rf"\b{_YEAR}[/\-.](\d{{1,2}})(?:[/\-.](\d{{1,2}}))?\b")),
    ("my_num", re.compile(rf"(?<![\d/\-.])(\d{{1,2}})[/\-]{_YEAR}\b")),
    ("quarter", re.compile(rf"\bQ([1-4])\s*[,\-']?\s*(?:FY\s*)?{_YEAR}\b", re.I)),
]
_YEAR_RE = re.compile(rf"\b{_YEAR}\b")
_PREC_RANK = {"day": 0, "month": 1, "quarter": 2, "year": 3}
PREC_FACTOR = {"day": 1.0, "month": 1.0, "quarter": 0.9, "year": 0.65}

# Order matters: earlier alternatives win when they start at the same spot.
_KEYWORDS = [
    ("rera", 1.0, r"rera\s+(?:possession|completion)(?:\s+date)?|proposed\s+date\s+of\s+completion"
                  r"|revised\s+(?:proposed\s+)?(?:date\s+of\s+)?completion(?:\s+date)?"),
    ("possession", 1.0, r"possession(?:\s+(?:date|starts?|by|from|in|on|time|due|expected|timeline|status))?"),
    ("completion", 0.85, r"completion(?:\s+(?:date|by|in|on|year|time))?|completed\s+(?:on|in|by)"),
    ("handover", 0.85, r"hand\s*-?\s*over(?:\s+(?:date|by|in|from))?"),
    ("ready", 0.8, r"ready\s+to\s+move(?:[\s\-]in)?(?:\s+(?:since|from|in|by))?"
                   r"|ready\s+for\s+possession(?:\s+(?:since|from|in|by))?"
                   r"|occupancy\s+certificate(?:\s+(?:received|obtained|date))?"),
    ("built", 0.75, r"(?:built|constructed)\s+in|year\s+(?:of\s+)?(?:construction|built|completion)"
                    r"|construction\s+year|built\s+year"),
]
_KW_RE = re.compile("|".join(rf"(?P<{k}>\b(?:{p})\b)" for k, _, p in _KEYWORDS), re.I)
_KW_WEIGHT = {k: w for k, w, _ in _KEYWORDS}

# Anything after these words inside the window describes something other than possession.
_STOP_RE = re.compile(
    r"launch|registration|registered|rera\s*(?:no|id|number|reg)|valid\s+(?:till|upto|up\s+to)|price|₹"
    r"|\brs\.?\s|\binr\b|sq\.?\s*ft|sqft|\bbhk\b|configuration|\bunits?\b|\btowers?\b|\bacres?\b"
    r"|\bfloors?\b|updated|posted|listed|review|rating|\bemi\b|carpet|super\s+(?:built|area)|©|copyright"
    r"|\bage\b|\bestd\b|established|founded", re.I)
_WINDOW = 60

_STATUS_RES = [
    ("Ready to Move", re.compile(r"ready\s+to\s+move|ready\s+for\s+possession|immediate\s+possession"
                                 r"|completed\s+project|possession\s+status\s*:?\s*(?:ready|immediate|completed)", re.I)),
    ("Under Construction", re.compile(r"under[\s\-]+construction|new\s+launch|pre[\s\-]*launch"
                                      r"|upcoming\s+project", re.I)),
]

_STRUCT_RE = re.compile(
    r"[\"']?((?:expected|proposed|rera|actual|target)?_?(?:possession|completion|handover)"
    r"_?(?:date|time|by|start|starts|startdate|on)?)[\"']?\s*:\s*[\"']?([^\"',}\]<\n]{2,40})", re.I)

DOMAIN_WEIGHT = {
    "housing.com": 0.9, "99acres.com": 0.9, "magicbricks.com": 0.9, "squareyards.com": 0.88,
    "proptiger.com": 0.85, "makaan.com": 0.85, "nobroker.in": 0.82, "commonfloor.com": 0.78,
    "zricks.com": 0.8, "roofandfloor.com": 0.78, "anarock.com": 0.8, "homebazaar.com": 0.72,
    "propequity.in": 0.8, "realestateindia.com": 0.6, "indiaproperty.com": 0.6, "propertywala.com": 0.6,
    "dealacres.com": 0.6, "nobrokerage.com": 0.6, "quikr.com": 0.5, "sulekha.com": 0.5,
    "justdial.com": 0.45, "keralarealestate.in": 0.6, "housiey.com": 0.75, "homznspace.com": 0.65,
    "propsamc.com": 0.7, "squarefeetgroup.com": 0.6, "youtube.com": 0.3, "facebook.com": 0.3,
}
DEFAULT_DOMAIN_WEIGHT = 0.55

GENERIC = set("""apartment apartments apts apt residency residences residence residential heights height
tower towers enclave society chs cghs co op operative housing homes home villas villa project projects estate
estates park gardens garden city complex plaza nagar flat flats building buildings bldg ltd pvt limited phase
block wing sector plot new premium luxury""".split())
STOPWORDS = {"the", "and", "of", "at", "in", "by", "a", "an", "on"}

CITY_ALIASES = {
    "bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai", "calcutta": "kolkata",
    "madras": "chennai", "poona": "pune", "trivandrum": "thiruvananthapuram", "mysore": "mysuru",
    "baroda": "vadodara", "cochin": "kochi", "vizag": "visakhapatnam", "mangalore": "mangaluru",
    "belgaum": "belagavi", "hubli": "hubballi", "pondicherry": "puducherry", "calicut": "kozhikode",
}


def norm(s: str) -> str:
    s = str(s or "").lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def _host(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def domain_weight(url: str) -> tuple[float, bool]:
    """Returns (weight, is_government_or_rera)."""
    h = _host(url)
    if "rera" in h or h.endswith(".gov.in") or h.endswith(".nic.in"):
        return 1.0, True
    for d, w in DOMAIN_WEIGHT.items():
        if h == d or h.endswith("." + d):
            return w, False
    return DEFAULT_DOMAIN_WEIGHT, False


# ----------------------------------------------------------------------------- name matching

class NameMatcher:
    def __init__(self, name: str, locality: str, city: str):
        toks = [t for t in norm(name).split() if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]
        self.tokens = [(t, 0.5 if (t in GENERIC or t.isdigit()) else 1.0) for t in toks]
        self.total = sum(w for _, w in self.tokens) or 1.0
        self.squashed = "".join(t for t, _ in self.tokens)
        distinct = [t for t, w in self.tokens if w == 1.0] or [t for t, _ in self.tokens]
        self.anchor = max(distinct, key=len) if distinct else ""
        self.weak = sum(1 for _, w in self.tokens if w == 1.0) <= 1 and len(self.anchor) <= 5
        self.places = []
        for p in (locality, city):
            n = norm(p)
            if n:
                self.places.append(n)
                if n in CITY_ALIASES:
                    self.places.append(CITY_ALIASES[n])
                inv = {v: k for k, v in CITY_ALIASES.items()}
                if n in inv:
                    self.places.append(inv[n])

    def score(self, text: str) -> float:
        """0..1: how confidently `text` talks about this building."""
        if not self.tokens:
            return 0.0
        nt = norm(text)
        words = set(nt.split())
        sq = nt.replace(" ", "")
        if len(self.squashed) >= 6 and self.squashed in sq:
            s = 1.0
        else:
            hit = sum(w for t, w in self.tokens if t in words or (len(t) >= 6 and t in sq))
            s = hit / self.total
        place_ok = any(f" {p} " in f" {nt} " for p in self.places) if self.places else True
        if not place_ok:
            s *= 0.8
        if self.weak and not place_ok:
            s *= 0.6
        return s

    def anchor_positions(self, text_lower: str, limit: int = 60) -> list[int]:
        out, i = [], 0
        if not self.anchor:
            return out
        while len(out) < limit:
            i = text_lower.find(self.anchor, i)
            if i < 0:
                break
            out.append(i)
            i += len(self.anchor)
        return out


# ----------------------------------------------------------------------------- date parsing

@dataclass
class ParsedDate:
    year: int
    month: int | None
    precision: str
    start: int


def _mk(y: int, m: int | None, prec: str, start: int) -> ParsedDate | None:
    if not MIN_YEAR <= y <= MAX_YEAR:
        return None
    if m is not None and not 1 <= m <= 12:
        return None
    return ParsedDate(y, m, prec, start)


def _mon(s: str) -> int:
    return MONTH_NUM[s[:3].lower()]


def _from_match(kind: str, m: re.Match) -> ParsedDate | None:
    g, st = m.groups(), m.start()
    if kind == "dmy_name":
        return _mk(int(g[2]), _mon(g[1]), "day", st) if 1 <= int(g[0]) <= 31 else None
    if kind == "my_name":
        return _mk(int(g[1]), _mon(g[0]), "month", st)
    if kind == "my_short":
        return _mk(2000 + int(g[1]), _mon(g[0]), "month", st)
    if kind == "dmy_num":
        a, b, y = int(g[0]), int(g[1]), int(g[2])
        if 1 <= b <= 12 and 1 <= a <= 31:
            return _mk(y, b, "day", st)
        if 1 <= a <= 12 and 1 <= b <= 31:
            return _mk(y, a, "day", st)
        return None
    if kind == "ymd_num":
        return _mk(int(g[0]), int(g[1]), "day" if g[2] else "month", st)
    if kind == "my_num":
        return _mk(int(g[1]), int(g[0]), "month", st)
    if kind == "quarter":
        return _mk(int(g[1]), int(g[0]) * 3, "quarter", st)
    return None


def parse_first_date(s: str) -> ParsedDate | None:
    found = []
    for kind, rx in _DATE_RES:
        for m in rx.finditer(s):
            pd = _from_match(kind, m)
            if pd:
                found.append(pd)
                break
    if found:
        return min(found, key=lambda p: (p.start, _PREC_RANK[p.precision]))
    m = _YEAR_RE.search(s)
    return _mk(int(m.group(1)), None, "year", m.start()) if m else None


def _epoch_date(v: str) -> ParsedDate | None:
    ts = int(v)
    if len(v) >= 13:
        ts //= 1000
    try:
        dt = datetime.fromtimestamp(ts, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return _mk(dt.year, dt.month, "month", 0)


# ----------------------------------------------------------------------------- candidates

@dataclass
class Candidate:
    year: int
    month: int | None
    precision: str
    kind: str
    score: float
    url: str
    domain: str
    context: str
    is_rera: bool

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}" if self.month else f"{self.year:04d}"


def _keyword_hits(text: str):
    """Yield (kind, keyword_start, ParsedDate, context) for every keyword followed by a date."""
    for m in _KW_RE.finditer(text):
        kind = m.lastgroup
        win = text[m.end(): m.end() + _WINDOW]
        stop = _STOP_RE.search(win)
        if stop:
            win = win[:stop.start()]
        pd = parse_first_date(win)
        if not pd or (pd.precision == "year" and pd.start > 25):
            continue
        ctx = text[max(0, m.start() - 50): m.end() + pd.start + 25]
        yield kind, m.start(), pd, re.sub(r"\s+", " ", ctx).strip()


def _statuses(text: str) -> Counter:
    c = Counter()
    for label, rx in _STATUS_RES:
        n = len(rx.findall(text))
        if n:
            c[label] += min(n, 3)
    return c


def from_snippet(title: str, snippet: str, url: str, matcher: NameMatcher) -> tuple[list[Candidate], Counter]:
    text = f"{title} . {snippet}"
    match = matcher.score(text)
    if match < 0.6:
        return [], Counter()
    dw, gov = domain_weight(url)
    out = []
    for kind, _, pd, ctx in _keyword_hits(text):
        s = _KW_WEIGHT[kind] * dw * match * PREC_FACTOR[pd.precision] * 0.85
        out.append(Candidate(pd.year, pd.month, pd.precision, kind, s, url, _host(url), ctx, gov or kind == "rera"))
    return out, _statuses(text)


def html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    h1 = soup.find("h1")
    if h1:
        title = f"{title} | {h1.get_text(' ', strip=True)}"
    for t in soup(["script", "style", "noscript", "svg", "iframe"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return title, text


def from_page(html: str, url: str, matcher: NameMatcher) -> tuple[list[Candidate], Counter]:
    title, text = html_to_text(html)
    title_match = matcher.score(title)
    about_page = title_match >= 0.8
    dw, gov = domain_weight(url)
    host = _host(url)
    out: list[Candidate] = []
    statuses = Counter()

    if about_page:
        # Page is dedicated to this building: structured data and every keyword hit count.
        n = 0
        for m in _STRUCT_RE.finditer(html):
            key, val = m.group(1).lower(), m.group(2).strip()
            if "status" in key:
                continue
            pd = _epoch_date(val) if val.isdigit() and len(val) in (10, 13) else parse_first_date(val)
            if not pd:
                continue
            s = dw * title_match * PREC_FACTOR[pd.precision]
            out.append(Candidate(pd.year, pd.month, pd.precision, "structured", s, url, host,
                                 f"{key}: {val}", gov or "rera" in key))
            n += 1
            if n >= 8:
                break
        hits = 0
        anchors = matcher.anchor_positions(text.lower(), limit=300)
        for kind, pos, pd, ctx in _keyword_hits(text):
            # Dedicated pages still carry "similar projects" carousels; discount hits away from the name.
            near = any(abs(pos - a) <= 300 for a in anchors) and \
                matcher.score(text[max(0, pos - 300): pos + 300]) >= 0.6
            s = _KW_WEIGHT[kind] * dw * title_match * PREC_FACTOR[pd.precision] * (1.0 if near else 0.4)
            out.append(Candidate(pd.year, pd.month, pd.precision, kind, s, url, host, ctx, gov or kind == "rera"))
            hits += 1
            if hits >= 25:
                break
        statuses = _statuses(text[:20000])
    else:
        # Listing / news page: only trust dates written close to this building's name.
        low = text.lower()
        anchors = matcher.anchor_positions(low)
        if not anchors:
            return [], Counter()
        for kind, pos, pd, ctx in _keyword_hits(text):
            if not any(abs(pos - a) <= 250 for a in anchors):
                continue
            local = text[max(0, pos - 300): pos + 300]
            lm = matcher.score(local)
            if lm < 0.75:
                continue
            s = _KW_WEIGHT[kind] * dw * lm * PREC_FACTOR[pd.precision] * 0.75
            out.append(Candidate(pd.year, pd.month, pd.precision, kind, s, url, host, ctx, gov or kind == "rera"))
    return out, statuses


# ----------------------------------------------------------------------------- verdict

@dataclass
class Verdict:
    found: bool = False
    display: str = ""
    iso: str = ""
    precision: str = ""
    status: str = ""
    label: str = "Not found"
    confidence: float = 0.0
    sources: int = 0
    rera_date: str = ""
    best_url: str = ""
    evidence: str = ""
    alternatives: str = ""
    queries: int = 0
    note: str = ""
    all_urls: list = field(default_factory=list)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d.pop("all_urls", None)
        return d


def _display(year: int, month: int | None, precision: str) -> str:
    if not month:
        return str(year)
    s = f"{MONTH_ABBR[month]} {year}"
    return f"{s} (Q{(month - 1) // 3 + 1})" if precision == "quarter" else s


def _months(key: str) -> int | None:
    if len(key) != 7:
        return None
    return int(key[:4]) * 12 + int(key[5:])


def aggregate(cands: list[Candidate], statuses: Counter) -> Verdict:
    v = Verdict()
    best_per: dict[tuple, Candidate] = {}
    for c in cands:
        k = (c.url, c.key)
        if k not in best_per or c.score > best_per[k].score:
            best_per[k] = c
    cands = sorted(best_per.values(), key=lambda c: c.month is None)

    groups: dict[str, dict] = {}
    for c in cands:
        if c.month is None:
            same = [k for k in groups if k.startswith(f"{c.year:04d}-")]
            if same:
                g = groups[max(same, key=lambda k: groups[k]["score"])]
                g["score"] += c.score * 0.6
                g["domains"].add(c.domain)
                continue
        g = groups.setdefault(c.key, {"score": 0.0, "domains": set(), "cands": []})
        g["score"] += c.score
        g["domains"].add(c.domain)
        g["cands"].append(c)

    if statuses:
        v.status = statuses.most_common(1)[0][0]
    if not groups:
        v.note = "no possession date found on the web"
        return v

    for k, g in groups.items():
        mk = _months(k)
        near = 0.0
        if mk is not None:
            for k2, g2 in groups.items():
                m2 = _months(k2)
                if k2 != k and m2 is not None and abs(m2 - mk) <= 3:
                    near += g2["score"]
        g["eff"] = g["score"] + 0.3 * near

    ranked = sorted(groups.items(), key=lambda kv: kv[1]["eff"], reverse=True)
    best_key, best = ranked[0]
    total = sum(g["score"] for g in groups.values()) or 1.0
    agreement = best["score"] / total
    top = max(best["cands"], key=lambda c: c.score)

    v.found = True
    v.iso = best_key
    v.precision = top.precision
    v.display = _display(top.year, top.month, top.precision)
    v.sources = len(best["domains"])
    v.best_url = top.url
    v.evidence = top.context[:300]
    v.confidence = round(min(1.0, best["eff"] / 1.5) * (0.5 + 0.5 * agreement), 2)
    strong_single = any(c.is_rera or c.kind == "structured" for c in best["cands"]) and top.score >= 0.7
    if v.confidence >= 0.55 and (v.sources >= 2 or strong_single):
        v.label = "High"
    elif v.confidence >= 0.3:
        v.label = "Medium"
    else:
        v.label = "Low"

    rera = [c for c in cands if c.is_rera]
    if rera:
        r = max(rera, key=lambda c: c.score)
        v.rera_date = _display(r.year, r.month, r.precision)

    v.alternatives = "; ".join(
        f"{_display(int(k[:4]), int(k[5:]) if len(k) == 7 else None, 'month')} ({len(g['domains'])} src)"
        for k, g in ranked[1:5])

    # Page-level status words are noisy (other listings on the page); the chosen date is the better signal.
    y, m = top.year, top.month or 12
    v.status = "Under Construction" if (y, m) > (TODAY.year, TODAY.month) else "Ready to Move"
    return v
