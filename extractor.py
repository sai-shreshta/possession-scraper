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
    ("extension", 1.0, r"(?:rera\s+)?extension(?:\s+(?:date|upto|up\s+to|till|until|granted|valid\s+(?:till|upto)))?"
                       r"|extended\s+(?:up\s*to|till|until|to)"),
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
RERA_KINDS = ("rera", "extension")

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
    "proptiger.com": 0.85, "makaan.com": 0.85, "commonfloor.com": 0.78,
    "zricks.com": 0.8, "roofandfloor.com": 0.78, "anarock.com": 0.8, "homebazaar.com": 0.72,
    "propequity.in": 0.8, "realestateindia.com": 0.5, "indiaproperty.com": 0.5, "propertywala.com": 0.5,
    "dealacres.com": 0.5, "nobrokerage.com": 0.5, "keralarealestate.in": 0.5, "housiey.com": 0.75,
    "homznspace.com": 0.6, "propsamc.com": 0.6, "squarefeetgroup.com": 0.5,
}
DEFAULT_DOMAIN_WEIGHT = 0.45
# Portals whose project pages are maintained per project; a date/price from anywhere else is shown as unconfirmed.
TRUSTED_PORTALS = ("housing.com", "99acres.com", "magicbricks.com", "squareyards.com", "proptiger.com", "makaan.com",
                   "commonfloor.com", "housiey.com", "roofandfloor.com", "zricks.com", "anarock.com")
# Never evidence: the user's own data (nobroker.in), social media, Q&A and classifieds.
BLOCKED_HOSTS = ("nobroker.in", "youtube.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
                 "linkedin.com", "quora.com", "reddit.com", "pinterest.com", "pinterest.in", "t.me", "whatsapp.com",
                 "justdial.com", "sulekha.com", "quikr.com", "olx.in", "wikipedia.org", "scribd.com",
                 "slideshare.net", "threads.net")
OFFICIAL_RERA_HOSTS = ("up-rera.in", "tnrera.in", "maharera.mahaonline.gov.in")
MAJOR_PORTALS = ("housing.com", "99acres.com", "magicbricks.com", "squareyards.com", "proptiger.com", "makaan.com")

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


def _on(h: str, domains) -> bool:
    return any(h == d or h.endswith("." + d) for d in domains)


def source_tier(url: str, matcher: "NameMatcher | None" = None) -> str:
    """official | rera-mirror | portal | project-site | other | blocked."""
    h = _host(url)
    if _on(h, BLOCKED_HOSTS):
        return "blocked"
    if h.endswith(".gov.in") or h.endswith(".nic.in") or _on(h, OFFICIAL_RERA_HOSTS):
        return "official"
    if "rera" in h:
        return "rera-mirror"  # reratracker.com, reragenie.com, ...: copies of the register, usually right
    if _on(h, TRUSTED_PORTALS):
        return "portal"
    if matcher and len(matcher.anchor) >= 6 and matcher.anchor in h.replace("-", "").replace(".", ""):
        return "project-site"  # e.g. prestigelakesidehabitat.com
    return "other"


TRUSTED_TIERS = ("official", "rera-mirror", "portal", "project-site")


def domain_weight(url: str) -> tuple[float, bool]:
    """Returns (weight, is_government_or_rera)."""
    h = _host(url)
    tier = source_tier(url)
    if tier == "official":
        return 1.0, True
    if tier == "rera-mirror":
        return 0.85, True
    for d, w in DOMAIN_WEIGHT.items():
        if h == d or h.endswith("." + d):
            return w, False
    return DEFAULT_DOMAIN_WEIGHT, False


# ----------------------------------------------------------------------------- name matching

class NameMatcher:
    def __init__(self, name: str, locality: str, city: str, ids: tuple = ()):
        # RERA IDs: a page quoting the project's registration number is about this project, whatever it calls it.
        self.ids = [re.sub(r"[^a-z0-9]", "", i.lower()) for i in ids if len(re.sub(r"[^a-z0-9]", "", i.lower())) >= 6]
        toks = [t for t in norm(name).split() if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]
        self.tokens = [(t, 0.5 if (t in GENERIC or t.isdigit()) else 1.0) for t in toks]
        self.total = sum(w for _, w in self.tokens) or 1.0
        self.squashed = "".join(t for t, _ in self.tokens)
        distinct = [t for t, w in self.tokens if w == 1.0] or [t for t, _ in self.tokens]
        self.anchor = max(distinct, key=len) if distinct else ""
        self.weak = sum(1 for _, w in self.tokens if w == 1.0) <= 1 and len(self.anchor) <= 5
        # "Sai Residency": one distinctive word, shared by many buildings - the page must also name the place.
        self.single = sum(1 for _, w in self.tokens if w == 1.0) <= 1
        self.locality, self.city = norm(locality), norm(city)
        self.raw_ids = [i for i in ids if len(re.sub(r"[^a-z0-9]", "", i.lower())) >= 6]
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
        if any(i in sq for i in self.ids):
            return 1.0
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

    def id_in(self, text: str) -> bool:
        sq = norm(text).replace(" ", "")
        return any(i in sq for i in self.ids)

    def place_in(self, text: str) -> bool:
        """The row's locality is mentioned ('Andheri West' matches 'Andheri (W)'); the city when there's no locality."""
        words = set(norm(text).split())
        loc = [t for t in self.locality.split() if len(t) >= 4 and t not in _PLACE_FILLER]
        if loc:
            return any(t in words for t in loc)
        return bool(self.city) and self.city in norm(text)

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


# ----------------------------------------------------------------------------- source checks

_PLACE_FILLER = {"east", "west", "north", "south", "road", "main", "nagar", "colony", "phase", "sector", "layout",
                 "extension", "new", "old", "city", "town", "village", "stage", "block", "cross", "near"}

# Names that mean "the same market"; a page naming only places from another group is about another city.
CITY_REGIONS = {
    "mumbai": ["mumbai", "bombay", "thane", "navi mumbai", "mira road", "mira bhayandar", "bhayandar", "kalyan",
               "dombivli", "vasai", "virar", "panvel", "ulwe", "kharghar", "badlapur", "ambernath", "palghar",
               "boisar", "karjat", "neral"],
    "pune": ["pune", "pimpri", "chinchwad", "pimpri chinchwad", "pcmc"],
    "bangalore": ["bangalore", "bengaluru"],
    "hyderabad": ["hyderabad", "secunderabad", "cyberabad"],
    "chennai": ["chennai", "madras"],
    "ncr": ["delhi", "new delhi", "gurgaon", "gurugram", "noida", "greater noida", "ghaziabad", "faridabad",
            "sonipat", "sohna", "bhiwadi", "dharuhera"],
    "kolkata": ["kolkata", "calcutta", "howrah"],
    "ahmedabad": ["ahmedabad", "gandhinagar"],
    "tricity": ["chandigarh", "mohali", "zirakpur", "panchkula", "kharar"],
    "kochi": ["kochi", "cochin", "ernakulam"],
    "mysore": ["mysore", "mysuru"],
    "vizag": ["visakhapatnam", "vizag"],
    "vadodara": ["vadodara", "baroda"],
    "trivandrum": ["thiruvananthapuram", "trivandrum"],
    "mangalore": ["mangalore", "mangaluru"],
    **{c: [c] for c in ["coimbatore", "jaipur", "lucknow", "indore", "bhopal", "nagpur", "nashik", "surat",
                        "bhubaneswar", "vijayawada", "goa", "dehradun", "rajkot", "ludhiana", "patna", "ranchi",
                        "raipur", "kanpur", "agra", "varanasi", "guntur", "nellore", "hubli", "madurai"]},
}
_ALL_CITY_NAMES = sorted({n for names in CITY_REGIONS.values() for n in names}, key=len, reverse=True)


def city_names_for(city: str) -> set[str]:
    c = norm(city)
    if not c:
        return set()
    c2 = CITY_ALIASES.get(c, c)
    for names in CITY_REGIONS.values():
        if c in names or c2 in names:
            return set(names)
    return {c, c2}


def city_conflict(text: str, city: str) -> bool:
    """True when `text` names a city, none of which is the row's city (e.g. a Bangalore page for a Hyderabad row)."""
    ours = city_names_for(city)
    if not ours:
        return False
    nt = f" {norm(text)} "
    if any(f" {n} " in nt for n in ours):
        return False
    return any(f" {n} " in nt for n in _ALL_CITY_NAMES if n not in ours)


# Pages about many projects or single resale units: their dates/prices are not this project's.
_AGGREGATE_URL = re.compile(
    r"new-projects?-in|projects?-in-|flats?-for-sale|propert(?:y|ies)-for-sale|apartments?-for-sale"
    r"|houses?-for-sale|villas?-for-sale|plots?-for-sale|for-sale-in|-pppfs|/property/buy/|for-rent|/rent/"
    r"|/search|/srp|[?&](?:q|query|keyword)=|/builders?/|/developers?/|/localit(?:y|ies)/|-prjtl|/news/"
    r"|/top-\d+|best-(?:projects|flats|apartments)|upcoming-projects|ready-to-move-(?:flats|projects|apartments)"
    r"|under-construction-(?:flats|projects)|/compare|resale|/updates?/|/blogs?/|/articles?/", re.I)
_AGGREGATE_TITLE = re.compile(
    r"^\s*(?:\d+\+?\s+|top\s+\d*\s*|best\s+|new\s+|upcoming\s+|latest\s+|ready\s+to\s+move\s+|resale\s+"
    r"|under\s+construction\s+)*(?:projects|flats|apartments|properties|property|homes|houses|villas|plots"
    r"|\d\s*bhk|residential\s+projects)\b", re.I)


def url_rejected(url: str) -> str:
    """Cheap URL-only check, used before fetching. Returns the reason or ''."""
    if source_tier(url) == "blocked":
        return "blocked site"
    if _AGGREGATE_URL.search(url) and "/buy/projects/page/" not in url:
        return "list / search / resale page"
    return ""


def _id_shape(rid: str) -> re.Pattern:
    """Regex matching other IDs of the same format: 'P51800012345' -> P\\d{11}."""
    parts = re.findall(r"[A-Za-z]+|\d+|[^A-Za-z\d]+", rid)
    rx = "".join(rf"\d{{{len(p)}}}" if p.isdigit() else (r"\s*[/\-]?\s*" if not p.isalnum() else re.escape(p))
                 for p in parts)
    return re.compile(rf"(?<![A-Za-z\d]){rx}(?!\d)", re.I)


def rera_conflict(text: str, matcher: "NameMatcher") -> bool:
    """The page quotes registration numbers of our format but never ours: another project or phase."""
    if not matcher.raw_ids or matcher.id_in(text):
        return False
    return any(_id_shape(rid).search(text[:8000]) for rid in matcher.raw_ids)


def reject_reason(url: str, title: str, text: str, matcher: "NameMatcher") -> str:
    why = url_rejected(url)
    if why:
        return why
    if _AGGREGATE_TITLE.search(title):
        return "list page"
    if city_conflict(f"{title} {url}", matcher.city):
        return "different city"
    if rera_conflict(text, matcher):
        return "different RERA number"
    if matcher.single and not matcher.place_in(f"{title} {url} {text[:6000]}"):
        return "common name, locality not on page"
    return ""


_SECTOR = re.compile(r"\bsector (\d{1,3}[a-z]?)\b")


def sector_conflict(text: str, matcher: "NameMatcher") -> bool:
    """Row says Sector 41, page title/URL says Sector 30: probably a namesake in another sector."""
    ours = set(_SECTOR.findall(matcher.locality))
    theirs = set(_SECTOR.findall(norm(text)))
    return bool(ours and theirs and not ours & theirs)


def is_trusted(url: str, title: str, matcher: "NameMatcher") -> bool:
    """Trusted site AND nothing on it hints at a namesake; otherwise its answer is shown only as unconfirmed."""
    return source_tier(url, matcher) in TRUSTED_TIERS and not sector_conflict(f"{title} {url}", matcher)


def is_dedicated(title: str, text: str, matcher: "NameMatcher") -> float:
    """How surely the page is about this one project (0 = not). The name must open the title."""
    head = title[:110]
    s = matcher.score(head)
    if matcher.id_in(text):
        return 1.0 if s >= 0.5 else 0.0
    return s if s >= 0.8 else 0.0


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
    trusted: bool = False   # RERA, a major portal, or the project's own site
    origin: str = ""        # page / page data / search snippet

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


def from_snippet(title: str, snippet: str, url: str, matcher: NameMatcher,
                 pairs_out: list | None = None, prices_out: list | None = None) -> tuple[list[Candidate], Counter]:
    text = f"{title} . {snippet}"
    if reject_reason(url, title, text, matcher) or city_conflict(text, matcher.city):
        return [], Counter()
    match = 1.0 if matcher.id_in(text) else matcher.score(title)
    if match < 0.75:
        return [], Counter()  # the result's title must be this project, not a list it appears in
    if pairs_out is not None:
        pairs_out += rera_pairs(text, url, matcher, title)
    dw, gov = domain_weight(url)
    trusted = is_trusted(url, title, matcher)
    out = []
    for kind, _, pd, ctx in _keyword_hits(text):
        s = _KW_WEIGHT[kind] * dw * match * PREC_FACTOR[pd.precision] * 0.85
        out.append(Candidate(pd.year, pd.month, pd.precision, kind, s, url, _host(url), ctx,
                             gov or kind in RERA_KINDS, trusted, "search snippet"))
    if prices_out is not None:
        prices_out += price_candidates(text, url, matcher, from_page=False, trusted=trusted)
    return out, _statuses(text)


_ORIG = r"(?:original|proposed)(?:\s+proposed)?(?:\s+date\s+of)?\s+completion(?:\s+date)?"
_REV = r"revised(?:\s+proposed)?(?:\s+date\s+of)?\s+completion(?:\s+date)?"
_PAIR_RE = re.compile(rf"{_ORIG}\s*[:\-]?\s*(?P<o>.{{0,40}}?)\s*{_REV}\s*[:\-]?\s*(?P<r>.{{0,40}})", re.I)


def rera_pairs(text: str, url: str, matcher: "NameMatcher", title: str = "") -> list[dict]:
    """'Original Completion 31 Dec 2018 ... Revised Completion 30 Jun 2020' as published on RERA mirror pages."""
    out = []
    sq_all = norm(f"{url} {text[:20000]}").replace(" ", "")
    id_match = any(i in sq_all for i in matcher.ids)
    for m in _PAIR_RE.finditer(text):
        o, r = parse_first_date(m.group("o")), parse_first_date(m.group("r"))
        if not o or not r or not o.month or not r.month:
            continue
        local = text[max(0, m.start() - 400): m.end() + 100]
        relevance = 1.0 if id_match else max(matcher.score(title), matcher.score(local))
        out.append({"orig": (o.year, o.month), "rev": (r.year, r.month), "url": url, "id_match": id_match,
                    "relevance": relevance, "text": re.sub(r"\s+", " ", m.group(0))[:200]})
        if len(out) >= 5:
            break
    return out


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


def from_page(html: str, url: str, matcher: NameMatcher,
              pairs_out: list | None = None, prices_out: list | None = None) -> tuple[list[Candidate], Counter]:
    """Only pages dedicated to this one project count. Lists, news roundups and other cities' namesakes are
    skipped entirely - a date written next to the name on such pages too often belongs to a neighbour."""
    title, text = html_to_text(html)
    if reject_reason(url, title, text, matcher):
        return [], Counter()
    title_match = is_dedicated(title, text, matcher)
    if not title_match:
        return [], Counter()
    if pairs_out is not None:
        pairs_out += rera_pairs(text, url, matcher, title)
    dw, gov = domain_weight(url)
    host = _host(url)
    trusted = is_trusted(url, title, matcher)
    out: list[Candidate] = []

    # Structured fields ("possessionDate": ...) - the first few belong to the page's own project;
    # later ones are usually the "similar projects" widgets.
    for n, m in enumerate(_STRUCT_RE.finditer(html)):
        if n >= 3:
            break
        key, val = m.group(1).lower(), m.group(2).strip()
        if "status" in key:
            continue
        pd = _epoch_date(val) if val.isdigit() and len(val) in (10, 13) else parse_first_date(val)
        if not pd:
            continue
        s = dw * title_match * PREC_FACTOR[pd.precision]
        out.append(Candidate(pd.year, pd.month, pd.precision, "structured", s, url, host,
                             f"{key}: {val}", gov or "rera" in key, trusted, "page data"))
    hits = 0
    anchors = matcher.anchor_positions(text.lower(), limit=300)
    for kind, pos, pd, ctx in _keyword_hits(text):
        # Only dates written near this project's name; carousels of other projects are ignored.
        if not (any(abs(pos - a) <= 300 for a in anchors)
                and matcher.score(text[max(0, pos - 300): pos + 300]) >= 0.6):
            continue
        s = _KW_WEIGHT[kind] * dw * title_match * PREC_FACTOR[pd.precision]
        out.append(Candidate(pd.year, pd.month, pd.precision, kind, s, url, host, ctx,
                             gov or kind in RERA_KINDS, trusted, "page"))
        hits += 1
        if hits >= 25:
            break
    if prices_out is not None:
        prices_out += price_candidates(text, url, matcher, from_page=True, html=html, trusted=trusted)
    return out, _statuses(text[:20000])


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

    # A date backed by RERA / a major portal beats any amount of agreement among unknown sites.
    ranked = sorted(groups.items(), key=lambda kv: (any(c.trusted for c in kv[1]["cands"]), kv[1]["eff"]),
                    reverse=True)
    best_key, best = ranked[0]
    total = sum(g["score"] for g in groups.values()) or 1.0
    agreement = best["score"] / total
    top = max(best["cands"], key=lambda c: (c.trusted, c.score))
    trusted_domains = {c.domain for c in best["cands"] if c.trusted}

    v.found = True
    v.iso = best_key
    v.precision = top.precision
    v.display = _display(top.year, top.month, top.precision)
    v.sources = len(best["domains"])
    v.best_url = top.url
    v.evidence = f"[{top.origin or top.kind}] {top.context[:300]}"
    v.confidence = round(min(1.0, best["eff"] / 1.5) * (0.5 + 0.5 * agreement), 2)
    strong_single = any(c.trusted and (c.is_rera or c.kind == "structured") for c in best["cands"]) \
        and top.score >= 0.7
    if not trusted_domains:
        v.label = "Low"
        v.note = "only seen on unverified websites - check before using"
    elif v.confidence >= 0.55 and (len(trusted_domains) >= 2 or strong_single):
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


def rera_summary(cands: list[Candidate], pairs: list[dict]) -> dict:
    """Current RERA completion date, the original one, and whether it was extended.
    Prefers the Original -> Revised pair from a page quoting the project's RERA ID."""
    out = {"rera_orig_display": "", "rera_current_iso": "", "rera_current_display": "", "extension_found": "Not found",
           "rera_evidence": "", "rera_url": "", "rera_alternatives": ""}

    def fmt(ym):
        return _display(ym[0], ym[1], "month")

    good = [p for p in pairs if p["relevance"] >= 0.75]
    if good:
        best = max(good, key=lambda p: (p["id_match"], p["rev"], p["relevance"]))
        out.update(rera_orig_display=fmt(best["orig"]), rera_current_iso=f"{best['rev'][0]:04d}-{best['rev'][1]:02d}",
                   rera_current_display=fmt(best["rev"]), rera_evidence=best["text"], rera_url=best["url"],
                   extension_found=(f"Yes - extended {fmt(best['orig'])} -> {fmt(best['rev'])}"
                                    if best["rev"] > best["orig"] else "No - revised date same as original"))
    rera = [c for c in cands if c.is_rera and c.score >= 0.2]
    if rera:
        v = aggregate(rera, Counter())
        if not out["rera_current_iso"]:
            out.update(rera_current_iso=v.iso, rera_current_display=v.display, rera_evidence=v.evidence,
                       rera_url=v.best_url)
        out["rera_alternatives"] = v.alternatives
        ext = [c for c in rera if c.kind == "extension" and c.score >= 0.35]
        if ext and out["extension_found"] == "Not found":
            b = max(ext, key=lambda c: c.score)
            out["extension_found"] = f"Mentioned: {b.context[:150]}"
    return out


# ----------------------------------------------------------------------------- price

_U = r"(cr|crores?|crs?|lakhs?|lacs?|lac|l)\b"
_NUM = r"(\d{1,3}(?:\.\d{1,2})?)"
_RS = r"(?:₹|rs\.?|inr)"
_PRICE_RANGE = re.compile(rf"{_RS}\s*{_NUM}\s*(?:{_U})?\s*(?:-|–|—|to)\s*{_RS}?\s*{_NUM}\s*{_U}", re.I)
_PRICE_ONE = re.compile(rf"{_RS}\s*{_NUM}\s*{_U}(?!\s*(?:/|per)\s*(?:sq|month))", re.I)
_PRICE_FULL = re.compile(rf"{_RS}\s*(\d{{1,3}}(?:,\d{{2,3}}){{2,4}})(?!\s*(?:/|per|\d))", re.I)
_PER_SQFT = re.compile(rf"{_RS}\s*([\d,]{{3,7}}|\d+(?:\.\d+)?\s*k)\s*(?:/|per)\s*(?:sq\.?\s*ft|sqft|sq\.?\s*feet)",
                       re.I)
# Amounts that are not the flat's price.
_NOT_PRICE = re.compile(r"\b(?:emi|booking|token|maintenance|registration|stamp|rent|deposit|charges?|gst|loan|income"
                        r"|salary|turnover|revenue|invest|worth|valuation|project\s+cost|funding|raised|sales\s+of"
                        r"|crore\s+project|deal|acquir)", re.I)
_JSONLD_LOW = re.compile(r'"lowPrice"\s*:\s*"?(\d{5,})')
_JSONLD_HIGH = re.compile(r'"highPrice"\s*:\s*"?(\d{5,})')
MIN_PRICE, MAX_PRICE = 5e5, 1.5e9          # ₹5 L .. ₹150 Cr per unit
_KIND_RANK = {"page data": 4, "range": 3, "onwards": 2, "single": 1}
_CAROUSEL = re.compile(r"similar|you\s+may\s+(?:also\s+)?like|other\s+projects|nearby|recommended|also\s+viewed"
                       r"|trending|newly\s+launched|more\s+projects|projects\s+(?:in|near|by)|top\s+projects|explore"
                       r"|don.?t\s+miss|popular|featured|sponsored", re.I)


@dataclass
class PriceCand:
    lo: float            # rupees
    hi: float
    kind: str            # range / page data / onwards / single
    url: str
    domain: str
    trusted: bool
    weight: float
    origin: str
    context: str
    per_sqft: int = 0


def _rupees(num: str, unit: str | None) -> float:
    u = (unit or "").lower()
    return float(num) * (1e7 if u.startswith("c") else 1e5)


def fmt_inr(v: float) -> str:
    if v >= 1e7:
        return f"₹{v / 1e7:.2f}".rstrip("0").rstrip(".") + " Cr"
    return f"₹{v / 1e5:.2f}".rstrip("0").rstrip(".") + " L"


def _clean_ctx(text: str, a: int, b: int) -> str:
    return re.sub(r"\s+", " ", text[max(0, a - 60): b + 40]).strip()


def _price_ok_ctx(text: str, start: int, end: int) -> bool:
    return not _NOT_PRICE.search(text[max(0, start - 45): start]) and not _NOT_PRICE.search(text[end: end + 14])


def _sqft_values(text: str) -> list[tuple[int, int]]:
    out = []
    for m in _PER_SQFT.finditer(text):
        raw = m.group(1).lower().replace(",", "").replace(" ", "")
        v = int(float(raw[:-1]) * 1000) if raw.endswith("k") else int(float(raw))
        if 1000 <= v <= 150000:
            out.append((v, m.start()))
    return out


def price_candidates(text: str, url: str, matcher: NameMatcher, from_page: bool, html: str = "",
                     trusted: bool | None = None) -> list[PriceCand]:
    """Price quoted for this project on one page or snippet. On a page, an amount counts only when this
    project's name comes shortly before it with no 'similar projects' style heading in between - pages
    carry carousels of other projects, each with its own price."""
    dw, _ = domain_weight(url)
    if trusted is None:
        trusted = source_tier(url, matcher) in TRUSTED_TIERS
    host = _host(url)
    origin = "page" if from_page else "search snippet"
    anchor = matcher.anchor

    def near(pos: int) -> bool:
        # The whole name (not one word of it - "Green Valley" is not "Dhruv Valley") must come shortly before
        # the amount, after any "similar projects"-style heading.
        win = text[max(0, pos - 220): pos]
        cuts = list(_CAROUSEL.finditer(win))
        if not from_page and not cuts:
            return True
        k = win.lower().rfind(anchor) if anchor else -1
        if k < 0 or (cuts and cuts[-1].start() > k):
            return False
        # the last mention before the amount must be this project's full name
        return matcher.score(win[max(0, k - 45): k + len(anchor) + 45]) >= 0.8

    sqft = [v for v, pos in _sqft_values(text) if near(pos)]
    per = sorted(sqft)[len(sqft) // 2] if sqft else 0
    out: list[PriceCand] = []
    if from_page and html:
        lo, hi = _JSONLD_LOW.search(html), _JSONLD_HIGH.search(html)
        if lo and hi:
            a, b = float(lo.group(1)), float(hi.group(1))
            if MIN_PRICE <= a <= b <= MAX_PRICE:
                out.append(PriceCand(a, b, "page data", url, host, trusted, dw, "page data",
                                     f"lowPrice {lo.group(1)} / highPrice {hi.group(1)}", per))
    for m in _PRICE_RANGE.finditer(text):
        if not near(m.start()) or not _price_ok_ctx(text, m.start(), m.end()):
            continue
        a, b = _rupees(m.group(1), m.group(2) or m.group(4)), _rupees(m.group(3), m.group(4))
        if MIN_PRICE <= a <= b <= MAX_PRICE:
            out.append(PriceCand(a, b, "range", url, host, trusted, dw, origin,
                                 _clean_ctx(text, m.start(), m.end()), per))
            break  # the first range near the name is the headline price
    if not any(c.kind == "range" for c in out):
        singles = []
        for m in _PRICE_ONE.finditer(text):
            if near(m.start()) and _price_ok_ctx(text, m.start(), m.end()):
                v = _rupees(m.group(1), m.group(2))
                if MIN_PRICE <= v <= MAX_PRICE:
                    singles.append((v, m))
        for m in _PRICE_FULL.finditer(text):
            if near(m.start()) and _price_ok_ctx(text, m.start(), m.end()):
                v = float(m.group(1).replace(",", ""))
                if MIN_PRICE <= v <= MAX_PRICE:
                    singles.append((v, m))
        if singles:
            singles = singles[:6]
            v0, m0 = singles[0]
            onwards = re.search(r"onwards|starting|starts?\s+(?:at|from)|from", text[max(0, m0.start() - 25): m0.end() + 15],
                                re.I)
            lo, hi = min(v for v, _ in singles), max(v for v, _ in singles)
            kind = "onwards" if onwards else "single"
            out.append(PriceCand(lo, hi if kind == "single" else lo, kind, url, host, trusted, dw, origin,
                                 _clean_ctx(text, m0.start(), m0.end()), per))
    if not out and per:
        out.append(PriceCand(0, 0, "per sq.ft only", url, host, trusted, dw, origin, f"₹{per:,}/sq.ft", per))
    return out


def aggregate_price(cands: list[PriceCand]) -> dict:
    """Best price with one source URL. High = two trusted sites agree (within 20%); Medium = one trusted site."""
    out = {"price_display": "", "price_min": "", "price_max": "", "price_sqft": "", "price_label": "Not found",
           "price_url": "", "price_evidence": "", "price_alternatives": ""}
    full = [c for c in cands if c.lo]
    sqft = [c for c in cands if c.per_sqft and c.trusted] or [c for c in cands if c.per_sqft]
    if not full and not sqft:
        return out
    for c in full:
        if c.trusted and c.hi > 5 * c.lo:
            # "Rs 2.75 - 16.8 Cr": a spread like that mixes several listings, not one project's price list
            c.trusted = False
            c.context = f"(very wide range - may mix other listings) {c.context}"
    if full:
        # Big portals keep current prices; smaller sites often still show launch-time prices.
        best = max(full, key=lambda c: (c.trusted, _on(c.domain, MAJOR_PORTALS), _KIND_RANK.get(c.kind, 0),
                                        c.origin != "search snippet", c.weight))
        agree = {c.domain for c in full if c.trusted and c.domain != best.domain
                 and abs(c.lo - best.lo) <= 0.2 * best.lo}
        if not best.trusted:
            label = "Low"
        elif agree:
            label = "High"
        else:
            label = "Medium"
        disp = (f"{fmt_inr(best.lo)} - {fmt_inr(best.hi)}" if best.hi > best.lo * 1.02
                else f"{fmt_inr(best.lo)} onwards" if best.kind == "onwards" else fmt_inr(best.lo))
        per = best.per_sqft or (sqft[0].per_sqft if sqft else 0)
        others, seen = [], {best.domain}
        for c in sorted(full, key=lambda c: -c.weight):
            if c.domain not in seen:
                seen.add(c.domain)
                others.append(f"{fmt_inr(c.lo)}{' - ' + fmt_inr(c.hi) if c.hi > c.lo * 1.02 else ''} ({c.domain})")
        out.update(price_display=disp, price_min=int(best.lo), price_max=int(best.hi), price_label=label,
                   price_url=best.url, price_evidence=f"[{best.origin}] {best.context[:250]}",
                   price_sqft=f"₹{per:,}/sq.ft" if per else "", price_alternatives="; ".join(others[:4]))
    else:
        b = max(sqft, key=lambda c: (c.trusted, c.weight))
        out.update(price_sqft=f"₹{b.per_sqft:,}/sq.ft", price_label="Medium" if b.trusted else "Low",
                   price_url=b.url, price_evidence=f"[{b.origin}] {b.context}")
    return out
