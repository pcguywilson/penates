"""employer_key normalization and employers_hidden round-trip.

Hide writes a temp config.json. The real config and jobs.json are only read
so a status byte can be compared. The loopback server is not the autofill server.
Run: python tests/test_employer.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

_here = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_here)
sys.path.insert(0, ROOT)

import jobs_store
import serve


class EmployerKey(unittest.TestCase):
    def test_suffix_paren_and_space(self):
        self.assertEqual(serve.employer_key("Acme, Inc."), "acme")
        self.assertEqual(serve.employer_key("Acme Inc."), "acme")
        self.assertEqual(serve.employer_key("Acme LLC"), "acme")
        self.assertEqual(serve.employer_key("Acme Corp."), "acme")
        self.assertEqual(serve.employer_key("Acme Co"), "acme")
        self.assertEqual(serve.employer_key("Acme Ltd."), "acme")
        self.assertEqual(serve.employer_key("  Acme   Corp  "), "acme")
        self.assertEqual(serve.employer_key("Acme, Inc. (Remote)"), "acme")
        self.assertEqual(serve.employer_key("Acme (Herndon), Inc."), "acme")
        self.assertEqual(serve.employer_key("Costco"), "costco")

    def test_gdit_alias(self):
        self.assertEqual(serve.employer_key("GDIT"), "gdit")
        self.assertEqual(serve.employer_key("GDIT, Inc."), "gdit")
        self.assertEqual(serve.employer_key("General Dynamics IT"), "gdit")
        self.assertEqual(
            serve.employer_key("General Dynamics Information Technology, Inc."), "gdit")
        self.assertEqual(
            serve.employer_key("General Dynamics Information Technology (Herndon)"), "gdit")

    def test_aggregator_parses_hiring_company_without_rewriting_stored_name(self):
        company = "Jobgether"
        role = "Stripe, Inc. | Senior DevOps Engineer"
        self.assertEqual(serve.employer_key(company, role), "stripe")
        self.assertEqual(company, "Jobgether")
        self.assertEqual(
            serve.employer_key("Jobgether", "Senior SRE at General Dynamics IT"), "gdit")
        self.assertEqual(
            serve.employer_key("Apex Staffing LLC", "Acme Corp | Platform Engineer"), "acme")
        self.assertEqual(
            serve.employer_key("Apex Staffing", "Platform Engineer at Acme, Inc."), "acme")
        # A direct employer keeps its own name even when the title has a pipe or "at".
        self.assertEqual(
            serve.employer_key("Example Co", "Stripe | Engineer"), "example")
        self.assertEqual(serve.employer_key("Jobgether", "Senior Engineer"), "jobgether")

    def test_remote_only_city_drops_country_keeps(self):
        self.assertFalse(serve._passes_remote_only({
            "location": "Austin, TX", "role": "DevOps Engineer", "workplace": ""}))
        self.assertTrue(serve._passes_remote_only({
            "location": "Austin, TX", "role": "DevOps Engineer (Remote)", "workplace": ""}))
        self.assertTrue(serve._passes_remote_only({
            "location": "United States", "role": "SRE", "workplace": ""}))
        self.assertTrue(serve._passes_remote_only({
            "location": "USA", "role": "SRE", "workplace": ""}))
        self.assertTrue(serve._passes_remote_only({
            "location": "Multiple Locations", "role": "SRE", "workplace": ""}))
        self.assertTrue(serve._passes_remote_only({
            "location": "", "role": "SRE", "workplace": "onsite"}))
        self.assertTrue(serve._passes_remote_only({
            "location": "New York, NY", "workplace": "New York, NY",
            "role": "Engineer", "remote": True}))

    def test_remote_only_title_onsite_hybrid_overrides_workplace(self):
        # JobSpy often sets workplace=Remote while the title says onsite/hybrid.
        wp = {"workplace": "Remote", "location": "", "remote": True}
        drops = [
            "IT Infrastructure Engineer (Onsite - Dallas)",
            "Senior DevOps Engineer (hybrid)",
            "Systems Administrator (onsite at Muskegon Community College)",
            "Systems Engineer (Mid-Level)- Hybrid (Partial Remote)",
        ]
        for role in drops:
            self.assertFalse(
                serve._passes_remote_only(dict(wp, role=role)), role)
        keeps = [
            "Senior Cloud Engineer (Hybrid @ Bellevue, WA or Remote @ Florida)",
            "Hybrid Infrastructure Engineer (Cloud Migration Lead)",
        ]
        for role in keeps:
            self.assertTrue(
                serve._passes_remote_only(dict(wp, role=role)), role)
        # in-office token; remote alternative forms
        self.assertFalse(serve._passes_remote_only(dict(
            wp, role="Platform Engineer (in-office, Austin)")))
        self.assertTrue(serve._passes_remote_only(dict(
            wp, role="SRE - Hybrid with remote option")))
        self.assertTrue(serve._passes_remote_only(dict(
            wp, role="Cloud Engineer /remote")))
        # tech hybrid phrases are not workplace
        self.assertTrue(serve._passes_remote_only(dict(
            wp, role="Engineer - hybrid cloud migration")))
        self.assertTrue(serve._passes_remote_only(dict(
            wp, role="hybrid environment platform lead")))
        self.assertTrue(serve._passes_remote_only(dict(
            wp, role="hybrid IT systems engineer")))
        self.assertTrue(serve._passes_remote_only(dict(
            wp, role="hybrid multi-cloud architect")))

    def test_remote_only_aggregator_five_leaks_fail(self):
        # Board seq 135: jobspy/builtin must not pass on remote_hint alone.
        cases = [
            {
                "source": "jobspy:indeed", "company": "Langley FCU",
                "role": "Cloud Infrastructure Engineer",
                "location": "Newport News, VA", "workplace": "",
                "remote_hint": True,
                "desc": "THIS IS NOT A REMOTE POSITION. Hybrid schedule from our "
                        "Oyster Point building.",
            },
            {
                "source": "jobspy:indeed", "company": "Wenger Group",
                "role": "Systems Engineer",
                "location": "Lancaster, PA", "workplace": "",
                "remote_hint": True,
                "desc": "Family-owned food and agricultural services in Pennsylvania.",
            },
            {
                "source": "builtin", "company": "Systems Technology Forum",
                "role": "Systems Administrator",
                "location": "Pearl Harbor, HI", "workplace": "Remote",
                "remote": True, "desc": "",
            },
            {
                "source": "builtin", "company": "Upwind",
                "role": "DevOps Engineer - US East Coast/Central",
                "location": "US", "workplace": "Remote",
                "remote": True, "desc": "",
            },
            {
                "source": "builtin", "company": "Horizon Industries",
                "role": "Senior Systems Administrator DaaS API GW",
                "location": "USA", "workplace": "Remote",
                "remote": True, "desc": "",
            },
        ]
        for row in cases:
            self.assertFalse(serve._passes_remote_only(row), row.get("company"))
            reason = serve._remote_only_filter_reason(row)
            self.assertIn(reason, ("not_remote", "remote_unknown"), row.get("company"))
        self.assertEqual(
            serve._remote_only_filter_reason(cases[0]), "not_remote")
        # City/state with no remote positive -> not_remote (seq 138).
        for row in cases[1:3]:
            self.assertEqual(
                serve._remote_only_filter_reason(row), "not_remote",
                row.get("company"))
        # Country-only US/USA with no signal -> remote_unknown (seq 138).
        for row in cases[3:]:
            self.assertEqual(
                serve._remote_only_filter_reason(row), "remote_unknown",
                row.get("company"))
            self.assertEqual(serve._remote_status(row), "unknown", row.get("company"))

    def test_remote_only_aggregator_keep_cases(self):
        keeps = [
            {"source": "jobspy:indeed", "role": "Senior SRE (Remote)",
             "location": "Austin, TX", "desc": ""},
            {"source": "builtin", "role": "Platform Engineer",
             "location": "Remote, US", "desc": "", "remote": True},
            {"source": "jobspy:indeed", "role": "SRE",
             "location": "United States",
             "desc": "This is a fully remote role."},
            {"source": "builtin", "role": "Cloud Engineer",
             "location": "Denver, CO",
             "desc": "Hybrid or remote options available."},
        ]
        for row in keeps:
            self.assertTrue(serve._passes_remote_only(row), row)
            self.assertIsNone(serve._remote_only_filter_reason(row), row)
            self.assertEqual(serve._remote_status(row), "remote", row)

    def test_remote_only_ats_keeps_current_logic(self):
        # ats-api / hiringcafe still trust remote / country-only location.
        self.assertTrue(serve._passes_remote_only({
            "source": "ats-api", "location": "New York, NY",
            "role": "Engineer", "remote": True, "desc": ""}))
        self.assertTrue(serve._passes_remote_only({
            "source": "hiringcafe", "location": "United States",
            "role": "SRE", "workplace": "", "desc": ""}))
        self.assertFalse(serve._passes_remote_only({
            "source": "ats-api", "location": "Austin, TX",
            "role": "DevOps Engineer", "workplace": "", "desc": ""}))

    def test_remote_only_desc_positives_seq136(self):
        # Board seq 136: workplace phrases in the description pass.
        # Desc negatives still override unless a remote alternative matches.
        base = {"source": "jobspy:indeed", "location": "Austin, TX",
                "workplace": "", "remote_hint": True, "role": "Engineer"}
        positives = [
            ("QED.ai", "The position can be remote, with periodic visits encouraged"),
            ("Ford", "This role is posted as remote unless you reside within 50 miles of Dearborn"),
            ("Worth", "All Remote Hires will be required to travel to Orlando"),
            ("Logic20/20", "Can work independently in a remote setting with minimal supervision"),
            ("Peraton", "This position is 100% remote."),
            ("Kit", "We have always been entirely remote."),
            ("LucyRx", "We are a nimble, remote-first team."),
            ("Justrite", "This remote-eligible role supports the ERP."),
            ("Johnson & Johnson", "This position is remote eligible."),
            ("Highmark", "Position Type: Remote-based"),
            ("Agilent", "It includes the option to work remotely."),
            ("Serco", "Telework eligible for this position"),
        ]
        for company, desc in positives:
            row = dict(base, company=company, desc=desc)
            self.assertTrue(serve._passes_remote_only(row), desc)
            self.assertIsNone(serve._remote_only_filter_reason(row), desc)
        # City/state (Austin, TX) with no workplace positive -> not_remote (seq 138).
        negatives = [
            ("Glydways", "scaling them with remote caching and remote build execution",
             "not_remote"),
            ("mecka", "device identity, remote updates, rollback, and recovery",
             "not_remote"),
            ("Wyetech", "Perform remote maintenance and refresh efforts across eight sites",
             "not_remote"),
            ("NinjaOne", "autonomous patching, backup, and remote access in a single console",
             "not_remote"),
            ("Stanford", "geographically distinct replication in a remote data center",
             "not_remote"),
            ("Synopsys", "**Remote Eligible** **No** and remote access support",
             "not_remote"),
            ("James Madison University",
             "This is an on-campus, on-site position (not remote) with normal business hours",
             "not_remote"),
            ("James Madison University",
             "This is an on-campus, on\\-site position (not remote) with normal business hours",
             "not_remote"),
            ("Charleston ENT", "This position is not remote.", "not_remote"),
            ("Northrop Grumman",
             "This position is 100% on site in Chandler, AZ (strongly preferred). "
             "Remote candidates may also be considered.",
             "not_remote"),
        ]
        for company, desc, reason in negatives:
            row = dict(base, company=company, desc=desc)
            self.assertFalse(serve._passes_remote_only(row), desc)
            self.assertEqual(serve._remote_only_filter_reason(row), reason, desc)

    def test_remote_only_desc_positives_seq137(self):
        # Board seq 137: more workplace phrases pass; boilerplate does not.
        # "for remote workers" (seq 136 Workato keep) is a support object now.
        base = {"source": "jobspy:indeed", "location": "Austin, TX",
                "workplace": "", "remote_hint": True, "role": "Engineer"}
        positives = [
            ("Motorola", "Schedule: Remote Full Time work, Mon - Fri"),
            ("Motorola", "remote full-time"),
            ("Packaging Corporation of America",
             "along active production lines when required. #LI-Remote"),
            ("Packaging Corporation of America",
             "disability line \\#LI\\-Remote \\#LI\\-PW1"),
            ("Modular", "in our office in Los Altos, CA or remotely from home"),
            ("Modular", "You can work in our office or remotely."),
            ("Mastercard", "Remote - Georgia: $170,000 - $281,000 USD"),
            ("Mastercard", "Remote - New Jersey: $170,000"),
            ("Anywhere", "Employees can work remotely from anywhere in the US."),
        ]
        for company, desc in positives:
            row = dict(base, company=company, desc=desc)
            self.assertTrue(serve._passes_remote_only(row), desc)
            self.assertIsNone(serve._remote_only_filter_reason(row), desc)
        # Pay line is line-anchored and capitalised. Mid-line does not count.
        # City/state base -> not_remote (seq 138).
        row = dict(base, company="Mastercard",
                   desc="Target cash Remote - Georgia: $170,000")
        self.assertEqual(
            serve._remote_only_filter_reason(row), "not_remote")
        # "or remotely" without an office alternative is not a workplace.
        row = dict(base, company="Other",
                   desc="Travel or remotely as needed for the project.")
        self.assertEqual(
            serve._remote_only_filter_reason(row), "not_remote")
        # A workplace phrase in another sentence still counts when E-Verify
        # boilerplate shares the posting.
        row = dict(base, company="Modular", desc=(
            "You can work in our office in Los Altos, CA or remotely from home. "
            "This employer participates in E-Verify and will provide Form I-9 "
            "information."))
        self.assertIsNone(serve._remote_only_filter_reason(row))
        negatives = [
            ("Sweet James", "AVD environment for remote workers",
             "not_remote"),
            ("Workato", "ranked the best company for remote workers",
             "not_remote"),
            ("Mueller",
             "all applicants and employees, including those hired to work "
             "remotely. Upon enrollment, employers participating in E\\-Verify "
             "are required to provide Form I\\-9 information.",
             "not_remote"),
            ("Mueller",
             "This fully remote role uses E-Verify and Form I-9.",
             "not_remote"),
            ("Las Vegas Sands", "Work remotely as necessary.",
             "not_remote"),
            ("Lincoln International",
             "Monday through Thursday in office, with the option to work "
             "remotely or from the office on Fridays.",
             "not_remote"),
            ("Lincoln International",
             "option to work remotely one day a week",
             "not_remote"),
            ("Lincoln International",
             "option to work remotely occasionally",
             "not_remote"),
        ]
        for company, desc, reason in negatives:
            row = dict(base, company=company, desc=desc)
            self.assertFalse(serve._passes_remote_only(row), desc)
            self.assertEqual(serve._remote_only_filter_reason(row), reason, desc)

    def test_everify_scrub_matches_legacy_quadratic(self):
        # New linear scrub must match the old [^.!?\n]* keyword form exactly
        # on the E-Verify fixtures above (seq 145 /ranked perf).
        legacy = re.compile(
            r"(?i)[^.!?\n]*\b(?:e\\?-?verify|i\\?-9)\b[^.!?\n]*")

        def legacy_scrub(text):
            text = str(text or "")
            if not text:
                return ""
            return legacy.sub(" ", text)

        cases = [
            "You can work in our office in Los Altos, CA or remotely from home. "
            "This employer participates in E-Verify and will provide Form I-9 "
            "information.",
            "all applicants and employees, including those hired to work "
            "remotely. Upon enrollment, employers participating in E\\-Verify "
            "are required to provide Form I\\-9 information.",
            "This fully remote role uses E-Verify and Form I-9.",
            "Work remotely as necessary.",
            "No keyword here at all, just remote work.",
            "Sentence one. Mid E-Verify stuff here! Next sentence remote.",
            "Form I-9. Remote work available.",
            "",
        ]
        for desc in cases:
            self.assertEqual(
                serve._strip_everify_sentences(desc), legacy_scrub(desc), desc)
            self.assertEqual(
                serve._desc_remote_pos_text(desc),
                serve._DESC_REMOTE_POS_SKIP_RE.sub(" ", legacy_scrub(desc)),
                desc)
        # Request-local memo: same scrubbed text across remote_status path.
        row = {"source": "jobspy:indeed", "company": "Modular",
               "role": "Engineer", "location": "Austin, TX",
               "desc": cases[0]}
        labels = {}
        first = serve._memo_desc_scrub(labels, row)
        second = serve._memo_desc_scrub(labels, row)
        self.assertIs(first, second)
        self.assertEqual(serve._remote_status(row, labels), "remote")
        self.assertEqual(labels.get((id(row), "rs")), "remote")

    def test_remote_status_unknown_and_include_unknown_seq138(self):
        # Empty / country-only + no signal -> unknown; city/state -> not_remote.
        unknown_rows = [
            {"source": "jobspy:indeed", "company": "NVIDIA", "role": "SRE",
             "location": "", "desc": "", "workplace": "", "remote_hint": True},
            {"source": "builtin", "company": "Akamai", "role": "Engineer",
             "location": "US", "desc": "", "workplace": "", "remote": True},
            {"source": "jobspy:indeed", "company": "Cisco", "role": "Platform",
             "location": "USA", "desc": ""},
            {"source": "builtin", "company": "VAST", "role": "DevOps",
             "location": "United States", "desc": ""},
            {"source": "jobspy:indeed", "company": "Recharge", "role": "SRE",
             "location": "U.S.", "desc": ""},
            {"source": "builtin", "company": "Other", "role": "Engineer",
             "location": "America", "desc": ""},
        ]
        for row in unknown_rows:
            self.assertEqual(
                serve._remote_only_filter_reason(row), "remote_unknown", row)
            self.assertEqual(serve._remote_status(row), "unknown", row)
            self.assertFalse(serve._passes_remote_only(row), row)
        city = {"source": "jobspy:indeed", "role": "Engineer",
                "location": "Austin, TX", "desc": "", "remote_hint": True}
        self.assertEqual(serve._remote_only_filter_reason(city), "not_remote")
        self.assertEqual(serve._remote_status(city), "not_remote")
        # ats-api: remote or not_remote only (no unknown).
        self.assertEqual(serve._remote_status({
            "source": "ats-api", "location": "United States",
            "role": "SRE", "workplace": ""}), "remote")
        self.assertEqual(serve._remote_status({
            "source": "ats-api", "location": "Austin, TX",
            "role": "DevOps", "workplace": ""}), "not_remote")
        # include_unknown keeps unknown under remote_only; default drops them.
        rows = [unknown_rows[0], city, {
            "source": "jobspy:indeed", "role": "SRE (Remote)",
            "location": "Austin, TX", "desc": ""}]
        kept_default = serve._filter_ranked(rows, {"remote_only": ["1"]})
        self.assertEqual(len(kept_default), 1)
        self.assertIn("Remote", kept_default[0]["role"])
        kept_inc = serve._filter_ranked(
            rows, {"remote_only": ["1"], "include_unknown": ["1"]})
        self.assertEqual(len(kept_inc), 2)
        statuses = {serve._remote_status(r) for r in kept_inc}
        self.assertEqual(statuses, {"remote", "unknown"})

    def test_remote_only_desc_positives_seq138(self):
        # New workplace phrases; amenity / product names stay non-positives.
        base = {"source": "jobspy:indeed", "location": "",
                "workplace": "", "remote_hint": True, "role": "Engineer"}
        positives = [
            ("Recharge",
             "Monthly Remote Life and Merchant stipends for every teammate"),
            ("Blackpoint",
             "comfortable in a remote, fast-moving environment"),
            ("Blackpoint",
             "thrives in a remote, distributed environment"),
            ("Blackpoint",
             "works well in a remote environment with autonomy"),
            ("Atlassian",
             "Engineering | San Francisco, United States | Remote, Remote"),
            ("Atlassian",
             "Team | Austin, United States | Remote"),
        ]
        for company, desc in positives:
            row = dict(base, company=company, desc=desc)
            self.assertTrue(serve._passes_remote_only(row), desc)
            self.assertIsNone(serve._remote_only_filter_reason(row), desc)
            self.assertEqual(serve._remote_status(row), "remote", desc)
        # Must stay false: amenity on-site phrases are not workplace negatives;
        # product "Remote Flight Deck" is not a workplace positive.
        false_rows = [
            ("JPMorgan",
             "Access to on-site health and wellness centers for employees",
             "remote_unknown"),
            ("iT1",
             "Perks include Onsite Fitness Center and free parking",
             "remote_unknown"),
            ("Skydio",
             "Experience with the Remote Flight Deck product and autonomy stack",
             "remote_unknown"),
        ]
        for company, desc, reason in false_rows:
            row = dict(base, company=company, desc=desc, location="")
            self.assertFalse(serve._passes_remote_only(row), desc)
            self.assertEqual(serve._remote_only_filter_reason(row), reason, desc)
            self.assertEqual(serve._remote_status(row), "unknown", desc)
            # Amenity text must not trip the on-site negative either.
            self.assertIsNone(serve._DESC_REMOTE_NEG_RE.search(desc), desc)

    def test_remote_only_desc_leaks_seq140(self):
        # Board seq 140: three jobspy:indeed leaks must fail remote-only.
        # Bare telework / location-line remote support / Onsite/Remote: label.
        base = {"source": "jobspy:indeed", "workplace": "",
                "remote_hint": True, "role": "Engineer"}
        leaks = [
            {
                "company": "State of Alaska",
                "role": "IT Systems Administrator 1/2/3",
                "location": "Juneau, AK",
                "desc": "Enjoy flexible schedules with telework options",
            },
            {
                "company": "Commonwealth of Kentucky",
                "role": "Systems Administrator",
                "location": "Frankfort, KY",
                "desc": ("Base Location: Frankfort, KY – In Office – "
                         "Provides remote support to KSP employees"),
            },
            {
                "company": "John Deere",
                "role": "Infrastructure Engineer",
                "location": "Davenport, IA",
                "desc": "***Onsite/Remote:****Onsite Position*",
            },
        ]
        for row_data in leaks:
            row = dict(base, **row_data)
            self.assertFalse(serve._passes_remote_only(row), row_data["company"])
            self.assertEqual(
                serve._remote_only_filter_reason(row), "not_remote",
                row_data["company"])
            self.assertEqual(
                serve._remote_status(row), "not_remote", row_data["company"])
        # Amenity on-site gyms stay non-signals (not a workplace negative).
        for desc in ("Access to on-site gyms for employees",
                     r"Access to on\-site gyms for employees"):
            row = dict(base, company="AmenityCo", location="", desc=desc)
            self.assertIsNone(serve._DESC_REMOTE_NEG_RE.search(desc), desc)
            self.assertEqual(
                serve._remote_only_filter_reason(row), "remote_unknown", desc)
        # Keep-cases: work-model Remote value; qualified telework positives.
        keeps = [
            ("Onsite/Remote: Remote", "Austin, TX"),
            ("100% telework", "Juneau, AK"),
            ("This position is eligible for full-time telework", "Frankfort, KY"),
        ]
        for desc, loc in keeps:
            row = dict(base, company="KeepCo", location=loc, desc=desc,
                       role="Engineer")
            self.assertTrue(serve._passes_remote_only(row), desc)
            self.assertIsNone(serve._remote_only_filter_reason(row), desc)
            self.assertEqual(serve._remote_status(row), "remote", desc)


class EmployerHide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_cfg = os.path.join(ROOT, "config.json")
        cls.real_jobs = os.path.join(ROOT, "data", "jobs.json")
        cls.real_cfg_bytes = open(cls.real_cfg, "rb").read()
        cls.real_jobs_bytes = open(cls.real_jobs, "rb").read()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg_path = os.path.join(cls.tmp.name, "config.json")
        shutil.copyfile(cls.real_cfg, cls.cfg_path)
        cls._orig_config_path = jobs_store._config_path
        jobs_store._config_path = lambda: cls.cfg_path
        cls.httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.H)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        jobs_store._config_path = cls._orig_config_path
        cls.tmp.cleanup()
        if open(cls.real_cfg, "rb").read() != cls.real_cfg_bytes:
            raise AssertionError("config.json was modified")
        if open(cls.real_jobs, "rb").read() != cls.real_jobs_bytes:
            raise AssertionError("jobs.json was modified")

    def setUp(self):
        # Seed a minimal temp config: keep live terms (asserted) but start with
        # an empty employers_hidden so live hides (e.g. jobgether) do not leak
        # into hide round-trip expectations.
        live = json.loads(self.real_cfg_bytes.decode("utf-8"))
        seed = {"terms": live.get("terms", []), "employers_hidden": []}
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(seed, f)

    def _post(self, key, hidden):
        req = urllib.request.Request(
            self.base + "/api/employers/hide",
            data=json.dumps({"key": key, "hidden": hidden}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_hide_round_trip_does_not_change_status(self):
        row = {"company": "Acme, Inc.", "role": "Site Reliability Engineer",
               "status": "discovered", "score": 12, "location": "United States"}
        code, body = self._post("Acme, Inc.", True)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["employers_hidden"], ["acme"])
        self.assertEqual(row["status"], "discovered")
        self.assertEqual(row["company"], "Acme, Inc.")
        hidden = serve._filter_ranked([row], {})
        self.assertEqual(hidden, [])
        self.assertEqual(row["status"], "discovered")
        shown = serve._filter_ranked([row], {"show_hidden": ["1"]})
        self.assertEqual(shown, [row])
        code, body = self._post("acme", False)
        self.assertEqual(code, 200)
        self.assertEqual(body["employers_hidden"], [])
        self.assertEqual(body["hidden"], False)
        back = serve._filter_ranked([row], {})
        self.assertEqual(back, [row])
        self.assertEqual(row["status"], "discovered")
        cfg = json.load(open(self.cfg_path, encoding="utf-8"))
        self.assertEqual(cfg["employers_hidden"], [])
        self.assertEqual(cfg["terms"], json.loads(self.real_cfg_bytes)["terms"])
        self.assertEqual(open(self.real_jobs, "rb").read(), self.real_jobs_bytes)

    def test_hide_alias_and_empty_key(self):
        code, body = self._post("General Dynamics Information Technology", True)
        self.assertEqual(code, 200)
        self.assertEqual(body["key"], "gdit")
        row = {"company": "GDIT, Inc.", "role": "Engineer", "status": "go", "score": None}
        self.assertEqual(serve._filter_ranked([row], {}), [])
        self.assertEqual(row["status"], "go")
        self.assertIsNone(row["score"])
        code, body = self._post("   ", True)
        self.assertEqual(code, 400)
        self.assertFalse(body["ok"])

    def test_api_job_and_ranked_omits_desc(self):
        # /ranked drops desc by default; desc=1 keeps it. /api/job returns desc
        # for one row, matched like /seen (url or apply_url, query stripped).
        jobs = json.loads(self.real_jobs_bytes.decode("utf-8")).get("queue") or []
        sample = next(
            (r for r in jobs
             if (r.get("url") or r.get("apply_url")) and (r.get("desc") or "").strip()),
            None)
        self.assertIsNotNone(sample, "jobs.json needs a row with url+desc")
        url = sample.get("url") or sample.get("apply_url")
        with urllib.request.urlopen(
                self.base + "/api/job?url=" + urllib.parse.quote(url, safe=""),
                timeout=30) as r:
            body = json.loads(r.read().decode())
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("desc"), sample.get("desc", ""))
        self.assertEqual(body.get("company"), sample.get("company", ""))
        # Query string on the url still matches.
        with urllib.request.urlopen(
                self.base + "/api/job?url=" + urllib.parse.quote(
                    url + "?utm=1", safe=""),
                timeout=30) as r:
            body_q = json.loads(r.read().decode())
        self.assertEqual(body_q.get("desc"), sample.get("desc", ""))
        try:
            urllib.request.urlopen(
                self.base + "/api/job?url=" + urllib.parse.quote(
                    "https://example.invalid/no-such-job", safe=""),
                timeout=30)
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
        # Tiny /ranked slice: no desc key unless desc=1.
        with urllib.request.urlopen(
                self.base + "/ranked?status=applied&us_only=1", timeout=60) as r:
            ranked = json.loads(r.read().decode())
        for job in ranked.get("jobs") or []:
            self.assertNotIn("desc", job)
        with urllib.request.urlopen(
                self.base + "/ranked?status=applied&us_only=1&desc=1",
                timeout=60) as r:
            ranked_desc = json.loads(r.read().decode())
        if ranked_desc.get("jobs"):
            self.assertIn("desc", ranked_desc["jobs"][0])


if __name__ == "__main__":
    unittest.main()
