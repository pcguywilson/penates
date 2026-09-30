"""employer_key normalization and employers_hidden round-trip.

Hide writes a temp config.json. The real config and jobs.json are only read
so a status byte can be compared. The loopback server is not the autofill server.
Run: python tests/test_employer.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
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
        with open(self.cfg_path, "wb") as f:
            f.write(self.real_cfg_bytes)

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


if __name__ == "__main__":
    unittest.main()
