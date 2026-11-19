"""Workday public CXS job-detail fetcher.

Career URLs look like:
  https://{tenant}.{dc}.myworkdayjobs.com/{site}/job/{jobpath}
  https://{tenant}.{dc}.myworkdayjobs.com/{locale}/{site}/job/{jobpath}
  ... optionally .../apply/...

Detail JSON (same endpoint the careers UI calls):
  GET https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{jobpath}
  -> jobPostingInfo.jobDescription (HTML)

Best-effort only: any failure returns "". No board crawl here — that is out of
scope for the JD-enrichment lane.
"""
import re
from . import base

# tenant.wdN.myworkdayjobs.com/...  (wd1, wd5, wd108, wd501, ...)
_HOST = re.compile(
    r"^https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(.+)$", re.I)
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}/")


def parse_url(url):
    """Return (tenant, dc, site, jobpath) for a Workday job URL, else None.

    jobpath is the path after /job/ with any /apply... suffix stripped (may include
    a location segment, e.g. 'Chelmsford-MA/Senior-Cloud-Engineer_R19596').
    """
    u = (url or "").split("?")[0].split("#")[0].rstrip("/")
    m = _HOST.match(u)
    if not m:
        return None
    tenant, dc, rest = m.group(1), m.group(2), m.group(3)
    rest = _LOCALE.sub("", rest)
    rest = re.sub(r"/apply.*$", "", rest, flags=re.I)
    if "/job/" not in rest:
        return None
    site, jobpath = rest.split("/job/", 1)
    site = (site or "").strip("/")
    jobpath = (jobpath or "").strip("/")
    if not site or not jobpath:
        return None
    return (tenant, dc, site, jobpath)


def cxs_detail_url(tenant, dc, site, jobpath):
    return "https://%s.%s.myworkdayjobs.com/wday/cxs/%s/%s/job/%s" % (
        tenant, dc, tenant, site, jobpath)


def fetch_description(url, timeout=6):
    """Plain-text JD for one Workday posting URL. Best-effort; never raises."""
    parts = parse_url(url)
    if not parts:
        return ""
    tenant, dc, site, jobpath = parts
    try:
        d = base.http_json(cxs_detail_url(tenant, dc, site, jobpath), timeout=timeout)
        info = d.get("jobPostingInfo") or {}
        return base.strip_html(info.get("jobDescription") or "")
    except Exception:
        return ""


# ---- board crawl (scan_ats) -------------------------------------------------
# companies.yml slug form: "<tenant>.<wdN>/<site>"  e.g. "zoll.wd5/ZOLLMedicalCorp"
def _terms():
    """Config search terms for the Workday term-search fallback, minus config.json
    "workday_skip_terms" (case-insensitive). Other sources still use every term."""
    try:
        import jobs_store
        terms = jobs_store.config_terms(["devops engineer", "site reliability engineer", "cloud engineer"])
        skip = jobs_store.load_config().get("workday_skip_terms") or []
    except Exception:
        terms, skip = ["devops engineer", "site reliability engineer", "cloud engineer"], []
    skip = {str(t).strip().lower() for t in skip if isinstance(t, str)}
    return [t for t in terms if str(t).strip().lower() not in skip]


PAGE = 20          # CXS max page size
MAX_PER_TERM = 200  # hard cap per search term


# ---- remote-facet crawl ------------------------------------------------------
# One unfiltered page-0 POST reads the board's facets. If the board exposes remote US
# location values, one filtered empty search pages only those jobs and the usual
# scan_ats title/location filter runs locally. Boards without them fall back to the
# per-term search below. Facet ids are tenant-specific, so they are matched by name.
FACET_MAX = 2000   # CXS returns at most 2000 per query
_REMOTE_TOK = re.compile(r"\b(remote|telework\w*|teleworker|virtual|work from home|wfh)\b", re.I)
_US_TOK = re.compile(r"\b(us|usa|u\.s\.a?\.?|united states( of america)?|any state|any location|nationwide)\b", re.I)
_US_EXACT = re.compile(r"^(us|usa|united states( of america)?)$", re.I)
_NON_US = re.compile(r"\b(united kingdom|uk|canada|india|israel|germany|ireland|mexico|brazil|"
                     r"poland|philippines|costa rica|japan|china|australia|singapore|netherlands|"
                     r"france|spain|romania|emea|apac|latam)\b", re.I)


def _walk_facets(facets):
    """Yield (facetParameter, value) for every leaf value, descending into container
    facets such as locationMainGroup whose values are facets themselves."""
    for f in facets or []:
        param = f.get("facetParameter")
        for v in f.get("values") or []:
            if isinstance(v, dict) and v.get("facetParameter") and v.get("values") is not None:
                yield from _walk_facets([v])
            elif isinstance(v, dict) and v.get("id"):
                yield param, v


def remote_us_facets(facets):
    """appliedFacets dict for remote US jobs, or None if the board has no usable values.

    1) A value named exactly "Remote" (a location-type facet) ANDed with a US value from
       a different facet when one exists.
    2) Otherwise, location values whose name has a remote token and a US token and no
       other country, ORed within their own facet parameter.
    """
    leaves = list(_walk_facets(facets))
    exact = [(p, v) for p, v in leaves if str(v.get("descriptor") or "").strip().lower() == "remote"]
    if exact:
        p, v = exact[0]
        applied = {p: [v["id"]]}
        us = [(q, u) for q, u in leaves if q != p and _US_EXACT.match(str(u.get("descriptor") or "").strip())]
        if us:
            q, u = us[0]
            applied[q] = [u["id"]]
        return applied
    picked = {}
    for p, v in leaves:
        d = str(v.get("descriptor") or "")
        if _REMOTE_TOK.search(d) and _US_TOK.search(d) and not _NON_US.search(d):
            picked.setdefault(p, []).append(v["id"])
    if not picked:
        return None
    p = max(picked, key=lambda k: len(picked[k]))   # one parameter: values OR within it
    return {p: picked[p]}


def _fetch_remote(api, root, site, board, company, tenant):
    """Rows from the remote-facet query, or None to fall back to term search."""
    d = base.http_post_json(api, {"appliedFacets": {}, "limit": PAGE, "offset": 0, "searchText": ""}, timeout=15)
    applied = remote_us_facets(d.get("facets"))
    if not applied:
        return None
    out, seen, offset, total = [], set(), 0, None
    while offset < FACET_MAX and (total is None or offset < total):
        d = base.http_post_json(api, {"appliedFacets": applied, "limit": PAGE, "offset": offset,
                                      "searchText": ""}, timeout=15)
        if total is None:   # later pages can report total 0; keep page 0's
            try:
                total = int(d.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
        posts = d.get("jobPostings") or []
        for j in posts:
            path = j.get("externalPath") or ""
            if not path or path in seen:
                continue
            seen.add(path)
            loc = j.get("locationsText") or ""
            if not loc or re.match(r"^\d+ Locations?$", loc, re.I):
                seg = path.split("/job/", 1)[-1].split("/")
                loc = seg[0].replace("-", " ") if len(seg) > 1 else ""
            if not _REMOTE_TOK.search(loc):   # "2 Locations" rows: the facet says remote US
                loc = "Remote, US" + ("; " + loc if loc else "")
            out.append(base.job(
                "workday", board, path.split("/job/", 1)[-1],
                title=j.get("title"), company=company or tenant,
                url="%s/%s%s" % (root, site, path), location=loc,
                remote=True))   # selected by a remote US facet
        if len(posts) < PAGE:
            break
        offset += PAGE
    return out


def fetch(board, company=None):
    host, site = board.split("/", 1)
    tenant, dc = host.split(".", 1)
    root = "https://%s.%s.myworkdayjobs.com" % (tenant, dc)
    api = "%s/wday/cxs/%s/%s/jobs" % (root, tenant, site)
    try:
        rows = _fetch_remote(api, root, site, board, company, tenant)
    except Exception:
        rows = None   # facet path failed: the term search below reports real board errors
    if rows is not None:
        return rows
    return _fetch_terms(board, company)


def _fetch_terms(board, company=None):
    host, site = board.split("/", 1)
    tenant, dc = host.split(".", 1)
    root = "https://%s.%s.myworkdayjobs.com" % (tenant, dc)
    api = "%s/wday/cxs/%s/%s/jobs" % (root, tenant, site)
    seen, out, errs, terms = set(), [], [], _terms()
    for term in terms:
        offset, total = 0, None
        while offset < MAX_PER_TERM and (total is None or offset < total):
            try:
                d = base.http_post_json(api, {"appliedFacets": {}, "limit": PAGE, "offset": offset,
                                              "searchText": term}, timeout=15)
            except Exception as e:
                errs.append(str(e)[:80])
                break
            if total is None:
                try:
                    total = int(d.get("total") or 0)
                except (TypeError, ValueError):
                    total = 0
            posts = d.get("jobPostings") or []
            for j in posts:
                path = j.get("externalPath") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                loc = j.get("locationsText") or ""
                if not loc or re.match(r"^\d+ Locations?$", loc, re.I):
                    seg = path.split("/job/", 1)[-1].split("/")
                    loc = seg[0].replace("-", " ") if len(seg) > 1 else ""
                out.append(base.job(
                    "workday", board, path.split("/job/", 1)[-1],
                    title=j.get("title"), company=company or tenant,
                    url="%s/%s%s" % (root, site, path), location=loc,
                    remote=True if re.search(r"remote", loc + path, re.I) else None))
            if len(posts) < PAGE:
                break
            offset += PAGE
    if errs and len(errs) >= len(terms) and not out:
        raise RuntimeError(errs[0])  # whole board failed: report as a board error
    return out
