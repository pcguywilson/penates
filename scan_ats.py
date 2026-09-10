#!/usr/bin/env python3
"""Scan public ATS boards (config/companies.yml) into the local store. No login, no LLM,
zero tokens. This is the reliable discovery spine; the aggregator sources stay secondary.

  python scan_ats.py                 # every enabled company
  python scan_ats.py --ats greenhouse
  python scan_ats.py --company bloomerang
  python scan_ats.py --no-filter     # keep every title/location (debug)
"""
import argparse, concurrent.futures as cf, os, sys, yaml
import jobs_store
from sources import greenhouse, lever, ashby

ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
HERE = os.path.dirname(os.path.abspath(__file__))

def load_cfg():
    for name in ("companies.yml", "companies.example.yml"):
        path = os.path.join(HERE, "config", name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f)
    raise SystemExit("no config/companies.yml (copy config/companies.example.yml to config/companies.yml)")

def _match(title, loc, tf, lf):
    t = (title or "").lower(); l = (loc or "").lower()
    if tf:
        pos = [p.lower() for p in (tf.get("positive") or [])]
        neg = [n.lower() for n in (tf.get("negative") or [])]
        if pos and not any(p in t for p in pos): return False
        if any(n in t for n in neg): return False
    if lf and l:
        if any(n.lower() in l for n in (lf.get("negative") or [])): return False
        pos = [p.lower() for p in (lf.get("positive") or [])]
        if pos and "remote" not in l and not any(p in l for p in pos): return False
    return True

def fetch_one(c):
    mod = ADAPTERS.get(c.get("ats"))
    if not mod:
        return (c, [], "no adapter for %s" % c.get("ats"))
    try:
        return (c, mod.fetch(c.get("slug"), company=c.get("name") or c.get("slug")), None)
    except Exception as e:
        return (c, [], str(e)[:120])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ats"); ap.add_argument("--company")
    ap.add_argument("--no-filter", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    cfg = load_cfg()
    tf = None if args.no_filter else cfg.get("title_filter")
    lf = None if args.no_filter else cfg.get("location_filter")
    comps = [c for c in cfg.get("companies", []) if c.get("enabled", True)]
    if args.ats:
        comps = [c for c in comps if c.get("ats") == args.ats]
    if args.company:
        a = args.company.lower()
        comps = [c for c in comps if c.get("slug") == a or (c.get("name", "").lower() == a)]
    if not comps:
        sys.exit("no companies match")
    all_rows, ok, err = [], 0, 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for c, rows, e in ex.map(fetch_one, comps):
            if e:
                err += 1; print("  %-11s %-22s ERROR %s" % (c.get("ats"), c.get("slug"), e)); continue
            kept = [r for r in rows if args.no_filter or _match(r.get("role"), r.get("location"), tf, lf)]
            ok += 1
            print("  %-11s %-22s %3d jobs, %3d kept" % (c.get("ats"), c.get("slug"), len(rows), len(kept)))
            all_rows.extend(kept)
    added = jobs_store.add_discovered(all_rows)
    print("\nboards ok=%d err=%d | kept=%d | NEW added=%d" % (ok, err, len(all_rows), added))

if __name__ == "__main__":
    main()
