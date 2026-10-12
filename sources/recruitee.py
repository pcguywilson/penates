"""Recruitee public offers API: https://{slug}.recruitee.com/api/offers/"""
from . import base
URL = "https://%s.recruitee.com/api/offers/"

def fetch(board, company=None):
    data = base.http_json(URL % board)
    out = []
    for o in data.get("offers", []) or []:
        loc = o.get("location") or ", ".join(x for x in (o.get("city"), o.get("state_name"), o.get("country")) if x)
        remote = o.get("remote")
        if remote and "remote" not in (loc or "").lower():
            loc = ("Remote - " + loc) if loc else "Remote"
        desc = base.strip_html((o.get("description") or "") + " " + (o.get("requirements") or ""))
        out.append(base.job(
            "recruitee", board, o.get("id"), title=o.get("title"),
            company=company or o.get("company_name") or board,
            url=o.get("careers_apply_url") or o.get("careers_url"), location=loc,
            remote=bool(remote) if remote is not None else None,
            posted=(o.get("published_at") or o.get("created_at") or "")[:10], desc=desc))
    return out
