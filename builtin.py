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
import argparse, sys, re, json, time
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
DESC_CAP = 20000  # match discover.py; cap stored JD length


def _session():
    try:
        import cloudscraper
        return cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    except ImportError:
        print("  [warn] cloudscraper NOT installed - falling back to plain requests, which builtin's "
              "Cloudflare usually BLOCKS. If job descriptions come back empty, run: pip install cloudscraper")
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


def _desc_from_html(page):
    """Extract (plaintext_description, truncated) from a builtin job page's schema.org
    JobPosting JSON-LD. Returns ('', False) if no JobPosting.description is found."""
    # builtin HTML-entity-encodes the + in the type attr (application/ld&#x2B;json), so match
    # +, &#x2B; or &#43; between 'ld' and 'json'.
    for m in re.finditer(r'<script[^>]*application/ld(?:\+|&#x2b;|&#43;)json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            nodes = data["@graph"]
        elif isinstance(data, list):
            nodes = data
        else:
            nodes = [data]
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "JobPosting" and node.get("description"):
                txt = BeautifulSoup(node["description"], "html.parser").get_text("\n")
                txt = re.sub(r"[ \t\xa0]+", " ", txt)
                txt = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", txt).strip()
                return txt[:DESC_CAP], len(txt) > DESC_CAP
    return "", False


def _job_desc(s, job_url):
    """Fetch a builtin.com /job/ PAGE and pull its JD. Returns (desc, truncated, note); note is
    'ok' / 'http:<code>' / 'err:<msg>' / 'nojd' so the caller can report WHY a page yielded nothing."""
    try:
        r = s.get(job_url, timeout=30)
        if r.status_code != 200:
            return "", False, "http:%d" % r.status_code
    except Exception as e:
        return "", False, "err:%s" % str(e)[:40]
    desc, trunc = _desc_from_html(r.text)
    return desc, trunc, ("ok" if desc else "nojd")


def _backfill_desc(s):
    """One-shot: fill desc for existing builtin rows that have none, no rediscovery needed."""
    d = jobs_store.load(); q = d["queue"]
    targets = [r for r in q if (r.get("source") == "builtin" or "builtin" in (r.get("ats") or ""))
               and not (r.get("desc") or "").strip() and "builtin.com/job/" in (r.get("url") or "")]
    print("Backfilling JD for %d builtin rows with no description...\n" % len(targets))
    got = 0; notes = {}
    for i, r in enumerate(targets, 1):
        desc, trunc, note = _job_desc(s, (r.get("url") or "").split("?")[0])
        notes[note.split(":")[0]] = notes.get(note.split(":")[0], 0) + 1
        if desc:
            r["desc"] = desc
            r["desc_truncated"] = bool(trunc)
            got += 1
        if i % 15 == 0:
            jobs_store.save(d); print("  ...%d/%d saved (%d filled) so far: %s" % (i, len(targets), got, notes))
        time.sleep(0.4)
    jobs_store.save(d)
    print("\nDone: %d/%d builtin rows now have a JD. Outcome breakdown: %s" % (got, len(targets), notes))
    if got == 0:
        print("  0 filled -> every page fetch failed. If notes show http:403/err, builtin is blocking: "
              "pip install cloudscraper  (then re-run). If notes show 'nojd', the JSON-LD selector needs work.")
    else:
        print("  Next: python rank.py")


def main():
    ap = argparse.ArgumentParser(description="Add builtin.com jobs to the local store")
    ap.add_argument("--terms", default="", help="comma-separated; blank = use config.json")
    ap.add_argument("--pages", type=int, default=3, help="pages per term (25 jobs each)")
    ap.add_argument("--include-easy", dest="include_easy", action="store_true",
                    help="also keep builtin Easy Apply jobs (default: skip them)")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--backfill-desc", dest="backfill", action="store_true",
                    help="fill desc for existing builtin rows that have none (no rediscovery)")
    ap.add_argument("--diag", action="store_true", help="fetch one builtin job page, save it + report markers")
    a = ap.parse_args()
    terms = [t.strip() for t in a.terms.split(",") if t.strip()] or jobs_store.config_terms(DEFAULT_TERMS)
    s = _session()

    if a.diag:
        d = jobs_store.load()
        url = next((r.get("url") for r in d["queue"]
                    if (r.get("source") == "builtin" or "builtin" in (r.get("ats") or ""))
                    and not (r.get("desc") or "").strip() and "builtin.com/job/" in (r.get("url") or "")), None)
        if not url:
            print("no empty builtin row found"); return
        url = url.split("?")[0]
        r = s.get(url, timeout=30)
        open("data/_bpage.html", "w", encoding="utf-8").write(r.text)
        print("url:", url, "status:", r.status_code, "len:", len(r.text))
        for mk in ["application/ld+json", "JobPosting", "jobPostInit", "__NEXT_DATA__",
                   "\"description\"", "responsibilities", "og:description"]:
            print("  contains %-22r : %s" % (mk, mk in r.text))
        print("saved -> data/_bpage.html")
        return

    if a.backfill:
        return _backfill_desc(s)

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
    # fetch the JD from each NEW posting's builtin job page (schema.org JobPosting); skip
    # ones already in the store so a refresh doesn't re-fetch the whole catalog.
    try:
        existing = {jobs_store._norm_url(r.get("url")) for r in jobs_store.load()["queue"] if r.get("url")}
    except Exception:
        existing = set()
    fresh = [r for r in batch if jobs_store._norm_url(r["url"]) not in existing]
    got = 0
    for r in fresh:
        desc, trunc, _note = _job_desc(s, r["url"])
        if desc:
            r["desc"] = desc
            if trunc:
                r["desc_truncated"] = True
            got += 1
        time.sleep(0.4)
    if fresh:
        print("  fetched JD for %d/%d new postings" % (got, len(fresh)))

    added = jobs_store.add_discovered(batch)
    print("\n%d in-lane builtin postings, %d NEW added to the store." % (len(batch), added))


if __name__ == "__main__":
    main()
