"""Workday remote-facet selection (no HTTP). Shapes follow live CXS page-0 facets."""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sources import workday


def F(param, values):
    return {"facetParameter": param, "descriptor": param, "values": values}

def V(desc, vid, count=1):
    return {"descriptor": desc, "id": vid, "count": count}


LEIDOS = [F("jobFamilyGroup", [V("Engineering", "eng"), V("Information Technology", "it")]),
          F("locationMainGroup", [
              F("locationCountry", [V("United States of America", "usa"), V("United Kingdom", "uk")]),
              F("locations", [V("6314 Remote/Teleworker US", "r1"), V("Remote, US", "r2"),
                              V("Remote, United Kingdom", "ruk"), V("Reston VA", "reston")])])]
NVIDIA = [F("locationMainGroup", [
              F("locationHierarchy2", [V("Office", "off"), V("Remote", "rem")]),
              F("locationHierarchy1", [V("United States", "us"), V("Israel", "il")]),
              F("locations", [V("US, CA, Remote", "s1")])])]
CACI = [F("locationMainGroup", [F("locations", [V("Remote (Any State)", "any"), V("999 REMOTE", "999"),
                                                V("Chantilly VA", "ch")])])]
NONE = [F("locationMainGroup", [F("locations", [V("Austin TX", "a"), V("Remote, Canada", "c")])])]


class Facets(unittest.TestCase):
    def test_leidos_ors_us_remote_locations_only(self):
        self.assertEqual(workday.remote_us_facets(LEIDOS), {"locations": ["r1", "r2"]})

    def test_nvidia_remote_type_and_us(self):
        self.assertEqual(workday.remote_us_facets(NVIDIA), {"locationHierarchy2": ["rem"], "locationHierarchy1": ["us"]})

    def test_caci_any_state(self):
        self.assertEqual(workday.remote_us_facets(CACI), {"locations": ["any"]})

    def test_gdit_any_location_remote(self):
        gdit = [F("locationMainGroup", [F("locations", [V("Any Location / Remote", "anyloc"), V("USA VA Chantilly", "ch"),
                                                        V("999 REMOTE", "999"), V("Remote, United Kingdom", "uk")])])]
        self.assertEqual(workday.remote_us_facets(gdit), {"locations": ["anyloc"]})

    def test_no_remote_us_falls_back(self):
        self.assertIsNone(workday.remote_us_facets(NONE))
        self.assertIsNone(workday.remote_us_facets([]))


class SkipTerms(unittest.TestCase):
    def test_config_skip_terms(self):
        import jobs_store
        with mock.patch.object(jobs_store, "config_terms", return_value=["DevOps Engineer", "Systems Analyst"]), \
             mock.patch.object(jobs_store, "load_config", return_value={"workday_skip_terms": ["systems analyst"]}):
            self.assertEqual(workday._terms(), ["DevOps Engineer"])
        with mock.patch.object(jobs_store, "config_terms", return_value=["a", "b"]), \
             mock.patch.object(jobs_store, "load_config", return_value={}):
            self.assertEqual(workday._terms(), ["a", "b"])


class FetchPath(unittest.TestCase):
    def test_facet_rows_are_remote_and_skip_terms(self):
        pages = [{"total": 2000, "facets": CACI, "jobPostings": []},
                 {"total": 2, "jobPostings": [
                     {"title": "Senior DevOps Engineer", "externalPath": "/job/Remote-Any-State/Senior-DevOps_R1",
                      "locationsText": "Remote (Any State)"},
                     {"title": "Systems Engineer", "externalPath": "/job/Chantilly-VA/Sys_R2", "locationsText": "2 Locations"}]}]
        with mock.patch.object(workday.base, "http_post_json", side_effect=pages) as post, \
             mock.patch.object(workday, "_fetch_terms") as terms:
            rows = workday.fetch("caci.wd1/external", company="CACI")
        terms.assert_not_called()
        self.assertEqual(post.call_count, 2)
        self.assertEqual([r["role"] for r in rows], ["Senior DevOps Engineer", "Systems Engineer"])
        self.assertTrue(all(r["remote"] is True for r in rows))
        self.assertEqual(rows[1]["location"], "Remote, US; Chantilly VA")
        import scan_ats, yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "config", "companies.yml")
        if not os.path.exists(path):
            path = os.path.join(root, "config", "companies.example.yml")
        cfg = yaml.safe_load(open(path))
        dev = dict(rows[0]); self.assertTrue(scan_ats._keep(dev, cfg.get("title_filter"), cfg.get("location_filter")))
        self.assertEqual(post.call_args_list[1][0][1]["appliedFacets"], {"locations": ["any"]})

    def test_no_facets_uses_terms(self):
        with mock.patch.object(workday.base, "http_post_json", return_value={"total": 5, "facets": NONE, "jobPostings": []}), \
             mock.patch.object(workday, "_fetch_terms", return_value=["T"]) as terms:
            self.assertEqual(workday.fetch("x.wd1/site"), ["T"])
        terms.assert_called_once()

    def test_facet_error_uses_terms(self):
        with mock.patch.object(workday.base, "http_post_json", side_effect=RuntimeError("HTTP Error 429")), \
             mock.patch.object(workday, "_fetch_terms", return_value=["T"]):
            self.assertEqual(workday.fetch("x.wd1/site"), ["T"])


if __name__ == "__main__":
    unittest.main()
