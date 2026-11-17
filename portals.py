"""
Readers for the e-procurement portals Ohio agencies use (each one hosts many agencies in the same format):

- OpenGov Procurement  procurement.opengov.com/portal/<code>   (Cleveland, Montgomery County, COTA, ...)
- Bonfire / Euna       <slug>.bonfirehub.com                    (Upper Arlington and others)

Agency codes are seeded in sources.json and discovered automatically once a day by web search.
The JSON readers are tolerant: they look for anything shaped like a list of solicitations with
a title and a closing date, so small format changes on the portal side do not break them.
"""
import datetime as dt
import json
import re
import time
import urllib.parse

TITLE_KEYS = ("title", "ProjectName", "projectName", "name", "projectTitle", "Title")
ID_KEYS = ("id", "ProjectID", "projectId", "ID", "project_id")
CLOSE_KEYS = ("proposalDeadline", "DateClose", "closeDate", "dueDate", "deadline", "responseDeadline",
              "closingDate", "submissionDeadline", "close_date")
POSTED_KEYS = ("releaseProjectDate", "DateOpen", "publishedDate", "postedDate", "publishDate", "createdAt",
               "releaseDate", "open_date")
NUMBER_KEYS = ("financialId", "ReferenceID", "referenceId", "bidNumber", "number")

OPENGOV_ENDPOINTS = [
    "https://procurement.opengov.com/api/v1/government/{code}/project/public",
    "https://procurement.opengov.com/api/v1/government/{code}/projects/public",
    "https://procurement.opengov.com/api/v1/projects/public/{code}",
]
BONFIRE_ENDPOINTS = [
    "https://{slug}.bonfirehub.com/PublicPortal/getOpenPublicOpportunitiesSectionData",
]


def _first(d, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def _date(v):
    if not v:
        return ""
    if isinstance(v, (int, float)):
        try:
            return dt.datetime.utcfromtimestamp(v / 1000 if v > 1e11 else v).date().isoformat()
        except Exception:
            return ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(v))
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(v))
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""


def find_solicitations(obj, depth=0):
    """Return every dict in a JSON tree that looks like a solicitation."""
    out = []
    if depth > 6:
        return out
    if isinstance(obj, dict):
        if _first(obj, TITLE_KEYS) and (_first(obj, CLOSE_KEYS) or _first(obj, ID_KEYS)):
            out.append(obj)
        else:
            for v in obj.values():
                out.extend(find_solicitations(v, depth + 1))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(find_solicitations(v, depth + 1))
    return out


def _records(ctx, items, agency, level, county, url_for, source):
    recs = []
    for it in items:
        title = str(_first(it, TITLE_KEYS) or "").strip()
        num = _first(it, NUMBER_KEYS)
        if num and str(num) not in title:
            title = f"{num} – {title}"
        if not ctx["VEHICLE"].search(title):
            continue
        rec = ctx["build_record"](title, url_for(it), title, {"name": source, "agency": agency, "level": level,
                                                               "county": county, "check_ohio": False})
        due, posted = _date(_first(it, CLOSE_KEYS)), _date(_first(it, POSTED_KEYS))
        if due:
            rec["dueDate"] = due
        if posted:
            rec["postedDate"] = posted
        recs.append(rec)
    return recs


def read_opengov(ctx, code, info):
    for ep in OPENGOV_ENDPOINTS:
        url = ep.format(code=code)
        try:
            raw, _ = ctx["fetch"](url, timeout=30)
            data = json.loads(raw)
        except Exception:
            continue
        items = find_solicitations(data)
        if items:
            base = f"https://procurement.opengov.com/portal/{code}/projects/"
            return _records(ctx, items, info.get("agency", code), info.get("level", "city"), info.get("county", ""),
                            lambda it: base + str(_first(it, ID_KEYS) or ""), f"OpenGov portal – {info.get('agency', code)}")
    raise RuntimeError("no readable OpenGov listing (portal may need a browser)")


def read_bonfire(ctx, slug, info):
    for ep in BONFIRE_ENDPOINTS:
        url = ep.format(slug=slug) + f"?_={int(time.time() * 1000)}"
        try:
            raw, _ = ctx["fetch"](url, timeout=30)
            data = json.loads(raw)
        except Exception:
            continue
        items = find_solicitations(data)
        if items or data.get("success") in (1, True):
            base = f"https://{slug}.bonfirehub.com/opportunities/"
            return _records(ctx, items, info.get("agency", slug), info.get("level", "city"), info.get("county", ""),
                            lambda it: base + str(_first(it, ID_KEYS) or ""), f"Bonfire portal – {info.get('agency', slug)}")
    raise RuntimeError("no readable Bonfire listing")


def search_fallback(ctx, kind, code, info):
    host = f"procurement.opengov.com/portal/{code}/projects" if kind == "opengov" else f"{code}.bonfirehub.com/opportunities"
    try:
        xml, _ = ctx["fetch"]("https://www.bing.com/search?format=rss&count=50&q=" + urllib.parse.quote("site:" + host))
    except Exception:
        return []
    recs = []
    for it in ctx["parse_rss"](xml):
        if host.split("/")[0] not in it["link"] or not ctx["VEHICLE"].search(it["title"]):
            continue
        rec = ctx["build_record"](re.split(r"\s[|–-]\s(?:OpenGov|Bonfire)", it["title"])[0], it["link"], it["desc"],
                                  {"name": f"{'OpenGov' if kind == 'opengov' else 'Bonfire'} portal – {info.get('agency', code)}",
                                   "agency": info.get("agency", code), "level": info.get("level", "city"),
                                   "county": info.get("county", ""), "check_ohio": False})
        if rec.get("dueDate"):
            recs.append(rec)
    return recs


def discover(ctx, cfg, state):
    """Once a day, search the web for more Ohio agencies on OpenGov and Bonfire."""
    if state.get("portal_discovery") == ctx["today"]():
        return
    state["portal_discovery"] = ctx["today"]()
    og, bf = state.setdefault("opengov", {}), state.setdefault("bonfire", {})
    for q in cfg.get("discovery_queries", []):
        try:
            xml, _ = ctx["fetch"]("https://www.bing.com/search?format=rss&count=50&q=" + urllib.parse.quote(q))
        except Exception:
            continue
        for it in ctx["parse_rss"](xml):
            blob = f"{it['title']} {it['desc']}"
            if not ctx["OHIO_RE"].search(blob):
                continue
            m = re.search(r"procurement\.opengov\.com/portal/([a-z0-9-]+)", it["link"], re.I)
            if m and m.group(1).lower() not in og:
                og[m.group(1).lower()] = {"agency": re.split(r"\s[-|–]\s", it["title"])[0][:60], "discovered": ctx["today"]()}
            m = re.search(r"https?://([a-z0-9-]+)\.bonfirehub\.com", it["link"], re.I)
            if m and m.group(1).lower() not in bf and m.group(1).lower() not in ("www", "app", "vendor"):
                bf[m.group(1).lower()] = {"agency": re.split(r"\s[-|–]\s", it["title"])[0][:60], "discovered": ctx["today"]()}


def run(ctx, cfg, state, web_turn):
    if not cfg.get("enabled", True):
        return []
    if web_turn:
        discover(ctx, cfg, state)
    out = []
    jobs = [("opengov", c, i) for c, i in {**cfg.get("opengov", {}), **state.get("opengov", {})}.items()] + \
           [("bonfire", s, i) for s, i in {**cfg.get("bonfire", {}), **state.get("bonfire", {})}.items()]
    ok = 0
    for kind, code, info in jobs:
        try:
            recs = (read_opengov if kind == "opengov" else read_bonfire)(ctx, code, info)
            ok += 1
            out.extend(recs)
        except Exception as e:
            info["lastError"] = str(e)[:80]
            if web_turn:  # fallback: find this agency's portal projects through web search
                out.extend(search_fallback(ctx, kind, code, info))
    ctx["log"](f"  portals: read {ok} of {len(jobs)} OpenGov/Bonfire agencies")
    return out
