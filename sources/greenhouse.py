"""Greenhouse public board API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"""
from . import base
URL = "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true"

def fetch(board, company=None):
    data = base.http_json(URL % board)
    out = []
    for j in data.get("jobs", []) or []:
        loc = (j.get("location") or {}).get("name") if isinstance(j.get("location"), dict) else j.get("location")
        out.append(base.job(
            "greenhouse", board, j.get("id"),
            title=j.get("title"), company=company or j.get("company_name") or board,
            url=j.get("absolute_url"), location=loc,
            remote=("remote" in (loc or "").lower()),
            posted=(j.get("updated_at") or j.get("first_published") or "")[:10],
            desc=base.strip_html(j.get("content") or "")))
    return out
