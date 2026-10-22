"""apply.classify never lets the model pick from the whole intent bank (2026-10-12, Grok seq 213):
zero keyword hits or an invalid model pick -> pause. Genre rules for definition/example questions."""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apply, essay

INTENTS = {
    "alpha": {"type": "static", "keywords": ["cloud platform"], "answer": "a"},
    "beta":  {"type": "static", "keywords": ["cloud tools"], "answer": "b"},
    "gamma": {"type": "static", "keywords": ["career goals"], "answer": "g"},
}
TOIL = "What does it mean to reduce toil? Share a specific example and the impact it had"
NINES = "In context of SRE, what would you define at \"5 9's\"?"


class ClassifyPause(unittest.TestCase):
    def _run(self, q, reply):
        calls = []
        def fake(prompt, *a, **k):
            calls.append(prompt); return reply
        with mock.patch.object(apply, "ollama_up", return_value=True), \
             mock.patch.object(apply, "ollama", side_effect=fake):
            return apply.classify(q, INTENTS, use_llm=True), calls

    def test_zero_hits_pauses_without_model(self):
        (intent, _, how), calls = self._run(TOIL, "gamma")
        self.assertEqual(intent, "pause")
        self.assertEqual(calls, [])

    def test_invalid_pick_pauses(self):
        (intent, _, _), _ = self._run("Tell us about your cloud platform and cloud tools.", "nonsense_id")
        self.assertEqual(intent, "pause")

    def test_pick_outside_shortlist_pauses(self):
        (intent, _, _), _ = self._run("Tell us about your cloud platform and cloud tools.", "gamma")
        self.assertEqual(intent, "pause")

    def test_tie_break_among_scored(self):
        (intent, _, how), calls = self._run("Tell us about your cloud platform and cloud tools.", "beta")
        self.assertEqual((intent, how), ("beta", "llm"))
        self.assertNotIn("gamma", calls[0])

    def test_live_bank_zero_hits(self):
        bank = apply.load("answers.yaml")["intents"]
        for q in (TOIL, NINES):
            self.assertEqual(apply.score_intents(q, bank), [], q)


PROD_YEARS = "How many years of hands-on production experience do you have with AWS and Azure?"


class WholeWordKeywords(unittest.TestCase):
    def _hits(self, q):
        bank = apply.load("answers.yaml")["intents"]
        return [i for _, i in apply.score_intents(q, bank)]

    def test_product_does_not_hit_production_years(self):
        self.assertNotIn("cross_team_collab", self._hits(PROD_YEARS))

    def test_product_as_its_own_word_still_scores(self):
        self.assertIn("cross_team_collab", self._hits("How do you work with the product team?"))

    def test_k8s_still_scores(self):
        self.assertIn("kubernetes", self._hits("Do you have k8s experience?"))

    def test_multiword_phrase_still_scores(self):
        self.assertIn("cross_team_collab", self._hits("Tell us about operational friction you reduced."))


class DefinitionGenres(unittest.TestCase):
    def g(self, q):
        return essay.classify_genre(q, essay.parse_constraints(q, None))

    def test_toil_is_story(self):
        self.assertEqual(self.g(TOIL), "owned_project")

    def test_nines_is_definition(self):
        self.assertEqual(self.g(NINES), "definition")

    def test_logistics_define_never_definition(self):
        with mock.patch.object(apply, "ollama_up", return_value=False):
            for q in ("Define your availability.", "Please define your preferred schedule.",
                      "Define your start date."):
                self.assertNotEqual(self.g(q), "definition", q)


if __name__ == "__main__":
    unittest.main()
