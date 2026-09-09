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
import os, sys, re, json, time, subprocess, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
LEARN_PATH = os.path.join(HERE, "data", "learned_answers.json")

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

def do_answer(payload):
    """Tiered. Imported lazily so a broken import can't stop the server.
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

    # tier 2: intent template
    try:
        r = _apply.answer(question=q, max_chars=limit)
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

    # tier 3: essay compose (gap-guarded)
    return _shape(_apply.compose(q, max_chars=limit))

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
        if parsed.path == "/apply":
            url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
            if not url:
                return self._send(400, b"missing url")
            print("[launch] " + url, flush=True)
            try:
                launch(url); return self._send(200, b"launched")
            except Exception as e:
                print("[error] " + str(e), flush=True); return self._send(500, str(e))
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
