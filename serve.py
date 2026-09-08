#!/usr/bin/env python3
"""Local server for the apply tool. Listens on 127.0.0.1:8765.

Two jobs:
  /apply?url=...   -> launches run_url.py --attach <url> (the CDP full-fill path)
  /answer  (POST)  -> JSON {question, limit?, company?, url?} -> grounded essay
                      answer from apply.compose(). No browser, no CDP. This is the
                      path the "Essay Local AI" extension uses next to Simplify.

Leave this window running in the background.
"""
import os, sys, json, subprocess, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765

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
    """Tiered, mirroring apply.py's own design. Imported lazily so a broken
    import can't stop the server from starting.
      1. structured/identity/salary  -> match_field (fields.yaml -> profile.yaml)
      2. intent template             -> answer(question=...)  (why_company etc)
      3. open-ended essay            -> compose()             (grounded LLM + gap guard)
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

    # tier 1: deterministic structured field (salary, country, links, yes/no...)
    try:
        prof = _apply.load("profile.yaml"); fields = _apply.load("fields.yaml")
        val, how = _apply.match_field(q, prof, fields, required=True)
        if how == "field" and val not in (None, ""):
            return _shape({"kind": "field", "method": "field", "chars": len(str(val)),
                           "gaps": [], "text": str(val)})
    except Exception as e:
        print("[tier1 skip] " + str(e), flush=True)

    # tier 2: intent template
    try:
        r = _apply.answer(question=q, max_chars=limit)
        if r.get("kind") == "answer":
            return _shape(r)
    except Exception as e:
        print("[tier2 skip] " + str(e), flush=True)

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
        if parsed.path == "/answer":
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                return self._send(400, json.dumps({"ok": False, "text": "[ERROR] bad json"}), "application/json")
            q = (payload.get("question") or "")[:600]
            print("[answer] " + q.replace("\n", " ")[:120], flush=True)
            try:
                out = do_answer(payload)
                print("       -> %s / %s chars / gaps=%s" % (out.get("method"), out.get("chars"), out.get("gaps")), flush=True)
                return self._send(200, json.dumps(out), "application/json")
            except Exception as e:
                print("[error] " + str(e), flush=True)
                return self._send(500, json.dumps({"ok": False, "text": "[ERROR] " + str(e)}), "application/json")
        return self._send(404, b"not found")
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print("Apply autofill server on http://127.0.0.1:%d  (/apply launch, /answer essay)" % PORT)
    print("Leave this window open.")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
