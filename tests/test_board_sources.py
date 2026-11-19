"""Offline contract tests for the PR2 board adapters and the PR1 aggregator-link resolver.
Shapes recorded from live public endpoints on 2026-10-07 (no network here)."""
import os, sys, json, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sources import base, recruitee, teamtailor, breezy, bamboohr, jazzhr, paylocity
import board_resolve, scan_ats

KEYS = {"ats", "board", "ats_id", "source", "company", "role", "url", "apply_url", "location", "remote", "posted", "desc"}


class Adapters(unittest.TestCase):
    def tearDown(self):
        base.http_json = self._j; base.http_text = self._t

    def setUp(self):
        self._j, self._t = base.http_json, base.http_text

    def _contract(self, rows, ats):
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(set(r), KEYS)
            self.assertEqual(r["ats"], ats)
            self.assertEqual(r["source"], "ats-api")
            self.assertTrue(r["role"] and r["url"].startswith("https://"))

    def test_recruitee(self):
        base.http_json = lambda url, timeout=25: {"offers": [{
            "id": 9, "title": "Nurse Manager", "location": "Remote job", "city": "United States",
            "country": "United States", "remote": True, "published_at": "2026-10-07 14:12:33 UTC",
            "company_name": "Acme", "careers_url": "https://acme.recruitee.com/o/nurse-manager",
            "careers_apply_url": "https://acme.recruitee.com/o/nurse-manager/c/new",
            "description": "<p>Lead &amp; care</p>", "requirements": "<p>RN</p>"}]}
        r = recruitee.fetch("acme"); self._contract(r, "recruitee")
        self.assertTrue(r[0]["remote"]); self.assertEqual(r[0]["posted"], "2026-10-07")
        self.assertIn("Remote", r[0]["location"]); self.assertEqual(r[0]["desc"], "Lead & care RN")

    def test_teamtailor(self):
        base.http_text = lambda url, timeout=25: """<?xml version="1.0"?>
<rss xmlns:tt="https://teamtailor.com/locations"><channel><item>
<title>Accountant</title><description>&lt;p&gt;Books&lt;/p&gt;</description>
<pubDate>Tue, 06 Oct 2026 10:00:00 +0200</pubDate>
<link>https://acme.teamtailor.com/jobs/8455138-accountant</link><remoteStatus>fully</remoteStatus>
<guid>x</guid><tt:locations><tt:location><tt:city>Austin</tt:city><tt:country>United States</tt:country></tt:location></tt:locations>
</item></channel></rss>"""
        r = teamtailor.fetch("acme"); self._contract(r, "teamtailor")
        self.assertEqual(r[0]["ats_id"], "teamtailor:acme:8455138")
        self.assertTrue(r[0]["remote"]); self.assertEqual(r[0]["posted"], "2026-10-06")
        self.assertIn("Austin, United States", r[0]["location"]); self.assertEqual(r[0]["desc"], "Books")

    def test_breezy(self):
        base.http_json = lambda url, timeout=25: [{
            "id": "9d13", "name": "Paralegal", "url": "https://acme.breezy.hr/p/9d13-paralegal",
            "published_date": "2026-10-06T13:27:21.167Z", "company": {"name": "Acme LLC"},
            "location": {"name": "Reston, VA", "is_remote": False}}]
        r = breezy.fetch("acme"); self._contract(r, "breezy")
        self.assertIs(r[0]["remote"], False); self.assertEqual(r[0]["company"], "Acme LLC")
        self.assertEqual(r[0]["location"], "Reston, VA")

    def test_bamboohr(self):
        base.http_json = lambda url, timeout=25: {"meta": {}, "result": [
            {"id": "206", "jobOpeningName": "Teacher", "atsLocation": {"city": "Austin", "state": "Texas", "country": "United States"}, "isRemote": None, "locationType": "1"},
            {"id": "207", "jobOpeningName": "Chef", "atsLocation": {"city": "Austin"}, "isRemote": None, "locationType": "0"}]}
        r = bamboohr.fetch("acme"); self._contract(r, "bamboohr")
        self.assertTrue(r[0]["remote"]); self.assertTrue(r[0]["location"].startswith("Remote - Austin"))
        self.assertIs(r[1]["remote"], False)
        self.assertEqual(r[0]["url"], "https://acme.bamboohr.com/careers/206")

    def test_jazzhr(self):
        base.http_text = lambda url, timeout=25: """<ul class="list-group">
<li class="list-group-item"> <h3 class="list-group-item-heading"> <a href="https://acme.applytojob.com/apply/tMQ8bIm5SM/Electrician?source=x"> Electrician </a> </h3>
<ul class="list-inline list-group-item-text"> <li><i class="fa fa-map-marker"></i>Remote</li> </ul> </li>
<li class="list-group-item"> <h3 class="list-group-item-heading"> <a href="/apply/Q3YDt6EWRx/Driver"> Driver </a> </h3>
<ul class="list-inline list-group-item-text"> <li><i class="fa fa-map-marker"></i>Charleston, WV</li> </ul> </li>
</ul></div>"""
        r = jazzhr.fetch("acme"); self._contract(r, "jazzhr")
        self.assertEqual([x["role"] for x in r], ["Electrician", "Driver"])
        self.assertTrue(r[0]["remote"]); self.assertEqual(r[0]["url"], "https://acme.applytojob.com/apply/tMQ8bIm5SM/Electrician")
        self.assertEqual(r[1]["url"], "https://acme.applytojob.com/apply/Q3YDt6EWRx/Driver")
        self.assertEqual(r[1]["location"], "Charleston, WV")

    def test_jazzhr_served_quotes_and_no_location(self):
        # served HTML (2026-10-07): single-quoted heading/marker classes; a row without a location is dropped
        base.http_text = lambda url, timeout=25: (
            '<li class="list-group-item"> <h3 class=\'list-group-item-heading\'> '
            '<a href="https://acme.applytojob.com/apply/Ydt7cRPKGe/AEM-Engineer"> AEM Engineer </a> </h3> '
            "<ul class='list-inline list-group-item-text'> <li><i class='fa fa-map-marker'></i>Remote</li> </ul> </li>"
            '<li class="list-group-item"> <h3 class=\'list-group-item-heading\'> '
            '<a href="https://acme.applytojob.com/apply/Zzz7cRPKGe/No-Loc"> No Loc </a> </h3> </li></ul></div>')
        r = jazzhr.fetch("acme")
        self.assertEqual([x["role"] for x in r], ["AEM Engineer"])
        self.assertEqual(r[0]["location"], "Remote"); self.assertTrue(r[0]["remote"])

    def test_paylocity(self):
        pd = {"Jobs": [
            {"JobId": 4559694, "JobTitle": "UX Designer III", "LocationName": None, "PublishedDate": "2026-10-05T15:50:02-05:00",
             "Description": "<p>Design</p>", "IsInternal": False, "IsRemote": True, "JobLocation": {"City": None, "State": None, "Country": "USA"}},
            {"JobId": 1, "JobTitle": "Internal only", "IsInternal": True}]}
        base.http_text = lambda url, timeout=25: "<script>window.pageData = %s;\n</script>" % json.dumps(pd)
        r = paylocity.fetch("cb5f6748-2a33-4c5d-83a2-fb0a3911d7d5", company="Missing Link")
        self._contract(r, "paylocity"); self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["url"], "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4559694")
        self.assertTrue(r[0]["remote"]); self.assertEqual(r[0]["posted"], "2026-10-05"); self.assertEqual(r[0]["location"], "Remote - USA")

    def test_registered_and_harvestable(self):
        for a in ("recruitee", "teamtailor", "breezy", "bamboohr", "jazzhr", "paylocity"):
            self.assertIn(a, scan_ats.ADAPTERS)
        hits = {}
        for u in ["https://acme.recruitee.com/o/x", "https://acme.teamtailor.com/jobs/1-x", "https://acme.breezy.hr/p/1",
                  "https://acme.bamboohr.com/careers/2", "https://acme.applytojob.com/apply/AbCdEf12/x",
                  "https://recruiting.paylocity.com/Recruiting/Jobs/All/cb5f6748-2a33-4c5d-83a2-fb0a3911d7d5"]:
            for ats, rx in scan_ats._BOARD_RES:
                m = rx.search(u)
                if m:
                    hits[ats] = scan_ats._clean_slug(ats, m); break
        self.assertEqual(set(hits), {"recruitee", "teamtailor", "breezy", "bamboohr", "jazzhr", "paylocity"})
        self.assertEqual(hits["jazzhr"], "acme")


class _Resp:
    def __init__(self, url, code=200, text=""):
        self.url, self.status_code, self.text = url, code, text


class _Sess:
    def __init__(self, table):
        self.table, self.calls = table, []
    def get(self, url, **kw):
        self.calls.append(url)
        return self.table[url]


class Resolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._cache = board_resolve.CACHE
        board_resolve.CACHE = os.path.join(self.tmp, "board_resolve.json")
        board_resolve.time.sleep = lambda s: None

    def tearDown(self):
        board_resolve.CACHE = self._cache

    def test_builtin_hop_stamps_and_caches(self):
        u = "https://builtin.com/job/devops-engineer/11172732"
        hop = u + "?handler=ApplyRedirect"
        s = _Sess({hop: _Resp("https://job-boards.greenhouse.io/lightfeatheriollc/jobs/5446026008")})
        board_resolve._session = lambda: s
        row = {"company": "LightFeather", "url": u, "apply_url": hop}
        board_resolve.run({}, [row], pause=0)
        self.assertEqual(row["apply_url"], "https://job-boards.greenhouse.io/lightfeatheriollc/jobs/5446026008")
        self.assertEqual(row["ats"], "greenhouse")
        # cached: a second run makes no request and still re-points a fresh row
        row2 = {"company": "LightFeather", "url": u, "apply_url": hop}
        board_resolve.run({}, [row2], pause=0)
        self.assertEqual(len(s.calls), 1)
        self.assertTrue(row2["apply_url"].startswith("https://job-boards.greenhouse.io/"))
        self.assertIn(("LightFeather", "https://job-boards.greenhouse.io/lightfeatheriollc/jobs/5446026008"), board_resolve.boards())

    def test_blocked_host_backs_off_and_negative_is_cached(self):
        rows = [{"url": "https://builtin.com/job/x/%d" % i} for i in range(8)]
        table = {r["url"] + "?handler=ApplyRedirect": _Resp(r["url"], 403) for r in rows}
        s = _Sess(table); board_resolve._session = lambda: s
        board_resolve.run({}, rows, pause=0)
        self.assertEqual(len(s.calls), board_resolve.HOST_FAIL_LIMIT)   # stops hammering builtin
        board_resolve.run({}, rows, pause=0)
        self.assertEqual(len(s.calls), board_resolve.HOST_FAIL_LIMIT)   # negatives cached, host skipped

    def test_paylocity_detail_yields_board_not_new_apply(self):
        u = "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4559670"
        s = _Sess({u: _Resp(u, 200, '<a href="/Recruiting/Jobs/All/cb5f6748-2a33-4c5d-83a2-fb0a3911d7d5">All</a>')})
        board_resolve._session = lambda: s
        row = {"company": "Missing Link", "url": u, "apply_url": u}
        board_resolve.run({}, [row], pause=0)
        self.assertEqual(row["apply_url"], u)
        b = dict((v, k) for k, v in board_resolve.boards())
        self.assertIn("https://recruiting.paylocity.com/Recruiting/Jobs/All/cb5f6748-2a33-4c5d-83a2-fb0a3911d7d5", b)

    def test_linkedin_is_never_followed(self):
        self.assertIsNone(board_resolve.target({"url": "https://www.linkedin.com/jobs/view/1"}))


if __name__ == "__main__":
    unittest.main()
