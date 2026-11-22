#!/usr/bin/env python3
"""Unit tests for the profile-relative job-fit scorer (rank.py).

Slice A: config search title band, level ladder, push_down, Secret/degree caps,
thin-jd formula, MY_CLOUDS from profile. Injected search config — does not
depend on the live config.json.
"""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import skills, rank

# Fixed DevOps fixture so the pins do not depend on whoever's profile.yaml is on disk.
HAVE = {"AWS", "AWS GovCloud", "Active Directory", "Ansible", "Azure Government", "Bash", "CI/CD",
        "CloudFormation", "Docker", "EBS", "EC2", "GitHub Actions", "HAProxy", "IAM", "IaC",
        "Kubernetes", "Linux", "Load Balancers", "NIST", "Nagios", "Nessus", "PowerShell", "Python",
        "Route 53", "S3", "SQL", "Splunk", "Terraform", "VPC", "VPN", "WSUS", "Wazuh",
        "Windows Server", "YAML"}
GAP = {"Bazel", "FedRAMP", "GCP", "Go", "HIPAA", "Jenkins", "Ruby", "SOC 2", "TS/SCI"}
SUPP = {"Azure", "Intune", "PostgreSQL"}
# Phrase list is the fixture, not whoever's profile.yaml is on disk.
PHRASES = tuple(sorted(
    (s for s in HAVE if len(s) >= 4),
    key=lambda s: (-len(s), s.lower())))

DEVOPS_SEARCH = {
    "target_titles": [
        "site reliability", "SRE", "devops", "devsecops", "platform engineer",
        "infrastructure engineer", "cloud engineer", "cloud infrastructure",
        "systems engineer", "reliability engineer", "cloud architect",
        "solutions architect", "infrastructure architect",
    ],
    "related_titles": [
        "systems administrator", "systems admin", "systems analyst", "linux",
        "sysadmin", "network engineer", "automation engineer", "build engineer",
        "release engineer", "cloud operations", "it engineer",
    ],
    "push_down_titles": [
        "Data", "Machine Learning", "ML", "Database", "DBA", "DNS", "Firmware",
        "Embedded", "Mainframe", "SAP", "Salesforce", "Snowflake", "Databricks",
    ],
    "exclude_titles": ["Intern", "Junior", "Sales"],
    "level": "senior",
}

AWS_DESC = (
    "Requirements:\n"
    "- Deep AWS experience (EC2, VPC, IAM, S3)\n"
    "- Kubernetes and Docker in production\n"
    "- Terraform and Ansible\n"
    "- CI/CD with GitHub Actions\n"
    "- Linux administration and Python\n"
)


def score(role, desc="", search=None, clearance_level="None",
          degree="Associate", my_clouds=None, **kw):
    r = {"role": role, "desc": desc}
    r.update(kw)
    if search is None:
        search = DEVOPS_SEARCH
    if my_clouds is None:
        my_clouds = {"AWS", "AWS GovCloud", "Azure Government"}
    return rank.score_row(
        r, HAVE, GAP, SUPP, search=search,
        clearance_level=clearance_level, degree=degree, my_clouds=my_clouds,
        phrases=PHRASES)[0]


def score_full(role, desc="", search=None, clearance_level="None",
               degree="Associate", my_clouds=None, **kw):
    r = {"role": role, "desc": desc}
    r.update(kw)
    if search is None:
        search = DEVOPS_SEARCH
    if my_clouds is None:
        my_clouds = {"AWS", "AWS GovCloud", "Azure Government"}
    return rank.score_row(
        r, HAVE, GAP, SUPP, search=search,
        clearance_level=clearance_level, degree=degree, my_clouds=my_clouds,
        phrases=PHRASES)


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    assert cond, name


def _level_mult_only(role, level="senior", targets=True):
    search = dict(DEVOPS_SEARCH)
    search["level"] = level
    if not targets:
        search["target_titles"] = []
    search = rank._norm_search(search)
    apply_ladder = bool(search["target_titles"])
    mult, rung, _way = rank._level_mult(search["level"], role, apply_ladder)
    return mult, rung


def _specialty(role):
    return rank._specialty_mult(role, rank._norm_search(DEVOPS_SEARCH))


def main():
    # --- existing polarity pins (injected DevOps search) -------------------
    check("GCP-only role is low", score("Cloud Engineer",
        "Requirements:\n- GCP, BigQuery, GKE, Go\nResponsibilities: build pipelines") <= 30)
    check("AWS/devops fit is high", score("Senior DevOps Engineer", AWS_DESC) >= 75)
    check("title-weak AWS-rich competitive", score("Engineer II",
        "Required:\n- AWS GovCloud, Terraform, Kubernetes, Docker, NIST, Wazuh, Nessus, Bash, Python") >= 60)
    check("onsite text is score-neutral", score("Platform Engineer",
        "Required: AWS, Kubernetes, Terraform. This role is on-site in NYC, no remote.",
        location="New York, NY (onsite required)") >= 60)
    check("TS/SCI capped", score("Cloud Engineer",
        "Requirements: AWS, Kubernetes. Must hold an active TS/SCI clearance.") <= 20)
    hi = score("DevOps Engineer", "Required:\n- AWS, Terraform, Kubernetes")
    lo = score("DevOps Engineer", "Required:\n- AWS, Terraform, Kubernetes, Jenkins, GCP, HIPAA")
    check("required gaps lower the score", lo < hi)
    check("thin snippet unscored", score("Senior SRE",
        "Requires 8+ years in SRE and infrastructure, container orchestration and Bash.") is None)
    check("thin JD not crushed", 15 <= (score("Senior SRE",
        "We run AWS and Kubernetes with Terraform, Docker and GitHub Actions on Linux. You will "
        "automate deploys, own on-call, and write Python and Bash tooling for our platform team.") or 0) <= 100)
    check("preferred TS/SCI does not cap", score("Senior Systems Engineer",
        "Requirements:\n- AWS, Linux, Terraform, Ansible\n- Python and Bash\nPreferred:\n- TS/SCI clearance") >= 60)
    tssci_req = "Requirements: AWS, Kubernetes. Must hold an active TS/SCI clearance."
    sc_ts, rs_ts = score_full("Cloud Engineer", tssci_req, clearance_level="TS/SCI")
    check("TS/SCI holder is not capped", sc_ts > 20)
    check("TS/SCI flag absent when held", "FLAG[TS/SCI-required]" not in rs_ts)
    sc_top, rs_top = score_full(
        "Cloud Engineer", tssci_req, clearance_level="Top Secret")
    check("Top Secret holder is not capped", sc_top > 20)
    check("Top Secret flag absent when held", "FLAG[TS/SCI-required]" not in rs_top)
    check("empty desc unscored", score("DevOps Engineer", "") is None)

    # --- seq 350 level ladder (at level=senior) ----------------------------
    m, rung = _level_mult_only("Senior Nurse", "senior")
    check("Senior Nurse x1.0 at senior", m == 1.0 and rung == "senior")

    m, rung = _level_mult_only("Staff Engineer", "senior")
    check("Staff Engineer x0.7 at senior", m == 0.7 and rung == "staff")

    m, rung = _level_mult_only("Lead Platform Engineer", "senior")
    check("Lead x0.7 at senior (staff rung)", m == 0.7 and rung == "staff")

    m, rung = _level_mult_only("Director of Sales", "senior")
    check("Director of Sales x0.35 at senior", m == 0.35 and rung == "management")

    m, rung = _level_mult_only("Senior Staff Engineer", "senior")
    check("Senior Staff once -> staff x0.7", m == 0.7 and rung == "staff")

    m, rung = _level_mult_only("Junior Systems Engineer", "senior")
    check("Junior x0.6 at senior (2 below)", m == 0.6 and rung == "entry")

    m, rung = _level_mult_only("Accountant", "senior")
    check("bare Accountant x1.0 (no level word)", m == 1.0 and rung is None)

    m, rung = _level_mult_only("Associate Attorney", "senior")
    check("Associate Attorney unmapped x1.0", m == 1.0 and rung is None)

    m, rung = _level_mult_only("Engineer II", "senior")
    check("Engineer II unmapped x1.0", m == 1.0 and rung is None)

    m, rung = _level_mult_only("Engineer III", "senior")
    check("Engineer III unmapped x1.0", m == 1.0 and rung is None)

    # --- push_down word boundaries -----------------------------------------
    sm, sp = _specialty("Data Center Engineer")
    check("Data Center misses push_down Data", sm == 1.0 and sp is None)

    sm, sp = _specialty("Database Engineer")
    check("Database Engineer hits Database", sm == 0.6 and sp.lower() == "database")

    sm, sp = _specialty("Senior Data Engineer")
    check("Data Engineer hits Data", sm == 0.6 and sp.lower() == "data")

    # --- empty search -> neutral 0.75, level x1.0, no nurse OFF penalty -----
    empty = {"target_titles": [], "related_titles": [], "push_down_titles": [],
             "exclude_titles": [], "level": "senior"}
    sc, rs = score_full("Registered Nurse", AWS_DESC, search=empty)
    check("empty search neutral title tag", "title=neutral(x0.75)" in rs)
    check("empty search records no target titles", "titles[none-set]" in rs)
    check("empty search no level token", "level[" not in rs)
    check("empty search no nurse off-target crush", sc is not None and sc >= 40)

    # --- Secret clearance cap ----------------------------------------------
    secret_desc = (
        "Requirements:\n- AWS, Kubernetes, Terraform\n"
        "- Active Secret clearance required\n- Linux and Python"
    )
    sc_none, rs_none = score_full(
        "Cloud Engineer", secret_desc, clearance_level="")
    check("Secret required caps when profile has none", sc_none <= 20)
    check("Secret flag token present", "FLAG[Secret-required]<=20" in rs_none)

    sc_held, rs_held = score_full(
        "Cloud Engineer", secret_desc, clearance_level="Active Secret")
    check("Secret required no cap when profile has Secret", sc_held > 20)
    check("Secret flag absent when held", "FLAG[Secret-required]" not in rs_held)

    secrets_mgmt = (
        "Requirements:\n- AWS, Kubernetes, Terraform\n"
        "- Experience with secrets management (Vault, AWS Secrets Manager)\n"
        "- Linux and Python"
    )
    sc_sm, rs_sm = score_full(
        "Cloud Engineer", secrets_mgmt, clearance_level="")
    check("secrets management does not Secret-cap", "FLAG[Secret-required]" not in rs_sm)
    check("secrets management score not capped to 20", sc_sm > 20)

    # --- degree cap --------------------------------------------------------
    deg_desc = (
        "Requirements:\n- Bachelor's degree in Computer Science\n"
        "- AWS, Kubernetes, Terraform, Linux, Python"
    )
    sc_deg, rs_deg = score_full(
        "Cloud Engineer", deg_desc, degree="Associate")
    check("degree required caps Associate profile", sc_deg <= 25)
    check("degree flag token", "FLAG[degree-required]<=25" in rs_deg)

    deg_equiv = (
        "Requirements:\n- Bachelor's degree in Computer Science or equivalent experience\n"
        "- AWS, Kubernetes, Terraform, Linux, Python"
    )
    sc_eq, rs_eq = score_full(
        "Cloud Engineer", deg_equiv, degree="Associate")
    check("degree with equivalent wording no cap", "FLAG[degree-required]" not in rs_eq)

    sc_bs, rs_bs = score_full(
        "Cloud Engineer", deg_desc, degree="Bachelor of Science")
    check("bachelor profile no degree cap", "FLAG[degree-required]" not in rs_bs)

    # --- thin-jd formula: no min(1, eff/0.7); uses min(len(preferred),4)/4 ----
    thin = (
        "We run AWS and Kubernetes with Terraform, Docker and GitHub Actions on Linux. "
        "You will automate deploys, own on-call, and write Python and Bash tooling "
        "for our platform team every week."
    )
    sc_thin, rs_thin = score_full("Senior SRE", thin)
    check("thin-jd reason present", "thin-jd" in rs_thin)
    check("thin-jd scores", sc_thin is not None)

    # --- title band CORE / NEAR / neutral ----------------------------------
    sc_c, rs_c = score_full("DevOps Engineer", AWS_DESC)
    check("devops is CORE", "title=CORE(x1.00)" in rs_c)

    sc_n, rs_n = score_full("Network Engineer", AWS_DESC)
    check("network engineer is NEAR", "title=NEAR(x0.90)" in rs_n)

    sc_u, rs_u = score_full("Registered Nurse", AWS_DESC)
    check("nurse is neutral not off-target", "title=neutral(x0.75)" in rs_u)
    check("no OFF nurse crush", sc_u is not None and sc_u >= 40)

    # --- MY_CLOUDS empty -> no competitor penalty --------------------------
    gcp_desc = (
        "Requirements:\n- GCP, GKE, BigQuery, Terraform\n- Python\n"
        "Responsibilities:\n- Build pipelines"
    )
    sc_empty_cloud, rs_ec = score_full(
        "Cloud Engineer", gcp_desc, my_clouds=set())
    check("empty MY_CLOUDS no competitor penalty", "competitor-cloud-only" not in rs_ec)

    sc_with_cloud, rs_wc = score_full(
        "Cloud Engineer", gcp_desc, my_clouds={"AWS", "Azure"})
    check("nonempty MY_CLOUDS applies competitor penalty", "competitor-cloud-only-15" in rs_wc)

    # --- reason token for level --------------------------------------------
    sc_st, rs_st = score_full("Staff Platform Engineer", AWS_DESC)
    check("level reason names staff rung above", "level[staff x0.70 above]" in rs_st)
    sc_sr, rs_sr = score_full("Senior DevOps Engineer", AWS_DESC)
    check("senior at senior is match", "level[senior x1.00 match]" in rs_sr)
    sc_jr, rs_jr = score_full("Junior Systems Engineer", AWS_DESC)
    check("junior is below", "level[entry x0.60 below]" in rs_jr)

    print("ALL RANK TESTS PASSED")
    # Slice B phrase / half-credit / reason_text checks. Same process so the
    # rank gate runs them.
    import importlib.util
    phrase_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "test_skills_phrase.py")
    spec = importlib.util.spec_from_file_location("test_skills_phrase", phrase_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()


if __name__ == "__main__":
    main()
