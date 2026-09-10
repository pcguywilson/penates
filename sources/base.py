"""Shared helpers for ATS board adapters. Every adapter returns a list of normalized
job dicts (the shape jobs_store.add_discovered accepts)."""
import json, re, html, urllib.request, urllib.error

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

def http_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

_TAG = re.compile(r"<[^>]+>")
def strip_html(s):
    if not s:
        return ""
    # Greenhouse sends entity-encoded HTML (&lt;p&gt;), so unescape BEFORE stripping tags.
    return re.sub(r"\s+", " ", _TAG.sub(" ", html.unescape(s))).strip()

def job(ats, board, jid, title, company, url, location=None, remote=None, posted=None, desc=None):
    """Normalized row. id stays a human slug (jobs_store owns that); ats_id carries the
    stable ats:board:jobid key used for dedup and the future search->apply handoff."""
    return {
        "ats": ats, "board": board, "ats_id": "%s:%s:%s" % (ats, board, jid),
        "source": "ats-api", "company": (company or board or "").strip(),
        "role": (title or "").strip(), "url": url, "apply_url": url,
        "location": (location or "").strip(),
        "remote": bool(remote) if remote is not None else None,
        "posted": (posted or "")[:10], "desc": (desc or "")[:8000],
    }
