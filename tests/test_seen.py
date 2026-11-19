#!/usr/bin/env python3
"""mark_seen and POST /seen. Uses a temp jobs.json. Does not start :8765.

Run: python tests/test_seen.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

_here = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_here)
sys.path.insert(0, ROOT)

import jobs_store
import serve

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <-- " + str(extra)))
    if not cond:
        fails.append(name)


def _seed(path):
    row = {
        "id": "acme-sre",
        "company": "Acme",
        "role": "SRE",
        "url": "https://boards.greenhouse.io/acme/jobs/111",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/111/application",
        "status": "discovered",
        "score": 7,
        "discovered_at": "2026-10-01T15:04:05+00:00",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"queue": [row]}, f)


def main():
    real_jobs = os.path.join(ROOT, "data", "jobs.json")
    real_bytes = open(real_jobs, "rb").read() if os.path.exists(real_jobs) else None
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "jobs.json")
    orig = jobs_store.PATH
    jobs_store.PATH = path
    httpd = None
    try:
        _seed(path)
        before = open(path, "rb").read()
        missing = jobs_store.mark_seen("https://example.com/nope")
        check("unknown url returns none", missing is None)
        check("unknown url adds nothing", open(path, "rb").read() == before)

        row = jobs_store.mark_seen("https://boards.greenhouse.io/acme/jobs/111")
        check("mark_seen set", bool(row and row.get("seen_at")))
        seen = row.get("seen_at")
        check("seen_at is ISO seconds",
              isinstance(seen, str) and "T" in seen and len(seen) >= 19 and seen[19:20] in ("+", "Z", "-"))
        stored = jobs_store.load()["queue"][0]
        check("status unchanged", stored.get("status") == "discovered", stored.get("status"))
        check("score unchanged", stored.get("score") == 7, stored.get("score"))
        check("discovered_at unchanged",
              stored.get("discovered_at") == "2026-10-01T15:04:05+00:00", stored.get("discovered_at"))

        d = jobs_store.load()
        d["queue"][0]["seen_at"] = "2020-01-02T03:04:05+00:00"
        jobs_store.save(d)
        again = jobs_store.mark_seen("HTTPS://Boards.Greenhouse.io/acme/jobs/111/")
        got = jobs_store.load()["queue"][0]
        check("second call keeps first timestamp",
              again.get("seen_at") == "2020-01-02T03:04:05+00:00"
              and got.get("seen_at") == "2020-01-02T03:04:05+00:00", got.get("seen_at"))
        check("second call leaves status", got.get("status") == "discovered")

        d = jobs_store.load()
        d["queue"][0]["seen_at"] = ""
        jobs_store.save(d)
        via_apply = jobs_store.mark_seen(
            "https://boards.greenhouse.io/acme/jobs/111/application?utm=1")
        got = jobs_store.load()["queue"][0]
        check("apply_url match sets seen_at", bool(via_apply and via_apply.get("seen_at")), via_apply)
        check("apply_url match is the same row",
              got.get("url") == "https://boards.greenhouse.io/acme/jobs/111")
        check("apply_url match keeps status", got.get("status") == "discovered" and got.get("score") == 7)
        check("queue still one row", len(jobs_store.load()["queue"]) == 1)

        httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.H)
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        req = urllib.request.Request(
            base + "/seen",
            data=json.dumps({"url": "https://example.com/missing"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            check("seen 404 on unknown", False, "expected 404")
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode())
            check("seen 404 on unknown", e.code == 404 and body == {"ok": False}, (e.code, body))
        check("404 wrote nothing new", len(jobs_store.load()["queue"]) == 1)

        req = urllib.request.Request(
            base + "/ranked",
            method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode())
        jobs = payload.get("jobs") or []
        check("ranked passes seen_at",
              len(jobs) == 1 and jobs[0].get("seen_at") == got.get("seen_at"), jobs[:1])
        check("ranked keeps discovered_at raw",
              jobs and jobs[0].get("discovered_at") == "2026-10-01T15:04:05+00:00",
              jobs[0].get("discovered_at") if jobs else None)
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        jobs_store.PATH = orig
        shutil.rmtree(tmp, ignore_errors=True)
        if real_bytes is not None and open(real_jobs, "rb").read() != real_bytes:
            check("real jobs.json untouched", False)
        else:
            check("real jobs.json untouched", True)

    print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
