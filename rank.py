#!/usr/bin/env python3
"""Rank the 'discovered' jobs by fit so the best float up. Rule-based, instant, no LLM
(scores on title/company, which is what discovery captured). Writes a score to each row
and prints them best-first; then you triage the top ones to go/verify/skip.

  python rank.py            # score all 'discovered', show top 30
  python rank.py --top 50
  python rank.py --all      # score every row, not just discovered
"""
import argparse, re
import jobs_store

CORE   = re.compile(r"site reliability|\bsre\b|devops|platform engineer|infrastructure engineer|cloud engineer", re.I)
NEAR   = re.compile(r"systems? engineer|systems? admin|systems? analyst|\blinux\b|reliability|cloud infrastructure|devsecops", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|staff|lead|principal|iv|iii)\b", re.I)
JUNIOR = re.compile(r"\b(jr\.?|junior|associate|intern|entry)\b", re.I)
EDGE   = re.compile(r"gov\b|govcloud|federal|secret|clearance|aws|kubernetes|terraform|fedramp|azure", re.I)
ARCH   = re.compile(r"architect", re.I)

def score_row(r):
    t = (r.get("role") or "") + " " + (r.get("company") or "")
    s = 0
    if CORE.search(t): s += 45
    elif NEAR.search(t): s += 28
    if SENIOR.search(t): s += 18
    if JUNIOR.search(t): s -= 25
    if ARCH.search(t): s -= 8
    s += min(len(EDGE.findall(t)) * 6, 24)
    return max(0, min(100, s))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show-all", action="store_true", help="include listing dead-ends (LinkedIn jobs/view)")
    a = ap.parse_args()
    d = jobs_store.load(); q = d["queue"]
    rows = q if a.all else [r for r in q if r.get("status") == "discovered"]
    for r in rows:
        r["score"] = score_row(r)
    jobs_store.save(d)
    if not a.show_all:
        rows = [r for r in rows if jobs_store.applyable(r)]
    rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)
    label = "all" if a.all else "discovered"
    print("Top %d of %d %s jobs:\n" % (min(a.top, len(rows)), len(rows), label))
    for r in rows[:a.top]:
        print("  %3d  %-20s %-40s  %s" % (r.get("score") or 0,
              (r.get("company") or "")[:20], (r.get("role") or "")[:40], r.get("ats") or ""))
        print("       %s" % (r.get("apply_url") or r.get("url") or ""))
    print("\nScores are saved to the store. Triage the top ones; GET /jobs?status=discovered for all.")

if __name__ == "__main__":
    main()
