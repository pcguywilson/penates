#!/usr/bin/env python3
"""Give listing-page jobs a working apply link that lands on the employer's real form,
and stamp full JD text onto thin rows via public ATS endpoints.

builtin's  <job-url>?handler=ApplyRedirect  bounces straight to the employer ATS when
opened in a browser (builtin blocks server-side fetches, so we don't resolve it here - we
just hand you the redirect link, which does the hop for you). We also *try* a live resolve
so the row can show the true ATS; if builtin blocks it, we keep the redirect link, which
still works when you click it.

After apply links are stamped, a desc-enrichment pass fetches full JD text for thin
greenhouse/lever/ashby/workday rows (so rank.py's required-coverage/gap logic can bite).

  pip install cloudscraper        # optional; plain requests is fine
  python resolve.py               # stamp builtin apply links + enrich thin descs
  python resolve.py --top 40      # just the highest-scored
  python resolve.py --all         # any status
  python resolve.py --enrich-only # skip builtin resolve; only fetch thin JDs
"""
import argparse, time
import jobs_store, job_context

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

FETCHABLE = ("greenhouse", "lever", "ashby", "workday")
THIN_DESC = 800


def _session():
    try:
        import cloudscraper
        return cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    except ImportError:
        import requests
        s = requests.Session(); s.headers.update({"User-Agent": UA}); return s


def _redirect_link(url):
    return url.split("?")[0] + "?handler=ApplyRedirect"


def _resolve(s, url):
    """Return (apply_url, ats). Best-effort live resolve; fall back to the redirect link
    (which does the hop in-browser when clicked)."""
    link = _redirect_link(url)
    try:
        r = s.get(link, allow_redirects=True, timeout=20, headers={"Referer": url.split("?")[0]})
        if r.url and "builtin.com" not in r.url:
            return r.url, jobs_store.infer_ats(r.url)
    except Exception:
        pass
    return link, "builtin (redirect link)"


def _row_url(r):
    return r.get("apply_url") or r.get("url") or ""


def _enrich_descs(d, rows, min_chars=THIN_DESC):
    """Stamp full JD text onto truncated/thin/no-req-section rows. Best-effort.

    Re-fetch when (a) desc_truncated, or (b) FETCHABLE and desc < min_chars, or
    (c) FETCHABLE and desc >= min_chars but no requirements section. (b)/(c) are
    gated on FETCHABLE; legit short non-FETCHABLE JDs are left alone.
    """
    import skills
    targets = []
    for r in rows:
        desc = r.get("desc") or ""
        ats = (job_context.parse_ats_url(_row_url(r)) or (None,))[0]
        truncated = bool(r.get("desc_truncated"))
        thin = ats in FETCHABLE and len(desc) < min_chars
        no_req = (ats in FETCHABLE and len(desc) >= min_chars
                  and skills.extract_jd_skills(desc)[2] is False)
        if truncated or thin or no_req:
            targets.append(r)
    if not targets:
        print("Desc enrichment: nothing to fetch.")
        return 0, 0
    print("Enriching full JD for %d rows (truncated/thin/no-req)...\n" % len(targets))
    got = skipped = 0
    for i, r in enumerate(targets, 1):
        try:
            txt = job_context.fetch_public_posting(_row_url(r))
        except Exception:
            txt = ""
        if txt and len(txt) > len(r.get("desc") or ""):
            r["desc"] = txt
            r["desc_truncated"] = False
            got += 1
        else:
            skipped += 1
        if i % 20 == 0:
            jobs_store.save(d)
            print("  %d/%d  (enriched %d, no-content %d)" % (i, len(targets), got, skipped))
        time.sleep(0.15)
    jobs_store.save(d)
    print("Desc enrichment done: enriched %d, no-content %d." % (got, skipped))
    return got, skipped


def _resolve_builtin(d, rows):
    needs = [r for r in rows if "builtin.com/job/" in (r.get("url") or "") and not r.get("apply_url")]
    if not needs:
        print("Builtin resolve: nothing to do (apply links already stamped).")
        return 0, 0
    print("Attaching apply links to %d builtin listings...\n" % len(needs))
    s = _session()
    live = 0
    for i, r in enumerate(needs, 1):
        apply_url, ats = _resolve(s, r["url"])
        r["apply_url"] = apply_url
        r["ats"] = ats
        if "builtin.com" not in apply_url:
            live += 1
        if i % 15 == 0:
            jobs_store.save(d)
        time.sleep(0.2)
    jobs_store.save(d)
    print("Builtin resolve done: %d/%d got the employer URL directly; the rest use builtin's "
          "redirect link.\n" % (live, len(needs)))
    return live, len(needs)


def main():
    ap = argparse.ArgumentParser(
        description="Attach apply links to builtin listings and enrich thin JD text")
    ap.add_argument("--top", type=int, default=0, help="only the N highest-scored")
    ap.add_argument("--all", action="store_true", help="any status, not just discovered")
    ap.add_argument("--enrich-only", action="store_true",
                    help="skip builtin apply-link resolve; only fetch thin JDs")
    ap.add_argument("--min", type=int, default=THIN_DESC,
                    help="enrich rows whose desc is shorter than this (default 800)")
    a = ap.parse_args()
    d = jobs_store.load(); q = d["queue"]

    rows = [r for r in q if (a.all or r.get("status") == "discovered")]
    rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)
    if a.top > 0:
        rows = rows[:a.top]

    if not a.enrich_only:
        _resolve_builtin(d, rows)
        # re-read queue membership after resolve so builtin rows with a new ATS apply_url
        # are eligible for enrichment in the same run
        rows = [r for r in q if (a.all or r.get("status") == "discovered")]
        rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)
        if a.top > 0:
            rows = rows[:a.top]

    _enrich_descs(d, rows, min_chars=a.min)


if __name__ == "__main__":
    main()
