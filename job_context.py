#!/usr/bin/env python3
"""job_context.py - get REAL text about the exact job the user is applying to, so
"why this company" answers are grounded in the posting instead of the model's guesses.

The apply form itself is the wrong document: an Ashby/Greenhouse/Workday application view
is field labels and questions ("First name, Email, Why 1Password?"), not the job
description. So we resolve the source text in priority order:

  1. jobs.json  - Penates already stored a clean desc for API-discovered jobs (free)
  2. exact public ATS posting - the same public endpoints scan_ats already hits, for the
     one job in the URL (not a crawl, not company research)
  3. scored page_context - the DOM snapshot, but ONLY if it looks like a JD (reject form
     chrome so a form-only page does not masquerade as company facts)
  4. nothing -> caller uses the honest candidate-only fallback

Then extract_facts() pulls 1-3 near-verbatim clauses. The writer must open with one of
them; overlaps() lets the caller detect and regenerate when the model ignored them.

No cloud LLM, no web search, no company homepage scrape. Every fact traces to the exact
posting the user is applying to. stdlib + the existing sources/ adapters only.
"""
import re

# ------------------------------------------------------------------ URL parsing
def parse_ats_url(url):
    """Return (ats, board_slug, job_id) for a supported ATS apply/posting URL, else
    (None, None, None). board_slug is what the sources/ adapters call 'board'."""
    u = url or ""
    m = re.search(r"ashbyhq\.com/([^/?#]+)/([0-9a-fA-F-]{8,})", u)
    if m:
        return ("ashby", m.group(1), m.group(2))
    # greenhouse: boards.greenhouse.io/<slug>/jobs/<id>, job-boards.greenhouse.io/..., <slug>.greenhouse.io
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+)/jobs/(\d+)", u)
    if m:
        return ("greenhouse", m.group(1), m.group(2))
    m = re.search(r"([a-z0-9-]+)\.greenhouse\.io/.*?[?&]gh_jid=(\d+)", u, re.I)
    if m:
        return ("greenhouse", m.group(1), m.group(2))
    m = re.search(r"lever\.co/([^/?#]+)/([0-9a-fA-F-]{8,})", u)
    if m:
        return ("lever", m.group(1), m.group(2))
    return (None, None, None)


def _match_job(rows, ats, board, jid):
    aid = "%s:%s:%s" % (ats, board, jid)
    for r in rows:
        if r.get("ats_id") == aid or r.get("id") == jid:
            return r
    # last resort: the job id appears in the row url
    for r in rows:
        if jid and jid in (r.get("url") or ""):
            return r
    return None


def fetch_public_posting(url, timeout=6):
    """Fetch the ONE public posting named in the URL and return its plain-text description.
    Same public endpoints scan_ats uses, but a single job and a hard timeout so the fill never
    stalls on it. Best-effort: any failure returns "". Greenhouse/Lever have per-job endpoints;
    Ashby needs the board, filtered by id."""
    from sources import base
    ats, board, jid = parse_ats_url(url)
    if not ats:
        return ""
    try:
        if ats == "greenhouse":
            d = base.http_json(
                "https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s?content=true" % (board, jid),
                timeout=timeout)
            return base.strip_html(d.get("content") or "")
        if ats == "lever":
            d = base.http_json(
                "https://api.lever.co/v0/postings/%s/%s?mode=json" % (board, jid), timeout=timeout)
            if isinstance(d, list):
                d = d[0] if d else {}
            return base.strip_html(d.get("descriptionPlain") or d.get("description") or "")
        if ats == "ashby":
            d = base.http_json(
                "https://api.ashbyhq.com/posting-api/job-board/%s?includeCompensation=true" % board,
                timeout=timeout)
            for j in d.get("jobs", []) or []:
                if str(j.get("id")) == str(jid):
                    return base.strip_html(j.get("descriptionPlain") or j.get("descriptionHtml") or "")
        return ""
    except Exception:
        return ""


# ------------------------------------------------------------------ page_context scoring
# A real JD has prose sections and role language; a form has field labels and questions.
_JD_SIGNAL = re.compile(
    r"\b(about (the|us|the role|the team|the company)|what you'?ll do|what you will do|"
    r"responsibilities|requirements|qualifications|who you are|the role|our mission|"
    r"we'?re looking for|you'?ll be|day to day|nice to have|benefits|compensation)\b", re.I)

def score_page_context(text):
    """Return the text only if it looks like a job description, else "". A raw length check
    is not enough: an application form is long but is all field chrome."""
    t = (text or "").strip()
    if len(t) < 300:
        return ""
    if _JD_SIGNAL.search(t):
        return t
    # or: at least one long non-question paragraph (real prose, not a form label)
    for para in re.split(r"\n{2,}|(?<=[.!?])\s{2,}", t):
        p = para.strip()
        if len(re.findall(r"\S+", p)) >= 40 and not p.rstrip().endswith("?"):
            return t
    return ""


_JD_CACHE = {}   # url -> (text, source); one network fetch per posting per server run

def resolve_job_text(url, page_context=None):
    """The source chain. Returns (text, source_label) where source_label is one of
    'jobs.json', 'fetch:<ats>', 'page', or ''."""
    key = (url or "").split("#")[0]
    if key in _JD_CACHE:
        text, src = _JD_CACHE[key]
        if src == "page":      # page snapshot can differ per call; re-score it, keep cached fetch
            d = score_page_context(page_context)
            if d:
                return d, "page"
        elif text:
            return text, src
    out = _resolve_job_text(url, page_context)
    if out[1] in ("jobs.json", "fetch:ashby", "fetch:greenhouse", "fetch:lever"):
        _JD_CACHE[key] = out    # only cache the durable network/db sources
    return out

def _resolve_job_text(url, page_context=None):
    # 1. already-stored clean desc (free, no network)
    try:
        import jobs_store
        d = jobs_store.desc_for(url)
        if d and len(d) >= 200:
            return d, "jobs.json"
    except Exception:
        pass
    # 2. exact public posting
    d = fetch_public_posting(url)
    if d and len(d) >= 200:
        ats = parse_ats_url(url)[0] or "ats"
        return d, "fetch:" + ats
    # 3. scored page context
    d = score_page_context(page_context)
    if d:
        return d, "page"
    return "", ""


# ------------------------------------------------------------------ fact extraction
_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "we", "our",
         "you", "your", "is", "are", "be", "as", "at", "by", "that", "this", "will", "role",
         "team", "who", "what", "have", "has", "from", "their", "they", "it", "its", "us"}

def _sentences(text):
    t = re.sub(r"\s+", " ", text or "").strip()
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]

def _wc(s):
    return len(re.findall(r"\S+", s or ""))

def extract_facts(text, company=None):
    """Pull up to 3 near-verbatim, distinctive clauses from the posting: what the company
    does, one role/responsibility line, one distinctive constraint. Verbatim so the writer
    cannot drift; never synthesized. Returns a list of strings (possibly empty)."""
    sents = [s for s in _sentences(text) if 5 <= _wc(s) <= 45]
    facts = []
    def take(rx):
        for s in sents:
            if s in facts:
                continue
            if re.search(rx, s, re.I):
                facts.append(s.strip()[:220])
                return
    # what they build / who they are
    take(r"\b(we build|we'?re building|we are building|is a|are a|provides|platform for|"
         r"password manager|helps (companies|teams|people)|our mission|founded|"
         r"company that|leading|we make|we help|building the)\b")
    # role / responsibility
    take(r"\b(you'?ll|you will|responsible for|in this role|as (a|an|the)|join (our|the) team|"
         r"the role|this position|reporting to|own(ing)? the|drive)\b")
    # distinctive constraint / domain
    take(r"\b(remote|clearance|on-?call|nonprofit|non-profit|government|federal|healthcare|"
         r"fintech|security|compliance|24/?7|distributed|open source|mission-driven)\b")
    return facts[:3]


# Words that appear in almost every tech JD AND almost every DevOps candidate's stack, so
# sharing one is NOT evidence the draft used a company fact. Grounding must land on a
# distinctive noun (e.g. "password", "manager", "donor", "payments"), not this filler.
_GENERIC = {
    "cloud", "infrastructure", "security", "secure", "automation", "automated", "engineer",
    "engineering", "systems", "system", "platform", "platforms", "remote", "experience",
    "technology", "technologies", "technical", "solutions", "operations", "operational",
    "software", "development", "developer", "services", "service", "company", "companies",
    "position", "working", "building", "build", "operate", "manage", "management", "support",
    "environment", "environments", "scalable", "reliable", "modern", "business", "businesses",
    "customer", "customers", "product", "products", "online", "data", "teams", "people",
    "world", "global", "leading", "mission", "driven", "fully", "across", "using",
}

def _distinctive(s):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9+#.]{4,}", (s or "").lower())
            if w not in _STOP and w not in _GENERIC}

def overlaps(draft, facts):
    """True if the draft actually used the facts: it shares a distinctive (non-filler) token
    with a fact, OR copied a chunk of one near-verbatim. Catches the model ignoring the fact
    list and answering with its own generic stack. A cheap warning, not proof of grounding -
    the human still reviews."""
    if not facts:
        return True
    dl = (draft or "").lower()
    dt = _distinctive(dl)
    for f in facts:
        if _distinctive(f) & dt:
            return True
        # near-verbatim copy of a fact clause (>= 5-word run) even if all words are generic
        words = re.findall(r"[a-z0-9]+", f.lower())
        for i in range(0, max(0, len(words) - 4)):
            if " ".join(words[i:i + 5]) in dl:
                return True
    return False
