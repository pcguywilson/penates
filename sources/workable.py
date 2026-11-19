"""Workable public widget API: https://apply.workable.com/api/v1/widget/accounts/{slug}"""
from . import base
URL = "https://apply.workable.com/api/v1/widget/accounts/%s"

def fetch(board, company=None):
    d = base.http_json(URL % board)
    out = []
    for j in d.get("jobs") or []:
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        out.append(base.job(
            "workable", board, j.get("shortcode"),
            title=j.get("title"), company=company or d.get("name") or board,
            url=j.get("url") or "https://apply.workable.com/%s/j/%s/" % (board, j.get("shortcode")),
            location=loc, remote=bool(j.get("telecommuting")),
            posted=(j.get("published_on") or "")[:10]))
    return out
