"""Lever public postings API: https://api.lever.co/v0/postings/{slug}?mode=json"""
import datetime
from . import base
URL = "https://api.lever.co/v0/postings/%s?mode=json"

def _date(ms):
    try:
        return datetime.datetime.utcfromtimestamp(int(ms) / 1000).date().isoformat()
    except Exception:
        return ""

def fetch(board, company=None):
    data = base.http_json(URL % board)
    out = []
    for j in data or []:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ""
        wk = (j.get("workplaceType") or "").lower()
        out.append(base.job(
            "lever", board, j.get("id"),
            title=j.get("text"), company=company or board,
            url=j.get("applyUrl") or j.get("hostedUrl"), location=loc,
            remote=(wk == "remote" or "remote" in loc.lower()),
            posted=_date(j.get("createdAt")),
            desc=base.strip_html(j.get("descriptionPlain") or j.get("description") or "")))
    return out
