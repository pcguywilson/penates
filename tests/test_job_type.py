"""Job type and workplace bucket for /ranked.

Run: python tests/test_job_type.py
"""
import os
import sys
import unittest

_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)
sys.path.insert(0, _root)
import builtin
import serve


def _row(**kw):
    base = {"role": "Engineer", "location": "", "workplace": "", "desc": "",
            "source": "jobspy:indeed"}
    base.update(kw)
    return base


class JobType(unittest.TestCase):
    def test_ziventra_contract_label_beats_full_time(self):
        # JSON-LD FULL_TIME is stored; Dover-style label in the desc wins.
        # The staffing blurb alone must not be what flips it.
        row = _row(
            company="Ziventra AI Ventures", role="Devops Engineer",
            source="builtin", location="United States", workplace="Remote",
            job_type="full_time",
            desc=("We are a contract staffing firm.\n\n"
                  "Employment Type: Contract\n\n"
                  "Build platforms on AWS."))
        self.assertEqual(serve._job_type(row), "contract")
        blurb = _row(
            company="Ziventra AI Ventures", role="Devops Engineer",
            source="builtin", location="United States",
            desc="Ziventra offers contract staffing.")
        self.assertEqual(serve._job_type(blurb), "unknown")
        stored = dict(blurb, job_type="full_time",
                      desc="We provide contract staffing. Full-time engineers welcome.")
        self.assertEqual(serve._job_type(stored), "full_time")

    def test_software_people_duration_is_contract_not_hybrid(self):
        row = _row(
            company="Software People", role="Systems Administrator",
            location="Remote, US",
            desc=("**Duration: 12\\+ months**\n"
                  "Microsoft Certified: Windows Server Hybrid Administrator\n"
                  "Work Location: Remote"))
        self.assertEqual(serve._job_type(row), "contract")
        self.assertFalse(serve._is_hybrid(row))
        self.assertEqual(serve._workplace(row), "remote")

    def test_stored_contract_beats_full_time_prose(self):
        row = _row(job_type="contract",
                   desc="This is a full-time permanent role.")
        self.assertEqual(serve._job_type(row), "contract")
        self.assertEqual(serve._stored_job_type("fulltime"), "full_time")
        self.assertEqual(serve._stored_job_type("PART_TIME"), "part_time")
        self.assertEqual(serve._stored_job_type("CONTRACTOR"), "contract")
        self.assertEqual(serve._stored_job_type(["FULL_TIME", "TEMPORARY"]), "contract")
        self.assertEqual(serve._stored_job_type("internship"), "part_time")
        self.assertEqual(serve._stored_job_type(""), "")

    def test_title_contract_forms_skip_bare_contract(self):
        self.assertEqual(serve._job_type(_row(role="DevOps Engineer (Contract)")), "contract")
        self.assertEqual(serve._job_type(_row(role="SRE contract-to-hire")), "contract")
        self.assertEqual(serve._job_type(_row(role="Contract Specialist")), "unknown")
        self.assertEqual(serve._job_type(_row(role="Engineer, no contract required")), "unknown")
        self.assertEqual(
            serve._job_type(_row(desc="Pay is hourly. This contract rate is $70/hr.")),
            "unknown")
        self.assertEqual(
            serve._job_type(_row(desc="This is a contract position.")), "contract")
        self.assertEqual(serve._job_type(_row(desc="C2C / corp-to-corp")), "contract")
        self.assertEqual(serve._job_type(_row(desc="1099 only")), "contract")

    def test_full_time_filter_keeps_unknown_drops_contract_and_part_time(self):
        unknown = _row(role="Site Reliability Engineer", location="Remote")
        contract = _row(role="DevOps Engineer (Contract)")
        part = _row(role="Systems Administrator (Part-time)")
        full = _row(role="Platform Engineer", job_type="full_time")
        self.assertEqual(serve._job_type(unknown), "unknown")
        rows = [unknown, contract, part, full]
        kept = serve._filter_ranked(rows, {"job_type": ["full_time"]})
        self.assertEqual(kept, [unknown, full])
        only = serve._filter_ranked(rows, {"job_type": ["contract"]})
        self.assertEqual(only, [contract])
        self.assertEqual(serve._filter_ranked(rows, {}), rows)

    def test_part_time_benefits_sentence_is_not_the_job_type(self):
        row = _row(job_type="full_time",
                   desc="Part-time employees accrue PTO on a different schedule.")
        self.assertEqual(serve._job_type(row), "full_time")


class Workplace(unittest.TestCase):
    def test_fairfax_hybrid_remote_in_place(self):
        row = _row(
            company="", role="DevOps Engineer", location="Fairfax, VA, US",
            remote_hint=True,
            desc="Work Location: Hybrid remote in Fairfax, VA 22031")
        self.assertTrue(serve._is_hybrid(row))
        self.assertEqual(serve._workplace(row), "hybrid")

    def test_switch_boilerplate_is_onsite_not_hybrid(self):
        menu = ("Whether in\\-office, hybrid, or fully remote, we offer the "
                "technology and support to keep teams connected and productive.")
        duty = ("Maintain reliable transportation and be able to work onsite "
                "at a Switch data center when needed.")
        row = _row(
            company="Switch, Ltd.", role="Systems Administrator I",
            location="Las Vegas, NV, US", remote_hint=True,
            desc=duty + "\n" + menu)
        self.assertFalse(serve._is_hybrid(row))
        self.assertEqual(serve._workplace(row), "onsite")
        menu_only = _row(company="Switch, Ltd.", role="Systems Administrator I",
                         location="Las Vegas, NV, US", desc=menu)
        self.assertFalse(serve._is_hybrid(menu_only))
        self.assertEqual(serve._workplace(menu_only), "onsite")

    def test_direct_onsite_when_needed_is_hybrid(self):
        row = _row(
            role="Systems Administrator", location="Las Vegas, NV",
            desc="You will work onsite at the data center when needed.")
        self.assertTrue(serve._is_hybrid(row))
        self.assertEqual(serve._workplace(row), "hybrid")
        # Same phrase with a fully-remote alternative stays remote.
        alt = _row(
            role="Systems Administrator", location="Las Vegas, NV",
            desc=("You will work onsite at the data center when needed. "
                  "This role is also fully remote."))
        self.assertFalse(serve._is_hybrid(alt))

    def test_instrumental_remote_or_hybrid_is_not_remote(self):
        for role in ("Site Reliability Engineer",
                     "Senior Site Reliability Engineer (SRE)"):
            row = _row(
                company="Instrumental", role=role, source="builtin",
                location="", workplace="Remote or Hybrid",
                desc="Operate the AWS platform. U.S. government contracts.")
            self.assertTrue(serve._is_hybrid(row), role)
            self.assertEqual(serve._workplace(row), "hybrid", role)
            self.assertEqual(serve._filter_ranked([row], {"remote_only": ["1"]}), [])
            self.assertEqual(
                serve._filter_ranked([row], {"workplace": ["remote"]}), [])
            self.assertEqual(
                serve._filter_ranked([row], {"workplace": ["hybrid"]}), [row])

    def test_hybrid_cloud_stays_remote(self):
        row = _row(
            role="Hybrid Cloud Engineer", location="Remote, US",
            desc="This is a fully remote role working on hybrid cloud infrastructure.")
        self.assertFalse(serve._is_hybrid(row))
        self.assertEqual(serve._workplace(row), "remote")
        self.assertEqual(serve._filter_ranked([row], {"workplace": ["remote"]}), [row])

    def test_n_days_in_office_and_hybrid_schedule(self):
        days = _row(location="Austin, TX",
                    desc="Expect 3 days a week in the office.")
        self.assertEqual(serve._workplace(days), "hybrid")
        sched = _row(location="Remote", desc="Hybrid schedule, Tue-Thu.")
        self.assertEqual(serve._workplace(sched), "hybrid")

    def test_remote_only_still_keeps_unknown_only_when_asked(self):
        unknown = _row(source="builtin", company="Akamai", role="Engineer",
                       location="United States", desc="")
        remote = _row(role="SRE (Remote)", location="Austin, TX", desc="")
        self.assertEqual(serve._workplace(unknown), "unknown")
        self.assertEqual(serve._workplace(remote), "remote")
        self.assertEqual(
            serve._filter_ranked([unknown, remote], {"remote_only": ["1"]}),
            [remote])
        both = serve._filter_ranked(
            [unknown, remote],
            {"remote_only": ["1"], "include_unknown": ["1"]})
        self.assertEqual(both, [unknown, remote])

    def test_workplace_or_list_and_filtered_surface(self):
        remote = _row(url="http://r", role="SRE (Remote)", location="Remote",
                      desc="This is a fully remote role.", score=1,
                      status="discovered")
        hybrid = _row(url="http://h", role="DevOps", location="Austin, TX",
                      desc="3 days per week in the office.", score=9,
                      status="filtered", filter_reason="not_remote")
        onsite = _row(url="http://o", role="Systems Administrator",
                      location="Dallas, TX", desc="On-site position.",
                      score=4, status="filtered", filter_reason="not_remote")
        unknown = _row(url="http://u", source="builtin", role="Engineer",
                       location="United States", desc="", score=2,
                       status="filtered", filter_reason="remote_unknown")
        queue = [remote, hybrid, onsite, unknown]
        surfaced = serve._surface_filtered_for_workplace(
            [remote], queue, {"workplace": ["hybrid,onsite"]})
        self.assertEqual({r["url"] for r in surfaced},
                         {"http://r", "http://h", "http://o", "http://u"})
        kept = serve._filter_ranked(surfaced, {"workplace": ["hybrid,onsite"]})
        self.assertEqual({r["url"] for r in kept}, {"http://h", "http://o"})
        with_u = serve._filter_ranked(
            surfaced, {"workplace": ["onsite"], "include_unknown": ["1"]})
        self.assertEqual({r["url"] for r in with_u}, {"http://o", "http://u"})
        # remote alone does not pull not_remote rows unless include_unknown
        # asks for remote_unknown.
        only_remote = serve._surface_filtered_for_workplace(
            [remote], queue, {"workplace": ["remote"]})
        self.assertEqual(only_remote, [remote])
        pulled = serve._surface_filtered_for_workplace(
            [remote], queue,
            {"remote_only": ["1"], "include_unknown": ["1"]})
        self.assertIn(unknown, pulled)
        self.assertNotIn(hybrid, pulled)
        for r in queue:
            self.assertIn(r["status"], ("discovered", "filtered"))


class Ingest(unittest.TestCase):
    def test_builtin_badge_keeps_remote_or_hybrid(self):
        loc, wp, remote, keep = builtin._geo(
            {"workplace": "Remote or Hybrid", "location": "United States"})
        self.assertTrue(keep)
        self.assertEqual(wp, "Remote or Hybrid")
        self.assertTrue(remote)
        loc, wp, remote, keep = builtin._geo(
            {"workplace": "Hybrid", "location": "Austin, TX"})
        self.assertFalse(keep)
        loc, wp, remote, keep = builtin._geo(
            {"workplace": "Remote", "location": ""})
        self.assertTrue(keep)
        self.assertEqual(wp, "Remote")

    def test_builtin_jsonld_employment_type(self):
        html = (
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"DevOps",'
            '"employmentType":"FULL_TIME",'
            '"description":"<p>Build things.</p>"}'
            '</script>')
        desc, trunc, jt = builtin._desc_from_html(html)
        self.assertIn("Build things", desc)
        self.assertFalse(trunc)
        self.assertEqual(jt, "full_time")
        html = html.replace("FULL_TIME", "CONTRACTOR")
        self.assertEqual(builtin._desc_from_html(html)[2], "contract")
        listed = (
            '<script type="application/ld&#x2B;json">'
            '{"@type":"JobPosting","employmentType":["FULL_TIME","TEMPORARY"],'
            '"description":"<p>Hi</p>"}'
            '</script>')
        self.assertEqual(builtin._desc_from_html(listed)[2], "contract")


if __name__ == "__main__":
    unittest.main()
