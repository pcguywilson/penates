#!/usr/bin/env python3
"""Unit tests for the profile-relative job-fit scorer (rank.py + skills.py).
Pins the co-designed polarity so a future change can't silently reintroduce the
GCP-scores-100 bug. Runs offline (PyYAML + profile.yaml only)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import skills, rank

HAVE, GAP, SUPP = skills.have_set(), skills.gap_set(), skills.supported_set()

def score(role, desc="", **kw):
    r = {"role": role, "desc": desc}; r.update(kw)
    return rank.score_row(r, HAVE, GAP, SUPP)[0]

def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    assert cond, name

def main():
    # polarity: pure-GCP no-AWS required role scores low even with a CORE title
    check("GCP-only role is low", score("Cloud Engineer",
        "Requirements:\n- GCP, BigQuery, GKE, Go\nResponsibilities: build pipelines") <= 30)
    # strong AWS/devops fit with a real requirements section scores high
    check("AWS/devops fit is high", score("Senior DevOps Engineer",
        "What we're looking for:\n- AWS EC2 VPC IAM S3, Kubernetes, Docker, Terraform, Ansible, CI/CD GitHub Actions, Linux, Python") >= 75)
    # title-weak but AWS-rich still competitive via coverage
    check("title-weak AWS-rich competitive", score("Engineer II",
        "Required:\n- AWS GovCloud, Terraform, Kubernetes, Docker, NIST, Wazuh, Nessus, Bash, Python") >= 60)
    # location is a FILTER now, not a score cap: an onsite word no longer sinks a good fit
    # (serve.py remote_only + dashboard checkbox exclude it at list time instead)
    check("onsite text is score-neutral", score("Platform Engineer",
        "Required: AWS, Kubernetes, Terraform. This role is on-site in NYC, no remote.",
        location="New York, NY (onsite required)") >= 60)
    # TS/SCI required (Secret only) hard-capped
    check("TS/SCI capped", score("Cloud Engineer",
        "Requirements: AWS, Kubernetes. Must hold an active TS/SCI clearance.") <= 20)
    # named gap under Requirements penalized vs same skills under Preferred
    hi = score("DevOps Engineer", "Required:\n- AWS, Terraform, Kubernetes")
    lo = score("DevOps Engineer", "Required:\n- AWS, Terraform, Kubernetes, Jenkins, GCP, HIPAA")
    check("required gaps lower the score", lo < hi)
    # thin/flat JD: no requirements section -> no required-gap crush, mid range
    # a one-line snippet with <3 skills is UNSCORED (None), not a fake mid score
    check("thin snippet unscored", score("Senior SRE",
        "Requires 8+ years in SRE and infrastructure, container orchestration and Bash.") is None)
    check("thin JD not crushed", 15 <= (score("Senior SRE",
        "We run AWS and Kubernetes with Terraform, Docker and GitHub Actions on Linux. You will "
        "automate deploys, own on-call, and write Python and Bash tooling for our platform team.") or 0) <= 100)
    check("preferred TS/SCI does not cap", score("Senior Systems Engineer",
        "Requirements:\n- AWS, Linux, Terraform, Ansible\n- Python and Bash\nPreferred:\n- TS/SCI clearance") >= 60)
    check("empty desc unscored", score("DevOps Engineer", "") is None)
    print("ALL RANK TESTS PASSED")

if __name__ == "__main__":
    main()
