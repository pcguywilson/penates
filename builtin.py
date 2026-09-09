#!/usr/bin/env python3
"""Pull jobs from builtin.com into the local store.

builtin server-renders its results as HTML, so we fetch the search page and parse the
job cards. Each card pairs a company-name link with a /job/ link (title + URL). builtin
/job/ URLs are builtin listing pages (you click through to apply), like LinkedIn - good
for discovery, not direct-autofill.

  pip install beautifulsoup4 cloudscraper
  python builtin.py
  python builtin.py --debug
  python builtin.py --terms "cloud engineer,sre" --pages 2
"""
import argparse, sys, re
from urllib.parse import quote_plus
try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("pip install beautifulsoup4")
import jobs_store

BASE = "https://builtin.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
DEFAULT_TERMS = ["devops engineer", "site reliability engineer", "cloud engineer",
                 "platform engineer", "infrastructure engineer",
                 "systems administrator", "systems engineer", "systems analyst"]
TITLE_OK = re.compile(r"devops|\bsre\b|site reliability|cloud|infrastructure|platform|"
                      r"systems? engineer|systems? admin|systems? analyst|linux|reliability|devsecops", re.I)
TITLE_NO = re.compile(r"\bsales\b|account exec|recruiter|\bmanager\b|director|\bintern\b|pre-?sales", re.I)


def _session():
    try:
        import cloudscraper
        return cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    except ImportError:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        return s


def _fetch(s, term, page):
    url = "%s/jobs/remote?search=%s&allLocations=true" % (BASE, quote_plus(term))
    if page > 1:
        url += "&page=%d" % page
    r = s.get(url, timeout=30); r.raise_for_status()
    return r.text


def _card_for(a):
    """Walk up from a /job/ anchor to the tightest ancestor that still wraps just this
    one job (its own card), so we can inspect that card's badges without bleeding into
    neighboring cards."""
    node = a
    card = a
    for _ in range(8):
        parent = node.parent
        if parent is None:
            break
        # if the parent starts wrapping a 2nd job link, `node` was the card boundary
        job_links = [x for x in parent.find_all("a", href=True)
                     if x.get("href", "").startswith("/job/")]
        if len(job_links) > 1:
            break
        card = parent
        node = parent
    return card


def _parse(html, include_easy=False):
    soup = BeautifulSoup(html, "html.parser")
    out, last_company = [], ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        if href.startswith("/company/") and text:
            last_company = text
        elif href.startswith("/job/") and text:
            card = _card_for(a)
            easy = "easy apply" in card.get_text(" ", strip=True).lower()
            if easy and not include_easy:
                continue
            out.append({"title": text, "company": last_company,
                        "url": BASE + href.split("?")[0], "easy": easy})
    return out


def main():
    ap = argparse.ArgumentParser(description="Add builtin.com jobs to the local store")
    ap.add_argument("--terms", default="", help="comma-separated; blank = use config.json")
    ap.add_argument("--pages", type=int, default=1, help="pages per term (25 jobs each)")
    ap.add_argument("--include-easy", dest="include_easy", action="store_true",
                    help="also keep builtin Easy Apply jobs (default: skip them)")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    terms = [t.strip() for t in a.terms.split(",") if t.strip()] or jobs_store.config_terms(DEFAULT_TERMS)
    s = _session()

    if a.debug:
        try:
            rows = _parse(_fetch(s, terms[0], 1), include_easy=True)
        except Exception as e:
            sys.exit("Fetch/parse failed: %s" % e)
        easy = sum(1 for r in rows if r.get("easy"))
        print("[debug] parsed %d cards for '%s' (%d Easy Apply, %d employer-direct):"
              % (len(rows), terms[0], easy, len(rows) - easy))
        for r in rows[:12]:
            tag = "EASY " if r.get("easy") else "     "
            print("   %s%-22s %s" % (tag, r["company"][:22], r["title"][:50]))
        return

    batch, seen = [], set()
    for term in terms:
        kept = 0
        for page in range(1, a.pages + 1):
            try:
                rows = _parse(_fetch(s, term, page), a.include_easy)
            except Exception as e:
                print("  [%-24s p%d] failed: %s" % (term, page, str(e)[:60])); break
            if not rows:
                break
            for r in rows:
                if r["url"] in seen or not r["title"]:
                    continue
                if TITLE_NO.search(r["title"]) or not TITLE_OK.search(r["title"]):
                    continue
                seen.add(r["url"])
                batch.append({"url": r["url"], "company": r["company"], "role": r["title"][:120], "source": "builtin"})
                kept += 1
        print("  %-30s %d in-lane" % (term, kept))
    added = jobs_store.add_discovered(batch)
    print("\n%d in-lane builtin postings, %d NEW added to the store." % (len(batch), added))


if __name__ == "__main__":
    main()
