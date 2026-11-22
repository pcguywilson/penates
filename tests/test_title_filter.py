#!/usr/bin/env python3
"""Discovery title gate (titles.keep) and scan_ats title wiring.

Injected search / title_filter. Does not depend on live config.json except
the one check that an empty search reads companies.yml.
"""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import titles

SEARCH = {
    "target_titles": ["devops", "systems engineer", "platform engineer",
                      "reliability engineer"],
    "related_titles": ["linux", "systems administrator"],
    "push_down_titles": ["Data", "Database", "ML"],
    "exclude_titles": ["Manager", "Intern", "Sales"],
}

YAML_TF = {
    "positive": ["DevOps", "Site Reliability"],
    "negative": ["Intern", "Director"],
}


class Keep(unittest.TestCase):
    def test_target_and_related_keep(self):
        self.assertTrue(titles.keep("Senior DevOps Engineer", search=SEARCH))
        self.assertTrue(titles.keep("Linux Administrator", search=SEARCH))
        self.assertTrue(titles.keep("Systems Administrator", search=SEARCH))
        self.assertFalse(titles.keep("Marketing Coordinator", search=SEARCH))

    def test_exclude_drops_word_boundary(self):
        # Same positive family; Manager drops, Management does not.
        self.assertFalse(titles.keep("DevOps Manager", search=SEARCH))
        self.assertFalse(titles.keep("Engineering Manager", search=SEARCH))
        self.assertTrue(titles.keep("Management Systems Engineer", search=SEARCH))
        self.assertFalse(titles.keep("DevOps Intern", search=SEARCH))
        self.assertFalse(titles.keep("Sales Engineer", search=SEARCH))

    def test_push_down_does_not_drop(self):
        self.assertTrue(titles.keep("Data Platform Engineer", search=SEARCH))
        self.assertTrue(titles.keep("Database Reliability Engineer", search=SEARCH))
        # push_down alone is not a keep list
        self.assertFalse(titles.keep("Snowflake Administrator", search=SEARCH))
        self.assertFalse(titles.keep("ML Engineer", search=SEARCH))

    def test_config_ignores_yaml_filter(self):
        trap = {"positive": ["Marketing"], "negative": ["DevOps"]}
        self.assertTrue(titles.keep("DevOps Engineer", search=SEARCH, title_filter=trap))
        self.assertFalse(titles.keep("Marketing Lead", search=SEARCH, title_filter=trap))

    def test_empty_search_uses_title_filter_not_config_exclude(self):
        empty = {"target_titles": [], "related_titles": [],
                 "exclude_titles": ["Zebra"], "push_down_titles": ["Data"]}
        self.assertTrue(titles.keep("DevOps Engineer", search=empty, title_filter=YAML_TF))
        self.assertTrue(titles.keep("Site Reliability Engineer", search=empty, title_filter=YAML_TF))
        self.assertFalse(titles.keep("Director of DevOps", search=empty, title_filter=YAML_TF))
        self.assertFalse(titles.keep("DevOps Intern", search=empty, title_filter=YAML_TF))
        self.assertFalse(titles.keep("Sales Engineer", search=empty, title_filter=YAML_TF))
        # exclude_titles / push_down are not a second source when search is empty
        self.assertTrue(titles.keep("DevOps Zebra", search=empty, title_filter=YAML_TF))
        self.assertTrue(titles.keep("Data Site Reliability Engineer", search=empty, title_filter=YAML_TF))

    def test_empty_search_reads_companies_yml(self):
        tf = titles.load_title_filter()
        self.assertIn("DevOps", tf.get("positive") or [])
        with mock.patch.object(titles, "load_title_filter", return_value=YAML_TF) as load:
            self.assertTrue(titles.keep("DevOps Engineer", search={}))
            self.assertFalse(titles.keep("Director of DevOps", search={}))
            load.assert_called()


class ScanUsesHelper(unittest.TestCase):
    def test_title_helper_and_location_filter(self):
        import scan_ats
        lf = {"positive": ["Remote", "United States"], "negative": ["India"]}
        # Would drop every systems/devops title if scan still used this YAML list.
        trap = {"positive": ["nope"], "negative": ["systems", "devops"]}
        with mock.patch.object(titles, "_load_search", return_value=SEARCH):
            self.assertTrue(scan_ats._keep(
                {"role": "Management Systems Engineer", "location": "Remote - United States"},
                trap, lf))
            self.assertTrue(scan_ats._keep(
                {"role": "Data DevOps Engineer", "location": "Remote, United States"},
                trap, lf))
            self.assertFalse(scan_ats._keep(
                {"role": "DevOps Manager", "location": "Remote - United States"},
                {"positive": ["DevOps Manager"], "negative": []}, lf))
            self.assertFalse(scan_ats._keep(
                {"role": "Engineering Manager", "location": "Remote - United States"},
                {"positive": ["Engineering"], "negative": []}, lf))
            self.assertFalse(scan_ats._match(
                "DevOps Engineer", "Bangalore, India", trap, lf))
            self.assertTrue(scan_ats._match(
                "DevOps Engineer", "Remote - United States", trap, lf))


if __name__ == "__main__":
    unittest.main()
