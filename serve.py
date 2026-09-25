#!/usr/bin/env python3
"""Local server for the apply tool. Listens on 127.0.0.1:8765.

Routes:
  (the old /apply Playwright launcher was removed; the extension fills in your normal Chrome)
  /answer  (POST)  -> JSON {question, limit?, company?, url?, fresh?, kind?, options?}
                      -> grounded answer. Tiers: structured field -> intent template ->
                      LEARNED (your vetted past answers) -> compose (LLM + gap guard).
                      method=field responses include field:<yaml value path>.
                      options non-empty: choice path only (never genre/essay/story).
                      kind=text: literals, else one short model line. kind=textarea:
                      essay ladder, review true.
  /answer-batch (POST) -> JSON {questions: [str|{q|question, kind?, limit?, options?,
                      required?}], company?, role?, url?, page_context?, bulk?, fresh?,
                      bulk_skip_essays?, options?}
                      -> {ok, answers: [{ok, method, tier, ms, text, kind, review, q}
                      | {q, ok:false, __error}]}.
                      answers[i] matches questions[i]. Deterministic items run first;
                      model items run in parallel with a cap of 3. Defaults bulk=true
                      and bulk_skip_essays=true (essays stay click-to-draft).
                      page_context is sent once and applied to every item.
  /learn   (POST)  -> JSON {question, answer, company?} -> remember YOUR final
                      answer, keyed by the normalized question, so the same
                      essay question returns your vetted words next time.
                      Company-specific "why this company" answers are NOT stored
                      (they don't transfer across postings).
  /api/profile (GET) -> structured sections (not raw yaml).
  /api/profile/<section> (PUT) -> validate, file lock, timestamped .bak,
                      clear data/answer_cache.json. Sections: identity, education,
                      clearance, eeo, compensation, candidate, eligibility,
                      application_defaults, screening, skills, limited_or_none,
                      limited_experience, references, work_history.
                      Tests may point PROFILE_PATH / WORK_HISTORY_PATH /
                      ANSWER_CACHE_PATH at temp copies. .bak lands beside that path.
  /api/imports/promote-story (POST) -> {key} one local-model STAR draft.
                      Gap-checks limited_or_none. Does not write stories.yaml.
  A required question that asks the candidate to provide references is filled
  from profile.references. Optional, willingness, and referred-by stay as before.
  /ranked  (GET)   -> scored job list. Optional filters (AND, post-sort):
                      remote_only=0|1, us_only=0|1, salary_min=<int annual USD>.
                      Structured fields first; text fallback only when empty.
                      Missing/unknown salary or location is KEPT (not hidden).

Leave this window running in the background.
"""
import os, sys, re, json, time, subprocess, threading, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
LEARN_PATH = os.path.join(HERE, "data", "learned_answers.json")
IMPORTED_PATH = os.path.join(HERE, "data", "imported_answers.json")
# Bump when essay/apply answer behavior changes so stale learned entries miss.
ENGINE_REV = 1

def _resume_path():
    """Absolute path to the resume to attach. Precedence: RESUME_PATH env / profile.yaml
    'resume_file', else the most recently modified .pdf/.docx under documents/resumes/."""
    cand = os.environ.get("RESUME_PATH")
    if cand and os.path.isfile(cand):
        return cand
    try:
        import apply as _a
        rf = (_a.load("profile.yaml") or {}).get("resume_file")
        if rf:
            rf = rf if os.path.isabs(rf) else os.path.join(HERE, rf)
            if os.path.isfile(rf):
                return rf
    except Exception:
        pass
    rdir = os.path.join(HERE, "documents", "resumes")
    try:
        files = [os.path.join(rdir, f) for f in os.listdir(rdir)
                 if f.lower().endswith((".pdf", ".docx"))]
        files = [f for f in files if os.path.isfile(f)]
        if files:
            return max(files, key=os.path.getmtime)
    except Exception:
        pass
    return None

def imported_lookup(q):
    """A prior answer imported from an AI-chat export (import_qa.py). Untrusted reference:
    exact normalized-question match, else a high-overlap near match. Returned as a REVIEW
    draft, never auto-trusted."""
    try:
        store = json.load(open(IMPORTED_PATH, encoding="utf-8"))
        answers = store.get("answers", {}) or {}
    except Exception:
        return None
    if not answers:
        return None
    nq = _norm_q(q)
    rec = answers.get(nq)
    if not rec:
        qt = set(nq.split())
        if not qt:
            return None
        best, bs = None, 0.0
        for k, r in answers.items():
            kt = set(k.split())
            if not kt:
                continue
            j = len(qt & kt) / float(len(qt | kt))
            if j > bs:
                bs, best = j, r
        if bs >= 0.72:
            rec = best
        else:
            return None
    return rec if rec and rec.get("variants") else None

# ---- local model (Ollama): status, model choice, test -------------------------
# The chosen models live in config.json (ollama_model / ollama_essay_model) and are applied to
# apply.MODEL / apply.ESSAY_MODEL, which every model call reads at call time: no restart needed.
def _ollama_root():
    import apply as _a
    return _a.OLLAMA_ROOT


def _ollama_models(timeout=3):
    req = urllib.request.Request(_ollama_root() + "/api/tags")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8") or "{}")
    return sorted(m.get("name") for m in (data.get("models") or []) if m.get("name"))


def apply_model_config():
    """Load the saved model choice into apply.* (startup and after a save)."""
    try:
        import apply as _a, jobs_store
        cfg = jobs_store.load_config()
        if cfg.get("ollama_model"):
            _a.MODEL = cfg["ollama_model"]
        if cfg.get("ollama_essay_model"):
            _a.ESSAY_MODEL = cfg["ollama_essay_model"]
    except Exception:
        pass


def ollama_status():
    import apply as _a
    out = {"ok": True, "base": _ollama_root(), "model": _a.MODEL, "essay_model": _a.ESSAY_MODEL}
    try:
        out["models"] = _ollama_models()
        out["reachable"] = True
    except Exception as e:
        out["models"] = []
        out["reachable"] = False
        out["error"] = str(e)[:160]
    out["model_installed"] = out["model"] in out["models"]
    out["essay_model_installed"] = out["essay_model"] in out["models"]
    return out


def ollama_set(payload):
    import apply as _a, jobs_store
    try:
        installed = _ollama_models()
    except Exception:
        return 503, {"ok": False, "error": "Ollama is not reachable at " + _ollama_root()}
    upd = {}
    for key, cfgkey in (("model", "ollama_model"), ("essay_model", "ollama_essay_model")):
        v = (payload or {}).get(key)
        if v:
            if v not in installed:
                return 400, {"ok": False, "error": "%s is not installed (ollama pull %s)" % (v, v)}
            upd[cfgkey] = v
    if not upd:
        return 400, {"ok": False, "error": "nothing to save"}
    cfg = jobs_store.load_config()
    cfg.update(upd)
    jobs_store.save_config(cfg)
    apply_model_config()
    _clear_answer_cache()
    return 200, ollama_status()


def ollama_test(model=None):
    import apply as _a
    t0 = time.time()
    try:
        txt = _a.ollama("Reply with exactly: OK", "You are a connectivity check.", temperature=0,
                        model=model or _a.MODEL)
        return {"ok": True, "model": model or _a.MODEL, "reply": str(txt).strip()[:80],
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "model": model or _a.MODEL, "error": str(e)[:200],
                "ms": int((time.time() - t0) * 1000)}


# ---- learned-answer store (correction memory) ------------------------------
def _norm_q(q):
    """Normalize a question to a stable key: lowercase, punctuation to spaces,
    collapse whitespace. 'Describe a time...' == 'describe a time'."""
    return re.sub(r"[^a-z0-9]+", " ", (q or "").lower()).strip()

def _norm_answer(t):
    """Normalize answer text for near-equality: lowercase, punct -> spaces, collapse ws."""
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()

def _content_hash(t):
    import hashlib
    return hashlib.sha256(_norm_answer(t).encode("utf-8")).hexdigest()[:16]

def _load_learned():
    try:
        with open(LEARN_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_learned(store):
    try:
        os.makedirs(os.path.dirname(LEARN_PATH), exist_ok=True)
        with open(LEARN_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=1, ensure_ascii=False)
    except Exception as e:
        print("[learn] save failed: %s" % e, flush=True)

_WHY_COMPANY_STOP = frozenset(
    ("do", "are", "would", "you", "us", "this", "the", "our", "i", "here", "there",
     "that", "they", "we", "a", "an", "my", "your", "their", "role", "job", "team",
     "position", "company", "organization", "work", "join"))

def _question_company_token(q):
    """Company token from a 'why <Company>' / 'work at <Company>' question, or None."""
    # re.I so 'Why 1Password?' matches; stop-list filters 'why do/are/this/...'.
    m = re.search(r"\bwhy\s+([A-Z0-9][\w&.\-]*(?:\s+[A-Z0-9][\w&.\-]*){0,3})", q or "", re.I)
    if not m:
        m = re.search(
            r"(?:work (?:at|for)|join|part of)\s+([A-Z0-9][\w&.\-]*(?:\s+[A-Z0-9][\w&.\-]*){0,3})",
            q or "", re.I)
    if not m:
        return None
    cand = re.sub(r"\s+(there|out|here|today).*$", "", m.group(1).strip().rstrip("?.,!"),
                  flags=re.I).strip()
    if cand and cand.lower() not in _WHY_COMPANY_STOP:
        return cand
    return None

def _company_specific(q, answer, company):
    """True if the answer is tied to a specific company and must NOT be reused
    across postings (the cross-contamination guard, at the learning layer)."""
    ql = (q or "").lower()
    if re.search(r"why.*(this )?(company|us|here|role|team|position|job)|"
                 r"interested in (working|this)|want to (work|join)|drew you|"
                 r"excites you about|why do you want", ql):
        return True
    # why-<Company> by company TOKEN in the question (not keyword-list alone)
    if _question_company_token(q):
        return True
    if company and len(company) >= 2 and re.search(
            r"\bwhy\s+" + re.escape(company) + r"\b", q or "", re.I):
        return True
    if company and len(company) >= 4 and company.lower() in (answer or "").lower():
        return True
    return False

def learned_lookup(q):
    store = _load_learned()
    e = store.get(_norm_q(q))
    if not e:
        return None
    # Version gate: missing or mismatched engine_rev => stale, fall through to live engine.
    if e.get("engine_rev") != ENGINE_REV:
        return None
    # bump usage counter (best-effort)
    try:
        e["uses"] = int(e.get("uses", 0)) + 1
        _save_learned(store)
    except Exception:
        pass
    return e.get("answer") or None

def _engine_draft_text(payload):
    """Fresh engine answer for the same question (no learned/imported), for learn-diff."""
    p = {
        "question": payload.get("question"),
        "limit": payload.get("limit"),
        "company": payload.get("company"),
        "role": payload.get("role"),
        "url": payload.get("url"),
        "page_context": payload.get("page_context"),
        "fresh": True,
        "_engine_only": True,
    }
    try:
        out = do_answer(p)
        return (out or {}).get("text") or ""
    except Exception as e:
        print("[learn] engine draft failed: %s" % e, flush=True)
        return ""

def do_learn(payload):
    q = (payload.get("question") or "").strip()
    a = (payload.get("answer") or "").strip()
    company = (payload.get("company") or "").strip()
    if not q or len(a) < 20:
        return {"ok": False, "skipped": "too short or empty (structured values aren't learned)"}
    if _company_specific(q, a, company):
        return {"ok": False, "skipped": "company-specific answer (not reused across postings)"}
    # Learn ONLY on a real correction: inserted text must meaningfully differ from the
    # engine's own draft (normalized). Near-identical insert = no-op (stop re-caching us).
    draft = _engine_draft_text(payload)
    if draft and _norm_answer(a) == _norm_answer(draft):
        return {"ok": False, "skipped": "identical to engine draft (not a correction)"}
    store = _load_learned()
    k = _norm_q(q)
    prev = store.get(k, {})
    store[k] = {"question": q[:400], "answer": a, "company_seen": company[:80],
                "uses": int(prev.get("uses", 0)), "updated": int(time.time()),
                "engine_rev": ENGINE_REV,
                "content_hash": _content_hash(a),
                "draft_hash": _content_hash(draft) if draft else ""}
    _save_learned(store)
    return {"ok": True, "key": k, "count": len(store)}

# ---- answer resolution -----------------------------------------------------
def _shape(r):
    out = {
        "ok": r.get("kind") in ("answer", "field"),
        "kind": r.get("kind"),
        "method": r.get("method"),
        "chars": r.get("chars"),
        "gaps": r.get("gaps", []),
        "text": r.get("text", ""),
    }
    # Additive: yaml value path on method=field hits (e.g. identity.linkedin, literal:Yes)
    if r.get("field"):
        out["field"] = r["field"]
    if r.get("reason"):
        out["reason"] = r["reason"]
    return out


def _match_field_key(label, fields):
    """Re-derive the fields.yaml value path for a label (serve.py lane; apply.match_field
    only returns resolved text). Same first-match-wins order as match_field."""
    lab = (label or "").lower()
    for rule in (fields or {}).get("fields") or []:
        try:
            if re.search(rule["pattern"], lab, re.I):
                return rule.get("value")
        except re.error:
            continue
    return None


def _norm_option(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _pick_option(text, options):
    """Return the original option string whose normalized form equals text, else None."""
    nt = _norm_option(text)
    if not nt:
        return None
    for o in options:
        if _norm_option(o) == nt:
            return o
    return None


def _yes_no_from_text(text):
    """Yes/No from a profile or yaml value. 'Yes, occasionally' is Yes.

    Word-boundary so 'notice' and 'yesterday' do not match. Longer qualifiers
    ('Yes, up to 25%') still count as Yes; the caller only applies that when
    the options themselves are yes/no-shaped.
    """
    t = _norm_option(text)
    if t in ("yes", "y", "true"):
        return "Yes"
    if t in ("no", "n", "false"):
        return "No"
    m = re.match(r"^(yes|no)\b", t)
    if not m:
        return None
    return "Yes" if m.group(1) == "yes" else "No"


def _pick_yes_no_option(yn, options):
    """Map Yes/No onto the closest option label (Yes/No/Y/N/True/False/...)."""
    hit = _pick_option(yn, options)
    if hit is not None:
        return hit
    want_yes = yn == "Yes"
    for o in options:
        no = _norm_option(o)
        if want_yes and (no in ("yes", "y", "true") or (no.startswith("yes") and len(no) <= 12)):
            return o
        if (not want_yes) and (no in ("no", "n", "false") or (no.startswith("no") and len(no) <= 12)):
            return o
    return None


def _constrain_to_options(out, options, q):
    """When the client sends choice labels, never return free-text/essay.
    Prefer a normalized match to an option; else Yes/No coerce; else review leave-blank.
    """
    opts = [str(o) for o in (options or []) if o is not None and str(o).strip()]
    if not opts:
        return out
    out = dict(out) if isinstance(out, dict) else {"ok": False, "text": str(out)}
    text = out.get("text") or ""
    matched = _pick_option(text, opts)
    if matched is not None:
        out["text"] = matched
        out["chars"] = len(matched)
        out["ok"] = True
        if out.get("kind") not in ("answer", "field"):
            out["kind"] = "field"
        return out
    yn = _yes_no_from_text(text)
    if yn:
        matched = _pick_yes_no_option(yn, opts)
        if matched is not None:
            out["text"] = matched
            out["chars"] = len(matched)
            out["ok"] = True
            out["kind"] = "field"
            out.setdefault("method", "options")
            out["gaps"] = out.get("gaps") or []
            return out
    # Options present but no safe match — leave blank (do not ship a story into a radio)
    return {
        "ok": False,
        "kind": "review",
        "method": "options-miss",
        "chars": 0,
        "gaps": [],
        "text": "",
        "reason": "answer not in options; left blank",
    }

# Server-side field guards - the single chokepoint every field passes through, so they hold
# even if the extension is stale. Order matters: SKIP beats the yaml matcher beats the essay.
#  - _SKIP_CONDITIONAL: "If you responded 'yes'/'other'..." follow-ups. The controlling Yes/No
#    is usually unset, and answering blind pulls a wrong-context match or invents specifics.
#  - _SKIP_LEAVEBLANK: open optional prompts (accommodations, "anything else", additional info).
#    No grounded answer exists; matching here leaked race "White" into an accommodations box that
#    merely said "other than your ethnicity".
#  - _COI_NO: a personal-relationship / conflict-of-interest Yes/No -> deterministic No.
#  - _FORMER_EMPLOYER_Q: "have you worked here / for <co>" checked against work_history.yaml.
#    Runs before fields.yaml so the static former-employee -> No rule cannot hide a real match.
#    "worked with <tool>" is not this question unless it also names employment.
_SKIP_CONDITIONAL = re.compile(
    r"\bif you (responded|answered|selected|indicated|checked|chose)\b|"
    r"\bif (yes|no|other|so|applicable|not|the above|you did)\b", re.I)
def _is_conditional_followup(q):
    """True only for a standalone follow-up field whose prompt IS the conditional ('If you
    responded yes, describe...'). A primary question with a trailing conditional clause
    (veteran: 'Are you a ... member ...? If so, what capacity?') is NOT a follow-up - the
    conditional trails a complete question, so it must still be answered."""
    m = _SKIP_CONDITIONAL.search(q or "")
    if not m:
        return False
    # a complete question (its own '?') before the conditional marker => primary, not follow-up
    return "?" not in (q or "")[:m.start()]
_SKIP_LEAVEBLANK = re.compile(
    r"accommodat|other than your|is there anything|anything (else|you.?d like|we should know)|"
    r"additional (information|comments|details)|feel free to (add|share|include)", re.I)
# Match the COI question robustly. The client truncates the question to ~160 chars and it
# contains "e.g." periods, so a distance match on [^.?] breaks and "conflict of interest" can be
# cut off. Anchor on the phrases that always survive: "close personal relationship(s)" or a
# relationship word followed (anywhere, periods allowed) by a working/employed token.
_COI_NO = re.compile(
    r"conflict of interest|close personal relationship|"
    r"(family member|domestic partner|friend)[\s\S]{0,140}(working|employed|currently (at|with|working))",
    re.I)
# Prior-employment Yes/No. Auth-to-work questions must not match (no "authorized"/"eligible").
# Bare "have you worked with <tool>" is experience, not a former-employer check.
_FORMER_EMPLOYER_Q = re.compile(
    r"(?:"
    r"have you(?: ever)? (?:worked|work) (?:for|at)\b"
    r"|have you(?: ever)? been employed\b"
    r"|were you(?: ever)? employed\b"
    r"|previously (?:employed|worked)\b"
    r"|formerly employed\b"
    r"|(?:former|previous) employee\b"
    r"|\brehire\b"
    r"|boomerang(?: employee)?\b"
    r"|affiliates and subsidiaries"
    r"|as an associate, intern, or contractor"
    r")",
    re.I)
_WORKED_WITH_EMPLOYER = re.compile(
    r"have you(?: ever)? (?:worked|work) with\b[\s\S]{0,80}"
    r"(?:employee|employed|employer|company|affiliate|subsidiar|contractor|rehire|boomerang|\bus\b|\bour\b)",
    re.I)
_Q_COMPANY = re.compile(
    r"\b(?:worked|work|employed)\s+(?:for|at|by)\s+"
    r"([A-Z0-9][\w&.'’/-]*(?:\s+[A-Z0-9][\w&.'’/-]*){0,5})")
_LEGAL_SUFFIX = re.compile(
    r"\b(?:incorporated|inc|llc|ltd|limited|corp|corporation|company|plc|co)\b")
_CO_STOP = {"the", "and", "of", "a", "an"}


def _company_tokens(name):
    """Case/punct/Inc/LLC-normalized tokens. Slashes become spaces (alias split)."""
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = _LEGAL_SUFFIX.sub(" ", s)
    return [t for t in s.split() if t and t not in _CO_STOP]


def _token_slice_match(left, right):
    """Equal, or the shorter multi-word name is a consecutive slice of the longer."""
    if not left or not right:
        return False
    if left == right:
        return True
    short, long_ = (left, right) if len(left) <= len(right) else (right, left)
    if len(short) < 2:
        return False
    n = len(short)
    return any(long_[i:i + n] == short for i in range(len(long_) - n + 1))


def _companies_match(target, employer):
    """Both ways: 'Acme' hits 'Acme/Globex'; a leading phrase hits the legal name.

    Slash/pipe components are compared on their own so either alias matches.
    A one-word name matches only the first token of the other (not a trailing 'Solutions').
    """
    t_parts = [p for p in re.split(r"[/|]", target or "") if p.strip()]
    e_parts = [p for p in re.split(r"[/|]", employer or "") if p.strip()]
    pairs = [(target, employer)]
    pairs += [(a, b) for a in t_parts for b in e_parts]
    for a, b in pairs:
        ta, eb = _company_tokens(a), _company_tokens(b)
        if _token_slice_match(ta, eb):
            return True
        if len(ta) == 1 and len(ta[0]) >= 4 and eb and eb[0] == ta[0]:
            return True
        if len(eb) == 1 and len(eb[0]) >= 4 and ta and ta[0] == eb[0]:
            return True
    return False


def _company_from_question(q):
    m = _Q_COMPANY.search(q or "")
    if not m:
        return ""
    name = m.group(1)
    name = re.split(r"\s+(?:or|as|and|including|any)\b|,|\?|:", name, maxsplit=1, flags=re.I)[0]
    return name.strip(" .")


def _work_history_companies():
    wh = _load_yaml_dict(_work_history_path()) if os.path.exists(_work_history_path()) else {}
    jobs = wh.get("jobs") if isinstance(wh, dict) else (wh or [])
    out = []
    for j in jobs or []:
        c = (j.get("company") or "").strip()
        if c and c not in out:
            out.append(c)
    return out


def _former_employer_answer(question, company):
    """Yes/No from work_history, or None when this is not a prior-employment question.

    Payload company wins (application companyGuess). Otherwise a capitalized name
    after worked/employed for|at|by. No company to check -> No (do not infer).
    """
    q = question or ""
    if not (_FORMER_EMPLOYER_Q.search(q) or _WORKED_WITH_EMPLOYER.search(q)):
        return None
    target = (company or "").strip() or _company_from_question(q)
    if not target:
        return "No"
    try:
        employers = _work_history_companies()
    except Exception:
        return "No"
    return "Yes" if any(_companies_match(target, e) for e in employers) else "No"

def _story_at_floor(essay_mod, question, story):
    """True when retrieve_story's pick shares >=1 domain/tool token with the question.

    That is the retrieval floor. The raw score is not: owned +2 and title/hero words
    inflate it, and retrieve_story does not return topic_hits. Same tokenizer as
    essay.retrieve_story. A title-only overlap stays below the floor so imported
    can still be the Regenerate reference.
    """
    if not story:
        return False
    qt = essay_mod._tokens(question)
    blob = " ".join(list(story.get("domains") or []) + list(story.get("tools") or []))
    return len(qt & essay_mod._tokens(blob)) >= 1


_MODEL_CAP = 3
_CHOICE_SYSTEM = (
    "You fill one job-application choice field. Reply with the option number only. "
    "No words. If the question says select all that apply, reply with the numbers "
    "separated by spaces. Use only the candidate facts. Never claim a skill that "
    "is not in the fact sheet. If unsure, pick No or the lowest option. "
    "Do not invent experience."
)
# Conditionals that do not apply to this candidate (under 18, referral follow-up,
# "if yes, explain"). Optional -> blank. Required choice -> N/A, else No.
_INAPPLICABLE_RE = re.compile(
    r"\bif under 18\b"
    r"|\bif you(?: are|'re)? under 18\b"
    r"|\bif referred\b"
    r"|\bif an employee\b[\s\S]{0,120}\breferred\b"
    r"|\bif yes\b[\s\S]{0,60}\b(?:please\s+)?(?:explain|describe|provide|list|elaborate)\b",
    re.I)
# Someone else's name. Blank unless a fields.yaml literal says otherwise.
# "reference" also covers "please provide professional references".
_OTHER_PERSON_RE = re.compile(
    r"\breferr(?:ed|al)\b|\breferee\b|\breferences?\b|emergency contact|"
    r"\bsupervisor\b|manager name",
    re.I)
_DEGREE_Q_RE = re.compile(
    r"highest.*(degree|education)|education level|level of education|"
    r"degree or level of education",
    re.I)
_NA_OPT_RE = re.compile(
    r"\b(n/?a|not applicable|does not apply|do not apply)\b", re.I)
_NAMED_YEARS_RE = re.compile(
    r"\b(sql|oracle|sap|crystal|postgres|postgresql|mysql|jenkins|java|python|"
    r"kubernetes|docker|terraform|ansible|splunk|salesforce)\b",
    re.I)
_SELECT_ALL_RE = re.compile(r"select all that apply", re.I)
_AT_LEAST_YEARS_RE = re.compile(
    r"at least\s+(\d+)\s*\+?\s*years|(\d+)\s*\+\s*years of", re.I)


def _options_clean(options):
    if not isinstance(options, list):
        return []
    return [str(o).strip() for o in options if o is not None and str(o).strip()]


def _norm_key(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _required_flag(payload):
    if not isinstance(payload, dict) or "required" not in payload:
        return True
    return bool(payload.get("required"))


def _tier_name(method):
    m = (method or "").lower()
    if m in ("leave-blank",):
        return "0"
    if m in ("field", "optional-skip", "references"):
        return "1"
    if m in ("choice-llm", "choice-unknown"):
        return "choice-llm"
    if m in ("short-llm", "short-unknown"):
        return "short-llm"
    if m.startswith("essay") or m.startswith("review:") or m.startswith("field:"):
        return "essay"
    if m == "learned":
        return "learned"
    if m == "imported":
        return "imported"
    if m == "error":
        return "error"
    return m or "essay"


def _pick_na_option(opts):
    """N/A-shaped option, if the control has one."""
    for o in opts or []:
        if _NA_OPT_RE.search(o or ""):
            return o
    return None


def _blank_review(method="leave-blank"):
    return {"ok": False, "kind": "pause", "method": method,
            "chars": 0, "gaps": [], "text": "", "review": True}


def _explicit_literal(q, required):
    """Winning fields.yaml value when that rule is literal:. Profile paths are not literals."""
    try:
        import apply as _apply
        fields = _apply.load("fields.yaml") or {}
        key = _match_field_key(q, fields)
        if not key or not str(key).startswith("literal:"):
            return None
        prof = _load_profile_doc()
        val, how = _apply.match_field(q, prof, fields, required=required)
    except Exception:
        return None
    if how == "field" and val not in (None, ""):
        return str(val)
    return None


def _inapplicable_result(payload):
    """Conditional that does not apply. None if the question is not one of these."""
    q = (payload.get("question") or "").strip()
    if not _INAPPLICABLE_RE.search(q):
        return None
    opts = _options_clean(payload.get("options"))
    if not _required_flag(payload):
        return _blank_review()
    if opts:
        pick = _pick_na_option(opts) or _pick_yes_no_option("No", opts)
        if pick:
            return {"ok": True, "kind": "field", "method": "field",
                    "chars": len(pick), "gaps": [], "text": pick, "review": False}
    return _blank_review()


# Required "please provide N professional references" only. Willingness
# ("willing to provide references") stays a fields.yaml literal. Referred-by,
# emergency contact, and supervisor name stay blank.
_PROVIDE_REFS_RE = re.compile(
    r"\b(?:provide|list|give|include|enter|supply|share)\b[\s\S]{0,80}\breferences?\b"
    r"|\bprofessional references\b",
    re.I)
_REF_WILLING_RE = re.compile(r"\b(?:willing|able) to provide\b", re.I)
_REF_COUNT_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_REF_LINE_KEYS = ("name", "title", "company", "relationship", "email", "phone")


def _asks_to_provide_references(q):
    if _REF_WILLING_RE.search(q or ""):
        return False
    return bool(_PROVIDE_REFS_RE.search(q or ""))


def _reference_count(q):
    """How many references the question asks for. None = no number stated."""
    m = re.search(r"\((\d+)\)", q or "")
    if m:
        n = int(m.group(1))
        if 1 <= n <= 10:
            return n
    m = re.search(r"\b(\d+)\b", q or "")
    if m:
        n = int(m.group(1))
        if 1 <= n <= 10:
            return n
    for word, n in _REF_COUNT_WORDS.items():
        if re.search(r"\b%s\b" % word, q or "", re.I):
            return n
    return None


def _load_profile_doc():
    try:
        return _load_yaml_dict(_profile_path())
    except Exception:
        return {}


def _profile_reference_lines():
    """One line per saved reference. No years_known. Empty fields are omitted."""
    raw = _load_profile_doc().get("references")
    if not isinstance(raw, list):
        return []
    lines = []
    for item in raw:
        cleaned = _clean_ref(item) if isinstance(item, dict) else None
        if not cleaned:
            continue
        parts = [str(cleaned.get(k) or "").strip() for k in _REF_LINE_KEYS]
        line = ", ".join(p for p in parts if p)
        if line:
            lines.append(line)
    return lines


def _references_answer(q):
    """Fill required provide-references from profile.references. Never logs the lines."""
    lines = _profile_reference_lines()
    if not lines:
        return _blank_review()
    asked = _reference_count(q)
    if asked is None:
        used = lines
        short = False
    else:
        used = lines[:asked]
        short = len(lines) < asked
    text = "\n".join(used)
    return {"ok": True, "kind": "field", "method": "references",
            "chars": len(text), "gaps": [], "text": text, "review": short}


def _other_person_result(payload):
    """Referrer / reference / emergency / supervisor name. Literals still win.

    A required question that asks the candidate to provide references is the
    exception: fill from profile.references. Optional stays blank.
    """
    q = (payload.get("question") or "").strip()
    if not _OTHER_PERSON_RE.search(q):
        return None
    if _explicit_literal(q, _required_flag(payload)) is not None:
        return None
    if _required_flag(payload) and _asks_to_provide_references(q):
        return _references_answer(q)
    return _blank_review()


def _tier0_result(payload):
    """Field guards. None when the question is not a guard hit."""
    q = (payload.get("question") or "").strip()
    inapplicable = _inapplicable_result(payload)
    if inapplicable is not None:
        return inapplicable
    other = _other_person_result(payload)
    if other is not None:
        return other
    if _is_conditional_followup(q) or _SKIP_LEAVEBLANK.search(q):
        return {"ok": False, "kind": "pause", "method": "leave-blank",
                "chars": 0, "gaps": [], "text": ""}
    if _COI_NO.search(q):
        return {"ok": True, "kind": "field", "method": "field",
                "chars": 2, "gaps": [], "text": "No"}
    prior = _former_employer_answer(q, payload.get("company"))
    if prior:
        return {"ok": True, "kind": "field", "method": "field",
                "chars": len(prior), "gaps": [], "text": prior}
    return None


def _field_match(q, required=True):
    import apply as _apply
    prof = _load_profile_doc()
    fields = _apply.load("fields.yaml") or {}
    val, how = _apply.match_field(q, prof, fields, required=required)
    key = _match_field_key(q, fields) if how == "field" else None
    return prof, val, how, key


def _fit_value_to_options(val, opts):
    """Exact option, then case/punct-normalized, then yes/no-shaped."""
    s = str(val if val is not None else "").strip()
    if not s or not opts:
        return None
    for o in opts:
        if o == s:
            return o
    nk = _norm_key(s)
    if nk:
        for o in opts:
            if _norm_key(o) == nk:
                return o
    yn = _yes_no_from_text(s)
    if yn:
        return _pick_yes_no_option(yn, opts)
    return _fit_degree_word(s, opts)


def _parse_years_token(val):
    m = re.search(r"(\d+)", str(val or ""))
    return int(m.group(1)) if m else None


def _parse_bucket(label):
    """(lo, hi) inclusive, hi None = open top, lo None = open bottom. None if not a bucket."""
    s = (label or "").lower().replace("years", " ").replace("year", " ")
    m = re.search(r"(\d+)\s*(?:-|–|—|to)\s*(\d+)", s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi
    m = re.search(r"(\d+)\s*\+", s)
    if m:
        return int(m.group(1)), None
    m = re.search(r"(?:<|less than|under|fewer than)\s*(\d+)", s)
    if m:
        return None, max(0, int(m.group(1)) - 1)
    return None


def _bucket_contains(bounds, years):
    lo, hi = bounds
    if lo is not None and years < lo:
        return False
    if hi is not None and years > hi:
        return False
    return True


def _tightest_bucket(hits):
    best, best_key = None, None
    for opt, bounds in hits:
        lo, hi = bounds
        if hi is None:
            width = 1000
        elif lo is None:
            width = hi + 1
        else:
            width = hi - lo
        key = (width, -(lo if lo is not None else -1))
        if best_key is None or key < best_key:
            best_key, best = key, opt
    return best


def _years_number_for_question(q, prof):
    """Locked candidate years for a general (or AWS) years question.

    Named technologies other than AWS have no locked year count. Do not
    substitute years_it and do not mine stories.
    """
    ql = q or ""
    if not re.search(r"how many years|years of .{0,60}experience", ql, re.I):
        return None
    cand = (prof or {}).get("candidate") or {}
    if re.search(r"\baws\b", ql, re.I) and not _NAMED_YEARS_RE.search(ql):
        return _parse_years_token(cand.get("years_aws"))
    if _NAMED_YEARS_RE.search(ql):
        return None
    return _parse_years_token(cand.get("years_it"))


def _years_bucket_option(q, opts, prof):
    parsed = []
    for o in opts:
        b = _parse_bucket(o)
        if b is not None:
            parsed.append((o, b))
    if not parsed:
        return None
    n = _years_number_for_question(q, prof)
    if n is None:
        return None
    hits = [(o, b) for o, b in parsed if _bucket_contains(b, n)]
    return _tightest_bucket(hits)


def _options_are_yesno(opts):
    if len(opts) < 2:
        return False
    for o in opts:
        no = _norm_option(o)
        if no in ("yes", "y", "no", "n", "true", "false"):
            continue
        if re.fullmatch(r"(yes|no)(,\s*\w+)?", no) and len(no) <= 24:
            continue
        return False
    return True


def _at_least_years_option(q, opts, prof):
    """Yes/No for 'at least N years' of general experience, from years_it."""
    if not _options_are_yesno(opts):
        return None
    m = _AT_LEAST_YEARS_RE.search(q or "")
    if not m:
        return None
    if _NAMED_YEARS_RE.search(q or ""):
        return None
    need = int(m.group(1) or m.group(2))
    have = _parse_years_token(((prof or {}).get("candidate") or {}).get("years_it"))
    if have is None:
        return None
    return _pick_yes_no_option("Yes" if have >= need else "No", opts)


def _choice_out(out, tier, review, kind):
    out = dict(out)
    out["tier"] = tier
    out["review"] = bool(review)
    out.setdefault("gaps", [])
    out["chars"] = len(out.get("text") or "")
    if kind:
        out["kind"] = kind
    return out


def _named_terms(q):
    """Lexicon and essay extra-system terms that appear in the question."""
    found = []
    try:
        import apply as _apply
        import essay as _essay
        terms = list(getattr(_apply, "TECH_LEXICON", []) or [])
        terms += list(getattr(_essay, "_EXTRA_SYSTEMS", []) or [])
        for term in terms:
            if _apply._tech_re(term).search(q or ""):
                found.append(term)
    except Exception as e:
        print("[named-tech skip] " + str(e), flush=True)
    return found


def _is_years_question(q):
    return bool(re.search(
        r"how many years|years of .{0,80}(?:experience|supporting)|years of experience",
        q or "", re.I))


def _lowest_safe_bucket(opts):
    """Lowest year bucket, or '' if that bucket still claims a year of experience.

    None when the options are not year buckets. 'Less than 2 years' and '0-1'
    are safe. '2-4 years' as the floor is not.
    """
    parsed = []
    for o in opts or []:
        bounds = _parse_bucket(o)
        if bounds is not None:
            parsed.append((o, bounds))
    if not parsed:
        return None

    def sort_key(item):
        lo, hi = item[1]
        return (lo if lo is not None else -1, hi if hi is not None else 10 ** 6)

    opt, (lo, _hi) = sorted(parsed, key=sort_key)[0]
    if lo is not None and lo >= 1:
        return ""
    return opt


def _honesty_choice(payload, prof):
    """Named-tech honesty. Runs before the model and before a generic years literal.

    Tech the candidate does not have: No on yes/no, lowest safe year bucket
    (or blank when the floor still claims experience). Tech they have with no
    locked year count: blank, review, never the top bucket. None => keep going.
    """
    q = (payload.get("question") or "").strip()
    opts = _options_clean(payload.get("options"))
    if not q or not opts:
        return None
    try:
        import essay as _essay
        gaps = _essay.detect_gaps(q) or {}
    except Exception as e:
        print("[gap check skip] " + str(e), flush=True)
        gaps = {}
    hard = [str(g) for g in (gaps.get("hard") or []) if g]
    years = _is_years_question(q)
    if hard:
        if years:
            low = _lowest_safe_bucket(opts)
            if not low:
                return _blank_review()
            return {"ok": True, "method": "field", "text": low, "gaps": hard,
                    "review": False, "_tier": "gap"}
        if _options_are_yesno(opts):
            no = _pick_yes_no_option("No", opts)
            if not no:
                return _blank_review()
            return {"ok": True, "method": "field", "text": no, "gaps": hard,
                    "review": False, "_tier": "gap"}
        return _blank_review()
    named = _named_terms(q) or bool(_NAMED_YEARS_RE.search(q or ""))
    if years and named and _years_number_for_question(q, prof) is None:
        # Has the tech (or it is named) but no locked count. Do not ask the model.
        return _blank_review()
    return None


def _fit_degree_word(val, opts):
    """Map a degree word onto the option that names it. None if it is not a degree."""
    s = str(val or "").lower().replace("'", "").replace("\u2019", "")
    if not s.strip():
        return None
    token = None
    if re.search(r"\bassociates?\b", s):
        token = "associate"
    else:
        for word in ("bachelor", "master", "doctorate", "doctoral", "phd",
                     "high school", "ged", "some college", "some schooling"):
            if word in s:
                token = word
                break
    if not token:
        return None
    for o in opts or []:
        ol = (o or "").lower().replace("'", "").replace("\u2019", "")
        if token == "associate" and "associate" in ol:
            return o
        if token != "associate" and token in ol:
            return o
    return None


def _education_degree_text(prof):
    """Locked degree string and the key it came from. Profile first, resume fallback."""
    degree = str(((prof or {}).get("education") or {}).get("degree") or "").strip()
    if degree:
        return degree, "education.degree"
    try:
        import apply as _apply
        resume = _apply.load("resume.yaml") or {}
        rd = str(((resume.get("education") or {}).get("degree") or "")).strip()
        if rd:
            return rd, "resume.education.degree"
    except Exception:
        pass
    return "", None


def _degree_choice(q, opts, prof):
    """Highest-degree questions use profile education.degree. None => model may run."""
    if not _DEGREE_Q_RE.search(q or ""):
        return None
    degree, key = _education_degree_text(prof)
    if not degree:
        return None
    fitted = _fit_degree_word(degree, opts)
    if not fitted:
        return None
    return fitted, key


def _choice_deterministic(payload):
    """Tier-0, named-tech honesty, degree, fields.yaml, then years. None => model."""
    q = (payload.get("question") or "").strip()
    opts = _options_clean(payload.get("options"))
    kind = str(payload.get("kind") or "").strip().lower()
    guard = _tier0_result(payload)
    if guard is not None:
        if not (guard.get("text") or "").strip():
            return _choice_out(guard, tier="0", review=bool(guard.get("review")), kind=kind)
        fitted = _fit_value_to_options(guard.get("text"), opts)
        if not fitted:
            return _choice_out(
                {"ok": False, "method": "choice-unknown", "text": "", "gaps": []},
                tier="0", review=True, kind=kind)
        return _choice_out(
            {"ok": True, "method": guard.get("method") or "field", "text": fitted, "gaps": []},
            tier="0", review=bool(guard.get("review")), kind=kind)
    prof = {}
    try:
        prof, val, how, key = _field_match(q, _required_flag(payload))
    except Exception as e:
        print("[tier1 skip] " + str(e), flush=True)
        val, how, key = None, "no-field", None
    honest = _honesty_choice(payload, prof)
    if honest is not None:
        tier = honest.pop("_tier", "gap")
        review = True if not (honest.get("text") or "").strip() else bool(honest.get("review"))
        return _choice_out(honest, tier=tier, review=review, kind=kind)
    deg = _degree_choice(q, opts, prof)
    if deg:
        text, dkey = deg
        return _choice_out(
            {"ok": True, "method": "field", "text": text, "gaps": [], "field": dkey},
            tier="1", review=False, kind=kind)
    if how and str(how).startswith("pause"):
        return _choice_out(
            {"ok": False, "kind": "pause", "method": "optional-skip", "text": "", "gaps": []},
            tier="1", review=False, kind=kind)
    if how == "field" and val not in (None, ""):
        fitted = _fit_value_to_options(val, opts)
        if fitted:
            out = {"ok": True, "method": "field", "text": fitted, "gaps": [], "field": key}
            return _choice_out(out, tier="1", review=False, kind=kind)
    bucket = _years_bucket_option(q, opts, prof)
    if bucket:
        return _choice_out(
            {"ok": True, "method": "field", "text": bucket, "gaps": []},
            tier="years", review=False, kind=kind)
    yn = _at_least_years_option(q, opts, prof)
    if yn:
        return _choice_out(
            {"ok": True, "method": "field", "text": yn, "gaps": []},
            tier="years", review=False, kind=kind)
    return None


def _skill_list(prof):
    out = []
    for k in ("cloud", "iac", "containers", "cicd", "monitoring_security",
              "systems", "aws_services", "languages"):
        for item in (prof or {}).get(k) or []:
            s = str(item).strip()
            if s and s not in out:
                out.append(s)
    return out


def _candidate_facts():
    """Compact fact sheet: headline profile, job titles, skills. No story text."""
    try:
        prof = _load_profile_doc()
    except Exception:
        prof = {}
    ident = prof.get("identity") or {}
    cand = prof.get("candidate") or {}
    elig = prof.get("eligibility") or {}
    lines = [
        "name: %s" % (ident.get("legal_name") or ""),
        "title: %s" % (ident.get("current_title") or ""),
        "employer: %s" % (ident.get("current_company") or ""),
        "location: %s" % (ident.get("location_full") or ""),
        "work_auth: %s" % (ident.get("work_auth") or ""),
        "sponsorship_required: %s" % ident.get("sponsorship_required"),
        "will_relocate: %s" % ident.get("will_relocate"),
        "years_it: %s" % (cand.get("years_it") or ""),
        "years_aws: %s" % (cand.get("years_aws") or ""),
        "clearance: %s" % ((prof.get("clearance") or {}).get("level") or ""),
        "travel: %s" % (elig.get("willing_to_travel") or ""),
        "skills: %s" % ", ".join(_skill_list(prof)),
        "do_not_claim: %s" % ", ".join(str(x) for x in (prof.get("limited_or_none") or [])),
        "jobs:",
    ]
    try:
        jobs = _work_history_jobs()
    except Exception:
        jobs = []
    for j in jobs:
        end = (j.get("end") or "").strip() or "present"
        lines.append("- %s | %s | %s-%s" % (
            j.get("title") or "", j.get("company") or "", j.get("start") or "", end))
    return "\n".join(lines)[:1800]


def _work_history_jobs():
    wh = _load_yaml_dict(_work_history_path()) if os.path.exists(_work_history_path()) else {}
    jobs = wh.get("jobs") if isinstance(wh, dict) else (wh or [])
    return list(jobs or [])


def _is_multi_choice(payload):
    q = payload.get("question") or ""
    kind = str(payload.get("kind") or "").lower()
    if _SELECT_ALL_RE.search(q):
        return True
    return kind == "checkbox" and bool(re.search(r"select all", q, re.I))


def _choice_prompt(q, opts, multi):
    lines = [
        "Select all that apply. Reply with the option numbers only, separated by spaces."
        if multi else "Reply with one option number only.",
        "Question: " + q,
        "Options:",
    ]
    for i, o in enumerate(opts, 1):
        lines.append("%d. %s" % (i, o))
    lines.append(
        "Never claim a skill that is not in the fact sheet. "
        "If unsure, pick No or the lowest option.")
    lines.append("Candidate facts:")
    lines.append(_candidate_facts())
    return "\n".join(lines)


def _parse_choice_reply(raw, opts, multi):
    text = (raw or "").strip().strip("`\"' ")
    if not text:
        return None
    text = text.splitlines()[0].strip()
    n = len(opts)
    if multi:
        if not re.fullmatch(r"[\d\s,|]+", text):
            return None
        nums = []
        for tok in re.findall(r"\d+", text):
            i = int(tok)
            if 1 <= i <= n and i not in nums:
                nums.append(i)
        if not nums:
            return None
        return " | ".join(opts[i - 1] for i in nums)
    text = re.sub(r"[\.\)\s]+$", "", text)
    if not re.fullmatch(r"\d+", text):
        return None
    i = int(text)
    if not 1 <= i <= n:
        return None
    return opts[i - 1]


def _model_complete(prompt, timeout=8, system=""):
    """Local chat completion. timeout is seconds (choice/short path uses 8)."""
    import apply as _apply
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": _apply.MODEL, "messages": msgs, "temperature": 0, "stream": False}
    req = urllib.request.Request(
        _apply.OLLAMA_BASE, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def _choice_llm(payload):
    q = (payload.get("question") or "").strip()
    opts = _options_clean(payload.get("options"))
    kind = str(payload.get("kind") or "").strip().lower()
    multi = _is_multi_choice(payload)
    raw = None
    try:
        raw = _model_complete(_choice_prompt(q, opts, multi), timeout=8, system=_CHOICE_SYSTEM)
    except Exception as e:
        print("[choice-llm] %s" % e, flush=True)
    text = _parse_choice_reply(raw, opts, multi) if raw else None
    if not text:
        return _choice_out(
            {"ok": False, "method": "choice-unknown", "text": "", "gaps": []},
            tier="choice-llm", review=True, kind=kind)
    return _choice_out(
        {"ok": True, "method": "choice-llm", "text": text, "gaps": []},
        tier="choice-llm", review=True, kind=kind)


def _answer_choice(payload):
    """Choice path. Never calls classify_genre, answer_essay, or story retrieval."""
    t0 = time.time()
    det = _choice_deterministic(payload)
    out = det if det is not None else _choice_llm(payload)
    out = dict(out)
    out["ms"] = int((time.time() - t0) * 1000)
    kind = str((payload or {}).get("kind") or "").strip().lower()
    if kind:
        out["kind"] = kind
    return out


def _short_cap(limit):
    try:
        n = int(limit) if limit else 0
    except Exception:
        n = 0
    if n <= 0:
        return 120
    return min(n, 120)


def _trim_short(raw, cap):
    text = (raw or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    line = re.sub(r"\s+", " ", line).strip().strip("\"'`")
    if not line or line.upper() == "UNKNOWN":
        return ""
    if len(line) <= cap:
        return line
    cut = line[:cap]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip()


def _short_deterministic(payload):
    kind = "text"
    guard = _tier0_result(payload)
    if guard is not None:
        g = dict(guard)
        g["tier"] = "0"
        g["review"] = bool(guard.get("review"))
        g["kind"] = kind
        return g
    q = (payload.get("question") or "").strip()
    try:
        _prof, val, how, key = _field_match(q, _required_flag(payload))
    except Exception as e:
        print("[tier1 skip] " + str(e), flush=True)
        return None
    if how and str(how).startswith("pause"):
        return {"ok": False, "kind": kind, "method": "optional-skip", "tier": "1",
                "review": False, "chars": 0, "gaps": [], "text": ""}
    if how == "field" and val not in (None, ""):
        text = str(val)
        out = {"ok": True, "kind": kind, "method": "field", "tier": "1",
               "review": False, "chars": len(text), "gaps": [], "text": text}
        if key:
            out["field"] = key
        return out
    # Optional single-line with no literal: do not invent a short-llm answer.
    if not _required_flag(payload):
        return {"ok": False, "kind": kind, "method": "leave-blank", "tier": "0",
                "review": True, "chars": 0, "gaps": [], "text": ""}
    return None


def _short_llm(payload):
    q = (payload.get("question") or "").strip()
    cap = _short_cap(payload.get("limit"))
    prompt = (
        "Answer the job-application field in ONE line, no story, at most %d characters. "
        "Use only the candidate facts. If the facts do not answer it, reply UNKNOWN.\n\n"
        "Question: %s\n\nFacts:\n%s" % (cap, q, _candidate_facts())
    )
    raw = None
    try:
        raw = _model_complete(
            prompt, timeout=8,
            system="You write a single short factual line. No stories.")
    except Exception as e:
        print("[short-llm] %s" % e, flush=True)
    text = _trim_short(raw, cap)
    if not text:
        return {"ok": False, "kind": "text", "method": "short-unknown", "tier": "short-llm",
                "review": True, "chars": 0, "gaps": [], "text": ""}
    return {"ok": True, "kind": "text", "method": "short-llm", "tier": "short-llm",
            "review": True, "chars": len(text), "gaps": [], "text": text}


def _answer_short(payload):
    """Single-line text. Literals/profile, else a short model line. Never an essay."""
    t0 = time.time()
    det = _short_deterministic(payload)
    out = det if det is not None else _short_llm(payload)
    out = dict(out)
    out["ms"] = int((time.time() - t0) * 1000)
    out["kind"] = "text"
    return out


def do_answer(payload):
    """Tiered. Imported lazily so a broken import can't stop the server.
      0. field guards               -> skip conditional/optional; deterministic COI = No;
                                   prior-employment Yes/No from work_history.yaml
      1. structured/identity/salary  -> match_field (fields.yaml -> profile.yaml)
      1.2 imported (Regenerate only) -> fresh:true review suggestion; never default fill.
          Skipped when inventory, or when owned_project/behavioral already has a story
          at/above the retrieval floor (one domain/tool token).
      1.5 genre router               -> essay.py (incl. definition / technical_experience / inventory)
      2. intent template             -> answer(question=...)  short-field bank only
      2.5 learned                    -> your vetted past answer (version-stamped)
      3. open-ended essay            -> compose()             (grounded LLM + gap guard)
    Pass fresh:true to skip the learned tier and force a new compose.
    Pass _engine_only:true (internal) to skip imported too (learn-diff draft).
    Pass options:[str] for the choice path (never genre/essay/story). Unknowns
    are a constrained number pick (choice-llm, review), not a story.
    Pass kind=text for a single-line answer (never an essay).
    Pass kind=textarea for today's essay ladder, marked review.
    """
    payload = payload or {}
    kind = str(payload.get("kind") or "").strip().lower()
    if _options_clean(payload.get("options")):
        return _answer_choice(payload)
    if kind == "text":
        return _answer_short(payload)
    if kind == "textarea":
        out = _do_answer_unconstrained(payload)
        out = dict(out) if isinstance(out, dict) else {"ok": False, "text": str(out)}
        if out.get("method") != "references":
            out["review"] = True
        out["kind"] = "textarea"
        out["tier"] = _tier_name(out.get("method"))
        return out
    return _do_answer_unconstrained(payload)


def _do_answer_unconstrained(payload):
    import apply as _apply
    q = (payload.get("question") or "").strip()
    if not q:
        return {"ok": False, "text": "[ERROR] missing question"}
    limit = payload.get("limit")
    try:
        limit = int(limit) if limit else None
    except Exception:
        limit = None
    fresh = bool(payload.get("fresh"))
    engine_only = bool(payload.get("_engine_only"))

    # tier 0: field guards (beat every other tier)
    guard = _tier0_result(payload)
    if guard is not None:
        return guard

    # tier 1: deterministic structured field (salary, country, links, yes/no...)
    try:
        prof = _load_profile_doc(); fields = _apply.load("fields.yaml")
        val, how = _apply.match_field(q, prof, fields, required=True)
        if how == "field" and val not in (None, ""):
            return _shape({"kind": "field", "method": "field", "chars": len(str(val)),
                           "gaps": [], "text": str(val),
                           "field": _match_field_key(q, fields)})
        if how and str(how).startswith("pause"):
            # fields.yaml explicitly marks this optional/leave-blank -> do NOT compose
            return _shape({"kind": "pause", "method": "optional-skip",
                           "chars": 0, "gaps": [], "text": ""})
    except Exception as e:
        print("[tier1 skip] " + str(e), flush=True)

    # Classify once, before the imported gate, so tier 1.5 reuses _genre/_c and does
    # not call the model a second time.
    _essay = None
    _c = None
    _genre = None
    try:
        import essay as _essay
        _c = _essay.parse_constraints(q, limit)
        _genre = _essay.classify_genre(q, _c)
    except Exception as e:
        print("[genre skip] " + str(e), flush=True)
        _essay = None
        _c = None
        _genre = None

    # tier 1.2: imported reference — Regenerate-only (fresh:true). Never on default
    # non-fresh fill (was a pre-engine override of live drafts). Skipped for learn-diff.
    # Also skipped when the genre engine is authoritative: inventory enumerate, or an
    # owned_project/behavioral story already at/above the retrieval floor.
    _skip_imported = _genre == "inventory"
    if (not _skip_imported and _essay is not None and _c is not None
            and _genre in ("owned_project", "behavioral")):
        try:
            _want_owned = bool(_c.get("owned")) or _genre == "owned_project"
            _story, _sc = _essay.retrieve_story(q, _want_owned)
            _skip_imported = _story_at_floor(_essay, q, _story)
        except Exception as e:
            print("[imported floor skip] " + str(e), flush=True)
    if fresh and not engine_only and not _skip_imported:
        try:
            rec = imported_lookup(q)
            if rec:
                v = (rec.get("variants") or [""])[0]
                if v:
                    return {"ok": True, "kind": "answer", "method": "imported",
                            "genre": "imported", "chars": len(v), "words": len(v.split()),
                            "gaps": [], "review": True,
                            "checks": ["imported - review before use"],
                            "variants": rec.get("variants", []),
                            "category": rec.get("category", ""), "text": v}
        except Exception as e:
            print("[imported skip] " + str(e), flush=True)

    # tier 1.5: genre router — project / hypothetical / behavioral / gap / why_* AND
    # definition / technical_experience / inventory go to essay.py BEFORE apply.py's keyword bank.
    try:
        if _essay is None:
            import essay as _essay
            _c = _essay.parse_constraints(q, limit)
            _genre = _essay.classify_genre(q, _c)
        _gaps = _essay.detect_gaps(q)
        _is_gap = bool(_gaps["hard"] or _gaps["limited"])
        if _genre in ("owned_project", "hypothetical", "behavioral", "short_text",
                      "why_company", "why_role", "definition", "technical_experience",
                      "inventory") or _is_gap:
            if not fresh:
                _la = learned_lookup(q)
                if _la:
                    return _shape({"kind": "answer", "method": "learned", "chars": len(_la),
                                   "gaps": [], "text": _la})
            # Full fill ATTEMPTS every essay inline (drafts it), so the user can read each answer
            # in the field and confirm it makes sense before submitting. The engine validates and
            # marks each one "review"; nothing auto-submits. Set bulk_skip_essays:true in the
            # payload to restore the old fast behavior (defer essays to click-to-draft).
            if payload.get("bulk") and payload.get("bulk_skip_essays") and not _is_gap:
                return {"ok": False, "kind": "pause", "method": "essay-skip",
                        "genre": _genre, "chars": 0, "gaps": [], "text": ""}
            out = _essay.answer_essay(q, limit=limit, company=payload.get("company"),
                                      url=payload.get("url"), page_context=payload.get("page_context"),
                                      role=payload.get("role"),
                                      why_hybrid=(bool(payload.get("fresh")) and not payload.get("bulk")))
            out.setdefault("ok", out.get("kind") in ("answer", "field"))
            return out
    except Exception as e:
        print("[essay route skip] " + str(e), flush=True)

    # tier 2: intent template (short-field bank; definition/experience already routed above)
    try:
        r = _apply.answer(question=q, max_chars=limit, cli_company=(payload.get("company") or None),
                          url=payload.get("url"), page_context=payload.get("page_context"))
        if r.get("kind") == "answer":
            return _shape(r)
    except Exception as e:
        print("[tier2 skip] " + str(e), flush=True)

    # tier 2.5: learned answer (free-text you already vetted). Skipped on fresh.
    if not fresh:
        try:
            la = learned_lookup(q)
            if la:
                return _shape({"kind": "answer", "method": "learned",
                               "chars": len(la), "gaps": [], "text": la})
        except Exception as e:
            print("[learned skip] " + str(e), flush=True)

    # tier 3: genre-aware essay engine (fallback for anything not caught above)
    try:
        import essay as _essay
        out = _essay.answer_essay(q, limit=limit, company=payload.get("company"),
                                  url=payload.get("url"), page_context=payload.get("page_context"),
                                  role=payload.get("role"),
                                  why_hybrid=(bool(payload.get("fresh")) and not payload.get("bulk")))
        out.setdefault("ok", out.get("kind") in ("answer", "field"))
        return out
    except Exception as e:
        print("[essay tier3 skip] " + str(e), flush=True)
        return _shape(_apply.compose(q, max_chars=limit))

def _log_answer(q, out, ms):
    try:
        _ANSWER_LOG.insert(0, {"q": (q or "")[:120], "method": (out or {}).get("method"),
                               "chars": (out or {}).get("chars"), "gaps": (out or {}).get("gaps"),
                               "ms": ms, "t": int(time.time())})
        del _ANSWER_LOG[60:]
    except Exception:
        pass


def _batch_one(item, shared, default_limit):
    limit = default_limit
    kind = None
    required = None
    has_required = False
    item_options = None
    if isinstance(item, str):
        q = item
    elif isinstance(item, dict):
        q = item.get("question") or item.get("q") or ""
        if "limit" in item:
            limit = item.get("limit")
        if "options" in item:
            item_options = item.get("options")
        if item.get("kind"):
            kind = item.get("kind")
        if "required" in item:
            has_required = True
            required = bool(item.get("required"))
    else:
        q = ""
    q = (q or "").strip()[:600]
    one = dict(shared)
    one["question"] = q
    one["limit"] = limit
    if item_options is not None:
        one["options"] = item_options
    if kind:
        one["kind"] = kind
    if has_required:
        one["required"] = required
    return one


def _route_item(one):
    if _options_clean(one.get("options")):
        return "choice"
    kind = str(one.get("kind") or "").strip().lower()
    if kind == "text":
        return "short"
    if kind == "textarea":
        return "essay"
    return "legacy"


def _contract(out, one, ms, tier=None, force_review=None):
    if not isinstance(out, dict):
        out = {"ok": False, "text": str(out)}
    else:
        out = dict(out)
    out["q"] = one.get("question") or out.get("q") or ""
    out["ms"] = int(ms)
    if "tier" not in out or not out.get("tier"):
        out["tier"] = tier or _tier_name(out.get("method"))
    if force_review is not None:
        out["review"] = bool(force_review)
    else:
        out["review"] = bool(out.get("review"))
    if one.get("kind"):
        out["kind"] = one.get("kind")
    out.setdefault("method", out.get("method") or "")
    out.setdefault("text", "" if out.get("text") is None else out.get("text"))
    if "ok" not in out:
        out["ok"] = False
    return out


def _log_batch_item(q, rec, ms):
    print("       -> %s / tier=%s / %s chars / gaps=%s / %dms" % (
        rec.get("method"), rec.get("tier"), rec.get("chars"), rec.get("gaps"), ms), flush=True)
    _log_answer(q, rec, ms)


def _exec_model(mode, one):
    t0 = time.time()
    try:
        if mode == "choice":
            out = _choice_llm(one)
        elif mode == "short":
            out = _short_llm(one)
        else:
            out = do_answer(one)
        return out, int((time.time() - t0) * 1000), None
    except Exception as e:
        return ({"ok": False, "text": "", "method": "error", "__error": str(e)},
                int((time.time() - t0) * 1000), e)


def do_answer_batch(payload):
    """Resolve many questions in one request. answers[i] corresponds to questions[i].

    Shared company/role/url/page_context/fresh/bulk/bulk_skip_essays apply to every
    item. Per-item kind, limit, options, and required are taken from the item.
    Deterministic choice/short/essay hits run first. Model leftovers run in parallel
    with a cap of 3. A throw becomes {q, ok:False, __error} for that slot only.
    Items with no kind and no options stay on the legacy sequential do_answer path.
    """
    payload = payload or {}
    raw_qs = payload.get("questions")
    if raw_qs is None:
        raw_qs = payload.get("items")
    if not isinstance(raw_qs, list):
        return {"ok": False, "error": "questions must be a list", "answers": []}
    if len(raw_qs) > 200:
        raw_qs = raw_qs[:200]

    bulk = True if payload.get("bulk") is None else bool(payload.get("bulk"))
    skip_essays = True if payload.get("bulk_skip_essays") is None else bool(payload.get("bulk_skip_essays"))
    shared = {
        "company": payload.get("company"),
        "role": payload.get("role"),
        "url": payload.get("url"),
        "page_context": payload.get("page_context"),
        "fresh": bool(payload.get("fresh")),
        "bulk": bulk,
        "bulk_skip_essays": skip_essays,
    }
    if isinstance(payload.get("options"), list):
        shared["options"] = payload.get("options")

    answers = [None] * len(raw_qs)
    model_jobs = []
    t_all = time.time()
    for i, item in enumerate(raw_qs):
        one = _batch_one(item, shared, payload.get("limit"))
        q = one.get("question") or ""
        if not q:
            answers[i] = {"q": q, "ok": False, "__error": "missing question",
                          "method": "", "tier": "0", "ms": 0, "text": "",
                          "kind": one.get("kind") or "", "review": False}
            continue
        route = _route_item(one)
        print("[answer] " + q.replace("\n", " ")[:120], flush=True)
        if route == "legacy":
            _t0 = time.time()
            try:
                out = do_answer(one)
                rec = _contract(out, one, int((time.time() - _t0) * 1000))
            except Exception as e:
                _ms = int((time.time() - _t0) * 1000)
                print("[error] batch item %s: %s" % (q[:80], e), flush=True)
                rec = _contract({"q": q, "ok": False, "__error": str(e), "method": "error",
                                 "text": ""}, one, _ms, tier="error")
            answers[i] = rec
            _log_batch_item(q, rec, rec.get("ms") or 0)
            continue
        _t0 = time.time()
        det = None
        try:
            if route == "choice":
                det = _choice_deterministic(one)
            elif route == "short":
                det = _short_deterministic(one)
            else:
                # Textarea: guards and literals stay here; the essay ladder is a model item.
                guard = _tier0_result(one)
                if guard is not None:
                    det = guard
                else:
                    try:
                        _prof, val, how, key = _field_match(q, _required_flag(one))
                    except Exception as e:
                        print("[tier1 skip] " + str(e), flush=True)
                        val, how, key = None, "no-field", None
                    if how and str(how).startswith("pause"):
                        det = {"ok": False, "kind": "pause", "method": "optional-skip",
                               "chars": 0, "gaps": [], "text": "", "tier": "1", "review": True}
                    elif how == "field" and val not in (None, ""):
                        det = {"ok": True, "kind": "field", "method": "field", "tier": "1",
                               "review": True, "chars": len(str(val)), "gaps": [],
                               "text": str(val), "field": key}
        except Exception as e:
            _ms = int((time.time() - _t0) * 1000)
            print("[error] batch item %s: %s" % (q[:80], e), flush=True)
            answers[i] = _contract(
                {"ok": False, "__error": str(e), "method": "error", "text": ""},
                one, _ms, tier="error")
            _log_batch_item(q, answers[i], _ms)
            continue
        if det is not None:
            force = True if route == "essay" else None
            # A full reference list is not an essay. A short list keeps review.
            if isinstance(det, dict) and det.get("method") == "references":
                force = None
            rec = _contract(det, one, int((time.time() - _t0) * 1000), force_review=force)
            answers[i] = rec
            _log_batch_item(q, rec, rec.get("ms") or 0)
        else:
            model_jobs.append((i, route, one))

    if model_jobs:
        with ThreadPoolExecutor(max_workers=_MODEL_CAP) as pool:
            futs = {
                pool.submit(_exec_model, mode, one): (i, mode, one)
                for i, mode, one in model_jobs
            }
            for fut in as_completed(futs):
                i, mode, one = futs[fut]
                q = one.get("question") or ""
                out, ms, err = fut.result()
                if err is not None:
                    print("[error] batch item %s: %s" % (q[:80], err), flush=True)
                force = True if mode in ("essay", "choice", "short") else None
                # Deterministic review flag on a choice-llm hit is already set.
                # Short and choice model results carry review themselves; force
                # it so a timeout/unknown is still review. Essays are always review.
                if mode == "choice" and isinstance(out, dict) and out.get("method") == "choice-llm":
                    force = True
                rec = _contract(out, one, ms, force_review=force)
                answers[i] = rec
                _log_batch_item(q, rec, ms)

    total_ms = int((time.time() - t_all) * 1000)
    print("[answer-batch] n=%d / %dms" % (len(answers), total_ms), flush=True)
    return {"ok": True, "answers": answers, "n": len(answers), "ms": total_ms}

# ---- refresh pipeline (dashboard "Refresh jobs" button) --------------------
# ---- /ranked list filters (structured-first; post score-sort; AND) ----------
_REMOTE_OK_RE = re.compile(
    r"\bremote\b|work[\s-]?from[\s-]?home|\bwfh\b|distributed\s+team", re.I)
_ONSITE_RE = re.compile(
    r"\bonsite\b|\bon-site\b|\bin-office\b|\bhybrid\b|office[\s-]?based|"
    r"must\s+relocate|relocation\s+required", re.I)
_US_POS_RE = re.compile(
    r"\bunited\s+states(?:\s+of\s+america)?\b|\busa\b|\bu\.?\s*s\.?\s*a\.?\b|"
    r"\bu\.s\.\b|\bremote\s*[-–—]?\s*us\b|\bremote\s*[-–—]?\s*americas\b|"
    r"\bamericas\b|\bus\s+only\b|\bus-based\b|\bbased\s+in\s+the\s+us\b", re.I)
# Explicit non-US geo. Canada/UK/etc alone DROP; "USA, Canada" still KEEP via _US_POS_RE.
_NON_US_RE = re.compile(
    r"\bindia\b|\bbangalore\b|\bbengaluru\b|\bemea\b|\beurope\b|"
    r"\bunited\s+kingdom\b|\buk\b|\blondon\b|\bcanada\b|\btoronto\b|"
    r"\bapac\b|\basia[\s-]?pacific\b|\baustralia\b|\bsydney\b|\bmelbourne\b|"
    r"\bgermany\b|\bberlin\b|\bfrance\b|\bparis\b|\bnetherlands\b|\bamsterdam\b|"
    r"\bireland\b|\bdublin\b|\bisrael\b|\btel\s+aviv\b|\bsingapore\b|"
    r"\bphilippines\b|\bmexico\b|\bbrazil\b|\blatam\b|\blatin\s+america\b|"
    r"\bhong\s+kong\b|\bargentina\b|\buruguay\b|\bchile\b|\bcolombia\b|"
    r"\bbuenos\s+aires\b|\bs[aã]o\s+paulo\b|\bmontevideo\b|\bsantiago\b|\bbogot[aá]\b|"
    r"\bpakistan\b|\bwarsaw\b|\bpoland\b", re.I)
# HQ / timezone / client-list mentions are not the job's location.
# "based in <place>" through end of line is the About/HQ form of
# "company based in" / "headquartered in" (e.g. "Based in San Francisco").
# It is not a US signal. Timezone words sit just before "time zones".
_US_BOILER_RE = re.compile(
    r"(?:headquartered|(?:company\s+)?based)\s+in\b[^\n]*"
    r"|clients\s+in\b[^.\n]{0,60}"
    r"|[^.\n]{0,50}\btime\s+zones?\b",
    re.I)
_LOC_LINE_RE = re.compile(
    r"\b(?:location|work\s+location)\s*:|\bbased\s+in\b|\bwork\s+location\b",
    re.I)
_US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il",
    "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt",
    "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri",
    "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
}
_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
}
# Uppercase abbrev only. Lowercase ", or" / ", in" in prose is not Oregon/Indiana.
_US_ABBR_ALT = "|".join(sorted((ab.upper() for ab in _US_STATES), key=len, reverse=True))
_US_ABBREV_ADDR_RE = re.compile(
    r"\b[A-Z][A-Za-z.'’-]{1,}(?:\s+[A-Z][A-Za-z.'’-]{1,}){0,3},\s*(?:%s)\b" % _US_ABBR_ALT)
_US_ABBREV_TAIL_RE = re.compile(
    r"\b(?:%s)\b(?:\s+\d{5}(?:-\d{4})?\b|\s*,?\s*(?:US|U\.S\.A?\.?)\b)" % _US_ABBR_ALT)
_US_ABBREV_END_RE = re.compile(
    r"(?:^|\n)\s*(?:%s)\b\s*[,.]?\s*(?:\n|$)|(?<![A-Za-z])(?:%s)\b\s*[,.]?\s*$"
    % (_US_ABBR_ALT, _US_ABBR_ALT))
_SAL_NAN_RE = re.compile(r"\$?\s*nan\b", re.I)
_SAL_NUM_RE = re.compile(
    r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(k)?", re.I)
_SAL_HR_RE = re.compile(r"/\s*hr|per\s*hour|\bhourly\b|\bhour\b", re.I)

def _qs_flag(qs, name):
    """Optional 0/1 filter flag. Absent, empty, or 0 => False (no filter)."""
    raw = (qs.get(name) or [""])[0].strip().lower()
    return raw in ("1", "true", "yes", "on")

def _qs_int(qs, name):
    """Optional int query param; absent/empty/invalid => None."""
    raw = (qs.get(name) or [""])[0].strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None

def _text_has_us(text):
    if not text:
        return False
    if _US_POS_RE.search(text):
        return True
    # "Remote, US" — comma blocks the remote-US pattern; bare lowercase "us" must not match.
    if re.search(r"\bUS\b", text):
        return True
    low = text.lower()
    for name in _US_STATE_NAMES:
        if re.search(r"\b%s\b" % re.escape(name), low):
            return True
    # Abbrev only on the original text: "Austin, TX", "TX 78701", "TX US", or a TX line.
    # ", or" / ", in" / ", me" in prose are lowercase and do not count.
    if _US_ABBREV_ADDR_RE.search(text) or _US_ABBREV_TAIL_RE.search(text) or _US_ABBREV_END_RE.search(text):
        return True
    return False

def _location_window(desc):
    """First ~400 chars plus Location / Based in / Work location lines.

    Empty structured location must not scan the whole posting: company-HQ
    boilerplate later in the description is not where the job sits.
    """
    text = desc or ""
    if not text.strip():
        return ""
    if len(text) <= 400:
        head = text
    else:
        nl = text.find("\n", 400)
        head = text[:nl] if 0 <= nl <= 520 else text[:400]
    labels = [ln for ln in text.splitlines() if _LOC_LINE_RE.search(ln)]
    if not labels:
        return head
    return head + "\n" + "\n".join(labels)

def _without_us_boilerplate(text):
    """Drop HQ, timezone, and client-geo phrases so they are not a US signal."""
    if not text:
        return ""
    return _US_BOILER_RE.sub(" ", text)

def _text_has_non_us(text):
    return bool(text and _NON_US_RE.search(text))

def _passes_remote_only(r):
    """KEEP by default; DROP only when clearly not remote.

    Unknown remote/workplace (common in discovery) stay; only explicit
    onsite/hybrid structured values or onsite-only text signals drop.
    """
    remote = r.get("remote")
    workplace = r.get("workplace")
    if remote is False:
        return False
    wp = workplace.strip() if isinstance(workplace, str) else workplace
    if wp not in (None, ""):
        if str(wp).strip().lower() != "remote":
            return False
    # Structured empty: text fallback. DROP only onsite/hybrid with no remote signal.
    # Include role — many rows carry '(remote)' only in the title.
    structured_empty = remote is None and wp in (None, "")
    if structured_empty:
        blob = "%s\n%s\n%s" % (
            r.get("role") or "", r.get("location") or "", r.get("desc") or "")
        has_remote = bool(_REMOTE_OK_RE.search(blob))
        has_onsite = bool(_ONSITE_RE.search(blob))
        if has_onsite and not has_remote:
            return False
    return True

def _passes_us_only(r):
    """KEEP US / US-inclusive; KEEP unknowns; DROP explicit non-US-only.

    A filled location field is the decision (title is not consulted).
    When location is empty, the role title is prepended to the posting's
    location lines (header plus labeled location lines). HQ/timezone/client
    US mentions, including a bare About "Based in <US place>" line, do not
    count. A non-US title with no US signal drops; a US title keeps.
    """
    loc = (r.get("location") or "").strip()
    if loc:
        if _text_has_us(loc):
            return True
        if _text_has_non_us(loc):
            return False
        return True  # unknown geo wording — keep
    title = (r.get("role") or "").strip()
    window = _location_window(r.get("desc") or "")
    if title:
        window = (title + "\n" + window) if window else title
    if not window.strip():
        return True  # no location info (blank title and blank desc)
    if _text_has_us(_without_us_boilerplate(window)):
        return True
    if _text_has_non_us(window):
        return False
    return True  # unknown geo wording — keep

def _salary_top_annual(s):
    """Parse salary string -> top-of-range annual USD, or None if unknown/junk."""
    if s is None:
        return None
    text = str(s).strip()
    if not text or _SAL_NAN_RE.search(text):
        return None
    nums = []
    for m in _SAL_NUM_RE.finditer(text):
        raw, k = m.group(1), m.group(2)
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if k:
            v *= 1000.0
        nums.append(v)
    if not nums:
        return None
    top = max(nums)
    if _SAL_HR_RE.search(text):
        top *= 2080.0
    return int(top)

def _passes_salary_min(r, minimum):
    """KEEP when top-of-range annual >= minimum; KEEP missing/nan salary."""
    top = _salary_top_annual(r.get("salary"))
    if top is None:
        return True
    return top >= minimum

def _filter_ranked(rows, qs):
    """Apply optional remote_only / us_only / salary_min after score-sort (AND)."""
    want_remote = _qs_flag(qs, "remote_only")
    want_us = _qs_flag(qs, "us_only")
    sal_min = _qs_int(qs, "salary_min")
    if not want_remote and not want_us and sal_min is None:
        return rows
    out = []
    for r in rows:
        if want_remote and not _passes_remote_only(r):
            continue
        if want_us and not _passes_us_only(r):
            continue
        if sal_min is not None and not _passes_salary_min(r, sal_min):
            continue
        out.append(r)
    return out

_PIPE = {"running": False, "log": [], "started": 0, "finished": 0}
_ANSWER_LOG = []   # local-model reasoning: recent /answer resolutions
_FILL_LOG = []     # form-fill reports posted by the extension
_PIPE_SCRIPTS = ["scan_ats.py", "discover.py", "hiringcafe.py", "builtin.py", "remotive.py", "remoteok.py", "resolve.py", "rank.py", "prune.py"]

def _run_pipeline():
    import jobs_store
    srcs = jobs_store.config_sources({"ats": True, "discover": True, "hiringcafe": True,
                                      "builtin": True, "remotive": True, "remoteok": True})
    src_of = {"scan_ats.py": "ats", "discover.py": "discover", "hiringcafe.py": "hiringcafe",
              "builtin.py": "builtin", "remotive.py": "remotive", "remoteok.py": "remoteok"}
    _PIPE.update(running=True, log=["starting refresh..."], started=time.time(), finished=0)
    for sc in _PIPE_SCRIPTS:
        key = src_of.get(sc)
        if key and not srcs.get(key, True):
            _PIPE["log"].append("skip " + sc + " (disabled)"); continue
        _PIPE["log"].append("running " + sc + " ...")
        try:
            pr = subprocess.run([sys.executable, sc], cwd=HERE, capture_output=True, text=True, timeout=900)
            tail = [ln for ln in (pr.stdout or "").splitlines() if ln.strip()][-2:]
            _PIPE["log"].append("  " + (" | ".join(tail) if tail else "(done)"))
        except Exception as e:
            _PIPE["log"].append("  ERROR: " + str(e)[:120])
    _PIPE.update(running=False, finished=time.time())
    _PIPE["log"].append("refresh complete.")

def _start_pipeline():
    if _PIPE.get("running"):
        return False
    threading.Thread(target=_run_pipeline, daemon=True).start()
    return True


# ---- profile read/write (dashboard Profile tab) ---------------------------
# PyYAML safe_dump. A successful PUT rewrites the yaml and drops comments;
# the timestamped .bak beside the file is the undo. No profile values in logs.
_PROFILE_LOCK = threading.Lock()
_PROFILE_SECTIONS = (
    "identity", "education", "clearance", "eeo", "compensation", "candidate",
    "eligibility", "application_defaults", "screening", "skills",
    "limited_or_none", "limited_experience", "references", "work_history",
)
_DICT_SECTIONS = (
    "identity", "education", "clearance", "eeo", "compensation", "candidate",
    "eligibility", "application_defaults",
)
_SKILL_KEYS = (
    "cloud", "iac", "containers", "cicd", "monitoring_security",
    "systems", "aws_services", "compliance_owned", "languages",
)
_SCREENING_KEYS = (
    "english_level", "cloud_most", "ai_agents", "infra_owned",
    "infra_defined", "k8s_ops", "spanish_portuguese", "latin_america",
)
_REF_KEYS = ("name", "relationship", "company", "title", "email", "phone", "years_known")


# Tests assign these to temp copies. None means the repo file. .bak is written
# beside whichever path is live, so a test crash cannot touch the user's profile.
PROFILE_PATH = None
WORK_HISTORY_PATH = None
ANSWER_CACHE_PATH = None


def _profile_path():
    return PROFILE_PATH or os.path.join(HERE, "profile.yaml")


def _work_history_path():
    return WORK_HISTORY_PATH or os.path.join(HERE, "data", "work_history.yaml")


def _answer_cache_path():
    if ANSWER_CACHE_PATH:
        return ANSWER_CACHE_PATH
    import apply as _apply
    return _apply.CACHE_PATH


def _load_yaml_dict(path):
    import yaml
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    return doc if isinstance(doc, dict) else {}


def _atomic_yaml(path, data):
    """Timestamped .bak of the current file, then atomic replace. Comments are not kept."""
    import datetime
    import shutil
    import yaml
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        shutil.copy2(path, path + ".bak-" + stamp)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=100000)
    os.replace(tmp, path)


def _clear_answer_cache():
    path = _answer_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({}, f)
    os.replace(tmp, path)


def _scalar_ok(v):
    return isinstance(v, (str, bool, int, float)) or v is None


def _clean_dict_section(section, body):
    if not isinstance(body, dict):
        return None, section + " must be an object"
    out = {}
    for k, v in body.items():
        if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_]+", k):
            return None, section + " has an invalid key"
        if isinstance(v, list):
            if not all(_scalar_ok(x) for x in v):
                return None, section + " has a non-scalar list"
            out[k] = list(v)
        elif _scalar_ok(v):
            out[k] = v
        else:
            return None, section + " has a nested value"
    if not out:
        return None, section + " is empty"
    return out, None


def _clean_screening(body):
    if not isinstance(body, dict):
        return None, "screening must be an object"
    out = {}
    for k, v in body.items():
        if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_]+", k):
            return None, "screening has an invalid key"
        if not isinstance(v, str):
            return None, "screening values must be strings"
        out[k] = v
    for k in _SCREENING_KEYS:
        if k not in out:
            return None, "screening missing " + k
    return out, None


def _clean_skills(body):
    if not isinstance(body, dict):
        return None, "skills must be an object"
    if any(k not in _SKILL_KEYS for k in body):
        return None, "unknown skills bucket"
    out = {}
    for k in _SKILL_KEYS:
        if k not in body:
            return None, "skills missing " + k
        v = body[k]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            return None, "skills." + k + " must be a list of strings"
        out[k] = list(v)
    return out, None


def _clean_str_list(section, body):
    if not isinstance(body, list) or not all(isinstance(x, str) for x in body):
        return None, section + " must be a list of strings"
    return list(body), None


def _clean_str_dict(section, body):
    if not isinstance(body, dict):
        return None, section + " must be an object"
    out = {}
    for k, v in body.items():
        if not isinstance(k, str) or not k.strip():
            return None, section + " has an invalid key"
        if not isinstance(v, str):
            return None, section + " values must be strings"
        out[k] = v
    if not out:
        return None, section + " is empty"
    return out, None


def _clean_ref(item):
    if not isinstance(item, dict):
        return None
    out = {}
    for k in _REF_KEYS:
        v = item.get(k, "")
        if v is None:
            v = ""
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            return None
        out[k] = v
    return out


def _clean_references(body):
    if not isinstance(body, list):
        return None, "references must be a list"
    out = []
    for item in body:
        cleaned = _clean_ref(item)
        if cleaned is None:
            return None, "references item must be an object of scalars"
        out.append(cleaned)
    return out, None


def _public_references(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        cleaned = _clean_ref(item) if isinstance(item, dict) else None
        out.append(cleaned if cleaned is not None else {k: "" for k in _REF_KEYS})
    return out


def _clean_job(item):
    if not isinstance(item, dict):
        return None
    out = {}
    for k, v in item.items():
        if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_]+", k):
            return None
        if not _scalar_ok(v):
            return None
        out[k] = v
    return out


def _clean_work_history(body):
    if not isinstance(body, list):
        return None, "work_history must be a list"
    out = []
    for item in body:
        cleaned = _clean_job(item)
        if cleaned is None:
            return None, "work_history item must be an object of scalars"
        out.append(cleaned)
    return out, None


def _read_jobs():
    path = _work_history_path()
    try:
        doc = _load_yaml_dict(path)
    except Exception:
        return []
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, list):
        return []
    return [dict(j) for j in jobs if isinstance(j, dict)]


def _validate_section(section, body):
    if section in _DICT_SECTIONS:
        return _clean_dict_section(section, body)
    if section == "screening":
        return _clean_screening(body)
    if section == "skills":
        return _clean_skills(body)
    if section == "limited_or_none":
        return _clean_str_list(section, body)
    if section == "limited_experience":
        return _clean_str_dict(section, body)
    if section == "references":
        return _clean_references(body)
    if section == "work_history":
        return _clean_work_history(body)
    return None, "unknown section"


SERVE_BUILD = "serve-2026-09-25b profile-splice"


def _splice_top_blocks(text, updates):
    """Replace only the given top-level keys' blocks in YAML text; everything else (comments,
    ordering, other sections) is kept byte-for-byte. A block runs from its 'key:' line to the
    line before the next top-level key, minus the comment/blank lines that lead into that next
    key (those belong to it). Missing keys are appended. Returns the new text."""
    import yaml
    lines = text.splitlines(keepends=True)
    top = re.compile(r"^([A-Za-z_][\w-]*)\s*:")
    for key, value in updates.items():
        dumped = yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True, width=100000)
        start = next((i for i, ln in enumerate(lines) if (m := top.match(ln)) and m.group(1) == key), None)
        if start is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines += ["\n"] + dumped.splitlines(keepends=True)
            continue
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if top.match(lines[j]):
                end = j
                break
        k = end
        while k > start + 1 and (lines[k - 1].strip() == "" or lines[k - 1].lstrip().startswith("#")) and not lines[k - 1].startswith(" "):
            k -= 1
        lines[start:k] = dumped.splitlines(keepends=True)
    return "".join(lines)


def _write_yaml_keys(path, updates):
    """Write only changed top-level keys; no-op (no .bak, no write) when nothing changed."""
    import datetime
    import shutil
    import yaml
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    cur = (yaml.safe_load(text) or {}) if text else {}
    changed = {k: v for k, v in updates.items() if cur.get(k) != v}
    if not changed:
        return False
    new_text = _splice_top_blocks(text, changed)
    check = yaml.safe_load(new_text) or {}
    for k, v in updates.items():                       # verify: the splice produced exactly the data
        if check.get(k) != v:
            raise ValueError("splice verify failed for %s" % k)
    for k in cur:
        if k not in updates and check.get(k) != cur.get(k):
            raise ValueError("splice disturbed %s" % k)
    if os.path.exists(path):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        shutil.copy2(path, path + ".bak-" + stamp)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    os.replace(tmp, path)
    return True


def _write_profile_section(section, cleaned):
    if section == "skills":
        return _write_yaml_keys(_profile_path(), {k: cleaned[k] for k in _SKILL_KEYS})
    return _write_yaml_keys(_profile_path(), {section: cleaned})


def _write_work_history(jobs):
    return _write_yaml_keys(_work_history_path(), {"jobs": jobs})


def profile_get():
    """Structured profile for the dashboard. Raw yaml stays on GET /profile."""
    prof = _load_yaml_dict(_profile_path())

    def dic(key):
        v = prof.get(key)
        return dict(v) if isinstance(v, dict) else {}

    skills = {}
    for k in _SKILL_KEYS:
        raw = prof.get(k) or []
        skills[k] = list(raw) if isinstance(raw, list) else []
    lim = prof.get("limited_or_none") or []
    lex = prof.get("limited_experience")
    out = {"ok": True}
    for key in _DICT_SECTIONS:
        out[key] = dic(key)
    out["screening"] = dic("screening")
    out["skills"] = skills
    out["limited_or_none"] = list(lim) if isinstance(lim, list) else []
    out["limited_experience"] = dict(lex) if isinstance(lex, dict) else {}
    out["references"] = _public_references(prof.get("references"))
    out["work_history"] = _read_jobs()
    return out


def profile_put(section, body):
    """Validate and write one section. Returns (http_status, json_object)."""
    if section not in _PROFILE_SECTIONS:
        return 400, {"ok": False, "error": "unknown section"}
    cleaned, err = _validate_section(section, body)
    if err:
        return 400, {"ok": False, "error": err}
    try:
        with _PROFILE_LOCK:
            if section == "work_history":
                wrote = _write_work_history(cleaned)
            else:
                wrote = _write_profile_section(section, cleaned)
            if wrote:
                _clear_answer_cache()
    except Exception:
        print("[profile] save failed", flush=True)
        return 500, {"ok": False, "error": "save failed"}
    print("[profile] saved %s" % section, flush=True)
    return 200, {"ok": True, "section": section, "data": cleaned}


_STAR_TEXT_KEYS = ("title", "situation", "task", "action", "result", "hero")
_STAR_LIST_KEYS = ("domains", "tools", "not_implied")


def _star_blank():
    draft = {k: "" for k in _STAR_TEXT_KEYS}
    for k in _STAR_LIST_KEYS:
        draft[k] = []
    draft["owned"] = False
    return draft


def _parse_star_draft(raw):
    draft = _star_blank()
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return draft
    try:
        data = json.loads(m.group(0))
    except Exception:
        return draft
    if not isinstance(data, dict):
        return draft
    for k in _STAR_TEXT_KEYS:
        v = data.get(k, "")
        if isinstance(v, str):
            draft[k] = v.strip()
        elif v:
            draft[k] = str(v).strip()
    for k in _STAR_LIST_KEYS:
        v = data.get(k, [])
        if isinstance(v, str):
            v = re.split(r"[,;\n]+", v)
        if not isinstance(v, list):
            v = []
        draft[k] = [str(x).strip() for x in v if str(x).strip()]
    draft["owned"] = bool(data.get("owned"))
    return draft


def _term_rx(term):
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(term) + r"s?(?![A-Za-z0-9])", re.I)


def _strip_term_text(s, term):
    rx = _term_rx(term)
    if not s or not rx.search(s):
        return s, False
    out = rx.sub("", s)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r"(?:,\s*){2,}", ", ", out)
    return out.strip(" ,;"), True


def _limited_or_none_terms():
    """Honesty list, matched directly. detect_gaps misses these because the
    profile dump that grounds the candidate already contains the words."""
    out, seen = [], set()
    for t in _load_profile_doc().get("limited_or_none") or []:
        s = str(t).strip()
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _strip_draft_claims(draft):
    """Strip limited_or_none claims, then hard gaps from essay.detect_gaps."""
    blob_parts = [draft.get(k) or "" for k in _STAR_TEXT_KEYS]
    for k in _STAR_LIST_KEYS:
        blob_parts.append(" ".join(draft.get(k) or []))
    extra = []
    try:
        import essay as _essay
        found = _essay.detect_gaps(" ".join(blob_parts))
        extra = list((found or {}).get("hard") or [])
    except Exception:
        extra = []
    terms = _limited_or_none_terms()
    have = {t.lower() for t in terms}
    for t in extra:
        s = str(t).strip()
        if s and s.lower() not in have:
            terms.append(s)
            have.add(s.lower())
    gaps = []
    for term in terms:
        hit = False
        for k in _STAR_TEXT_KEYS:
            nxt, found_term = _strip_term_text(draft.get(k) or "", term)
            if found_term:
                draft[k] = nxt
                hit = True
        for k in _STAR_LIST_KEYS:
            kept = []
            for item in draft.get(k) or []:
                nxt, found_term = _strip_term_text(item, term)
                if found_term:
                    hit = True
                if nxt:
                    kept.append(nxt)
            draft[k] = kept
        if hit:
            gaps.append(term)
    return draft, gaps


def _imports_load():
    import import_qa
    import_qa.DEST = IMPORTED_PATH
    return import_qa.load_store()


def _imports_save(store):
    import import_qa
    import_qa.DEST = IMPORTED_PATH
    import_qa.save_store(store)


def _mark_import_used(key, story_id):
    if not key or not story_id:
        return False
    store = _imports_load()
    rec = (store.get("answers") or {}).get(key)
    if not isinstance(rec, dict):
        return False
    rec["used_in_story"] = story_id
    _imports_save(store)
    return True


def promote_story(key):
    """One local-model STAR draft. Does not write stories.yaml or the import."""
    key = (key or "").strip()
    if not key:
        return 400, {"ok": False, "error": "key required"}
    store = _imports_load()
    rec = (store.get("answers") or {}).get(key)
    if not isinstance(rec, dict):
        return 404, {"ok": False, "error": "unknown import"}
    variants = rec.get("variants") or []
    answer = variants[0] if variants else ""
    question = rec.get("question") or ""
    system = (
        "Turn one imported job-application answer into a STAR story. "
        "Reply with JSON only, keys title, domains, tools, not_implied, "
        "situation, task, action, result, hero. domains, tools, and not_implied "
        "are arrays of short strings. Use only facts written in the answer. "
        "Do not add employers, tools, or outcomes that are not in the answer."
    )
    prompt = "Question:\n%s\n\nAnswer:\n%s" % (str(question)[:500], str(answer)[:4000])
    try:
        raw = _model_complete(prompt, timeout=60, system=system)
    except Exception:
        print("[promote-story] model failed", flush=True)
        return 502, {"ok": False, "error": "model failed"}
    draft = _parse_star_draft(raw)
    draft, gaps = _strip_draft_claims(draft)
    print("[promote-story] drafted", flush=True)
    return 200, {"ok": True, "draft": draft, "gaps": gaps}


class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code); self._cors()
        self.send_header("Content-Type", ctype); self.end_headers()
        if isinstance(body, str): body = body.encode()
        self.wfile.write(body)
    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        m = re.fullmatch(r"/api/profile/([A-Za-z0-9_]+)", parsed.path.rstrip("/"))
        if not m:
            return self._send(404, b"not found")
        try:
            body = json.loads(raw or b"null")
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})
        code, obj = profile_put(m.group(1), body)
        return self._json(code, obj)
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/version":
            return self._json(200, {"ok": True, "build": SERVE_BUILD, "pid": os.getpid()})
        if parsed.path in ("/", "/dashboard"):
            try:
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, str(e))
        if parsed.path == "/demo":
            try:
                with open(os.path.join(HERE, "demo.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, str(e))
        if parsed.path == "/qa":
            try:
                with open(os.path.join(HERE, "qa.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, str(e))
        if parsed.path == "/qa/questions":
            try:
                p = os.path.join(HERE, "data", "qa_questions.json")
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        return self._json(200, json.load(f))
                return self._json(200, {"questions": []})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if parsed.path == "/resume":
            # Serve the resume bytes so the extension can attach the file on an application form
            # (a content script cannot read the local disk; the background worker fetches this).
            try:
                import base64 as _b64
                p = _resume_path()
                if not p:
                    return self._json(404, {"ok": False, "error": "no resume found under documents/resumes"})
                with open(p, "rb") as f:
                    raw = f.read()
                mime = "application/pdf" if p.lower().endswith(".pdf") else \
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if p.lower().endswith(".docx") else \
                    "application/octet-stream"
                return self._json(200, {"ok": True, "filename": os.path.basename(p),
                                        "mime": mime, "b64": _b64.b64encode(raw).decode()})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/stories":
            try:
                with open(os.path.join(HERE, "stories.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, str(e))
        if parsed.path == "/api/queue-lite":
            try:
                import jobs_store
                out = []
                for r in jobs_store.load()["queue"]:
                    out.append({"id": r.get("id"), "company": r.get("company", ""),
                                "role": r.get("role", ""), "url": r.get("apply_url") or r.get("url", ""),
                                "has_jd": len(r.get("desc") or "") > 200})
                out.sort(key=lambda x: (not x["has_jd"], x["company"].lower()))
                return self._json(200, {"count": len(out), "jobs": out})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if parsed.path == "/api/imports":
            try:
                st = json.load(open(IMPORTED_PATH, encoding="utf-8"))
                ans = st.get("answers", {}) or {}
                items = [{"key": k, "question": v.get("question", ""),
                          "category": v.get("category", ""), "variants": v.get("variants", []),
                          "source": v.get("source", ""),
                          "used_in_story": v.get("used_in_story") or ""}
                         for k, v in ans.items()]
                items.sort(key=lambda r: (r["category"], r["question"].lower()))
                return self._json(200, {"count": len(items), "items": items})
            except Exception:
                return self._json(200, {"count": 0, "items": []})
        if parsed.path == "/api/imports/stats":
            try:
                st = json.load(open(IMPORTED_PATH, encoding="utf-8"))
                return self._json(200, {"total": len(st.get("answers", {}) or {}),
                                        "last_import": st.get("last_import")})
            except Exception:
                return self._json(200, {"total": 0, "last_import": None})
        if parsed.path == "/api/stories":
            try:
                import stories_store
                return self._json(200, {"stories": stories_store.load()})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if parsed.path == "/ranked":
            try:
                import jobs_store
                qs = urllib.parse.parse_qs(parsed.query)
                st = (qs.get("status") or ["discovered"])[0].strip() or "discovered"
                rows = _filter_ranked(jobs_store.ranked(status=st), qs)
                out = [{"company": r.get("company", ""), "role": r.get("role", ""),
                        "score": r.get("score") or 0, "ats": r.get("ats", ""),
                        "status": r.get("status"), "source": r.get("source", ""),
                        "posted": r.get("posted", ""),
                        "discovered_at": r.get("discovered_at"),
                        "location": r.get("location") or r.get("workplace", ""),
                        "workplace": r.get("workplace", ""),
                        "salary": r.get("salary", ""), "desc": r.get("desc", ""),
                        "url": r.get("url", ""),
                        "apply": r.get("apply_url") or r.get("url", ""),
                        "closed_reason": r.get("closed_reason") or ""} for r in rows]
                return self._json(200, {"count": len(out), "jobs": out})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if parsed.path == "/discover/status":
            return self._json(200, _PIPE)
        if parsed.path == "/logs":
            return self._json(200, {"pipeline": _PIPE.get("log", []), "answers": _ANSWER_LOG, "fills": _FILL_LOG})
        if parsed.path == "/config":
            try:
                import jobs_store
                return self._json(200, jobs_store.load_config())
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if parsed.path == "/api/ollama":
            return self._json(200, ollama_status())
        if parsed.path == "/api/profile":
            try:
                return self._json(200, profile_get())
            except Exception:
                print("[profile] read failed", flush=True)
                return self._json(500, {"ok": False, "error": "profile read failed"})
        if parsed.path == "/profile":
            def _read(fn):
                try:
                    with open(os.path.join(HERE, fn), encoding="utf-8") as f:
                        return f.read()
                except Exception:
                    return ""
            return self._json(200, {"profile": _read("profile.yaml"),
                                    "work_history": _read(os.path.join("data", "work_history.yaml"))})
        if parsed.path == "/stats":
            try:
                import jobs_store
                return self._json(200, jobs_store.stats())
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/jobs":
            try:
                import jobs_store
                q = jobs_store.load()["queue"]
                st = (urllib.parse.parse_qs(parsed.query).get("status") or [""])[0]
                if st:
                    q = [r for r in q if r.get("status") == st]
                return self._json(200, {"count": len(q), "jobs": q})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        return self._send(404, b"not found")
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        if parsed.path == "/discover":
            started = _start_pipeline()
            return self._json(200, {"started": started, "running": _PIPE.get("running", False)})
        if parsed.path == "/fill-log":
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {}
            payload["t"] = int(time.time())
            _FILL_LOG.insert(0, payload); del _FILL_LOG[30:]
            return self._json(200, {"ok": True})
        if parsed.path == "/config":
            try:
                new = json.loads(raw or b"{}")
                import jobs_store
                cfg = jobs_store.load_config()
                cfg.update(new if isinstance(new, dict) else {})   # merge: keep keys other cards own
                jobs_store.save_config(cfg)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path in ("/api/ollama", "/api/ollama/test"):
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                return self._json(400, {"ok": False, "error": "bad json"})
            if parsed.path == "/api/ollama/test":
                return self._json(200, ollama_test(payload.get("model")))
            code, obj = ollama_set(payload)
            return self._json(code, obj)
        if parsed.path == "/api/imports/promote-story":
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                return self._json(400, {"ok": False, "error": "bad json"})
            code, obj = promote_story(payload.get("key"))
            return self._json(code, obj)
        if parsed.path == "/api/imports/update":
            try:
                payload = json.loads(raw or b"{}")
                import import_qa
                store = import_qa.load_store(); ans = store.setdefault("answers", {})
                old = ans.pop((payload.get("key") or "").strip(), {})
                q = (payload.get("question") or old.get("question") or "").strip()
                if not q:
                    return self._json(400, {"ok": False, "error": "question required"})
                variants = [v.strip() for v in (payload.get("variants") or []) if v and v.strip()]
                if not variants:
                    variants = old.get("variants", [])
                nk = import_qa._norm_q(q)
                ans[nk] = {"question": q[:400], "variants": variants[:6],
                           "category": (payload.get("category") or old.get("category") or "").strip(),
                           "source": old.get("source", "edited"),
                           "imported_at": old.get("imported_at"), "status": "reference"}
                import_qa.save_store(store)
                return self._json(200, {"ok": True, "key": nk})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/imports/delete":
            try:
                payload = json.loads(raw or b"{}")
                import import_qa
                store = import_qa.load_store()
                store.get("answers", {}).pop((payload.get("key") or "").strip(), None)
                import_qa.save_store(store)
                return self._json(200, {"ok": True, "count": len(store.get("answers", {}))})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/tailor":
            try:
                payload = json.loads(raw or b"{}")
                import tailor as _t
                jd = payload.get("jd") or ""
                company = payload.get("company") or ""
                role = payload.get("role") or ""
                if not jd and payload.get("job"):
                    jd, row = _t._load_jd_from_job(payload["job"])
                    if row:
                        company = company or row.get("company", "")
                        role = role or row.get("role", "")
                if not jd:
                    return self._json(400, {"ok": False, "error": "no job description (paste one or pick a job with a JD)"})
                t = _t.tailor(jd)
                return self._json(200, {"ok": True, "coverage": t["coverage"],
                                        "html": _t.resume_html(t), "markdown": _t.to_markdown(t),
                                        "company": company, "role": role})
            except Exception as e:
                print("[error] tailor: " + str(e), flush=True)
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/cover":
            try:
                payload = json.loads(raw or b"{}")
                import tailor as _t
                jd = payload.get("jd") or ""
                company = payload.get("company") or ""
                role = payload.get("role") or ""
                if not jd and payload.get("job"):
                    jd, row = _t._load_jd_from_job(payload["job"])
                    if row:
                        company = company or row.get("company", "")
                        role = role or row.get("role", "")
                if not jd:
                    return self._json(400, {"ok": False, "error": "no job description"})
                out = _t.cover_letter(jd, company=company, role=role)
                return self._json(200, {"ok": True, **out})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/import":
            try:
                payload = json.loads(raw or b"{}")
                content = payload.get("content") or ""
                fn = re.sub(r"[^A-Za-z0-9_.-]", "_", (payload.get("filename") or "import.json"))[:80]
                if not fn.lower().endswith((".json", ".md", ".txt")):
                    fn += ".json"
                impdir = os.path.join(HERE, "imports")
                os.makedirs(impdir, exist_ok=True)
                path = os.path.join(impdir, fn)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                import import_qa
                r = import_qa.run(path, use_llm=bool(payload.get("llm")))
                print("[import] %s -> +%d ~%d skip=%d total=%d" %
                      (fn, r["added"], r["updated"], r["skipped"], r["total"]), flush=True)
                return self._json(200, {"ok": True, **r})
            except Exception as e:
                print("[error] import: " + str(e), flush=True)
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/stories":
            try:
                payload = json.loads(raw or b"{}")
                import stories_store
                st = stories_store.upsert(payload)
                import_key = (payload.get("import_key") or "").strip()
                if import_key:
                    _mark_import_used(import_key, st.get("id"))
                print("[stories] upsert %s" % st.get("id"), flush=True)
                return self._json(200, {"ok": True, "story": st})
            except Exception as e:
                print("[error] " + str(e), flush=True)
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/stories/delete":
            try:
                payload = json.loads(raw or b"{}")
                import stories_store
                n = stories_store.delete((payload.get("id") or "").strip())
                return self._json(200, {"ok": True, "count": n})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path == "/applied":
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                return self._json(400, {"ok": False, "error": "bad json"})
            url = (payload.get("url") or "").strip()
            if not url:
                return self._json(400, {"ok": False, "error": "missing url"})
            try:
                import jobs_store
                r = jobs_store.upsert_applied(url, company=payload.get("company"),
                                              role=payload.get("role"), ats=payload.get("ats"))
                print("[applied] %s | %s (%s)" % (r.get("company"), r.get("role"), r.get("ats")), flush=True)
                return self._json(200, {"ok": True, "id": r.get("id"), "applied_at": r.get("applied_at"),
                                        "company": r.get("company"), "role": r.get("role")})
            except Exception as e:
                print("[error] " + str(e), flush=True)
                return self._json(500, {"ok": False, "error": str(e)})
        if parsed.path in ("/answer", "/learn"):
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                return self._json(400, {"ok": False, "text": "[ERROR] bad json"})
            if parsed.path == "/learn":
                try:
                    out = do_learn(payload)
                    print("[learn] %s :: %s" % (out.get("ok"), out.get("skipped") or out.get("key")), flush=True)
                    return self._json(200, out)
                except Exception as e:
                    print("[error] " + str(e), flush=True)
                    return self._json(500, {"ok": False, "error": str(e)})
            q = (payload.get("question") or "")[:600]
            print("[answer] " + q.replace("\n", " ")[:120], flush=True)
            try:
                _t0 = time.time()
                out = do_answer(payload)
                _ms = int((time.time() - _t0) * 1000)
                print("       -> %s / tier=%s / %s chars / gaps=%s / %dms" % (
                    out.get("method"), out.get("tier"), out.get("chars"), out.get("gaps"), _ms), flush=True)
                _log_answer(q, out, _ms)
                return self._json(200, out)
            except Exception as e:
                print("[error] " + str(e), flush=True)
                return self._json(500, {"ok": False, "text": "[ERROR] " + str(e)})
        if parsed.path == "/answer-batch":
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                return self._json(400, {"ok": False, "error": "bad json", "answers": []})
            qs = payload.get("questions")
            if qs is None:
                qs = payload.get("items")
            n = len(qs) if isinstance(qs, list) else 0
            print("[answer-batch] %d questions" % n, flush=True)
            try:
                out = do_answer_batch(payload)
                code = 200 if out.get("ok") else 400
                return self._json(code, out)
            except Exception as e:
                print("[error] " + str(e), flush=True)
                return self._json(500, {"ok": False, "error": str(e), "answers": []})
        if parsed.path == "/qa/questions":
            try:
                payload = json.loads(raw or b"{}")
                qs = payload.get("questions")
                if not isinstance(qs, list):
                    return self._json(400, {"ok": False, "error": "questions must be a list"})
                os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
                _qp = os.path.join(HERE, "data", "qa_questions.json")
                _clean = [str(q)[:2000] for q in qs if str(q).strip()][:500]
                with open(_qp + ".tmp", "w", encoding="utf-8") as f:
                    json.dump({"questions": _clean}, f, indent=1, ensure_ascii=False)
                os.replace(_qp + ".tmp", _qp)
                return self._json(200, {"ok": True, "count": len(_clean)})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
        return self._send(404, b"not found")
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    apply_model_config()
    print("Penates server on http://127.0.0.1:%d  (dashboard at /, /answer, /answer-batch)" % PORT)
    print("Leave this window open.")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
