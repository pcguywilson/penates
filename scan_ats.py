#!/usr/bin/env python3
"""Scan public ATS boards (config/companies.yml) into the local store. No login, no LLM,
zero tokens. This is the reliable discovery spine; the aggregator sources stay secondary.

  python scan_ats.py                 # every enabled company
  python scan_ats.py --ats greenhouse
  python scan_ats.py --company bloomerang
  python scan_ats.py --no-filter     # keep every title/location (debug)
"""
import argparse, concurrent.futures as cf, json, os, re, sys, time, urllib.parse, yaml
import jobs_store
from sources import greenhouse, lever, ashby, workday, smartrecruiters, workable

ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workday": workday,
            "smartrecruiters": smartrecruiters, "workable": workable}

# Board slugs recognized in job URLs from ANY source (LinkedIn, Built In, Google, ...).
# harvest() adds each new board to companies.yml so every later scan reads its whole board.
_BOARD_RES = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("workday", re.compile(r"https?://([a-z0-9-]+\.wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)/", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)/", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)/", re.I)),
]
_SKIP_SLUGS = {"j", "embed", "api", "v1", "jobs", "careers"}


_SLUG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,80}$")


def _clean_slug(ats, m):
    if ats == "workday":
        host, site = m.group(1).lower(), m.group(2)
        return "%s/%s" % (host, site) if re.match(r"^[A-Za-z0-9_-]+$", site) else None
    slug = urllib.parse.unquote(m.group(1)).strip()
    if not _SLUG_OK.match(slug) or slug.lower() in _SKIP_SLUGS:
        return None
    return slug


def _locked_append(path, text):
    """Append under a lock file, via temp + os.replace so a crash never leaves a torn YAML."""
    lock = path + ".lock"
    for _ in range(50):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.close(fd); break
        except FileExistsError:
            if time.time() - os.path.getmtime(lock) > 60:
                try: os.remove(lock)
                except OSError: pass
            time.sleep(0.1)
    else:
        raise RuntimeError("companies.yml is locked")
    try:
        with open(path, encoding="utf-8") as f:
            cur = f.read()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(cur + text)
        yaml.safe_load(cur + text)  # refuse to write something we cannot read back
        os.replace(tmp, path)
    finally:
        try: os.remove(lock)
        except OSError: pass


def harvest(cfg, path):
    """Append boards seen in data/jobs.json that companies.yml doesn't list yet."""
    try:
        q = jobs_store.load()["queue"]
    except Exception:
        return 0
    have = {(c.get("ats"), str(c.get("slug") or "").lower()) for c in cfg.get("companies", [])}
    new = []
    for r in q:
        for u in (r.get("url") or "", r.get("apply_url") or ""):
            for ats, rx in _BOARD_RES:
                m = rx.search(u)
                if not m:
                    continue
                slug = _clean_slug(ats, m)
                if not slug or (ats, slug.lower()) in have:
                    continue
                have.add((ats, slug.lower()))
                name = (r.get("company") or slug).replace('"', "'").replace("\\", "").strip()
                new.append((name, ats, slug))
    if new:
        text = "\n# --- auto-added by scan_ats harvest (boards seen in discovered jobs) ---\n"
        for name, ats, slug in new:
            text += '- name: "%s"\n  ats: %s\n  slug: "%s"\n  enabled: true\n' % (name, ats, slug.replace('"', ""))
        _locked_append(path, text)
    return len(new)


# Board health: a board that errors on 3 scans in a row is skipped (no YAML rewrite).
HEALTH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ats_board_health.json")
FAIL_LIMIT = 3


def _health_load():
    try:
        with open(HEALTH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _health_save(h):
    try:
        tmp = HEALTH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(h, f, indent=0, sort_keys=True)
        os.replace(tmp, HEALTH)
    except OSError:
        pass


HERE = os.path.dirname(os.path.abspath(__file__))

def cfg_path():
    for name in ("companies.yml", "companies.example.yml"):
        path = os.path.join(HERE, "config", name)
        if os.path.exists(path):
            return path
    return None

def load_cfg():
    path = cfg_path()
    if path:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    raise SystemExit("no config/companies.yml (copy config/companies.example.yml to config/companies.yml)")

def _has(term, text):
    return re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(term.lower()), text) is not None


def _match(title, loc, tf, lf):
    t = (title or "").lower(); l = (loc or "").lower()
    if tf:
        pos = [p.lower() for p in (tf.get("positive") or [])]
        neg = [n.lower() for n in (tf.get("negative") or [])]
        if pos and not any(p in t for p in pos): return False
        if any(n in t for n in neg): return False
    if lf and l:
        if any(_has(n, l) for n in (lf.get("negative") or [])): return False
        pos = lf.get("positive") or []
        if pos and not _has("remote", l) and not any(_has(p, l) for p in pos): return False
    return True


_US = None


def _us_ok(r):
    """serve._passes_us_only (word-boundary geo lists); fail-open if serve can't import."""
    global _US
    if _US is None:
        try:
            import serve
            _US = serve._passes_us_only
        except Exception:
            _US = lambda r: True
    try:
        return _US(r)
    except Exception:
        return True


def _remote_us(loc):
    """For an adapter-flagged remote row whose location is a bare city: keep only on a US signal
    (or no geo at all), so remote Prague/Tokyo does not slip through on unknown geo."""
    bare = re.sub(r"(?i)\b(remote|anywhere|hybrid|virtual|home[- ]based)\b|[-,/()|]", " ", loc or "").strip()
    if not bare:
        return True
    try:
        import serve
        return bool(serve._text_has_us(loc))
    except Exception:
        return False


_COUNTRY_ONLY = re.compile(r"(?i)\b(united states( of america)?|usa|u\.s\.a?\.?|us|america|nationwide|"
                           r"multiple locations|\d+ locations?|anywhere)\b|[-,/()|.]")


def _remote_signal(r):
    blob = " ".join(str(r.get(k) or "") for k in ("location", "workplace", "role"))
    return r.get("remote") is True or re.search(r"(?i)\b(remote|work from home|wfh|telecommut|virtual|distributed)\b", blob)


def _keep(r, tf, lf):
    role, loc = r.get("role"), r.get("location") or ""
    # Remote-only search: a city/state location with no remote signal anywhere is onsite/unknown -> drop.
    # Country-only ("United States", "USA", "Multiple Locations") with no city still keeps.
    if loc and not _remote_signal(r) and _COUNTRY_ONLY.sub(" ", loc).strip():
        return False
    if _match(role, loc, tf, lf):
        return _us_ok(r)
    # Title OK but location lacks a Remote/US token: rescue adapter-flagged remote rows in the US.
    if r.get("remote") is True and _match(role, "", tf, lf) and not (lf and any(_has(n, loc.lower()) for n in (lf.get("negative") or []))):
        return _remote_us(loc) and _us_ok(r)
    return False


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
    ap.add_argument("--no-harvest", action="store_true", help="don't add new boards from discovered jobs")
    args = ap.parse_args()
    cfg = load_cfg()
    if not args.no_harvest and cfg_path() and cfg_path().endswith("companies.yml"):
        n = harvest(cfg, cfg_path())
        if n:
            print("harvest: added %d new boards to companies.yml" % n)
            cfg = load_cfg()
    tf = None if args.no_filter else cfg.get("title_filter")
    lf = None if args.no_filter else cfg.get("location_filter")
    comps = [c for c in cfg.get("companies", []) if c.get("enabled", True)]
    if args.ats:
        comps = [c for c in comps if c.get("ats") == args.ats]
    if args.company:
        a = args.company.lower()
        comps = [c for c in comps if c.get("slug") == a or (c.get("name", "").lower() == a)]
    health = _health_load()
    skipped = [c for c in comps if (health.get("%s:%s" % (c.get("ats"), c.get("slug"))) or 0) >= FAIL_LIMIT]
    if skipped and not args.company:
        comps = [c for c in comps if c not in skipped]
        print("skipping %d boards with %d+ consecutive errors (data/ats_board_health.json)" % (len(skipped), FAIL_LIMIT))
    if not comps:
        sys.exit("no companies match")
    all_rows, ok, err = [], 0, 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for c, rows, e in ex.map(fetch_one, comps):
            key = "%s:%s" % (c.get("ats"), c.get("slug"))
            if e:
                health[key] = (health.get(key) or 0) + 1
                err += 1; print("  %-11s %-22s ERROR %s" % (c.get("ats"), c.get("slug"), e)); continue
            health.pop(key, None)
            kept = [r for r in rows if args.no_filter or _keep(r, tf, lf)]
            ok += 1
            print("  %-11s %-22s %3d jobs, %3d kept" % (c.get("ats"), c.get("slug"), len(rows), len(kept)))
            all_rows.extend(kept)
    _health_save(health)
    added = jobs_store.add_discovered(all_rows)
    print("\nboards ok=%d err=%d | kept=%d | NEW added=%d" % (ok, err, len(all_rows), added))

if __name__ == "__main__":
    main()
