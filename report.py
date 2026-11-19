#!/usr/bin/env python3
"""How the job search is going. Reads data/jobs.json via jobs_store; no server needed.

  python report.py
"""
import jobs_store

s = jobs_store.stats()
print("APPLICATIONS")
print("  this week (Mon-)  : %d" % s["applied_this_week"])
print("  this month        : %d" % s["applied_this_month"])
print("  last 7 days        : %d" % s["applied_last7"])
print("  total              : %d  (%d historical/undated)" % (s["applied_total"], s["applied_undated"]))
if s["applied_by_ats"]:
    print("\nAPPLIED BY ATS")
    for a, n in s["applied_by_ats"].items():
        print("  %-14s %d" % (a, n))
print("\nPIPELINE (all rows)")
for st, n in s["pipeline_by_status"].items():
    print("  %-12s %d" % (st, n))
if s["recent"]:
    print("\nRECENT APPLICATIONS")
    for r in s["recent"]:
        print("  %s  %-24s %s" % (r.get("applied_at") or "----------",
                                  (r.get("company") or "")[:24], r.get("role") or ""))
