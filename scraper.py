#!/usr/bin/env python3
"""
Ohio Vehicle Bid Tracker - scraper
==================================
Finds Ohio public bid notices (townships, cities, villages, counties, state)
for NEW vehicles (dump/plow trucks, pickups, chassis, utility, fire/EMS, equipment),
saves them to data/bids.json, rebuilds the website in docs/, and sends alerts.

Pure Python standard library - no installs needed (Python 3.9+).

  python scraper.py               run one check
  python scraper.py --watch 15    check every 15 minutes and serve the website
                                  at http://localhost:8765 on this computer

Alerts (optional, set as environment variables or GitHub secrets):
  NTFY_TOPIC   free instant phone push via the ntfy app (https://ntfy.sh)
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_EMAIL   email alerts
"""
import argparse, datetime as dt, email, email.header, email.utils, imaplib, hashlib, html, json, os, re, shutil, smtplib
import sys, threading, time, urllib.parse, urllib.request, webbrowser
from email.message import EmailMessage
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
DOCS = os.path.join(ROOT, "docs")
BIDS_FILE = os.path.join(DATA, "bids.json")
SEEN_FILE = os.path.join(DATA, "seen_urls.json")
STATE_FILE = os.path.join(DATA, "state.json")
SOURCES_FILE = os.path.join(ROOT, "sources.json")
UA = "OhioBidTracker/1.0 (+https://skeltrent.github.io/BidRequests; public bid notice reader)"

# ---------------------------------------------------------------- keywords
VEHICLE = re.compile(r"\b(trucks?|pick-?ups?|chassis|dump\s*(?:truck|body)|plow|snow\s*plow|spreader|"
                     r"f-?\s?[2-7]50|silverado|sierra|ram\s?[2-5]500|freightliner|international\s+(?:hv|cv|mv)|"
                     r"western\s*star|mack|peterbilt|kenworth|ambulance|brush\s+truck|fire\s+(?:engine|apparatus|truck)|"
                     r"pumper|tanker|rescue\s+(?:truck|vehicle)|bucket\s+truck|aerial|utility\s+(?:truck|body|vehicle)|"
                     r"vehicles?|cab\s*&?\s*chassis|tandem|single\s+axle|backhoe|loader|excavator|grader|"
                     r"mower|tractor|street\s+sweeper|sweeper|vactor|jet\s*vac|police\s+(?:cruiser|vehicle|interceptor)|"
                     r"suv|van|bus|tahoe|expedition|explorer|interceptor|durango|equinox|colorado|ranger|f-?150|gator|utv|"
                     r"side[\s-]by[\s-]side|utility\s+vehicle|motorcycle|transit\s+(?:van|vehicle|connect)|box\s+truck|"
                     r"refuse\s+truck|garbage\s+truck|packer|roll[\s-]?off|hook\s*lift|water\s+truck|flusher|"
                     r"skid\s*steer|dozer|truck\s+tractor|semi[\s-]?tractor)\b", re.I)
BIDDY = re.compile(r"\b(bids?|bidders?|invitation\s+to\s+bid|request\s+for\s+(?:bids|proposals?|quotes?)|rfp|rfq|ifb|itb|"
                   r"sealed\s+(?:bids|proposals)|proposals|quotes?|legal\s+notice|notice\s+to\s+bidders)\b", re.I)
SALE = re.compile(r"\b(surplus|for\s+sale|sale\s+of|sell(?:ing)?|auction|govdeals|declared?\s+(?:as\s+)?excess|"
                  r"no\s+longer\s+needed|as[\s-]is)\b", re.I)
NOISE = re.compile(r"\b(toy|diecast|die-cast|collectible|lyrics|nfl|nba|recipe|obituar|crash|accident|police\s+said|"
                   r"food\s+truck\s+festival|bidding\s+war|bidens?)\b", re.I)
NOT_VEHICLE_CONTEXT = re.compile(r"\b(trash|refuse\s+collection|mowing|lawn|paving|resurfacing|culvert|curb|"
                                 r"roof|hvac|concrete|demolition|waterline|sewer|asphalt)\b", re.I)

CATEGORIES = [
    ("fire", r"ambulance|brush\s+truck|fire|pumper|tanker|rescue|ems\b|medic"),
    ("utility", r"bucket|aerial|utility|service\s+body|crane|compressor|sweeper|vactor|jet\s*vac"),
    ("dump", r"dump|plow|spreader|salt|tandem|single\s+axle|class\s*[6-8]|freightliner|international|"
             r"western\s*star|mack|peterbilt|kenworth|cab\s*&?\s*chassis"),
    ("equipment", r"backhoe|loader|excavator|grader|tractor|mower|skid\s*steer|roller|paver"),
    ("pickup", r"pick-?up|one[\s-]ton|1[\s-]ton|f-?\s?[2-5]50|silverado|sierra|ram|half[\s-]ton|3/4|suv|van|"
               r"cruiser|interceptor|vehicle"),
]

OHIO_COUNTIES = ("Adams Allen Ashland Ashtabula Athens Auglaize Belmont Brown Butler Carroll Champaign Clark "
                 "Clermont Clinton Columbiana Coshocton Crawford Cuyahoga Darke Defiance Delaware Erie Fairfield "
                 "Fayette Franklin Fulton Gallia Geauga Greene Guernsey Hamilton Hancock Hardin Harrison Henry "
                 "Highland Hocking Holmes Huron Jackson Jefferson Knox Lake Lawrence Licking Logan Lorain Lucas "
                 "Madison Mahoning Marion Medina Meigs Mercer Miami Monroe Montgomery Morgan Morrow Muskingum "
                 "Noble Ottawa Paulding Perry Pickaway Pike Portage Preble Putnam Richland Ross Sandusky Scioto "
                 "Seneca Shelby Stark Summit Trumbull Tuscarawas Union Van_Wert Vinton Warren Washington Wayne "
                 "Williams Wood Wyandot").replace("_", " ").split()
OHIO_COUNTIES = [c.replace("Van", "Van Wert") if c == "Van" else c for c in OHIO_COUNTIES]
OHIO_COUNTIES = [c for c in OHIO_COUNTIES if c != "Wert"]
COUNTY_RE = re.compile(r"\b(" + "|".join(re.escape(c) for c in OHIO_COUNTIES) + r")\s+(?:County|Co\.)", re.I)
OHIO_RE = re.compile(r"\bOhio\b|,\s*OH\b|\bOH\s+4[3-5]\d{3}\b|" + COUNTY_RE.pattern, re.I)
OTHER_STATES = re.compile(r"\b(Michigan|Indiana|Pennsylvania|Kentucky|West Virginia|New York|Illinois|Texas|"
                          r"Florida|Wisconsin|Minnesota|Iowa|Missouri|Tennessee|Georgia|Virginia|Maryland|"
                          r"New Jersey|California|Ontario|Canada)\b")

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_WORDS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_RE = re.compile(r"(" + DATE_WORDS + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?|(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I)
DUE_CUE = re.compile(r"(until|due|no\s+later\s+than|received\s+(?:by|until|at)|deadline|closes?|close\s+date|"
                     r"opened|opening|submitted\s+by|on\s+or\s+before)", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|noon)\b", re.I)


# ---------------------------------------------------------------- helpers
def now():
    return dt.datetime.now(dt.timezone.utc)


def today_str():
    return dt.datetime.now().strftime("%Y-%m-%d")


RUN_LOG = []


def log(*a):
    line = f"[{dt.datetime.now():%H:%M:%S}] " + " ".join(str(x) for x in a)
    RUN_LOG.append(line)
    print(line, flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


_ROBOTS = {}
_ROBOTS_LOCK = threading.Lock()


def allowed_by_robots(url):
    """Honor each site's robots.txt for page visits (cached per site)."""
    import urllib.robotparser
    u = urllib.parse.urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    with _ROBOTS_LOCK:
        rp = _ROBOTS.get(base)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            txt, _ = fetch(base + "/robots.txt", timeout=10)
            rp.parse(txt.splitlines())
        except Exception:
            rp.parse([])  # no robots.txt (or unreachable) = no restrictions
        with _ROBOTS_LOCK:
            _ROBOTS[base] = rp
    try:
        return rp.can_fetch("OhioBidTracker", url)
    except Exception:
        return True


def fetch_page(url, timeout=25):
    """Fetch a normal web page, but only if the site's robots.txt allows it."""
    if not allowed_by_robots(url):
        raise PermissionError("blocked by robots.txt")
    return fetch(url, timeout)


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(4_000_000)
        cs = r.headers.get_content_charset() or "utf-8"
        return raw.decode(cs, "replace"), r.geturl()


def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def norm_title(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def make_id(url, title):
    return hashlib.sha1((norm_title(title) + "|" + (url or "")).encode()).hexdigest()[:14]


def parse_date(m, ref=None):
    ref = ref or dt.date.today()
    try:
        if m.group(1):
            mon = MONTHS[m.group(1)[:3].lower()]
            day = int(m.group(2))
            yr = int(m.group(3)) if m.group(3) else ref.year
            d = dt.date(yr, mon, day)
            if not m.group(3) and d < ref - dt.timedelta(days=60):
                d = dt.date(yr + 1, mon, day)
            return d
        mo, day, yr = int(m.group(4)), int(m.group(5)), int(m.group(6))
        yr = yr + 2000 if yr < 100 else yr
        return dt.date(yr, mo, day)
    except (ValueError, KeyError):
        return None


def find_due(text, posted=None):
    """Return (YYYY-MM-DD, time) for the date that follows a 'due/until/opened' cue."""
    best = None
    for cue in DUE_CUE.finditer(text):
        window = text[cue.end(): cue.end() + 140]
        m = DATE_RE.search(window)
        if m:
            d = parse_date(m)
            if d and (not posted or d >= posted - dt.timedelta(days=3)):
                t = TIME_RE.search(text[max(0, cue.start() - 40): cue.end() + 160])
                best = (d.isoformat(), t.group(1).upper().replace(".", "") if t else "")
                break
    return best or ("", "")


POSTED_CUE = re.compile(r"(posted|published|dated|date\s+of\s+(?:notice|publication)|advertised|first\s+publication|"
                        r"publish(?:ed)?\s+(?:on|dates?))\s*:?", re.I)


def find_posted(text):
    """Date the notice says it was posted/published, if it says so."""
    for cue in POSTED_CUE.finditer(text):
        m = DATE_RE.search(text[cue.end(): cue.end() + 40])
        if m:
            d = parse_date(m)
            if d and d <= dt.date.today() + dt.timedelta(days=1):
                return d
    return None


def find_agency(text):
    pats = [
        r"((?:[A-Z][a-zA-Z.']+\s){1,3}Township)\b",
        r"\b((?:City|Village)\s+of\s+(?:[A-Z][a-zA-Z.']+\s?){1,3})",
        r"\b((?:[A-Z][a-zA-Z.']+\s){1,2}County\s+(?:Engineer|Commissioners|Board\s+of\s+Commissioners|Sheriff))",
        r"\b((?:[A-Z][a-zA-Z.']+\s){1,2}(?:County))\b",
        r"\b(Ohio\s+Department\s+of\s+[A-Z][a-zA-Z ]{3,40}?)(?:[,.;(]|\s{2}|$)",
    ]
    bad = re.compile(r"^(The|Board|Trustees|Sealed|Notice|Legal|Bids?|Of|And|For|In|Said|Clerk|Fiscal)\b", re.I)
    for p in pats:
        for m in re.finditer(p, text):
            a = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
            a = re.sub(r"^(?:The|Board of(?: Township)? Trustees of|Trustees of|Of)\s+", "", a)
            if not bad.match(a) and len(a) > 6:
                return a
    return ""


def classify_level(agency, text):
    a = (agency or "") + " " + text[:300]
    if re.search(r"\bTownship\b|\bTwp\b", agency or ""):
        return "township"
    if re.search(r"\b(City|Village)\s+of\b", agency or ""):
        return "city"
    if re.search(r"\bCounty\b", agency or ""):
        return "county"
    if re.search(r"Ohio\s+Department|State\s+of\s+Ohio|ODOT|OhioBuys|\bDAS\b", a):
        return "state"
    if re.search(r"\bTownship\b", text):
        return "township"
    if re.search(r"\bVillage\b|\bCity\b", text):
        return "city"
    if re.search(r"\bCounty\b", text):
        return "county"
    return "other"


def classify_category(text):
    for name, pat in CATEGORIES:
        if re.search(pat, text, re.I):
            return name
    return "other"


def specs_from(text):
    out = []
    for pat in [r"\b(20[2-3]\d\s+or\s+newer)", r"\b(\d{2},?\d{3}\s*(?:lb|lbs|pound)s?\.?\s*GVW\w*)",
                r"\b(\d\s*x\s*\d|4WD|4x4|2WD)\b", r"\b(single\s+axle|tandem\s+axle|tandem)\b",
                r"\b(diesel|gas(?:oline)?\s+engine)\b", r"\b(F-?\s?[2-7]50\s?\w*)", r"\b(Allison\s+\w+[\w\s-]{0,20})",
                r"\b(qty\.?\s*\d+|\(\d\)\s+\w+)", r"\b(trade[\s-]in[^.;]{0,60})"]:
        m = re.search(pat, text, re.I)
        if m:
            s = m.group(1).strip()
            if s.lower() not in [x.lower() for x in out]:
                out.append(s)
    return out[:5]


GENERIC_TITLE = re.compile(r"^(legal\s+notice|public\s+notice|notice\s+to\s+bidders|invitation\s+to\s+bid|"
                           r"advertisement\s+for\s+bids?|request\s+for\s+(?:bids|quotes|proposals)|bid\s+notice|"
                           r"notice|bids?|sealed\s+bids)[\s.:,-]*$", re.I)


def better_title(title, text):
    """Replace a generic heading like 'NOTICE TO BIDDERS' with what is actually being bought."""
    t = clean(title)
    if 28 < len(t) <= 100 and not GENERIC_TITLE.match(t) and VEHICLE.search(t):
        return t
    m = re.search(r"\b(?:for|purchase\s+of|purchasing|furnish(?:ing)?|supply(?:ing)?)\s+(?:the\s+)?(?:purchase\s+of\s+)?"
                  r"((?:one|two|three|\(\d\)|\d+|a|an|new)[^.;]{5,110}?)(?=[.;]|,\s*(?:due|bids|to\s+be|which)|\s+(?:until|by|at|on|due)\s|$)", text, re.I)
    if m and VEHICLE.search(m.group(1)):
        what = m.group(1).strip(" ,")
        return (what[0].upper() + what[1:])[:140]
    return (t[:100].rsplit(" ", 1)[0] + "…") if len(t) > 100 else (t or "Bid notice")


def build_record(title, url, text, src, posted=None):
    text = clean(text)
    title = better_title(title, text)
    full = f"{title}. {text}"
    posted = posted or find_posted(full)
    posted_d = posted or dt.date.today()
    agency = src.get("agency") or find_agency(full) or src.get("name", "")
    m = COUNTY_RE.search(full)
    county = (m.group(1).title() if m else "") or src.get("county") or ""
    level = src.get("level") or classify_level(agency, full)
    due, due_time = find_due(full, posted_d)
    rec = {
        "agency": agency,
        "level": level,
        "county": county,
        "kind": "sale" if SALE.search(full) else "purchase",
        "category": classify_category(title + " " + text[:600]),
        "title": title[:180],
        "summary": text[:280] + ("…" if len(text) > 280 else ""),
        "specs": specs_from(full),
        "postedDate": posted.isoformat() if posted else "",
        "dueDate": due,
        "dueTime": due_time,
        "source": src.get("name", ""),
        "url": url,
        "foundDate": today_str(),
        "auto": True,
    }
    if src.get("city"):
        rec["city"] = src["city"]
    if src.get("check_ohio") and not OHIO_RE.search(full):
        rec["unconfirmedLocation"] = True
    return {k: v for k, v in rec.items() if v not in ("", [], None)}


def relevant(title, text, src):
    blob = f"{title} {text}"
    if NOISE.search(blob):
        return False
    if not VEHICLE.search(blob):
        return False
    # avoid "vehicle" matches inside clearly non-vehicle projects (e.g. paving bids)
    if NOT_VEHICLE_CONTEXT.search(title) and not re.search(r"truck|chassis|plow|ambulance|pick-?up", title, re.I):
        return False
    if not (src.get("bid_page") or BIDDY.search(blob)):
        return False
    if src.get("check_ohio"):
        if OTHER_STATES.search(blob) and not OHIO_RE.search(blob):
            return False
    return True


# ---------------------------------------------------------------- parsers
class LinkAndHeadingParser(HTMLParser):
    """Collects <a> links and heading/paragraph blocks with nearby text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self._stack, self._buf, self._href = [], [], [], None
        self.blocks, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []
        if tag in ("h1", "h2", "h3", "h4", "h5", "p", "li", "td", "div", "article", "section", "br", "tr"):
            self.blocks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        if tag == "a" and self._href is not None:
            self.items.append((clean(" ".join(self._buf)), self._href, len("".join(self.blocks))))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        data = data.replace("\r", " ").replace("\n", " ")
        self.blocks.append(data)
        if self._href is not None:
            self._buf.append(data)


def parse_page(html_text, base_url):
    p = LinkAndHeadingParser()
    try:
        p.feed(html_text)
    except Exception:
        pass
    full = "".join(p.blocks)
    links = []
    for text, href, pos in p.items:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        ctx = clean(full[max(0, pos - len(text) - 60): pos + 320])
        links.append((text, urllib.parse.urljoin(base_url, href), ctx))
    # text blocks: split page into notice-sized chunks around bid cues
    lines = [clean(x) for x in full.split("\n")]
    lines = [x for x in lines if x]
    return links, lines


def parse_rss(xml_text):
    items = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "replace") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return items
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for it in root.iter("item"):
        g = lambda t: (it.findtext(t) or "").strip()
        src_el = it.find("source")
        items.append({"title": clean(g("title")), "link": g("link"), "desc": clean(g("description")),
                      "date": g("pubDate"), "source": src_el.text.strip() if src_el is not None and src_el.text else ""})
    for it in root.findall("a:entry", ns):
        link = it.find("a:link", ns)
        items.append({"title": clean(it.findtext("a:title", "", ns)), "link": link.get("href") if link is not None else "",
                      "desc": clean(it.findtext("a:summary", "", ns) or it.findtext("a:content", "", ns)),
                      "date": it.findtext("a:updated", "", ns) or it.findtext("a:published", "", ns), "source": ""})
    return items


def rss_date(s):
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        pass
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- source runners
def run_rss(src, max_age_days, seen=None, follow_budget=None):
    out = []
    seen = seen if seen is not None else {}
    follow_budget = follow_budget or [0]
    is_web = "bing.com" in src["url"]
    xml, _ = fetch(src["url"])
    for it in parse_rss(xml):
        posted = rss_date(it["date"])
        if posted and posted < dt.date.today() - dt.timedelta(days=max_age_days):
            continue
        title, text = it["title"], f"{it['desc']} {it['source']}"
        if it["source"] and title.endswith(" - " + it["source"]):
            title = title[: -len(it["source"]) - 3].strip()
        if not relevant(title, text, src):
            continue
        link = it["link"]
        if is_web:
            # Web results carry no reliable date: read the notice itself and keep it only if it is still open.
            if link in seen or follow_budget[0] <= 0 or link.lower().endswith(".pdf"):
                continue
            follow_budget[0] -= 1
            seen[link] = today_str()
            try:
                body, _ = fetch_page(link)
                _, lines = parse_page(body, link)
                text = " ".join(lines)[:4000]
            except Exception:
                continue
            if not relevant(title, text, src):
                continue
        s = dict(src)
        if it["source"]:
            s["name"] = f"{src.get('name', 'News')} – {it['source']}"
        rec = build_record(title, link, text, s, posted)
        if is_web and not rec.get("dueDate"):
            continue
        out.append(rec)
    return out


def run_page(src, seen, follow_budget):
    page, final_url = fetch_page(src["url"])
    return run_page_html(src, page, final_url, seen, follow_budget)


def run_page_html(src, page, final_url, seen, follow_budget):
    out = []
    links, lines = parse_page(page, final_url)
    host = urllib.parse.urlparse(final_url).netloc
    candidates = []
    for text, href, ctx in links:
        if len(text) < 6:
            continue
        blob = f"{text} {ctx}"
        if BIDDY.search(blob) or src.get("bid_page"):
            if VEHICLE.search(text) or (src.get("follow") and BIDDY.search(text)):
                candidates.append((text, href, ctx))
    # notices written directly on the page (no link): chunk by headings/lines
    cue = re.compile(r"notice\s+to\s+bidders|sealed\s+(?:bids|proposals)|invitation\s+to\s+bid|requesting\s+bids|"
                     r"accepting\s+(?:sealed\s+)?bids|bid\s+proposal|request\s+for\s+(?:bids|proposals|quotes)", re.I)
    link_texts = {t for t, _, _ in links}
    covered = -1
    for i, line in enumerate(lines):
        if i <= covered or len(line) < 15 or line in link_texts or not cue.search(line):
            continue
        # the notice runs until the next line that starts another notice
        end = i + 1
        while end < len(lines) and end < i + 8 and not (cue.search(lines[end]) and len(lines[end]) < 120) \
                and lines[end] not in link_texts:
            end += 1
        chunk = " ".join(lines[i:end])
        covered = end - 1
        if VEHICLE.search(chunk) and not NOT_VEHICLE_CONTEXT.search(chunk[:250]):
            if len(line) <= 140:
                title = line
            elif i and 8 < len(lines[i - 1]) < 140 and lines[i - 1] not in link_texts:
                title = lines[i - 1]
            else:
                title = line[:140] + "…"
            candidates.append((title, final_url + "#" + make_id("", title), chunk))
    for title, href, ctx in candidates:
        detail = ctx
        is_doc = urllib.parse.urlparse(href).netloc == host or href.lower().endswith(".pdf")
        if src.get("follow") and href not in seen and follow_budget[0] > 0 and not href.lower().endswith(".pdf") \
                and "#" not in href and is_doc:
            follow_budget[0] -= 1
            seen[href] = today_str()
            try:
                body, _ = fetch_page(href)
                _, dl = parse_page(body, href)
                detail = " ".join(dl)[:3000]
            except Exception as e:
                log("   follow failed:", href, e)
        if relevant(title, detail, src):
            out.append(build_record(title, href, detail, src))
    return out


def run_email(cfg_mail):
    """Read bid-alert emails (DemandStar, BidNet, OhioBuys, Google Alerts, newspapers…) from an IMAP inbox."""
    host, user, pw = os.environ.get("IMAP_HOST"), os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASS")
    if not (host and user and pw):
        return []
    out = []
    box = os.environ.get("IMAP_FOLDER") or cfg_mail.get("folder", "INBOX")
    since = (dt.date.today() - dt.timedelta(days=cfg_mail.get("days", 3))).strftime("%d-%b-%Y")
    M = imaplib.IMAP4_SSL(host)
    try:
        M.login(user, pw)
        M.select(f'"{box}"', readonly=True)
        typ, data = M.search(None, "SINCE", since)
        for num in (data[0].split() if data and data[0] else [])[-200:]:
            typ, msgdata = M.fetch(num, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            out.extend(parse_email_bytes(msgdata[0][1]))
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out


def parse_email_bytes(raw):
    msg = email.message_from_bytes(raw)
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    sender = email.utils.parseaddr(msg.get("From", ""))[1]
    sent = None
    try:
        sent = email.utils.parsedate_to_datetime(msg.get("Date")).date()
    except Exception:
        pass
    html_part, text_part = "", ""
    for part in msg.walk():
        ct = part.get_content_type()
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ct == "text/html":
            html_part += payload
        elif ct == "text/plain":
            text_part += payload
    src = {"name": f"Email – {sender or 'alert'}", "check_ohio": True}
    recs = []
    if html_part:
        links, lines = parse_page(html_part, "https://mail.invalid/")
        body = " ".join(lines)
        for text, href, ctx in links:
            if href.startswith("https://mail.invalid") or len(text) < 8:
                continue
            if VEHICLE.search(text) and relevant(text, ctx, src):
                recs.append(build_record(text, href, ctx, src, sent))
    else:
        body = text_part
    if not recs and relevant(subject, body, src):
        url = (re.search(r"https?://[^\s<>\"')]+", body) or [None])[0] if body else None
        recs.append(build_record(subject, url or "", body[:3000], src, sent))
    return recs


def run_sam(cfg_sam):
    """Federal opportunities located in Ohio from the free SAM.gov API (needs SAM_API_KEY)."""
    key = os.environ.get("SAM_API_KEY")
    if not key:
        return []
    out = []
    frm = (dt.date.today() - dt.timedelta(days=cfg_sam.get("days", 14))).strftime("%m/%d/%Y")
    to = dt.date.today().strftime("%m/%d/%Y")
    for kw in cfg_sam.get("keywords", ["truck"]):
        url = ("https://api.sam.gov/opportunities/v2/search?" + urllib.parse.urlencode(
            {"api_key": key, "postedFrom": frm, "postedTo": to, "state": "OH", "title": kw, "limit": 100}))
        try:
            data = json.loads(fetch(url)[0])
        except Exception as e:
            log("  SAM.gov:", e)
            continue
        for o in data.get("opportunitiesData", []):
            title = o.get("title", "")
            if not VEHICLE.search(title):
                continue
            pop = (o.get("placeOfPerformance") or {})
            city = ((pop.get("city") or {}).get("name") or "")
            rec = build_record(title, o.get("uiLink", ""), f"{o.get('fullParentPathName','')} {city}",
                               {"name": "SAM.gov (federal)", "agency": re.sub(r",\s*Department\s+of$", "", (o.get("fullParentPathName") or "Federal agency").split(".")[0].title(), flags=re.I),
                                "level": "federal", "city": city.title()},
                               rss_date(o.get("postedDate")))
            if o.get("responseDeadLine"):
                rec["dueDate"] = o["responseDeadLine"][:10]
            out.append(rec)
    return out


def _walk_strings(obj, out, depth=0):
    if depth > 4:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                out.setdefault(k.lower(), v)
            else:
                _walk_strings(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:5]:
            _walk_strings(v, out, depth + 1)


def run_column(cfg_col):
    """Ohio newspaper legal notices through Column's public-notice API (powers PublicNoticesOhio.com).
    Needs COLUMN_API_TOKEN (request one from Column)."""
    token = os.environ.get("COLUMN_API_TOKEN")
    if not token:
        return []
    out = []
    for endpoint in cfg_col.get("endpoints", ["https://utility.column.us/v1/ohio/public_notices"]):
        for page in range(cfg_col.get("pages", 3)):
            url = endpoint + ("&" if "?" in endpoint else "?") + f"page={page}"
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            items = data.get("data", data if isinstance(data, list) else [])
            if not items:
                break
            for it in items:
                f = {}
                _walk_strings(it, f)
                text = clean(f.get("text") or f.get("content") or f.get("body") or f.get("description") or "")
                title = clean(f.get("title") or f.get("name") or text[:120])
                link = f.get("url") or f.get("link") or f.get("pdfurl") or "https://www.publicnoticesohio.com/"
                np = it.get("newspaper") if isinstance(it, dict) else None
                paper = (np.get("name") if isinstance(np, dict) else np) or f.get("newspapername") or f.get("publication") or ""
                posted = rss_date(f.get("publicationdate") or f.get("published_at") or f.get("createdat") or f.get("date") or "")
                src = {"name": "Newspaper legal notice" + (f" – {paper}" if paper else ""), "check_ohio": False}
                if relevant(title, text, src) and not NOT_VEHICLE_CONTEXT.search(title):
                    out.append(build_record(title, link, text, src, posted))
    return out


def discover_bid_pages(cfg_disc, state):
    """Find more Ohio agency bid pages built on CivicPlus (…/Bids.aspx) and remember their bid RSS feeds."""
    found = state.setdefault("discovered_feeds", {})
    if not cfg_disc.get("enabled"):
        return []
    last = state.get("last_discovery", "")
    if last == today_str():
        return []  # once a day is plenty
    state["last_discovery"] = today_str()
    added = []
    for q in cfg_disc.get("queries", []):
        try:
            xml, _ = fetch("https://www.bing.com/search?format=rss&count=50&q=" + urllib.parse.quote(q))
        except Exception as e:
            log("  discovery failed:", e)
            continue
        for it in parse_rss(xml):
            u = urllib.parse.urlparse(it["link"])
            if "bids.aspx" not in u.path.lower() or not u.netloc:
                continue
            blob = f"{it['title']} {it['desc']}"
            if not OHIO_RE.search(blob) and not u.netloc.endswith(".oh.us"):
                continue
            base = f"{u.scheme}://{u.netloc}"
            if base not in found:
                found[base] = {"name": clean(it["title"])[:80] or u.netloc, "since": today_str()}
                added.append(base)
    if added:
        log(f"  discovered {len(added)} new agency bid pages")
    return added


# ---------------------------------------------------------------- outputs
def write_site(bids, meta):
    os.makedirs(DOCS, exist_ok=True)
    save_json(os.path.join(DOCS, "bids.json"), {"meta": meta, "bids": bids})
    # RSS feed so you can follow new bids in any feed reader
    items = []
    for b in sorted(bids, key=lambda x: x.get("foundDate", ""), reverse=True)[:100]:
        desc = f"{b.get('agency','')} · {b.get('county','')} County · due {b.get('dueDate','?')} — {b.get('summary','')}"
        items.append(f"<item><title>{html.escape(b['title'])}</title><link>{html.escape(b.get('url',''))}</link>"
                     f"<guid isPermaLink=\"false\">{b['id']}</guid><description>{html.escape(desc)}</description></item>")
    feed = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>BidRequests</title>'
            '<link>./</link><description>New Ohio public bids for vehicles</description>' + "".join(items) + "</channel></rss>")
    with open(os.path.join(DOCS, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(feed)


def notify(new):
    if not new:
        return
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    site = os.environ.get("SITE_URL", "").strip()
    if topic:
        for b in new[:10]:
            body = f"{b.get('agency','')} ({b.get('county','?')} Co.)\nDue: {b.get('dueDate') or 'see notice'} {b.get('dueTime','')}"
            req = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}", data=body.encode(),
                headers={"Title": ("New bid: " + b["title"])[:120].encode("ascii", "ignore").decode(),
                         "Tags": "truck", "Click": b.get("url") or site or "https://ntfy.sh"})
            try:
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as e:
                log("ntfy failed:", e)
        if len(new) > 10:
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"https://ntfy.sh/{urllib.parse.quote(topic)}", data=f"+{len(new)-10} more new bids".encode(),
                    headers={"Click": site or "https://ntfy.sh"}), timeout=15).read()
            except Exception:
                pass
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_EMAIL")
    if host and to:
        msg = EmailMessage()
        msg["Subject"] = f"{len(new)} new Ohio vehicle bid{'s' if len(new) > 1 else ''}"
        msg["From"] = os.environ.get("SMTP_USER", to)
        msg["To"] = to
        lines = [f"• {b['title']}\n  {b.get('agency','')} – {b.get('county','?')} County – due {b.get('dueDate') or 'see notice'}\n  {b.get('url','')}"
                 for b in new]
        msg.set_content("\n\n".join(lines) + (f"\n\nTracker: {site}" if site else ""))
        try:
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as s:
                s.starttls()
                s.login(os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", ""))
                s.send_message(msg)
        except Exception as e:
            log("email failed:", e)


# ---------------------------------------------------------------- main
def run_once(sweep=False):
    """Quick run (feeds, searches, portals, every 15 min) or the daily full sweep of every agency website."""
    cfg = load_json(SOURCES_FILE, {})
    db = load_json(BIDS_FILE, {"bids": []})
    bids = db.get("bids", [])
    seen = load_json(SEEN_FILE, {})
    ids = {b["id"] for b in bids}
    titles = {(norm_title(b.get("agency")), norm_title(b["title"])) for b in bids}
    urls = {b.get("url") for b in bids if b.get("url")}
    max_age = cfg.get("max_age_days", 21)
    follow_budget = [cfg.get("max_follows_per_run", 40)]
    found, errors = [], 0

    def gnews(q):
        return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:14d")
                + "&hl=en-US&gl=US&ceid=US:en")

    def bing(q):
        return "https://www.bing.com/search?format=rss&count=50&q=" + urllib.parse.quote(q)

    sources = []
    web_turn = True
    if not sweep:
        run_state = load_json(STATE_FILE, {})
        run_n = run_state.get("run_n", 0)
        run_state["run_n"] = run_n + 1
        save_json(STATE_FILE, run_state)
        web_turn = run_n % max(1, int(cfg.get("web_search_every_n_runs", 4))) == 0  # spread web searches out
        for q in cfg.get("google_news_queries", []):
            sources.append({"type": "rss", "name": "Google News", "check_ohio": True, "url": gnews(q)})
        if web_turn:
            for q in cfg.get("bing_queries", []):
                sources.append({"type": "rss", "name": "Web search", "check_ohio": True, "url": bing(q)})

        # County sweep: a few of Ohio's 88 counties per run, so the whole state is covered on rotation.
        county_cfg = cfg.get("county_sweep", {})
        if county_cfg.get("enabled"):
            state = load_json(STATE_FILE, {})
            start = state.get("sweep_pos", 0) % len(OHIO_COUNTIES)
            per = max(1, int(county_cfg.get("per_run", 8)))
            batch = [OHIO_COUNTIES[(start + i) % len(OHIO_COUNTIES)] for i in range(per)]
            for c in batch:
                q = county_cfg.get("query", "\"{county} County\" Ohio bids truck OR chassis OR plow OR vehicle").replace("{county}", c)
                sources.append({"type": "rss", "name": f"County sweep – {c}", "county": c, "check_ohio": True, "url": gnews(q)})
                if county_cfg.get("use_bing", True) and web_turn:
                    sources.append({"type": "rss", "name": f"County sweep – {c}", "county": c, "check_ohio": True, "url": bing(q)})
            state["sweep_pos"] = (start + per) % len(OHIO_COUNTIES)
            state["last_sweep"] = batch
            save_json(STATE_FILE, state)
        for s in cfg.get("rss", []):
            sources.append({"type": "rss", **s})
        # Agency bid feeds discovered automatically (CivicPlus sites)
        st = load_json(STATE_FILE, {})
        discover_bid_pages(cfg.get("discovery", {}), st)
        save_json(STATE_FILE, st)
        known = {x.get("url", "").split("/RSSFeed")[0].lower() for x in cfg.get("rss", [])}
        for base, info in st.get("discovered_feeds", {}).items():
            if base.lower() not in known:
                sources.append({"type": "rss", "name": f"Bid feed – {info.get('name', base)}", "bid_page": True,
                                "check_ohio": False, "url": base + "/RSSFeed.aspx?ModID=76&CID=All-bids.xml"})
        for s in cfg.get("pages", []):
            sources.append({"type": "page", **s})

        for src in sources:
            try:
                recs = run_rss(src, max_age, seen, follow_budget) if src["type"] == "rss" else run_page(src, seen, follow_budget)
                # never announce a bid whose closing date has already passed
                recs = [r for r in recs if not (r.get("dueDate") and r["dueDate"] < today_str())]
                found.extend(recs)
                log(f"{len(recs):3d} matches  {src.get('name') or src['url'][:70]}")
            except Exception as e:
                errors += 1
                log(f"  ERR  {src.get('name') or src['url'][:70]}: {e}")

        for name, fn, arg in (("Email inbox", run_email, cfg.get("email", {})), ("SAM.gov", run_sam, cfg.get("sam_gov", {})),
                              ("Newspaper notices (Column API)", run_column, cfg.get("column_api", {}))):
            try:
                recs = [r for r in fn(arg) if not (r.get("dueDate") and r["dueDate"] < today_str())]
                if recs or os.environ.get({run_email: "IMAP_HOST", run_sam: "SAM_API_KEY"}.get(fn, "COLUMN_API_TOKEN")):
                    log(f"{len(recs):3d} matches  {name}")
                found.extend(recs)
            except Exception as e:
                errors += 1
                log(f"  ERR  {name}: {e}")

        # OpenGov / Bonfire procurement portals (Cleveland and others)
        try:
            import portals
            pctx = {"fetch": fetch, "parse_rss": parse_rss, "build_record": build_record, "VEHICLE": VEHICLE,
                    "OHIO_RE": OHIO_RE, "log": log, "today": today_str}
            st = load_json(STATE_FILE, {})
            recs = portals.run(pctx, cfg.get("portals", {}), st, web_turn)
            save_json(STATE_FILE, st)
            recs = [r for r in recs if not (r.get("dueDate") and r["dueDate"] < today_str())]
            log(f"{len(recs):3d} matches  OpenGov / Bonfire portals")
            found.extend(recs)
        except Exception as e:
            errors += 1
            log(f"  ERR  Portals: {e}")

    if sweep:
        # Every Ohio township, city and village website, on rotation
        try:
            import agencies as agency_watch
            ctx = {"fetch": fetch, "fetch_page": fetch_page, "parse_page": parse_page, "parse_rss": parse_rss,
                   "run_page_html": run_page_html, "load_json": load_json, "save_json": save_json, "log": log,
                   "agencies_file": os.path.join(DATA, "agencies.json")}
            st = load_json(STATE_FILE, {})
            follow_budget[0] = cfg.get("agency_sites", {}).get("max_follows", 400)
            recs = agency_watch.run(ctx, cfg.get("agency_sites", {}), st, seen, follow_budget, web_turn, full=True)
            save_json(STATE_FILE, st)
            recs = [r for r in recs if not (r.get("dueDate") and r["dueDate"] < today_str())]
            log(f"{len(recs):3d} matches  Agency websites (statewide)")
            found.extend(recs)
        except Exception as e:
            errors += 1
            log(f"  ERR  Agency websites: {e}")

    new = []
    for r in found:
        r["id"] = make_id(r.get("url"), r["title"])
        key = (norm_title(r.get("agency")), norm_title(r["title"]))
        if r["id"] in ids or key in titles or (r.get("url") in urls and "#" not in r.get("url", "")):
            continue
        ids.add(r["id"]); titles.add(key); urls.add(r.get("url"))
        r["firstSeen"] = now().isoformat(timespec="minutes")
        bids.append(r)
        new.append(r)

    meta = {"lastChecked": now().isoformat(timespec="minutes"), "sources": len(sources), "errors": errors,
            "newThisRun": len(new)}
    save_json(BIDS_FILE, {"bids": bids})
    save_json(SEEN_FILE, seen)
    write_site(bids, meta)
    log(f"Done: {len(new)} new, {len(bids)} total, {errors} source errors")
    for b in new:
        log("  NEW:", b["title"][:90], "|", b.get("agency", ""), "|", b.get("dueDate", ""))
    try:  # keep the latest run's log in the repo so problems are easy to spot
        with open(os.path.join(DATA, "last-sweep-log.txt" if sweep else "last-run-log.txt"), "w", encoding="utf-8") as f:
            f.write(f"{now().isoformat(timespec='minutes')} UTC\n" + "\n".join(RUN_LOG[-600:]) + "\n")
    except OSError:
        pass
    notify([b for b in new if b.get("kind") == "purchase" and not b.get("unconfirmedLocation")] or new)
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"new={len(new)}\n")
    return new


def serve(port):
    os.chdir(DOCS)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"Website: http://localhost:{port}")
    return srv


def main():
    ap = argparse.ArgumentParser(description="Ohio Vehicle Bid Tracker")
    ap.add_argument("--watch", type=float, metavar="MINUTES", help="keep running, checking every N minutes")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--sweep", action="store_true", help="run the nightly full sweep of every Ohio agency website")
    a = ap.parse_args()
    if not a.watch:
        run_once(sweep=a.sweep)
        return
    if not os.path.exists(os.path.join(DOCS, "bids.json")):
        write_site(load_json(BIDS_FILE, {"bids": []}).get("bids", []), {})
    serve(a.port)
    if not a.no_browser:
        webbrowser.open(f"http://localhost:{a.port}")
    while True:
        try:
            run_once()
            st = load_json(STATE_FILE, {})
            if st.get("last_full_sweep") != today_str() and dt.datetime.now().hour >= 23:
                log("Starting the nightly sweep of every Ohio township, city and village website…")
                run_once(sweep=True)
                st = load_json(STATE_FILE, {})
                st["last_full_sweep"] = today_str()
                save_json(STATE_FILE, st)
        except Exception as e:
            log("run failed:", e)
        log(f"Next check in {a.watch:g} min (Ctrl+C to stop)")
        time.sleep(a.watch * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
