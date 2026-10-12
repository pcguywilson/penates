"""Paylocity public job board: https://recruiting.paylocity.com/Recruiting/Jobs/All/{company-guid}
The page embeds window.pageData = {...,"Jobs":[...]}; the slug is the company GUID."""
import json
from . import base
URL = "https://recruiting.paylocity.com/Recruiting/Jobs/All/%s"
JOB = "https://recruiting.paylocity.com/Recruiting/Jobs/Details/%s"

def _page_data(page):
    i = page.find("window.pageData")
    if i < 0:
        return {}
    j = page.find("{", i)
    obj, _ = json.JSONDecoder().raw_decode(page[j:])
    return obj

def fetch(board, company=None):
    pd = _page_data(base.http_text(URL % board))
    out = []
    for o in pd.get("Jobs", []) or []:
        if o.get("IsInternal"):
            continue
        jl = o.get("JobLocation") or {}
        loc = o.get("LocationName") or ", ".join(x for x in (jl.get("City"), jl.get("State"), jl.get("Country")) if x)
        remote = o.get("IsRemote")
        if remote and "remote" not in (loc or "").lower():
            loc = ("Remote - " + loc) if loc else "Remote"
        out.append(base.job("paylocity", board, o.get("JobId"), title=o.get("JobTitle"),
                            company=company or board, url=JOB % o.get("JobId"), location=loc,
                            remote=bool(remote) if remote is not None else None,
                            posted=(o.get("PublishedDate") or "")[:10],
                            desc=base.strip_html(o.get("Description") or "")))
    return out
