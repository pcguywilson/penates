"""Local job store: the single source of truth for discovered and applied jobs.

Backed by data/jobs.json ({"queue":[...]}) so it stays compatible with run_url.py.
Every feature reads/writes rows here so they share one shape:

  id, company, role, url, status, note,        (existing)
  ats, source, score, discovered_at, applied_at, answers[]   (added, all optional)

status vocabulary: discovered, verify, go, applied, closed, skip
This module is import-safe with no third-party deps (stdlib only).
"""
import os, json, re, datetime, threading

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "data", "jobs.json")
_LOCK = threading.Lock()

_ATS = [
    (r"greenhouse\.io|boards\.greenhouse", "greenhouse"),
    (r"lever\.co", "lever"),
    (r"ashbyhq\.com", "ashby"),
    (r"myworkdayjobs\.com|\bworkday\b", "workday"),
    (r"icims\.com", "icims"),
    (r"paylocity\.com", "paylocity"),
    (r"rippling\.com", "rippling"),
    (r"workable\.com", "workable"),
    (r"jobvite\.com", "jobvite"),
    (r"smartrecruiters\.com", "smartrecruiters"),
    (r"taleo\.net", "taleo"),
    (r"bamboohr\.com", "bamboohr"),
    (r"trakstar\.com", "trakstar"),
    (r"pinpointhq\.com", "pinpoint"),
    (r"zohorecruit\.com", "zoho"),
    (r"jobscore\.com", "jobscore"),
    (r"clearcompany\.com", "clearcompany"),
    (r"paycomonline\.net", "paycom"),
    (r"adp\.com", "adp"),
    (r"teksystems\.com", "teksystems"),
    (r"builtin\.com", "builtin"),
    (r"hrmdirect\.com", "hrmdirect"),
]

def infer_ats(url):
    u = (url or "").lower()
    for pat, name in _ATS:
        if re.search(pat, u):
            return name
    return "other"

def _norm_url(u):
    return (u or "").split("#")[0].split("?")[0].rstrip("/").lower()

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "job"

def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("queue"), list):
            return d
    except Exception:
        pass
    return {"queue": []}

def save(d):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, PATH)

def find_by_url(queue, url):
    n = _norm_url(url)
    for r in queue:
        if _norm_url(r.get("url")) == n:
            return r
    return None

def upsert_applied(url, company=None, role=None, ats=None, when=None):
    """Mark a job applied (create the row if it's new). Returns the row."""
    when = when or datetime.date.today().isoformat()
    with _LOCK:
        d = load(); q = d["queue"]
        r = find_by_url(q, url)
        if r is None:
            r = {"id": (_slug(company) + "-" + _slug(role or "job"))[:60],
                 "company": company or "", "role": role or "", "url": url,
                 "source": "manual", "discovered_at": when}
            q.append(r)
        r["status"] = "applied"
        r["applied_at"] = when
        if company and not r.get("company"): r["company"] = company
        if role and not r.get("role"): r["role"] = role
        r["ats"] = ats or r.get("ats") or infer_ats(url)
        save(d)
        return r

def add_discovered(jobs):
    """Insert discovered jobs (dicts: url, company?, role?, score?, source?),
    skipping URLs already present. Returns count added (the dedupe backbone)."""
    with _LOCK:
        d = load(); q = d["queue"]
        have = {_norm_url(r.get("url")) for r in q}
        added = 0
        today = datetime.date.today().isoformat()
        for j in jobs:
            u = j.get("url")
            if not u or _norm_url(u) in have:
                continue
            row = {"id": (_slug(j.get("company")) + "-" + _slug(j.get("role")))[:60],
                   "company": j.get("company", ""), "role": j.get("role", ""),
                   "url": u, "status": "discovered", "ats": infer_ats(u),
                   "source": j.get("source", "discovery"),
                   "score": j.get("score"), "discovered_at": today}
            for k in ("posted", "location", "workplace", "salary", "desc"):
                if j.get(k):
                    row[k] = j[k]
            q.append(row)
            have.add(_norm_url(u)); added += 1
        if added:
            d["last_added"] = datetime.datetime.now().isoformat(timespec="seconds")
            save(d)
        return added

def _on_or_after(datestr, start):
    try:
        return datetime.date.fromisoformat(str(datestr)[:10]) >= start
    except Exception:
        return False

def _config_path():
    return os.path.join(HERE, "config.json")


def load_config():
    try:
        with open(_config_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def config_terms(default):
    t = load_config().get("terms")
    return t if isinstance(t, list) and t else list(default)


def config_sources(default):
    src = load_config().get("sources")
    return src if isinstance(src, dict) else dict(default)


def applyable(r):
    """False for listing-page dead ends we can't get to an apply form on (LinkedIn
    jobs/view with no resolved apply_url is login-gated)."""
    u = (r.get("apply_url") or r.get("url") or "")
    if "linkedin.com/jobs/view" in u and not r.get("apply_url"):
        return False
    return True


def ranked(status="discovered", applyable_only=True):
    q = load()["queue"]
    rows = [r for r in q if (status is None or r.get("status") == status)]
    if applyable_only:
        rows = [r for r in rows if applyable(r)]
    rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)
    return rows


def stats():
    d = load(); q = d["queue"]
    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())   # Monday
    month_start = today.replace(day=1)
    applied = [r for r in q if r.get("status") == "applied"]
    dated = [r for r in applied if r.get("applied_at")]
    def cnt(since): return sum(1 for r in dated if _on_or_after(r["applied_at"], since))
    by_ats, by_status = {}, {}
    for r in q:
        s = r.get("status", "unknown"); by_status[s] = by_status.get(s, 0) + 1
    for r in applied:
        a = r.get("ats") or infer_ats(r.get("url")); by_ats[a] = by_ats.get(a, 0) + 1
    return {
        "last_added": d.get("last_added"),
        "applied_this_week": cnt(week_start),
        "applied_this_month": cnt(month_start),
        "applied_last7": cnt(today - datetime.timedelta(days=6)),
        "applied_total": len(applied),
        "applied_undated": len(applied) - len(dated),
        "applied_by_ats": dict(sorted(by_ats.items(), key=lambda x: -x[1])),
        "pipeline_by_status": dict(sorted(by_status.items(), key=lambda x: -x[1])),
        "recent": [{"company": r.get("company"), "role": r.get("role"),
                    "applied_at": r.get("applied_at"), "ats": r.get("ats") or infer_ats(r.get("url"))}
                   for r in sorted(dated, key=lambda r: r.get("applied_at", ""), reverse=True)[:10]],
    }
