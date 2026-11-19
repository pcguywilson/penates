"""Cloud essay question mode + followup (no network: apply.ollama mocked)."""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import essay

Q = "What makes you excited about this role?"


class AskQuestions(unittest.TestCase):
    def run_with(self, reply, ask=True):
        with mock.patch.object(essay._a, "ollama", return_value=reply) as m, \
             mock.patch.object(essay._jc, "resolve_job_text", return_value=("posting", "page")):
            return essay.cloud_essay(Q, ask_questions=ask), m

    def test_default_off_is_plain_answer(self):
        out, m = self.run_with("I like owning systems end to end.", ask=False)
        self.assertEqual(out["method"], "essay:cloud")
        self.assertNotIn("JSON", m.call_args[0][1])

    def test_questions_object(self):
        out, _ = self.run_with('```json\n{"questions": ["Which team?", "What scale?", "When?", "extra"], "why": "thin"}\n```')
        self.assertEqual(out["method"], "essay:cloud-questions")
        self.assertFalse(out["ok"]); self.assertEqual(out["text"], "")
        self.assertEqual(out["questions"], ["Which team?", "What scale?", "When?"])

    def test_answer_object(self):
        out, _ = self.run_with('{"answer": "Reliability is the product here."}')
        self.assertEqual(out["method"], "essay:cloud"); self.assertEqual(out["text"], "Reliability is the product here.")

    def test_prose_is_answer(self):
        out, _ = self.run_with("Plain prose answer.")
        self.assertEqual(out["text"], "Plain prose answer.")

    def test_broken_json_retries_plain(self):
        with mock.patch.object(essay._a, "ollama", side_effect=['{"answer": "unterminated', "Retry answer."]), \
             mock.patch.object(essay._jc, "resolve_job_text", return_value=("posting", "page")):
            out = essay.cloud_essay(Q, ask_questions=True)
        self.assertEqual(out["text"], "Retry answer.")


class Followup(unittest.TestCase):
    def test_answer_and_story(self):
        reply = ('{"answer": "I am excited about owning uptime.", "story": {"title": "T", "domains": ["sre"], '
                 '"tools": ["Terraform"], "star": {"situation": "s", "task": "t", "action": "a", "result": "r"}, "hero": "h"}}')
        with mock.patch.object(essay._a, "ollama", return_value=reply), \
             mock.patch.object(essay._jc, "resolve_job_text", return_value=("posting", "page")):
            out = essay.cloud_followup(Q, [{"q": "Which team?", "a": "A four person SRE team."}])
        self.assertEqual(out["answer"], "I am excited about owning uptime.")
        self.assertEqual(out["story"]["title"], "T")

    def test_no_typed_answers(self):
        self.assertIsNone(essay.cloud_followup(Q, [{"q": "x", "a": "  "}]))


if __name__ == "__main__":
    unittest.main()
