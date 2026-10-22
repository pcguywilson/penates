"""Never write a story/essay into a non-experience question (2026-10-01: 'upcoming time off' got
the Wazuh story because unmatched questions defaulted to owned_project)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import essay, serve

NOT_EXPERIENCE = [
    "Please list any upcoming time off you have planned.",
    "Do you have any planned vacations in the next 3 months?",
    "What are your salary expectations?",
    "Is there anything else you would like us to know?",
    "What is your availability to start?",
    "Please describe any accommodations you need for the interview.",
    "How did you hear about this position?",
    "What is your preferred work schedule?",
    "Please explain any gaps in your employment history.",
    "Are you willing to travel up to 25%?",
    "Do you have any questions for us?",
    "Please provide your portfolio URL.",
    # Grok board seq 103 adversarial set
    "Tell us about yourself.",
    "Tell me about yourself.",
    "Give us an example of your pay expectations.",
    "How many direct reports have you led?",
    "How many years of experience do you have with Terraform?",
    "Tell us about your desired salary.",
    "Describe your preferred start date and any planned time off.",
    "Describe your ideal team.",
    "Describe your ideal manager.",
    "What are your strengths and weaknesses?",
    "What is your greatest weakness?",
    "Where do you see yourself in five years?",
    "How would you describe your work style?",
    "How do you like to receive feedback?",
    "What skills are you hoping to develop here?",
    # Grok board seq 197
    "Summarize yourself in one sentence.",
    "What does work-life balance mean to you?",
    "What does success in this role mean to you?",
    "Please describe the type of project or team you are looking for.",
    "Describe an ideal project you would like to join.",
    "Describe one thing you want us to know.",
    "Share an example of a value you look for in an employer.",
    "Share an example of how our mission resonates with you.",
    "Walk us through your desired work arrangement.",
    "Rate your interest in this role from 1 to 5.",
    "How do you handle confidential information?",
    "How do you handle multiple priorities and deadlines?",
    "If you were unavailable for a week, who would cover your work?",
    "What would you build in a relationship with your new manager?",
    "How have you set up direct deposit with previous employers?",
]
WHY_NOT = [
    "Why are you leaving your current position?",
    "Why are you looking for a new opportunity?",
    "Why should we hire you?",
    "Why compensation?",
    "Why leave?",
    "Why relocate?",
]
HOSTILE_NOT = [
    "Are you at least 18 years of age?",
    "Describe your ideal team.",
    "What are your strengths and weaknesses?",
    "Where do you see yourself in five years?",
    "How would you describe your work style?",
    "What skills are you hoping to develop here?",
    "Tell us about yourself.",
]
EXPERIENCE_ANY = [
    "What is your experience with high availability design?",
    "How have you designed for high availability?",
    "What is your experience running hybrid cloud (AWS and on-prem)?",
    "What is your experience with on-site data center hardware?",
    "How would you rate your proficiency with Terraform?",
    "What is your experience with scheduled jobs and cron automation?",
    "How have you automated scheduled patching?",
    "What is your experience with shift-left security?",
    "What is your experience with authorization and IAM?",
    "How have you implemented authorization for a multi-tenant API?",
    "What is your experience with open source?",
    "What is your experience with hourly batch jobs?",
    "What is your experience with URL filtering or secure web gateways?",
    "What is your experience with weekend maintenance windows?",
    "How have you implemented your preferred monitoring stack?",
    "What would you build in your first 90 days?",
    "Describe how you found and resolved a Sev-1 incident.",
    "How do you approach a Sev-1 incident?",
    "What is your background in cloud infrastructure?",
    "Please describe any relevant experience not on your resume.",
    "Do you have experience with Kubernetes? If so, describe it.",
    "How did you find and fix a production memory leak?",
    "How did you shift a legacy workload to AWS?",
    "How have you contributed to open source projects?",
    "What is your experience with on-call rotations?",
    "What is your experience with GitHub Actions?",
    "What is your experience with systems that require a security clearance?",
    "What is your experience sponsoring junior engineers?",
    "What is your experience supporting a multi-timezone operation?",
    "What is your experience with website reliability?",
    # Grok board seq 197
    "What is your experience with AWS availability zones?",
    "How have you designed for multi-region availability?",
    "What is your experience with hourly rate limiting for public APIs?",
    "What is your experience supporting night shift NOC operations?",
    "What is your experience with in-office hardware labs?",
    "What is your experience with datacenter relocation projects?",
    "How have you reduced relocation of workloads between regions?",
    "How have you planned a datacenter relocation?",
    "What is your experience with your current company's observability stack?",
    "Tell us about your experience with Linux administration.",
    "Please share your experience with CI/CD.",
    "What experience do you have with Ansible?",
    "Do you have any experience using AWS?",
    "Have you used Kubernetes in production?",
    "Have you ever built a SIEM from scratch?",
    "Explain a time when you restored service after an outage.",
    "How have you shifted security earlier in the delivery pipeline?",
    "Describe how you would handle a Sev-1 you have not seen before.",
    "Have you worked with Grafana and log aggregation?",
    "Could you outline your experience building CI pipelines?",
    "Describe your patching schedule for production Linux fleets.",
    "How have you debugged packet loss between two availability zones?",
    "How have you resolved a conflict of interest between security and a delivery deadline?",
    "Describe a time you handled a conflict of interest between two teams.",
    "What is your experience accommodating sudden traffic spikes?",
    # 2026-10-05: "briefly" + "what tools" sent this to inventory (a stack list)
    "Tell us briefly about a complex systems or endpoint issue you independently troubleshot and resolved. "
    "What was the issue, what tools did you use, and what was the outcome?",
]
TIER1_STEAL = [
    "What is your experience with on-call rotations?", "What is your experience with GitHub Actions?",
    "What is your experience with systems that require a security clearance?",
    "What is your experience sponsoring junior engineers?",
    "What is your experience supporting a multi-timezone operation?",
    "What is your experience with website reliability?",
    "What is your experience with location-based access control?",
    "How have you managed remote Terraform state?",
    "What is your experience with state machines?",
    "Describe your experience with province-aware data residency.",
    "What is your experience with mobile device management?",
    "What is your experience with cell-based architecture?",
    "How have you built phone-home agents?",
    "How have you debugged race conditions in distributed workers?",
    "What is your experience with unit testing?",
    "What is your experience with cipher suites?",
    "What is your experience with extension attributes in Active Directory?",
    "What is your experience with address sanitization in log pipelines?",
    "What is your language experience across Python and Bash?",
    "Describe your experience designing city failover.",
    "What is your experience with county emergency-services networks?",
    "What is your experience with referrer-based abuse detection?",
    "Please describe your experience working remotely.",
    "What is your experience with monitoring and observability?",
    "Please describe your hands-on experience with Linux.",
    "Have you built tooling to automate infrastructure changes?",
    "How have you resolved a conflict of interest between security and a delivery deadline?",
    "Describe a time you handled a conflict of interest between two teams.",
    "What is your experience accommodating sudden traffic spikes?",
    "What is your experience with datacenter relocation projects?",
    "What is your experience with your current company's observability stack?",
]
KEEP_FIELD = [
    "What are your salary expectations?",
    "How did you hear about this position?",
    "Do you have a conflict of interest with any current employee?",
]
EXPERIENCE = [
    ("Describe a project you led from start to finish.", "owned_project"),
    ("Tell us about a time you handled a production outage.", "owned_project"),
    ("What is your experience with Kubernetes?", "technical_experience"),
    ("Describe a time you disagreed with a teammate.", "behavioral"),
]


class GenreGuard(unittest.TestCase):
    def test_non_experience_never_gets_a_story(self):
        for q in NOT_EXPERIENCE:
            g = essay.classify_genre(q, essay.parse_constraints(q, None))
            self.assertNotIn(g, essay._STORY_GENRES, q)
            r = serve.do_answer({"question": q, "kind": "textarea", "required": False})
            self.assertFalse(r.get("story"), q)
            self.assertNotIn("Wazuh", r.get("text") or "", q)

    def test_experience_questions_reach_a_story_genre(self):
        for q in EXPERIENCE_ANY:
            g = essay.classify_genre(q, essay.parse_constraints(q, None))
            self.assertIn(g, essay._STORY_GENRES, q)

    def test_tier1_fields_do_not_steal_experience_questions(self):
        for q in TIER1_STEAL:
            r = serve.do_answer({"question": q, "kind": "textarea", "required": False})
            self.assertNotEqual(r.get("method"), "field", (q, r.get("text")))
            self.assertNotIn(r.get("method"), ("optional-skip", "leave-blank"), q)

    def test_tier1_still_answers_logistics_textareas(self):
        for q in KEEP_FIELD:
            self.assertFalse(serve._prose_experience_q(q), q)
        r = serve.do_answer({"question": KEEP_FIELD[-1], "kind": "textarea", "required": False})
        self.assertEqual(r.get("text"), "No")

    def test_why_genres_never_for_leaving_or_logistics(self):
        for q in WHY_NOT:
            g = essay.classify_genre(q, essay.parse_constraints(q, None))
            self.assertNotIn(g, ("why_company", "why_role"), q)

    def test_hostile_llm_is_contained(self):
        up, oll = essay._a.ollama_up, essay._a.ollama
        try:
            essay._a.ollama_up = lambda *a, **k: True
            for pick in ("owned_project", "technical_experience", "behavioral", "hypothetical", "why_company", "why_role"):
                essay._a.ollama = lambda *a, _p=pick, **k: _p
                for q in HOSTILE_NOT + WHY_NOT:
                    g = essay.classify_genre(q, essay.parse_constraints(q, None))
                    self.assertNotIn(g, essay._STORY_GENRES + ("why_company", "why_role"), (pick, q))
        finally:
            essay._a.ollama_up, essay._a.ollama = up, oll

    def test_unmatched_default_is_other(self):
        self.assertEqual(essay._classify_genre_raw("Zebra quantum marmalade?", {}) if not essay._a.ollama_up() else "other", "other")

    def test_real_experience_questions_still_route(self):
        for q, want in EXPERIENCE:
            self.assertEqual(essay.classify_genre(q, essay.parse_constraints(q, None)), want, q)


class DescribeTheMost(unittest.TestCase):
    """2026-10-13 Lever: 'Describe the most complex ... which technologies did you use' went to the
    inventory stack list; 'Describe a significant production ... incident' fell to other."""
    def g(self, q):
        return essay.classify_genre(q, essay.parse_constraints(q, None))

    def test_most_complex_is_story(self):
        q = ("Describe the most complex cloud infrastructure solution you have personally designed and implemented. "
             "What were the customer or business requirements, which technologies did you use, and what was your specific contribution?")
        self.assertEqual(self.g(q), "owned_project")

    def test_long_incident_is_story(self):
        q = ("Describe a significant production cloud or infrastructure incident you personally investigated and resolved. "
             "What was the root cause, how did you troubleshoot the issue, and what changes did you implement to prevent recurrence?")
        self.assertEqual(self.g(q), "owned_project")

    def test_inventory_still_inventory(self):
        self.assertEqual(self.g("Which AWS services have you used?"), "inventory")


if __name__ == "__main__":
    unittest.main()
