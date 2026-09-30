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
CORE = re.compile(r"site reliability|\bsre\b|devops|devsecops|(?<!data )(?<!ml )(?<!ai )(?<!software )(?<!analytics )platform engineer|"
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

DATA_ML_TITLE = re.compile(r"\bdata\b|\bml\b|machine learning|\bai\b|analytics|databricks|\bllm", re.I)
INFRA_CORE = {"AWS", "AWS GovCloud", "Azure", "Azure Government", "GCP", "Kubernetes", "EKS", "AKS",
              "GKE", "Terraform", "Linux", "CI/CD", "Docker", "Ansible", "IaC"}
TITLE_MULT = {"CORE": 1.0, "NEAR": 0.9, "neutral": 0.75, "off-target": 0.45}
REQ_CLEAR = re.compile(r"\bts/sci\b|\bts\s*sci\b|top secret|polygraph|full[- ]scope", re.I)


def score_row(r, HAVE, GAP, SUPP):
    """Phase 1 (co-designed). Returns (score|None, reason).

    score = 100 * cov * conf * title_mult + min(pcov*8, 8) - gap_pen
      cov   = required credit / (known required units + unknown required tech, cap 8)
      conf  = min(known_required, 4)/4 (sparse-required damping)
      data/ML title with zero infra skills in required -> cov *= 0.5
    Thin JD (no requirements header): 60 * pcov * title_mult.
    No usable desc -> None ('unscored'), never a fake 30.
    TS/SCI/poly is a FLAG (clearance_flag) from the required block only, not a score factor."""
    role = (r.get("role") or "")
    desc = r.get("desc") or ""
    band, tags = _title_band(role)
    tkey = tags[0] if tags else "neutral"
    tmult = TITLE_MULT.get(tkey, 0.75)
    if tkey == "CORE" and "junior" in tags:
        tmult = 0.8
    reason = ["title=%s(x%.2f)" % ("/".join(tags), tmult)]

    required, preferred, found = skills.extract_jd_skills(desc)
    if not (required or preferred) or (len(skills.clean_desc(desc)) < 120 and len(required | preferred) < 3):
        reason.append("UNSCORED(no usable desc)")
        return None, " ".join(reason)

    unknown = skills.unknown_required(desc) if found else []
    rcov, rmatch, rsupp, rgap = skills.coverage(required, HAVE, SUPP)
    pcov, pmatch, psupp, pgap = skills.coverage(preferred, HAVE, SUPP)

    if found and (required or unknown):
        known_units = skills.coverage_units(required)
        credit = rcov * known_units
        units = known_units + len(unknown)
        cov = credit / units if units else 0.0
        conf = min(max(known_units, len(unknown) and 1), 4) / 4.0
        if DATA_ML_TITLE.search(role) and not (required & INFRA_CORE):
            cov *= 0.5
            reason.append("data/ml-title-no-infra x0.5")
        score = 90 * cov * conf * tmult + min(pcov * 10, 10)
        reason.append("req cov=%.0f%%%s have[%s]" % (cov * 100, "" if conf == 1.0 else " conf=%.2f" % conf,
                                                      _fmt(rmatch | rsupp)))
        if unknown:
            reason.append("unknown[%s]" % _fmt(set(unknown)))
    else:
        # Flat JD: every mentioned skill is 'preferred', alternatives included, so ~70% of the
        # mentioned set is a full fit. Unknown tech in the whole text still drags it.
        unk_all = skills.unknown_required("Requirements:\n" + skills.clean_desc(desc))
        eff = pcov * len(preferred) / (len(preferred) + len(unk_all)) if preferred else 0.0
        score = 85 * min(1.0, eff / 0.7) * tmult
        if DATA_ML_TITLE.search(role) and not (preferred & INFRA_CORE):
            score *= 0.5
            reason.append("data/ml-title-no-infra x0.5")
        reason.append("thin-jd cov=%.0f%% have[%s]" % (eff * 100, _fmt(pmatch | psupp)))
        if unk_all:
            reason.append("unknown[%s]" % _fmt(set(unk_all)))

    named_gaps = rgap & GAP
    if found and named_gaps:
        pen = min(len(named_gaps) * 6, 20)
        score -= pen
        reason.append("GAP[%s]-%d" % (_fmt(named_gaps), pen))

    jd_all = required | preferred
    if (COMPETITOR_CLOUDS & jd_all) and not (MY_CLOUDS & jd_all):
        score -= 15
        reason.append("competitor-cloud-only-15")

    if r.get("desc_truncated"):
        reason.append("JD-TRUNCATED")
    if REQ_CLEAR.search(skills.required_block_text(desc) if found else ""):
        # The profile holds Secret only. Until a clearance filter toggle exists, a REQUIRED TS/SCI/poly
        # stays a hard cap; preferred/"nice to have" TS/SCI no longer touches the score.
        score = min(score, 20)
        reason.append("FLAG[TS/SCI-required]<=20")
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
        lambda s: s is None or 20 <= s <= 70),
      ("Databricks/Spark required, AWS only in duties", {"role": "Senior Data Platform Engineer", "desc":
        "Requirements:\n- 5+ years with Databricks and Apache Spark\n- Python and SQL\n- Airflow orchestration\n"
        "Responsibilities:\n- Run workloads on AWS with Terraform and Kubernetes\n- Partner with analytics teams"},
        lambda s: s is not None and s < 40),
      ("TS/SCI in preferred does not cap", {"role": "Senior Systems Engineer", "desc":
        "Requirements:\n- AWS, Linux, Terraform, Ansible administration\n- Python and Bash scripting\n"
        "Preferred:\n- TS/SCI clearance\n- Master's degree"},
        lambda s: s is not None and s >= 60),
      ("Empty desc is unscored", {"role": "DevOps Engineer", "desc": ""}, lambda s: s is None),
    ]
    print("SMOKE (score_row):")
    ok = True
    for name, row, chk in cases:
        s, rs = score_row(row, HAVE, GAP, SUPP)
        p = chk(s)
        ok = ok and p
        print("  [%s] %-28s score=%4s  %s" % ("PASS" if p else "FAIL", name, s, rs))
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1

if __name__ == "__main__":
    main()
