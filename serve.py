#!/usr/bin/env python3
"""Local server for the apply tool. Listens on 127.0.0.1:8765.

Routes:
  /apply?url=...   -> launches run_url.py --attach <url> (the CDP full-fill path)
  /answer  (POST)  -> JSON {question, limit?, company?, url?, fresh?} -> grounded
                      answer. Tiers: structured field -> intent template ->
                      LEARNED (your vetted past answers) -> compose (LLM + gap guard).
  /learn   (POST)  -> JSON {question, answer, company?} -> remember YOUR final
                      answer, keyed by the normalized question, so the same
                      essay question returns your vetted words next time.
                      Company-specific "why this company" answers are NOT stored
                      (they don't transfer across postings).

Leave this window running in the background.
"""
import os, sys, re, json, time, subprocess, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
LEARN_PATH = os.path.join(HERE, "data", "learned_answers.json")
IMPORTED_PATH = os.path.join(HERE, "data", "imported_answers.json")

def imported_lookup(q):
    """A prior answer imported from an AI-chat export (import_qa.py). Untrusted reference:
    exact normalized-question match, else a high-overlap near match. Returned as a REVIEW
    draft, never auto-trusted."""
    try:
        store = json.load(open(IMPORTED_PATH, encoding="utf-8"))
        answers = store.get("answers", {}) or {}
    except Exception:
        return None
    if not answers:
        return None
    nq = _norm_q(q)
    rec = answers.get(nq)
    if not rec:
        qt = set(nq.split())
        if not qt:
            return None
        best, bs = None, 0.0
        for k, r in answers.items():
            kt = set(k.split())
            if not kt:
                continue
            j = len(qt & kt) / float(len(qt | kt))
            if j > bs:
                bs, best = j, r
        if bs >= 0.72:
            rec = best
        else:
            return None
    return rec if rec and rec.get("variants") else None

_last_proc = None

def launch(url):
    global _last_proc
    if _last_proc is not None and _last_proc.poll() is None:
        try:
            _last_proc.terminate()
            print("[kill] terminated previous run pid=%s" % _last_proc.pid, flush=True)
        except Exception:
            pass
    args = [sys.executable, os.path.join(HERE, "run_url.py"), "--attach", url]
    kw = {"cwd": HERE}
    if os.name == "nt":
        kw["creationflags"] = 0x00000010  # CREATE_NEW_CONSOLE
    _last_proc = subprocess.Popen(args, **kw)

# ---- learned-answer store (correction memory) ------------------------------
def _norm_q(q):
    """Normalize a question to a stable key: lowercase, punctuation to spaces,
    collapse whitespace. 'Describe a time...' == 'describe a time'."""
    return re.sub(r"[^a-z0-9]+", " ", (q or "").lower()).strip()

def _load_learned():
    try:
        with open(LEARN_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_learned(store):
    try:
        os.makedirs(os.path.dirname(LEARN_PATH), exist_ok=True)
        with open(LEARN_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=1, ensure_ascii=False)
    except Exception as e:
        print("[learn] save failed: %s" % e, flush=True)

def _company_specific(q, answer, company):
    """True if the answer is tied to a specific company and must NOT be reused
    across postings (the cross-contamination guard, at the learning layer)."""
    ql = (q or "").lower()
    if re.search(r"why.*(this )?(company|us|here|role|team|position|job)|"
                 r"interested in (working|this)|want to (work|join)|drew you|"
                 r"excites you about|why do you want", ql):
        return True
    if company and len(company) >= 4 and company.lower() in (answer or "").lower():
        return True
    return False

def learned_lookup(q):
    store = _load_learned()
    e = store.get(_norm_q(q))
    if not e:
        return None
    # bump usage counter (best-effort)
    try:
        e["uses"] = int(e.get("uses", 0)) + 1
        _save_learned(store)
    except Exception:
        pass
    return e.get("answer") or None

def do_learn(payload):
    q = (payload.get("question") or "").strip()
    a = (payload.get("answer") or "").strip()
    company = (payload.get("company") or "").strip()
    if not q or len(a) < 20:
        return {"ok": False, "skipped": "too short or empty (structured values aren't learned)"}
    if _company_specific(q, a, company):
        return {"ok": False, "skipped": "company-specific answer (not reused across postings)"}
    store = _load_learned()
    k = _norm_q(q)
    prev = store.get(k, {})
    store[k] = {"question": q[:400], "answer": a, "company_seen": company[:80],
                "uses": int(prev.get("uses", 0)), "updated": int(time.time())}
    _save_learned(store)
    return {"ok": True, "key": k, "count": len(store)}

# ---- answer resolution -----------------------------------------------------
def _shape(r):
    return {
        "ok": r.get("kind") in ("answer", "field"),
        "kind": r.get("kind"),
        "method": r.get("method"),
        "chars": r.get("chars"),
        "gaps": r.get("gaps", []),
        "text": r.get("text", ""),
    }

# Server-side field guards - the single chokepoint every field passes through, so they hold
# even if the extension is stale. Order matters: SKIP beats the yaml matcher beats the essay.
#  - _SKIP_CONDITIONAL: "If you responded 'yes'/'other'..." follow-ups. The controlling Yes/No
#    is usually unset, and answering blind pulls a wrong-context match or invents specifics.
#  - _SKIP_LEAVEBLANK: open optional prompts (accommodations, "anything else", additional info).
#    No grounded answer exists; matching here leaked race "White" into an accommodations box that
#    merely said "other than your ethnicity".
#  - _COI_NO: a personal-relationship / conflict-of-interest Yes/No -> deterministic No.
_SKIP_CONDITIONAL = re.compile(
    r"\bif you (responded|answered|selected|indicated|checked|chose)\b|"
    r"\bif (yes|no|other|so|applicable|not|the above|you did)\b", re.I)
_SKIP_LEAVEBLANK = re.compile(
    r"accommodat|other than your|is there anything|anything (else|you.?d like|we should know)|"
    r"additional (information|comments|details)|feel free to (add|share|include)", re.I)
_COI_NO = re.compile(
    r"(close personal|personal relationship|family member|domestic partner|friend)"
    r"[^.?]{0,80}(working|employed|currently at|conflict)|conflict of interest", re.I)

def do_answer(payload):
    """Tiered. Imported lazily so a broken import can't stop the server.
      0. field guards               -> skip conditional/optional; deterministic COI = No
      1. structured/identity/salary  -> match_field (fields.yaml -> profile.yaml)
      2. intent template             -> answer(question=...)  (why_company etc)
      2.5 learned                    -> your vetted past answer for this question
      3. open-ended essay            -> compose()             (grounded LLM + gap guard)
    Pass fresh:true to skip the learned tier and force a new compose.
    """
    import apply as _apply
    q = (payload.get("question") or "").strip()
    if not q:
        return {"ok": False, "text": "[ERROR] missing question"}
    limit = payload.get("limit")
    try:
        limit = int(limit) if limit else None
    except Exception:
        limit = None
    fresh = bool(payload.get("fresh"))

    # tier 0: field guards (beat every other tier)
    if _SKIP_CONDITIONAL.search(q) or _SKIP_LEAVEBLANK.search(q):
        return {"ok": False, "kind": "pause", "method": "leave-blank",
                "chars": 0, "gaps": [], "text": ""}
    if _COI_NO.search(q):
        return {"ok": True, "kind": "field", "method": "field",
                "chars": 2, "gaps": [], "text": "No"}

    # tier 1: deterministic structured field (salary, country, links, yes/no...)
    try:
        prof = _apply.load("profile.yaml"); fields = _apply.load("fields.yaml")
        val, how = _apply.match_field(q, prof, fields, required=True)
        if how == "field" and val not in (None, ""):
            return _shape({"kind": "field", "method": "field", "chars": len(str(val)),
                           "gaps": [], "text": str(val)})
        if how and str(how).startswith("pause"):
            # fields.yaml explicitly marks this optional/leave-blank -> do NOT compose
            return _shape({"kind": "pause", "method": "optional-skip",
                           "chars": 0, "gaps": [], "text": ""})
    except Exception as e:
        print("[tier1 skip] " + str(e), flush=True)

    # tier 1.2: imported reference (your prior AI-chat answers) - a REVIEW draft, not trusted.
    # Skipped on fresh so Regenerate falls through to a newly generated answer.
    if not fresh:
        try:
            rec = imported_lookup(q)
            if rec:
                v = (rec.get("variants") or [""])[0]
                if v:
                    return {"ok": True, "kind": "answer", "method": "imported",
                            "genre": "imported", "chars": len(v), "words": len(v.split()),
                            "gaps": [], "review": True,
                            "checks": ["imported - review before use"],
                            "variants": rec.get("variants", []),
                            "category": rec.get("category", ""), "text": v}
        except Exception as e:
            print("[imported skip] " + str(e), flush=True)

    # tier 1.5: genre router - project / hypothetical / gap questions go to the genre
    # engine (essay.py), NOT the flat answers.yaml templates. why_company / why_role also
    # route here so they name the real company and invent no company facts. technical_experience
    # / definition still flow to the tier-2 templates below.
    try:
        import essay as _essay
        _c = _essay.parse_constraints(q, limit)
        _genre = _essay.classify_genre(q, _c)
        _gaps = _essay.detect_gaps(q)
        _is_gap = bool(_gaps["hard"] or _gaps["limited"])
        if _genre in ("owned_project", "hypothetical", "behavioral", "short_text", "why_company", "why_role") or _is_gap:
            if not fresh:
                _la = learned_lookup(q)
                if _la:
                    return _shape({"kind": "answer", "method": "learned", "chars": len(_la),
                                   "gaps": [], "text": _la})
            out = _essay.answer_essay(q, limit=limit, company=payload.get("company"),
                                      url=payload.get("url"), page_context=payload.get("page_context"))
            out.setdefault("ok", out.get("kind") in ("answer", "field"))
            return out
    except Exception as e:
        print("[essay route skip] " + str(e), flush=True)

    # tier 2: intent template
    try:
        r = _apply.answer(question=q, max_chars=limit, cli_company=(payload.get("company") or None))
        if r.get("kind") == "answer":
            return _shape(r)
    except Exception as e:
        print("[tier2 skip] " + str(e), flush=True)

    # tier 2.5: learned answer (free-text you already vetted). Skipped on fresh.
    if not fresh:
        try:
            la = learned_lookup(q)
            if la:
                return _shape({"kind": "answer", "method": "learned",
                               "chars": len(la), "gaps": [], "text": la})
        except Exception as e:
            print("[learned skip] " + str(e), flush=True)

    # tier 3: genre-aware essay engine (fallback for anything not caught above)
    try:
        import essay as _essay
        out = _essay.answer_essay(q, limit=limit, company=payload.get("company"),
                                  url=payload.get("url"), page_context=payload.get("page_context"))
        out.setdefault("ok", out.get("kind") in ("answer", "field"))
        return out
    except Exception as e:
        print("[essay tier3 skip] " + str(e), flush=True)
        return _shape(_apply.compose(q, max_chars=limit))

# ---- refresh pipeline (dashboard "Refresh jobs" button) --------------------
_PIPE = {"running": False, "log": [], "started": 0, "finished": 0}
_ANSWER_LOG = []   # local-model reasoning: recent /answer resolutions
_FILL_LOG = []     # form-fill reports posted by the extension
_PIPE_SCRIPTS = ["scan_ats.py", "discover.py", "hiringcafe.py", "builtin.py", "remotive.py", "remoteok.py", "resolve.py", "rank.py"]

def _run_pipeline():
    import jobs_store
    srcs = jobs_store.config_sources({"ats": True, "discover": True, "hiringcafe": True,
                                      "builtin": True, "remotive": True, "remoteok": True})
    src_of = {"scan_ats.py": "ats", "discover.py": "discover", "hiringcafe.py": "hiringcafe",
              "builtin.py": "builtin", "remotive.py": "remotive", "remoteok.py": "remoteok"}
    _PIPE.update(running=True, log=["starting refresh..."], started=time.time(), finished=0)
    for sc in _PIPE_SCRIPTS:
        key = src_of.get(sc)
        if key and not srcs.get(key, True):
            _PIPE["log"].append("skip " + sc + " (disabled)"); continue
        _PIPE["log"].append("running " + sc + " ...")
        try:
            pr = subprocess.run([sys.executable, sc], cwd=HERE, capture_output=True, text=True, timeout=900)
            tail = [ln for ln in (pr.stdout or "").splitlines() if ln.strip()][-2:]
            _PIPE["log"].append("  " + (" | ".join(tail) if tail else "(done)"))
        except Exception as e:
            _PIPE["log"].append("  ERROR: " + str(e)[:120])
    _PIPE.update(running=False, finished=time.time())
    _PIPE["log"].append("refresh complete.")

def _start_pipeline():
    if _PIPE.get("running"):
        return False
    threading.Thread(target=_run_pipeline, daemon=True).start()
    return True


class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code); self._cors()
        self.send_header("Content-Type", ctype); self.end_headers()
        if isinstance(body, str): body = body.encode()
        self.wfile.write(body)
    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/dashboard"):
            try:
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, str(e))
        if parsed.path == "/demo":
            try:
                with open(os.path.join(HERE, "demo.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, str(e))
        if parsed.path == "/stories":
            try:
                with open(os.path.join(HERE, "stories.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, str(e))
        if parsed.path == "/api/queue-lite":
            try:
                import jobs_store
                out = []
                for r in jobs_store.load()["queue"]:
                    out.append({"id": r.get("id"), "company": r.get("company", ""),
                                "role": r.get("role", ""), "url": r.get("apply_url") or r.get("url", ""),
                                "has_jd": len(r.get("desc") or "") > 200})
                out.sort(key=lambda x: (not x["has_jd"], x["company"].lower()))
                return self._json(200, {"count": len(out), "jobs": out})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if parsed.path == "/api/imports":
            try:
                st = json.load(open(IMPORTED_PATH, encoding="utf-8"))
                ans = st.get("answers", {}) or {}
                items = [{"key": k, "question": v.get("question", ""),
                          "category": v.get("category", ""), "variants": v.get("variants", []),
                          "source": v.get("source", "")} for k, v in ans.items()]
                items.sort(key=lambda r: (r["category"], r["question"].lower()))
                return self._json(200, {"count": len(items), "items": items})
            except Exception:
                return self._json(200, {"count": 0, "items": []})
        if parsed.path == "/api/imports/stats":
            try:
                st = json.load(open(IMPORTED_PATH, encoding="utf-8"))
                return self._json(200, {"total": len(st.get("answers", {}) or {}),
                                        "last_import": st.get("last_import")})
            except Exception:
                return self._json(200, {"total": 0, "last_import": None})
        if parsed.path == "/api/stories":
            try:
                import stories_store
                return self._json(200, {"stories": stories_store.load()})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if parsed.path == "/ranked":
            try:
                import jobs_store
                rows = jobs_store.ranked()
                out = [{"company": r.get("company", ""), "role": r.get("role", ""),
                        "score": r.get("score") or 0, "ats": r.get("ats", ""),
                        "status": r.get("status"), "source": r.get("source", ""),
                        "posted": r.get("posted", ""),
                        "location": r.get("location") or r.get("workplace", ""),
                        "workplace": r.get("workplace", ""),
                        "salary": r.get("salary", ""), "desc": r.get("desc", ""),
                        "url": r.get("url", ""),
                        "apply": r.get("apply_url") or r.get("url", "")} for r in rows]
                return self._json(200, {"count": len(out), "jobs": out})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if parsed.path == "/discover/status":
            return self._json(200, _PIPE)
        if parsed.path == "/logs":
            return self._json(200, {"pipeline": _PIPE.get("log", []), "answers": _ANSWER_LOG, "fills": _FILL_LOG})
        if parsed.path == "/config":
            try:
                import jobs_store
                return self._json(200, jobs_store.load_config())
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if parsed.path == "/profile":
            def _read(fn):
                try:
                    with open(os.path.join(HERE, fn), encoding="utf-8") as f:
                        return f.read()
                except Exception:
                    return ""
            return self._json(200, {"profile": _read("profile.yaml"),
                                    "work_history": _read(os.path.join("data", "work_history.yaml"))})
        if parsed.path == "/stats":
            try:
                import jobs_store
                return self._json(200, jobs_store.stats())
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/jobs":
            try:
                import jobs_store
                q = jobs_store.load()["queue"]
                st = (urllib.parse.parse_qs(parsed.query).get("status") or [""])[0]
                if st:
                    q = [r for r in q if r.get("status") == st]
                return self._json(200, {"count": len(q), "jobs": q})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path != "/apply":
            return self._send(404, b"not found")
        url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
        if not url:
            return self._send(400, b"missing url")
        print("[launch] " + url, flush=True)
        try:
            launch(url); self._send(200, b"launched")
        except Exception as e:
            print("[error] " + str(e), flush=True); self._send(500, str(e))
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        if parsed.path == "/discover":
            started = _start_pipeline()
            return self._json(200, {"started": started, "running": _PIPE.get("running", False)})
        if parsed.path == "/fill-log":
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {}
            payload["t"] = int(time.time())
            _FILL_LOG.insert(0, payload); del _FILL_LOG[30:]
            return self._json(200, {"ok": True})
        if parsed.path == "/config":
            try:
                cfg = json.loads(raw or b"{}")
                import jobs_store
                jobs_store.save_config(cfg)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/imports/update":
            try:
                payload = json.loads(raw or b"{}")
                import import_qa
                store = import_qa.load_store(); ans = store.setdefault("answers", {})
                old = ans.pop((payload.get("key") or "").strip(), {})
                q = (payload.get("question") or old.get("question") or "").strip()
                if not q:
                    return self._json(400, {"ok": False, "error": "question required"})
                variants = [v.strip() for v in (payload.get("variants") or []) if v and v.strip()]
                if not variants:
                    variants = old.get("variants", [])
                nk = import_qa._norm_q(q)
                ans[nk] = {"question": q[:400], "variants": variants[:6],
                           "category": (payload.get("category") or old.get("category") or "").strip(),
                           "source": old.get("source", "edited"),
                           "imported_at": old.get("imported_at"), "status": "reference"}
                import_qa.save_store(store)
                return self._json(200, {"ok": True, "key": nk})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/imports/delete":
            try:
                payload = json.loads(raw or b"{}")
                import import_qa
                store = import_qa.load_store()
                store.get("answers", {}).pop((payload.get("key") or "").strip(), None)
                import_qa.save_store(store)
                return self._json(200, {"ok": True, "count": len(store.get("answers", {}))})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/tailor":
            try:
                payload = json.loads(raw or b"{}")
                import tailor as _t
                jd = payload.get("jd") or ""
                company = payload.get("company") or ""
                role = payload.get("role") or ""
                if not jd and payload.get("job"):
                    jd, row = _t._load_jd_from_job(payload["job"])
                    if row:
                        company = company or row.get("company", "")
                        role = role or row.get("role", "")
                if not jd:
                    return self._json(400, {"ok": False, "error": "no job description (paste one or pick a job with a JD)"})
                t = _t.tailor(jd)
                return self._json(200, {"ok": True, "coverage": t["coverage"],
                                        "html": _t.resume_html(t), "markdown": _t.to_markdown(t),
                                        "company": company, "role": role})
            except Exception as e:
                print("[error] tailor: " + str(e), flush=True)
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/cover":
            try:
                payload = json.loads(raw or b"{}")
                import tailor as _t
                jd = payload.get("jd") or ""
                company = payload.get("company") or ""
                role = payload.get("role") or ""
                if not jd and payload.get("job"):
                    jd, row = _t._load_jd_from_job(payload["job"])
                    if row:
                        company = company or row.get("company", "")
                        role = role or row.get("role", "")
                if not jd:
                    return self._json(400, {"ok": False, "error": "no job description"})
                out = _t.cover_letter(jd, company=company, role=role)
                return self._json(200, {"ok": True, **out})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/import":
            try:
                payload = json.loads(raw or b"{}")
                content = payload.get("content") or ""
                fn = re.sub(r"[^A-Za-z0-9_.-]", "_", (payload.get("filename") or "import.json"))[:80]
                if not fn.lower().endswith((".json", ".md", ".txt")):
                    fn += ".json"
                impdir = os.path.join(HERE, "imports")
                os.makedirs(impdir, exist_ok=True)
                path = os.path.join(impdir, fn)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                import import_qa
                r = import_qa.run(path, use_llm=bool(payload.get("llm")))
                print("[import] %s -> +%d ~%d skip=%d total=%d" %
                      (fn, r["added"], r["updated"], r["skipped"], r["total"]), flush=True)
                return self._json(200, {"ok": True, **r})
            except Exception as e:
                print("[error] import: " + str(e), flush=True)
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/stories":
            try:
                payload = json.loads(raw or b"{}")
                import stories_store
                st = stories_store.upsert(payload)
                print("[stories] upsert %s" % st.get("id"), flush=True)
                return self._json(200, {"ok": True, "story": st})
            except Exception as e:
                print("[error] " + str(e), flush=True)
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/stories/delete":
            try:
                payload = json.loads(raw or b"{}")
                import stories_store
                n = stories_store.delete((payload.get("id") or "").strip())
                return self._json(200, {"ok": True, "count": n})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/apply":
            url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
            if not url:
                return self._send(400, b"missing url")
            print("[launch] " + url, flush=True)
            try:
                launch(url); return self._send(200, b"launched")
            except Exception as e:
                print("[error] " + str(e), flush=True); return self._send(500, str(e))
        if parsed.path == "/applied":
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                return self._json(400, {"ok": False, "error": "bad json"})
            url = (payload.get("url") or "").strip()
            if not url:
                return self._json(400, {"ok": False, "error": "missing url"})
            try:
                import jobs_store
                r = jobs_store.upsert_applied(url, company=payload.get("company"),
                                              role=payload.get("role"), ats=payload.get("ats"))
                print("[applied] %s | %s (%s)" % (r.get("company"), r.get("role"), r.get("ats")), flush=True)
                return self._json(200, {"ok": True, "id": r.get("id"), "applied_at": r.get("applied_at"),
                                        "company": r.get("company"), "role": r.get("role")})
            except Exception as e:
                print("[error] " + str(e), flush=True)
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path in ("/answer", "/learn"):
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                return self._json(400, {"ok": False, "text": "[ERROR] bad json"})
            if parsed.path == "/learn":
                try:
                    out = do_learn(payload)
                    print("[learn] %s :: %s" % (out.get("ok"), out.get("skipped") or out.get("key")), flush=True)
                    return self._json(200, out)
                except Exception as e:
                    print("[error] " + str(e), flush=True)
                    return self._json(500, {"ok": False, "error": str(e)})
            q = (payload.get("question") or "")[:600]
            print("[answer] " + q.replace("\n", " ")[:120], flush=True)
            try:
                out = do_answer(payload)
                print("       -> %s / %s chars / gaps=%s" % (out.get("method"), out.get("chars"), out.get("gaps")), flush=True)
                try:
                    _ANSWER_LOG.insert(0, {"q": q[:120], "method": out.get("method"),
                                           "chars": out.get("chars"), "gaps": out.get("gaps"),
                                           "t": int(time.time())})
                    del _ANSWER_LOG[60:]
                except Exception:
                    pass
                return self._json(200, out)
            except Exception as e:
                print("[error] " + str(e), flush=True)
                return self._json(500, {"ok": False, "text": "[ERROR] " + str(e)})
        return self._send(404, b"not found")
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print("Apply autofill server on http://127.0.0.1:%d  (/apply, /answer, /learn)" % PORT)
    print("Leave this window open.")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
