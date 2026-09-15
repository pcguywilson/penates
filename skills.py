#!/usr/bin/env python3
"""skills.py - profile-relative skill engine for job-fit scoring.

Single source of truth for how Penates recognizes skills in a JD and classifies
them against your own profile. Ported from a section-aware extraction approach
(skill-extract.mjs + jd-skill-gap.mjs) and Resume-Matcher's coverage tokenizer,
tailored to a cloud/DevOps profile and driven by profile.yaml so nothing about
the candidate is hardcoded here (the sanitized public repo ships profile.example.yaml).

HARD RULE (a hard-won lesson, and the reason a naive keyword scorer puts a
pure-GCP role at 100): NO umbrella aliases. 'cloud' / 'devops' / 'sre' /
'platform' / 'infrastructure' are NOT skills in this vocabulary - they never
credit AWS/GCP/Azure. Only concrete tools/services/languages count. Umbrella
words are handled by the title band in rank.py, never as skill fit.
"""
import re, html, functools

# --------------------------------------------------------------- canonical vocab
# surface spelling (lowercased) -> canonical display form. Curated cloud / devops /
# security / language set: the tools your target roles actually name, INCLUDING
# ones he lacks (GCP, Jenkins, ...) so a JD requiring them is recognized and can be
# scored as a gap. Longest surface forms are matched first (see _VOCAB_RE).
CANON = {
    # cloud providers (concrete only - never a bare 'cloud')
    "aws": "AWS", "amazon web services": "AWS",
    "govcloud": "AWS GovCloud", "aws govcloud": "AWS GovCloud",
    "gcp": "GCP", "google cloud": "GCP", "google cloud platform": "GCP",
    "azure": "Azure", "microsoft azure": "Azure",
    "azure government": "Azure Government", "azure gov": "Azure Government",
    # containers / orchestration
    "docker": "Docker", "containerd": "Docker",
    "kubernetes": "Kubernetes", "k8s": "Kubernetes",
    "eks": "EKS", "aks": "AKS", "gke": "GKE", "openshift": "OpenShift",
    "helm": "Helm", "ecs": "ECS", "fargate": "Fargate", "nomad": "Nomad",
    # infrastructure as code / config mgmt
    "terraform": "Terraform", "opentofu": "Terraform",
    "cloudformation": "CloudFormation", "cdk": "CDK", "pulumi": "Pulumi",
    "ansible": "Ansible", "puppet": "Puppet", "chef": "Chef", "saltstack": "SaltStack",
    "infrastructure as code": "IaC", "iac": "IaC",
    # ci/cd
    "jenkins": "Jenkins", "github actions": "GitHub Actions",
    "gitlab ci": "GitLab CI", "gitlab": "GitLab CI", "circleci": "CircleCI",
    "argocd": "ArgoCD", "argo cd": "ArgoCD", "flux": "Flux", "fluxcd": "Flux",
    "spinnaker": "Spinnaker", "azure devops": "Azure DevOps",
    "teamcity": "TeamCity", "bamboo": "Bamboo", "travis": "Travis CI",
    "ci/cd": "CI/CD", "cicd": "CI/CD", "gitops": "GitOps",
    # monitoring / observability
    "prometheus": "Prometheus", "grafana": "Grafana", "datadog": "Datadog",
    "new relic": "New Relic", "splunk": "Splunk", "elk": "ELK",
    "elasticsearch": "Elasticsearch", "kibana": "Kibana", "logstash": "Logstash",
    "nagios": "Nagios", "zabbix": "Zabbix", "cloudwatch": "CloudWatch",
    "opentelemetry": "OpenTelemetry", "sumo logic": "Sumo Logic",
    "wazuh": "Wazuh", "sentinel": "Microsoft Sentinel",
    "microsoft sentinel": "Microsoft Sentinel", "pagerduty": "PagerDuty",
    # security / compliance
    "nessus": "Nessus", "nist": "NIST", "pci dss": "PCI DSS", "pci-dss": "PCI DSS",
    "fedramp": "FedRAMP", "soc 2": "SOC 2", "soc2": "SOC 2", "soc ii": "SOC 2",
    "hipaa": "HIPAA", "iso 27001": "ISO 27001", "cissp": "CISSP",
    "ts/sci": "TS/SCI", "ts sci": "TS/SCI",
    "hl7": "HL7", "fhir": "FHIR", "loinc": "LOINC", "snomed": "SNOMED",
    # languages
    "python": "Python", "bash": "Bash", "shell scripting": "Bash",
    "powershell": "PowerShell", "go": "Go", "golang": "Go", "ruby": "Ruby",
    "java": "Java", "typescript": "TypeScript", "javascript": "JavaScript",
    "node.js": "Node.js", "nodejs": "Node.js", "sql": "SQL", "yaml": "YAML",
    "rust": "Rust", "c++": "C++", "c#": "C#", "php": "PHP", "scala": "Scala",
    "perl": "Perl", "groovy": "Groovy",
    # aws services
    "ec2": "EC2", "vpc": "VPC", "iam": "IAM", "s3": "S3", "ebs": "EBS",
    "route 53": "Route 53", "route53": "Route 53", "lambda": "Lambda",
    "rds": "RDS", "dynamodb": "DynamoDB", "cloudfront": "CloudFront",
    "load balancer": "Load Balancers", "load balancers": "Load Balancers",
    "elb": "Load Balancers", "alb": "Load Balancers", "api gateway": "API Gateway",
    "sqs": "SQS", "sns": "SNS", "waf": "WAF", "kms": "KMS",
    "secrets manager": "Secrets Manager", "bedrock": "Amazon Bedrock",
    "titan": "Amazon Titan",
    # systems / platforms
    "linux": "Linux", "windows server": "Windows Server", "sccm": "SCCM",
    "foreman": "Foreman", "katello": "Foreman", "iis": "IIS", "haproxy": "HAProxy",
    "vpn": "VPN", "nginx": "Nginx", "apache": "Apache",
    "active directory": "Active Directory", "vmware": "VMware", "hyper-v": "Hyper-V",
    "wsus": "WSUS",
    # databases
    "postgresql": "PostgreSQL", "postgres": "PostgreSQL", "mysql": "MySQL",
    "oracle": "Oracle", "mongodb": "MongoDB", "redis": "Redis",
    "cassandra": "Cassandra", "sql server": "SQL Server",
    # networking / concepts (concrete enough to be signal)
    "microservices": "Microservices", "serverless": "Serverless",
    "disaster recovery": "Disaster Recovery", "on-call": "On-call",
    "on call": "On-call", "observability": "Observability",
}

# Multi-word surfaces must be tried before their single-word substrings.
_SURFACES = sorted(CANON.keys(), key=len, reverse=True)
_VOCAB_RE = re.compile(
    r"(?<![A-Za-z0-9])(" +
    "|".join(re.escape(s) for s in _SURFACES) +
    r")(?![A-Za-z0-9])", re.I)

def canon_skills_in(text):
    """Return the set of canonical skills whose surface form appears in text."""
    if not text:
        return set()
    out = set()
    for m in _VOCAB_RE.finditer(text):
        c = CANON.get(m.group(1).lower())
        if c:
            out.add(c)
    return out

# --------------------------------------------------------------- section-aware JD parse
# Section-aware JD parsing. A REQUIRED header opens a requirements
# block; a PREFERRED header opens a lighter block; a NON-REQ header closes it.
_REQ_HEADER = re.compile(
    r"^\s{0,6}(?:required|requirements|qualifications|must[- ]?have|"
    r"what\s+we(?:'|’)?\s*re\s+looking\s+for|"
    r"what\s+you(?:(?:'|’)ll|\s+will)?\s+bring|who\s+you\s+are|about\s+you|"
    r"your\s+(?:background|experience|profile)|"
    r"you(?:(?:'|’)ll|\s+will)?\s+have|ideal\s+candidate|"
    r"skills\s+(?:and|&)\s+experience|basic\s+qualifications|minimum\s+qualifications)\b",
    re.I)
_PREF_HEADER = re.compile(
    r"^\s{0,6}(?:preferred|nice[- ]?to[- ]?have|bonus|"
    r"(?:it\s+would\s+be\s+)?(?:a\s+)?plus|"
    r"preferred\s+qualifications|nice\s+to\s+haves?|great\s+to\s+have)\b",
    re.I)
_NONREQ_HEADER = re.compile(
    r"^\s{0,6}(?:you\s+will(?!\s+have)|responsib|benefits?|perks?|compensation|"
    r"salary|pay\s+range|what\s+we\s+offer|why\s+(?:join|work)|"
    r"about\s+(?:us|the\s+company|the\s+team|the\s+role)|how\s+(?:and\s+where\s+)?we\s+work|"
    r"equal\s+opportunity|eeo|diversity|interview\s+process|how\s+to\s+apply|to\s+apply|"
    r"our\s+(?:stack|process|values|mission)|role\s+overview|the\s+opportunity)\b",
    re.I)

_BLOCK_TAG = re.compile(r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|section|header)\b[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")

def clean_desc(desc):
    """HTML/entity -> newline-delimited plain text so the line-based parser can see
    section headers that were <h3>/<strong>/<li> in the original posting."""
    if not desc:
        return ""
    t = _BLOCK_TAG.sub("\n", desc)
    t = _ANY_TAG.sub(" ", t)
    t = html.unescape(t)
    t = re.sub(r"\\([&\-.()\[\]+#])", r"\1", t)   # markdown backslash-escapes: \& -> &
    t = re.sub(r"\*\*|__|`", "", t)                     # bold / code emphasis
    t = re.sub(r"(?m)^\s{0,6}#{1,6}\s*", "", t)         # md heading marks -> plain header line
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return t

_INLINE_HEADERS = re.compile(
    r"(?<!\n)\s*("
    r"required|requirements|qualifications|must[- ]?haves?|basic qualifications|minimum qualifications|"
    r"what\s+we(?:'|\u2019)?\s*re\s+looking\s+for|what\s+you(?:(?:'|\u2019)ll|\s+will)?\s+bring|"
    r"who\s+you\s+are|about\s+you|your\s+(?:background|experience|profile)|"
    r"you(?:(?:'|\u2019)ll|\s+will)?\s+have|ideal\s+candidate|skills\s+(?:and|&)\s+experience|"
    r"preferred(?:\s+qualifications)?|nice[- ]?to[- ]?haves?|bonus(?:\s+points)?|"
    r"responsibilities|what\s+you(?:(?:'|\u2019)ll|\s+will)?\s+do|the\s+role|role\s+overview|"
    r"benefits?|perks?|compensation|what\s+we\s+offer|about\s+(?:us|the\s+company|the\s+team|the\s+role)|"
    r"why\s+(?:join|work)|equal\s+opportunity|how\s+to\s+apply|our\s+(?:stack|values|mission)"
    r")\s*:",
    re.I)

def _split_inline_headers(text):
    """These postings often run headers inline ('...Requirements: 5+ years...AWS...') with no line
    breaks. Put each 'Header:' on its own line so the section parser can see it."""
    return _INLINE_HEADERS.sub(lambda m: "\n" + m.group(1) + ":", text)


def extract_jd_skills(desc):
    """Return (required, preferred, found_req_section).

    Section-aware: skills under REQUIRED headers -> required; under PREFERRED ->
    preferred; NON-REQ closes the block. If no requirement/preferred header is
    ever found (thin/flat JD), EVERY recognized skill is returned as PREFERRED and
    found_req_section=False, so the caller applies no required-gap penalty
    (false-soft beats false-hard on thin aggregator blurbs)."""
    text = _split_inline_headers(clean_desc(desc))
    required, preferred = set(), set()
    found_req_section = False
    mode = None  # None | 'req' | 'pref'
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _NONREQ_HEADER.match(line):
            mode = None
            # a header line can still carry inline skills; fall through to scan it
        elif _PREF_HEADER.match(line):
            mode = "pref"; found_req_section = True
        elif _REQ_HEADER.match(line):
            mode = "req"; found_req_section = True
        skills = canon_skills_in(line)
        if not skills:
            continue
        if mode == "req":
            required |= skills
        elif mode == "pref":
            preferred |= skills
        # mode None: skills seen outside any block are held for the fallback below
    if not found_req_section:
        # thin/flat JD: no structure to trust -> everything is a soft (preferred) signal
        preferred = canon_skills_in(text)
        required = set()
    return required, preferred, found_req_section

# --------------------------------------------------------------- profile -> HAVE / GAP
_HAVE_KEYS = ["cloud", "iac", "containers", "cicd", "monitoring_security",
              "systems", "aws_services", "compliance_owned", "languages"]

@functools.lru_cache(maxsize=1)
def _profile():
    import yaml
    with open("profile.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def have_set():
    """Canonical skills you HAVE, from profile.yaml HAVE lists (+ derived: CI/CD
    from GitHub Actions, IaC from Terraform/Ansible)."""
    p = _profile()
    have = set()
    for k in _HAVE_KEYS:
        for item in (p.get(k) or []):
            have |= canon_skills_in(str(item)) or {_title(item)}
    if {"GitHub Actions", "GitLab CI", "Jenkins"} & have or "GitHub Actions" in have:
        have.add("CI/CD")
    if {"Terraform", "Ansible", "CloudFormation"} & have:
        have.add("IaC")
    return have

def gap_set():
    """Canonical skills you LACK (heavier penalty when REQUIRED), from
    profile.yaml limited_or_none plus Go/Ruby (no primary-language experience)."""
    p = _profile()
    gap = set()
    for item in (p.get("limited_or_none") or []):
        gap |= canon_skills_in(str(item)) or {_title(item)}
    gap |= {"Go", "Ruby"}
    return gap - have_set()  # a HAVE never doubles as a GAP

def supported_blob():
    """Lowercased prose from signature_projects + canonical_stories - a skill not in
    the HAVE lists but named here counts as SUPPORTED (0.5 of a HAVE)."""
    p = _profile()
    parts = list(p.get("signature_projects") or [])
    cs = p.get("canonical_stories") or {}
    if isinstance(cs, dict):
        parts += list(cs.values())
    return " ".join(str(x) for x in parts)

def supported_set():
    return canon_skills_in(supported_blob()) - have_set()

def _title(s):
    return str(s).strip()

# --------------------------------------------------------------- peer / one-of-many groups
# Members of a group are interchangeable ALTERNATIVES. If a JD's REQUIRED set names
# >=2 members of a group (multi-vendor, e.g. "AWS or Azure or GCP"), the group collapses
# to ONE coverage unit satisfied by ANY member the candidate has, and the unmet members
# are NOT gaps. A JD that names exactly ONE member (single-vendor shop, e.g. pure GCP)
# treats it as a normal skill, so a real gap still bites. No umbrella aliases (that rule
# is enforced in the vocab, not here).
PEER_GROUPS = [
    {"AWS", "AWS GovCloud", "GCP", "Azure", "Azure Government"},                 # cloud providers
    {"Jenkins", "GitHub Actions", "GitLab CI", "CircleCI", "Azure DevOps",
     "TeamCity", "Bamboo", "Travis CI", "ArgoCD", "Flux", "Spinnaker"},          # ci/cd runners
    {"Terraform", "CloudFormation", "CDK", "Pulumi"},                            # iac provisioning
    {"Ansible", "Puppet", "Chef", "SaltStack"},                                 # config mgmt
    {"Kubernetes", "EKS", "AKS", "GKE", "OpenShift", "Nomad", "ECS", "Fargate"},  # orchestration
    {"Prometheus", "Grafana", "Datadog", "New Relic", "Splunk", "ELK", "Nagios",
     "Zabbix", "CloudWatch", "OpenTelemetry", "Sumo Logic"},                     # monitoring
]

def coverage(required, HAVE, SUPP):
    """Peer-group-aware required coverage. Returns (frac, matched, supported, gaps).

    A multi-member peer group in `required` counts as ONE unit, credited fully when the
    candidate has any member (0.5 when only SUPPORTED); its unmet members are alternatives,
    not gaps. Every other required skill (including a lone group member) scores individually.
    frac is credit/units in 0..1, the Resume-Matcher coverage denominator with one-of-many
    collapsed so a multi-cloud JD does not gap the provider you happen to lack."""
    required = set(required)
    if not required:
        return 0.0, set(), set(), set()
    units = 0.0
    credit = 0.0
    matched, supported, gaps = set(), set(), set()
    consumed = set()
    for grp in PEER_GROUPS:
        inter = required & grp
        if len(inter) >= 2:                       # multi-vendor requirement -> one unit
            units += 1
            consumed |= inter
            have_hit = inter & HAVE
            supp_hit = inter & SUPP
            if have_hit:
                credit += 1.0; matched |= have_hit
            elif supp_hit:
                credit += 0.5; supported |= supp_hit
            else:
                gaps |= inter                      # whole group unmet -> alternatives are the gap
    for sk in required - consumed:                # singletons incl. lone group members
        units += 1
        if sk in HAVE:
            credit += 1.0; matched.add(sk)
        elif sk in SUPP:
            credit += 0.5; supported.add(sk)
        else:
            gaps.add(sk)
    return (credit / units if units else 0.0), matched, supported, gaps


# --------------------------------------------------------------- jaccard (Resume-Matcher)
_STOP = set("""a an the and or but if for with from into over this that these those of at by to in on off up down
role position job work working team company looking seeking required requirements responsibilities qualifications
preferred experience years year ability skills knowledge strong excellent good great well include including includes
must may like via etc will would could should our your you we they will have has are was were be been being do does
new using use used across within per about who what which when where why how all each every more most other some such""".split())
_TOK = re.compile(r"[a-z0-9+#./-]+", re.I)

def tokenize(text):
    """Hyphen/plus/hash-preserving tokens (keeps ci-cd, k8s, c++, .net), len>=3 or
    contains a digit, minus stopwords - Resume-Matcher's keyword-matcher approach."""
    out = set()
    for w in _TOK.findall((text or "").lower()):
        w = w.strip("./-")
        if not w:
            continue
        if (len(w) >= 3 or any(c.isdigit() for c in w)) and w not in _STOP and not w.isdigit():
            out.add(w)
    return out

def jaccard(a_text, b_text):
    a, b = tokenize(a_text), tokenize(b_text)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)
