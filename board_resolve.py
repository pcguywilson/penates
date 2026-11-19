#!/usr/bin/env python3
"""Follow aggregator apply links to the employer's own job board, so scan_ats.harvest can
add that board and scan it directly from then on.

Aggregator rows (builtin redirects, grnh.se short links, indeed redirect links) often never
show the employer board URL, so harvest cannot learn the board. Paylocity detail links name a
job but not the company board; the detail page does. This module GETs those links (no HEAD:
many hosts 405 or return an empty body on HEAD), records where they land, and stamps the
employer URL onto the row's apply_url.

Public, unauthenticated pages only (the same hop a browser makes when you click Apply). No
login-gated sources (LinkedIn is skipped). Results live in data/board_resolve.json:
  urls:   {link: {ok, final, board, company, code, checked}}   hits kept forever,
                                                              misses retried after NEG_TTL_DAYS
  _hosts: {host: consecutive 403/429 count}                    a host at HOST_FAIL_LIMIT is skipped
"""
import json, os, re, time, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "board_resolve.json")
NEG_TTL_DAYS = 7
HOST_FAIL_LIMIT = 5
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# Links that only bounce to the employer: the row's apply_url is replaced by where they land.
HOP = re.compile(r"(builtin\.com/job/|grnh\.se/|indeed\.com/(?:viewjob|rc/clk|applystart|pagead/clk|job/))", re.I)
# Links that are already the job, but hide the company board: we only read the board off the page.
BOARD_LOOKUP = re.compile(r"recruiting\.paylocity\.com/Recruiting/Jobs/Details/\d+", re.I)
_PAYLOCITY_BOARD = re.compile(r"Jobs/All/([0-9a-fA-F-]{36})")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _host(u):
    m = re.match(r"https?://([^/?#]+)", u or "")
    return (m.group(1) if m else "").lower()


def load():
    try:
        with open(CACHE, encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("urls", {}); d.setdefault("_hosts", {})
        return d
    except Exception:
        return {"urls": {}, "_hosts": {}}


def save(c):
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=1)
    os.replace(tmp, CACHE)


def _session():
    try:
        import cloudscraper
        return cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    except ImportError:
        import requests
        s = requests.Session(); s.headers.update({"User-Agent": UA}); return s


def target(r):
    """The link to follow for this row, or None. Builtin rows use builtin's ApplyRedirect."""
    for u in (r.get("apply_url") or "", r.get("url") or ""):
        if not u:
            continue
        if "builtin.com/job/" in u:
            return u.split("?")[0] + "?handler=ApplyRedirect"
        if HOP.search(u) or BOARD_LOOKUP.search(u):
            return u
    return None


def follow(s, url):
    """One GET with redirects. Returns a cache entry."""
    e = {"ok": False, "final": None, "board": None, "code": None,
         "checked": _now().isoformat(timespec="seconds")}
    try:
        ref = url.split("?")[0]
        r = s.get(url, allow_redirects=True, timeout=20, headers={"Referer": ref})
        e["code"] = getattr(r, "status_code", None)
        final = getattr(r, "url", "") or ""
        if e["code"] and e["code"] >= 400:
            return e
        if BOARD_LOOKUP.search(url):
            m = _PAYLOCITY_BOARD.search(getattr(r, "text", "") or "")
            if m:
                e.update(ok=True, final=url,
                         board="https://recruiting.paylocity.com/Recruiting/Jobs/All/%s" % m.group(1))
            return e
        if final and not HOP.search(final) and "builtin.com" not in final:
            e.update(ok=True, final=final, board=final)
    except Exception as ex:
        e["code"] = "err:" + type(ex).__name__
    return e


def _stale(e):
    try:
        t = datetime.datetime.fromisoformat(e.get("checked"))
        return (_now() - t).days >= NEG_TTL_DAYS
    except Exception:
        return True


def _stamp(r, e):
    """Put the employer URL on the row when its apply link is still an aggregator hop."""
    try:
        import jobs_store
        infer = jobs_store.infer_ats
    except Exception:
        infer = lambda u: None
    cur = r.get("apply_url") or r.get("url") or ""
    if e.get("final") and (HOP.search(cur) or "builtin.com" in cur):
        r["apply_url"] = e["final"]
        a = infer(e["final"])
        if a:
            r["ats"] = a
        return True
    return False


def run(d, rows, limit=150, pause=0.6, save_rows=None):
    """Follow up to `limit` new links. Cached hits are re-applied for free."""
    c = load(); urls, hosts = c["urls"], c["_hosts"]
    s = None
    tried = hit = stamped = 0
    for r in rows:
        u = target(r)
        if not u:
            continue
        e = urls.get(u)
        if e and e.get("ok"):
            stamped += _stamp(r, e)
            continue
        if e and not _stale(e):
            continue
        h = _host(u)
        if hosts.get(h, 0) >= HOST_FAIL_LIMIT or tried >= limit:
            continue
        s = s or _session()
        e = follow(s, u)
        e["company"] = r.get("company") or ""
        urls[u] = e
        tried += 1
        if e["ok"]:
            hit += 1; hosts.pop(h, None); stamped += _stamp(r, e)
        elif e.get("code") in (403, 429):
            hosts[h] = hosts.get(h, 0) + 1
        if tried % 15 == 0:
            save(c)
            if save_rows:
                save_rows(d)
        time.sleep(pause)
    save(c)
    if save_rows:
        save_rows(d)
    print("board resolve: followed %d new links, %d reached an employer page, %d rows re-pointed"
          % (tried, hit, stamped))
    return tried, hit, stamped


def boards():
    """(company, url) for every resolved link, for scan_ats.harvest."""
    return [(e.get("company") or "", e.get("board") or e.get("final"))
            for e in load()["urls"].values() if e.get("ok") and (e.get("board") or e.get("final"))]
