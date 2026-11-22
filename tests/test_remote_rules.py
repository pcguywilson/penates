"""Slice D remote detection. Fixtures from the live rows named on the board.

Run: python tests/test_remote_rules.py
"""
import os
import sys
import unittest

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
sys.path.insert(0, _root)
import serve


# Comcast DNS Platform & DevOps Engineer (jobspy, Philadelphia). The
# parenthetical is the only remote wording.
COMCAST_DESC = (
    "You will do the best work of your career right here at Comcast. "
    "(In most cases, Comcast prefers to have employees on-site collaborating "
    "unless the team has been designated as virtual due to the nature of "
    "their work. If a position is listed with both office locations and "
    "virtual offerings, Comcast may be willing to consider candidates who "
    "live greater than 100 miles from the office for the remote option.) "
    "Job Summary The DNS Platform engineer joins the team."
)


class RemoteRules(unittest.TestCase):
    def test_comcast_boiler_is_not_a_remote_alternative(self):
        row = {
            "source": "jobspy:indeed",
            "company": "Comcast",
            "role": "DNS Platform & DevOps Engineer",
            "location": "Philadelphia, PA, US",
            "desc": COMCAST_DESC,
        }
        self.assertTrue(serve._REMOTE_ALT_RE.search(COMCAST_DESC))
        stripped = serve._remote_rule_desc(row)
        self.assertFalse(serve._REMOTE_ALT_RE.search(stripped))
        self.assertNotIn("virtual", stripped.lower())
        self.assertFalse(serve._passes_remote_only(row))
        self.assertEqual(serve._remote_only_filter_reason(row), "not_remote")
        # A real workplace sentence after the boilerplate still counts.
        kept = dict(row, desc=COMCAST_DESC + " This position is 100% remote.")
        self.assertTrue(serve._passes_remote_only(kept))
        self.assertIsNone(serve._remote_only_filter_reason(kept))

    def test_city_silent_stored_remote_does_not_confirm(self):
        # Cerebras Tools/DevOps, Sunnyvale. INflow Platform Engineer, Springfield.
        # Country-only and empty locations stay.
        cerebras = {
            "source": "ats-api",
            "company": "Cerebras Systems",
            "role": "Software Engineer - Tools & Infrastructure / DevOps",
            "location": "Sunnyvale, CA",
            "remote": True,
            "desc": "Build internal tools. Kubernetes and Terraform.",
        }
        inflow = {
            "source": "ats-api",
            "company": "INflow Federal",
            "role": "Platform Engineer (GitLab practice)",
            "location": "Springfield, VA",
            "remote": True,
            "desc": "Cleared customer-facing GitLab professional services.",
        }
        for row in (cerebras, inflow):
            self.assertFalse(serve._passes_remote_only(row), row["company"])
            self.assertEqual(serve._remote_status(row), "not_remote", row["company"])
        self.assertTrue(serve._passes_remote_only({
            "source": "ats-api", "location": "United States",
            "role": "SRE", "remote": True, "desc": ""}))
        self.assertTrue(serve._passes_remote_only({
            "source": "ats-api", "location": "",
            "role": "SRE", "remote": True, "desc": ""}))
        # A remote word in the description confirms the city.
        self.assertTrue(serve._passes_remote_only(dict(
            cerebras, desc="Employees can work remotely from anywhere in the US.")))

    def test_hybrid_model_unless_fully_remote_alternative(self):
        # Delinea Data Center: hybrid model, no remote alternative.
        delinea = {
            "source": "ats-api",
            "company": "Delinea",
            "role": "Senior Systems Engineer - Data Center",
            "location": "Washington, D.C.",
            "remote": True,
            "desc": (
                "The role is based in Reston/DC and operates in a hybrid model "
                "out of the DC office, with regular time in local data centers."
            ),
        }
        self.assertFalse(serve._passes_remote_only(delinea))
        self.assertEqual(serve._remote_status(delinea), "not_remote")
        self.assertTrue(serve._is_hybrid(delinea))
        self.assertEqual(serve._workplace(delinea), "hybrid")
        # UCSD: hybrid (preferred) or a remote schedule stays remote.
        ucsd = {
            "source": "ats-api",
            "company": "UC San Diego Health",
            "role": "Intel Automation Engineer - Cloud & Integration",
            "location": "San Diego, CA",
            "remote": True,
            "desc": "UCSD offers hybrid (preferred) or a remote schedule.",
        }
        self.assertTrue(serve._passes_remote_only(ucsd))
        self.assertFalse(serve._is_hybrid(ucsd))
        self.assertEqual(serve._workplace(ucsd), "remote")
        ucsd_or = dict(ucsd, desc="hybrid (preferred) or remote")
        self.assertTrue(serve._passes_remote_only(ucsd_or))
        self.assertFalse(serve._is_hybrid(ucsd_or))
        # Aggregator: hybrid schedule plus a fully-remote alternative stays.
        sched = {
            "source": "jobspy:indeed",
            "role": "SRE",
            "location": "Austin, TX",
            "desc": "This role uses a hybrid schedule. Fully remote is also available.",
        }
        self.assertTrue(serve._passes_remote_only(sched))
        self.assertIsNone(serve._remote_only_filter_reason(sched))
        self.assertEqual(serve._workplace(sched), "remote")
        # Same phrase with no alternative is hybrid.
        bare = dict(sched, desc="This role uses a hybrid work arrangement.")
        self.assertFalse(serve._passes_remote_only(bare))
        self.assertEqual(serve._remote_only_filter_reason(bare), "not_remote")
        self.assertEqual(serve._workplace(bare), "hybrid")

    def test_non_us_location_without_us_is_not_remote(self):
        bulgaria = {
            "source": "ats-api",
            "company": "Smartsheet",
            "role": "DevOps Engineer, Edge and Developer Experience",
            "location": "-REMOTE, BULGARIA-",
            "remote": True,
            "desc": "You will work remotely from Bulgaria. #LI-Remote",
        }
        gitlab = {
            "source": "ats-api",
            "company": "GitLab",
            "role": "Senior Backend Engineer, Core DevOps: Container Registry",
            "location": "Remote, Canada; Remote, United States",
            "remote": True,
            "desc": "All of our roles are remote.",
        }
        supabase = {
            "source": "ats-api",
            "company": "Supabase",
            "role": "Platform Engineer, Managed Postgres",
            "location": "Remote, Global",
            "remote": True,
            "desc": "Fully remote.",
        }
        self.assertTrue(serve._location_names_foreign(bulgaria["location"]))
        self.assertFalse(serve._passes_remote_only(bulgaria))
        self.assertEqual(serve._remote_status(bulgaria), "not_remote")
        self.assertFalse(serve._location_names_foreign(gitlab["location"]))
        self.assertTrue(serve._passes_remote_only(gitlab))
        self.assertEqual(serve._remote_status(gitlab), "remote")
        self.assertFalse(serve._location_names_foreign(supabase["location"]))
        self.assertTrue(serve._passes_remote_only(supabase))
        self.assertEqual(serve._remote_status(supabase), "remote")
        # Same country rule on an aggregator row.
        bg_agg = dict(bulgaria, source="jobspy:indeed")
        self.assertEqual(serve._remote_only_filter_reason(bg_agg), "not_remote")


if __name__ == "__main__":
    unittest.main()
