"""JazzHR public career page (HTML list, no JSON): https://{slug}.applytojob.com/apply"""
import re, html
from . import base
URL = "https://%s.applytojob.com/apply"
# The served HTML mixes quote styles (class="list-group-item", class='fa fa-map-marker'), so every
# attribute match accepts either quote.
_ITEM = re.compile(r'class=["\']list-group-item["\'](.*?)(?=class=["\']list-group-item["\']|</ul>\s*</div>|\Z)', re.S)
_A = re.compile(r'<a[^>]+href=["\']([^"\']*/apply/([A-Za-z0-9]{6,})/[^"\']*)["\'][^>]*>\s*(.*?)\s*</a>', re.S)
_LOC = re.compile(r'fa-map-marker["\'][^>]*>\s*(?:</i>)?\s*([^<]*)', re.S)

def fetch(board, company=None):
    page = base.http_text(URL % board)
    out, seen = [], set()
    for seg in _ITEM.findall(page):
        m = _A.search(seg)
        if not m:
            continue
        href, jid, title = m.group(1), m.group(2), html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
        if jid in seen or not title:
            continue
        seen.add(jid)
        if href.startswith("/"):
            href = "https://%s.applytojob.com%s" % (board, href)
        lm = _LOC.search(seg)
        loc = html.unescape(lm.group(1)).strip() if lm else ""
        if not loc:
            continue   # no location -> the remote/US filters cannot judge it; leave it out
        out.append(base.job("jazzhr", board, jid, title=title, company=company or board,
                            url=href.split("?")[0], location=loc,
                            remote=True if "remote" in loc.lower() else None))
    return out
