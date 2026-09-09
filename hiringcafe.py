#!/usr/bin/env python3
"""Pull jobs from hiring.cafe into the local store via its Next.js data endpoint.

hiringcafe.com is a Next.js app; its results come from
  GET /_next/data/<buildId>/index.json?searchState=<url-encoded JSON>
The buildId changes each deploy, so we read it from the homepage at runtime. hiring.cafe
aggregates 40+ ATSs, so results usually carry a direct apply URL.

  pip install requests
  python hiringcafe.py --debug        # dump the response shape; confirm field names
  python hiringcafe.py                # default lane -> store
  python hiringcafe.py --terms "cloud engineer,sre"
"""
import argparse, sys, json, re
from urllib.parse import quote
try:
    import requests
except ImportError:
    sys.exit("pip install requests")
import jobs_store

BASE = "https://hiringcafe.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
DEFAULT_TERMS = ["devops engineer", "site reliability engineer", "cloud engineer",
                 "platform engineer", "infrastructure engineer",
                 "systems administrator", "systems engineer", "systems analyst"]
TITLE_OK = re.compile(r"devops|\bsre\b|site reliability|cloud|infrastructure|platform|"
                      r"systems? engineer|systems? admin|systems? analyst|linux|reliability|devsecops", re.I)
TITLE_NO = re.compile(r"\bsales\b|account exec|recruiter|\bmanager\b|director|\bintern\b|pre-?sales", re.I)


def _session():
    # hiring.cafe sits behind Cloudflare; cloudscraper solves the challenge and carries
    # the cf_clearance cookie automatically. Plain requests gets a 403.
    try:
        import cloudscraper
        return cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    except ImportError:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
        return s


def _build_id(s):
    r = s.get(BASE + "/", timeout=30); r.raise_for_status()
    m = re.search(r'"buildId":"([^"]+)"', r.text)
    if not m:
        raise RuntimeError("could not find Next.js buildId on the homepage")
    return m.group(1)


def _search_state(term):
    return {"commitmentTypes": ["Full Time", "Part Time", "Contract", "Temporary"],
            "workplaceTypes": ["Remote"], "searchQuery": term, "sortBy": "date"}


def _fetch(s, build_id, term):
    ss = quote(json.dumps(_search_state(term)))
    url = "%s/_next/data/%s/index.json?searchState=%s" % (BASE, build_id, ss)
    r = s.get(url, headers={"x-nextjs-data": "1", "Referer": BASE + "/"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _find_jobs(root):
    """Walk the pageProps JSON; return the longest list of dicts that look like jobs."""
    best = []
    def looks_job(d):
        if not isinstance(d, dict):
            return False
        keys = " ".join(k.lower() for k in d.keys())
        return ("title" in keys or "job" in keys or "position" in keys) and \
               ("company" in keys or "apply" in keys or "employer" in keys or "url" in keys or "source" in keys)
    def walk(o):
        nonlocal best
        if isinstance(o, list):
            if o and all(isinstance(x, dict) for x in o[:3]) and any(looks_job(x) for x in o[:3]):
                if len(o) > len(best):
                    best = o
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
    walk(root)
    return best


def _title(j):
    ji = j.get("job_information")
    if isinstance(ji, dict):
        for k in ("title", "job_title_raw"):
            if ji.get(k):
                return str(ji[k])
    v5 = j.get("v5_processed_job_data")
    if isinstance(v5, dict) and v5.get("core_job_title"):
        return str(v5["core_job_title"])
    return ""


def _company(j):
    ecd = j.get("enriched_company_data")
    if isinstance(ecd, dict):
        for k in ("name", "company_name", "display_name", "company"):
            if ecd.get(k):
                return str(ecd[k])
    ao = j.get("attributed_org")
    if isinstance(ao, str) and ao:
        return ao
    if isinstance(ao, dict):
        for k in ("name", "company_name"):
            if ao.get(k):
                return str(ao[k])
    if j.get("board_token"):
        return str(j["board_token"]).replace("-", " ").title()
    return ""


def _url(j):
    return str(j.get("apply_url") or "")


def _wp(j):
    v5 = j.get("v5_processed_job_data") or {}
    return str(v5.get("workplace_type") or "")


def _loc(j):
    v5 = j.get("v5_processed_job_data") or {}
    for k in ("formatted_workplace_location", "workplace_location", "location"):
        if v5.get(k):
            return str(v5[k])
    cities = v5.get("workplace_cities") or []
    countries = v5.get("workplace_countries") or []
    parts = []
    if isinstance(cities, list) and cities:
        parts.append(", ".join(map(str, cities[:2])))
    if isinstance(countries, list) and countries:
        parts.append(", ".join(map(str, countries[:1])))
    return " . ".join(parts)


def _money(x):
    try:
        x = float(x)
    except Exception:
        return None
    return ("$%.0fk" % (x / 1000)) if x >= 1000 else ("$%.0f" % x)


def _num(x):
    try:
        return float(x)
    except Exception:
        return None


def _desc(j):
    ji = j.get("job_information") or {}
    v5 = j.get("v5_processed_job_data") or {}
    for src in (v5.get("requirements_summary"), ji.get("description"), v5.get("role_summary")):
        t = str(src or "").strip()
        if t:
            return t[:1200]
    return ""


def _sal(j):
    v5 = j.get("v5_processed_job_data") or {}
    for name, mult in (("yearly", 1), ("annual", 1), ("hourly", 2080), ("daily", 260),
                       ("weekly", 52), ("bi-weekly", 26), ("monthly", 12)):
        lo, hi = _num(v5.get(name + "_min_compensation")), _num(v5.get(name + "_max_compensation"))
        if lo or hi:
            a = _money(lo * mult) if lo else None
            b = _money(hi * mult) if hi else None
            if a or b:
                return ((a + "-" + b) if a and b else (a or b)) + "/yr"
    return ""



def _posted(j):
    import datetime as _dt
    v5 = j.get("v5_processed_job_data") or {}
    ms = v5.get("estimated_publish_date_millis")
    if ms:
        try:
            return _dt.datetime.fromtimestamp(int(ms) / 1000).date().isoformat()
        except Exception:
            pass
    ji = j.get("job_information") or {}
    for src, k in [(v5, "estimated_publish_date"), (ji, "posted"), (ji, "date_posted")]:
        if isinstance(src, dict) and src.get(k):
            return str(src[k])[:10]
    return ""


def main():
    ap = argparse.ArgumentParser(description="Add hiring.cafe jobs to the local store")
    ap.add_argument("--terms", default="", help="comma-separated; blank = use config.json")
    ap.add_argument("--debug", action="store_true", help="dump the response shape and exit")
    a = ap.parse_args()
    terms = [t.strip() for t in a.terms.split(",") if t.strip()] or jobs_store.config_terms(DEFAULT_TERMS)
    s = _session()
    try:
        bid = _build_id(s)
    except Exception as e:
        if "403" in str(e):
            sys.exit("hiring.cafe blocked the request (Cloudflare).\n"
                     "Fix:  pip install cloudscraper   then re-run.\n"
                     "If it still 403s, the site is using a hard challenge and we'll fetch via the browser instead.")
        sys.exit("Could not load hiring.cafe: %s" % e)
    print("buildId: %s" % bid)

    if a.debug:
        try:
            data = _fetch(s, bid, terms[0])
        except Exception as e:
            sys.exit("Fetch failed: %s" % e)
        pp = data.get("pageProps", data) if isinstance(data, dict) else data
        print("[debug] top keys:", list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        print("[debug] pageProps keys:", list(pp.keys())[:40] if isinstance(pp, dict) else type(pp).__name__)
        jobs = _find_jobs(data)
        print("[debug] located a job list of length:", len(jobs))
        if jobs:
            j0 = jobs[0]
            v5 = j0.get("v5_processed_job_data") or {}
            print("[debug] first job keys:", list(j0.keys())[:50])
            print("[debug] v5_processed_job_data keys:", list(v5.keys())[:70])
            print("[debug] job_information keys:", list((j0.get("job_information") or {}).keys())[:30])
            print("[debug] extracted -> posted=%r location=%r workplace=%r salary=%r"
                  % (_posted(j0), _loc(j0), _wp(j0), _sal(j0)))
            print("[debug] first job JSON (truncated):\n" + json.dumps(j0, indent=1)[:2600])
        print("\nShare the [debug] output and I'll finalize the field mapping.")
        return

    batch, seen = [], set()
    for term in terms:
        try:
            jobs = _find_jobs(_fetch(s, bid, term))
        except Exception as e:
            print("  [%-28s] failed: %s" % (term, str(e)[:70])); continue
        kept = 0
        for j in jobs:
            title = _title(j); company = _company(j); url = _url(j)
            if not url or not title or url in seen:
                continue
            if TITLE_NO.search(title) or not TITLE_OK.search(title):
                continue
            seen.add(url)
            batch.append({"url": url, "company": company, "role": title[:120], "source": "hiringcafe",
                          "posted": _posted(j), "location": _loc(j), "workplace": _wp(j),
                          "salary": _sal(j), "desc": _desc(j)})
            kept += 1
        print("  %-30s %d in-lane" % (term, kept))
    added = jobs_store.add_discovered(batch)
    print("\n%d in-lane hiring.cafe postings, %d NEW added to the store." % (len(batch), added))


if __name__ == "__main__":
    main()
