#!/usr/bin/env python3
"""Build answers.yaml (intent bank) from source_answers.json.

New in v2:
  - company_interest has three VARIANTS (mission / technology / general) so the
    same intent produces different framings, chosen per application.
  - intents may declare max_sentences / max_chars (machine-readable limits).
"""
import json, yaml

src = json.load(open("source_answers.json"))["job_application_answers"]

def find(substr):
    for qa in src:
        if substr.lower() in qa["question"].lower():
            return qa["answer"]
    raise SystemExit(f"source answer not found for: {substr!r}")

bank = {}

# ===== TEMPLATED =====
# {{WHY_COMPANY}} is one sentence written by Ollama from application.company_description.
# It must describe ONLY the target company (cross-contamination guard).
bank["company_interest"] = {
    "type": "template",
    "keywords": ["why do you want", "why are you interested", "what excites",
                 "what interests", "why work", "want to work at", "interested in working",
                 "join", "motivates you", "spark", "attracted", "drew you",
                 "made you apply", "what made you", "why apply", "apply to this",
                 "apply to the", "excited to apply"],
    "variants": {
        "technology": (
            "I'm interested in {{COMPANY}} because {{WHY_COMPANY}} My background in cloud "
            "infrastructure, automation, reliability, and security maps directly to that kind of "
            "technical challenge, and I'd like to contribute hands-on while continuing to grow "
            "in {{ROLE_FOCUS}}."
        ),
        "mission": (
            "I'm drawn to {{COMPANY}} because {{WHY_COMPANY}} I want my infrastructure and DevOps "
            "work to support something with real impact, and I'd bring experience building secure, "
            "dependable systems that let teams move quickly and safely."
        ),
        "general": (
            "I'm interested in {{COMPANY}} because {{WHY_COMPANY}} The role lines up well with my "
            "background in cloud infrastructure, automation, and production operations, and it's the "
            "kind of environment where I can contribute immediately while continuing to grow toward "
            "senior infrastructure and reliability work."
        ),
    },
    "default_variant": "technology",
}

bank["role_alignment"] = {
    "type": "template",
    "keywords": ["align with your career", "career goals", "align with your goals",
                 "how does this role", "fit your goals"],
    "answer": (
        "This role aligns with my goal of continuing to grow as a cloud, DevOps, and infrastructure "
        "engineer while taking on greater ownership of production systems. I want to keep working "
        "with cloud infrastructure, automation, Kubernetes, security, and reliability while expanding "
        "into {{ROLE_FOCUS}}. I'm particularly interested in opportunities where I can contribute "
        "hands-on while developing toward senior-level infrastructure and reliability responsibilities."
    ),
}

bank["good_fit"] = {
    "type": "template",
    "keywords": ["good fit", "why are you a good fit", "right person",
                 "make you a strong candidate", "sentences tell us why", "why should we hire"],
    "max_sentences": 4,
    "answer": (
        "I bring {{YEARS_IT}} years of IT experience focused on cloud infrastructure, DevOps, "
        "security, and production operations. I have hands-on experience with AWS, AWS GovCloud, "
        "Azure Government, Terraform, Ansible, Docker, Kubernetes, monitoring, and compliance-driven "
        "environments, supporting both commercial and government customers. I troubleshoot complex "
        "infrastructure and application issues while improving reliability and automation, and I bring "
        "a strong ownership mindset{{FIT_HOOK}}."
    ),
}

# ===== STATIC =====
STATIC = {
    "aws_experience":        "Are you proficient in AWS",
    "aws_years":             "How many years of professional experience do you have with AWS",
    "aws_gcp_scope":         "Describe your AWS experience level and scope",
    "docker":                "Describe your experience working with Docker",
    "kubernetes":            "What is your experience with Kubernetes in production",
    "k8s_migration":         "Describe a time you led a major migration or infrastructure upgrade",
    "terraform_helm":        "Rate your hands-on experience with Terraform and Helm",
    "iac_approach":          "What's your approach to Infrastructure-as-Code",
    "cicd_daily":            "What CI/CD tools do you use daily",
    "cicd_extensive":        "Which CI/CD platforms have you used extensively",
    "secure_cicd_envs":      "building secure development and test environments",
    "cloud_providers":       "Which cloud providers have you worked with",
    "onprem":                "On-Prem Experience",
    "windows_stack":         "Windows Server, IIS, MS SQL, PowerShell, Ansible",
    "networking_issue":      "routing, load balancing, or firewall",
    "security_controls_aws": "designing, implementing and maintaining security controls across the AWS",
    "infra_security":        "Do you have previous infrastructure security experience",
    "security_first":        "security-first infrastructure",
    "monitoring_incident":   "How do you monitor system performance and respond to incidents",
    "devops_definition":     "What does DevOps mean to you",
    "devops_aws":            "Briefly tell me about your DevOps experience working within AWS",
    "saas_multi_env":        "managing SaaS applications across multiple customer",
    "idp_provisioning":      "Google Workspace is our user directory",
    "prioritization":        "prioritize and manage several technical issues",
    "code_review_collab":    "reviewing code and collaborating with developers",
    "cross_team_collab":     "collaborated with teams such as QA, Product",
    "regulated_industry":    "working within a regulated industry",
    "compliance_frameworks": "Have you supported compliance-driven environments",
    "fedramp":               "FedRAMP experience",
    "public_health":         "public health surveillance systems",
    "founding_hire":         "founding DevOps hire",
    "it_years":              "years of professional experience do you have in IT administration",
    "enterprise_it":         "What is your experience Enterprise IT",
    "skills_list":           "List relevant skills",
    "expertise_area":        "Your area of expertise",
    "skill_self_rating":     "describe your experience/skill level with the IT systems",
    "industries":            "List the industries you have experience in",
    "technical_issue":       "technical issue you supported",
    "reliability_improvement":"production reliability or operational improvement",
    "automation_example":    "manual process you've automated",
    "automation_workflow":   "Describe a workflow or process you automated",
    "learn_quickly":         "learn something very quickly",
    "create_clarity":        "took responsibility for creating clarity",
    "built_unprompted":      "built, fixed, or improved even though no one asked",
    "used_ai_work":          "time you used AI in a piece of meaningful work",
    "ai_tools_usage":        "How do you currently use AI tools",
    "motivation_generic":    "What motivates you to work with us",
}

KEYWORDS = {
    "aws_experience": ["proficient in aws", "aws experience", "describe your aws"],
    "aws_years": ["how many years", "years of aws", "years of professional experience with aws"],
    "aws_gcp_scope": ["gcp", "google cloud", "both at once", "aws experience level and scope"],
    "docker": ["docker", "app packaging", "containers packaging"],
    "kubernetes": ["kubernetes in production", "k8s", "kubernetes experience", "scale", "kubernetes", "used kubernetes"],
    "k8s_migration": ["major migration", "infrastructure upgrade", "zero downtime migration"],
    "terraform_helm": ["helm", "rate your", "terraform and helm"],
    "iac_approach": ["infrastructure-as-code", "infrastructure as code", "iac", "approach to"],
    "cicd_daily": ["ci/cd tools", "cicd", "pipelines", "optimized pipelines"],
    "cicd_extensive": ["ci/cd platforms", "used extensively"],
    "secure_cicd_envs": ["secure development", "test environments", "release cycles"],
    "cloud_providers": ["which cloud providers", "cloud providers have you", "services did you manage"],
    "onprem": ["on-prem", "on premise", "on-premise", "server environments"],
    "windows_stack": ["windows server", "iis", "sql server", "powershell", "ansible"],
    "networking_issue": ["routing", "load balancing", "firewall", "connectivity"],
    "security_controls_aws": ["security controls", "across the aws", "designing security"],
    "infra_security": ["infrastructure security experience", "previous infrastructure security"],
    "security_first": ["security-first", "security first infrastructure"],
    "monitoring_incident": ["monitor system", "respond to incidents", "incident response"],
    "devops_definition": ["what does devops mean", "devops mean to you"],
    "devops_aws": ["devops experience working within aws", "devops within aws"],
    "saas_multi_env": ["saas applications", "multiple customer environments", "customer environments"],
    "idp_provisioning": ["google workspace", "idp", "provisioning", "offboarding", "access reviews"],
    "prioritization": ["prioritize", "several technical issues", "at the same time", "multiple systems"],
    "code_review_collab": ["reviewing code", "collaborating with developers", "deployment impacts"],
    "cross_team_collab": ["qa", "product", "professional services", "operational friction", "collaborated with teams"],
    "regulated_industry": ["regulated industry"],
    "compliance_frameworks": ["soc 2", "hipaa", "pci", "compliance-driven"],
    "fedramp": ["fedramp"],
    "public_health": ["nbs", "hl7", "fhir", "loinc", "snomed", "public health"],
    "founding_hire": ["founding", "first devops", "0-to-1", "from scratch", "first hire"],
    "it_years": ["it administration", "systems administration", "it operations", "years of professional experience in it"],
    "enterprise_it": ["enterprise it"],
    "skills_list": ["list relevant skills", "list your skills", "relevant skills"],
    "expertise_area": ["area of expertise", "tech stacks", "familiar with"],
    "skill_self_rating": ["skill level", "particularly strong", "don't have exposure"],
    "industries": ["list the industries", "industries you have"],
    "technical_issue": ["technical issue", "how you resolved", "diagnose"],
    "reliability_improvement": ["reliability", "operational improvement", "personally implemented"],
    "automation_example": ["manual process", "worth the investment", "automated"],
    "automation_workflow": ["workflow or process you automated", "automated in an it context"],
    "learn_quickly": ["learn something", "very quickly", "learn quickly"],
    "create_clarity": ["unclear", "creating clarity", "took responsibility", "ambiguous", "ambiguity", "ambiguous environment"],
    "built_unprompted": ["no one asked", "built, fixed, or improved", "even though no one"],
    "used_ai_work": ["used ai in", "meaningful work", "validate the output"],
    "ai_tools_usage": ["how do you currently use ai", "ai tools in your"],
    "motivation_generic": ["motivates you", "consider the offer", "motivate you to work"],
}

for intent, substr in STATIC.items():
    ans = find(substr)
    # drive any hardcoded IT-year mention from the single {{YEARS_IT}} field
    ans = ans.replace("15+ years", "{{YEARS_IT}} years").replace("15 years", "{{YEARS_IT}} years")
    bank[intent] = {
        "type": "static",
        "keywords": KEYWORDS.get(intent, []),
        "answer": ans,
    }

# ---- extra intents not present in the source Q&A ----
bank["english_proficiency"] = {
    "type": "static",
    "keywords": ["english proficiency", "level of english", "english level",
                 "proficiency in english", "fluency in english", "how is your english"],
    "answer": "I am a native English speaker with full professional working proficiency in both written and spoken English.",
}
bank["linux_experience"] = {
    "type": "static",
    "keywords": ["linux experience", "describe your linux", "linux administration",
                 "what kind of linux", "your linux", "experience with linux"],
    "answer": ("I have extensive hands-on Linux experience across production environments, "
               "including Rocky Linux, RHEL/CentOS, and Ubuntu. My work spans administration, "
               "hardening, networking, services, shell scripting, patching, monitoring, and "
               "troubleshooting. Recent examples include rebuilding a Wazuh SIEM on Rocky Linux 9.7 "
               "and managing Linux fleets across AWS and AWS GovCloud."),
}
bank["databases"] = {
    "type": "static",
    "keywords": ["data storage", "postgresql", "postgres", "redis", "nosql",
                 "relational database", "database technologies", "mysql", "in-memory database"],
    "answer": ("My database experience is primarily operational rather than schema design. I have "
               "supported relational databases including Microsoft SQL Server and PostgreSQL as part "
               "of application infrastructure — handling maintenance, performance considerations, "
               "backups, migrations, and cost optimization (including a database and instance "
               "downsizing initiative). I have worked with these datastores in production AWS "
               "environments, though my depth is in infrastructure and operations rather than "
               "application-level data modeling."),
}
# route generic "backend/devops experience" to the AWS DevOps answer, not aws_years
bank["devops_aws"]["keywords"] = bank["devops_aws"]["keywords"] + ["devops experience", "backend/devops"]

with open("answers.yaml", "w") as f:
    yaml.safe_dump({"intents": bank}, f, sort_keys=False, width=100, allow_unicode=True)

n_t = sum(1 for v in bank.values() if v["type"] == "template")
print(f"built answers.yaml: {len(bank)} intents ({n_t} templates, {len(bank)-n_t} static)")
