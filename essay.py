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

HERE = os.path.dirname(os.path.abspath(__file__))

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
    ("owned_project",r"describe (a|the|your).{0,40}(project|time|situation|example)|tell (us|me) about (a|the|your)|most (complex|challenging|difficult|impactful)|(a|one) (project|time|situation) (you|where you)|walk (us|me) through|you (personally )?(owned|led|built|architected)|give (us|me) an example|share an example"),
    ("technical_experience", r"what is your experience (with|in)|describe your .{0,30}experience|how (do|have) you (use|used)|rate your|proficiency (with|in)|how familiar are you|level of experience|how many years"),
    ("definition",   r"what does .{0,30}mean to you|how do you define|what is your definition|what do you (understand|think).{0,20}means"),
]
def classify_genre(question, constraints):
    q = (question or "").lower()
    # short_text: an explicit one-sentence / very short ask that is not a project story
    if (constraints.get("max_sent") == 1 or (constraints.get("max_words") or 999) < 40) \
       and not re.search(r"project|time you|example|describe a", q):
        return "short_text"
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
    best, bs = None, -10
    ql = (question or "").lower()
    for st in stories:
        hay = " ".join(list(st.get("domains", [])) + list(st.get("tools", [])) +
                       [st.get("title", ""), st.get("hero", "")])
        score = len(qt & _tokens(hay))
        if want_owned and st.get("owned"):
            score += 2
        if not ignore_not_implied:
            for ni in st.get("not_implied", []):
                if re.search(r"(?<![a-z0-9])" + re.escape(ni.lower()) + r"(?![a-z0-9])", ql):
                    score -= 5
        if score > bs:
            bs, best = score, st
    return best, bs

# ---------------------------------------------------------------- prompts
def _star_block(st):
    s = st.get("star", {}) or {}
    return ("STORY (the only facts you may use; do not add tools, employers, numbers, or "
            "outcomes that are not here):\n"
            "- Situation: %s\n- Task: %s\n- Action: %s\n- Result: %s\n- Tools you may name: %s"
            % (s.get("situation", ""), s.get("task", ""), s.get("action", ""),
               s.get("result", ""), ", ".join(st.get("tools", []))))

def build_prompt(genre, question, c, story, gaps, company, page_context=None, facts=None):
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
                       "or 'Throughout my career'. Name the specific project first, then your "
                       "role, what you did, and what changed. Only name tools listed under Tools.")
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
        stack = ""
        try:
            stack = _a._real_stack(_a.load("profile.yaml"))
        except Exception:
            pass
        if facts:
            # extract-then-write: the model gets 1-3 VERIFIED clauses from the posting and
            # must open with one. A small model ignores "use a page fact" when handed the
            # raw JD, so we hand it only the facts and enforce the opening in code.
            factlist = "\n".join("- " + f for f in facts)
            sysp = base + (" You are given VERIFIED FACTS about this company/role, taken from the job "
                           "posting. Your FIRST sentence must lead with ONE of those facts, copied closely "
                           "(quote or near-verbatim), and make it the reason this role fits. Then ONE or "
                           "TWO sentences connecting it to the candidate's real experience below. Do NOT "
                           "add any company product, customer, metric, award, or claim that is not in the "
                           "facts. Refer to the company by name.")
            user = ("QUESTION:\n%s\n\nCompany: %s\n\nVERIFIED FACTS (open with one, copy it closely; add "
                    "no company fact beyond these):\n%s\n\nThe candidate's real experience: %s\n\nAnswer:"
                    % (question, blurb, factlist, stack))
        else:
            # no verified job text -> honest, obviously non-researched. Role title is a role
            # fact and may be used; what the company sells may not be invented.
            sysp = base + (" You were given NO verified facts about the company, so you must not state, "
                           "describe, or guess anything about what it does, sells, or builds, or about its "
                           "products, features, technology, or reputation. Refer to the company ONLY by "
                           "name. Ground every sentence in the candidate's real experience and the role "
                           "title. Never write 'their product' or any capability of the company. If you "
                           "cannot say something specific and true, keep it about the candidate.")
            user = ("QUESTION:\n%s\n\nCompany name (use as a name only, invent no facts about it): %s\n"
                    "The candidate's real experience to ground every sentence in: %s\n\nAnswer:"
                    % (question, blurb, stack))
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

def validate(text, genre, c, gaps, facts=None):
    fails = []
    t = (text or "").strip()
    if len(t) < 15:
        return ["empty"]
    # why-company grounding: if we handed the model verified facts, the draft must actually
    # use one (shares a distinctive token). Otherwise it went generic and ignored the posting.
    if genre in ("why_company", "why_role") and facts and not _jc.overlaps(t, facts):
        fails.append("ignored_jd")
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
    disclosure = "To be straight about it, " + "; ".join(clauses) + "."
    if story:
        s = story.get("star", {})
        hero = story.get("hero") or story.get("title", "a related project")
        detail = (s.get("action", "") + " " + s.get("result", "")).strip()
        ans = disclosure + " My closest related work: " + hero
        if detail:
            ans += " " + detail
    else:
        ans = disclosure + " I would lean on my closest related experience and be upfront about the ramp."
    return _a.enforce_length(ans, {}, limit, False).strip()

# ---------------------------------------------------------------- main entry
def answer_essay(question, limit=None, company=None, url=None, model=None, want_meta=True, page_context=None):
    q = (question or "").strip()
    if not q:
        return {"ok": False, "kind": "pause", "method": "essay", "chars": 0, "gaps": [],
                "text": "[ERROR] missing question"}
    c = parse_constraints(q, limit)
    genre = classify_genre(q, c)
    gaps = detect_gaps(q)
    want_owned = c.get("owned") or genre == "owned_project"
    story, score = retrieve_story(q, want_owned)

    stories = _load_stories()
    if not stories and genre in ("owned_project", "technical_experience", "behavioral"):
        return {"ok": False, "kind": "pause", "method": "needs-input", "genre": genre,
                "chars": 0, "gaps": [], "story": None,
                "text": "[NEEDS INPUT] Add at least one story in stories.yaml (Profile - Stories) "
                        "so this can be answered from your real work."}

    # domain gap (e.g. 'endpoint management' with no endpoint story) -> treat as a gap
    dgap = _domain_gap(q, story) if genre in ("owned_project", "technical_experience") else None
    if dgap and (score is None or score < 2):
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

    if not _a.ollama_up():
        if story:
            s = story.get("star", {})
            txt = _a.enforce_length("%s %s" % (s.get("action", ""), s.get("result", "")),
                                    {}, c.get("max_chars") or 900, False).strip()
            return {"ok": True, "kind": "answer", "method": "story-offline", "genre": genre,
                    "story": story.get("id"), "chars": len(txt), "words": _wordcount(txt),
                    "gaps": [], "review": True, "checks": ["ollama_offline"], "text": txt}
        return {"ok": False, "kind": "pause", "method": "essay", "genre": genre, "chars": 0,
                "gaps": [], "story": None, "text": "[PAUSE] Ollama offline and no story to fall back on."}

    # why-company grounding: resolve REAL text about the exact job (stored desc -> public
    # posting -> scored page snapshot), then extract 1-3 verified facts for the writer to
    # open with. No facts -> honest candidate-only. Never invents company facts.
    facts, grounding = [], "none"
    if genre in ("why_company", "why_role"):
        try:
            src_text, grounding = _jc.resolve_job_text(url, page_context)
            if src_text:
                facts = _jc.extract_facts(src_text, company)
            if not facts:
                grounding = "none"
        except Exception:
            facts, grounding = [], "none"

    sysp, user = build_prompt(genre, q, c, story, gaps, company, page_context, facts)
    model = model or _a.ESSAY_MODEL

    def _gen(extra=""):
        out = _a.ollama(user + extra, sysp, temperature=0.3, model=model).strip().strip('"')
        return _a.enforce_length(out, {"max_sentences": c.get("max_sent")}, c.get("max_chars"), False).strip()

    text = _gen()
    fails = validate(text, genre, c, gaps, facts)
    if fails:
        nudge = ("\n\nYour previous draft failed these checks: %s. Fix them. "
                 "Answer the question directly in the first sentence." % ", ".join(fails))
        if "ignored_jd" in fails and facts:
            nudge += (" You ignored the verified facts. Your FIRST sentence must start with one of "
                      "these, copied closely: " + " | ".join(facts))
        text2 = _gen(nudge)
        f2 = validate(text2, genre, c, gaps, facts)
        if len(f2) <= len(fails):
            text, fails = text2, f2

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
