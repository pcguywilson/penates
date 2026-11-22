#!/usr/bin/env python3
"""Slice B: HAVE phrase credit, limited_experience half credit, reason_text.

Injected profiles and phrase lists. Does not depend on live config.json
or profile.yaml.
"""
import io
import json
import os
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import jobs_store
import rank
import reasons
import serve
import skills

SEARCH = {
    "target_titles": ["devops", "systems engineer"],
    "related_titles": [],
    "push_down_titles": [],
    "level": "senior",
}

PHRASE_PROFILE = {
    "systems": ["Epic", "QuickBooks", "QuickBooks Online", "Go", "AD", "IT", "C", "R"],
    "languages": ["Python"],
    "cloud": ["AWS"],
}


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    assert cond, name


def _get(path):
    handler = serve.H.__new__(serve.H)
    handler.path = path
    handler.headers = {}
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 9)
    handler.requestline = "GET " + path
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler.do_GET()
    raw = handler.wfile.getvalue()
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        head, sep, body = raw.partition(b"\n\n")
    if not sep:
        raise AssertionError("no HTTP body from %s: %r" % (path, raw[:300]))
    return json.loads(body.decode("utf-8"))


def main():
    # --- HAVE phrases: Epic / QuickBooks, not short tokens -----------------
    hits = skills.phrase_skills_in(
        "Let's go ship Epic and talk IT, AD, C and R.", profile=PHRASE_PROFILE)
    check("Epic phrase-matches a JD line", "Epic" in hits)
    check("Go is not a free-string HAVE phrase", "Go" not in hits)
    check("AD is not a free-string HAVE phrase", "AD" not in hits)
    check("IT is not a free-string HAVE phrase", "IT" not in hits)
    check("C is not a free-string HAVE phrase", "C" not in hits)
    check("R is not a free-string HAVE phrase", "R" not in hits)

    verb = skills.phrase_skills_in("ready to go", profile=PHRASE_PROFILE)
    check("verb go is not free-string matched", "Go" not in verb)

    # Canon/alias still owns the language token.
    req_go, _, _ = skills.extract_jd_skills(
        "Requirements:\n- Go programming and Python\n", PHRASE_PROFILE)
    check("Go programming still matches via canon", "Go" in req_go)

    desc = (
        "Requirements:\n- Epic clinical systems\n- QuickBooks\n"
        "Preferred:\n- QuickBooks Online reporting\n"
    )
    req, pref, found = skills.extract_jd_skills(desc, PHRASE_PROFILE)
    check("found a requirements section", found)
    check("Epic lands in required", "Epic" in req)
    check("QuickBooks lands in required", "QuickBooks" in req)
    check("QuickBooks Online lands in preferred", "QuickBooks Online" in pref)
    check("Epic is not also preferred", "Epic" not in pref)

    # Longest first: the shorter phrase is inside the longer one, so only the
    # long form is credited on that line.
    req_long, _, _ = skills.extract_jd_skills(
        "Requirements:\n- QuickBooks Online for finance\n- Python\n",
        PHRASE_PROFILE)
    check("longest phrase QuickBooks Online credited", "QuickBooks Online" in req_long)
    check("shorter QuickBooks not also credited inside the long phrase",
          "QuickBooks" not in req_long)

    both = skills.extract_jd_skills(
        "Requirements:\n- QuickBooks and QuickBooks Online\n", PHRASE_PROFILE)[0]
    check("separate QuickBooks mention still credits", "QuickBooks" in both)
    check("separate QuickBooks Online mention still credits",
          "QuickBooks Online" in both)

    have = skills.have_set(PHRASE_PROFILE)
    supp = skills.supported_set(PHRASE_PROFILE)
    frac, matched, _, gaps = skills.coverage(req, have, supp)
    check("Epic credited like a canon skill", "Epic" in matched and "Epic" in have)
    check("QuickBooks credited like a canon skill", "QuickBooks" in matched)
    check("phrase hits are not gaps when they are HAVE",
          "Epic" not in gaps and "QuickBooks" not in gaps)
    check("required phrase coverage is full for those hits", frac == 1.0)

    # Same tokens through the scorer: owning Epic beats missing it.
    jd = (
        "Requirements:\n- Epic\n- AWS\n- Linux\n- Terraform\n"
        "- Kubernetes\n- Python\n"
    )
    owned = {
        "systems": ["Epic", "Linux"],
        "cloud": ["AWS"],
        "languages": ["Python"],
        "iac": ["Terraform"],
        "containers": ["Kubernetes"],
    }
    row = {"role": "DevOps Engineer", "desc": jd}
    have_owned = skills.have_set(owned)
    gap_owned = skills.gap_set(owned)
    supp_owned = skills.supported_set(owned)
    s_with, _ = rank.score_row(
        row, have_owned, gap_owned, supp_owned,
        search=SEARCH, clearance_level="Active Secret", degree="Associate",
        my_clouds={"AWS"}, profile=owned)
    s_without, _ = rank.score_row(
        row, have_owned - {"Epic"}, gap_owned, supp_owned,
        search=SEARCH, clearance_level="Active Secret", degree="Associate",
        my_clouds={"AWS"}, profile=owned)
    check("Epic HAVE raises the score", s_with is not None and s_without is not None
          and s_with > s_without)

    # --- limited_experience: half credit, not HAVE ------------------------
    lim = {
        "languages": ["Python", "Go"],
        "limited_experience": {
            "PostgreSQL": "surface level",
            "Go": "reading only",
        },
    }
    h_lim = skills.have_set(lim)
    s_lim = skills.supported_set(lim)
    check("limited PostgreSQL removed from HAVE", "PostgreSQL" not in h_lim)
    check("limited Go removed from HAVE", "Go" not in h_lim)
    check("Python stays HAVE", "Python" in h_lim)
    check("limited PostgreSQL is SUPPORTED", "PostgreSQL" in s_lim)
    check("limited Go is SUPPORTED", "Go" in s_lim)
    frac_pg, _, part_pg, _ = skills.coverage({"PostgreSQL"}, h_lim, s_lim)
    check("PostgreSQL half credit", frac_pg == 0.5 and "PostgreSQL" in part_pg)
    frac_go, _, part_go, _ = skills.coverage({"Go"}, h_lim, s_lim)
    check("Go limited_experience half credit", frac_go == 0.5 and "Go" in part_go)
    text = reasons.explain(
        {"role": "Data Engineer", "score": 50,
         "score_reason": "title=neutral(x0.75)",
         "desc": "Requirements:\n- PostgreSQL\n- Python\n- Linux\n- AWS\n"},
        h_lim, s_lim)
    check("reason names partly-have PostgreSQL",
          "PostgreSQL" in text and "partly have" in text)

    # --- gap_set: no hardcoded Go/Ruby ------------------------------------
    empty_gap = skills.gap_set({})
    check("empty profile gap has no Go", "Go" not in empty_gap)
    check("empty profile gap has no Ruby", "Ruby" not in empty_gap)
    listed = skills.gap_set({"limited_or_none": ["Go", "Ruby"]})
    check("Go is a gap only when the profile lists it", "Go" in listed)
    check("Ruby is a gap only when the profile lists it", "Ruby" in listed)

    # --- reason_text on /ranked and /api/job ------------------------------
    blank = {"role": "X", "score": None,
             "score_reason": "title=neutral(x0.75) UNSCORED(no usable desc)",
             "desc": ""}
    blank_text = serve._job_reason_text(blank, set(), set())
    check("desc-less unscored skips to the short reason",
          blank_text.startswith("Not scored"))
    check("desc-less reason matches explain",
          blank_text == reasons.explain(blank, set(), set()))

    sample = {
        "company": "Phrase Test Co",
        "role": "DevOps Engineer",
        "score": 80,
        "score_reason": "title=CORE(x1.00) level[senior x1.00 match]",
        "status": "applied",
        "ats": "greenhouse",
        "source": "test",
        "desc": "Requirements:\n- AWS, Terraform, Kubernetes, Linux, Python\n",
        "url": "https://example.test/phrase-job",
        "apply_url": "https://example.test/phrase-job",
        "location": "Remote - United States",
    }
    # /ranked and /api/job call have_set() themselves. Pass the same sets so
    # reason_text matches. A missing profile.yaml is {} on both sides.
    sample_have = skills.have_set()
    sample_supp = skills.supported_set()
    sample_text = serve._job_reason_text(sample, sample_have, sample_supp)
    check("scored row reason_text is a string", isinstance(sample_text, str) and
          len(sample_text) > 20)
    check("scored row reason mentions the title", "Title" in sample_text)

    orig_ranked = jobs_store.ranked
    orig_load = jobs_store.load
    jobs_store.ranked = lambda status="discovered", applyable_only=True: [sample]
    jobs_store.load = lambda: {"queue": [sample]}
    try:
        ranked = _get("/ranked?status=applied")
        job = _get("/api/job?url=" + urllib.parse.quote(sample["url"], safe=""))
    finally:
        jobs_store.ranked = orig_ranked
        jobs_store.load = orig_load

    check("/ranked returns one row", ranked.get("count") == 1 and len(ranked.get("jobs") or []) == 1)
    rj = ranked["jobs"][0]
    check("/ranked reason_text is a string", isinstance(rj.get("reason_text"), str)
          and rj["reason_text"] == sample_text)
    check("/ranked still omits desc by default", "desc" not in rj)
    check("/api/job reason_text is a string", isinstance(job.get("reason_text"), str)
          and job["reason_text"] == sample_text)
    check("/api/job still returns desc", job.get("desc") == sample["desc"])

    print("ALL SKILL PHRASE TESTS PASSED")


if __name__ == "__main__":
    main()
