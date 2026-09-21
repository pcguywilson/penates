"""Deterministic tests for the answer engine's role-pinning and STAR fallback (build 17).
No Ollama, no network. Run: python -m pytest tests/test_essay.py  (or python tests/test_essay.py)

Covers Grok's Priority-1 spec:
  - current-title-as-role validator: true and false cases
  - why-company fallback names the TARGET role, not the current title
  - resolve_target_role rejects form chrome ("Application") and passes a real title
  - _compose_star contains Action + Result and passes validate (no no_action_verb)
"""
import os, sys, unittest

# import the engine whether this file lives in tests/ (repo root is the parent) or alongside it
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))
sys.path.insert(0, _here)
import essay


class RoleValidator(unittest.TestCase):
    CT = "Network Systems Analyst 4"
    TR = "Senior IT Engineer"

    def test_flags_current_title_as_the_job(self):
        bad = ("I am particularly drawn to the Network Systems Analyst 4 role at 1Password "
               "because it aligns with my hands-on experience in cloud infrastructure.")
        self.assertTrue(essay.why_uses_title_as_role(bad, self.CT, self.TR))

    def test_allows_current_title_as_background(self):
        good = ("At 1Password, we're building a safe digital future. As a Network Systems "
                "Analyst 4 with hands-on cloud and compliance experience, this role is a fit.")
        self.assertFalse(essay.why_uses_title_as_role(good, self.CT, self.TR))

    def test_allows_when_target_role_also_named(self):
        # mentions the current title as a role BUT also names the real target role -> background
        both = ("Coming from a Network Systems Analyst 4 role, I am excited about the Senior IT "
                "Engineer position at 1Password.")
        self.assertFalse(essay.why_uses_title_as_role(both, self.CT, self.TR))

    def test_no_flag_when_applying_to_same_title(self):
        same = "I am excited about the Network Systems Analyst 4 role at Acme."
        self.assertFalse(essay.why_uses_title_as_role(same, self.CT, "Network Systems Analyst 4"))

    def test_no_flag_without_current_title(self):
        self.assertFalse(essay.why_uses_title_as_role("anything at all", "", self.TR))

    def test_validate_surfaces_current_title_as_role(self):
        bad = ("I am drawn to the Network Systems Analyst 4 role at 1Password because I secure "
               "cloud infrastructure and compliance every day.")
        fails = essay.validate(bad, "why_company", {}, {"hard": [], "limited": []},
                               facts=[], current_title=self.CT, target_role=self.TR)
        self.assertIn("current_title_as_role", fails)


class ResolveTargetRole(unittest.TestCase):
    def test_rejects_form_chrome(self):
        # role chrome + no url -> no jobs.json / posting to fall back to -> empty
        self.assertEqual(essay.resolve_target_role("Application", ""), "")
        self.assertEqual(essay.resolve_target_role("Application Questions", ""), "")

    def test_passes_real_title(self):
        self.assertEqual(essay.resolve_target_role("Senior IT Engineer", ""), "Senior IT Engineer")


class WhyFallback(unittest.TestCase):
    # mission/slogan copy - the ONLY fact this fixture's ATS exposes
    FACTS = ["At 1Password, we're building the foundation for a safe, productive digital future"]
    # an operational posting detail (team/seniority/remote) - the kind worth citing
    OP_FACTS = ["This is a senior role on the Infrastructure team, fully remote in the US"]

    def test_template_names_target_role_not_current_title(self):
        out = essay._why_candidate_only(self.FACTS, {"max_chars": 700}, "Senior IT Engineer", "1Password")
        self.assertIn("Senior IT Engineer", out)
        self.assertFalse(essay.why_uses_title_as_role(out, essay._current_title(), "Senior IT Engineer"))

    def test_template_is_candidate_first_not_slogan(self):
        # Grok correction: sentence 1 must be the candidate/role, never the company's About line.
        out = essay._why_candidate_only(self.FACTS, {"max_chars": 700}, "Senior IT Engineer", "1Password")
        first = out.split(".")[0].lower()
        self.assertIn("i'm applying", first, "sentence 1 must be candidate-first, got %r" % first)
        # the marketing slogan must not appear anywhere when it's the only fact
        self.assertNotIn("safe, productive digital future", out.lower())
        self.assertNotIn("building the foundation", out.lower())
        self.assertNotIn("drew me to the company", out.lower())

    def test_marketing_fact_is_dropped(self):
        self.assertFalse(essay._fact_is_operational(essay._fact_phrase(self.FACTS[0], "1Password")))
        self.assertEqual(essay._operational_facts(self.FACTS, "1Password"), [])

    def test_operational_fact_is_kept_and_cited(self):
        self.assertTrue(essay._fact_is_operational(essay._fact_phrase(self.OP_FACTS[0], "1Password")))
        out = essay._why_candidate_only(self.OP_FACTS, {"max_chars": 700}, "Senior IT Engineer", "1Password")
        self.assertIn("posting", out.lower(), "an operational fact should be cited, got %r" % out)
        # still candidate-first
        self.assertIn("i'm applying", out.split(".")[0].lower())

    def test_template_is_clean(self):
        out = essay._why_candidate_only(self.FACTS, {"max_chars": 700}, "Senior IT Engineer", "1Password").lower()
        for bad in ("seamless", "aws govcloud", "you're building", "aligns perfectly", "strong fit", "my focus"):
            self.assertNotIn(bad, out, "template must not contain %r" % bad)

    def test_template_passes_its_own_validators(self):
        out = essay._why_candidate_only(self.FACTS, {"max_chars": 700}, "Senior IT Engineer", "1Password")
        fails = essay.validate(out, "why_company", {}, {"hard": [], "limited": []},
                               facts=self.FACTS, current_title="Network Systems Analyst 4",
                               target_role="Senior IT Engineer")
        self.assertEqual(fails, [], "clean template should pass validate, got %r" % fails)

    def test_template_with_operational_fact_passes_validators(self):
        out = essay._why_candidate_only(self.OP_FACTS, {"max_chars": 700}, "Senior IT Engineer", "1Password")
        fails = essay.validate(out, "why_company", {}, {"hard": [], "limited": []},
                               facts=self.OP_FACTS, current_title="Network Systems Analyst 4",
                               target_role="Senior IT Engineer")
        self.assertEqual(fails, [], "template citing an operational fact should pass, got %r" % fails)


class GarbageRejection(unittest.TestCase):
    # the exact build-17 live draft the user rejected
    GARBAGE = ("At 1Password, you're building the foundation for a safe, productive digital future, "
               "which aligns perfectly with my hands-on experience in cloud infrastructure, "
               "particularly with AWS and AWS GovCloud. My focus on ensuring secure and compliant "
               "environments will help drive best practices and seamless user experiences, making "
               "me a strong fit for this Senior IT Engineer role.")
    FACTS = ["At 1Password, we're building the foundation for a safe, productive digital future"]

    def test_live_garbage_draft_is_rejected(self):
        fails = essay.validate(self.GARBAGE, "why_company", {}, {"hard": [], "limited": []},
                               facts=self.FACTS, current_title="Network Systems Analyst 4",
                               target_role="Senior IT Engineer")
        self.assertTrue(fails, "the garbage draft must NOT validate clean")
        # it fails for the right reasons
        joined = " ".join(fails)
        self.assertTrue("fluff" in joined or "claims_unowned" in joined or "rewrote_fact" in joined)

    def test_individual_gates(self):
        self.assertIsNotNone(essay.why_fluff("this aligns perfectly with my work"))
        self.assertIsNone(essay.why_fluff("the compliance work I do aligns with this role"))  # bare 'aligns' is fine now
        self.assertIsNotNone(essay.why_fluff("resonates with my passion"))
        self.assertIsNotNone(essay.why_fluff("a seamless user experience"))
        self.assertTrue(essay.why_rewrote_fact("you're building a great future", self.FACTS))
        self.assertFalse(essay.why_rewrote_fact("1Password's focus on building the future", self.FACTS))

    def test_fact_words_are_not_fluff(self):
        # a word that appears inside the verified fact is grounding, not fluff (Grok's allowlist)
        facts = ["we thrive in a fast-paced, dynamic environment"]
        self.assertIsNone(essay.why_fluff("I want to thrive there", facts))
        self.assertIsNotNone(essay.why_fluff("I want to thrive there", []))   # no fact -> fluff

    def test_clean_model_draft_passes(self):
        # what a future hybrid would be ALLOWED to ship: dry, first-person, fact-led, no echo
        clean = ("1Password's focus on building the foundation for a safe, productive digital future "
                 "is what draws me. My work is in cloud infrastructure and compliance, which is why the "
                 "Senior IT Engineer role is a fit.")
        fails = essay.validate(clean, "why_company", {}, {"hard": [], "limited": []},
                               facts=self.FACTS, current_title="Network Systems Analyst 4",
                               target_role="Senior IT Engineer")
        self.assertEqual(fails, [], "a clean model draft must pass, got %r" % fails)

    def test_model_draft_that_slipped_the_gate_is_now_caught(self):
        # a real qwen2.5:7b draft that passed the first-cut gate: soft fluff + product echo
        draft = ("1Password's focus on building the foundation for a safe, productive digital future "
                 "resonates with my passion for infrastructure and security engineering. In my current "
                 "role, I have experience in cloud environments and security monitoring, which aligns "
                 "well with the need to ensure every device is trusted and every application sign-in is "
                 "secure. This opportunity allows me to contribute to these critical areas and support "
                 "the company's mission.")
        fails = essay.validate(draft, "why_company", {}, {"hard": [], "limited": []},
                               facts=self.FACTS, current_title="Network Systems Analyst 4",
                               target_role="Senior IT Engineer")
        self.assertTrue(fails, "the product-echo/fluff draft must be rejected")


class GapOpener(unittest.TestCase):
    def test_gap_opener_is_professional(self):
        story = {"id": "s", "hero": "Rebuilt a broken Wazuh SIEM",
                 "star": {"action": "I rebuilt it on Rocky Linux.", "result": "Monitoring came back."}}
        out = essay._honest_gap("endpoint?", {"hard": ["endpoint management"], "limited": []}, story, {"max_chars": 700})
        self.assertFalse(out.lower().startswith("to be straight"))
        self.assertIn("have not worked directly", out.lower())


class ComposeStar(unittest.TestCase):
    STORY = {
        "id": "wazuh-rebuild",
        "hero": "Rebuilt a broken Wazuh SIEM",
        "star": {
            "situation": "Inherited a Wazuh SIEM that was down: indexer certs, /tmp noexec, agents offline.",
            "action": "Rebuilt it on Rocky Linux 9.7, restored monitoring, and deployed Windows and Linux agents.",
            "result": "Monitoring was restored, agents reported again, and I captured a hardened reusable AMI.",
        },
    }

    def test_compose_has_action_and_result(self):
        out = essay._compose_star(self.STORY)
        self.assertTrue(essay._ACTION_VERB.search(out), "composed answer must contain an action verb")
        self.assertIn("restored", out.lower())      # result content present
        self.assertIn("deployed", out.lower())       # action content present

    def test_compose_passes_validate_no_action_verb(self):
        out = essay._compose_star(self.STORY)
        fails = essay.validate(out, "owned_project", {}, {"hard": [], "limited": []})
        self.assertNotIn("no_action_verb", fails)


class InventoryGenre(unittest.TestCase):
    """Gem DevOps app: 'which/what X have you used' must enumerate the real stack, not become a
    STAR project story; genuine 'describe a ...' narratives stay owned_project."""
    GEM = {
        "Which AWS services and monitoring tools have you used in production?": "inventory",
        "What automated testing have you set up or maintained, such as synthetic monitoring, integration tests, or endpoint checks?": "owned_project",  # practice noun -> not inventory
        "Briefly describe a CI/CD pipeline you built or maintained.": "owned_project",
        "Describe one security issue you identified and resolved within a pipeline or cloud environment.": "owned_project",
    }

    def test_gem_question_genres(self):
        for q, want in self.GEM.items():
            g = essay.classify_genre(q, essay.parse_constraints(q, None))
            self.assertEqual(g, want, "genre for %r" % q[:45])

    def test_years_and_experience_stay_technical(self):
        for q in ("How many years of hands-on DevOps or cloud engineering experience do you have?",
                  "What is your experience with AWS?"):
            g = essay.classify_genre(q, essay.parse_constraints(q, None))
            self.assertEqual(g, "technical_experience", "genre for %r" % q[:45])

    def test_aws_list_enumerates_no_story(self):
        a = essay._enumerate_experience("Which AWS services and monitoring tools have you used in production?")
        self.assertTrue(a, "must produce a list")
        low = a.lower()
        for marker in ("rocky linux", "rebuilt", "hardened ami", "filebeat"):
            self.assertNotIn(marker, low, "must not contain wazuh story marker %r" % marker)
        self.assertIn("ec2", low)

if __name__ == "__main__":
    unittest.main(verbosity=2)
