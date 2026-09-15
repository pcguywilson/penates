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
