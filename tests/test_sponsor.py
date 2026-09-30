"""Work-authorization vs sponsorship-need wording (SpecterOps 2026-09-30 regression)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serve

YN = ["Yes", "No"]


def ans(q):
    return serve.do_answer({"question": q, "kind": "select", "options": YN, "required": True}).get("text")


class Sponsor(unittest.TestCase):
    def test_authorized_without_sponsorship_is_yes(self):
        self.assertEqual(ans("Are you currently authorized to work in the United States without visa sponsorship?"), "Yes")
        self.assertEqual(ans("Are you legally eligible to work in the US without requiring sponsorship?"), "Yes")

    def test_need_sponsorship_is_no(self):
        self.assertEqual(ans("Will you now or in the future require visa sponsorship?"), "No")
        self.assertEqual(ans("Do you require sponsorship for employment visa status (e.g. H-1B)?"), "No")


if __name__ == "__main__":
    unittest.main()
