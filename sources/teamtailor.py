"""Teamtailor public jobs RSS: https://{slug}.teamtailor.com/jobs.rss
slug is the host prefix (it may carry a region, e.g. "acme.na")."""
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from . import base
URL = "https://%s.teamtailor.com/jobs.rss"

def _local(tag):
    return tag.rsplit("}", 1)[-1]

def _kids(el, name):
    return [c for c in el if _local(c.tag) == name]

def _text(el, name):
    k = _kids(el, name)
    return (k[0].text or "").strip() if k and k[0].text else ""

def fetch(board, company=None):
    root = ET.fromstring(base.http_text(URL % board))
    out = []
    for it in root.iter():
        if _local(it.tag) != "item":
            continue
        link = _text(it, "link")
        m = re.search(r"/jobs/(\d+)", link)
        jid = m.group(1) if m else (_text(it, "guid") or link)
        locs = []
        for ls in _kids(it, "locations"):
            for l in _kids(ls, "location"):
                parts = [_text(l, "city"), _text(l, "country")]
                s = ", ".join(p for p in parts if p)
                if s:
                    locs.append(s)
        rs = _text(it, "remoteStatus").lower()
        remote = True if rs == "fully" else (False if rs in ("none", "temporary") else None)
        loc = " / ".join(locs)
        if remote and "remote" not in loc.lower():
            loc = ("Remote - " + loc) if loc else "Remote"
        posted = ""
        try:
            posted = parsedate_to_datetime(_text(it, "pubDate")).strftime("%Y-%m-%d")
        except Exception:
            pass
        out.append(base.job("teamtailor", board, jid, title=_text(it, "title"),
                            company=company or board, url=link, location=loc, remote=remote,
                            posted=posted, desc=base.strip_html(_text(it, "description"))))
    return out
