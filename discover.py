#!/usr/bin/env python3
"""Discover new jobs and add them to the local store (data/jobs.json).

Local + free. Uses JobSpy (MIT) to search LinkedIn, Indeed, ZipRecruiter and Google Jobs for
your search terms (config.json "terms", or --terms), keeps remote US postings, and writes
the NEW ones (deduped by URL against everything already in the store) as status
'discovered'. Run it as a batch; then `python report.py` or GET /stats show the pipeline.

  pip install python-jobspy          # one-time
  python discover.py                 # your configured terms, last 7 days
  python discover.py --terms "registered nurse,charge nurse" --results 30 --hours 72
"""
import argparse, sys, re, math
import jobs_store

DESC_CAP = 20000

def _clean(x):
    if x is None: return ""
    if isinstance(x, float) and math.isnan(x): return ""
    v = str(x).strip()
    return "" if v.lower() == "nan" else v


def _salary(r):
    def k(x):
        try: x = float(x)
        except Exception: return None
        if math.isnan(x) or x <= 0: return None   # pandas NaN -> no "$nan" salaries
        return ("$%.0fk" % (x / 1000)) if x >= 1000 else ("$%.0f" % x)
    lo, hi = k(r.get("min_amount")), k(r.get("max_amount"))
    if not lo and not hi: return ""
    rng = (lo + "-" + hi) if lo and hi else (lo or hi)
    iv = str(r.get("interval") or "").lower()
    return rng + ("/yr" if "year" in iv else "/hr" if "hour" in iv else "")

DEFAULT_TERMS = [
    "devops engineer", "site reliability engineer", "cloud engineer",
    "platform engineer", "infrastructure engineer",
    "systems administrator", "systems engineer", "systems analyst",
]
# keep only titles that look like your lane; drop obvious non-fits
TITLE_OK = re.compile(r"devops|sre|site reliability|cloud|infrastructure|platform|"
                      r"systems? engineer|systems? admin|systems? analyst|linux|reliability", re.I)
TITLE_NO = re.compile(r"\bsales\b|account exec|recruiter|\bmanager\b|director|"
                      r"\bintern\b|principal architect|pre-?sales|solutions consultant", re.I)


def discover(terms, results, hours, sites, include_easy=False):
    try:
        from jobspy import scrape_jobs
    except Exception:
        sys.exit("Missing JobSpy. Install it first:  pip install python-jobspy")

    import serve as _serve
    batch, seen = [], set()
    for term in terms:
        try:
            df = scrape_jobs(site_name=sites, search_term=term, google_search_term=term + " remote",
                             location="United States", results_wanted=results, hours_old=hours,
                             country_indeed="USA", is_remote=True)
        except Exception as e:
            print("  [%-30s] scrape failed: %s" % (term, str(e)[:60])); continue
        n_kept = 0
        for _, r in df.iterrows():
            site = str(r.get("site") or "")
            direct = _clean(r.get("job_url_direct"))
            url = direct or _clean(r.get("job_url"))
            title = _clean(r.get("title"))
            if not url or url in seen:
                continue
            seen.add(url)
            # LinkedIn: skip Easy Apply (no external link -> stays on LinkedIn, can't autofill)
            if site == "linkedin" and not direct and not include_easy:
                continue
            if TITLE_NO.search(title) or not TITLE_OK.search(title):
                continue
            loc = _clean(r.get("location"))
            # JobSpy is_remote is often true for onsite/hybrid titles; drop those
            # at ingest (same gate as serve._passes_remote_only).
            if _serve._explicit_onsite_hybrid_blocks_remote({"role": title, "location": loc}):
                continue
            desc = _clean(r.get("description"))
            # is_remote is a hint only — never stamp workplace=Remote from it.
            row = {"url": url, "company": _clean(r.get("company")),
                   "role": title[:120], "source": "jobspy:" + site,
                   "posted": _clean(r.get("date_posted")),
                   "location": loc,
                   "workplace": "",
                   "salary": _salary(r),
                   "desc": desc[:DESC_CAP]}
            if r.get("is_remote"):
                row["remote_hint"] = True
            if len(desc) > DESC_CAP:
                row["desc_truncated"] = True
            batch.append(row)
            n_kept += 1
        print("  %-32s %d in-lane" % (term, n_kept))

    added = jobs_store.add_discovered(batch)
    # Mark aggregator rows that lack positive remote confirmation as filtered.
    # Re-run remote_unconfirmed / remote_unknown; restore when they now pass.
    # Never touches user statuses.
    filtered, restored = jobs_store.reconcile_remote_only()
    print("\n%d in-lane postings found, %d NEW added to the store "
          "(the rest were already there)." % (len(batch), added))
    if filtered:
        print("%d discovered jobspy/builtin rows marked filtered "
              "(remote_unknown / not_remote)." % filtered)
    if restored:
        print("%d remote_unknown/unconfirmed rows restored to discovered." % restored)
    if added:
        print("They're status 'discovered' -- triage with `python report.py` "
              "or GET http://127.0.0.1:8765/jobs?status=discovered")
    return added


def main():
    ap = argparse.ArgumentParser(description="Add newly-discovered jobs to the local store")
    ap.add_argument("--terms", default="", help="comma-separated search terms (default: config.json terms)")
    ap.add_argument("--results", type=int, default=25, help="results wanted per term per site")
    ap.add_argument("--hours", type=int, default=168, help="max posting age in hours (default 7 days)")
    ap.add_argument("--sites", default="linkedin,indeed,google",
                    help="comma-separated: linkedin,indeed,zip_recruiter,glassdoor,google")
    ap.add_argument("--include-easy-apply", action="store_true",
                    help="keep LinkedIn Easy Apply jobs too (default: skip them)")
    a = ap.parse_args()
    terms = [t.strip() for t in a.terms.split(",") if t.strip()] or jobs_store.config_terms(DEFAULT_TERMS)
    sites = [s.strip() for s in a.sites.split(",") if s.strip()]
    print("Discovering (remote, US) across %s\nTerms: %s\n" % (", ".join(sites), ", ".join(terms)))
    discover(terms, a.results, a.hours, sites, a.include_easy_apply)


if __name__ == "__main__":
    main()
