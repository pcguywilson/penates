#!/usr/bin/env python3
"""Pull remote jobs from RemoteOK (https://remoteok.com/api) into the local store.

RemoteOK has a free, public, no-key JSON API. The response is a JSON array whose first
element is a legal notice; the rest are jobs. `apply_url` (when present) is the direct
employer link; otherwise the RemoteOK listing (`url`) forwards to it. Remote-focused.

RemoteOK asks API users to send a real User-Agent and to attribute the source; we do
both and make a small number of requests.

  python remoteok.py
  python remoteok.py --tags "devops,sre,cloud,sysadmin"
  python remoteok.py --debug
"""
import argparse, sys, re
import jobs_store

API = "https://remoteok.com/api"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
DEFAULT_TAGS = ["devops", "sre", "cloud", "sysadmin", "infrastructure", "linux"]
TITLE_OK = re.compile(r"devops|\bsre\b|site reliability|cloud|infrastructure|platform|"
                      r"systems? engineer|systems? admin|systems? analyst|linux|reliability|devsecops", re.I)
TITLE_NO = re.compile(r"\bsales\b|account exec|recruiter|\bmanager\b|director|\bintern\b|pre-?sales", re.I)
_TAG = re.compile(r"<[^>]+>")


def _session():
    try:
        import cloudscraper
        return cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    except ImportError:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "application/json"})
        return s


def _strip(html):
    return re.sub(r"\s+", " ", _TAG.sub(" ", html or "")).strip()


def _jobs(s, tag):
    url = API + ("?tags=" + tag if tag else "")
    r = s.get(url, timeout=30); r.raise_for_status()
    data = r.json()
    return [x for x in data if isinstance(x, dict) and x.get("position")]


def _sal(j):
    lo, hi = j.get("salary_min"), j.get("salary_max")
    def k(x):
        try: x = int(x)
        except Exception: return None
        return "$%dk" % round(x / 1000) if x >= 1000 else None
    a, b = k(lo), k(hi)
    if not a and not b: return ""
    return ((a + "-" + b) if a and b else (a or b)) + "/yr"


def _row(j):
    return {"url": j.get("apply_url") or j.get("url", ""),
            "company": j.get("company", ""), "role": (j.get("position") or "")[:120],
            "source": "remoteok", "posted": (j.get("date") or "")[:10],
            "location": j.get("location") or "", "workplace": "Remote",
            "salary": _sal(j), "desc": _strip(j.get("description"))[:1500]}


def main():
    ap = argparse.ArgumentParser(description="Add RemoteOK jobs to the local store")
    ap.add_argument("--tags", default=",".join(DEFAULT_TAGS), help="comma-separated RemoteOK tags")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    s = _session()

    if a.debug:
        try:
            js = _jobs(s, tags[0])
        except Exception as e:
            sys.exit("Fetch failed: %s" % e)
        print("[debug] %d jobs for tag '%s'. First 5:" % (len(js), tags[0]))
        for j in js[:5]:
            print("   %-22s %-46s %s" % ((j.get("company") or "")[:22],
                  (j.get("position") or "")[:46], j.get("apply_url") or j.get("url")))
        return

    batch, seen = [], set()
    for tag in tags:
        kept = 0
        try:
            js = _jobs(s, tag)
        except Exception as e:
            print("  [%-16s] failed: %s" % (tag, str(e)[:60])); continue
        for j in js:
            title = j.get("position") or ""
            url = j.get("apply_url") or j.get("url", "")
            if not url or url in seen:
                continue
            if TITLE_NO.search(title) or not TITLE_OK.search(title):
                continue
            seen.add(url); batch.append(_row(j)); kept += 1
        print("  tag %-16s %d in-lane" % (tag, kept))
    added = jobs_store.add_discovered(batch)
    print("\n%d in-lane RemoteOK postings, %d NEW added to the store." % (len(batch), added))


if __name__ == "__main__":
    main()
