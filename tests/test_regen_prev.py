"""apply.ollama puts seed on the body only when passed.
serve.do_answer passes regen_prev only on a fresh non-bulk fill, at both
answer_essay call sites. Bulk never receives prev. No Ollama required.
"""
import json, os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apply, serve

TOIL = "What does it mean to reduce toil? Share a specific example and the impact it had"
OTHER = "Zebra quantum marmalade?"


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()


def _capture_body(**kw):
    captured = {}

    def fake_urlopen(req, timeout=120):
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        text = apply.ollama("hi", **kw)
    return text, captured["body"]


class OllamaSeed(unittest.TestCase):
    def test_seed_absent_unless_passed(self):
        text, body = _capture_body()
        self.assertEqual(text, "ok")
        self.assertNotIn("seed", body)
        self.assertNotIn("options", body)
        self.assertEqual(body["temperature"], 0.2)
        self.assertFalse(body["stream"])

    def test_seed_on_body_and_options(self):
        _, body = _capture_body(seed=7)
        self.assertEqual(body["seed"], 7)
        self.assertEqual(body["options"], {"seed": 7})

    def test_seed_zero_is_passed(self):
        _, body = _capture_body(seed=0)
        self.assertEqual(body["seed"], 0)
        self.assertEqual(body["options"], {"seed": 0})


class RegenPrev(unittest.TestCase):
    def _run(self, payload):
        calls = []

        def fake(*a, **k):
            calls.append(k)
            return {"ok": True, "kind": "answer", "method": "essay", "chars": 1,
                    "gaps": [], "text": "draft"}

        orig_imp = serve.imported_lookup
        orig_learn = serve.learned_lookup
        serve.imported_lookup = lambda q: None
        serve.learned_lookup = lambda q: None
        try:
            with mock.patch.object(apply, "ollama_up", return_value=False), \
                 mock.patch("essay.answer_essay", fake):
                out = serve.do_answer(payload)
        finally:
            serve.imported_lookup = orig_imp
            serve.learned_lookup = orig_learn
        self.assertEqual(len(calls), 1, payload)
        self.assertEqual(out.get("text"), "draft")
        return calls[0]

    def test_tier15_fresh_non_bulk_caps_prev(self):
        kw = self._run({"question": TOIL, "fresh": True, "prev": "P" * 700})
        self.assertEqual(kw["regen_prev"], "P" * 600)
        self.assertTrue(kw["why_hybrid"])

    def test_tier15_bulk_and_not_fresh_pass_none(self):
        prev = "previous draft"
        bulk = self._run({"question": TOIL, "fresh": True, "bulk": True, "prev": prev})
        self.assertIsNone(bulk["regen_prev"])
        self.assertFalse(bulk["why_hybrid"])
        stale = self._run({"question": TOIL, "prev": prev})
        self.assertIsNone(stale["regen_prev"])
        self.assertFalse(stale["why_hybrid"])

    def test_tier15_empty_prev_is_none(self):
        kw = self._run({"question": TOIL, "fresh": True, "prev": ""})
        self.assertIsNone(kw["regen_prev"])

    def test_tier3_same_contract(self):
        fresh = self._run({"question": OTHER, "fresh": True, "prev": "Q" * 50})
        self.assertEqual(fresh["regen_prev"], "Q" * 50)
        bulk = self._run({"question": OTHER, "fresh": True, "bulk": True, "prev": "Q" * 50})
        self.assertIsNone(bulk["regen_prev"])
        stale = self._run({"question": OTHER, "prev": "Q" * 50})
        self.assertIsNone(stale["regen_prev"])


if __name__ == "__main__":
    unittest.main()
