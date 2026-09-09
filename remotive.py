#!/usr/bin/env python3
"""Pull remote jobs from Remotive (https://remotive.com/api) into the local store.

Remotive has a free, public, no-key JSON API. Each job links to the Remotive listing
which forwards to the employer. Good for discovery.

  python remotive.py
  python remotive.py --terms "devops engineer,sre" --limit 40
  python remotive.py --debug
"""
import argparse, sys, re, json
from urllib.parse import quote_plus
import jobs_store

API = "https://remotive.com/api/remote-jobs"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
DEFAULT_TERMS = ["devops engineer", "site reliability engineer", "cloud engineer",
                 "platform engineer", "infrastructure engineer",
                 "systems administrator", "systems engineer", "systems analyst"]
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


def _fetch_search(s, term, limit):
    r = s.get("%s?search=%s&limit=%d" % (API, quote_plus(term), limit), timeout=30)
    r.raise_for_status()
    return r.json().get("jobs", [])


def _fetch_all(s):
    r = s.get(API, timeout=40); r.raise_for_status()
    return r.json().get("jobs", [])


def _fetch_category(s, cat):
    r = s.get("%s?category=%s" % (API, cat), timeout=30)
    r.raise_for_status()
    return r.json().get("jobs", [])


def _row(j):
    return {"url": j.get("url", ""), "company": j.get("company_name", ""),
            "role": (j.get("title") or "")[:120], "source": "remotive",
            "posted": (j.get("publication_date") or "")[:10],
            "location": j.get("candidate_required_location", ""),
            "workplace": "Remote", "salary": (j.get("salary") or "").strip(),
            "desc": _strip(j.get("description"))[:1500]}


def main():
    ap = argparse.ArgumentParser(description="Add Remotive jobs to the local store")
    ap.add_argument("--terms", default="", help="comma-separated; blank = use config.json")
    ap.add_argument("--limit", type=int, default=50, help="results per term")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    s = _session()
    terms = [t.strip() for t in a.terms.split(",") if t.strip()]

    if a.debug:
        try:
            js = _fetch_all(s)
        except Exception as e:
            sys.exit("Fetch failed: %s" % e)
        inlane = [j for j in js if TITLE_OK.search(j.get("title") or "") and not TITLE_NO.search(j.get("title") or "")]
        print("[debug] full feed: %d jobs, %d in-lane. First 8 in-lane:" % (len(js), len(inlane)))
        for j in inlane[:8]:
            print("   %-22s %-46s %s" % ((j.get("company_name") or "")[:22],
                  (j.get("title") or "")[:46], j.get("url")))
        return

    batch, seen = [], set()
    # Default: sweep the remote software-dev + devops categories and keep our lane.
    # If --terms is given, fall back to Remotive's full-text search per term instead.
    if terms:
        for term in terms:
            kept = 0
            try:
                js = _fetch_search(s, term, a.limit)
            except Exception as e:
                print("  [%-28s] failed: %s" % (term, str(e)[:60])); continue
            for j in js:
                url, title = j.get("url", ""), j.get("title") or ""
                if not url or url in seen or TITLE_NO.search(title) or not TITLE_OK.search(title):
                    continue
                seen.add(url); batch.append(_row(j)); kept += 1
            print("  %-30s %d in-lane" % (term, kept))
    else:
        try:
            js = _fetch_all(s)
        except Exception as e:
            sys.exit("Fetch failed: %s" % e)
        kept = 0
        for j in js:
            url, title = j.get("url", ""), j.get("title") or ""
            if not url or url in seen or TITLE_NO.search(title) or not TITLE_OK.search(title):
                continue
            seen.add(url); batch.append(_row(j)); kept += 1
        print("  full feed (%d jobs)  %d in-lane" % (len(js), kept))
    added = jobs_store.add_discovered(batch)
    print("\n%d in-lane Remotive postings, %d NEW added to the store." % (len(batch), added))


if __name__ == "__main__":
    main()
