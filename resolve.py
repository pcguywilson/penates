#!/usr/bin/env python3
"""Give listing-page jobs a working apply link that lands on the employer's real form.

builtin's  <job-url>?handler=ApplyRedirect  bounces straight to the employer ATS when
opened in a browser (builtin blocks server-side fetches, so we don't resolve it here - we
just hand you the redirect link, which does the hop for you). We also *try* a live resolve
so the row can show the true ATS; if builtin blocks it, we keep the redirect link, which
still works when you click it.

  pip install cloudscraper        # optional; plain requests is fine
  python resolve.py               # stamp all builtin 'discovered' jobs
  python resolve.py --top 40      # just the highest-scored
  python resolve.py --all         # any status
"""
import argparse, time
import jobs_store

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


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


def main():
    ap = argparse.ArgumentParser(description="Attach a working apply link to builtin listings")
    ap.add_argument("--top", type=int, default=0, help="only the N highest-scored")
    ap.add_argument("--all", action="store_true", help="any status, not just discovered")
    a = ap.parse_args()
    s = _session()
    d = jobs_store.load(); q = d["queue"]

    def needs(r):
        return "builtin.com/job/" in (r.get("url") or "") and not r.get("apply_url")

    rows = [r for r in q if (a.all or r.get("status") == "discovered") and needs(r)]
    rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)
    if a.top > 0:
        rows = rows[:a.top]
    if not rows:
        print("Nothing to do (builtin jobs already have apply links)."); return
    print("Attaching apply links to %d builtin listings...\n" % len(rows))

    live = 0
    for i, r in enumerate(rows, 1):
        apply_url, ats = _resolve(s, r["url"])
        r["apply_url"] = apply_url
        r["ats"] = ats
        if "builtin.com" not in apply_url:
            live += 1
        if i % 15 == 0:
            jobs_store.save(d)
        time.sleep(0.2)
    jobs_store.save(d)
    print("Done: %d/%d got the employer URL directly; the rest use builtin's redirect link "
          "(which opens the employer form when you click it).\n"
          "rank.py now shows the apply link for these." % (live, len(rows)))


if __name__ == "__main__":
    main()
