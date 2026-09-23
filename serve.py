#!/usr/bin/env python3
"""Local server for the apply tool. Listens on 127.0.0.1:8765.

Routes:
  /apply?url=...   -> launches run_url.py --attach <url> (the CDP full-fill path)
  /answer  (POST)  -> JSON {question, limit?, company?, url?, fresh?, options?} -> grounded
                      answer. Tiers: structured field -> intent template ->
                      LEARNED (your vetted past answers) -> compose (LLM + gap guard).
                      method=field responses include field:<yaml value path>.
                      When options:[str] is non-empty, answer is constrained to an option
                      (or kind:review leave-blank) — never free-text/essay.
  /answer-batch (POST) -> JSON {questions: [str|{q|question, limit?, options?}], company?,
                      role?, url?, page_context?, bulk?, fresh?, bulk_skip_essays?, options?}
                      -> {ok, answers: [{q, .../answer fields} | {q, ok:false, __error}]}.
                      answers[i] matches questions[i]. One item failing never fails the
                      batch. Defaults bulk=true and bulk_skip_essays=true (essays stay
                      click-to-draft). page_context is sent once and applied to every item.
  /learn   (POST)  -> JSON {question, answer, company?} -> remember YOUR final
                      answer, keyed by the normalized question, so the same
                      essay question returns your vetted words next time.
                      Company-specific "why this company" answers are NOT stored
                      (they don't transfer across postings).
  /ranked  (GET)   -> scored job list. Optional filters (AND, post-sort):
                      remote_only=0|1, us_only=0|1, salary_min=<int annual USD>.
                      Structured fields first; text fallback only when empty.
                      Missing/unknown salary or location is KEPT (not hidden).

Leave this window running in the background.
"""
import os, sys, re, json, time, subprocess, threading, urllib.parse
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

_last_proc = None

def launch(url):
    global _last_proc
    if _last_proc is not None and _last_proc.poll() is None:
        try:
            _last_proc.terminate()
            print("[kill] terminated previous run pid=%s" % _last_proc.pid, flush=True)
        except Exception:
            pass
    args = [sys.executable, os.path.join(HERE, "run_url.py"), "--attach", url]
    kw = {"cwd": HERE}
    if os.name == "nt":
        kw["creationflags"] = 0x00000010  # CREATE_NEW_CONSOLE
    _last_proc = subprocess.Popen(args, **kw)

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
    t = _norm_option(text)
    if t in ("yes", "y", "true"):
        return "Yes"
    if t in ("no", "n", "false"):
        return "No"
    if t.startswith("yes") and len(t) <= 8:
        return "Yes"
    if t.startswith("no") and len(t) <= 8:
        return "No"
    return None


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
    import apply as _apply
    wh = _apply.load(os.path.join("data", "work_history.yaml")) or {}
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
    Pass options:[str] to constrain the result to a choice label (never essay/story).
    """
    out = _do_answer_unconstrained(payload)
    opts = (payload or {}).get("options")
    if isinstance(opts, list) and any(o is not None and str(o).strip() for o in opts):
        q = ((payload or {}).get("question") or "").strip()
        return _constrain_to_options(out, opts, q)
    return out


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

    # tier 1: deterministic structured field (salary, country, links, yes/no...)
    try:
        prof = _apply.load("profile.yaml"); fields = _apply.load("fields.yaml")
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


def do_answer_batch(payload):
    """Resolve many questions in one request. answers[i] corresponds to questions[i].

    Shared company/role/url/page_context/fresh/bulk/bulk_skip_essays apply to every
    item. Per-item limit is taken from the item when present. A throw in do_answer
    becomes {q, ok:False, __error} for that slot only.
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

    answers = []
    t_all = time.time()
    for item in raw_qs:
        limit = payload.get("limit")
        item_options = None
        if isinstance(item, str):
            q = item
        elif isinstance(item, dict):
            q = item.get("question") or item.get("q") or ""
            if "limit" in item:
                limit = item.get("limit")
            if "options" in item:
                item_options = item.get("options")
        else:
            q = ""
        q = (q or "").strip()[:600]
        if not q:
            answers.append({"q": q, "ok": False, "__error": "missing question"})
            continue
        one = dict(shared)
        one["question"] = q
        one["limit"] = limit
        if item_options is not None:
            one["options"] = item_options
        print("[answer] " + q.replace("\n", " ")[:120], flush=True)
        _t0 = time.time()
        try:
            out = do_answer(one)
            rec = dict(out) if isinstance(out, dict) else {"ok": False, "text": str(out)}
            rec["q"] = q
            answers.append(rec)
            _ms = int((time.time() - _t0) * 1000)
            print("       -> %s / %s chars / gaps=%s / %dms" % (
                rec.get("method"), rec.get("chars"), rec.get("gaps"), _ms), flush=True)
            _log_answer(q, rec, _ms)
        except Exception as e:
            _ms = int((time.time() - _t0) * 1000)
            print("[error] batch item %s: %s" % (q[:80], e), flush=True)
            rec = {"q": q, "ok": False, "__error": str(e)}
            answers.append(rec)
            _log_answer(q, {"method": "error", "chars": 0, "gaps": []}, _ms)

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
    r"\bpakistan\b|\bwarsaw\b|\bpoland\b", re.I)
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
    low = text.lower()
    for name in _US_STATE_NAMES:
        if re.search(r"\b%s\b" % re.escape(name), low):
            return True
    # State abbrev only in address form: "City, TX" / "City, TX," / "City, TX US"
    # (bare "or"/"in"/"me" in prose must NOT count as Oregon/Indiana/Maine)
    for ab in _US_STATES:
        if re.search(r",\s*%s\b" % ab, low):
            return True
    return False

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
    """KEEP US / US-inclusive; KEEP unknowns; DROP explicit non-US-only."""
    loc = (r.get("location") or "").strip()
    blob = loc if loc else (r.get("desc") or "")
    if not (blob or "").strip():
        return True  # no location info
    if _text_has_us(blob):
        return True
    if _text_has_non_us(blob):
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


class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
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
                          "source": v.get("source", "")} for k, v in ans.items()]
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
        if parsed.path != "/apply":
            return self._send(404, b"not found")
        url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
        if not url:
            return self._send(400, b"missing url")
        print("[launch] " + url, flush=True)
        try:
            launch(url); self._send(200, b"launched")
        except Exception as e:
            print("[error] " + str(e), flush=True); self._send(500, str(e))
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
                cfg = json.loads(raw or b"{}")
                import jobs_store
                jobs_store.save_config(cfg)
                return self._json(200, {"ok": True})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})
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
        if parsed.path == "/apply":
            url = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
            if not url:
                return self._send(400, b"missing url")
            print("[launch] " + url, flush=True)
            try:
                launch(url); return self._send(200, b"launched")
            except Exception as e:
                print("[error] " + str(e), flush=True); return self._send(500, str(e))
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
                print("       -> %s / %s chars / gaps=%s / %dms" % (out.get("method"), out.get("chars"), out.get("gaps"), _ms), flush=True)
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
    print("Apply autofill server on http://127.0.0.1:%d  (/apply, /answer, /answer-batch, /learn)" % PORT)
    print("Leave this window open.")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
