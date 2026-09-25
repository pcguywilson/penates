#!/usr/bin/env python3
"""Rank discovered jobs by PROFILE-RELATIVE fit (rule-based, no LLM).

Scores each row 0-100 against the user's actual profile.yaml, not by absolute
keyword counts. The old scorer counted JD skill hits in the absolute, so a
100%-GCP role scored 100 even with zero GCP, and perfect AWS/DevOps fits sat at
0 on a title miss. This version uses required-COVERAGE (matched / required, the
Resume-Matcher polarity) + gap penalties + career-ops section-aware extraction,
so a pure-GCP role scores LOW and a real AWS/k8s/Terraform fit scores HIGH.

Every row also gets a human-readable `score_reason` so the user can disagree with
the number. Co-designed with Grok (board seq 24-39); reference approach borrowed
from career-ops + srbhr/Resume-Matcher. No per-role hardcoding: HAVE/GAP come
from profile.yaml via skills.py.

  python rank.py            # score all 'discovered', show top 30
  python rank.py --top 50
  python rank.py --all      # score every row
  python rank.py --why URL  # explain one row's score
  python rank.py --smoke    # run the co-designed smoke checks (no save)
"""
import argparse, re
import jobs_store, skills

# --- title band -------------------------------------------------------------
CORE = re.compile(r"site reliability|\bsre\b|devops|devsecops|platform engineer|"
                  r"infrastructure engineer|cloud engineer|cloud infrastructure|"
                  r"systems engineer|reliability engineer|cloud architect|"
                  r"solutions architect|infrastructure architect", re.I)
NEAR = re.compile(r"systems? admin|systems? analyst|\blinux\b|sysadmin|"
                  r"network engineer|automation engineer|build engineer|"
                  r"release engineer|cloud operations|it engineer", re.I)
OFF  = re.compile(r"\bsales\b|account executive|recruiter|marketing|\bnurse\b|"
                  r"data scientist|data analyst|\bml\b engineer|machine learning|"
                  r"front[- ]?end|frontend|ux|ui designer|product manager|"
                  r"customer success|support (?:engineer|specialist)|\bqa\b|"
                  r"business analyst|project manager|scrum master|technician", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|staff|lead|principal|iv|iii)\b", re.I)
JUNIOR = re.compile(r"\b(jr\.?|junior|associate|intern|entry[- ]level)\b", re.I)

# --- constraint signals still scored until the B filter toggles land ---------
# Location / remote / WV-exclusion HARD-CAPS were REMOVED: those are structured FILTERS
# now (serve.py params + dashboard checkboxes), not fit-score factors. This is what fixes
# the MaintainX misfire (a remote=True row that an "onsite" word in the JD was capping).
# Only TS/SCI and degree remain as in-score signals; they move to filter toggles next.
TSSCI    = re.compile(r"\bts/sci\b|\bts\s*sci\b|top secret|\bpolygraph\b|\bpoly\b|"
                      r"\bsci\b clearance|full[- ]scope", re.I)
DEGREE   = re.compile(r"bachelor|master(?:'s|s)?\s+degree|\bb\.?s\.?\b required|"
                      r"degree in|graduate degree|phd", re.I)
MY_CLOUDS = {"AWS", "AWS GovCloud", "Azure", "Azure Government"}
COMPETITOR_CLOUDS = {"GCP"}

def _title_band(role):
    band, tags = 10, []
    if CORE.search(role):
        band, tags = 30, ["CORE"]
    elif NEAR.search(role):
        band, tags = 18, ["NEAR"]
    elif OFF.search(role):
        band, tags = 0, ["off-target"]
    else:
        tags = ["neutral"]
    if JUNIOR.search(role):
        band = min(band, 12) - 5; tags.append("junior")
    elif SENIOR.search(role):
        tags.append("senior")
    return max(0, band), tags

def _fmt(s, n=4):
    s = sorted(s)
    return (",".join(s[:n]) + ("+%d" % (len(s) - n) if len(s) > n else "")) if s else "-"

def score_row(r, HAVE, GAP, SUPP):
    role = (r.get("role") or "")
    desc = r.get("desc") or ""
    band, tags = _title_band(role)

    required, preferred, found = skills.extract_jd_skills(desc)
    rcov, rmatch, rsupp, rgap = skills.coverage(required, HAVE, SUPP)
    pcov, pmatch, psupp, pgap = skills.coverage(preferred, HAVE, SUPP)

    prof_blob = " ".join(sorted(HAVE | SUPP)) + " " + skills.supported_blob()
    jac = skills.jaccard(role + " " + skills.clean_desc(desc), prof_blob)

    reason = ["band=%s(%d)" % ("/".join(tags), band)]

    if found and required:
        # near-linear: required-coverage carries the score, the title band is the floor.
        # Sparse-required damping: a JD whose parsed "required" set is only 1-2 skills is weak
        # evidence (thin or badly-parsed section) - 100% coverage of it must NOT score like a real
        # 4+ skill match. Confidence scales with the required-set size (1->.25 .. 4+->1.0), so a
        # lone [SQL] "requirement" can't reach 90 the way an 8-skill AWS/DevOps match does.
        conf = min(len(required), 4) / 4.0
        score = band + rcov * 60 * conf + pcov * 8 + jac * 8
        reason.append("req cov=%.0f%%%s have[%s]" % (rcov * 100,
                      "" if conf == 1.0 else " conf=%.2f" % conf, _fmt(rmatch | rsupp)))
    elif preferred:
        # thin/flat JD: no requirements section to trust -> soft preferred signal only
        score = band + pcov * 34 + jac * 12
        reason.append("thin-jd cov=%.0f%% have[%s]" % (pcov * 100, _fmt(pmatch | psupp)))
    else:
        score = band + jac * 8
        reason.append("title-only")

    # additive gap penalty: only skills the user is KNOWN to lack (GAP), already peer-collapsed
    # so a competing cloud/CI tool that is one-of-many in the JD is NOT counted as a gap.
    named_gaps = rgap & GAP
    if found and named_gaps:
        pen = min(len(named_gaps) * 6, 20)
        score -= pen
        reason.append("GAP[%s]-%d" % (_fmt(named_gaps), pen))

    # pure competitor-cloud shop: a GAP cloud is named and NONE of the user's clouds appear
    jd_all = required | preferred
    if (COMPETITOR_CLOUDS & jd_all) and not (MY_CLOUDS & jd_all):
        score -= 15
        reason.append("competitor-cloud-only-15")

    # off-target title with zero skill signal stays low
    if "off-target" in tags and not (rmatch or rsupp or pmatch or psupp):
        score = min(score, 30)
        reason.append("off-target<=30")

    # transparency flags (NOT score factors) so the user can disagree with the number
    if r.get("desc_truncated"):
        reason.append("JD-TRUNCATED")
    if not found and (required or preferred or desc):
        reason.append("no-req-section")

    score = max(0, min(100, score))

    # --- remaining in-score signals (move to filter toggles in B) ------------
    if TSSCI.search(desc):
        score *= 0.2
        reason.append("CAP[TS/SCI]")
    if found and DEGREE.search(desc):
        score = max(0, score - 5)
        reason.append("degree-5")

    return int(round(max(0, min(100, score)))), " ".join(reason)

def _score_all(rows):
    HAVE, GAP, SUPP = skills.have_set(), skills.gap_set(), skills.supported_set()
    for r in rows:
        r["score"], r["score_reason"] = score_row(r, HAVE, GAP, SUPP)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show-all", action="store_true")
    ap.add_argument("--why", help="explain the score for the row with this apply_url/url substring")
    ap.add_argument("--smoke", action="store_true", help="run co-designed smoke checks, no save")
    a = ap.parse_args()

    if a.smoke:
        return _smoke()

    d = jobs_store.load(); q = d["queue"]
    if a.why:
        HAVE, GAP, SUPP = skills.have_set(), skills.gap_set(), skills.supported_set()
        for r in q:
            if a.why in (r.get("apply_url") or "") or a.why in (r.get("url") or ""):
                sc, rs = score_row(r, HAVE, GAP, SUPP)
                print("%s @ %s\n  score=%d\n  %s" % (r.get("role"), r.get("company"), sc, rs))
                return
        print("no row matched", a.why); return

    rows = q if a.all else [r for r in q if r.get("status") == "discovered"]
    _score_all(rows)
    jobs_store.save(d)
    if not a.show_all:
        rows = [r for r in rows if jobs_store.applyable(r)]
    rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)
    label = "all" if a.all else "discovered"
    print("Top %d of %d %s jobs:\n" % (min(a.top, len(rows)), len(rows), label))
    for r in rows[:a.top]:
        print("  %3d  %-18s %-38s  %s" % (r.get("score") or 0,
              (r.get("company") or "")[:18], (r.get("role") or "")[:38], r.get("ats") or ""))
        print("       %s" % (r.get("score_reason") or ""))
    print("\nScores saved. Triage the top ones; GET /jobs?status=discovered for all.")

def _smoke():
    HAVE, GAP, SUPP = skills.have_set(), skills.gap_set(), skills.supported_set()
    cases = [
      ("GCP-only (no AWS)", {"role": "Cloud Engineer", "desc":
        "Requirements:\n- 5+ years on Google Cloud Platform (GCP)\n- BigQuery, GKE, and Terraform on GCP\n- Go programming\nResponsibilities:\n- Build data pipelines"},
        lambda s: s <= 30),
      ("Strong AWS/DevOps fit", {"role": "Senior DevOps Engineer", "desc":
        "What we're looking for:\n- Deep AWS experience (EC2, VPC, IAM, S3)\n- Kubernetes and Docker in production\n- Terraform and Ansible\n- CI/CD with GitHub Actions\n- Linux administration and Python"},
        lambda s: s >= 75),
      ("Title-weak but AWS-rich", {"role": "Engineer II", "desc":
        "Required:\n- AWS GovCloud, Terraform, Kubernetes, Docker\n- NIST compliance, Wazuh, Nessus\n- Bash and Python scripting"},
        lambda s: s >= 60),
      ("Onsite text now score-neutral (filtered in B, not capped)", {"role": "Platform Engineer", "location": "New York, NY (onsite required)", "desc":
        "Required:\n- AWS, Kubernetes, Terraform\nThis role is on-site in our NYC office, no remote."},
        lambda s: s >= 60),
      ("Thin aggregator snippet", {"role": "Senior SRE", "desc":
        "Requires 8+ years in SRE and infrastructure, container orchestration, cloud expertise and Bash."},
        lambda s: 20 <= s <= 70),
    ]
    print("SMOKE (score_row):")
    ok = True
    for name, row, chk in cases:
        s, rs = score_row(row, HAVE, GAP, SUPP)
        p = chk(s)
        ok = ok and p
        print("  [%s] %-28s score=%3d  %s" % ("PASS" if p else "FAIL", name, s, rs))
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1

if __name__ == "__main__":
    main()
