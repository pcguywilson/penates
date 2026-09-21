#!/usr/bin/env python3
"""Close dead postings. Appended to the refresh pipeline after rank.py.

Probes a bounded slice of the queue and sets status=closed only on a strong
signal (HTTP 404/410, a finished redirect onto a generic listing, a real-200
kill phrase, or an Ashby board payload that omits the job id). Timeouts,
blocks, empty bodies, and Cloudflare challenges are stamped last_checked and
left open. Discovered rows never triaged and older than 30 days become
status=expired (no network). Nothing is deleted.

  python prune.py
"""
import datetime, json, re, socket, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import jobs_store

CAP = 300
CONCURRENCY = 6
TIMEOUT = 8
TTL_HOURS = 24
EXPIRE_DAYS = 30
_API_ATS = ("greenhouse", "lever", "ashby", "workday")
_OPEN = ("discovered", "go", "verify")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

_CHALLENGE = re.compile(
    r"just a moment|cf-browser-verification|challenge-platform|"
    r"attention required|enable javascript and cookies", re.I)
# "job not found" is separate: only a short body or a JSON error (SPA shells contain it).
_PHRASES = (
    "no longer accepting",
    "position has been filled",
    "no longer available",
    "posting is closed",
    "this position is closed",
    "the job you are looking for is no longer open",
    "this job is no longer available",
    "this job has expired",
    "job has expired",
)
_UUID = re.compile(r"/[0-9a-fA-F-]{8,}(?:/|$)")
_GH_JOB = re.compile(r"/jobs/\d+")


def _norm_status(status):
    if status is None or isinstance(status, bool):
        return None
    if isinstance(status, str):
        s = status.strip().lower()
        if s in ("", "timeout", "error", "none"):
            return None
        try:
            return int(s)
        except ValueError:
            return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _is_challenge(body):
    return bool(body and _CHALLENGE.search(body[:4096]))


def _json_obj(body):
    try:
        d = json.loads(body)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _json_error(body):
    d = _json_obj(body)
    if not d:
        return False
    if d.get("error") or d.get("errorCode") or d.get("message") and d.get("status") in (
            404, 410, "404", "410"):
        return True
    if str(d.get("status")) in ("404", "410"):
        return True
    return False


def _phrase_hit(body):
    if not body:
        return False
    snip = body[:4096].lower()
    for p in _PHRASES:
        if p in snip:
            return True
    if "job not found" in snip and (len(body) < 2048 or _json_error(body)):
        return True
    return False


def _is_listing(url):
    """True when the URL is a generic board/search/home, not one posting."""
    try:
        p = urllib.parse.urlparse(url or "")
    except Exception:
        return False
    host = (p.netloc or "").lower()
    path = (p.path or "/")
    path_stripped = path.rstrip("/") or "/"
    low = path_stripped.lower()
    if "builtin.com" in host:
        return low in ("/", "/jobs", "/jobs/search")
    if "boards-api.greenhouse.io" in host or host.startswith("api.lever.co") or "api.ashbyhq.com" in host:
        return False
    if "greenhouse.io" in host:
        return not _GH_JOB.search(path)
    if "lever.co" in host:
        return not _UUID.search(path + "/")
    if "ashbyhq.com" in host:
        return not _UUID.search(path + "/")
    if "myworkdayjobs.com" in host:
        if "/job/" in low:
            return False
        return True
    return False


def _ashby_state(final_url, body):
    """'absent' | 'present' | 'bad'. bad = partial/empty/unreadable (do not close)."""
    if not (body or "").strip():
        return "bad"
    m = re.search(r"ashbyhq\.com/([^/?#]+)/([0-9a-fA-F-]{8,})", final_url or "")
    jid = m.group(2) if m else None
    d = _json_obj(body)
    jobs = d.get("jobs") if d else None
    if not isinstance(jobs, list) or not jid:
        return "bad"
    for j in jobs:
        if isinstance(j, dict) and str(j.get("id")) == str(jid):
            return "present"
    return "absent"


def _workday_state(body):
    """'live' | 'http_404' | 'empty' | None (not JSON — caller may use phrases)."""
    if not (body or "").strip():
        return "empty"
    d = _json_obj(body)
    if d is None:
        return None
    err = str(d.get("errorCode") or "")
    if err.upper() in ("HTTP_404", "404"):
        return "http_404"
    info = d.get("jobPostingInfo")
    if isinstance(info, dict) and any(info.get(k) for k in (
            "jobDescription", "title", "id", "jobReqId", "externalUrl")):
        return "live"
    return "empty"


def _structured_live(ats, body):
    """Greenhouse/Lever JSON that still describes the posting is live, not a phrase hit."""
    d = _json_obj(body)
    if ats == "lever" and d is None:
        try:
            arr = json.loads(body)
        except Exception:
            arr = None
        if isinstance(arr, list) and arr and isinstance(arr[0], dict) and arr[0].get("id"):
            return True
    if not isinstance(d, dict):
        return False
    if ats == "greenhouse" and d.get("id") and (d.get("title") or d.get("absolute_url")):
        return True
    if ats == "lever" and d.get("id") and (d.get("text") or d.get("hostedUrl") or d.get("applyUrl")):
        return True
    return False


def decide(http_status, final_url, body, ats):
    """Return ('close', reason) or ('skip', None). No network.

    reason is one of http_404, http_410, redirect_listing, body_phrase, missing_from_board.
    """
    status = _norm_status(http_status)
    body = body or ""
    ats = (ats or "other").lower()
    final_url = final_url or ""

    # Ashby has no per-job 404. Close only when the board payload is 200 and omits this id.
    if ats == "ashby":
        if status != 200 or _is_challenge(body):
            return ("skip", None)
        state = _ashby_state(final_url, body)
        if state == "absent":
            return ("close", "missing_from_board")
        return ("skip", None)

    if status is None or status in (401, 403, 429) or status == 0 or status >= 500:
        return ("skip", None)
    if status == 404:
        return ("close", "http_404")
    if status == 410:
        return ("close", "http_410")
    if _is_challenge(body):
        return ("skip", None)

    if ats == "workday" and status == 200:
        wd = _workday_state(body)
        if wd == "http_404":
            return ("close", "http_404")
        if wd in ("live", "empty"):
            return ("skip", None)

    if status == 200 and ats in ("greenhouse", "lever") and _structured_live(ats, body):
        return ("skip", None)

    if status in (200, 301, 302, 303, 307, 308) and _is_listing(final_url):
        return ("close", "redirect_listing")
    if status != 200:
        return ("skip", None)
    if not body.strip():
        return ("skip", None)
    if _phrase_hit(body):
        return ("close", "body_phrase")
    return ("skip", None)


def _row_ats(row):
    a = (row.get("ats") or "").strip().lower()
    if a:
        return a
    return jobs_store.infer_ats(row.get("apply_url") or row.get("url") or "")


def _as_date(value):
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return datetime.date.fromisoformat(s[:10])
    raise ValueError("not a date")


def _parse_stamp(value):
    s = str(value).strip().replace("Z", "+00:00")
    t = datetime.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc)


def due(row, now, ttl_hours=TTL_HOURS):
    """True when last_checked is missing or older than the TTL."""
    lc = row.get("last_checked")
    if not lc:
        return True
    try:
        return (now - _parse_stamp(lc)) >= datetime.timedelta(hours=ttl_hours)
    except Exception:
        return True


def should_expire(row, now, days=EXPIRE_DAYS):
    """Discovered, never triaged, discovered_at older than `days`. No network."""
    if row.get("status") != "discovered":
        return False
    if not row.get("discovered_at"):
        return False
    try:
        age = (now.date() - _as_date(row.get("discovered_at"))).days
    except Exception:
        return False
    return age > days


def _sort_key(row):
    # Missing last_checked first, then oldest discovered_at. Undated rows last.
    return (0 if not row.get("last_checked") else 1, row.get("discovered_at") or "9999")


def select_candidates(queue, now, cap=CAP):
    """(1) go+verify past TTL, (2) discovered API ATS oldest first, (3) builtin/other."""
    due_rows = [r for r in queue if due(r, now)]
    b1, b2, b3 = [], [], []
    for r in due_rows:
        st = r.get("status")
        if st in ("go", "verify"):
            b1.append(r)
        elif st == "discovered":
            if _row_ats(r) in _API_ATS:
                b2.append(r)
            else:
                b3.append(r)
    out = []
    for group in (b1, b2, b3):
        group.sort(key=_sort_key)
        for r in group:
            if len(out) >= cap:
                return out
            out.append(r)
    return out


def row_key(row):
    u = (row.get("url") or "").split("#")[0].split("?")[0].rstrip("/").lower()
    return (row.get("ats_id") or "", u, (row.get("company") or ""), (row.get("role") or ""))


def _parse_ids(url):
    try:
        import job_context
        ats, board, jid = job_context.parse_ats_url(url)
        if ats and board and jid:
            return ats, board, jid
    except Exception:
        pass
    return None, None, None


def _ids_for(row, url):
    parsed = _parse_ids(url)
    if parsed[0]:
        return parsed
    aid = row.get("ats_id") or ""
    parts = aid.split(":", 2)
    if len(parts) == 3 and all(parts):
        return parts[0].lower(), parts[1], parts[2]
    return parsed


def _quote(part, safe=""):
    return urllib.parse.quote(str(part), safe=safe)


def _eval_api(row, page_url, ats, fetch, timeout):
    _ats, board, jid = _ids_for(row, page_url)
    if ats == "workday":
        try:
            from sources import workday as wd
            parts = wd.parse_url(page_url)
        except Exception:
            parts = None
        if not parts:
            status, final, body, err = fetch(page_url, timeout)
            if err or _norm_status(status) is None:
                return decide(None, final or page_url, "", "workday")
            return decide(status, final or page_url, body or "", "workday")
        api = wd.cxs_detail_url(*parts)
        status, final, body, err = fetch(api, timeout)
        if err or _norm_status(status) is None:
            return decide(None, page_url, "", "workday")
        return decide(status, final or api, body or "", "workday")

    if ats == "ashby":
        if not board:
            return ("skip", None)
        api = "https://api.ashbyhq.com/posting-api/job-board/%s" % _quote(board)
        status, final, body, err = fetch(api, timeout)
        if err or _norm_status(status) != 200:
            return ("skip", None)
        posting = page_url
        if jid and ("api.ashbyhq.com" in (page_url or "").lower() or not re.search(r"[0-9a-fA-F-]{8,}", page_url or "")):
            posting = "https://jobs.ashbyhq.com/%s/%s" % (board, jid)
        return decide(200, posting, body or "", "ashby")

    if ats == "greenhouse" and board and jid:
        api = "https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s" % (_quote(board), _quote(jid))
        status, final, body, err = fetch(api, timeout)
        if err or _norm_status(status) is None:
            return decide(None, page_url, "", "greenhouse")
        return decide(status, final or api, body or "", "greenhouse")

    if ats == "lever" and board and jid:
        api = "https://api.lever.co/v0/postings/%s/%s" % (_quote(board), _quote(jid))
        status, final, body, err = fetch(api, timeout)
        if err or _norm_status(status) is None:
            return decide(None, page_url, "", "lever")
        return decide(status, final or api, body or "", "lever")

    status, final, body, err = fetch(page_url, timeout)
    if err or _norm_status(status) is None:
        return decide(None, final or page_url, "", ats)
    return decide(status, final or page_url, body or "", ats)


def evaluate(row, fetch, timeout=TIMEOUT):
    """Probe one row with an injected fetch(url, timeout) -> (status, final_url, body, error)."""
    page = (row.get("url") or row.get("apply_url") or "").strip()
    if not page:
        return ("skip", None)
    ats = _row_ats(row)
    builtinish = ats == "builtin" or "builtin.com" in page.lower()
    if builtinish:
        status, final, body, err = fetch(page, timeout)
        if err or _norm_status(status) is None:
            return decide(None, final or page, body or "", "builtin")
        landed = jobs_store.infer_ats(final or "")
        if landed in _API_ATS:
            return _eval_api(row, final or page, landed, fetch, timeout)
        return decide(status, final or page, body or "", "builtin")
    if ats in _API_ATS:
        return _eval_api(row, page, ats, fetch, timeout)
    status, final, body, err = fetch(page, timeout)
    if err or _norm_status(status) is None:
        return decide(None, final or page, "", ats)
    landed = jobs_store.infer_ats(final or "")
    if landed in _API_ATS:
        return _eval_api(row, final or page, landed, fetch, timeout)
    return decide(status, final or page, body or "", ats)


def http_get(url, timeout=TIMEOUT):
    """Status-aware GET. (status, final_url, body, error). error is 'timeout'|'error'|None."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(2_000_000)
            status = getattr(r, "status", None) or r.getcode()
            return status, r.geturl(), raw.decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(2_000_000)
        except Exception:
            raw = b""
        final = getattr(e, "url", None) or url
        return e.code, final, raw.decode("utf-8", "replace"), None
    except (TimeoutError, socket.timeout):
        return None, url, "", "timeout"
    except Exception as e:
        msg = str(e).lower()
        kind = "timeout" if ("timed out" in msg or "timeout" in msg) else "error"
        return None, url, "", kind


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    d = jobs_store.load()
    queue = d.get("queue") or []
    active = [r for r in queue if not should_expire(r, now)]
    picked = select_candidates(active, now, cap=CAP)
    outcomes = {}
    if picked:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futs = {ex.submit(evaluate, r, http_get, TIMEOUT): r for r in picked}
            for fut in as_completed(futs):
                r = futs[fut]
                try:
                    outcomes[row_key(r)] = fut.result()
                except Exception:
                    outcomes[row_key(r)] = ("skip", None)
    stamp = now.replace(microsecond=0).isoformat()
    checked = closed = expired = 0
    with jobs_store._LOCK:
        d = jobs_store.load()
        q = d.setdefault("queue", [])
        seen = set()
        for r in q:
            k = row_key(r)
            if k in seen:
                continue
            if should_expire(r, now):
                r["status"] = "expired"
                expired += 1
                seen.add(k)
                continue
            if k not in outcomes:
                continue
            action, reason = outcomes[k]
            r["last_checked"] = stamp
            checked += 1
            if action == "close" and r.get("status") in _OPEN and reason:
                r["status"] = "closed"
                r["closed_reason"] = reason
                r["closed_at"] = stamp
                closed += 1
            seen.add(k)
        d["last_prune"] = {"at": stamp, "checked": checked, "closed": closed, "expired": expired}
        jobs_store.save(d)
    print("prune: checked=%d closed=%d expired=%d" % (checked, closed, expired))


if __name__ == "__main__":
    main()
