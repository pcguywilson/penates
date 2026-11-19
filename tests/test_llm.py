"""Offline router tests. No live cloud calls. Real config.json and secrets.yaml stay untouched."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
import urllib.error
import urllib.request
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apply
import llm
import serve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XAI_KEY = "xai-test-key-abcdef"
ANT_KEY = "ant-test-key-12345678"


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def openai(text):
    return {"choices": [{"message": {"content": text}}]}


def anthropic(text):
    return {"content": [{"text": text}]}


def http(method, url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        code = exc.code
        exc.close()
        return code, json.loads(raw) if raw else {}


class LlmRouter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._files = {}
        for name in ("config.json", "secrets.yaml"):
            path = os.path.join(ROOT, name)
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    cls._files[path] = fh.read()
            else:
                cls._files[path] = None

    @classmethod
    def tearDownClass(cls):
        for path, before in cls._files.items():
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    after = fh.read()
            else:
                after = None
            if after != before:
                raise AssertionError("test wrote " + os.path.basename(path))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sec = os.path.join(self.tmp.name, "secrets.yaml")
        self._model = apply.MODEL
        self._essay = apply.ESSAY_MODEL
        self._meta = dict(llm.last_meta)
        self._env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "XAI_API_KEY": ""})
        self._env.start()
        self._sec = mock.patch("llm.secrets_path", return_value=self.sec)
        self._sec.start()
        self._cfg = mock.patch("jobs_store.load_config", return_value={})
        self._cfg.start()
        self._save = mock.patch("jobs_store.save_config", side_effect=AssertionError("refusing to write config"))
        self._save.start()
        self._cache = mock.patch.object(serve, "_clear_answer_cache")
        self._cache.start()

    def tearDown(self):
        apply.MODEL = self._model
        apply.ESSAY_MODEL = self._essay
        llm.last_meta = self._meta
        self._cache.stop()
        self._save.stop()
        self._cfg.stop()
        self._sec.stop()
        self._env.stop()
        self.tmp.cleanup()

    def _use_config(self, cfg):
        box = {"cfg": json.loads(json.dumps(cfg))}

        def load():
            return json.loads(json.dumps(box["cfg"]))

        def save(new_cfg):
            box["cfg"] = new_cfg

        return box, mock.patch("jobs_store.load_config", side_effect=load), mock.patch("jobs_store.save_config", side_effect=save)

    def test_missing_block_synthesizes_ollama(self):
        with mock.patch("jobs_store.load_config", return_value={
            "terms": ["devops"], "ollama_model": "m-ans", "ollama_essay_model": "m-ess",
        }):
            view = llm.public_view()
        self.assertEqual(view["answers"], {"provider": "ollama", "model": "m-ans"})
        self.assertEqual(view["essays"], {"provider": "ollama", "model": "m-ess"})
        self.assertIs(view["fallback_local"], False)
        self.assertEqual(view["key_set"], {"anthropic": False, "xai": False})
        self.assertEqual(set(view), {"ok", "answers", "essays", "fallback_local", "key_set", "cli_found"})
        self.assertEqual(set(view["cli_found"]), {"claude", "grok"})

    def test_apply_ollama_delegates(self):
        with mock.patch.object(llm, "complete", return_value="ok") as complete:
            text = apply.ollama("p", system="s", temperature=0.4, model="m", seed=3)
        self.assertEqual(text, "ok")
        complete.assert_called_once_with("p", system="s", temperature=0.4, model="m", seed=3)

    def test_ollama_body_keeps_seed_and_timeout(self):
        seen = {}

        def fake(req, timeout=120):
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            seen["body"] = json.loads(req.data.decode())
            return Resp(openai("  ok  "))

        with mock.patch("urllib.request.urlopen", fake):
            text = apply.ollama("hi", seed=7)
        self.assertEqual(text, "ok")
        self.assertEqual(seen["timeout"], 120)
        self.assertEqual(seen["url"], apply.OLLAMA_BASE)
        self.assertEqual(seen["body"]["seed"], 7)
        self.assertEqual(seen["body"]["options"], {"seed": 7})
        self.assertEqual(seen["body"]["stream"], False)
        self.assertEqual(llm.last_meta["provider"], "ollama")
        self.assertIs(llm.last_meta["fell_back"], False)

    def test_task_inference_and_cloud_model_id(self):
        apply.MODEL = "local-a"
        apply.ESSAY_MODEL = "local-e"
        cfg = {"llm": {
            "answers": {"provider": "xai", "model": "grok-a"},
            "essays": {"provider": "anthropic", "model": "claude-e"},
            "fallback_local": False,
        }, "ollama_model": "local-a", "ollama_essay_model": "local-e"}
        os.environ["XAI_API_KEY"] = XAI_KEY
        os.environ["ANTHROPIC_API_KEY"] = ANT_KEY
        seen = []

        def fake(req, timeout=120):
            seen.append({
                "url": req.full_url,
                "timeout": timeout,
                "body": json.loads(req.data.decode()),
                "auth": req.get_header("Authorization"),
                "xkey": req.get_header("X-api-key"),
                "version": req.get_header("Anthropic-version"),
            })
            if "anthropic" in req.full_url:
                return Resp(anthropic("essay-text"))
            return Resp(openai("answer-text"))

        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("urllib.request.urlopen", fake):
            answers = llm.complete("q", model=None, seed=9)
            essays = llm.complete("q", system="SYS", model="local-e", seed=9, temperature=0.3)
            explicit = llm.complete("q", task="answers", model="local-a", seed=4)
        self.assertEqual(answers, "answer-text")
        self.assertEqual(essays, "essay-text")
        self.assertEqual(explicit, "answer-text")
        self.assertEqual(seen[0]["url"], llm.XAI_URL)
        self.assertEqual(seen[0]["body"]["model"], "grok-a")
        self.assertNotIn("seed", seen[0]["body"])
        self.assertNotIn("options", seen[0]["body"])
        self.assertEqual(seen[0]["auth"], "Bearer " + XAI_KEY)
        self.assertEqual(seen[0]["timeout"], 120)
        self.assertEqual(seen[1]["url"], llm.ANTHROPIC_URL)
        self.assertEqual(seen[1]["body"]["model"], "claude-e")
        self.assertEqual(seen[1]["body"]["system"], "SYS")
        self.assertEqual(seen[1]["body"]["max_tokens"], 2048)
        self.assertEqual(seen[1]["body"]["messages"], [{"role": "user", "content": "q"}])
        self.assertNotIn("seed", seen[1]["body"])
        self.assertEqual(seen[1]["version"], "2023-06-01")
        self.assertEqual(seen[1]["xkey"], ANT_KEY)
        self.assertEqual(seen[1]["body"]["temperature"], 0.3)
        self.assertEqual(seen[2]["body"]["model"], "grok-a")

    def test_file_key_beats_env(self):
        os.environ["XAI_API_KEY"] = "env-key-should-lose-99"
        with open(self.sec, "w", encoding="utf-8") as f:
            f.write("xai_api_key: %s\n" % XAI_KEY)
        seen = {}

        def fake(req, timeout=120):
            seen["auth"] = req.get_header("Authorization")
            return Resp(openai("ok"))

        cfg = {"llm": {"answers": {"provider": "xai", "model": "grok-a"},
                       "essays": {"provider": "ollama", "model": "local-e"},
                       "fallback_local": False}}
        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("urllib.request.urlopen", fake):
            llm.complete("q", task="answers")
        self.assertEqual(seen["auth"], "Bearer " + XAI_KEY)

    def test_key_never_in_error_text(self):
        os.environ["ANTHROPIC_API_KEY"] = ANT_KEY
        cfg = {"llm": {"answers": {"provider": "anthropic", "model": "claude-e"},
                       "essays": {"provider": "ollama", "model": "local-e"},
                       "fallback_local": False}}

        def fake(req, timeout=120):
            fp = io.BytesIO(('{"error":"%s"}' % ANT_KEY).encode())
            raise urllib.error.HTTPError(req.full_url, 401, "nope " + ANT_KEY, {}, fp)

        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(RuntimeError) as ctx:
                llm.complete("q", task="answers")
        text = str(ctx.exception)
        rendered = "".join(traceback.format_exception(type(ctx.exception), ctx.exception, ctx.exception.__traceback__))
        self.assertNotIn(ANT_KEY, text)
        self.assertNotIn(ANT_KEY, rendered)
        self.assertNotIn(ANT_KEY, repr(ctx.exception))
        self.assertIn("[redacted]", text)
        self.assertIs(llm.last_meta["fell_back"], False)

    def test_fallback_off_does_not_retry(self):
        os.environ["XAI_API_KEY"] = XAI_KEY
        cfg = {"llm": {"answers": {"provider": "xai", "model": "grok-a"},
                       "essays": {"provider": "ollama", "model": "local-e"},
                       "fallback_local": False},
               "ollama_model": "local-a"}
        urls = []

        def fake(req, timeout=120):
            urls.append(req.full_url)
            raise urllib.error.URLError("down")

        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(RuntimeError) as ctx:
                llm.complete("q", task="answers", seed=2)
        self.assertEqual(urls, [llm.XAI_URL])
        self.assertNotIn("local fallback", str(ctx.exception))
        self.assertIs(llm.last_meta["fell_back"], False)

    def test_fallback_on_uses_ollama_model_and_seed(self):
        os.environ["ANTHROPIC_API_KEY"] = ANT_KEY
        cfg = {"llm": {"answers": {"provider": "anthropic", "model": "claude-e"},
                       "essays": {"provider": "anthropic", "model": "claude-e"},
                       "fallback_local": True},
               "ollama_model": "local-a", "ollama_essay_model": "local-e"}
        seen = []

        def fake(req, timeout=120):
            body = json.loads(req.data.decode())
            seen.append((req.full_url, body))
            if "anthropic" in req.full_url:
                raise urllib.error.URLError("down")
            return Resp(openai("from-local"))

        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("urllib.request.urlopen", fake):
            text = llm.complete("q", system="S", task="essays", seed=5)
        self.assertEqual(text, "from-local")
        self.assertEqual(seen[0][0], llm.ANTHROPIC_URL)
        self.assertNotIn("seed", seen[0][1])
        self.assertEqual(seen[1][0], apply.OLLAMA_BASE)
        self.assertEqual(seen[1][1]["model"], "local-e")
        self.assertEqual(seen[1][1]["seed"], 5)
        self.assertEqual(seen[1][1]["options"], {"seed": 5})
        self.assertEqual(llm.last_meta, {"provider": "ollama", "fell_back": True, "model": "local-e"})

    def test_missing_key_does_not_call_network(self):
        cfg = {"llm": {"answers": {"provider": "xai", "model": "grok-a"},
                       "essays": {"provider": "ollama", "model": "local"},
                       "fallback_local": False}}

        def fake(req, timeout=120):
            raise AssertionError("network")

        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(RuntimeError) as ctx:
                llm.complete("q", task="answers")
        self.assertIn("not set", str(ctx.exception))
        self.assertNotIn(XAI_KEY, str(ctx.exception))

    def test_put_merges_config_and_keeps_neighbors(self):
        box, load, save = self._use_config({
            "terms": ["devops"], "sources": {"discover": True}, "ollama_model": "old-a",
        })
        with load, save:
            changed = llm.apply_settings({
                "answers": {"provider": "xai", "model": "grok-2"},
                "fallback_local": True,
            })
            view = llm.public_view()
        self.assertTrue(changed)
        saved = box["cfg"]
        self.assertEqual(saved["terms"], ["devops"])
        self.assertEqual(saved["sources"], {"discover": True})
        self.assertEqual(saved["ollama_model"], "old-a")
        self.assertEqual(saved["llm"]["answers"], {"provider": "xai", "model": "grok-2"})
        self.assertEqual(saved["llm"]["essays"]["provider"], "ollama")
        self.assertIs(saved["llm"]["fallback_local"], True)
        self.assertNotIn("api_key", json.dumps(saved))
        self.assertEqual(view["answers"]["model"], "grok-2")
        self.assertIs(view["fallback_local"], True)

    def test_put_ollama_syncs_flat_keys_and_model_globals(self):
        box, load, save = self._use_config({"terms": ["devops"], "ollama_model": "old-a"})
        with load, save:
            code, obj = serve.llm_put({"answers": {"provider": "ollama", "model": "new-a"},
                                       "essays": {"provider": "ollama", "model": "new-e"}})
        self.assertEqual(code, 200)
        self.assertEqual(box["cfg"]["ollama_model"], "new-a")
        self.assertEqual(box["cfg"]["ollama_essay_model"], "new-e")
        self.assertEqual(box["cfg"]["terms"], ["devops"])
        self.assertEqual(apply.MODEL, "new-a")
        self.assertEqual(apply.ESSAY_MODEL, "new-e")
        self.assertEqual(obj["answers"]["model"], "new-a")
        self.assertNotIn("anthropic_api_key", obj)

    def test_secrets_merge_keeps_workday(self):
        with open(self.sec, "w", encoding="utf-8") as f:
            f.write("# keep\nworkday:\n  email: keep@example.com\n  password: \"change-me\"\n"
                    "icims:\n  user: someone\n")
        with open(self.sec, encoding="utf-8") as fh:
            before = fh.read()
        llm.apply_settings({"xai_api_key": ""})
        with open(self.sec, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)
        llm.apply_settings({"xai_api_key": XAI_KEY, "anthropic_api_key": ANT_KEY})
        with open(self.sec, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# keep", text)
        doc = yaml.safe_load(text)
        self.assertEqual(doc["workday"], {"email": "keep@example.com", "password": "change-me"})
        self.assertEqual(doc["icims"], {"user": "someone"})
        self.assertEqual(doc["xai_api_key"], XAI_KEY)
        self.assertEqual(doc["anthropic_api_key"], ANT_KEY)
        llm.apply_settings({"xai_api_key": "   "})
        with open(self.sec, encoding="utf-8") as fh:
            self.assertEqual(yaml.safe_load(fh)["xai_api_key"], XAI_KEY)
        llm.apply_settings({"xai_api_key": None})
        with open(self.sec, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        self.assertIsNone(doc.get("xai_api_key"))
        self.assertEqual(doc["workday"]["email"], "keep@example.com")
        self.assertEqual(doc["anthropic_api_key"], ANT_KEY)
        self.assertEqual(llm.api_key("xai"), "")
        self.assertTrue(llm.key_set("anthropic"))
        view = llm.public_view()
        blob = json.dumps(view)
        self.assertNotIn(XAI_KEY, blob)
        self.assertNotIn(ANT_KEY, blob)
        self.assertIs(view["key_set"]["anthropic"], True)
        self.assertIs(view["key_set"]["xai"], False)

    def test_put_response_does_not_echo_key(self):
        box, load, save = self._use_config({"terms": ["devops"]})
        with load, save:
            code, obj = serve.llm_put({
                "fallback_local": False,
                "anthropic_api_key": ANT_KEY,
            })
        self.assertEqual(code, 200)
        blob = json.dumps(obj)
        self.assertNotIn(ANT_KEY, blob)
        self.assertNotIn("anthropic_api_key", blob)
        self.assertIs(obj["key_set"]["anthropic"], True)
        self.assertEqual(box["cfg"]["terms"], ["devops"])
        self.assertNotIn("anthropic_api_key", json.dumps(box["cfg"]))

    def test_bad_provider_rejected(self):
        with self.assertRaises(llm.SettingsError):
            llm.apply_settings({"answers": {"provider": "openai", "model": "gpt"}})

    def test_ollama_set_updates_ollama_tier_only(self):
        box, load, save = self._use_config({
            "terms": ["devops"],
            "llm": {"answers": {"provider": "ollama", "model": "old"},
                    "essays": {"provider": "xai", "model": "grok-e"},
                    "fallback_local": False},
        })
        with load, save, mock.patch.object(serve, "_ollama_models", return_value=["new-model", "old"]):
            code, obj = serve.ollama_set({"model": "new-model"})
        self.assertEqual(code, 200)
        self.assertEqual(box["cfg"]["ollama_model"], "new-model")
        self.assertEqual(box["cfg"]["llm"]["answers"]["model"], "new-model")
        self.assertEqual(box["cfg"]["llm"]["essays"]["model"], "grok-e")
        self.assertEqual(box["cfg"]["terms"], ["devops"])
        self.assertEqual(obj["model"], "new-model")

    def test_llm_test_shape(self):
        def fake(req, timeout=120):
            return Resp(openai("OK"))

        with mock.patch("urllib.request.urlopen", fake):
            got = serve.llm_test("answers")
        self.assertEqual(set(got), {"ok", "provider", "model", "ms", "error"})
        self.assertTrue(got["ok"])
        self.assertEqual(got["provider"], "ollama")
        self.assertIsNone(got["error"])
        self.assertIsInstance(got["ms"], int)
        bad = serve.llm_test("nope")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "task must be answers or essays")

    def test_http_routes(self):
        box, load, save = self._use_config({
            "terms": ["devops"], "ollama_model": "local-a", "ollama_essay_model": "local-e",
        })

        httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.H)
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        real_urlopen = urllib.request.urlopen

        def fake(req, timeout=120):
            url = getattr(req, "full_url", "")
            if isinstance(url, str) and url.startswith(base):
                return real_urlopen(req, timeout=timeout)
            return Resp(openai("OK"))
        try:
            with load, save, \
                 mock.patch.object(serve, "_ollama_models", return_value=["local-a", "local-e"]), \
                 mock.patch("urllib.request.urlopen", fake):
                status, got = http("GET", base + "/api/llm")
                self.assertEqual(status, 200)
                self.assertEqual(got["answers"], {"provider": "ollama", "model": "local-a"})
                self.assertEqual(got["key_set"], {"anthropic": False, "xai": False})
                status, got = http("PUT", base + "/api/llm", {
                    "answers": {"provider": "ollama", "model": "local-a"},
                    "fallback_local": False,
                    "xai_api_key": XAI_KEY,
                })
                self.assertEqual(status, 200)
                self.assertNotIn(XAI_KEY, json.dumps(got))
                self.assertIs(got["key_set"]["xai"], True)
                self.assertEqual(box["cfg"]["terms"], ["devops"])
                status, got = http("POST", base + "/api/llm/test", {"task": "answers"})
                self.assertEqual(status, 200)
                self.assertTrue(got["ok"])
                self.assertEqual(got["provider"], "ollama")
                self.assertEqual(got["model"], "local-a")
                self.assertIsNone(got["error"])
                status, bad = http("POST", base + "/api/llm/test", {"task": "other"})
                self.assertEqual(status, 400)
                self.assertFalse(bad["ok"])
                status, oll = http("GET", base + "/api/ollama")
                self.assertEqual(status, 200)
                self.assertEqual(oll["model"], "local-a")
                self.assertEqual(oll["essay_model"], "local-e")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def _cli_cfg(self, provider, model="", fallback=False, cli=None):
        block = {
            "answers": {"provider": provider, "model": model},
            "essays": {"provider": "ollama", "model": "local-e"},
            "fallback_local": fallback,
        }
        if cli:
            block["cli"] = cli
        return {"llm": block, "ollama_model": "local-a", "ollama_essay_model": "local-e"}

    def _which(self, name):
        found = {"claude": r"C:\fake\claude.CMD", "grok": r"C:\fake\grok.EXE",
                 "my-claude": r"C:\fake\my-claude.EXE"}
        return found.get(name)

    def _completed(self, text="OK", err="", code=0):
        return subprocess.CompletedProcess(args=[], returncode=code, stdout=text, stderr=err)

    def test_cli_argv_stdin_cwd_and_ansi(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append((argv, kwargs))
            return self._completed("\x1b[31mOK\x1b[0m\n")

        cfg = self._cli_cfg("claude_cli", model="sonnet")
        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("shutil.which", side_effect=self._which), \
             mock.patch("subprocess.run", fake_run):
            text = llm.complete("Say hi", system="Be brief", temperature=0.9, seed=3, task="answers")
        self.assertEqual(text, "OK")
        argv, kwargs = seen[0]
        self.assertEqual(argv[0], r"C:\fake\claude.CMD")
        self.assertEqual(argv[1:9], [
            "-p", "--output-format", "text", "--tools", "",
            "--permission-mode", "dontAsk", "--permission-prompts",
        ])
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "Be brief")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertNotIn("Say hi", argv)
        self.assertNotIn("--seed", argv)
        self.assertNotIn("0.9", argv)
        self.assertEqual(kwargs.get("input"), "Say hi")
        self.assertIs(kwargs.get("shell"), False)
        self.assertEqual(kwargs.get("timeout"), 180)
        cwd = os.path.abspath(kwargs["cwd"])
        root = os.path.abspath(ROOT)
        self.assertNotEqual(cwd, root)
        self.assertFalse(cwd.startswith(root + os.sep))
        self.assertEqual(llm.last_meta, {"provider": "claude_cli", "fell_back": False, "model": "sonnet"})

    def test_grok_cli_uses_p_arg_not_stdin(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append((argv, kwargs))
            return self._completed("OK")

        cfg = self._cli_cfg("grok_cli", model="")
        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("shutil.which", side_effect=self._which), \
             mock.patch("subprocess.run", fake_run):
            text = llm.complete("Reply with exactly: OK", system="", task="answers")
        self.assertEqual(text, "OK")
        argv, kwargs = seen[0]
        self.assertEqual(argv[:3], [r"C:\fake\grok.EXE", "-p", "Reply with exactly: OK"])
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "plain")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "1")
        self.assertIn("--no-subagents", argv)
        self.assertIn("--disable-web-search", argv)
        self.assertIn("--verbatim", argv)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--system-prompt", argv)
        self.assertIsNone(kwargs.get("input"))
        self.assertIs(kwargs.get("shell"), False)
        cwd = os.path.abspath(kwargs["cwd"])
        root = os.path.abspath(ROOT)
        self.assertNotEqual(cwd, root)
        self.assertFalse(cwd.startswith(root + os.sep))

    def test_cli_timeout_and_missing_binary(self):
        cfg = self._cli_cfg("claude_cli")

        def boom(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=180)

        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("shutil.which", side_effect=self._which), \
             mock.patch("subprocess.run", boom):
            with self.assertRaises(RuntimeError) as ctx:
                llm.complete("q", task="answers")
        self.assertIn("timed out", str(ctx.exception))
        self.assertNotIn("Say hi", str(ctx.exception))
        self.assertIs(llm.last_meta["fell_back"], False)

        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch("subprocess.run", side_effect=AssertionError("ran")):
            with self.assertRaises(RuntimeError) as ctx:
                llm.complete("q", task="answers")
        self.assertIn("not found", str(ctx.exception))
        self.assertIn("claude", str(ctx.exception))

    def test_cli_nonzero_scrubs_secret_and_fallback(self):
        os.environ["ANTHROPIC_API_KEY"] = ANT_KEY
        cfg = self._cli_cfg("grok_cli", model="grok-4", fallback=False)

        def bad(argv, **kwargs):
            return self._completed("", err="fail " + ANT_KEY + " end", code=1)

        with mock.patch("jobs_store.load_config", return_value=cfg), \
             mock.patch("shutil.which", side_effect=self._which), \
             mock.patch("subprocess.run", bad):
            with self.assertRaises(RuntimeError) as ctx:
                llm.complete("q", task="answers", seed=9)
        msg = str(ctx.exception)
        self.assertIn("exited 1", msg)
        self.assertNotIn(ANT_KEY, msg)
        self.assertIn("[redacted]", msg)
        self.assertNotIn("local fallback", msg)

        def stdout_only(argv, **kwargs):
            return self._completed("Failed to authenticate: expired", err="  \n", code=1)

        with mock.patch("jobs_store.load_config", return_value=self._cli_cfg("claude_cli")), \
             mock.patch("shutil.which", side_effect=self._which), \
             mock.patch("subprocess.run", stdout_only):
            with self.assertRaises(RuntimeError) as ctx:
                llm.complete("q", task="answers")
        self.assertIn("Failed to authenticate", str(ctx.exception))

        cfg_fb = self._cli_cfg("claude_cli", fallback=True)
        calls = {"run": 0}

        def down(argv, **kwargs):
            calls["run"] += 1
            raise subprocess.TimeoutExpired(cmd="claude", timeout=180)

        def fake(req, timeout=120):
            body = json.loads(req.data.decode())
            self.assertEqual(body["model"], "local-a")
            self.assertEqual(body["seed"], 4)
            return Resp(openai("from-local"))

        with mock.patch("jobs_store.load_config", return_value=cfg_fb), \
             mock.patch("shutil.which", side_effect=self._which), \
             mock.patch("subprocess.run", down), \
             mock.patch("urllib.request.urlopen", fake):
            text = llm.complete("q", system="S", task="answers", seed=4)
        self.assertEqual(text, "from-local")
        self.assertEqual(calls["run"], 1)
        self.assertEqual(llm.last_meta, {"provider": "ollama", "fell_back": True, "model": "local-a"})

    def test_put_cli_provider_bins_and_found(self):
        box, load, save = self._use_config({"terms": ["devops"], "ollama_model": "old-a"})
        with load, save:
            changed = llm.apply_settings({
                "answers": {"provider": "claude_cli"},
                "cli": {"claude_bin": "my-claude"},
            })
            view = llm.public_view()
        self.assertTrue(changed)
        self.assertEqual(box["cfg"]["terms"], ["devops"])
        self.assertEqual(box["cfg"]["ollama_model"], "old-a")
        self.assertEqual(box["cfg"]["llm"]["answers"], {"provider": "claude_cli", "model": ""})
        self.assertEqual(box["cfg"]["llm"]["cli"], {"claude_bin": "my-claude"})
        self.assertEqual(view["answers"]["provider"], "claude_cli")
        self.assertEqual(set(view["cli_found"]), {"claude", "grok"})
        with load, save, mock.patch("shutil.which", side_effect=self._which):
            view = llm.public_view()
        self.assertIs(view["cli_found"]["claude"], True)
        self.assertIs(view["cli_found"]["grok"], True)
        with load, save:
            llm.apply_settings({"fallback_local": True, "cli": {"grok_bin": "grok"}})
        self.assertEqual(box["cfg"]["llm"]["cli"], {"claude_bin": "my-claude", "grok_bin": "grok"})
        self.assertIs(box["cfg"]["llm"]["fallback_local"], True)
        self.assertEqual(box["cfg"]["llm"]["answers"]["model"], "")
        with self.assertRaises(llm.SettingsError):
            llm.apply_settings({"cli": {"claude_bin": "bad\nname"}})
        with load, save:
            code, obj = serve.llm_put({"essays": {"provider": "grok_cli", "model": "grok-4"}})
        self.assertEqual(code, 200)
        self.assertEqual(obj["essays"]["provider"], "grok_cli")
        self.assertEqual(obj["essays"]["model"], "grok-4")
        self.assertIn("cli_found", obj)
        self.assertNotIn("claude_bin", json.dumps(obj))
        self.assertEqual(box["cfg"]["llm"]["answers"]["provider"], "claude_cli")


if __name__ == "__main__":
    unittest.main()
