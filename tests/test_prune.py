#!/usr/bin/env python3
"""Offline tests for prune.decide and the closed/expired pin. No network."""
import datetime, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jobs_store
import prune

fails = []

def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <-- " + str(extra)))
    if not cond:
        fails.append(name)

GH = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/111"
ASH = "https://jobs.ashbyhq.com/acme/11111111-2222-3333-4444-555555555555"
WD = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/Role_R1"
NOW = datetime.datetime(2026, 9, 21, 18, 0, tzinfo=datetime.timezone.utc)

def main():
    action, reason = prune.decide(404, GH, "", "greenhouse")
    check("greenhouse 404 closes", action == "close" and reason == "http_404", (action, reason))

    action, reason = prune.decide(None, GH, "", "greenhouse")
    check("greenhouse timeout skips", action == "skip" and reason is None, (action, reason))

    action, reason = prune.decide(403, "https://builtin.com/job/example", "", "builtin")
    check("builtin 403 skips", action == "skip" and reason is None, (action, reason))

    missing = json.dumps({"jobs": [{"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}]})
    action, reason = prune.decide(200, ASH, missing, "ashby")
    check("ashby 200-without-id closes", action == "close" and reason == "missing_from_board", (action, reason))

    action, reason = prune.decide(None, ASH, "", "ashby")
    check("ashby fetch-error skips", action == "skip" and reason is None, (action, reason))
    action, reason = prune.decide(500, ASH, "bad gateway", "ashby")
    check("ashby 500 skips", action == "skip", (action, reason))
    action, reason = prune.decide(404, "https://api.ashbyhq.com/posting-api/job-board/acme", "", "ashby")
    check("ashby board 404 skips", action == "skip", (action, reason))

    action, reason = prune.decide(200, WD, "", "workday")
    check("empty workday 200 skips", action == "skip" and reason is None, (action, reason))
    action, reason = prune.decide(200, WD, '{"jobPostingInfo":{}}', "workday")
    check("empty workday jobPostingInfo skips", action == "skip", (action, reason))

    present = json.dumps({"jobs": [{"id": "11111111-2222-3333-4444-555555555555"}]})
    action, reason = prune.decide(200, ASH, present, "ashby")
    check("ashby id present skips", action == "skip", (action, reason))

    action, reason = prune.decide(410, GH, "", "greenhouse")
    check("410 closes", action == "close" and reason == "http_410", (action, reason))

    live = json.dumps({"id": 111, "title": "SRE", "absolute_url": "https://boards.greenhouse.io/acme/jobs/111",
                       "content": "We are no longer accepting applications for something else"})
    action, reason = prune.decide(200, GH, live, "greenhouse")
    check("live greenhouse JSON skips despite phrase", action == "skip", (action, reason))

    action, reason = prune.decide(200, "https://builtin.com/jobs", "<html>search</html>", "builtin")
    check("builtin listing redirect closes", action == "close" and reason == "redirect_listing", (action, reason))

    action, reason = prune.decide(200, "https://builtin.com/job/example", "<html>Just a moment...</html>", "builtin")
    check("cloudflare challenge skips", action == "skip", (action, reason))

    action, reason = prune.decide(200, "https://boards.greenhouse.io/acme/jobs/111",
                                  "The job you are looking for is no longer open", "greenhouse")
    check("greenhouse phrase closes", action == "close" and reason == "body_phrase", (action, reason))

    long_body = "x" * 3000 + " job not found "
    action, reason = prune.decide(200, "https://example.com/job/1", long_body, "other")
    check("long job-not-found skips", action == "skip", (action, reason))
    action, reason = prune.decide(200, "https://example.com/job/1", "Job not found", "other")
    check("short job-not-found closes", action == "close" and reason == "body_phrase", (action, reason))

    action, reason = prune.decide(200, WD, '{"errorCode":"HTTP_404"}', "workday")
    check("workday errorCode closes", action == "close" and reason == "http_404", (action, reason))
    action, reason = prune.decide(200, WD, '{"jobPostingInfo":{"title":"SRE","jobDescription":"live"}}', "workday")
    check("workday live skips", action == "skip", (action, reason))

    # builtin that stays on builtin is not closed; a hop onto greenhouse uses the API rule
    calls = []
    def fetch(url, timeout):
        calls.append(url)
        if "builtin.com" in url:
            return (403, url, "blocked", None)
        raise AssertionError(url)
    action, reason = prune.evaluate({"url": "https://builtin.com/job/x", "ats": "builtin"}, fetch)
    check("evaluate builtin 403 skips", action == "skip" and len(calls) == 1, (action, reason, calls))

    calls.clear()
    def fetch_hop(url, timeout):
        calls.append(url)
        if "builtin.com" in url:
            return (200, "https://boards.greenhouse.io/acme/jobs/111", "<html>", None)
        if "boards-api.greenhouse.io" in url and url.rstrip("/").endswith("/jobs/111"):
            return (404, url, '{"status":404}', None)
        raise AssertionError(url)
    action, reason = prune.evaluate({"url": "https://builtin.com/job/x", "ats": "builtin"}, fetch_hop)
    check("builtin hop to greenhouse 404 closes via API", action == "close" and reason == "http_404", (action, reason, calls))

    calls.clear()
    def fetch_stay(url, timeout):
        calls.append(url)
        return (200, "https://builtin.com/job/x", "<html>Apply now</html>", None)
    action, reason = prune.evaluate({"url": "https://builtin.com/job/x", "ats": "builtin"}, fetch_stay)
    check("builtin ApplyRedirect stays open", action == "skip" and len(calls) == 1, (action, reason))

    old = "2026-08-01T00:00:00+00:00"
    mid = "2026-09-10T00:00:00+00:00"
    recent_check = "2026-09-21T12:00:00+00:00"
    rows = [
        {"url": "https://builtin.com/job/a", "ats": "builtin", "status": "discovered", "discovered_at": old, "company": "A", "role": "R"},
        {"url": "https://boards.greenhouse.io/acme/jobs/1", "ats": "greenhouse", "status": "discovered", "discovered_at": mid, "company": "B", "role": "R"},
        {"url": "https://boards.greenhouse.io/acme/jobs/2", "ats": "greenhouse", "status": "discovered", "discovered_at": old, "company": "C", "role": "R"},
        {"url": "https://example.com/go", "ats": "other", "status": "go", "discovered_at": mid, "company": "D", "role": "R"},
        {"url": "https://example.com/verify", "ats": "other", "status": "verify", "discovered_at": old, "last_checked": recent_check, "company": "E", "role": "R"},
        {"url": "https://boards.greenhouse.io/acme/jobs/3", "ats": "greenhouse", "status": "discovered", "discovered_at": old, "last_checked": recent_check, "company": "F", "role": "R"},
    ]
    # Aug 1 is >30d before Sep 21, so the builtin + gh/2 rows expire and are not probed.
    picked = [r["url"] for r in prune.select_candidates([r for r in rows if not prune.should_expire(r, NOW)], NOW)]
    check("select order go then oldest greenhouse",
          picked == ["https://example.com/go", "https://boards.greenhouse.io/acme/jobs/1"], picked)
    check("verify inside TTL not selected", "https://example.com/verify" not in picked)
    check("old discovered expires", prune.should_expire(rows[0], NOW) and prune.should_expire(rows[2], NOW))
    check("go does not expire", not prune.should_expire(rows[3], NOW))
    check("recent discovered does not expire", not prune.should_expire(
        {"status": "discovered", "discovered_at": mid}, NOW))

    # rediscover must not reopen closed or expired
    tmp = tempfile.mkdtemp()
    jobs_store.PATH = os.path.join(tmp, "jobs.json")
    row = {"url": "https://boards.greenhouse.io/acme/jobs/9", "company": "Acme", "role": "SRE",
           "ats": "greenhouse", "ats_id": "greenhouse:acme:9", "desc": "short"}
    check("seed discovered", jobs_store.add_discovered([row]) == 1)
    d = jobs_store.load()
    d["queue"][0]["status"] = "closed"
    d["queue"][0]["closed_reason"] = "http_404"
    jobs_store.save(d)
    jobs_store.add_discovered([dict(row, desc="a much longer description than before")])
    got = jobs_store.load()["queue"][0]
    check("rediscover keeps closed", got["status"] == "closed" and got["closed_reason"] == "http_404", got.get("status"))
    check("rediscover still enriches desc", "longer" in (got.get("desc") or ""), got.get("desc"))
    d = jobs_store.load()
    d["queue"][0]["status"] = "expired"
    jobs_store.save(d)
    jobs_store.add_discovered([dict(row, desc="a much longer description than before, again")])
    got = jobs_store.load()["queue"][0]
    check("rediscover keeps expired", got["status"] == "expired", got.get("status"))

    d = jobs_store.load()
    d["last_prune"] = {"at": "2026-09-21T18:00:00+00:00", "checked": 1, "closed": 1, "expired": 0}
    jobs_store.save(d)
    check("stats surfaces last_prune", (jobs_store.stats().get("last_prune") or {}).get("closed") == 1)

    print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
    return 0 if not fails else 1

if __name__ == "__main__":
    sys.exit(main())
