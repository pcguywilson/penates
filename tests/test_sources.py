#!/usr/bin/env python3
"""Offline tests for ATS adapters + dedup (no network). Uses recorded JSON shapes."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sources import base, greenhouse, lever, ashby

fails=[]
def check(n,c,x=""):
    print(("PASS " if c else "FAIL ")+n+("" if c else "  <-- "+str(x)))
    if not c: fails.append(n)

# --- greenhouse ---
base.http_json = lambda url, timeout=25: {"jobs":[
  {"id":111,"title":"Senior DevOps Engineer","location":{"name":"Remote - US"},
   "absolute_url":"https://boards.greenhouse.io/acme/jobs/111","updated_at":"2026-09-08T10:00:00-04:00",
   "content":"&lt;p&gt;Build &lt;b&gt;pipelines&lt;/b&gt;&lt;/p&gt;"}]}
g=greenhouse.fetch("acme",company="Acme")[0]
check("gh title", g["role"]=="Senior DevOps Engineer", g)
check("gh apply url", g["url"]=="https://boards.greenhouse.io/acme/jobs/111", g)
check("gh location", g["location"]=="Remote - US", g)
check("gh remote true", g["remote"] is True, g)
check("gh posted date", g["posted"]=="2026-09-08", g)
check("gh ats_id", g["ats_id"]=="greenhouse:acme:111", g)
check("gh desc stripped+unescaped", g["desc"]=="Build pipelines", repr(g["desc"]))
check("gh ats", g["ats"]=="greenhouse" and g["source"]=="ats-api", g)

# --- lever (top-level array, epoch ms) ---
base.http_json = lambda url, timeout=25: [
  {"id":"abc","text":"Site Reliability Engineer","hostedUrl":"https://jobs.lever.co/acme/abc",
   "applyUrl":"https://jobs.lever.co/acme/abc/apply","createdAt":1757000000000,
   "categories":{"location":"Remote"},"workplaceType":"remote","descriptionPlain":"Keep it up"}]
l=lever.fetch("acme",company="Acme")[0]
check("lever title", l["role"]=="Site Reliability Engineer", l)
check("lever apply url is applyUrl", l["url"].endswith("/apply"), l)
check("lever remote", l["remote"] is True, l)
check("lever posted parsed", len(l["posted"])==10 and l["posted"].startswith("2025"), l)

# --- ashby ---
base.http_json = lambda url, timeout=25: {"jobs":[
  {"id":"z9","title":"Platform Engineer","location":"Remote - US","isRemote":True,
   "publishedAt":"2026-09-05T14:00:00.000+00:00",
   "applyUrl":"https://jobs.ashbyhq.com/acme/z9/application","jobUrl":"https://jobs.ashbyhq.com/acme/z9",
   "descriptionPlain":"Own the platform"}]}
a=ashby.fetch("acme",company="Acme")[0]
check("ashby title", a["role"]=="Platform Engineer", a)
check("ashby apply url", a["url"].endswith("/application"), a)
check("ashby posted", a["posted"]=="2026-09-05", a)

# --- dedup: same ats_id / fingerprint collapses; scan filter ---
import importlib, json, tempfile
import jobs_store as js
# point the store at a temp file
tmp=tempfile.mkdtemp(); js.PATH=os.path.join(tmp,"jobs.json")
rows=[g, dict(g, url="https://other.com/x"),                                   # same ats_id -> dup
      dict(g, ats_id=None, url="https://third.com/y")]                          # same fingerprint -> dup
added=js.add_discovered(rows)
check("dedup collapses ats_id + fingerprint (1 added of 3)", added==1, added)
# a genuinely different role is kept
added2=js.add_discovered([dict(g, ats_id="greenhouse:acme:222", role="Cloud Engineer",
                               url="https://boards.greenhouse.io/acme/jobs/222")])
check("distinct role added", added2==1, added2)
stored=js.load()["queue"]
check("stored row carries clean ats + posted + location", 
      stored[0]["ats"]=="greenhouse" and stored[0].get("posted")=="2026-09-08" and stored[0].get("location")=="Remote - US", stored[0])

print("\n"+("ALL PASS" if not fails else "FAILURES: "+", ".join(fails)))
sys.exit(1 if fails else 0)
