"""
Statewide watcher: every Ohio township, city and village.

1. Directory   - builds data/agencies.json from Wikidata (official websites) and Wikipedia
                 (complete lists of townships and municipalities). Rebuilt monthly.
2. Websites    - for agencies with no known website, looks one up (Wikipedia article link,
                 then a web search), a few per run.
3. Bid pages   - opens each website once and remembers its bids / legal notices / news pages.
4. Sweep       - once a day (11 PM Eastern), checks every one of those pages in parallel. A page is
                 only read in full when its text has changed since the day before.

All page visits respect robots.txt (through scraper.fetch_page).
"""
import datetime as dt
import hashlib
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql?format=json&query="
WIKI_API = "https://en.wikipedia.org/w/api.php?format=json&formatversion=2&"

# Ohio (Q1397). Everything located in a county that is located in Ohio, that has an official website,
# and whose type is a township, city or village.
SPARQL = """
SELECT ?item ?itemLabel ?countyLabel ?website ?typeLabel WHERE {
  ?county wdt:P131 wd:Q1397 .
  ?item wdt:P131 ?county ; wdt:P31 ?type .
  OPTIONAL { ?item wdt:P856 ?website . }
  ?type rdfs:label ?tl . FILTER(LANG(?tl) = "en")
  FILTER(REGEX(?tl, "township|city|village|municipality|county seat", "i"))
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""

BIDLINK = re.compile(r"\b(bids?|bidding|bid\s+(?:postings|opportunities|notices)|rfps?|rfqs?|procurement|purchasing|"
                     r"legal\s+(?:notices?|ads?|advertisements?)|public\s+notices?|notices?|requests?\s+for\s+(?:bids|proposals)|"
                     r"invitations?\s+to\s+bid|announcements?|news)\b", re.I)
BIDLINK_STRONG = re.compile(r"\b(bids?|rfp|rfq|legal\s+notice|public\s+notice|procurement|purchasing|invitation)", re.I)
SKIP_DOMAINS = re.compile(r"(wikipedia|wikidata|wikimedia|census\.gov|google\.|bing\.|facebook\.|twitter\.|x\.com|"
                          r"youtube\.|instagram\.|linkedin\.|yelp\.|mapquest|tripadvisor|zillow|city-data|"
                          r"niche\.com|ohio\.gov/wps|usa\.com|areavibes|bestplaces|ballotpedia|opengovus)", re.I)
SITE_MONTHS = 1


def _today():
    return dt.date.today().isoformat()


def _key(name, county):
    return re.sub(r"[^a-z0-9]+", "-", f"{name}-{county}".lower()).strip("-")


def _level(type_label, name):
    t = f"{type_label} {name}".lower()
    if "township" in t:
        return "township"
    if "village" in t or "city" in t or "municipal" in t:
        return "city"
    return "city"


def _clean_site(url):
    if not url:
        return ""
    u = urllib.parse.urlparse(url.strip())
    if not u.netloc or SKIP_DOMAINS.search(u.netloc):
        return ""
    return f"{u.scheme or 'https'}://{u.netloc}{u.path if u.path not in ('', '/') else ''}".rstrip("/")


# ------------------------------------------------------------------ 1. directory
def build_directory(ctx, agencies):
    """Merge Wikidata + Wikipedia lists into the agency directory. Returns number added."""
    fetch, log = ctx["fetch"], ctx["log"]
    added = 0
    # Wikidata: names, counties and official websites in one query
    try:
        raw, _ = fetch(WIKIDATA_SPARQL + urllib.parse.quote(SPARQL), timeout=90)
        rows = json.loads(raw).get("results", {}).get("bindings", [])
        for r in rows:
            name = r.get("itemLabel", {}).get("value", "")
            county = r.get("countyLabel", {}).get("value", "").replace(" County", "")
            if not name or re.match(r"^Q\d+$", name) or not county:
                continue
            k = _key(name, county)
            if k not in agencies:
                added += 1
            tl = r.get("typeLabel", {}).get("value", "")
            a = agencies.setdefault(k, {"name": name, "county": county, "added": _today(), "level": _level(tl, name)})
            if a["level"] == "city" and not re.search(r"\b(city|village)\b", name, re.I):
                a["display"] = ("Village of " if "village" in tl.lower() else "City of ") + name
            site = _clean_site(r.get("website", {}).get("value", ""))
            if site and not a.get("website"):
                a["website"] = site
                a["websiteFrom"] = "wikidata"
        log(f"  directory: Wikidata returned {len(rows)} rows")
    except Exception as e:
        log("  directory: Wikidata failed:", e)

    # Wikipedia list pages: complete township list ("X Township, Y County, Ohio") and municipalities
    for page, level in (("List_of_townships_in_Ohio", "township"), ("List_of_municipalities_in_Ohio", "city")):
        cont = ""
        for _ in range(6):
            try:
                raw, _ = fetch(WIKI_API + "action=query&prop=links&pllimit=max&titles=" + page + cont, timeout=60)
                j = json.loads(raw)
            except Exception as e:
                log(f"  directory: Wikipedia {page} failed:", e)
                break
            for pg in j.get("query", {}).get("pages", []):
                for ln in pg.get("links", []):
                    t = ln.get("title", "")
                    m = re.match(r"^(.+? Township), (.+?) County, Ohio$", t)
                    if m and level == "township":
                        name, county = m.group(1), m.group(2)
                    else:
                        m2 = re.match(r"^([^,]+), Ohio$", t)
                        if not (m2 and level == "city") or re.search(r"County|Township|List of|Ohio$", m2.group(1)):
                            continue
                        name, county = m2.group(1), ""
                    k = _key(name, county) if county else None
                    if k and k in agencies:
                        agencies[k].setdefault("wiki", t)
                        continue
                    # municipality without county: match an existing Wikidata entry by name
                    if not county:
                        hit = next((kk for kk, a in agencies.items() if a["name"] == name and a["level"] == "city"), None)
                        if hit:
                            agencies[hit].setdefault("wiki", t)
                            continue
                        k = _key(name, "oh")
                    agencies.setdefault(k, {"name": name if level == "city" else name, "county": county,
                                            "level": level, "wiki": t, "added": _today()})
                    added += 1
            c = j.get("continue", {}).get("plcontinue")
            if not c:
                break
            cont = "&plcontinue=" + urllib.parse.quote(c)
    return added


# ------------------------------------------------------------------ 2. websites
def resolve_websites(ctx, agencies, budget, allow_search, workers=8, search_budget=150):
    fetch, log = ctx["fetch"], ctx["log"]
    todo = [k for k, a in agencies.items() if not a.get("website") and not a.get("websiteTried")][:budget]
    searches = [search_budget]
    lock = __import__("threading").Lock()

    def one(k):
        a = agencies[k]
        a["websiteTried"] = _today()
        site = ""
        if a.get("wiki"):
            try:
                raw, _ = fetch(WIKI_API + "action=parse&prop=externallinks&page=" + urllib.parse.quote(a["wiki"]), timeout=30)
                links = json.loads(raw).get("parse", {}).get("externallinks", [])
                for l in links:
                    c = _clean_site(l)
                    if c and re.search(r"(twp|township|city|village|oh\.us|ohio|" +
                                       re.escape(re.sub(r"[^a-z]", "", a["name"].lower().split(" ")[0])) + ")", c, re.I):
                        site = c
                        break
            except Exception:
                pass
        with lock:
            can_search = allow_search and searches[0] > 0
            if can_search and not site:
                searches[0] -= 1
        if not site and can_search:
            q = f"{a['name']} {a['county'] + ' County ' if a['county'] else ''}Ohio official website"
            try:
                xml, _ = fetch("https://www.bing.com/search?format=rss&count=10&q=" + urllib.parse.quote(q))
                for it in ctx["parse_rss"](xml):
                    c = _clean_site(it["link"])
                    first = re.sub(r"[^a-z]", "", a["name"].lower().split(" ")[0])
                    if c and first and first in c.lower().replace("-", ""):
                        site = c
                        break
            except Exception:
                pass
            time.sleep(0.5)
        if site:
            a["website"], a["websiteFrom"] = site, ("wikipedia" if a.get("wiki") else "search")
            return 1
        if not can_search:
            a.pop("websiteTried", None)  # not searched yet: try again on a later day
        return 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        found = sum(ex.map(one, todo))
    if todo:
        log(f"  websites: looked up {len(todo)}, found {found}")


def _guesses(a):
    base = re.sub(r"\b(township|city|village|of)\b", "", a["name"], flags=re.I)
    slug = re.sub(r"[^a-z0-9]", "", base.lower())
    dash = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    cty = re.sub(r"[^a-z]", "", (a.get("county") or "").lower())
    if not slug:
        return []
    if a["level"] == "township":
        stems = [f"{slug}township", f"{slug}twp", f"{dash}-township", f"{slug}township{cty}", f"{slug}twp{cty}",
                 f"{slug}{cty}township", f"{slug}townshipohio", f"{slug}twpohio", f"{slug}-twp"]
        tlds = [".org", ".com", ".us", ".gov", ".net"]
    else:
        stems = [f"cityof{slug}", f"villageof{slug}", f"{slug}ohio", f"{slug}oh", f"{slug}", f"{slug}village",
                 f"{slug}-oh", f"cityof{slug}oh", f"villageof{slug}oh"]
        tlds = [".gov", ".org", ".com", ".us", ".net"]
    out = [f"https://www.{s}{t}" for s in stems for t in tlds]
    if a["level"] == "township" and cty:
        out += [f"https://www.{slug}.{cty}.oh.us", f"https://{slug}twp.{cty}.oh.us"]
    else:
        out += [f"https://www.ci.{slug}.oh.us", f"https://www.{slug}.oh.us"]
    return out


def guess_websites(ctx, agencies, workers):
    """Web search is often blocked from cloud servers, so try the address patterns Ohio governments use.
    A guess is only accepted if the page names the place (and its county or 'Ohio')."""
    import socket
    todo = [k for k, a in agencies.items() if not a.get("website") and not a.get("guessed")]
    if not todo:
        return

    def resolves(url):
        try:
            socket.setdefaulttimeout(4)
            socket.gethostbyname(urllib.parse.urlparse(url).netloc)
            return True
        except Exception:
            return False

    def one(k):
        a = agencies[k]
        a["guessed"] = _today()
        name = re.sub(r"\b(city|village)\s+of\s+", "", a["name"], flags=re.I).lower()
        core = re.sub(r"\b(township|city|village)\b", "", name).strip()
        for url in _guesses(a):
            if not resolves(url):
                continue
            try:
                html, final = ctx["fetch"](url, timeout=12)
            except Exception:
                continue
            low = re.sub(r"\s+", " ", html.lower())
            title = (re.search(r"<title[^>]*>(.*?)</title>", low) or [None, ""])[1]
            place_ok = core and core in low and (("township" in low) if a["level"] == "township" else True)
            where_ok = (a.get("county", "").lower() in low) if a.get("county") else ("ohio" in low or ", oh" in low)
            if place_ok and where_ok and (core in title or core in low[:5000]):
                a["website"], a["websiteFrom"] = final.rstrip("/"), "guess"
                return 1
        return 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        found = sum(ex.map(one, todo))
    ctx["log"](f"  websites: tried address patterns for {len(todo)} places, found {found}")


# ------------------------------------------------------------------ 3. bid pages
def find_bid_pages(ctx, agencies, budget, workers):
    todo = [k for k, a in agencies.items() if a.get("website") and "watch" not in a][:budget]
    if not todo:
        return

    def work(k):
        a = agencies[k]
        try:
            html, final = ctx["fetch_page"](a["website"])
        except Exception as e:
            return k, None, str(e)[:80]
        links, _ = ctx["parse_page"](html, final)
        host = urllib.parse.urlparse(final).netloc.replace("www.", "")
        scored = []
        for text, href, _ctx in links:
            u = urllib.parse.urlparse(href)
            if u.netloc.replace("www.", "") != host or href.endswith((".jpg", ".png", ".pdf")):
                continue
            blob = f"{text} {u.path}"
            if BIDLINK.search(blob):
                score = 2 if BIDLINK_STRONG.search(blob) else 1
                scored.append((score, href.split("#")[0]))
        pages, seen = [], set()
        for _score, h in sorted(scored, key=lambda x: -x[0]):
            if h not in seen:
                seen.add(h)
                pages.append(h)
        return k, (pages[:3] or [final]), None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for k, pages, err in ex.map(work, todo):
            a = agencies[k]
            if pages:
                a["watch"] = pages
            else:
                a["siteErrors"] = a.get("siteErrors", 0) + 1
                a["lastError"] = err
                if a["siteErrors"] >= 3:
                    a["watch"] = []  # give up on this site until the next monthly rebuild
    ctx["log"](f"  bid pages: scanned {len(todo)} sites")


# ------------------------------------------------------------------ 4. watch
def watch(ctx, agencies, state, budget, workers, seen, follow_budget):
    pages = [(k, u) for k, a in sorted(agencies.items()) for u in (a.get("watch") or [])]
    if not pages:
        return []
    start = state.get("watch_pos", 0) % len(pages)
    batch = [pages[(start + i) % len(pages)] for i in range(min(budget, len(pages)))]
    state["watch_pos"] = (start + len(batch)) % len(pages)
    hashes = state.setdefault("page_hash", {})

    def get(item):
        k, u = item
        try:
            html, final = ctx["fetch_page"](u)
            return k, u, html, final, None
        except Exception as e:
            return k, u, None, None, str(e)[:80]

    out, changed, errors = [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(get, batch))
    for k, u, html, final, err in results:
        if err:
            errors += 1
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
        h = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]
        if hashes.get(u) == h:
            continue
        hashes[u] = h
        changed += 1
        a = agencies[k]
        src = {"name": f"{a.get('display', a['name'])} website", "agency": a.get("display", a["name"]), "level": a["level"],
               "county": a.get("county", ""), "follow": True}
        try:
            recs = ctx["run_page_html"](src, html, final, seen, follow_budget)
        except Exception:
            recs = []
        # Town pages keep old notices up for months: only keep ones with a closing date,
        # or that say they were posted in the last 45 days.
        cutoff = (dt.date.today() - dt.timedelta(days=45)).isoformat()
        out.extend(r for r in recs if r.get("dueDate") or (r.get("postedDate") and r["postedDate"] >= cutoff))
    total_sites = sum(1 for a in agencies.values() if a.get("watch"))
    ctx["log"](f"  watched {len(batch)} pages ({changed} changed, {errors} unreachable) "
               f"of {len(pages)} pages on {total_sites} agency sites")
    return out


# ------------------------------------------------------------------ entry point
def run(ctx, cfg, state, seen, follow_budget, web_turn, full=False):
    """Called from scraper.run_once(sweep=True). full=True checks every agency page in one pass."""
    if not cfg.get("enabled", True):
        return []
    path = ctx["agencies_file"]
    data = ctx["load_json"](path, {"built": "", "agencies": {}})
    agencies = data["agencies"]
    built = data.get("built", "")
    if not agencies or not built or (dt.date.today() - dt.date.fromisoformat(built)).days >= 30:
        n = build_directory(ctx, agencies)
        data["built"] = _today()
        ctx["log"](f"  directory: {len(agencies)} Ohio townships, cities and villages ({n} new)")
    big = 10 ** 6
    workers = cfg.get("workers", 32)
    t0 = time.time()
    resolve_websites(ctx, agencies, big if full else 40, allow_search=web_turn,
                     search_budget=cfg.get("web_searches_per_day", 150))
    guess_websites(ctx, agencies, workers)
    find_bid_pages(ctx, agencies, big if full else 120, workers)
    recs = watch(ctx, agencies, state, big if full else 250, workers, seen, follow_budget)
    ctx["log"](f"  statewide sweep finished in {int(time.time() - t0)} s")
    ctx["save_json"](path, data)
    return recs
