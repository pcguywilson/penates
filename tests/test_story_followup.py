"""Pass 2: story followup validation and single-answer question mode.

No network and no write to the real stories.yaml. Model calls are stubbed.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

_here = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_here)
sys.path.insert(0, ROOT)

import serve
import stories_store

Q = "Tell us about a time you handled a production incident"
_STAR = {
    "title": "Night page",
    "situation": "Down.",
    "task": "Fix.",
    "action": "Restarted.",
    "result": "Up.",
    "hero": "Restored service",
}


class _TempBank(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real = os.path.join(ROOT, "stories.yaml")
        with open(self.real, "rb") as fh:
            self.real_bytes = fh.read()
        self.prev = stories_store.PATH
        self.path = os.path.join(self.tmp.name, "stories.yaml")
        stories_store.PATH = self.path

    def tearDown(self):
        stories_store.PATH = self.prev
        self.tmp.cleanup()
        with open(self.real, "rb") as fh:
            self.assertEqual(fh.read(), self.real_bytes)

    def _post(self, base, path, body):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            finally:
                e.close()


class StoreTests(_TempBank):
    def test_create_only_does_not_clobber(self):
        first = stories_store.upsert(dict(_STAR, id="night-page"))
        self.assertEqual(first["title"], "Night page")
        with self.assertRaises(stories_store.StoryExists) as ctx:
            stories_store.upsert(dict(_STAR, id="night-page", title="Hijack",
                                      create_only=True, source="followup"))
        self.assertEqual(ctx.exception.sid, "night-page")
        kept = stories_store.load()
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["title"], "Night page")
        self.assertNotIn("source", kept[0])

    def test_create_only_insert_keeps_source_and_editor_still_replaces(self):
        made = stories_store.upsert(dict(_STAR, id="night-page", create_only=True,
                                         source="followup"))
        self.assertEqual(made["source"], "followup")
        edited = stories_store.upsert(dict(_STAR, id="night-page", title="Edited",
                                           not_implied=["HHS"]))
        self.assertEqual(edited["title"], "Edited")
        self.assertEqual(edited["not_implied"], ["HHS"])
        self.assertNotIn("source", edited)
        self.assertEqual(stories_store.load()[0]["title"], "Edited")

    def test_bak_and_round_trip(self):
        stories_store.upsert(dict(_STAR, id="night-page"))
        self.assertFalse(os.path.exists(self.path + ".bak"))
        stories_store.upsert(dict(_STAR, id="night-page", title="Second"))
        self.assertTrue(os.path.exists(self.path + ".bak"))
        import yaml
        with open(self.path + ".bak", encoding="utf-8") as f:
            bak = yaml.safe_load(f)
        self.assertEqual(bak["stories"][0]["title"], "Night page")
        with open(self.path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        self.assertEqual(doc["stories"][0]["title"], "Second")
        self.assertEqual(stories_store.load()[0]["id"], "night-page")


class FollowupShape(_TempBank):
    def _story(self):
        return {
            "id": "night-page",
            "title": "Pager at HHS (NIH)",
            "kind": "function",
            "approach_only": True,
            "challenges": "do not keep",
            "not_implied": ["Salesforce"],
            "hero": "Cleared the VA queue",
            "domains": ["sre", "HHS", "linux", "a", "b", "c", "d", "e", "f", "g"],
            "tools": ["Terraform", "DoD"],
            "star": {
                "situation": "The HHS indexer was down.",
                "task": "Bring NIH reporting back.",
                "action": "I rebuilt the DoD certs.",
                "result": "Agents reported to the FBI queue.",
            },
        }

    def test_caps_scrub_and_unique_id(self):
        stories_store.upsert(dict(_STAR, id="night-page"))
        draft = {"answer": "I owned the page.", "story": self._story()}
        with mock.patch("essay.cloud_essays_on", return_value=True), \
             mock.patch("essay.cloud_followup", return_value=draft) as call:
            code, obj = serve.story_followup({
                "question": Q,
                "answers": [{"q": "Which team?", "a": "Four people."}, {"q": "x", "a": "  "}],
                "company": "Acme", "role": "SRE", "url": "https://jobs.example/x",
                "page_context": "ctx", "limit": 400,
            })
        self.assertEqual(code, 200)
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["answer"], "I owned the page.")
        self.assertNotIn("error", obj)
        story = obj["story"]
        self.assertEqual(story["id"], "night-page-2")
        self.assertEqual(story["title"], "Pager at")
        self.assertEqual(story["hero"], "Cleared the queue")
        self.assertEqual(story["domains"], ["sre", "linux", "a", "b", "c", "d", "e", "f"])
        self.assertEqual(story["tools"], ["Terraform"])
        self.assertEqual(story["star"]["situation"], "The indexer was down.")
        self.assertNotIn("NIH", story["star"]["task"])
        self.assertNotIn("DoD", story["star"]["action"])
        self.assertNotIn("FBI", story["star"]["result"])
        for dropped in ("kind", "approach_only", "challenges", "not_implied", "source"):
            self.assertNotIn(dropped, story)
        self.assertEqual(set(story), {"id", "title", "hero", "domains", "tools", "star"})
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        # followup did not replace the seeded story
        self.assertEqual(stories_store.load()[0]["title"], "Night page")
        self.assertEqual(call.call_args.kwargs["limit"], 400)
        self.assertEqual(call.call_args.args[1], [{"q": "Which team?", "a": "Four people."}])

    def test_incomplete_story_is_null_and_cloud_off_is_400(self):
        draft = {"answer": "Just the answer.", "story": {"title": "No star"}}
        with mock.patch("essay.cloud_essays_on", return_value=True), \
             mock.patch("essay.cloud_followup", return_value=draft):
            code, obj = serve.story_followup({"question": Q, "answers": [{"q": "q", "a": "a"}]})
        self.assertEqual(code, 200)
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["answer"], "Just the answer.")
        self.assertIsNone(obj["story"])
        self.assertFalse(os.path.exists(self.path))
        with mock.patch("essay.cloud_essays_on", return_value=False), \
             mock.patch("essay.cloud_followup", side_effect=AssertionError("called")):
            code, obj = serve.story_followup({"question": Q, "answers": [{"q": "q", "a": "a"}]})
        self.assertEqual(code, 400)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"], "cloud essays are off")
        self.assertEqual(obj["answer"], "")
        self.assertIsNone(obj["story"])

    def test_no_draft(self):
        with mock.patch("essay.cloud_essays_on", return_value=True), \
             mock.patch("essay.cloud_followup", return_value=None):
            code, obj = serve.story_followup({"question": Q, "answers": [{"q": "q", "a": "a"}]})
        self.assertEqual(code, 200)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["error"], "no draft")
        self.assertEqual(obj["answer"], "")
        self.assertIsNone(obj["story"])


class AskQuestions(_TempBank):
    def _run(self, payload, cloud_on=True, cloud_return=None, learned=None):
        seen = {}

        def fake_cloud(*args, **kwargs):
            seen["ask"] = kwargs.get("ask_questions")
            seen["called"] = True
            return cloud_return

        def fake_bank(*args, **kwargs):
            seen["bank"] = True
            return {"kind": "answer", "method": "template", "text": "KEYWORD", "chars": 7, "gaps": []}

        def fake_local(*args, **kwargs):
            seen["local"] = True
            return {"ok": True, "kind": "answer", "method": "essay:local", "text": "LOCAL",
                    "chars": 5, "gaps": []}

        with mock.patch.object(serve, "_tier0_result", return_value=None), \
             mock.patch.object(serve, "learned_lookup", return_value=learned), \
             mock.patch("apply.match_field", return_value=(None, "no-field")), \
             mock.patch("essay.cloud_essays_on", return_value=cloud_on), \
             mock.patch("essay.cloud_essay", side_effect=fake_cloud), \
             mock.patch("essay.answer_essay", side_effect=fake_local), \
             mock.patch("apply.answer", side_effect=fake_bank), \
             mock.patch("essay._a.ollama", side_effect=AssertionError("network")):
            out = serve.do_answer(payload)
        return out, seen

    def test_questions_do_not_become_a_keyword_answer(self):
        qobj = {
            "ok": False, "kind": "review", "method": "essay:cloud-questions",
            "text": "", "review": True, "questions": ["Which team?"], "why": "thin",
            "chars": 0, "gaps": [],
        }
        out, seen = self._run(
            {"question": Q, "kind": "textarea", "bulk": False, "fresh": True},
            cloud_return=qobj)
        self.assertEqual(out["method"], "essay:cloud-questions")
        self.assertFalse(out["ok"])
        self.assertEqual(out["text"], "")
        self.assertEqual(out["questions"], ["Which team?"])
        self.assertEqual(out["why"], "thin")
        self.assertTrue(out["review"])
        self.assertEqual(out["kind"], "textarea")
        self.assertTrue(seen.get("ask"))
        self.assertNotIn("bank", seen)
        self.assertNotIn("local", seen)
        rec = serve._contract(out, {"question": Q, "kind": "textarea"}, 12)
        self.assertEqual(rec["questions"], ["Which team?"])
        self.assertEqual(rec["why"], "thin")
        self.assertEqual(rec["kind"], "textarea")
        self.assertEqual(rec["text"], "")
        self.assertFalse(rec["ok"])

    def test_bulk_never_asks_and_learned_wins(self):
        answer = {"ok": True, "kind": "answer", "method": "essay:cloud", "text": "Draft.",
                  "chars": 6, "gaps": [], "review": True}
        out, seen = self._run(
            {"question": Q, "kind": "textarea", "bulk": True, "fresh": True},
            cloud_return=answer)
        self.assertFalse(seen.get("ask"))
        self.assertEqual(out["text"], "Draft.")
        self.assertNotIn("bank", seen)
        out, seen = self._run(
            {"question": Q, "bulk": False, "fresh": True},
            cloud_return=answer)
        self.assertTrue(seen.get("ask"))
        out, seen = self._run(
            {"question": Q, "kind": "textarea", "fresh": False},
            cloud_on=True, learned="vetted line")
        self.assertEqual(out["method"], "learned")
        self.assertEqual(out["text"], "vetted line")
        self.assertNotIn("called", seen)
        out, seen = self._run(
            {"question": Q, "kind": "textarea", "bulk": False, "fresh": True},
            cloud_on=False, cloud_return=answer)
        self.assertNotIn("called", seen)
        self.assertNotEqual(out.get("text"), "KEYWORD")


class HttpShapes(_TempBank):
    def setUp(self):
        super().setUp()
        self.httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.H)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def test_create_only_409_and_followup_route(self):
        code, made = self._post(self.base, "/api/stories", dict(_STAR, id="night-page"))
        self.assertEqual(code, 200)
        self.assertTrue(made["ok"])
        code, clash = self._post(self.base, "/api/stories", dict(
            _STAR, id="night-page", title="Hijack", create_only=True, source="followup"))
        self.assertEqual(code, 409)
        self.assertEqual(clash, {"ok": False, "error": "story id exists", "id": "night-page"})
        self.assertEqual(stories_store.load()[0]["title"], "Night page")
        code, made = self._post(self.base, "/api/stories", dict(
            _STAR, id="from-card", create_only=True, source="followup"))
        self.assertEqual(code, 200)
        self.assertEqual(made["ok"], True)
        self.assertEqual(made["story"]["id"], "from-card")
        self.assertEqual(made["story"]["source"], "followup")
        self.assertEqual(made["story"]["star"]["action"], "Restarted.")
        draft = {"answer": "From the route.", "story": None}
        with mock.patch("essay.cloud_essays_on", return_value=True), \
             mock.patch("essay.cloud_followup", return_value=draft):
            code, obj = self._post(self.base, "/api/story/followup", {
                "question": Q, "answers": [{"q": "Which team?", "a": "Four people."}]})
        self.assertEqual(code, 200)
        self.assertEqual(obj, {"ok": True, "answer": "From the route.", "story": None})
        with mock.patch("essay.cloud_essays_on", return_value=False):
            code, obj = self._post(self.base, "/api/story/followup", {
                "question": Q, "answers": [{"q": "q", "a": "a"}]})
        self.assertEqual(code, 400)
        self.assertEqual(obj["error"], "cloud essays are off")
        self.assertEqual(obj["answer"], "")
        self.assertIsNone(obj["story"])


if __name__ == "__main__":
    unittest.main()
