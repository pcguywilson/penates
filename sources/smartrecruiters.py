"""SmartRecruiters public postings API: https://api.smartrecruiters.com/v1/companies/{id}/postings"""
from . import base
URL = "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100&offset=%d"

def fetch(board, company=None):
    out = []
    for page in range(10):  # cap 1000
        try:
            d = base.http_json(URL % (board, page * 100))
        except Exception:
            if page == 0:
                raise
            break
        rows = d.get("content") or []
        for j in rows:
            loc = j.get("location") or {}
            out.append(base.job(
                "smartrecruiters", board, j.get("id"),
                title=j.get("name"), company=company or (j.get("company") or {}).get("name") or board,
                url="https://jobs.smartrecruiters.com/%s/%s" % (board, j.get("id")),
                location=loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                remote=loc.get("remote"),
                posted=(j.get("releasedDate") or "")[:10]))
        if len(rows) < 100 or (page + 1) * 100 >= int(d.get("totalFound") or 0):
            break
    return out
