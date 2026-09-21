#!/usr/bin/env python3
"""
essay.py - genre-aware complex-answer engine for Penates.

The problem this fixes: the old compose() used ONE generic prompt for every free-text
question, so an "owned project" question got a skills dump, a "what would you build"
hypothetical got a DevOps definition, and a Salesforce question (a tool the candidate
lacks) got a confident unrelated story.

Design (deterministic control around a small local model):

  full question + parsed constraints
        -> genre  (rules first, model only if no rule hits)
        -> retrieve 0-1 STORY from stories.yaml  (or detect a gap)
        -> per-genre prompt that dresses ONE story (or states the gap honestly)
        -> Ollama
        -> validate in code (banned openings, word/sentence bounds, honesty, genre)
        -> one regenerate, else return REVIEW (draft shown, not auto-inserted)

The model NEVER decides what is true about the candidate. It only phrases approved
content. Works for any user: stories live in stories.yaml, gaps come from
profile.limited_or_none / limited_experience. Empty story bank -> NEEDS_INPUT, never
a fabricated project.
"""
import os, re, json, hashlib
import apply as _a          # reuse load(), ollama(), ollama_up(), enforce_length(), gap helpers
import job_context as _jc   # source chain + fact extraction for why-company grounding
import jobs_store as _js    # stored role/title for the applied-for job

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- applied-for role (never the candidate's CURRENT title) ------------------------------
_ROLE_JUNK = re.compile(
    r"^\s*(application|apply|apply now|application questions|job application|careers?|"
    r"open positions?|overview|position)\s*$", re.I)
def _current_title():
    try:
        return (((_a.load("profile.yaml") or {}).get("identity") or {}).get("current_title") or "").strip()
    except Exception:
        return ""
def resolve_target_role(role, url):
    """The role the candidate is APPLYING to. Precedence: role passed from the extension
    (page title) -> stored jobs.json row -> public posting title. Rejects form chrome like
    'Application'. Empty string if unknown (then we simply don't name a target role)."""
    r = (role or "").strip()
    if r and not _ROLE_JUNK.match(r):
        return r
    try:
        r = _js.role_for(url)
        if r and not _ROLE_JUNK.match(r):
            return r.strip()
    except Exception:
        pass
    try:
        r = _jc.fetch_public_title(url)
        if r and not _ROLE_JUNK.match(r):
            return r.strip()
    except Exception:
        pass
    return ""
def _norm_role(s):
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()
def why_uses_title_as_role(text, current_title, target_role):
    """True if the draft calls the JOB by the candidate's CURRENT title (e.g. 'the Network
    Systems Analyst 4 role at 1Password') when that is not the target role. Naming the real
    target role too makes a current-title mention background, which is allowed; so is
    'I am a Systems Analyst II' (that is not '<title> role')."""
    ct = (current_title or "").strip()
    if not ct:
        return False
    if target_role and _norm_role(ct) == _norm_role(target_role):
        return False
    pat = re.compile(
        r"\b(the\s+)?" + re.escape(ct) + r"\s+(role|position|job|opening|opportunity)\b|"
        r"\b(role|position|job|opening|opportunity)\s+(of|as|for)\s+(an?\s+)?" + re.escape(ct) + r"\b",
        re.I)
    if not pat.search(text or ""):
        return False
    if target_role and re.search(re.escape(target_role), text or "", re.I):
        return False   # names the real role too -> current title is background
    return True

def _load_stories():
    try:
        d = _a.load("stories.yaml") or {}
        return d.get("stories", []) or []
    except Exception:
        return []

# ---------------------------------------------------------------- constraints
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10}
def _n(tok):
    tok = tok.strip().lower()
    return int(tok) if tok.isdigit() else _NUM.get(tok)

def parse_constraints(question, limit=None):
    q = (question or "").lower()
    c = {"min_sent": None, "max_sent": None, "min_words": None, "max_words": None,
         "max_chars": limit, "owned": False, "first_person": True}
    # sentence ranges: "three to four sentences", "3-4 sentences", "in 2 sentences"
    m = re.search(r"(\d+|one|two|three|four|five|six)\s*(?:-|to|or)\s*(\d+|one|two|three|four|five|six)\s+sentences", q)
    if m:
        c["min_sent"], c["max_sent"] = _n(m.group(1)), _n(m.group(2))
    else:
        m = re.search(r"(?:in|within|max(?:imum)?(?: of)?|up to|no more than)\s*(\d+|one|two|three|four|five|six)\s+sentences", q)
        if m: c["max_sent"] = _n(m.group(1))
        m = re.search(r"(?:at least|minimum(?: of)?)\s*(\d+|one|two|three|four|five|six)\s+sentences", q)
        if m: c["min_sent"] = _n(m.group(1))
        if re.search(r"\bone sentence\b|\bin a sentence\b|\ba single sentence\b", q):
            c["min_sent"] = c["max_sent"] = 1
    # word ranges: "100-250 words", "100 to 250 words", "at least 150 words", "under 300 words"
    m = re.search(r"(\d{2,4})\s*(?:-|to|and)\s*(\d{2,4})\s*words", q)
    if m:
        c["min_words"], c["max_words"] = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(?:at least|minimum(?: of)?|no fewer than)\s*(\d{2,4})\s*words", q)
        if m: c["min_words"] = int(m.group(1))
        m = re.search(r"(?:under|below|no more than|max(?:imum)?(?: of)?|fewer than|up to|less than)\s*(\d{2,4})\s*words", q)
        if m: c["max_words"] = int(m.group(1))
    # char cap from a "words" range dominates the popup maxlength if both present
    if c["max_words"] and not c["max_chars"]:
        c["max_chars"] = int(c["max_words"] * 6.5)
    c["owned"] = bool(re.search(r"personally owned|you owned|you personally|you led|that you owned|you built", q))
    return c

# ---------------------------------------------------------------- genre
_GENRE_RULES = [
    ("why_company",  r"why (do you want|are you interested|us\b|here\b|this (company|organization|team))|what (excites|interests|draws|appeals to) you|what makes you (excited|want)|excited to (work|join|be part)|want to work (at|for|here)|why (would you like )?work(ing)? (here|with us|for us)|interested in (working|joining)|^\s*why \S+\s*\??\s*$"),
    ("why_role",     r"why this (position|role|job)|why (are you interested in )?(this )?(devops|sre|cloud|platform|the) (role|position)|why do you want this (role|position|job)"),
    ("hypothetical", r"what would you build|if you (were|had|could|got)|imagine (you|that)|suppose you|given (a|the).{0,20}(month|week|opportunity|chance)|spend a (month|week|day)|how would you (design|build|approach|architect)|greenfield|from scratch, what|blue ?sky"),
    ("behavioral",   r"influenc\w+|disagree\w*|convince|persuad\w+|conflict|push(ed)? ?back|difficult (person|co-?worker|colleague|teammate|customer|client|manager|boss|conversation|stakeholder)|had to (get|win|bring|convince|persuade|talk)|build(ing)? consensus|align(ing|ed)? (a |the )?(team|group|stakeholders|people)|work(ed)? with (a )?(difficult|resistant)|handl\w+ (a )?(disagreement|conflict)|gave (someone )?(difficult|hard|critical) feedback"),
    ("owned_project",r"describe (a|the|your).{0,40}(project|time|situation|example)|tell (us|me) about (a|the|your)|most (complex|challenging|difficult|impactful)|(a|one) (project|time|situation) (you|where you)|walk (us|me) through|you (personally )?(owned|led|built|architected)|give (us|me) an example|share an example"),
    ("technical_experience", r"what is your experience (with|in)|describe your .{0,30}experience|how (do|have) you (use|used)|rate your|proficiency (with|in)|how familiar are you|level of experience|how many years"),
    ("definition",   r"what does .{0,30}mean to you|how do you define|what is your definition|what do you (understand|think).{0,20}means"),
]
# Inventory / list questions ("which AWS services / what tools have you used") must enumerate the
# candidate's real stack, NOT become a STAR project story. Narrative cue (describe a / one time /
# example) keeps a genuine project question as owned_project.
_INVENTORY_RE = re.compile(
    r"(which|what)\b.{0,60}\b(services?|tools?|technolog\w+|platforms?|languages?|stack|frameworks?|software|monitoring|databases?)\b"
    r".{0,40}\b(have you|did you|you.?ve|do you)\b.{0,14}\b(use|used|using|worked with|run|ran|operated?|set up|manage[d]?|maintain\w*)\b"
    r"|what\b.{0,70}\bhave you\b.{0,14}\b(set up|configured?|maintain\w*|worked with)\b",
    re.I)
_NARRATIVE_CUE_RE = re.compile(
    r"describe (a|an|one|your)|give (us|me) an example|share an example|walk (us|me) through"
    r"|tell (us|me) about|(a|one) (time|project|issue|situation|example)\b", re.I)
_PRACTICE_RE = re.compile(r"\b(testing|test suite|synthetic monitoring|integration tests?|endpoint checks?|unit tests?|smoke tests?|monitoring practice)\b", re.I)


def classify_genre(question, constraints):
    q = (question or "").lower()
    # short_text: an explicit one-sentence / very short ask that is not a project story
    if (constraints.get("max_sent") == 1 or (constraints.get("max_words") or 999) < 40) \
       and not re.search(r"project|time you|example|describe a", q):
        return "short_text"
    # inventory/list question -> enumerate real stack, not a project story. A PRACTICE noun
    # (testing/synthetic monitoring/integration/endpoint) asks for how you do it, not a tool list.
    if _INVENTORY_RE.search(q) and not _NARRATIVE_CUE_RE.search(q) and not _PRACTICE_RE.search(q):
        return "inventory"
    for genre, rx in _GENRE_RULES:
        if re.search(rx, q):
            return genre
    if _a.ollama_up():
        try:
            ids = [g for g, _ in _GENRE_RULES] + ["short_text"]
            pick = _a.ollama(
                "Question: %s\n\nReply with ONE id from:\n%s" % (question, "\n".join(ids)),
                "You label a job-application question with exactly one genre id. Reply ONLY the id.",
                temperature=0).split()[0].strip().strip(".,").lower()
            if pick in ids:
                return pick
        except Exception:
            pass
    return "owned_project"  # safe default: force a concrete answer, not a skills dump

# ---------------------------------------------------------------- inventory / list answers
_BUCKET_KEYWORDS = {
    "aws_services": ["aws service", "aws services"],
    "cloud": ["cloud", " aws", "azure", "gcp", "govcloud"],
    "monitoring_security": ["monitor", "observab", "siem", "logging", "log analytics", "alerting",
                            "synthetic", "security tool", "security stack", "endpoint check"],
    "cicd": ["ci/cd", "cicd", "ci cd", "continuous integration", "continuous delivery",
             "continuous deployment", "pipeline", "github actions", "automated testing",
             "integration test", "testing"],
    "containers": ["container", "docker", "kubernetes", "k8s", "orchestrat"],
    "iac": ["infrastructure as code", "iac", "terraform", "ansible", "provision",
            "configuration management"],
    "systems": ["operating system", "linux", "windows", "server os", "sysadmin"],
    "languages": ["language", "scripting", "programming language", "coding"],
}
def _enumerate_experience(question):
    """Deterministic honest list for inventory/list questions: real items from profile.yaml buckets
    whose keyword appears in the question. No model, no story, no narrative."""
    p = _a.load("profile.yaml") or {}
    ql = (question or "").lower()
    items, seen = [], set()
    for bucket, kws in _BUCKET_KEYWORDS.items():
        if any(k in ql for k in kws):
            for x in (p.get(bucket) or []):
                xs = str(x).strip()
                if xs and xs.lower() not in seen:
                    seen.add(xs.lower()); items.append(xs)
    if not items:
        for s2 in (_a._real_stack(p) or "").split(","):
            s2 = s2.strip()
            if s2 and s2.lower() not in seen:
                seen.add(s2.lower()); items.append(s2)
    if not items:
        return ""
    return "In production I have worked with " + ", ".join(items[:16]) + "."


# ---------------------------------------------------------------- gaps
# Systems a question may name that, if the candidate has no evidence of them, must be
# disclosed rather than storied over. Tech gaps come from apply.TECH_LEXICON; these add
# the endpoint/ITSM/CRM/MDM class that the tech lexicon misses (e.g. Salesforce). Anything
# already in the candidate's real material is filtered out by _cand_text below.
_EXTRA_SYSTEMS = [
    "salesforce", "servicenow", "service now", "intune", "jamf", "workspace one", "airwatch",
    "sccm", "mecm", "bigfix", "tanium", "kandji", "mosyle", "ivanti", "manageengine",
    "lansweeper", "freshservice", "zendesk", "okta", "ping identity", "sailpoint",
    "dynamics", "netsuite", "sap", "jira service management", "mdm", "jenkins",
]
_DOMAIN_HINTS = {
    "endpoint management": "endpoint management",
    "endpoint automation": "endpoint automation",
    "device automation": "device automation",
    "device management": "device management",
    "mobile device": "mobile device management",
    "patch management": "patch management",
    "desktop support": "desktop support",
}
def _named_system_gaps(question):
    q = (question or "").lower()
    cand = _a._cand_text()
    lim_none = []
    try:
        lim_none = [str(x).lower() for x in (_a.load("profile.yaml").get("limited_or_none") or [])]
    except Exception:
        pass
    out = []
    seen = set()
    for term in list(_EXTRA_SYSTEMS) + lim_none:
        t = term.strip().lower()
        if not t or t in seen:
            continue
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"s?(?![a-z0-9])", re.I)
        if rx.search(q) and not rx.search(cand):
            seen.add(t)
            out.append(_a._gap_disp(t))
    return out

def detect_gaps(question):
    hard = list(_a._question_gaps(question))                 # tech lexicon (kafka, salesforce not here)
    named = _named_system_gaps(question)                     # salesforce, intune, jamf, limited_or_none
    disp_hard = [_a._gap_disp(g) for g in hard]
    merged = []
    for g in disp_hard + named:
        if g not in merged:
            merged.append(g)
    limited = _a._question_limited(question)                 # [(display, qualifier)]
    return {"hard": merged, "limited": limited}

def _domain_gap(question, story):
    q = (question or "").lower()
    have = set()
    if story:
        have = set(d.lower() for d in story.get("domains", []))
    for phrase, disp in _DOMAIN_HINTS.items():
        if phrase in q and not any(phrase.split()[0] in d for d in have):
            return disp
    return None

# A why-company/why-role draft must not claim a capability the gap-guard would block for an
# owned-work question. If the draft asserts a domain/system the candidate has NO real evidence
# of (endpoint/MDM/Intune/Jamf/device management, etc.), that is a fabricated qualification -
# exactly what the endpoint gap analog on the same form correctly disclaims. Generic across
# every such domain; not tied to any one company.
_WHY_CLAIM_TERMS = list(_DOMAIN_HINTS.keys()) + [
    "endpoint", "mdm", "intune", "jamf", "sccm", "workspace one", "airwatch", "device fleet",
    "device lifecycle", "mobile device",
    # posting/product language the candidate must not adopt as his own owned work
    "user experience", "user experiences", "end-user", "end user", "seamless",
    "sign-in", "sign in", "single sign-on", "sso", "password manager", "every device",
    "device is trusted", "trusted device", "credential management", "identity security",
]
def why_claims_unowned(text):
    t = (text or "").lower()
    try:
        cand = (_a._cand_text() or "").lower()
    except Exception:
        cand = ""
    for term in _WHY_CLAIM_TERMS:
        term = term.strip().lower()
        if not term:
            continue
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"s?(?![a-z0-9])", re.I)
        if rx.search(t) and not rx.search(cand):
            return term
    return None

# why-company quality gates (would have rejected the build-17 live draft).
# Real clichés only (per Grok review): bare "aligns" is a normal word ("the compliance work I do
# aligns with this role"), so only "aligns perfectly/seamlessly" counts. Words that appear inside
# the verified posting fact are allowlisted (see why_fluff) - echoing the fact is grounding, not
# fluff.
_FLUFF_RE = re.compile(
    r"\baligns?\s+(perfectly|seamlessly)\b|\b(perfect|strong|great)\s+fit\b|"
    r"\bmakes?\s+me\s+a\s+(great|strong|perfect)\b|\bpassion(ate)?\b|\bseamless\b|\bbest\s+practices\b|"
    r"\bexcited\s+to\s+contribute\b|\bhit\s+the\s+ground\s+running\b|\bleverage\s+my\b|"
    r"\bwell[- ]equipped\b|\bcutting[- ]edge\b|\bbring\s+to\s+the\s+table\b|\bmeaningful\s+impact\b|"
    r"\bthrive\b|\bresonates?\s+with\s+me\b", re.I)
_BANNED_MID = re.compile(r"\bmy\s+focus\s+(on|has\s+been)\b|\bthroughout\s+my\s+career\b", re.I)
def why_fluff(text, facts=None):
    factblob = " ".join(facts or "").lower()
    for m in _FLUFF_RE.finditer(text or ""):
        hit = m.group(0).strip()
        if hit.lower() in factblob:   # echoing the verified fact's own words is not fluff
            continue
        return hit
    return None
def why_tech_dump(text):
    t = (text or "").lower()
    n = 0
    for s in _stack_tokens():
        if re.search(r"(?<![a-z0-9])" + re.escape(s.lower()) + r"(?![a-z0-9])", t):
            n += 1
    return n >= 3   # 3+ real-stack product names in one why answer = a tech dump
def why_rewrote_fact(text, facts):
    # reject the model flipping the posting's "we're building" into "you're building"
    if not facts:
        return False
    said_we = any(re.search(r"\bwe'?re\b|\bwe\s+are\b|\bour\b", f, re.I) for f in facts)
    said_you = bool(re.search(r"\byou'?re\s+(building|creating|developing|working)\b|\byou\s+are\s+(building|creating)\b", text or "", re.I))
    return said_we and said_you

# The candidate half of a why-company answer must be first-person AND cite something from the
# real stack, or the draft is just restated company text with no candidate connection.
def _stack_tokens():
    try:
        s = _a._real_stack(_a.load("profile.yaml")) or ""
    except Exception:
        s = ""
    return [x.strip() for x in s.split(",") if x.strip()]

def _candidate_identity():
    """A short, natural, TRUE sentence describing who the candidate is, assembled from
    profile.yaml facts (never invented). Reads like a person, not a keyword list - this is
    what a coherent why-company answer connects to. profile.yaml 'pitch' overrides it."""
    try:
        p = _a.load("profile.yaml") or {}
    except Exception:
        return ""
    pitch = p.get("pitch")
    if pitch:
        return str(pitch).strip().rstrip(".")
    ident = p.get("identity") or {}
    title = ident.get("current_title") or "an infrastructure engineer"
    bits = []
    cloud = p.get("cloud") or []
    if cloud:
        bits.append("cloud infrastructure (" + ", ".join(str(c) for c in cloud[:2]) + ")")
    sec = [str(s) for s in (p.get("monitoring_security") or [])]
    if any("wazuh" in s.lower() for s in sec):
        bits.append("a Wazuh SIEM")
    elif sec:
        bits.append("security monitoring")
    comp = p.get("compliance_owned") or []
    if comp:
        bits.append(", ".join(str(x) for x in comp[:2]) + " compliance")
    # plain join (NOT _human_list, which title-cases each item and mangles the prose)
    if not bits:
        focus = "cloud and security engineering"
    elif len(bits) == 1:
        focus = bits[0]
    else:
        focus = ", ".join(bits[:-1]) + ", and " + bits[-1]
    return "a %s whose hands-on work centers on %s" % (title, focus)

# words that count as a genuine candidate connection even without an exact stack token, so a
# coherent answer isn't forced back to the template just because it didn't name a product.
_IDENTITY_WORDS = re.compile(
    r"\b(security|secur\w+|infrastructure|infra|cloud|compliance|complian\w+|reliab\w+|"
    r"platform|systems?|devops|sre|engineer\w*|govcloud|hardening|automation)\b", re.I)
def why_has_candidate_link(text):
    t = (text or "")
    first_person = bool(re.search(r"\b(I|I'?ve|I'?m|my|me)\b", t))
    has_substance = any(re.search(r"(?<![a-z0-9])" + re.escape(s.lower()) + r"(?![a-z0-9])", t.lower())
                        for s in _stack_tokens()) or bool(_IDENTITY_WORDS.search(t))
    return first_person and has_substance

def _why_identity():
    """SHORT candidate identity for why-company: domains only, NO product/tool names (a small
    model copies a tool list straight into a dump). profile.yaml 'why_identity' overrides."""
    try:
        p = _a.load("profile.yaml") or {}
    except Exception:
        p = {}
    wi = p.get("why_identity")
    if wi:
        return str(wi).strip().rstrip(".")
    return "cloud infrastructure, security monitoring, and compliance in restricted environments"

# A posting fact is worth citing only if it is OPERATIONAL (team, reporting line, remote/US,
# seniority, a concrete stack) - not mission/slogan copy. Grok correction: never lead a why-company
# answer with the company's About line; if extract_facts only yields mission-speak, drop the fact.
_MARKETING_FACT_RE = re.compile(
    r"\b(mission|vision|future|foundation|believe|empower\w*|world|passion\w*|purpose|values|"
    r"productive\s+digital|safer?\b|journey|reimagin\w*|transform\w*|dream|make\s+\w+\s+better|"
    r"building\s+the\s+foundation|committed\s+to)\b", re.I)
_OPERATIONAL_FACT_RE = re.compile(
    r"\b(remote|distributed\s+team|hybrid|on-?call|reports?\s+to|reporting\s+to|team\s+of|"
    r"engineering\s+team|infrastructure\s+team|security\s+team|platform\s+team|it\s+team|"
    r"senior|staff|principal|full-?time|contract|based\s+in|headquarter\w*|offices?\s+in|"
    r"kubernetes|k8s|terraform|ansible|aws|gcp|azure|linux|siem|soc\b|ci/?cd|on-?prem)\b", re.I)
def _fact_is_operational(phrase):
    """True only for concrete operational posting detail, never marketing/mission copy."""
    if not phrase:
        return False
    if _MARKETING_FACT_RE.search(phrase):
        return False
    return bool(_OPERATIONAL_FACT_RE.search(phrase))

def _operational_facts(facts, company=None):
    out = []
    for f in (facts or []):
        if _fact_is_operational(_fact_phrase(f, company)):
            out.append(f)
    return out

def _fact_phrase(fact, company):
    """Reduce a verified posting fact to a clean phrase we can attribute to the company, dropping
    a leading 'At <Company>, we're' so the candidate never speaks as 'we'."""
    s = re.sub(r"\s+", " ", (fact or "")).strip().rstrip(".")
    # normalize curly apostrophes/quotes so the strip patterns match (postings use typographic ’)
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    if company:
        s = re.sub(r"^at\s+" + re.escape(company) + r"\s*,?\s*", "", s, flags=re.I)
    s = re.sub(r"^(we\s*'?re|we\s+are|you\s*'?re|you\s+are|it\s*'?s|it\s+is|they\s*'?re|they\s+are|"
               r"our\s+mission\s+is\s+to|our\s+goal\s+is\s+to|we\s+want\s+to|we\s+help)\s+", "", s, flags=re.I)
    if company:
        s = re.sub(r"^" + re.escape(company) + r"\s+(is|are|'?s)\s+", "", s, flags=re.I)
    # if it still opens with a bare pronoun we couldn't map, drop it rather than say "we're"
    s = re.sub(r"^(we|you|they)\s*'?re\s+", "", s, flags=re.I)
    return s.strip()

# Domain themes matched against the POSTING text (not the mission slogan) - so the why-company hook
# is what the company/role is actually about, honest engagement rather than marketing. Ordered
# specific -> general; the phrases deliberately avoid the exact _WHY_CLAIM_TERMS strings so the
# claims_unowned gate stays satisfied. Kept to domains that overlap the candidate's real lane.
_WHY_DOMAINS = [
    (re.compile(r"\b(secret|credential|password|vault|privileged\s+access|key\s+management)\b", re.I), "credentials and secrets"),
    (re.compile(r"\b(identity|access\s+management|\biam\b|sso|single\s+sign|authenticat|zero\s+trust)\b", re.I), "identity and access"),
    (re.compile(r"\b(security|secure|threat|vulnerab|appsec|infosec|\bsoc\b|siem|detection|hardening)\b", re.I), "security"),
    (re.compile(r"\b(compliance|nist|fedramp|soc\s?2|iso\s?27|\baudit|governance|\bgrc\b)\b", re.I), "compliance"),
    (re.compile(r"\b(cloud|aws|azure|gcp|kubernetes|container)\b", re.I), "cloud infrastructure"),
    (re.compile(r"\b(infrastructure|platform|reliability|\bsre\b|uptime|devops)\b", re.I), "infrastructure and reliability"),
]

def _company_domain(blob):
    """The single domain theme the POSTING most emphasizes, from a set that overlaps the candidate's
    lane. Returns a short honest phrase ('credentials and secrets', ...) or None if none is present."""
    for rx, phrase in _WHY_DOMAINS:
        if rx.search(blob or ""):
            return phrase
    return None

def _no_dash(s):
    """A hard style rule: NO em dashes, ever. Convert em/en dashes and a spaced-hyphen-as-dash to
    commas in generated prose (real hyphenated words like 'zero-downtime' keep their hyphen)."""
    s = (s or "")
    s = re.sub(r"\s*[—–]\s*", ", ", s)       # em/en dash (with any surrounding space) -> ", "
    s = re.sub(r"\s+-\s+", ", ", s)          # spaced hyphen used as a dash -> ", "
    s = re.sub(r"\s+,", ",", s)              # no space before a comma
    s = re.sub(r",\s*,", ",", s)             # collapse doubled commas
    s = re.sub(r"\s{2,}", " ", s)            # collapse double spaces
    return s.strip()

def _question_company(q):
    """Company name a 'why <company>' question names, or None. Used to catch a question whose
    company token does not match the application's company context (answering the wrong company)."""
    m = re.search(r"\bwhy\s+([A-Z0-9][\w&.\-]*(?:\s+[A-Z0-9][\w&.\-]*){0,3})", q or "")
    if not m:
        m = re.search(r"(?:work (?:at|for)|join|part of)\s+([A-Z0-9][\w&.\-]*(?:\s+[A-Z0-9][\w&.\-]*){0,3})", q or "")
    if not m:
        return None
    cand = re.sub(r"\s+(there|out|here|today).*$", "", m.group(1).strip().rstrip("?.,!"), flags=re.I).strip()
    if cand and cand.lower() not in ("do", "are", "would", "you", "us", "this", "the", "our", "i"):
        return cand
    return None

def _company_match(a, b):
    na = re.sub(r"[^a-z0-9]", "", (a or "").lower())
    nb = re.sub(r"[^a-z0-9]", "", (b or "").lower())
    if not na or not nb:
        return True
    return na in nb or nb in na

def _story_is_interpersonal(story):
    """True only if the story actually carries influence/conflict content. Our corpus is technical,
    so this is normally False and behavioral questions floor to an honest gap instead of a fake STAR."""
    if not story:
        return False
    s = story.get("star", {}) or {}
    blob = " ".join([str(story.get("hero", "")), str(story.get("title", "")),
                     " ".join(story.get("domains", []) or []),
                     str(s.get("situation", "")), str(s.get("action", "")), str(s.get("result", ""))]).lower()
    return bool(re.search(r"influenc|persuad|convince|consensus|disagree|conflict|stakeholder|align the team|pushed back|negotiat", blob))

def _behavioral_gap(question, c):
    """Honest floor for a behavioral/influence prompt when no story has real interpersonal conflict.
    Do NOT invent a conflict or relabel a technical project as one."""
    limit = c.get("max_chars") or 700
    ans = ("I want to be accurate rather than force an example. I do not have a standout story of "
           "overturning a team's disagreement that I would want to overstate. Where I do have "
           "influence, it is usually by putting a clear, tested proposal in front of people and "
           "letting the results make the case, not by winning an argument. I would rather be upfront "
           "about that than invent a conflict.")
    return _no_dash(_a.enforce_length(ans, {}, limit, False).strip())

def _why_candidate_only(facts, c, target_role=None, company=None, job_text=""):
    """Deterministic why-company floor (option B, Grok-approved): ENGAGE the company with a true
    domain hook taken from the POSTING (never the mission slogan), tie it to the candidate's real
    lane, then say why the role fits. No slogans, no unowned-work claims, no em dashes.
    Falls back to a candidate-first line if the posting yields no usable domain."""
    limit = c.get("max_chars") or 700
    co = company or "this company"
    role = target_role or "this role"
    ident = _why_identity()
    blob = (job_text or "") + " " + " ".join(facts or [])
    domain = _company_domain(blob)
    parts = []
    if target_role:
        parts.append("I'm applying for the %s role." % role)
    if domain:
        parts.append("%s's work sits in %s, which is the same ground I cover day to day: %s." % (co, domain, ident))
        parts.append("That overlap is what draws me to the role, and it is the kind of work I want to keep doing.")
    else:
        parts.append("The work I already do is %s, and it maps directly onto the %s role." % (ident, role))
        parts.append("That is the kind of work I want to keep doing, which is what draws me to this role.")
    op = _operational_facts(facts, company)
    if op:
        _ph = _fact_phrase(op[0], company).rstrip(".")
        parts.append("It also lines up with what the posting describes: %s." % _ph)
    ans = _no_dash(" ".join(parts))
    return _a.enforce_length(ans, {}, limit, False).strip()

def _compose_star(story):
    """Deterministic owned-project answer straight from a story's STAR fields, used when the
    model keeps dropping the Action/Result. Contains real action verbs, so it passes validate."""
    s = (story or {}).get("star", {}) or {}
    hero = (story or {}).get("hero") or (story or {}).get("title", "")
    situation = (s.get("situation") or "").strip()
    action = (s.get("action") or "").strip()
    result = (s.get("result") or "").strip()
    parts = []
    lead = (hero or situation).strip()
    if lead:
        parts.append(lead if lead.endswith((".", "!", "?")) else lead + ".")
    if action:
        parts.append(action if action.endswith((".", "!", "?")) else action + ".")
    if result:
        parts.append(result if result.endswith((".", "!", "?")) else result + ".")
    return _no_dash(" ".join(p for p in parts if p).strip())

# ---------------------------------------------------------------- retrieval
def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower())) - {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "you", "your", "with",
        "for", "what", "how", "why", "was", "were", "did", "do", "is", "are", "that",
        "this", "it", "we", "our", "us", "at", "as", "by", "have", "has", "tell", "us",
        "describe", "about", "time", "project", "would", "could", "role"}
def retrieve_story(question, want_owned, ignore_not_implied=False):
    stories = _load_stories()
    if not stories:
        return None, -1
    qt = _tokens(question)
    best, bs, b_topic = None, -10, -1
    ql = (question or "").lower()
    for st in stories:
        dt = _tokens(" ".join(list(st.get("domains", [])) + list(st.get("tools", []))))   # topic/tool tokens
        th = _tokens(" ".join([st.get("title", ""), st.get("hero", "")])) - dt             # title/hero only
        topic_hits = len(qt & dt)                 # real domain/tool overlap
        score = topic_hits * 3 + len(qt & th)     # weight topic/tool matches over title/hero words
        if want_owned and st.get("owned"):
            score += 2
        if not ignore_not_implied:
            for ni in st.get("not_implied", []):
                if re.search(r"(?<![a-z0-9])" + re.escape(ni.lower()) + r"(?![a-z0-9])", ql):
                    score -= 5
        # tie-break on real topic/tool overlap, NOT file order (was: first story in file won ties)
        if score > bs or (score == bs and topic_hits > b_topic):
            bs, best, b_topic = score, st, topic_hits
    return best, bs

# ---------------------------------------------------------------- prompts
def _star_block(st):
    s = st.get("star", {}) or {}
    return ("STORY (the only facts you may use; do not add tools, employers, numbers, or "
            "outcomes that are not here):\n"
            "- Situation: %s\n- Task: %s\n- Action: %s\n- Result: %s\n- Tools you may name: %s"
            % (s.get("situation", ""), s.get("task", ""), s.get("action", ""),
               s.get("result", ""), ", ".join(st.get("tools", []))))

def build_prompt(genre, question, c, story, gaps, company, page_context=None, facts=None, target_role=None):
    limit = c.get("max_chars") or 900
    length_line = ""
    if c.get("min_words") or c.get("max_words"):
        length_line = "Write between %s and %s words. " % (c.get("min_words") or 60, c.get("max_words") or 250)
    elif c.get("max_sent"):
        length_line = "Write %s to %s sentences. " % (c.get("min_sent") or 1, c.get("max_sent"))
    base = ("You draft ONE job-application answer in first person as the candidate. "
            "Answer THIS question directly - the first sentence must address what it asks. "
            "Use only facts given below. No preamble, no filler. " + length_line +
            "Never use an em dash. Output only the answer text.")

    if genre in ("owned_project", "technical_experience", "behavioral"):
        sysp = base + (" Do NOT open with 'I have experience with', 'My focus has been', "
                       "or 'Throughout my career'. Name the specific project first, then WHAT YOU "
                       "DID (the Action - concrete verbs) and WHAT CHANGED (the Result). You MUST "
                       "include both the action you took and the outcome; do not stop after "
                       "describing the problem. Only name tools listed under Tools.")
        user = "QUESTION:\n%s\n\n%s\n\nAnswer:" % (question, _star_block(story) if story else "(no story)")
        return sysp, user

    if genre == "hypothetical":
        sysp = base + (" This is HYPOTHETICAL. Propose ONE specific thing you would build and why "
                       "it matters, and what you would ship in the timebox. Use 'I would build' / "
                       "'I would use' - never claim you already built it. Do NOT define a job title "
                       "or a methodology (do not write 'DevOps means'). You may cite your real skills "
                       "as why you could build it.")
        skills = ""
        try:
            skills = _a._real_stack(_a.load("profile.yaml"))
        except Exception:
            pass
        user = ("QUESTION:\n%s\n\nYour real skills you may cite as capability: %s\n\nAnswer:"
                % (question, skills))
        return sysp, user

    if genre in ("why_company", "why_role"):
        blurb = (company or "the company")
        identity = _why_identity()   # SHORT, no product/tool names (keeps the model from dumping tools)
        # Role lock: the identity line contains the candidate's CURRENT title; a small model will
        # otherwise reuse it as the job. Pin the applied-for role explicitly.
        role_lock = ""
        if target_role:
            role_lock = (" You are applying to the role: %s. Refer to the position you want as "
                         "'%s' (or 'this role') ONLY. NEVER call it by the candidate's current "
                         "title. The candidate's current title is background, not the job." %
                         (target_role, target_role))
        role_line = ("\n\nYou are applying to this role (name it, do NOT use the current title as "
                     "the job): %s" % target_role) if target_role else ""
        if facts:
            # extract-then-write: the model gets 1-3 VERIFIED clauses from the posting and a
            # COHERENT one-line candidate identity (not a tech list, which makes small models
            # keyword-dump). It writes a genuine, specific reason connecting the two.
            factlist = "\n".join("- " + f for f in facts)
            sysp = base + (" Write a genuine, specific reason this candidate is excited about THIS company. "
                           "Three sentences, first person, and it must read like a real person talking - "
                           "NOT a list of technologies. Sentence 1: lead with ONE of the verified facts, "
                           "copied closely, as what appeals. Sentences 2-3: connect it to the candidate's "
                           "actual work using the identity line below, and say plainly why that makes this a "
                           "fit. Do NOT list tools or name more than one or two technologies. Do NOT add any "
                           "company product, customer, metric, or claim beyond the verified facts. Do NOT "
                           "claim work the candidate has not done. Name the company." + role_lock)
            user = ("QUESTION:\n%s\n\nCompany: %s%s\n\nVERIFIED FACTS (open with one, copy it closely; add no "
                    "company fact beyond these):\n%s\n\nWHO THE CANDIDATE IS (connect to this in your own "
                    "words; do not just repeat it, and do not turn it into a tech list):\n%s\n\nAnswer:"
                    % (question, blurb, role_line, factlist, identity or "an infrastructure and security engineer"))
        else:
            # no verified job text -> honest, obviously non-researched. Role title is a role
            # fact and may be used; what the company sells may not be invented.
            sysp = base + (" You were given NO verified facts about the company, so you must not state, "
                           "describe, or guess anything about what it does, sells, or builds, or about its "
                           "products, features, technology, or reputation. Refer to the company ONLY by "
                           "name. Write a genuine, specific, first-person reason grounded in the candidate's "
                           "actual work (the identity line below) and the role title - three sentences that "
                           "read like a person, NOT a list of technologies. Never write 'their product' or "
                           "any capability of the company." + role_lock)
            user = ("QUESTION:\n%s\n\nCompany name (use as a name only, invent no facts about it): %s%s\n\n"
                    "WHO THE CANDIDATE IS (ground every sentence in this; do not turn it into a tech "
                    "list):\n%s\n\nAnswer:"
                    % (question, blurb, role_line, identity or "an infrastructure and security engineer"))
        return sysp, user

    if genre == "definition":
        sysp = base + " Define the term in your own words and tie it to how you actually work."
        return sysp, "QUESTION:\n%s\n\nAnswer:" % question

    # short_text
    sysp = base + " One tight sentence. No story."
    return sysp, "QUESTION:\n%s\n\nAnswer:" % question

# ---------------------------------------------------------------- validation
_BANNED_PREFIX = re.compile(
    r"^\s*(i have experience with|to me,|my focus has been|throughout my career|"
    r"as a professional|in today'?s|i am a highly|i'?m a highly|i believe that )", re.I)
_ACTION_VERB = re.compile(
    r"\b(rebuilt|automated|migrated|deployed|restored|built|owned|led|scripted|designed|"
    r"implemented|configured|hardened|reconnected|captured|ran|resolved|fixed|integrated|"
    r"stood up|rolled|patched|provisioned|architected|delivered)\b", re.I)
_DEF_OPENER = re.compile(r"^\s*(to me,|.{0,30}\b(devops|sre|cloud|automation)\b[^.]{0,20}\bmeans\b)", re.I)

def _wordcount(t): return len(re.findall(r"\S+", t or ""))
def _sentcount(t): return len([x for x in re.split(r"(?<=[.!?])\s+", (t or "").strip()) if x.strip()])

def validate(text, genre, c, gaps, facts=None, current_title=None, target_role=None):
    fails = []
    t = (text or "").strip()
    if len(t) < 15:
        return ["empty"]
    # why-company grounding: if we handed the model verified facts, the draft must actually
    # use one (shares a distinctive token). Otherwise it went generic and ignored the posting.
    # grounding gate only applies to OPERATIONAL facts: if the posting yields only mission/slogan
    # copy, candidate-first correctly drops it and must not be penalized for "ignoring" it (Grok).
    if genre in ("why_company", "why_role") and facts:
        _opf = _operational_facts(facts)
        if _opf and not _jc.overlaps(t, _opf):
            fails.append("ignored_jd")
    if genre in ("why_company", "why_role"):
        _bad = why_claims_unowned(t)
        if _bad:
            fails.append("claims_unowned(%s)" % _bad)
        if not why_has_candidate_link(t):
            fails.append("no_candidate_link")
        if why_uses_title_as_role(t, current_title, target_role):
            fails.append("current_title_as_role")
        _fl = why_fluff(t, facts)
        if _fl:
            fails.append("fluff(%s)" % _fl)
        if why_tech_dump(t):
            fails.append("tech_dump")
        if why_rewrote_fact(t, facts):
            fails.append("rewrote_fact")
        if _BANNED_MID.search(t):
            fails.append("banned_opening_mid")
    if genre in ("owned_project", "technical_experience", "behavioral", "hypothetical", "why_company", "why_role"):
        if _BANNED_PREFIX.match(t):
            fails.append("banned_opening")
    w = _wordcount(t)
    if c.get("min_words") and w < c["min_words"] * 0.9:
        fails.append("under_min_words(%d/%d)" % (w, c["min_words"]))
    if c.get("max_words") and w > c["max_words"] * 1.1:
        fails.append("over_max_words(%d/%d)" % (w, c["max_words"]))
    s = _sentcount(t)
    if c.get("min_sent") and s < c["min_sent"]:
        fails.append("under_min_sentences(%d/%d)" % (s, c["min_sent"]))
    if c.get("max_sent") and s > c["max_sent"] + 1:
        fails.append("over_max_sentences(%d/%d)" % (s, c["max_sent"]))
    if genre == "owned_project" and not _ACTION_VERB.search(t):
        fails.append("no_action_verb")
    if genre == "hypothetical":
        if _DEF_OPENER.match(t) or re.search(r"\b(devops|sre)\b[^.]{0,15}\bmeans\b", t, re.I):
            fails.append("is_a_definition")
        if not re.search(r"\bi (would|'?d|would build|would use|could build|plan to)\b|i would\b", t, re.I):
            fails.append("not_a_proposal")
    # honesty: if the question probes a system the candidate lacks, the answer must own it
    gap_terms = list(gaps.get("hard", [])) + [d for d, _ in gaps.get("limited", [])]
    if gap_terms and not re.search(r"\b(have not|haven'?t|not worked|no direct|not owned|"
                                   r"limited|surface|closest|not used)\b", t, re.I):
        fails.append("gap_not_disclosed")
    return fails

# ---------------------------------------------------------------- honest gap answer
def _honest_gap(question, gaps, story, c):
    limit = c.get("max_chars") or 700
    hard = gaps.get("hard", [])
    limited = gaps.get("limited", [])
    clauses = []
    if hard:
        clauses.append("I have not worked directly with " + _a._human_list([h.lower() for h in hard]))
    for disp, note in limited:
        clauses.append("my experience with %s is %s" % (disp, note))
    joined = "; ".join(clauses).strip()
    disclosure = (joined[0].upper() + joined[1:] + ".") if joined else \
        "I want to be accurate about the scope of my direct experience here."
    if story:
        s = story.get("star", {})
        hero = (story.get("hero") or story.get("title", "") or "a related project").strip()
        detail = (s.get("action", "") + " " + s.get("result", "")).strip()
        ans = disclosure + " The most relevant related work I can point to: " + hero.rstrip(".") + "."
        if detail:
            ans += " " + detail
    else:
        ans = disclosure + " I would lean on my closest related experience and be upfront about the ramp."
    return _no_dash(_a.enforce_length(ans, {}, limit, False).strip())

# ---------------------------------------------------------------- main entry
def answer_essay(question, limit=None, company=None, url=None, model=None, want_meta=True, page_context=None, role=None, why_hybrid=False):
    q = (question or "").strip()
    if not q:
        return {"ok": False, "kind": "pause", "method": "essay", "chars": 0, "gaps": [],
                "text": "[ERROR] missing question"}
    c = parse_constraints(q, limit)
    genre = classify_genre(q, c)
    # applied-for role (never the candidate's current title) and the current title, for the
    # why-company role lock + validator.
    target_role = resolve_target_role(role, url) if genre in ("why_company", "why_role") else ""
    current_title = _current_title() if genre in ("why_company", "why_role") else ""
    if genre == "why_company":
        qco = _question_company(q)
        if qco and company and not _company_match(qco, company):
            mtext = ("This reads as a 'why %s' question, but the application is set to %s. I will not "
                     "answer for the wrong company. Set the company to %s and regenerate." % (qco, company, qco))
            return {"ok": True, "kind": "review", "method": "essay:ctx_mismatch", "genre": "why_company",
                    "chars": len(mtext), "gaps": ["company ctx mismatch"], "story": None, "text": mtext}
    if genre == "inventory":
        _inv = _enumerate_experience(q)
        if _inv:
            return {"ok": True, "kind": "answer", "method": "field:inventory",
                    "genre": "inventory", "grounding": "profile", "story": None,
                    "chars": len(_inv), "words": _wordcount(_inv), "gaps": [],
                    "review": False, "checks": [], "text": _inv}
    gaps = detect_gaps(q)
    want_owned = c.get("owned") or genre == "owned_project"
    story, score = retrieve_story(q, want_owned)

    stories = _load_stories()
    if not stories and genre in ("owned_project", "technical_experience", "behavioral"):
        return {"ok": False, "kind": "pause", "method": "needs-input", "genre": genre,
                "chars": 0, "gaps": [], "story": None,
                "text": "[NEEDS INPUT] Add at least one story in stories.yaml (Profile - Stories) "
                        "so this can be answered from your real work."}

    # Behavioral / influence questions need a real interpersonal example. The corpus is technical,
    # so a plain owned_project STAR here answers the wrong question. If no story actually carries
    # influence/conflict content, disclose honestly rather than fake it (Grok-agreed floor).
    if genre == "behavioral" and not _story_is_interpersonal(story):
        btext = _behavioral_gap(q, c)
        return {"ok": True, "kind": "answer", "method": "essay:behavioral_gap", "genre": "behavioral",
                "chars": len(btext), "gaps": [], "story": None, "text": btext}

    # domain gap (e.g. 'endpoint management' with no endpoint story) -> treat as a gap
    dgap = _domain_gap(q, story) if genre in ("owned_project", "technical_experience") else None
    if dgap:
        gaps = dict(gaps)
        if dgap.title() not in gaps["hard"]:
            gaps["hard"] = gaps["hard"] + [dgap]

    is_gap = bool(gaps["hard"] or gaps["limited"])

    # gap questions never go to the free model first - honest deterministic answer, flagged.
    # Cite the CLOSEST real story (ignore not_implied here - we are disclosing the gap, we
    # just want the nearest true work to point at).
    if is_gap:
        gstory, gscore = retrieve_story(q, True, ignore_not_implied=True)
        cite = gstory if (gscore is not None and gscore >= 1) else None
        honest = _honest_gap(q, gaps, cite, c)
        flag = list(gaps["hard"]) + [d for d, _ in gaps["limited"]]
        return {"ok": True, "kind": "answer", "method": "gap_analog", "genre": "gap_analog",
                "story": (cite or {}).get("id"),
                "chars": len(honest), "words": _wordcount(honest), "gaps": flag,
                "review": True, "checks": [], "text": honest}

    # why-company grounding: resolve the ONE verified posting fact up front (deterministic, no
    # model).
    facts, grounding, src_text = [], "none", ""
    if genre in ("why_company", "why_role"):
        try:
            src_text, grounding = _jc.resolve_job_text(url, page_context)
            if src_text:
                facts = _jc.extract_facts(src_text, company)
            if not facts:
                grounding = "none"
        except Exception:
            facts, grounding, src_text = [], "none", ""

    # why-company / why-role: TEMPLATE by default (per Grok review). The model lost 4/4 live and
    # only added latency + leak risk, so it does not run on a normal fill. The hybrid (model ->
    # strict gate -> template floor) is kept fully in place but dormant: it turns on only when
    # WHY_COMPANY_HYBRID=1, or on a per-field Regenerate (why_hybrid passed by serve.py for a
    # fresh, non-bulk request). When on but the model still fails the gate, the template ships
    # and 'checks' says so.
    if genre in ("why_company", "why_role"):
        _hybrid = why_hybrid or os.environ.get("WHY_COMPANY_HYBRID") == "1"
        if not (_hybrid and _a.ollama_up()):
            text = _why_candidate_only(facts, c, target_role, company, src_text)
            fails = validate(text, genre, c, gaps, facts, current_title, target_role)
            return {"ok": True, "kind": "answer", "method": ("review:" if fails else "essay:") + genre,
                    "genre": genre, "grounding": ("fact+template" if facts else "template"),
                    "story": None, "chars": len(text), "words": _wordcount(text), "gaps": [],
                    "review": bool(fails), "checks": fails, "text": text}
        # else: hybrid is ON and Ollama is up -> fall through to the model + gate below.

    if not _a.ollama_up():
        # why-company doesn't need the model - the template works offline.
        if genre in ("why_company", "why_role"):
            text = _why_candidate_only(facts, c, target_role, company, src_text)
            fails = validate(text, genre, c, gaps, facts, current_title, target_role)
            return {"ok": True, "kind": "answer", "method": ("review:" if fails else "essay:") + genre,
                    "genre": genre, "grounding": ("fact+template" if facts else "template"),
                    "story": None, "chars": len(text), "words": _wordcount(text), "gaps": [],
                    "review": bool(fails), "checks": fails, "text": text}
        if story:
            s = story.get("star", {})
            txt = _a.enforce_length("%s %s" % (s.get("action", ""), s.get("result", "")),
                                    {}, c.get("max_chars") or 900, False).strip()
            return {"ok": True, "kind": "answer", "method": "story-offline", "genre": genre,
                    "story": story.get("id"), "chars": len(txt), "words": _wordcount(txt),
                    "gaps": [], "review": True, "checks": ["ollama_offline"], "text": txt}
        return {"ok": False, "kind": "pause", "method": "essay", "genre": genre, "chars": 0,
                "gaps": [], "story": None, "text": "[PAUSE] Ollama offline and no story to fall back on."}

    sysp, user = build_prompt(genre, q, c, story, gaps, company, page_context, facts, target_role)
    model = model or _a.ESSAY_MODEL

    def _gen(extra=""):
        out = _a.ollama(user + extra, sysp, temperature=0.3, model=model).strip().strip('"')
        return _no_dash(_a.enforce_length(out, {"max_sentences": c.get("max_sent")}, c.get("max_chars"), False).strip())

    def _val(tx):
        return validate(tx, genre, c, gaps, facts, current_title, target_role)

    text = _gen()
    fails = _val(text)
    if fails:
        nudge = ("\n\nYour previous draft failed these checks: %s. Fix them. "
                 "Answer the question directly in the first sentence." % ", ".join(fails))
        if "ignored_jd" in fails and facts:
            nudge += (" You ignored the verified facts. Your FIRST sentence must start with one of "
                      "these, copied closely: " + " | ".join(facts))
        if any(f.startswith("claims_unowned") for f in fails):
            nudge += (" Your second sentence described the CANDIDATE, not the company, and claimed "
                      "work you have not done. Do NOT claim endpoint/MDM/device-management or any "
                      "system not in the candidate's real stack. Connect the fact ONLY to the "
                      "candidate's real experience.")
        if "no_candidate_link" in fails:
            nudge += (" Add one first-person sentence tying the fact to the candidate's real stack.")
        if "current_title_as_role" in fails and target_role:
            nudge += (" You called the job '%s', which is the candidate's CURRENT title. The role "
                      "being applied to is '%s'. Refer to the position as '%s' or 'this role', never "
                      "as the current title." % (current_title, target_role, target_role))
        if "no_action_verb" in fails:
            nudge += (" You only described the problem. Add what you DID (concrete action verbs) and "
                      "what CHANGED (the result).")
        if any(f.startswith("fluff") for f in fails):
            nudge += (" Cut the cliches (no 'aligns perfectly', 'strong fit', 'best practices', "
                      "'seamless', 'excited to contribute'). Say something concrete instead.")
        if "tech_dump" in fails:
            nudge += (" Do NOT list technologies. Name at most one, or none.")
        if "rewrote_fact" in fails:
            nudge += (" Do NOT say 'you're building'. Attribute the company's own words to the "
                      "company (e.g. \"1Password's focus on ...\"), do not put them in your voice.")
        if "banned_opening_mid" in fails:
            nudge += (" Do not use 'My focus on' or 'Throughout my career' anywhere.")
        text2 = _gen(nudge)
        f2 = _val(text2)
        if len(f2) <= len(fails):
            text, fails = text2, f2

    # HYBRID gate: the model got its shots. For why-company / why-role, if the best draft STILL
    # trips any quality gate (fluff, tech dump, unowned/endpoint claim, rewrote the fact, wrong
    # role, no candidate link), throw it away and ship the deterministic template. Clean model
    # prose survives; garbage never reaches the field.
    if genre in ("why_company", "why_role") and fails:
        text = _why_candidate_only(facts, c, target_role, company, src_text)
        grounding = ("fact+template" if facts else "template")
        fails = validate(text, genre, c, gaps, facts, current_title, target_role) + ["model rejected, template used"]

    # owned_project: model kept dropping the Action/Result -> compose straight from the story's
    # STAR fields so the review draft is the real work, not just the problem statement.
    if genre == "owned_project" and "no_action_verb" in fails and story:
        composed = _compose_star(story)
        if composed and _ACTION_VERB.search(composed):
            text = composed
            grounding = "star-template"
            fails = [f for f in _val(text) if f != "no_action_verb"]

    review = bool(fails)
    return {"ok": True, "kind": "answer",
            "method": ("review:" if review else "essay:") + genre,
            "genre": genre, "grounding": grounding,
            "story": ((story or {}).get("id") if (story and genre in ("owned_project", "technical_experience", "behavioral")) else None),
            "chars": len(text), "words": _wordcount(text), "gaps": [],
            "review": review, "checks": fails, "text": text}

# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--company")
    args = ap.parse_args()
    qq = " ".join(args.question).strip()
    r = answer_essay(qq, limit=args.limit, company=args.company)
    meta = {k: v for k, v in r.items() if k != "text"}
    print("# " + json.dumps(meta))
    print()
    print(r["text"])
