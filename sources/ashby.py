"""Ashby public job-board API: https://api.ashbyhq.com/posting-api/job-board/{slug}"""
from . import base
URL = "https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true"

def fetch(board, company=None):
    data = base.http_json(URL % board)
    out = []
    for j in data.get("jobs", []) or []:
        out.append(base.job(
            "ashby", board, j.get("id"),
            title=j.get("title"), company=company or board,
            url=j.get("applyUrl") or j.get("jobUrl"), location=j.get("location"),
            remote=j.get("isRemote"),
            posted=(j.get("publishedAt") or "")[:10],
            desc=base.strip_html(j.get("descriptionPlain") or j.get("descriptionHtml") or "")))
    return out
