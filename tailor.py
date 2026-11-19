#!/usr/bin/env python3
"""Penates resume tailoring. Given a job description, SELECT and REORDER the real bullets
and achievements from resume.yaml to fit the JD, reorder skills to lead with what the JD
asks for, and report covered-vs-missing keywords. It never writes a new bullet, adds a skill
you do not have, or invents a metric. You review and edit the output.

  python tailor.py --job <id|url>     # a row from data/jobs.json
  python tailor.py --jd job.txt       # a pasted JD file
  python tailor.py --jd job.txt --md  # print the tailored resume as markdown
"""
import os, re, json, argparse
import yaml
import apply as _a
try:
    import essay as _essay
    _EXTRA = set(_essay._EXTRA_SYSTEMS)
except Exception:
    _EXTRA = set()

HERE = os.path.dirname(os.path.abspath(__file__))

# role/skill terms beyond the tech lexicon that a JD may require
ROLE_TERMS = {
    "devops", "sre", "site reliability", "reliability", "platform engineer", "cloud engineer",
    "infrastructure", "ci/cd", "cicd", "pipeline", "automation", "observability", "monitoring",
    "incident response", "on-call", "iac", "infrastructure as code", "compliance", "fedramp",
    "soc 2", "hipaa", "pci", "nist", "cis", "stig", "ato", "poa&m", "zero trust", "security",
    "hardening", "remediation", "vulnerability", "cost optimization", "migration", "high availability",
    "load balancing", "networking", "active directory", "patching", "disaster recovery", "backup",
    "rag", "llm", "genai", "government", "govcloud", "clearance", "secret",
}

def load_resume():
    return yaml.safe_load(open(os.path.join(HERE, "resume.yaml"), encoding="utf-8"))

def _profile():
    try: return _a.load("profile.yaml")
    except Exception: return {}

def _word_re(term):
    core = re.escape(term.strip().lower())
    return re.compile(r"(?<![a-z0-9])" + core + r"(?![a-z0-9])", re.I)

def _vocab(resume):
    terms = set()
    for cat, items in (resume.get("skills") or {}).items():
        for it in items:
            terms.add(it.lower())
            for part in re.split(r"[/,]", it):
                p = part.strip().lower()
                if len(p) >= 2: terms.add(p)
    prof = _profile()
    for k in ("cloud", "iac", "containers", "cicd", "monitoring_security", "systems",
              "aws_services", "compliance_owned", "languages"):
        for it in (prof.get(k) or []):
            terms.add(str(it).lower())
    terms |= set(t.lower() for t in _a.TECH_LEXICON)
    terms |= set(t.lower() for t in _EXTRA)
    terms |= set(ROLE_TERMS)
    return {t for t in terms if len(t) >= 2}

def _candidate_text(resume):
    parts = [resume.get("profile_summary", "")]
    for cat, items in (resume.get("skills") or {}).items():
        parts.append(cat)                       # count the category name (e.g. "Observability")
        parts.append(" ".join(items))
    for j in resume.get("experience", []):
        parts.append(j.get("title", ""))        # count the job title (e.g. "Development Operations")
        parts += j.get("bullets", [])
        parts += [a["text"] + " " + a["label"] for a in j.get("achievements", [])]
    try:
        parts.append(json.dumps(_profile()))
        wh = _a.load(os.path.join("data", "work_history.yaml")) or {}
        for jb in (wh.get("jobs", []) if isinstance(wh, dict) else []):
            parts.append(jb.get("description", ""))
    except Exception:
        pass
    return " ".join(parts).lower()

_SYN = {
    "ci/cd": ["cicd", "ci cd", "continuous integration"], "cicd": ["ci/cd", "ci cd"],
    "devops": ["development operations", "dev ops"],
    "iac": ["infrastructure as code"], "infrastructure as code": ["iac"],
    "load balancing": ["load balancer", "load balancers", "haproxy"],
}
def _variants(term):
    t = term.strip().lower()
    vs = {t, t.replace("/", " "), t.replace("/", ""), t.replace("-", " "),
          t.replace("-", ""), t.replace(" ", "")}
    if t.endswith("ing"):
        vs.add(t[:-3] + "er"); vs.add(t[:-3] + "ers")
    for extra in _SYN.get(t, []):
        vs.add(extra)
    return {v for v in vs if len(v) >= 2}

def _has(text, term):
    return any(_word_re(v).search(text) for v in _variants(term))

_JD_START = re.compile(
    r"(the role\b|\brole\b|responsibilities|what you.{0,3}ll do|things you get to do|"
    r"who you are|requirements|must.?haves|qualifications|about the role|your impact|"
    r"what we.{0,3}re looking for|key responsibilities)", re.I)
def _focus_jd(jd):
    """Trim the leading company/marketing blurb so investor and brand names in 'Who We Are'
    do not get scored as required skills (e.g. 'Spark Capital')."""
    m = _JD_START.search(jd or "")
    return jd[m.start():] if m else (jd or "")

def analyze(jd_text, resume=None):
    resume = resume or load_resume()
    vocab = _vocab(resume)
    jd_l = _focus_jd(jd_text).lower()
    jd_terms = sorted({t for t in vocab if _word_re(t).search(jd_l)}, key=len, reverse=True)
    cand = _candidate_text(resume)
    covered = [t for t in jd_terms if _has(cand, t)]
    missing = [t for t in jd_terms if t not in covered]
    return {"jd_terms": jd_terms, "covered": covered, "missing": missing,
            "match_set": set(covered)}

def _score(text, match_set):
    tl = (text or "").lower()
    return sum(1 for t in match_set if _word_re(t).search(tl))

# how many items to keep per job, most-recent first (keeps ~2 pages, relevant first)
_BULLET_CAP = [9, 7, 3, 3, 2]
_ACH_CAP    = [5, 3, 3, 0, 0]
_BULLET_MIN = [5, 4, 3, 2, 2]

def tailor(jd_text, resume=None):
    resume = resume or load_resume()
    a = analyze(jd_text, resume)
    ms = a["match_set"]
    exp = []
    for i, job in enumerate(resume.get("experience", [])):
        bcap = _BULLET_CAP[i] if i < len(_BULLET_CAP) else 3
        bmin = _BULLET_MIN[i] if i < len(_BULLET_MIN) else 2
        acap = _ACH_CAP[i] if i < len(_ACH_CAP) else 0
        # rank bullets by (score desc, original order) - relevant first, keep original as tiebreak
        bl = list(enumerate(job.get("bullets", [])))
        bl.sort(key=lambda t: (-_score(t[1], ms), t[0]))
        keep_n = max(bmin, min(bcap, sum(1 for _, b in bl if _score(b, ms) > 0) or bmin))
        keep_n = min(keep_n, len(bl))
        chosen = sorted(bl[:keep_n], key=lambda t: t[0])   # restore original order for the kept set
        bullets = [b for _, b in chosen]
        # achievements ranked by relevance, capped
        ac = list(job.get("achievements", []))
        ac.sort(key=lambda x: -_score(x["text"] + " " + x["label"], ms))
        achievements = ac[:acap]
        exp.append({"title": job["title"], "company": job["company"], "dates": job["dates"],
                    "location": job["location"], "bullets": bullets, "achievements": achievements})
    # reorder skills: JD-matched first within each category (only real skills, nothing added)
    skills = {}
    _fj = _focus_jd(jd_text).lower()
    for cat, items in (resume.get("skills") or {}).items():
        skills[cat] = sorted(items, key=lambda it: (0 if _word_re(it).search(_fj) else 1))
    return {"header": resume["header"], "profile_summary": resume["profile_summary"],
            "skills": skills, "experience": exp, "education": resume["education"],
            "coverage": {"covered": a["covered"], "missing": a["missing"]},
            "jd_term_count": len(a["jd_terms"])}

def to_markdown(t):
    prof = _profile().get("identity", {})
    contact = " | ".join(x for x in [prof.get("email"), prof.get("linkedin"), prof.get("github")] if x)
    out = ["# %s" % t["header"]["name"], "*%s*" % t["header"]["title"], contact, "",
           "## Profile", t["profile_summary"], "", "## Technical Skills"]
    for cat, items in t["skills"].items():
        out.append("**%s:** %s" % (cat, ", ".join(items)))
    out += ["", "## Professional Experience"]
    for j in t["experience"]:
        out += ["", "### %s | %s" % (j["title"], j["company"]), "*%s | %s*" % (j["dates"], j["location"]), ""]
        for b in j["bullets"]:
            out.append("- " + b)
        if j["achievements"]:
            out.append("")
            out.append("**Key Achievements**")
            for ac in j["achievements"]:
                out.append("- **%s:** %s" % (ac["label"], ac["text"]))
    out += ["", "## Certificates & Education"]
    for c in t["education"].get("certs", []):
        out.append("- " + c)
    out.append(t["education"].get("degree", ""))
    return "\n".join(out)

def _load_jd_from_job(ref):
    import jobs_store
    q = jobs_store.load()["queue"]
    for r in q:
        if r.get("id") == ref or r.get("url") == ref or (r.get("apply_url") == ref):
            return (r.get("desc") or ""), r
    return "", None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job"); ap.add_argument("--jd"); ap.add_argument("--md", action="store_true")
    args = ap.parse_args()
    if args.jd:
        jd = open(args.jd, encoding="utf-8").read()
    elif args.job:
        jd, _ = _load_jd_from_job(args.job)
        if not jd: raise SystemExit("no JD text on that job row (desc empty)")
    else:
        raise SystemExit("pass --jd <file> or --job <id|url>")
    t = tailor(jd)
    if args.md:
        print(to_markdown(t)); return
    print("== coverage ==")
    print("covered (%d):" % len(t["coverage"]["covered"]), ", ".join(t["coverage"]["covered"][:40]))
    print("MISSING (%d):" % len(t["coverage"]["missing"]), ", ".join(t["coverage"]["missing"][:40]))
    print("\n== kept per job ==")
    for j in t["experience"]:
        print("  %-32s bullets=%d achievements=%d" % (j["title"][:32], len(j["bullets"]), len(j["achievements"])))

if __name__ == "__main__":
    main()

# ---- HTML render matching the default resume format (centered header, ruled sections,
#      categorized skills, bullets + Key Achievements). Prints to a clean 2-page PDF. ----
def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def resume_html(t):
    prof = _profile().get("identity", {})
    contact = " &nbsp;|&nbsp; ".join(_esc(x) for x in
        [prof.get("email"), prof.get("linkedin"), prof.get("github")] if x)
    css = """
    :root{--ink:#1a1a1a;--muted:#555;--rule:#bbb}
    *{box-sizing:border-box} 
    body{font:10.5pt/1.4 'Calibri','Carlito','Segoe UI',system-ui,sans-serif;color:var(--ink);margin:0}
    .page{max-width:8.5in;margin:0 auto;padding:0.55in 0.7in}
    h1{font-size:22pt;text-align:center;margin:0;font-weight:700}
    .subtitle{text-align:center;color:var(--ink);font-size:11pt;margin:2px 0 1px}
    .contact{text-align:center;color:var(--muted);font-size:9.5pt;margin:0 0 10px}
    h2{font-size:12pt;font-weight:700;margin:13px 0 5px;border-bottom:1px solid var(--rule);padding-bottom:2px}
    .prof{margin:0 0 2px}
    .skill{margin:2px 0}
    .skill b{font-weight:700}
    .job{margin-top:9px}
    .job .jt{font-size:11.5pt;font-weight:700}
    .job .jt .co{font-weight:400;color:#333}
    .job .meta{font-style:italic;color:var(--muted);font-size:9.5pt;margin:1px 0 3px}
    ul{margin:2px 0 2px 0;padding-left:18px}
    li{margin:2px 0}
    .ka{font-weight:700;font-size:10pt;margin:6px 0 2px}
    .ach{margin:2px 0}
    .ach b{font-weight:700}
    .edu b{font-weight:700}
    @page{size:letter;margin:0}
    """
    h = ['<!doctype html><html><head><meta charset="utf-8"><style>%s</style></head><body><div class="page">' % css]
    h.append('<h1>%s</h1>' % _esc(t["header"]["name"]))
    h.append('<div class="subtitle">%s</div>' % _esc(t["header"]["title"]))
    h.append('<div class="contact">%s</div>' % contact)
    h.append('<h2>Profile</h2><div class="prof">%s</div>' % _esc(t["profile_summary"]))
    h.append('<h2>Technical Skills</h2>')
    for cat, items in t["skills"].items():
        h.append('<div class="skill"><b>%s:</b> %s</div>' % (_esc(cat), _esc(", ".join(items))))
    h.append('<h2>Professional Experience</h2>')
    for j in t["experience"]:
        h.append('<div class="job"><div class="jt">%s <span class="co">| %s</span></div>'
                 % (_esc(j["title"]), _esc(j["company"])))
        h.append('<div class="meta">%s &nbsp;|&nbsp; %s</div>' % (_esc(j["dates"]), _esc(j["location"])))
        if j["bullets"]:
            h.append('<ul>' + "".join('<li>%s</li>' % _esc(b) for b in j["bullets"]) + '</ul>')
        if j["achievements"]:
            h.append('<div class="ka">Key Achievements</div>')
            for ac in j["achievements"]:
                h.append('<div class="ach"><b>%s:</b> %s</div>' % (_esc(ac["label"]), _esc(ac["text"])))
        h.append('</div>')
    h.append('<h2>Certificates &amp; Education</h2>')
    for c in t["education"].get("certs", []):
        h.append('<div class="edu"><b>%s</b></div>' % _esc(c))
    h.append('<div class="edu">%s</div>' % _esc(t["education"].get("degree", "")))
    h.append('</div></body></html>')
    return "\n".join(h)

# ---- cover letter: why-company + ONE real story, grounded, gap-guarded ----
def cover_letter(jd_text, company="", role="", model=None):
    """Draft a short cover letter. Uses one retrieved story + your real stack; never claims a
    skill the JD wants that you do not have. Returns {text, story, checks}. Needs Ollama."""
    prof = _profile()
    stack = ""
    try:
        stack = _a._real_stack(prof)
    except Exception:
        pass
    a = analyze(jd_text)
    missing = a["missing"]
    story = None
    try:
        import essay
        st, sc = essay.retrieve_story(jd_text, True)
        if st and sc >= 1:
            story = st
    except Exception:
        pass
    if not _a.ollama_up():
        return {"text": "[PAUSE] Ollama offline; cannot draft a cover letter.", "story": None, "checks": ["ollama_offline"]}
    story_txt = ""
    if story:
        s = story.get("star", {})
        story_txt = "One thing I have done that fits: %s %s" % (s.get("action", ""), s.get("result", ""))
    sysp = ("You draft a concise, professional cover letter in first person, 3 short paragraphs, "
            "about 200-260 words. Use ONLY the facts given. Do not claim any tool or skill that is "
            "not in the candidate's stack or story. Do not invent metrics, employers, or projects. "
            "No em dashes. Open by naming the role and company and why it fits; middle paragraph uses "
            "the real story; close with fit and enthusiasm. Output only the letter.")
    usr = ("Company: %s\nRole: %s\nCandidate real stack: %s\n%s\n\nJob highlights:\n%s\n\nLetter:"
           % (company or "the company", role or "this role", stack, story_txt, _focus_jd(jd_text)[:1500]))
    try:
        txt = _a.ollama(usr, sysp, temperature=0.3, model=model or _a.ESSAY_MODEL).strip().strip('"')
        txt = _a._no_emdash(txt)
    except Exception as e:
        return {"text": "[PAUSE] cover draft failed: %s" % e, "story": None, "checks": ["error"]}
    checks = []
    for g in missing:
        if _word_re(g).search(txt):
            checks.append("mentions gap:" + g)
    return {"text": txt, "story": (story or {}).get("id"), "checks": checks}
