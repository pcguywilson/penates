"""Breezy HR public positions: https://{slug}.breezy.hr/json"""
from . import base
URL = "https://%s.breezy.hr/json"

def fetch(board, company=None):
    data = base.http_json(URL % board)
    out = []
    for o in data if isinstance(data, list) else []:
        loc_o = o.get("location") or {}
        loc = loc_o.get("name") or ""
        remote = loc_o.get("is_remote")
        if remote and "remote" not in loc.lower():
            loc = ("Remote - " + loc) if loc else "Remote"
        co = o.get("company")
        co = co.get("name") if isinstance(co, dict) else co
        out.append(base.job("breezy", board, o.get("id"), title=o.get("name"),
                            company=company or co or board, url=o.get("url"), location=loc,
                            remote=bool(remote) if remote is not None else None,
                            posted=(o.get("published_date") or "")[:10]))
    return out
