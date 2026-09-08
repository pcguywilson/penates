#!/usr/bin/env python3
"""
run_url.py — open a job application URL in a visible browser, scrape the company,
role, and questions, let Ollama infer the company context, then fill every field
using the local answer engine. You review and click Submit yourself.

Usage (PowerShell, from C:\\src\\Claude-apply-local):
    python run_url.py "https://job-boards.greenhouse.io/acme/jobs/123"
    python run_url.py "<url>" --dry          # scrape + print answers, DON'T fill
    python run_url.py "<url>" --no-upload     # skip resume upload
    python run_url.py "<url>" --company "Acme" --role "SRE"   # override scrape

First-time setup:
    pip install playwright pyyaml
    playwright install chromium

Notes
- Headed browser with a persistent profile (.browser-profile) so Workday/Lever
  logins stick between runs. It never clicks Submit — that's you.
- Greenhouse/Lever are single-page and fill cleanly. Workday is multi-step and
  dynamic: expect to run per step and finish some fields by hand. Use --dry first.
- EEO/demographic fields and anything it can't answer confidently are left blank
  (logged), never guessed.
"""
import os, re, sys, json, argparse
import yaml
import apply  # local engine (answer(), match_field, ollama, load, dotted)

# Load a local .env (KEY=VALUE lines) so runs launched from the extension/server
# or a plain shell get RESUME_PATH / OLLAMA_* without the parent setting them.
def _load_dotenv():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if not os.path.exists(p):
            return
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass
_load_dotenv()

# Line-buffer stdout/stderr so progress shows live even when redirected to a file
# (batch runs, scheduled runs). Without this, a long-lived run that keeps the
# browser open never flushes its buffer to the log.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# Tee everything printed to a per-run logfile in ./logs so a run can be
# inspected after its console window is gone (button launches, scheduled runs).
try:
    import datetime as _dt
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_path = os.path.join(_log_dir, "apply_%s.log" % _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    _log_fh = open(_log_path, "a", encoding="utf-8", buffering=1)
    class _Tee:
        def __init__(self, *streams): self.streams = streams
        def write(self, d):
            for st in self.streams:
                try: st.write(d); st.flush()
                except Exception: pass
        def flush(self):
            for st in self.streams:
                try: st.flush()
                except Exception: pass
        def isatty(self): return False
    sys.stdout = _Tee(sys.__stdout__, _log_fh)
    sys.stderr = _Tee(sys.__stderr__, _log_fh)
    # mirror latest run to a stable path for easy tailing
    try:
        _latest = os.path.join(_log_dir, "latest.log")
        with open(_latest, "w", encoding="utf-8") as _lf:
            _lf.write(_log_path + "\n")
    except Exception:
        pass
    print("[log] " + _log_path)
except Exception:
    pass

_MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
def _name_date(fn):
    """Sortable (year, month) parsed from a filename, so 'August_2026' beats 'July_2026'
    regardless of file timestamps. (0,0) when no date is found."""
    b = os.path.basename(fn).lower()
    m = re.search(r"(20\d\d)[-_ ]?(0[1-9]|1[0-2])\b", b)          # 2026-08 / 2026_08
    if m: return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_ ]?(20\d\d)", b)
    if m: return (int(m.group(2)), _MONTHS[m.group(1)])
    m = re.search(r"\b(20\d\d)\b", b)                            # just a year
    if m: return (int(m.group(1)), 0)
    return (0, 0)

def _find_doc(kind):
    """Pick the best resume/cover PDF. Searches documents/<resumes|covers>/ first, then
    documents/, then the tool root. Ranks by DATE-IN-FILENAME, then file mtime - so you can
    keep several versions and control which is used by naming (e.g. 'Resume_2026-08.pdf').
    kind='resume' matches 'resume'/'cv' (excludes cover); kind='cover' matches 'cover'."""
    import glob
    here = os.path.dirname(os.path.abspath(__file__))
    sub = "resumes" if kind == "resume" else "covers"
    roots = [os.path.join(here, "documents", sub), os.path.join(here, "documents"), here]
    def keep(b):
        if kind == "resume":
            return ("cover" not in b) and ("resume" in b or re.search(r"\bcv\b|_cv|cv_", b))
        return "cover" in b
    for root in roots:
        cands = [f for f in glob.glob(os.path.join(root, "*.pdf")) if keep(os.path.basename(f).lower())]
        if cands:
            pick = max(cands, key=lambda f: (_name_date(f), os.path.getmtime(f)))
            print(f"[{kind}] using {os.path.relpath(pick, here)}")
            return pick
    env = os.environ.get("RESUME_PATH" if kind == "resume" else "COVER_LETTER_PATH", "")
    if env:
        print(f"[{kind}] none found in documents/; using env path: {os.path.basename(env)}")
    return env

RESUME_PATH = _find_doc("resume")
COVER_PATH = _find_doc("cover")
HOW_HEAR = "LinkedIn"   # default "how did you learn about this job?" pick; set from profile in run()
# Ordered fallbacks for tenants whose menu lacks the preferred source (e.g. Workday
# some tenants offer no "LinkedIn"): pick the first option the menu actually has.
HOW_HEAR_FALLBACKS = ["Indeed", "Glassdoor", "ZipRecruiter", "Company Website",
                      "Corporate Website", "Social Media", "Website", "Other"]

ATS_HINTS = [
    ("greenhouse", ["greenhouse.io", "gh_jid="]),   # incl. Greenhouse embedded on a custom domain
    ("lever",      ["lever.co"]),
    ("workday",    ["myworkdayjobs.com", "workday.com"]),
    ("ashby",      ["ashbyhq.com"]),
    ("workable",   ["workable.com"]),
    ("smartrecruiters", ["smartrecruiters.com"]),
    ("paycom",     ["paycomonline.net"]),
    ("zoho",       ["zohorecruit.com"]),
    ("hrmdirect",  ["hrmdirect.com"]),
    ("rippling",   ["rippling.com"]),
    ("trakstar",   ["trakstar.com"]),
    ("paylocity",  ["paylocity.com"]),
    ("jobvite",    ["jobvite.com"]),
    ("icims",      ["icims.com"]),
    ("clearcompany", ["clearcompany.com"]),
    ("bamboohr",   ["bamboohr.com"]),
    ("jobscore",   ["jobscore.com"]),
    ("adp",        ["adp.com", "workforcenow.adp.com", "myjobs.adp.com"]),
    ("taleo",      ["taleo.net", "tbe.taleo"]),
    ("phenom",     ["phenompeople.com"]),
]

def detect_ats(url):
    u = url.lower()
    for name, hints in ATS_HINTS:
        if any(h in u for h in hints):
            return name
    return "generic"

# JS that computes a human label for a form control, tried in priority order.
LABEL_JS = r"""
(el) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const junk = s => !s || /rec[_ ]?form|\d{7,}|^field\d*$/i.test(s);
  const al = el.getAttribute('aria-label');
  if (al && !junk(al)) return clean(al);
  const lb = el.getAttribute('aria-labelledby');
  if (lb) { const t = lb.split(' ').map(id => (document.getElementById(id)||{}).innerText||'').join(' '); if (clean(t) && !junk(clean(t))) return clean(t); }
  // Salesforce LWC: the slds-input is nested TWO shadow roots deep
  // (lightning-input > lightning-primitive-input-simple > input). The real label
  // ("Mailing Address", "Phone Number") is a .label property on the lightning-input
  // host, invisible to document.querySelector. Climb host-by-host until we find it.
  try {
    let node = el;
    for (let depth = 0; depth < 6 && node; depth++) {
      // (a) the SLDS field wrapper (.slds-form-element) is an ANCESTOR within some
      // shadow tree - check closest at every level, since it may sit above one or
      // more shadow boundaries from the raw <input>.
      if (node.closest) {
        const fe = node.closest('.slds-form-element');
        if (fe) {
          const fl = fe.querySelector('.slds-form-element__label, label, legend');
          if (fl && clean(fl.innerText) && !junk(clean(fl.innerText))) return clean(fl.innerText);
          // label isn't a <label> tag here - take the wrapper's own first text line
          // (the visible "Mailing Address" / "City" etc.), minus the field's value.
          const fv = (el.value||'');
          const line = (fe.innerText||'').split('\n').map(x=>clean(x))
            .filter(x=>x && x!==clean(fv) && !junk(x))[0];
          if (line && line.length>=2 && line.length<80) return line;
        }
      }
      const root = node.getRootNode();
      if (!root || !root.host) break;
      // (b) LWC host label property / a label inside this shadow root
      if (root.host.label && !junk(root.host.label)) return clean(root.host.label);
      const le = root.querySelector('label, legend, .slds-form-element__label, [class*="label" i]');
      if (le && clean(le.innerText) && !junk(clean(le.innerText))) return clean(le.innerText);
      // author-rendered label sibling (lightning-input is variant="label-hidden", so the
      // visible "Mailing Address" lives as text in the parent component's shadow root).
      const rt = clean(root.textContent || '');
      const phv = clean(el.getAttribute('placeholder') || '');
      if (rt && rt.length >= 2 && rt.length < 80 && rt !== clean(el.value) && rt !== phv && !junk(rt))
        return rt;
      node = root.host;                  // climb one shadow boundary up
    }
  } catch (e) {}
  if (el.id && !junk(el.id)) { const l = document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if (l && clean(l.innerText)) return clean(l.innerText); }
  const wrap = el.closest('label'); if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
  const container = el.closest('.application-field, .field, [class*="field"], fieldset, [data-automation-id]');
  if (container) {
    const lg = container.querySelector('label, legend, .application-label, [class*="label"]');
    if (lg && clean(lg.innerText) && !junk(clean(lg.innerText))) return clean(lg.innerText);
  }
  // proximity: nearest visible preceding label-ish text (for generated-id fields, e.g. Zoho)
  let cur = el;
  for (let up = 0; up < 4 && cur; up++) {
    let sib = cur.previousElementSibling, hops = 0;
    while (sib && hops < 5) {
      const t = clean(sib.innerText || sib.textContent || '');
      if (t && t.length > 2 && t.length < 160 && !/^select\.?\.?\.?$/i.test(t) && !junk(t)) return t;
      sib = sib.previousElementSibling; hops++;
    }
    cur = cur.parentElement;
  }
  const ph = el.getAttribute('placeholder'); if (ph && !junk(ph)) return clean(ph);
  const nm = el.getAttribute('name'); if (nm && !junk(nm)) return clean(nm.replace(/[_\-\[\]]/g, ' '));
  return '';
}
"""

def scrape_meta(page, ats, cli_company, cli_role):
    from urllib.parse import urlparse
    def txt(sel):
        try:
            el = page.query_selector(sel)
            return el.inner_text().strip() if el else ""
        except Exception:
            return ""

    title = page.title() or ""
    # Greenhouse/Lever/Ashby titles read "Job Application for <ROLE> at <COMPANY>"
    title_company = title.split(" at ")[-1].strip() if " at " in title else ""
    title_role = ""
    if " at " in title:
        title_role = re.sub(r"(?i)^job application for\s*", "", title.split(" at ")[0]).strip()
    # org slug is the first path segment on these ATSs: /<slug>/jobs/<id>
    slug = ""
    parts = [p for p in urlparse(page.url).path.split("/") if p]
    if ats in ("greenhouse", "lever", "ashby", "workable") and parts:
        slug = parts[0].replace("%20", " ")

    role = cli_role or ""
    company = cli_company or ""
    if ats == "greenhouse":
        role = role or txt(".app-title") or title_role or txt("h1")
    elif ats == "lever":
        role = role or txt(".posting-headline h2") or title_role or txt("h2")
    elif ats == "workday":
        role = role or txt('[data-automation-id="jobPostingHeader"]') or txt("h1")
    role = role or title_role or txt("h1") or title.split("-")[0].strip()

    company = company or title_company or (slug.title() if slug else "")
    if not company:
        company = title.split(" at ")[-1].strip() if " at " in title else ""

    body = ""
    try:
        body = page.inner_text("body")[:3500]
    except Exception:
        pass
    return company.strip(), role.strip(), body

def infer_context(company, role, jd_text, use_llm=True):
    """Ask Ollama for a one-line company_description + interest_style from the page."""
    app = {"company": company or "the company", "role_title": role or "this role",
           "company_description": "", "interest_style": "technology"}
    if not use_llm or not apply.ollama_up() or not jd_text:
        return app
    sys_p = ("From the job posting text, output STRICT JSON only: "
             '{"company_description": "<one sentence on what THIS company/product does>", '
             '"interest_style": "technology|mission|general"}. '
             "Describe only this company. No prose, no code fence.")
    try:
        raw = apply.ollama(f"Company: {company}\nRole: {role}\n\nPosting:\n{jd_text}", sys_p, 0.2)
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.I).strip()
        data = json.loads(raw)
        if data.get("company_description"): app["company_description"] = data["company_description"].strip()
        if data.get("interest_style") in ("technology", "mission", "general"):
            app["interest_style"] = data["interest_style"]
    except Exception as e:
        print(f"  ! context inference failed ({e}); using generic framing")
    return app

def dismiss_cookies(page):
    """Click a cookie-consent 'Accept' button if present, so the overlay can't
    intercept clicks during fill. Not application data — safe in dry runs too."""
    wants = ["accept all", "accept cookies", "allow all", "i accept", "accept",
             "agree", "i agree", "got it", "allow cookies"]
    try:
        for b in page.query_selector_all("button, a, [role=button]"):
            try:
                if not b.is_visible():
                    continue
                t = (b.inner_text() or "").strip().lower()
            except Exception:
                continue
            if t and len(t) <= 22 and any(w == t or w in t for w in wants):
                try:
                    b.click(); page.wait_for_timeout(300); return True
                except Exception:
                    pass
    except Exception:
        pass
    return False

APPLY_NAMES = [r"^apply now$", r"^apply$", r"^apply manually$",
               r"^apply for this (job|position|role|opening)", r"apply for this job online",
               r"^apply for (job|this)$", r"^apply online$", r"^apply to this job$",
               r"^start application$", r"^start your application$", r"^begin application$",
               r"^i.?m interested$", r"^complete application$"]

def click_by_role(page, patterns, force=False):
    """Click the first visible button/link whose ACCESSIBLE NAME matches a pattern,
    in priority order. Uses Playwright roles, not text search, so nav/header 'Apply'
    and 'Application: X' headings are not matched."""
    import re as _re
    for pat in patterns:
        rx = _re.compile(pat, _re.I)
        for getter in ("button", "link"):
            try:
                loc = page.get_by_role(getter, name=rx).first
                loc.wait_for(state="visible", timeout=1000)
            except Exception:
                continue
            try:
                loc.click(timeout=2000, force=force)
            except Exception:
                try: loc.evaluate("el => el.click()")
                except Exception: continue
            return True
    return False

def wait_for_form(page, timeout_ms=45000):
    """Poll up to timeout for a real application form (First name / Email visible).
    A page with a visible password field is the account/login step, NOT the form."""
    import re as _re
    step = 1000
    waited = 0
    while waited < timeout_ms:
        pw_visible = False
        try:
            pw_visible = any(p.is_visible() for p in page.query_selector_all("input[type=password]"))
        except Exception:
            pass
        if not pw_visible:
            for probe in ("first name", "email"):
                rx = _re.compile(probe, _re.I)
                for how in ("get_by_label", "get_by_placeholder"):
                    try:
                        loc = getattr(page, how)(rx).first
                        if loc.is_visible():
                            return True
                    except Exception:
                        pass
        page.wait_for_timeout(step); waited += step
    return False

def load_work_history():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "work_history.yaml")
    if not os.path.exists(p):
        return []
    try:
        return (yaml.safe_load(open(p, encoding="utf-8")) or {}).get("jobs", []) or []
    except Exception:
        return []

def dump_workday_experience_dom(page, path="data/experience_dom_dump.txt"):
    """READ-ONLY inventory of the Work Experience area. Writes every input/textarea/
    button with its id, data-automation-id and label so live selectors are visible
    without DevTools. Types nothing."""
    js = r"""
    () => {
      const norm = s => (s||'').replace(/\s+/g,' ').trim().slice(0,80);
      const labelFor = el => {
        let t = el.getAttribute('aria-label') || '';
        if (!t && el.id){const l=document.querySelector('label[for=\''+(el.id.replace(/([^\w-])/g,'\\\\$1'))+'\']'); if(l)t=l.textContent;}
        if (!t){const l=el.closest('label'); if(l)t=l.textContent;}
        return norm(t);
      };
      const rows = [];
      rows.push('=== ids starting workExperience- ===');
      [...document.querySelectorAll('[id^="workExperience-"]')].forEach(e=>{
        rows.push(e.tagName.toLowerCase()+(e.type?'[type='+e.type+']':'')+'  id="'+e.id+'"  aid="'+(e.getAttribute('data-automation-id')||'')+'"  label="'+labelFor(e)+'"');
      });
      rows.push('');
      rows.push('=== all inputs/textareas/buttons with a label or automation-id ===');
      [...document.querySelectorAll('input,textarea,select,button')].forEach(el=>{
        const aid=el.getAttribute('data-automation-id')||'', lab=labelFor(el);
        if(!aid && !lab && !el.id) return;
        rows.push(el.tagName.toLowerCase()+(el.type?'[type='+el.type+']':'')+'  id="'+(el.id||'')+'"  aid="'+aid+'"  label="'+lab+'"');
      });
      return rows.join('\n');
    }
    """
    try:
        txt = page.evaluate(js)
    except Exception as e:
        txt = f"(dump failed: {e})"
    try:
        full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w", encoding="utf-8").write(txt)
        print(f"    [experience] DOM inventory written to {path} ({len(txt)} chars)")
    except Exception as e:
        print(f"    [experience] could not write DOM dump: {e}")
    return txt


# ---------------------------------------------------------------------------
# Surescripts/Workday "My Experience" work-history fill.
#
# Verified against the live Surescripts tenant (wd503): the Work Experience
# fields carry NO data-automation-id. They are keyed by ELEMENT ID in the
# pattern  workExperience-<N>--<field>, where <N> is a per-candidate counter
# that is NOT 1-based (this account was on workExperience-6). So we never
# hardcode the number: click the Work Experience "Add" button, read the index
# off the new block's jobTitle id, and fill by id.
#
#   Job Title            input#workExperience-<N>--jobTitle
#   Company              input#workExperience-<N>--companyName
#   Location             input#workExperience-<N>--location
#   Role Description     textarea#workExperience-<N>--roleDescription
#   I currently work here input[type=checkbox]#workExperience-<N>--currentlyWorkHere
#   From month/year      input#workExperience-<N>--startDate-dateSectionMonth-input
#                        input#workExperience-<N>--startDate-dateSectionYear-input
#   To month/year        input#workExperience-<N>--endDate-dateSectionMonth-input
#                        input#workExperience-<N>--endDate-dateSectionYear-input
#
# There is no per-entry container with an automation-id; the shared id prefix
# workExperience-<N>-- IS the grouping key. The Work Experience section wrapper
# is data-automation-id="applyFlowMyExpPage" and each field wrapper is
# data-automation-id="formField-<field>". The section "Add" button is
# data-automation-id="add-button" but the page has several (Education, Websites
# too), so we pick the one whose nearest heading is "Work Experience".
# ---------------------------------------------------------------------------

def _wd_exp_indices(page):
    """Existing Work Experience block indices, e.g. ['6'], in document order."""
    out = []
    try:
        for el in page.query_selector_all('input[id^="workExperience-"][id$="--jobTitle"]'):
            m = re.match(r'workExperience-(\d+)--jobTitle$', el.get_attribute("id") or "")
            if m:
                out.append(m.group(1))
    except Exception:
        pass
    return out


def _wd_click_add_work_experience(page):
    """Click the Add button that belongs to the Work Experience section."""
    try:
        idx = page.evaluate("""() => {
            const adds=[...document.querySelectorAll('button[data-automation-id="add-button"]')];
            for(let i=0;i<adds.length;i++){
                let n=adds[i], h='';
                for(let k=0;k<12 && n;k++){ n=n.parentElement; if(!n) break;
                    const el=n.querySelector && n.querySelector('h2,h3,h4,[role=heading]');
                    if(el){ h=el.textContent||''; break; } }
                if(/work experience/i.test(h)) return i;
            }
            return -1;
        }""")
    except Exception:
        idx = -1
    if idx is None or idx < 0:
        return False
    adds = page.query_selector_all('button[data-automation-id="add-button"]')
    if idx >= len(adds):
        return False
    try:
        adds[idx].scroll_into_view_if_needed()
        adds[idx].click(force=True)
        page.wait_for_timeout(900)
        return True
    except Exception:
        return False


def _wd_fill_id(page, field_id, value):
    """Set a text input/textarea by id. Tries Playwright fill (short timeout),
    then a React-aware JS setter so the value sticks in controlled components.
    Never blocks longer than the timeout."""
    if value is None or str(value) == "":
        return False
    val = str(value)
    try:
        page.locator(f'[id="{field_id}"]').fill(val, timeout=4000)
        return True
    except Exception:
        pass
    try:
        return bool(page.evaluate(
            """([id, val]) => {
                const el = document.getElementById(id); if(!el) return false;
                const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.dispatchEvent(new Event('blur', {bubbles:true}));
                return true;
            }""", [field_id, val]))
    except Exception:
        return False


def _wd_fill_date_id(page, idx, which, mmYYYY):
    """Fill a Workday MM/YYYY date. which='startDate'|'endDate'. Types the whole date
    as one keystroke chain into the MONTH segment (Workday auto-advances month->year
    after 2 digits, exactly like a human), then verifies via the display element after
    blur (the masked <input> reverts on blur, so reading input_value lies). Retries.
    Returns True only on a verified display value."""
    if not mmYYYY or "/" not in mmYYYY:
        return False
    mm, yyyy = [p.strip() for p in mmYYYY.split("/", 1)]
    mm = mm.zfill(2)
    base = f"workExperience-{idx}--{which}"
    month_in = page.locator(f'[id="{base}-dateSectionMonth-input"]')
    year_disp_id = f"{base}-dateSectionYear-display"
    month_disp_id = f"{base}-dateSectionMonth-display"

    def _verify():
        try:
            md = (page.locator(f'[id="{month_disp_id}"]').inner_text(timeout=800) or "").strip()
            yd = (page.locator(f'[id="{year_disp_id}"]').inner_text(timeout=800) or "").strip()
            return md.lstrip("0") == mm.lstrip("0") and yd == yyyy
        except Exception:
            return False

    for attempt in range(3):
        try:
            month_in.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            month_in.click(timeout=3000)
            try:
                month_in.press("Control+a", timeout=1200)
                month_in.press("Delete", timeout=1200)
            except Exception:
                pass
            # one continuous chain: month (auto-advances) then year
            page.keyboard.type(mm + yyyy, delay=90)
            page.wait_for_timeout(200)
            if not globals().get("_DATE_DBG_DONE"):
                try:
                    iv = month_in.input_value(timeout=600)
                except Exception as _e:
                    iv = f"<err {_e}>"
                try:
                    md = page.locator(f'[id="{month_disp_id}"]').inner_text(timeout=600)
                    yd = page.locator(f'[id="{year_disp_id}"]').inner_text(timeout=600)
                except Exception as _e:
                    md = yd = f"<err {_e}>"
                print(f"    [date-dbg] after type: month_input={iv!r} monthDisp={md!r} yearDisp={yd!r}")
            # commit the field by moving focus off it
            try: month_in.press("Tab", timeout=1000)
            except Exception: pass
            page.wait_for_timeout(250)
            if not globals().get("_DATE_DBG_DONE"):
                try:
                    md2 = page.locator(f'[id="{month_disp_id}"]').inner_text(timeout=600)
                    yd2 = page.locator(f'[id="{year_disp_id}"]').inner_text(timeout=600)
                except Exception as _e:
                    md2 = yd2 = f"<err {_e}>"
                print(f"    [date-dbg] after Tab:  monthDisp={md2!r} yearDisp={yd2!r}")
                globals()["_DATE_DBG_DONE"] = True
        except Exception as _e:
            if not globals().get("_DATE_ERR_DONE"):
                print(f"    [date-dbg] attempt {attempt} EXCEPTION: {str(_e)[:180]}")
                globals()["_DATE_ERR_DONE"] = True
            continue
        if _verify():
            return True
        page.wait_for_timeout(250)
    return False


def _wd_set_current(page, idx, current):
    """Toggle 'I currently work here' via a native JS click (fires React onChange).
    Avoids Playwright .check() stalling on Workday's custom checkbox widget."""
    fid = f"workExperience-{idx}--currentlyWorkHere"
    try:
        return bool(page.evaluate(
            """([id, want]) => {
                const el = document.getElementById(id); if(!el) return false;
                if (el.checked !== want) el.click();
                return el.checked === want;
            }""", [fid, bool(current)]))
    except Exception:
        return False


def _wd_delete_experience_block(page, idx):
    """Delete one Work Experience block by its index. Finds the block's delete/remove
    button by climbing from its jobTitle input; if absent, dumps the nearby buttons to
    the log. Any confirm click is scoped to a modal dialog only."""
    try:
        info = page.evaluate(r"""(idx)=>{
          const t=document.getElementById('workExperience-'+idx+'--jobTitle'); if(!t) return {found:false,dump:[]};
          let n=t, btn=null, hops=0;
          while(n && hops++<14){
            btn = n.querySelector('button[data-automation-id*="elete"],button[data-automation-id*="emove"],'
                 +'button[aria-label^="Delete"],button[aria-label^="Remove"],[data-automation-id="panel-set-delete-button"]');
            if(!btn){
              // Workday renders the per-block delete as a plain <button>Delete</button>
              // with no automation-id, so match on visible text as a fallback.
              const bs=[...n.querySelectorAll('button')];
              btn = bs.find(b=>/^(delete|remove)$/i.test((b.innerText||b.getAttribute('aria-label')||'').trim()));
            }
            if(btn) break; n=n.parentElement;
          }
          if(!btn){
            let m=t, dump=[];
            for(let k=0;k<14&&m;k++){ m=m.parentElement; if(!m)break;
              m.querySelectorAll('button').forEach(b=>dump.push((b.getAttribute('data-automation-id')||'')
                +':'+((b.getAttribute('aria-label')||b.innerText||'').trim().slice(0,24))));
              if(dump.length>=3) break;
            }
            return {found:false, dump:[...new Set(dump)].slice(0,20)};
          }
          document.querySelectorAll('[data-__delme]').forEach(e=>e.removeAttribute('data-__delme'));
          btn.setAttribute('data-__delme','1'); return {found:true};
        }""", idx)
        if not info.get("found"):
            print(f"    [experience] block {idx}: no delete button; nearby buttons: {info.get('dump')}")
            return False
        page.click('button[data-__delme="1"]', timeout=1500)
        page.wait_for_timeout(400)
        # confirm ONLY inside a modal dialog
        try:
            btn = page.evaluate_handle(r"""() => {
              const dlg=document.querySelector('[role=dialog],[aria-modal=true]'); if(!dlg) return null;
              const bs=[...dlg.querySelectorAll('button')];
              return bs.find(b=>/^(delete|ok|yes|confirm)$/i.test((b.innerText||'').trim())) || null;
            }""")
            el = btn.as_element()
            if el:
                el.click(); page.wait_for_timeout(400)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"    [experience] delete error {idx}: {e}")
        return False

def _wd_prune_experience(page):
    """Remove Work Experience blocks left EMPTY (no company) -- e.g. extra blocks the
    Workday resume parser created beyond the jobs we filled. Re-queries each pass
    since indices shift after a delete. Capped."""
    for _ in range(12):
        blocks = page.evaluate(r"""() => {
          const out=[];
          document.querySelectorAll('input[id^="workExperience-"][id$="--jobTitle"]').forEach(t=>{
            const m=t.id.match(/workExperience-(\d+)--jobTitle/); if(!m) return;
            const idx=m[1];
            const c=document.getElementById('workExperience-'+idx+'--companyName');
            out.push({idx, company:c?(c.value||'').trim():'', title:(t.value||'').trim()});
          });
          return out;
        }""")
        empties=[b for b in blocks if not b["company"]]
        if not empties:
            return
        b=empties[0]
        print(f"    [experience] pruning empty block {b['idx']}")
        if not _wd_delete_experience_block(page, b["idx"]):
            return
        page.wait_for_timeout(600)

def correct_workday_experience(page, dry):
    """Fill Workday work history from data/work_history.yaml using live-verified,
    id-based selectors. Read-only inventory first. Every step is timeout-bounded so
    it can never hang. Never submits."""
    jobs = load_work_history()
    if not jobs:
        print("    [experience] no data/work_history.yaml -- skipping")
        return

    dump_workday_experience_dom(page)  # read-only, safe in dry runs

    existing = _wd_exp_indices(page)
    print(f"    [experience] {len(existing)} existing block(s); {len(jobs)} job(s) to enter")

    if dry:
        for i, j in enumerate(jobs):
            print(f"      would set: {j['title']} @ {j['company']} "
                  f"({j['start']}-{j['end'] or 'present'})")
        return

    for i, j in enumerate(jobs):
        try:
            if i < len(existing):
                idx = existing[i]
            else:
                before = set(_wd_exp_indices(page))
                if not _wd_click_add_work_experience(page):
                    print("    [experience] Work Experience Add button not found -- stopping")
                    break
                page.wait_for_timeout(700)
                fresh = [x for x in _wd_exp_indices(page) if x not in before]
                if not fresh:
                    print("    [experience] Add did not create a new block -- stopping")
                    break
                idx = fresh[-1]

            print(f"    [experience] block {idx}: filling {j.get('title','')} @ {j.get('company','')} ...")
            _wd_fill_id(page, f"workExperience-{idx}--jobTitle", j.get("title", ""))
            _wd_fill_id(page, f"workExperience-{idx}--companyName", j.get("company", ""))
            _wd_fill_id(page, f"workExperience-{idx}--location", j.get("location", ""))
            _wd_fill_id(page, f"workExperience-{idx}--roleDescription", (j.get("description", "") or "").strip())
            sd = _wd_fill_date_id(page, idx, "startDate", j.get("start", ""))
            if j.get("current"):
                _wd_set_current(page, idx, True)
                ed = True
            else:
                _wd_set_current(page, idx, False)
                ed = _wd_fill_date_id(page, idx, "endDate", j.get("end", ""))
            dstat = "dates OK" if (sd and ed) else f"DATES FAILED (start={sd} end={ed})"
            print(f"    [experience] block {idx}: done "
                  f"({j.get('start','')}-{j.get('end','') or 'present'}) [{dstat}]")
        except Exception as e:
            print(f"    [experience] block {i+1} error (continuing): {e}")
            continue
    try:
        _wd_prune_experience(page)
    except Exception as e:
        print(f"    [experience] prune error: {e}")


def load_secrets():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "secrets.yaml")
    if not os.path.exists(p):
        # first run: seed from the example so downloads never overwrite a real one
        ex = os.path.join(here, "secrets.example.yaml")
        if os.path.exists(ex):
            try:
                import shutil; shutil.copyfile(ex, p)
                print("Created secrets.yaml from the example — add your Workday password there.")
            except Exception:
                pass
    if not os.path.exists(p):
        return {}
    try:
        return yaml.safe_load(open(p, encoding="utf-8")) or {}
    except Exception:
        return {}

def _click_text(page, texts, timeout_each=1500):
    """Click the first visible button/link whose text matches any of `texts`."""
    for b in page.query_selector_all("button, a, [role=button], [data-automation-id]"):
        try:
            if not b.is_visible():
                continue
            t = (b.inner_text() or "").strip().lower()
        except Exception:
            continue
        if t and any(x == t or x in t for x in texts):
            try:
                b.click(); page.wait_for_timeout(timeout_each); return True
            except Exception:
                pass
    return False

def _wd_pw_fields(page):
    try:
        return [p for p in page.query_selector_all('input[type=password]') if _visible(p)]
    except Exception:
        return []

def _wd_dump_account_dom(page):
    """Log every visible input/button on the account page with its Workday
    data-automation-id and label, so we can target fields exactly."""
    try:
        rows = page.evaluate(r"""() => {
          const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
            return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none'; };
          const lab = el => {
            let t = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
            if (!t && el.id){ const l=document.querySelector('label[for="'+el.id+'"]'); if(l) t=l.innerText; }
            return (t||'').trim().slice(0,40);
          };
          return [...document.querySelectorAll('input, button, [role=button]')].map((el,i)=>({
            i, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type')||''),
            aid: el.getAttribute('data-automation-id')||'', label: lab(el), vis: vis(el)
          })).filter(r => r.vis);
        }""")
        print("    [account-dom] %d visible fields/buttons:" % len(rows))
        for r in rows:
            print("      #%02d %-7s type=%-9s aid=%-30s label=%s" % (
                r["i"], r["tag"], r["type"], (r["aid"] or "-")[:30], r["label"]))
    except Exception as e:
        print("    [account-dom] dump failed: %s" % e)

def workday_login(page, dry):
    """Get past Workday's account step. Creates the account (fills email + both
    passwords + terms); if it already exists, Workday shows a Sign In and we fall
    through to signing in. Returns 'in' or 'manual'."""
    creds = (load_secrets().get("workday") or {})
    email, pw = creds.get("email", ""), creds.get("password", "")
    if not email or pw in ("", "CHANGE-ME") or dry:
        return "manual"

    def email_field():
        return page.query_selector('input[type=email], input[autocomplete="username"], '
                                   'input[name="email"], input[data-automation-id="email"]')

    def _really_in():
        # logged in = login form gone AND stays gone (avoid transient reload false-positive)
        if looks_like_login(page):
            return False
        page.wait_for_timeout(1200)
        return not looks_like_login(page)

    def sign_in_attempt():
        # If we're on the Create Account view (2+ password fields), switch to Sign In
        # via the signInLink control (it has no text label, so match by automation-id).
        if len(_wd_pw_fields(page)) >= 2:
            for lk in page.query_selector_all('[data-automation-id="signInLink"]'):
                try:
                    if _visible(lk):
                        lk.click(force=True); break
                except Exception:
                    pass
            page.wait_for_timeout(1500)
        # wait for the sign-in fields to actually be present
        e, pws = None, []
        for _ in range(8):
            e, pws = email_field(), _wd_pw_fields(page)
            if e and pws:
                break
            page.wait_for_timeout(500)
        if not e or not pws:
            return False
        try:
            e.fill(email); pws[0].fill(pw)
        except Exception:
            return False
        page.wait_for_timeout(600)
        # The version that worked: force-click the click_filter div overlay.
        clicked = False
        for sel in ('[data-automation-id="click_filter"]',
                    'div[role="button"][aria-label="Sign In"]'):
            try:
                b = page.query_selector(sel)
                if b:
                    b.scroll_into_view_if_needed()
                    b.click(force=True); clicked = True
                    print(f"    signed-in click via {sel}")
                    break
            except Exception:
                pass
        if not clicked:
            try: pws[0].press("Enter")
            except Exception: pass
        for _ in range(8):
            page.wait_for_timeout(800)
            if _really_in():
                return True
        # If it failed, capture why (helps fix without guessing).
        try:
            errs = page.evaluate(r"""() => {
              const els = [...document.querySelectorAll('[role=alert], [class*=rror], [data-automation-id*=rror]')];
              return els.map(e => (e.innerText||'').trim()).filter(Boolean).slice(0,4);
            }""")
            print(f"    [signin-diag] still_login={looks_like_login(page)} errors={errs}")
        except Exception:
            pass
        return False

    def _vis_by_aid(aid):
        for el in page.query_selector_all('[data-automation-id="%s"]' % aid):
            try:
                if _visible(el):
                    return el
            except Exception:
                pass
        return None

    def _all_vis(aid):
        return [el for el in page.query_selector_all('[data-automation-id="%s"]' % aid)
                if _visible(el)]

    def create_attempt():
        # some tenants render the Create Account panel more than once;
        # only one copy is the one you see. Fill EVERY visible copy so the real one
        # always gets the values. The beecatcher honeypot has its own aid, so it is
        # never touched here.
        if not _all_vis("verifyPassword"):
            click_by_role(page, [r"^create account$"]); page.wait_for_timeout(1200)
        ems, pwds, vers = _all_vis("email"), _all_vis("password"), _all_vis("verifyPassword")
        if not (ems and pwds and vers):
            return False
        try:
            for e in ems: e.fill(email)
            for e in pwds: e.fill(pw)
            for e in vers: e.fill(pw)
            for cb in _all_vis("createAccountCheckbox") or page.query_selector_all('input[type=checkbox]'):
                try:
                    if _visible(cb): cb.check()
                except Exception:
                    pass
        except Exception:
            return False
        # Confirm a value actually landed in a visible field before submitting.
        if not any((e.input_value() or "") for e in ems):
            print("    [create] fields would not accept input")
            return False
        clicked = False
        for b in _all_vis("createAccountSubmitButton"):
            try:
                b.scroll_into_view_if_needed(); b.click(force=True); clicked = True
                print("    create-account submit clicked"); break
            except Exception:
                pass
        if not clicked:
            for d in page.query_selector_all('[data-automation-id="click_filter"]'):
                try:
                    if _visible(d): d.click(force=True); clicked = True; break
                except Exception:
                    pass
        for _ in range(8):
            page.wait_for_timeout(800)
            if _really_in():
                return True
        try:
            errs = page.evaluate(r"""() => [...document.querySelectorAll('[data-automation-id*=rror],[role=alert],[class*=rror]')].map(e=>(e.innerText||'').trim()).filter(Boolean).slice(0,6)""")
            print("    [create-diag] still_login=%s errors=%s" % (looks_like_login(page), errs))
        except Exception:
            pass
        return False

    try:
        npw = len(_wd_pw_fields(page))
        has_email = email_field() is not None
        print(f"    [login] pw_fields={npw} email_field={has_email} looks_login={looks_like_login(page)}")
        # Decide by page shape: a Verify/Confirm field (2+ password inputs) = Create
        # Account view; a single password input = Sign In view.
        # Sign In FIRST: a returning applicant already has an account, so
        # Create Account would fail ("already exists") and leave the page in an
        # error state that breaks the sign-in fallback. Only create if sign-in
        # genuinely fails.
        # A visible verifyPassword field means the Create Account panel is showing;
        # this tenant is new to us, so create first. Otherwise sign in first.
        if _vis_by_aid("verifyPassword"):
            if create_attempt(): return "in"
            if sign_in_attempt(): return "in"
        else:
            if sign_in_attempt(): return "in"
            if create_attempt(): return "in"
        _wd_dump_account_dom(page)
        return "manual"
    except Exception as e:
        print(f"    [login] error: {e}")
        return "manual"

def workday_next(page):
    """Advance Workday's wizard. Prefer the footer Next button by its data-automation-id
    (Workday hides the real button and overlays a click_filter div), else fall back to text."""
    for sel in ('div[data-automation-id="pageFooterNextButton"] [data-automation-id="click_filter"]',
                'button[data-automation-id="pageFooterNextButton"]',
                'div[data-automation-id="pageFooterNextButton"]',
                '[data-automation-id="bottom-navigation-next-button"]'):
        try:
            b = page.query_selector(sel)
            if b:
                b.scroll_into_view_if_needed()
                b.click(force=True)
                return True
        except Exception:
            pass
    return _click_text(page, ["save and continue", "continue", "next"], 1600)

def _icims_iform_eeo(page):
    """iCIMS iForm EEO self-ID: radios have EMPTY labels; the option lives in the value/id
    (name 'icims_f_Veteran' -> value 'NotProtectedVeteran'/'ProtectedVeteran'/'optout').
    Match the profile eeo answer against the value/id with negation-aware token scoring."""
    eeo = {
        "veteran": _eeo_get("veteran_status") or "",
        "disab":   _eeo_get("disability_status") or "",
        "gender":  _eeo_get("gender") or "",
        "sex":     _eeo_get("gender") or "",
        "race":    _eeo_get("race") or "",
        "ethnic":  _eeo_get("race") or "",
        "hispanic":_eeo_get("hispanic_latino") or "",
    }
    _JS = r"""(eeo)=>{
          const norm=(s)=>(s||'').replace(/([a-z])([A-Z])/g,'$1 $2').replace(/[_\-]/g,' ')
            .toLowerCase().split(/\s+/).filter(Boolean);
          const neg=(toks,raw)=>toks.includes('not')||toks.includes('no')||toks.includes('optout')||
            /\bnot\b|\bno\b|decline|wish not|do not/.test((raw||'').toLowerCase());
          const groups={};
          for(const e of document.querySelectorAll('input[type=radio],input[type=checkbox]')){
            if(e.offsetParent===null) continue;
            const nm=e.name||''; (groups[nm]=groups[nm]||[]).push(e);
          }
          const done=[];
          for(const nm in groups){
            const key=Object.keys(eeo).find(k=>nm.toLowerCase().includes(k));
            if(!key) continue;
            const phrase=eeo[key]; if(!phrase) continue;
            const pt=norm(phrase), pset=new Set(pt), pneg=neg(pt,phrase);
            let best=null, bestScore=0;
            for(const r of groups[nm]){
              const cand=(r.value||'')+' '+((r.id||'').split('_').pop()||'');
              const ct=norm(cand);
              let inter=ct.filter(t=>pset.has(t)).length;
              let score=inter - (neg(ct,cand)!==pneg ? 3 : 0);
              if(score>bestScore){ bestScore=score; best=r; }
            }
            if(best && bestScore>0){
              best.checked=true;
              try{ best.click(); }catch(_){}
              best.dispatchEvent(new Event('input',{bubbles:true}));
              best.dispatchEvent(new Event('change',{bubbles:true}));
              done.push(nm+' -> '+(best.value||best.id)+' checked='+best.checked);
            }
          }
          return done;
        }"""
    picks = []
    for fr in [page] + list(getattr(page, "frames", []) or []):
        try:
            r = fr.evaluate(_JS, eeo)
            if r: picks += r
        except Exception:
            pass
    for pk in picks:
        print(f"    [icims-eeo] {pk}")
    if not picks:
        print("    [icims-eeo] no self-ID group matched")

def _icims_tick_signature(page):
    """Tick a required signature/attestation checkbox whose own label is empty and whose
    meaning is in nearby text ('...equivalent to a handwritten signature'). Completes the
    form; the human still reviews before the final Submit."""
    try:
        page.wait_for_timeout(300)
        n = page.evaluate(r"""()=>{
          const rx=/equivalent to a (handwritten )?signature|electronic signature|\bsignature\b/i;
          const cand=[...document.querySelectorAll('div,section,fieldset,p,label,span,td')]
            .filter(n=>rx.test(n.textContent||'') && (n.textContent||'').length<300);
          for(const box of cand){
            let scope=box;
            for(let i=0;i<4&&scope;i++){
              const cb=scope.querySelector('input[type=checkbox]');
              if(cb){ if(!cb.checked){ cb.click(); } return 1; }
              scope=scope.parentElement;
            }
          }
          return 0;
        }""")
        if n:
            print("    [icims-signature] ticked signature attestation")
    except Exception as e:
        print(f"    [icims-signature] failed: {e}")

def _icims_use_address_anyway(page):
    """iCIMS validates City+Zip+State against a geo-DB that lacks small towns and shows
    'Use this address anyway? [ ] Yes'. The address IS valid, so tick that override. The
    checkbox's own label is just 'Yes', so we locate it by the nearby verification text.
    This runs AFTER the address is filled, since the error only renders post-validation."""
    try:
        page.wait_for_timeout(600)   # let the validation message render
        n = page.evaluate(r"""()=>{
          const rx=/use this address anyway/i;
          const cand=[...document.querySelectorAll('div,section,fieldset,p,label,span')]
            .filter(n=>rx.test(n.textContent||'') && (n.textContent||'').length<400);
          for(const box of cand){
            let scope=box;
            for(let i=0;i<4&&scope;i++){
              const cb=scope.querySelector('input[type=checkbox]');
              if(cb){ if(!cb.checked){ cb.click(); } return 1; }
              scope=scope.parentElement;
            }
          }
          return 0;
        }""")
        if n:
            print("    [icims-address] ticked 'Use this address anyway'")
    except Exception as e:
        print(f"    [icims-address] failed: {e}")

def _icims_create_login(page, dry):
    """iCIMS embeds account creation ('Create a login': Login + Password + Re-enter) INSIDE
    the application form. Fill it from secrets.yaml (login = email, password = the stored
    one) so applying creates the account. The tool - not Claude - supplies the password,
    read from the user's local secrets.yaml, same as the Workday login."""
    creds = (load_secrets().get("icims") or {})
    email, pw = creds.get("email", ""), creds.get("password", "")
    if dry:
        return
    if not email or pw in ("", "CHANGE-ME"):
        print("    [icims-login] add your icims.password to secrets.yaml to auto-create the account")
        return
    filled = 0
    for fr in [page] + list(getattr(page, "frames", []) or []):
        try:
            filled += fr.evaluate(r"""([email,pw])=>{
              const setv=(e,v)=>{const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
                s.call(e,v); e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}));};
              const vis=e=>e.offsetParent!==null;
              const labFor=(e)=>{ let t='';
                const l=e.closest('label')||(e.id&&document.querySelector("label[for='"+e.id+"']"));
                if(l) t=l.innerText;
                if(!t){ const a=e.getAttribute('aria-labelledby'); if(a){const n=document.getElementById(a); if(n)t=n.innerText;} }
                if(!t){ let p=e.parentElement; for(let i=0;i<3&&p;i++){ const lb=p.querySelector('label'); if(lb){t=lb.innerText;break;} p=p.parentElement; } }
                return (t||'').trim().toLowerCase(); };
              let n=0;
              for(const e of document.querySelectorAll('input[type=password]')){ if(vis(e)){ setv(e,pw); n++; } }
              for(const e of document.querySelectorAll('input[type=text],input:not([type]),input[type=email]')){
                if(!vis(e)) continue;
                const t=labFor(e);
                if(/(^|\b)login(\b|$)|user\s?name|user\s?id/.test(t) && !/password/.test(t)){
                  if(!(e.value||'').trim()){ setv(e,email); n++; } break;
                }
              }
              return n;
            }""", [email, pw])
        except Exception:
            pass
    print(f"    [icims-login] create-account fields filled: {filled} (login={email})")

def _icims_captcha_present(page):
    try:
        for fr in list(getattr(page, "frames", []) or []):
            u = (getattr(fr, "url", "") or "").lower()
            if "hcaptcha.com" in u or "recaptcha" in u or "captcha" in u:
                return True
        return bool(page.query_selector("iframe[src*=hcaptcha], iframe[src*=recaptcha], .h-captcha, .g-recaptcha"))
    except Exception:
        return False

def _icims_login(page, dry):
    """iCIMS gates login behind hCaptcha, which by design cannot be automated. So we only
    PRE-FILL the email as a convenience, then hand off: the human solves the captcha, enters
    the password and signs in, and re-runs autofill on the form (iCIMS keeps the session in
    the browser profile). Returns 'captcha-handoff', 'in', or 'manual'."""
    email = (load_secrets().get("icims") or {}).get("email", "")
    if not dry and email:
        for sel in ("#email", "input[name=css_loginName]", "input[type=email]",
                    "input[autocomplete=username]"):
            try:
                e = page.query_selector(sel)
                if e and _visible(e) and not (e.input_value() or "").strip():
                    e.fill(email); break
            except Exception:
                pass
    if _icims_captcha_present(page):
        print(">>> iCIMS login is protected by a CAPTCHA, which cannot be automated.")
        print("    I pre-filled your email. Please solve the captcha, enter your password,")
        print("    and sign in; then click Apply Autofill again to fill the application form.")
        return "captcha-handoff"
    return "in" if not looks_like_login(page) else "manual"

def looks_like_login(page):
    """A visible password field almost always means a sign-in / create-account wall.
    Application forms don't have password fields; login pages do."""
    try:
        for el in page.query_selector_all("input[type=password]"):
            if el.is_visible():
                return True
    except Exception:
        pass
    for aid in ("signInContent", "createAccountLink", "signInLink", "backToSignInLink"):
        try:
            el = page.query_selector(f'[data-automation-id="{aid}"]')
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False

def _probe_choices(page, path="data/choices_probe.txt"):
    """Dump radio/checkbox controls (across frames) with type/name/value/id, the label our
    heuristic finds, and the raw nearby text - to see how iForm structures its options."""
    try:
        rows=[]
        for fr in [page] + list(getattr(page, "frames", []) or []):
            try:
                data = fr.evaluate(r"""()=>{
                  const out=[];
                  for(const e of document.querySelectorAll('input[type=radio],input[type=checkbox]')){
                    if(e.offsetParent===null) continue;
                    const lfor = e.id && document.querySelector("label[for='"+e.id+"']");
                    const lclose = e.closest('label');
                    let near=''; let p=e.parentElement;
                    for(let i=0;i<4&&p;i++){ const t=(p.innerText||'').trim(); if(t){near=t.slice(0,120);break;} p=p.parentElement; }
                    out.push({type:e.type, name:e.name, value:e.value, id:e.id,
                      labelFor:(lfor?lfor.innerText.trim().slice(0,80):''),
                      labelClosest:(lclose?lclose.innerText.trim().slice(0,80):''),
                      near:near});
                  }
                  return out;
                }""")
                rows.extend(data)
            except Exception as e:
                rows.append({"frame_error": str(e)})
        import json as _j
        with open(path,"w",encoding="utf-8") as f: f.write(_j.dumps(rows, indent=1, ensure_ascii=False))
        print(f"    [choices-probe] {len(rows)} control(s) -> {path}")
    except Exception as e:
        print(f"    [choices-probe] failed: {e}")

def _probe_login(page, path="data/login_probe.txt"):
    """Capture inputs + buttons on an account/login page (across frames) so we can build a
    precise login-fill (email step, next button, password step, sign-in button)."""
    try:
        rows = []
        for fr in [page] + list(getattr(page, "frames", []) or []):
            try:
                data = fr.evaluate(r"""()=>{
                  const vis=(e)=>e.offsetParent!==null;
                  const out={url:location.href, inputs:[], buttons:[]};
                  for(const e of document.querySelectorAll('input')){
                    const a={}; for(const x of e.attributes) a[x.name]=x.value;
                    out.inputs.push({type:e.type, id:e.id, name:e.name, ph:e.placeholder,
                      vis:vis(e), aria:e.getAttribute('aria-label'), cls:e.className, attrs:a});
                  }
                  for(const b of document.querySelectorAll('button,a[role=button],input[type=submit],[role=button]')){
                    out.buttons.push({tag:b.tagName.toLowerCase(), text:(b.innerText||b.value||'').trim().slice(0,40),
                      id:b.id, name:b.getAttribute('name'), vis:vis(b), cls:b.className});
                  }
                  return out;
                }""")
                rows.append(data)
            except Exception as e:
                rows.append({"frame_error": str(e)})
        import json as _j
        with open(path,"w",encoding="utf-8") as f: f.write(_j.dumps(rows, indent=1, ensure_ascii=False))
        print(f"    [login-probe] captured {len(rows)} frame(s) -> {path}")
    except Exception as e:
        print(f"    [login-probe] failed: {e}")

def _probe_address(page, path="data/address_probe.txt"):
    """Capture the exact DOM of any address field so we can see how the autocomplete
    widget stores its value (single input vs hidden value + search box vs chips)."""
    try:
        data = page.evaluate(r"""()=>{
          const out=[];
          const labs=[...document.querySelectorAll('label,div,span')]
            .filter(n=>/\baddress\b/i.test((n.innerText||'').slice(0,60)));
          const roots=new Set();
          for(const n of labs){ const b=n.closest('label,.form-group,[class*=field],[class*=Field],div')||n.parentElement; if(b) roots.add(b);}
          const seen=new Set();
          for(const box of roots){
            for(const e of box.querySelectorAll('input,textarea,[role=combobox],[contenteditable]')){
              if(seen.has(e)) continue; seen.add(e);
              const cs=getComputedStyle(e);
              const attrs={}; for(const a of e.attributes) attrs[a.name]=a.value;
              out.push({tag:e.tagName.toLowerCase(), type:e.getAttribute('type'),
                hidden:(e.type==='hidden'||e.offsetParent===null),
                value:(e.value!==undefined?e.value:e.textContent),
                role:e.getAttribute('role'), autocomplete:e.getAttribute('autocomplete'),
                cls:e.className, attrs:attrs,
                parent:(e.parentElement?e.parentElement.outerHTML.slice(0,900):'')});
            }
          }
          // Google Places dropdown present?
          out.push({pac_containers: document.querySelectorAll('.pac-container').length,
                    pac_items: document.querySelectorAll('.pac-item').length});
          return out;
        }""")
        import json as _j
        with open(path,"w",encoding="utf-8") as f: f.write(_j.dumps(data, indent=1, ensure_ascii=False))
        print(f"    [address-probe] {len(data)-1} address input(s) -> {path}")
    except Exception as e:
        print(f"    [address-probe] failed: {e}")

def _probe_phone(page, path="data/phone_probe.txt"):
    """Ground-truth capture of every phone/mobile input: visibility, geometry, computed
    style, mask hints, sibling inputs, and the END-OF-RUN value (call this AFTER the fill
    loop). Distinguishes 'React cleared it' from 'CSS/overlay hides a filled value'."""
    try:
        data = page.evaluate(r"""()=>{
          const labs=[...document.querySelectorAll('label,div,span')]
            .filter(n=>/telephone|phone|mobile|cell/i.test((n.innerText||'').slice(0,80)));
          const roots=new Set();
          for(const n of labs){ const b=n.closest('label,.form-group,[class*=field],[class*=Field],div')||n.parentElement; if(b) roots.add(b); }
          const out=[]; const seen=new Set();
          for(const box of roots){
            for(const e of box.querySelectorAll('input')){
              if(seen.has(e)) continue; seen.add(e);
              const cs=getComputedStyle(e); const r=e.getBoundingClientRect();
              const attrs={}; for(const a of e.attributes) attrs[a.name]=a.value;
              const sibs=[...(e.parentElement?e.parentElement.querySelectorAll('input'):[])]
                .map(x=>({type:x.type,hidden:x.type==='hidden'||x.offsetParent===null,cls:x.className,value:x.value}));
              out.push({
                type:e.type, hidden:(e.type==='hidden'||e.offsetParent===null),
                value:e.value,
                rect:{w:Math.round(r.width),h:Math.round(r.height)},
                style:{color:cs.color,opacity:cs.opacity,visibility:cs.visibility,display:cs.display},
                inputmode:e.getAttribute('inputmode'), autocomplete:e.getAttribute('autocomplete'),
                maxlength:e.getAttribute('maxlength'), cls:e.className,
                dataset:Object.assign({},e.dataset), attrs:attrs,
                siblings:sibs,
                self:e.outerHTML.slice(0,500),
                parent:(e.parentElement?e.parentElement.outerHTML.slice(0,1000):'')
              });
            }
          }
          return out;
        }""")
        import json as _j
        with open(path,"w",encoding="utf-8") as f:
            f.write(_j.dumps(data, indent=1, ensure_ascii=False))
        print(f"    [phone-probe] {len(data)} phone input(s) -> {path}")
    except Exception as e:
        print(f"    [phone-probe] failed: {e}")

def _dump_form_dom(page, path="data/form_dom_dump.txt"):
    """Dump the fields the walker actually sees, straight off controls() handles
    (works across frames the same way the fill does). Captures name/id/autocomplete
    too - unlabeled address inputs usually carry autocomplete=address-line1 etc.
    Non-destructive."""
    try:
        els = controls(page)
        js = r"""e => {
          const near = () => {
            let n=e, h=0;
            while(n && h++<6){
              const l = n.querySelector && n.querySelector('label,legend');
              if(l && (l.innerText||'').trim()) return (l.innerText||'').trim().split('\n')[0];
              let p=n.previousElementSibling;
              while(p){ if(/label/i.test(p.tagName)&&(p.innerText||'').trim()) return (p.innerText||'').trim().split('\n')[0]; p=p.previousElementSibling; }
              n=n.parentElement;
            }
            return '';
          };
          return {
            tag:e.tagName.toLowerCase(), type:e.getAttribute('type')||'',
            name:e.getAttribute('name')||'', id:e.id||'', role:e.getAttribute('role')||'',
            aria:(e.getAttribute('aria-label')||'').slice(0,60),
            ph:(e.getAttribute('placeholder')||'').slice(0,40),
            auto:e.getAttribute('autocomplete')||'',
            hostchain:(function(){let n=e,c=[];for(let i=0;i<6&&n;i++){let fe=n.closest&&n.closest('.slds-form-element');if(fe){let l=fe.querySelector('.slds-form-element__label,label,legend');c.push('FE:'+((l&&l.innerText||'').trim().slice(0,30)));break;}let r=n.getRootNode();if(!r||!r.host){c.push('light');break;}c.push(r.host.tagName.toLowerCase()+(r.host.label?'[label='+r.host.label+']':''));n=r.host;}return c.join(' > ');})(),
            climb:(function(){let n=e,o=[];for(let i=0;i<6&&n;i++){let r=n.getRootNode();if(!r||!r.host){o.push('light');break;}o.push(r.host.tagName.toLowerCase()+':txt='+(r.textContent||'').replace(/\s+/g,' ').trim().slice(0,40));n=r.host;}return o.join(' | ');})(),
            cls:(e.getAttribute('class')||'').slice(0,50),
            maxlen:e.getAttribute('maxlength')||'',
            imode:e.getAttribute('inputmode')||'',
            val:(e.value||e.innerText||'').trim().slice(0,30),
            label:near().slice(0,70)
          };
        }"""
        rows=[]
        for el in els:
            try: rows.append(el.evaluate(js))
            except Exception: pass
        import json, os
        os.makedirs("data", exist_ok=True)
        open(path,"w",encoding="utf-8").write(json.dumps(rows, indent=1))
        print(f"    [form-dump] {len(rows)} widgets -> {path}")
    except Exception as _e:
        print(f"    [form-dump] {_e}")

def controls(page):
    # Query inputs/dropdowns globally (Workday etc. don't wrap fields in a <form>,
    # so form-scoping would miss text inputs while still matching dropdowns).
    # Walk EVERY frame, not just the top document: iCIMS (and other ATSs) render the
    # whole application inside a same-origin iframe, so a page-level query alone finds
    # nothing. Cross-origin frames (ad/social widgets) just raise and are skipped.
    sel = ("input:not([type=hidden]):not([type=submit]):not([type=button]), "
           "textarea, select, "
           '[role="combobox"], button[aria-haspopup="listbox"], [aria-haspopup="listbox"]')
    out = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    per_frame = []
    for fr in frames:
        try:
            found = fr.query_selector_all(sel)
            per_frame.append((getattr(fr, "url", "?"), len(found)))
            out.extend(found)
        except Exception as e:
            per_frame.append((getattr(fr, "url", "?"), "x-origin/err"))
    if not out:
        try:
            out = page.query_selector_all(sel)
        except Exception:
            out = []
    if not out and not globals().get("_FRAME_DBG"):
        globals()["_FRAME_DBG"] = True
        print(f"    [frames] {len(frames)} frame(s); fields per frame:")
        for u, n in per_frame:
            print(f"       {n:>12}  {str(u)[:90]}")
    return out

def upload_resume_workable(page, dry):
    """Workable has no plain file input — it's an 'Import resume from' dropdown that
    opens the OS picker. Try: click the dropdown -> 'My computer' -> catch the file
    chooser -> set the PDF. Returns True on success, False to fall back to manual."""
    if dry or not RESUME_PATH:
        return False
    try:
        # find the "Import resume from" trigger
        trigger = None
        for b in page.query_selector_all("button, a, [role=button], div"):
            try:
                t = (b.inner_text() or "").strip().lower()
            except Exception:
                continue
            if "import resume" in t and b.is_visible():
                trigger = b; break
        if not trigger:
            return False
        trigger.click()
        page.wait_for_timeout(400)
        # click "My computer" and catch the file chooser it triggers
        opt = None
        for o in page.query_selector_all("li, [role=option], button, a, div"):
            try:
                t = (o.inner_text() or "").strip().lower()
            except Exception:
                continue
            if t in ("my computer", "computer", "upload from computer") and o.is_visible():
                opt = o; break
        if not opt:
            page.keyboard.press("Escape")
            return False
        with page.expect_file_chooser(timeout=5000) as fc_info:
            opt.click()
        fc_info.value.set_files(RESUME_PATH)
        page.wait_for_timeout(800)
        return True
    except Exception:
        try: page.keyboard.press("Escape")
        except Exception: pass
        return False

JOB_ANSWERS = []   # per-job [{match, value}] overrides, set in run()
CURRENT_ATS = ""   # detected ATS for the current run, set in run()

def job_override(label):
    """Return a locked per-job answer if this label matches one, else None."""
    low = label.lower()
    for ov in JOB_ANSWERS:
        m = (ov.get("match") or "").lower().strip()
        if m and m in low:
            return ov.get("value")
    return None

QUESTION_JS = r"""
(el) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const isOpt = t => /^(yes|no|n\/a|hybrid|on-site|remote|diploma|associate|bachelors|masters|doctorate|juris doctorate|none|less than a year|\d+\s*[-+]?\s*\d*\s*years?|\d+\+?\s*years?)$/i.test(t);
  const fs = el.closest('fieldset');
  if (fs) { const lg = fs.querySelector('legend'); if (lg && clean(lg.innerText)) return clean(lg.innerText); }
  // Salesforce Flow / LWC: the QUESTION lives in a parent component's shadow root
  // (c-as-apply-question, lightning-radio-group, ...), not near the option input.
  // Climb host-by-host and return the first host whose text reads like a question.
  let node = el;
  for (let d = 0; d < 8 && node; d++) {
    const root = node.getRootNode();
    if (!root || !root.host) break;
    const host = root.host;
    const tag = (host.tagName || '').toLowerCase();
    const named = /apply-question|radio-group|checkbox-group|-question/.test(tag);
    // the QUESTION is rendered in the component's SHADOW root; host.textContent would
    // be the slotted light children (the option labels). Read root.textContent.
    const t = clean(root.textContent || '');
    if (t && t.length >= 12 && t.length < 400 && !isOpt(t)) {
      if (named || /\?/.test(t) || t.length > 40) return t;
    }
    node = host;
  }
  // fallback: bounded parentElement climb (non-shadow forms)
  let c = el.parentElement;
  for (let i = 0; i < 6 && c; i++) {
    const txt = clean(c.innerText || '');
    if (txt.length > 20 && txt.length < 400 && !isOpt(txt)) return txt;
    c = c.parentElement;
  }
  return '';
}
"""

_EEO_CACHE = None
def _eeo_get(key):
    """Read a value from profile.yaml eeo: block (cached)."""
    global _EEO_CACHE
    if _EEO_CACHE is None:
        try:
            _EEO_CACHE = (apply.load("profile.yaml") or {}).get("eeo", {}) or {}
        except Exception:
            _EEO_CACHE = {}
    return _EEO_CACHE.get(key)

def _eeo_group_answer(el):
    """If el's radio/checkbox group is an EEO self-ID group, return the profile eeo value
    whose option is present. Uses the GROUP's OPTION texts, because the per-control label is
    often the section heading (e.g. iCIMS 'Why are you being asked to complete this form?')."""
    try:
        labs = el.evaluate(r"""(e)=>{
          let grp=[]; const nm=e.getAttribute('name');
          if(nm) grp=[...document.querySelectorAll('input[name="'+nm+'"]')];
          if(grp.length<2){ const fs=e.closest('fieldset,[role=radiogroup],[role=group],table,div,form');
            if(fs) grp=[...fs.querySelectorAll('input[type=radio],input[type=checkbox]')]; }
          const txt=(r)=>{ const l=r.closest('label')||(r.id&&document.querySelector("label[for='"+r.id+"']"));
            let t=l?(l.innerText||''):'';
            if(!t){ let p=r.parentElement; for(let i=0;i<3&&p;i++){ const t2=(p.innerText||'').trim(); if(t2&&t2.length<90){t=t2;break;} p=p.parentElement; } }
            if(!t) t=r.value||''; return (t||'').trim(); };
          return grp.map(txt);
        }""") or []
    except Exception:
        labs = []
    joined = " | ".join(labs).lower()
    if not labs:
        return ""
    if "protected veteran" in joined or re.search(r"\bveteran\b", joined):
        return _eeo_get("veteran_status") or ""
    if re.search(r"have a disability|do not have a disability|disability status|yes, i have", joined):
        return _eeo_get("disability_status") or ""
    if re.search(r"\bhispanic\b|latino", joined):
        return _eeo_get("hispanic_latino") or ""
    if re.search(r"\bmale\b", joined) and re.search(r"\bfemale\b", joined):
        return _eeo_get("gender") or ""
    if re.search(r"\bwhite\b|black or african|\basian\b|two or more races|american indian|native hawaiian", joined):
        return _eeo_get("race") or ""
    return ""

_PROFILE_CACHE = None
def _profile_list(key):
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        try: _PROFILE_CACHE = apply.load("profile.yaml") or {}
        except Exception: _PROFILE_CACHE = {}
    v = _PROFILE_CACHE.get(key)
    return [str(x) for x in v] if isinstance(v, list) else []

def _profile_multiselect(el, qtext):
    """For a 'which languages / cloud providers / OS have you used' multi-select, check the
    options that match the profile list (and ONLY those - e.g. never Go/Ruby). Returns the
    list of checked option texts, or None if the question isn't a known profile multi-select."""
    ql = (qtext or "").lower()
    if re.search(r"which .*(language|programming)|languages have you|languages are you|languages do you", ql):
        items = _profile_list("languages")
    elif re.search(r"which .*cloud (provider|platform)|cloud providers have you|clouds? have you", ql):
        items = _profile_list("cloud") + ["AWS", "Azure", "GCP"]
    elif re.search(r"which .*operating system|which os\b", ql):
        items = ["Linux", "Windows", "Ubuntu", "Red Hat", "RHEL", "CentOS", "Rocky"]
    else:
        return None
    if not items:
        return []
    try:
        return el.evaluate(r"""(el, items)=>{
          const norm=s=>(s||'').toLowerCase().replace(/[^a-z0-9+#. ]/g,' ').replace(/\s+/g,' ').trim();
          let grp=[]; const nm=el.getAttribute('name');
          if(nm) grp=[...document.querySelectorAll('input[name="'+nm+'"]')];
          if(grp.length<2){ const fs=el.closest('fieldset,[role=group],table,ul,ol,div');
            if(fs) grp=[...fs.querySelectorAll('input[type=checkbox],input[type=radio]')]; }
          const txt=(r)=>{ const l=r.closest('label')||(r.id&&document.querySelector("label[for='"+r.id+"']"));
            let t=l?(l.innerText||''):'';
            if(!t){ let p=r.parentElement; for(let i=0;i<3&&p;i++){ const t2=(p.innerText||'').trim(); if(t2&&t2.length<40){t=t2;break;} p=p.parentElement; } }
            if(!t) t=r.value||''; return norm(t); };
          const toks=s=>norm(s).split(' ').filter(Boolean);
          const its=items.map(norm);
          const checked=[];
          for(const r of grp){
            const t=txt(r); if(!t) continue;
            const tw=toks(t);
            const match=its.some(it=> it===t || tw.includes(it) || toks(it).includes(t) ||
                                       (it.length>=3 && t.length>=3 && (it===t)) );
            if(match){ if(!r.checked){ r.checked=true; try{r.click()}catch(_){}; 
                       r.dispatchEvent(new Event('change',{bubbles:true})); } checked.push(t); }
          }
          return checked;
        }""", items)
    except Exception:
        return []

_LABEL_ABOVE_JS = r"""
(el) => {
  const clean = s => (s||'').replace(/\s+/g,' ').trim();
  const good = t => t && t.length<=220 && /[a-zA-Z]{3}/.test(t) && !/^select\b/i.test(t)
                    && !/^\+?\d/.test(t) && !/drop or select/i.test(t);
  // 1) aria-labelledby
  const lb = el.getAttribute('aria-labelledby');
  if (lb){ const t = lb.split(/\s+/).map(id=>{const e=document.getElementById(id);return e?e.innerText:'';}).join(' ');
           if (good(clean(t))) return clean(t).split('\n')[0]; }
  // 2) walk up; at each level scan preceding siblings (and their last text line)
  let n = el;
  for (let i=0; i<8 && n; i++){
    let p = n.previousElementSibling, hops=0;
    while(p && hops++<8){
      const lines = clean(p.innerText).split('\n').map(clean).filter(Boolean);
      // prefer the LAST short line (label usually sits right above the field)
      for (let k=lines.length-1;k>=0;k--){ if (good(lines[k])) return lines[k]; }
      p = p.previousElementSibling;
    }
    n = n.parentElement;
  }
  return '';
}
"""

def _is_required(label, el):
    """Best-effort: is this form field marked required? Label asterisk / the word
    'required', or an aria-required/required attribute. Used to decide ifreq: fills."""
    lab = label or ""
    if "*" in lab: return True
    if re.search(r"\brequired\b", lab, re.I): return True
    try:
        if (el.get_attribute("aria-required") or "").lower() == "true": return True
        if el.get_attribute("required") is not None: return True
    except Exception:
        pass
    # SLDS/LWC: the required flag and the red * live on the parent component across
    # shadow boundaries. Climb host-by-host like the label lookup does.
    try:
        if el.evaluate(r"""e => {
          let n = e;
          for (let d = 0; d < 6 && n; d++) {
            if (n.getAttribute) {
              if ((n.getAttribute('aria-required')||'').toLowerCase() === 'true') return true;
              if (n.hasAttribute && n.hasAttribute('required')) return true;
            }
            const root = n.getRootNode();
            if (root && root.querySelector &&
                root.querySelector('abbr.slds-required, .slds-required, [class*="required" i]')) return true;
            if (!root || !root.host) break;
            n = root.host;
          }
          return false;
        }"""):
            return True
    except Exception:
        pass
    return False

def _wd_multiselect_pick(page, el, values):
    """Workday multi-select 'prompt' (How Did You Hear About Us?). Menu renders in a
    body portal and is hierarchical: top level is CATEGORIES (Job Board, etc.), the
    real answers are LEAVES (LinkedIn, Indeed...). Typing a leaf name filters to it.
    For each candidate leaf: type to filter, then select via click / Enter /
    ArrowDown+Enter, and VERIFY a selected chip actually shows that leaf. Logs the
    visible options and each attempt. Returns True only on a verified chip."""
    if isinstance(values, str):
        values = [values]
    try:
        scope = el.owner_frame() or page
    except Exception:
        scope = page

    def _field():
        try:
            f = scope.locator('[data-automation-id="formField-source"]').first
            if f.count():
                return f
        except Exception:
            pass
        return None

    def _chip_has(cand):
        """True only if a SELECTED chip in the source field shows this leaf."""
        f = _field()
        if f is None:
            return False
        try:
            chips = f.locator('[data-automation-id="selectedItem"]')
            if chips.count():
                t = (chips.first.inner_text() or "").lower()
                return cand.lower() in t
        except Exception:
            pass
        try:
            t = (f.inner_text() or "").lower()
            return cand.lower() in t and "0 items selected" not in t
        except Exception:
            return False

    def _visible_opts():
        try:
            return scope.eval_on_selector_all(
                "[data-automation-id='promptOption'],[role=option],li[role=option]",
                "els => [...new Set(els.filter(e=>e.offsetParent!==null)"
                ".map(e=>(e.innerText||'').trim()).filter(Boolean))].slice(0,30)") or []
        except Exception:
            return []

    def _search_box():
        for sel in ('input[data-automation-id="searchBox"]',
                    'input[data-automation-id="monikerSearchBox"]',
                    '[data-automation-id="multiselectInputContainer"] input',
                    '[role=combobox] input', 'input[type=text]:not([readonly])'):
            try:
                sb = scope.locator(sel).first
                if sb.count() and sb.is_visible():
                    return sb
            except Exception:
                pass
        return None

    # open
    try: el.click()
    except Exception:
        try: el.evaluate("e=>e.click()")
        except Exception:
            print("    [how-hear] could not open widget"); return False
    page.wait_for_timeout(450)

    import re as _re
    for cand in values:
        sb = _search_box()
        try:
            if sb is not None:
                sb.fill(""); sb.type(cand[:40], delay=15)
            else:
                el.type(cand[:40], delay=15)
        except Exception:
            pass
        page.wait_for_timeout(750)
        opts = _visible_opts()
        # method 1: click the exact leaf promptOption
        clicked = False
        try:
            loc = scope.locator("[data-automation-id='promptOption'],[role=option],li[role=option]").filter(
                has_text=_re.compile(r"^\s*" + _re.escape(cand) + r"\s*$", _re.I))
            if loc.count():
                loc.first.scroll_into_view_if_needed(timeout=800)
                loc.first.click(timeout=1500); clicked = True
        except Exception:
            pass
        if not _chip_has(cand):
            # method 2: keyboard select from the search box
            try:
                tgt = sb if sb is not None else el
                tgt.press("ArrowDown"); page.wait_for_timeout(200); tgt.press("Enter")
            except Exception:
                pass
        ok = _chip_has(cand)
        print(f"    [how-hear] try '{cand}': opts={opts[:8]} clicked={clicked} selected={ok}")
        if ok:
            try: el.evaluate("e=>e.blur&&e.blur()")
            except Exception: pass
            print(f"    [how-hear] selected '{cand}'")
            return True

    print(f"    [how-hear] no verified selection. last visible options: {_visible_opts()[:30]}")
    return False

def _howhear_group_has(el, want):
    """True if el's radio group looks like a 'how did you hear' group AND contains an
    option matching `want`. Lets us pick the preferred source no matter where it sits in
    the group's DOM order (so options before it don't each report 'pick manually')."""
    try:
        return bool(el.evaluate(r"""(e,w)=>{
          w=(w||'').trim().toLowerCase();
          let grp=[];
          const nm=e.getAttribute('name');
          if(nm) grp=[...document.querySelectorAll(`input[type=radio][name="${nm}"]`)];
          if(grp.length<2){ const fs=e.closest('fieldset,[role=radiogroup],[role=group],.form-group,div'); if(fs) grp=[...fs.querySelectorAll('input[type=radio]')]; }
          const txt=(r)=>{ const l=r.closest('label')||(r.id&&document.querySelector(`label[for='${r.id}']`)); let t=l?(l.innerText||''):''; if(!t) t=r.getAttribute('aria-label')||r.value||''; return t.trim().toLowerCase().split('\n')[0]; };
          const labs=grp.map(txt);
          if(labs.length<2) return false;
          const joined=labs.join(' | ');
          const context=/career page|hear about|referral|from a friend|recommendation|job board|social media|in-person event|glassdoor|indeed|linkedin|word of mouth/i.test(joined);
          const hasWant=labs.some(t=>t===w||t.startsWith(w)||(w.length>=5&&t.includes(w)));
          return context && hasWant;
        }""", want))
    except Exception:
        return False

def _click_group_option(el, value):
    """From any option element in a radio/checkbox group, click the option whose
    visible text (label / aria-label / nearby text) matches `value`. Handles native
    input[type=radio] and Workday-style [role=radio] custom controls."""
    try:
        return bool(el.evaluate(r"""(e,v)=>{
          v = (v||'').trim().toLowerCase();
          const grp = e.closest('fieldset,[role=radiogroup],[role=group]') || e.form || document;
          let cands = [...grp.querySelectorAll('input[type=radio],input[type=checkbox],[role=radio],[role=checkbox]')];
          if (e.name) cands = cands.concat([...document.querySelectorAll(`input[name="${e.name}"]`)]);
          cands = [...new Set(cands)];
          const txt = (r)=>{
            const l = r.closest('label') || (r.id && document.querySelector(`label[for='${r.id}']`));
            let t = l ? (l.innerText||'') : '';
            if (!t) t = r.getAttribute('aria-label')||'';
            if (!t && r.parentElement) t = (r.parentElement.innerText||'');
            return t.trim().toLowerCase().split('\n')[0];
          };
          for (const r of cands){
            const t = txt(r);
            if (t===v || t.startsWith(v)){ (r.closest('label')||r).click(); return true; }
          }
          // longer answers (e.g. "LinkedIn") may live inside a broader option label
          // like "Social media (LinkedIn, Instagram, X)" -> contains-match.
          if (v.length>=5){
            for (const r of cands){
              const t = txt(r);
              if (t && t.includes(v)){ (r.closest('label')||r).click(); return true; }
            }
          }
          return false;
        }""", value))
    except Exception:
        return False

def _select_option_el(page, el):
    """Select ONE radio/checkbox option by clicking its LWC wrapper (c-ts-radio /
    c-ts-check-box), climbing shadow hosts to reach it. Clicking the raw inner input
    can leave Salesforce Flow state stale; the wrapper click is what commits."""
    try: el.scroll_into_view_if_needed(timeout=1000)
    except Exception: pass
    try:
        el.evaluate(r"""e => {
          let n = e;
          for (let d = 0; d < 6 && n; d++) {
            if (n.tagName && /^C-TS-(RADIO|CHECK-BOX)$/i.test(n.tagName)) { n.click(); return; }
            const r = n.getRootNode();
            if (!r || !r.host) break;
            n = r.host;
          }
          (e.closest('label') || e).click();
        }""")
    except Exception:
        pass
    page.wait_for_timeout(150)
    try:
        if el.is_checked(): return True
    except Exception:
        pass
    try:
        el.check(timeout=1000); return bool(el.is_checked())
    except Exception:
        try: el.click(); return True
        except Exception: return False


ASHBY_SEL = "input.ashby-application-form-input-autocomplete"

def _ashby_focused(page, sel):
    try:
        return bool(page.evaluate("(s)=>{const a=document.activeElement;"
                                  "return !!(a&&a.matches&&a.matches(s));}", sel))
    except Exception:
        return False

def _ashby_focus(page, sel):
    loc = page.locator(sel).first
    try: loc.wait_for(state="visible", timeout=2000)
    except Exception: return False
    if not _ashby_focused(page, sel):
        try: loc.focus()
        except Exception: pass
        if not _ashby_focused(page, sel):
            try: loc.click(timeout=800)
            except Exception: pass
    return _ashby_focused(page, sel)

def _ashby_type_prefix(page, sel, prefix):
    """Type per char, re-resolving the input each keystroke (Ashby remounts it on every
    change, invalidating any stored handle). Re-focus if focus was lost; native-setter
    fallback if a keystroke didn't land."""
    _ashby_focus(page, sel)
    try:
        page.evaluate("""(s)=>{const e=document.querySelector(s); if(!e)return; e.focus();
          const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
          set.call(e,''); e.dispatchEvent(new Event('input',{bubbles:true}));}""", sel)
    except Exception:
        pass
    for ch in prefix:
        if not _ashby_focused(page, sel):
            _ashby_focus(page, sel)
        try: page.keyboard.type(ch, delay=0)
        except Exception: pass
        page.wait_for_timeout(90)
        try:
            cur = page.evaluate("(s)=>{const e=document.querySelector(s);return e?(e.value||''):'';}", sel)
        except Exception:
            cur = ""
        if ch not in cur:
            try:
                page.evaluate("""(a)=>{const [s,ch]=a; const e=document.querySelector(s); if(!e)return; e.focus();
                  const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
                  set.call(e,(e.value||'')+ch);
                  e.dispatchEvent(new InputEvent('input',{bubbles:true,data:ch,inputType:'insertText'}));}""",
                  [sel, ch])
            except Exception:
                pass
    page.wait_for_timeout(500)

def _ashby_popup_opts(page):
    try:
        return page.evaluate("""() => {
          const pops=[...document.querySelectorAll(
            '[class*="ashby-application-form-input-autocomplete-popup"],[class*="floatingContainer"]'
          )].filter(e=>e.offsetParent!==null);
          const out=[];
          for(const p of pops){ for(const n of p.querySelectorAll('[role=option],div,li,button')){
            const t=(n.innerText||'').trim();
            if(t && t.length<80 && t.toLowerCase()!=='no results') out.push(t); }}
          return [...new Set(out)];
        }""") or []
    except Exception:
        return []

def _ashby_click_option(page, want):
    try:
        return bool(page.evaluate("""(want)=>{
          const wantL=want.toLowerCase();
          const pops=[...document.querySelectorAll(
            '[class*="ashby-application-form-input-autocomplete-popup"],[class*="floatingContainer"]'
          )].filter(e=>e.offsetParent!==null);
          for(const p of pops){
            const n=[...p.querySelectorAll('*')].find(el=>(el.innerText||'').trim().toLowerCase()===wantL);
            if(!n) continue;
            n.scrollIntoView({block:'center'});
            const o={bubbles:true,cancelable:true,view:window};
            n.dispatchEvent(new PointerEvent('pointerdown',o));
            n.dispatchEvent(new MouseEvent('mousedown',o));
            n.dispatchEvent(new PointerEvent('pointerup',o));
            n.dispatchEvent(new MouseEvent('mouseup',o));
            n.dispatchEvent(new MouseEvent('click',o));
            return true;
          }
          return false;
        }""", want))
    except Exception:
        return False

def _ashby_value(page, sel):
    try:
        return page.evaluate("(s)=>{const e=document.querySelector(s);return e?(e.value||''):'';}", sel) or ""
    except Exception:
        return ""

def _typeahead_pick(page, el, value):
    """Ashby autocomplete driver (remount-safe, per Grok). Falls back to a generic
    react-select approach when the Ashby input isn't present."""
    # Ashby path
    try:
        is_ashby = page.locator(ASHBY_SEL).count() > 0
    except Exception:
        is_ashby = False
    if is_ashby:
        for prefix in ["United", "US", "Unit", "Uni", value]:
            _ashby_type_prefix(page, ASHBY_SEL, prefix)
            if not globals().get("_ATA_DBG"):
                try:
                    val = _ashby_value(page, ASHBY_SEL)
                    act = page.evaluate("()=>((document.activeElement||{}).tagName||'')")
                    opts = _ashby_popup_opts(page)
                    print(f"    [ashby-typeahead] prefix={prefix!r} value={val!r} active={act} options={opts[:10]}")
                except Exception:
                    pass
                globals()["_ATA_DBG"] = True
            if _ashby_click_option(page, value):
                page.wait_for_timeout(250)
                try: page.keyboard.press("Tab")
                except Exception: pass
                page.wait_for_timeout(250)
                v = _ashby_value(page, ASHBY_SEL).strip().lower()
                if v and (value.lower() in v or v in value.lower()):
                    print(f"    [commit-dbg] want={value!r} control-shows={_ashby_value(page, ASHBY_SEL)!r}")
                    return True
        print(f"    [commit-dbg] want={value!r} control-shows={_ashby_value(page, ASHBY_SEL)!r}")
        return False
    # generic fallback (non-Ashby typeaheads)
    try:
        el.click(timeout=1500)
    except Exception:
        try: el.evaluate("e=>e.click()")
        except Exception: return False
    page.wait_for_timeout(400)
    try:
        page.keyboard.type(value[:40], delay=45); page.wait_for_timeout(600)
        page.keyboard.press("Enter")
    except Exception:
        pass
    page.wait_for_timeout(300)
    try: el.evaluate("e=>e.blur&&e.blur()")
    except Exception: pass
    return _combo_committed(el, value)

def _combo_committed(el, val):
    """Honest commit check. Ashby keeps the picked value in the INPUT's value AFTER blur
    (typed-but-unpicked text is cleared on blur); still supports react-select single-value
    and native <select> for other ATSs. Menu still open (aria-expanded) => not settled."""
    v = (val or "").strip().lower()
    try:
        if (el.get_attribute("aria-expanded") or "") == "true":
            return False
    except Exception:
        pass
    try:
        shown = el.evaluate(r"""(e)=>{
          if(e.tagName==='SELECT'){const o=e.options[e.selectedIndex];return o?(o.text||''):'';}
          const ph = e.getAttribute && (e.getAttribute('placeholder')||'');
          if(e.tagName==='INPUT'){ const v=(e.value||'').trim(); if(v) return v; }
          const root=e.closest('[class*=control],[class*=select],[class*=Select],[class*=field],[class*=nputContainer]')||e.parentElement;
          if(root){
            const sv=root.querySelector('[class*=singleValue],[class*=single-value],'
              +'[class*=multiValue],[class*=selectedItem],[data-automation-id=selectedItem]');
            if(sv && (sv.innerText||'').trim()) return (sv.innerText||'').trim();
            const inp=root.querySelector('input'); if(inp){const iv=(inp.value||'').trim(); if(iv) return iv;}
          }
          return '';
        }""") or ""
    except Exception:
        shown = ""
    sl = shown.strip().lower()
    if not globals().get("_COMMIT_DBG"):
        try: print(f"    [commit-dbg] want={val!r} control-shows={shown!r}")
        except Exception: pass
        globals()["_COMMIT_DBG"] = True
    if not sl or "start typing" in sl or sl.startswith("select"):
        return False
    return bool(v) and (v[:5] in sl or sl in v or v in sl)


def fill_control(page, el, app, dry, upload, state):
    try:
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        typ = (el.get_attribute("type") or "").lower()
        label = clean_label(el.evaluate(LABEL_JS))
    except Exception:
        return ("skip", "", "unreadable")
    label = (label or "").strip()

    # If the "label" is just a dropdown placeholder ("Select One [Required]"), the real
    # question is a nearby label/legend -> recover it so answer rules can match.
    if (re.match(r"^\s*select\s*(one)?\b", label, re.I) or re.match(r"^\s*select\s*\.\.\.", label, re.I)
            or re.match(r"^\s*(textbox|combobox|listbox|dropdown|choose|option)\s*$", label, re.I)
            or not label):
        try:
            q = clean_label(el.evaluate(QUESTION_JS)) or ""
        except Exception:
            q = ""
        q = (q or "").strip()
        if not q or re.match(r"^\s*select\s*(one)?\b", q, re.I) or len(q) > 180:
            try:
                q2 = clean_label(el.evaluate(_LABEL_ABOVE_JS)) or ""
            except Exception:
                q2 = ""
            if q2 and not re.match(r"^\s*select", q2, re.I):
                q = q2
        if q and not re.match(r"^\s*select\s*(one)?\b", q, re.I):
            label = q

    # NEVER fill password fields via the generic walker — that's the login flow's job.
    if typ == "password" or re.search(r"\bpassword\b", label, re.I):
        return ("skip", label, "password field (login flow only)")

    # Self-identification (CC-305) signature date -> today's date. Handles both the
    # 3-segment (Month/Day/Year) and a single "Date" (MM/DD/YYYY) input.
    if re.match(r"^(month|day|year|date)$", label.strip(), re.I):
        try:
            ptext = page.evaluate("() => (document.body.innerText||'').slice(0,6000).toLowerCase()")
        except Exception:
            ptext = ""
        on_selfid = bool(re.search(r"self-?identif|cc-?305|voluntary self", ptext))
        if not globals().get("_SELFID_DBG"):
            print(f"    [selfid-date] label={label!r} on_selfid={on_selfid}")
            globals()["_SELFID_DBG"] = True
        if on_selfid and not dry:
            import datetime as _dt
            now = _dt.date.today()
            lo = label.strip().lower()
            # Type the WHOLE date as one continuous chain into the first segment we hit
            # (month auto-advances to day to year), like a human. Mark done so the
            # sibling Day/Year segments aren't re-typed.
            if state.get("selfid_date_done"):
                return ("skip", label, "self-id date already entered")
            full = f"{now.month:02d}{now.day:02d}{now.year}"
            try:
                try: el.scroll_into_view_if_needed(timeout=1500)
                except Exception: pass
                try:
                    el.focus()
                except Exception:
                    el.click(force=True, timeout=1500)
                try: el.press("Control+a", timeout=800); el.press("Delete", timeout=800)
                except Exception: pass
                page.keyboard.type(full, delay=90)
                page.wait_for_timeout(200)
                state["selfid_date_done"] = True
                return ("field", label, full)
            except Exception as _e:
                if not globals().get("_SELFID_ERR"):
                    print(f"    [selfid-date] EXCEPTION on {label!r}: {str(_e)[:140]}")
                    globals()["_SELFID_ERR"] = True

    # Date / month / year fields must never receive prose — skip them (Workday date pickers).
    if re.search(r"^(month|year|day|mm|yyyy|from|to)$|date of|/yyyy|mm/yyyy", label, re.I) \
       or (typ in ("date", "month")):
        return ("skip", label, "date field (fill manually)")

    # Search / filter boxes are site navigation, not application fields. Never
    # fill or essay into them (a stale posting can show only a job-board search).
    if typ == "search" or re.search(r"^search\b|\bsearch jobs\b|\bfilter\b|search this site|chatbot|chat (with|input|window|box|button)|\blive chat\b|message (the )?(bot|assistant)|ask (a |your )?question", label, re.I):
        return ("skip", label, "search / chat / filter widget")

    # Work-history free-text fields (Job Title, Company, Employer, Role Description,
    # Location) live inside Workday's work-experience blocks and are owned by the
    # corrector / resume parse — never essay them THERE. Gate to Workday so that a
    # top-level candidate field like Greenhouse's "Location (City)" is NOT skipped
    # and still fills from identity on other ATSs.
    if CURRENT_ATS == "workday" and re.search(r"^(job title|company|employer|organization|position title|role description|description|location)\b", label, re.I):
        return ("skip", label, "workday work-history field (corrector/resume owns it)")

    # resume / cover-letter upload
    if typ == "file":
        try:
            ctext = el.evaluate("e => (e.closest('[class*=field], fieldset, div')||{}).innerText || ''")
        except Exception:
            ctext = ""
        ctext = (ctext or "") + " " + label
        is_cover = bool(re.search(r"cover letter", label, re.I)) or (re.search(r"cover letter", ctext, re.I) and not re.search(r"\bresume\b|\bcv\b|r\xe9sum\xe9", ctext, re.I))
        if is_cover:
            cover = COVER_PATH or os.environ.get("COVER_LETTER_PATH", "")
            if upload and cover:
                if not dry:
                    try: el.set_input_files(cover)
                    except Exception as e: return ("pause", label, f"cover upload failed: {e}")
                return ("cover", label, cover)
            return ("skip", label, "cover letter (no COVER_LETTER_PATH)")
        # resume: upload once only (ATS often renders the field twice).
        # Match explicit resume wording OR common dropzone text ("drag and drop",
        # "choose file"); and as a fallback, a file input with a junk/empty label
        # (e.g. Workable's plain dropzone whose label resolves to an SVG's alt text)
        # is treated as the resume slot too. Never upload into a clearly-other doc
        # slot (transcript, portfolio, writing sample, references).
        resume_hit = re.search(r"resume|cv|r\xe9sum\xe9|curriculum vitae|attach|upload|replace|drag.?and.?drop|drop .*file|choose file", ctext, re.I)
        other_doc = re.search(r"transcript|portfolio|writing sample|work sample|reference letter|certificate|certification doc|photo|headshot|head shot|\bpicture\b|avatar|profile image|profile picture|\bimage\b", ctext, re.I)
        junk_label = (not re.search(r"[A-Za-z]{3,}", label)) or re.search(r"\bsvg\b|not supported", label, re.I)
        if upload and RESUME_PATH and not other_doc and not re.search(r"cover letter", label, re.I) and (resume_hit or junk_label):
            if state.get("resume_done"):
                return ("skip", label, "resume already uploaded")
            if not dry:
                try: el.set_input_files(RESUME_PATH)
                except Exception as e: return ("pause", label, f"upload failed: {e}")
            state["resume_done"] = True
            return ("resume", label, RESUME_PATH)
        return ("skip", label, "file field")

    if not label:
        return ("pause", "", "no label")

    # A label needs at least one real word (>=3 letters) to be a meaningful question.
    # Rejects junk like "(+1)", "*", "Select" so it doesn't get an essay.
    if not re.search(r"[A-Za-z]{3,}", label) and typ not in ("radio", "checkbox"):
        return ("skip", label, "no meaningful label")

    maxlen = None
    try:
        ml = el.get_attribute("maxlength")
        if ml and ml.isdigit(): maxlen = int(ml)
    except Exception:
        pass

    # radios/checkboxes: never type free text into these.
    if typ in ("radio", "checkbox"):
        # per-job locked answer, matched against the GROUP question + this option
        try:
            qtext = el.evaluate(QUESTION_JS) or ""
        except Exception:
            qtext = ""
        ov = job_override(qtext + " || " + label)
        if ov is not None:
            # only click the option that matches the locked value; skip the others
            if _norm(label) == _norm(ov) or _norm(label).startswith(_norm(ov)) or _norm(ov) in _norm(label):
                if not dry:
                    try: el.check()
                    except Exception:
                        try: el.click()
                        except Exception: pass
                return ("locked", label, ov)
            return ("skip", label, "not the locked option")
        # multi-select from profile: "which languages / cloud providers have you used?"
        # -> check only the options you actually have (never Go/Ruby), once per group.
        _ms = _profile_multiselect(el, qtext)
        if _ms is not None:
            _gk = el.get_attribute("name") or qtext or label
            if state.get("radio_done::" + _gk):
                return ("skip", label, "multi-select already handled")
            if not dry:
                state["radio_done::" + _gk] = True
            if _ms:
                return ("field", (qtext or label)[:40], "checked: " + ", ".join(_ms[:8]))
            return ("skip", (qtext or label)[:40], "none of your items are offered")
        # auto-select the preferred "how did you learn about this job?" source
        if typ == "radio" and _norm(label) == _norm(HOW_HEAR):
            if not dry:
                try: el.check()
                except Exception:
                    try: el.click()
                    except Exception: pass
            _gk = (el.get_attribute("name") or qtext or "howhear")
            state["radio_done::" + _gk] = True   # sibling options -> skip, not "choice"
            return ("select", label, "auto: how-did-you-hear")
        # RADIOS ONLY: single-choice groups lose the question in the per-option label,
        # so resolve on the group question (qtext) first (e.g. "previously employed at
        # <co>?" -> No via fields.yaml), then the option label. Click the matching
        # option ANYWHERE in the group, once per group. Checkboxes are opt-in
        # (e.g. "I have a preferred name") -> never auto-check; leave for the human.
        if typ == "radio":
            gk = (el.get_attribute("name") or qtext or label)
            if gk and state.get("radio_done::" + gk):
                return ("skip", label, "group already answered")
            # how-did-you-hear group: pick the preferred source now, from ANY option in
            # the group, so options ordered before it don't each fall to "pick manually".
            if _howhear_group_has(el, HOW_HEAR):
                if dry:
                    state["radio_done::" + gk] = True
                    return ("select", HOW_HEAR, "auto: how-did-you-hear (group)")
                if _click_group_option(el, HOW_HEAR):
                    state["radio_done::" + gk] = True
                    return ("select", HOW_HEAR, "auto: how-did-you-hear (group)")
            # EEO self-ID group (veteran/gender/race/disability/hispanic): resolve on the
            # group's OPTION texts and pick the profile value (labels are often the heading).
            _eeoval = _eeo_group_answer(el)
            if _eeoval:
                if dry:
                    state["radio_done::" + gk] = True
                    return ("field", "EEO self-ID", _eeoval)
                if _click_group_option(el, _eeoval):
                    state["radio_done::" + gk] = True
                    return ("field", "EEO self-ID", _eeoval)
            ans = ""
            for src in (qtext, label):
                if not src:
                    continue
                rr = apply.answer(field=src)
                if rr.get("kind") == "field" and rr.get("text") and len(rr["text"]) <= 25:
                    ans = rr["text"]; break
            if ans:
                _na, _nl = _norm(ans), _norm(label)
                _match = (_nl == _na or (_na and _nl.startswith(_na))
                          or (len(_na) >= 4 and _na in _nl))
                if _match:
                    # THIS option is the answer: click its wrapper directly (no
                    # cross-shadow group search needed - avoids the SLDS blindness).
                    if dry:
                        state["radio_done::" + gk] = True
                        return ("field", (qtext or label)[:40], ans)
                    if _select_option_el(page, el):
                        state["radio_done::" + gk] = True
                        return ("field", (qtext or label)[:40], ans)
                    return ("choice", qtext or label, f"couldn't click '{ans}'")
                # answer known but not this option: try a whole-group click (non-shadow
                # forms), else skip and wait for the matching option (do NOT mark done).
                if _click_group_option(el, ans):
                    state["radio_done::" + gk] = True
                    return ("field", (qtext or label)[:40], ans)
                return ("skip", label, f"waiting for '{ans}' in group")
            # how-did-you-hear as a RADIO GROUP (e.g. Paylocity): the source lives in
            # each option's `value`, not in a shared question label. Detect by value and
            # pick the best available source once per group.
            try:
                _rv = _norm(el.get_attribute("value") or "")
            except Exception:
                _rv = ""
            _SRC_STRONG = ("online job board", "company website", "friend or family member",
                           "current employee", "career fair", "employee referral", "job board",
                           "recruiter", "social media", "linkedin", "indeed", "job fair")
            if _rv in _SRC_STRONG:
                if state.get("radio_done::" + gk):
                    return ("skip", label, "how-hear source group already answered")
                _SRC_PREF = ["online job board", "job board", "linkedin", "indeed",
                             "company website", "social media", "recruiter", "other"]
                if dry:
                    state["radio_done::" + gk] = True
                    return ("select", "How did you hear", "auto: source -> Online Job Board")
                _picked = _pick_source_radio(page, el, _SRC_PREF)
                state["radio_done::" + gk] = True
                if _picked:
                    return ("field", "How did you hear", _picked)
                return ("choice", "How did you hear", "source: pick failed")
        # Disability self-identification (CC-305): tick the option matching the
        # profile answer (from profile.yaml); leave the other options unchecked.
        if typ == "checkbox" and re.search(r"disabilit|do not want to answer|have a disability|do not have a disability", ((qtext or "") + " " + label), re.I):
            lab_l = label.strip().lower()
            is_no = lab_l.startswith("no,") or "do not have a disability" in lab_l
            is_yes = lab_l.startswith("yes,")
            no_answer = "i do not want to answer" in lab_l or "don't wish" in lab_l
            want = (_eeo_get("disability_short") or "No").strip().lower()
            want_no = want.startswith("n")
            if (want_no and is_no) or ((not want_no) and is_yes):
                if not dry:
                    try: el.evaluate("e=>{ if(!e.checked) e.click(); }")
                    except Exception:
                        try: el.check()
                        except Exception: pass
                return ("field", label[:40], "disability self-ID")
            return ("skip", label, "disability option (not selected)")

        # Required consent / terms / acknowledgement checkboxes -> tick them (a blocked
        # application can't proceed without them; this is the user's own submission).
        if typ == "checkbox":
            ctext = ((qtext or "") + " " + label).lower()
            # work-arrangement checkbox group ("What work settings are you open to?"
            # Hybrid / On-site / Remote). Remote-only profile -> check Remote, skip others.
            try:
                _remote_only = bool(apply.dotted(apply.load("profile.yaml"), "eligibility.remote_only"))
            except Exception:
                _remote_only = False
            if _remote_only and re.search(r"work setting|work arrangement|open to\b.*(setting|work)|which .*settings", ctext):
                _ll = label.strip().lower()
                if re.search(r"\bremote\b", _ll):
                    if not dry:
                        try: el.evaluate("e=>{ if(!e.checked) e.click(); }")
                        except Exception: pass
                        try: _ok = bool(el.is_checked())
                        except Exception: _ok = False
                        if not _ok:
                            _select_option_el(page, el)   # wrapper-click fallback (SLDS)
                    return ("field", (qtext or label)[:40], "Remote")
                return ("skip", label, "work setting (not Remote)")
            # address-verification override (iCIMS: "Use this address anyway? Yes") - the
            # address IS valid; their geo-DB just can't verify small towns. Tick to proceed.
            if re.search(r"use this address anyway|use address anyway", ctext):
                if not dry:
                    try: el.evaluate("e=>{ if(!e.checked) e.click(); }")
                    except Exception:
                        try: el.check()
                        except Exception: pass
                return ("field", label, "address override checked")
            if _is_required(label, el) and re.search(
                    r"consent|i agree|i acknowledge|terms and conditions|accept.*terms|"
                    r"certif|i have read|authorize|electronic signature|e-?sign|"
                    r"equivalent to a (handwritten )?signature|by checking.*signature", ctext):
                if not dry:
                    try:
                        el.evaluate("e=>{ if(!e.checked) e.click(); }")
                    except Exception:
                        try: el.check()
                        except Exception: pass
                return ("field", label, "consent checked")
            # otherwise: unchecked is valid. Only flag when required.
            if not _is_required(label, el):
                return ("skip", label, "optional checkbox - leave unchecked")
        return ("choice", label, "pick manually")

    # Detect combobox-like control. react-select renders a dropdown as a TEXT INPUT
    # with role=combobox (tag!='select'), so these must be caught or they get essayed.
    try:
        role = (el.get_attribute("role") or "").lower()
        haspopup = (el.get_attribute("aria-haspopup") or "").lower()
        autocomp = (el.get_attribute("aria-autocomplete") or "").lower()
        readonly = el.get_attribute("readonly") is not None
    except Exception:
        role = haspopup = autocomp = ""; readonly = False
    is_combo = (tag == "select" or role == "combobox"
                or haspopup in ("listbox", "true", "menu") or autocomp == "list" or readonly)

    # per-job locked answer for a dropdown or text field (matched on the label)
    ov = job_override(label)
    if ov is not None:
        if not dry:
            set_value(page, el, tag, typ, ov)
        return ("locked", label, ov)

    # how-did-you-hear: Workday renders this as a multi-select "prompt" whose control
    # is a PLAIN TEXT input (not role=combobox), so a text fill types the word but
    # selects nothing ("0 items selected"). Trigger the widget picker on the LABEL for
    # any non-<select>, non-radio control, BEFORE the identity text-fill. Native
    # <select> how-hear still fills fine via set_value below. Dumps the menu on a miss.
    if (tag != "select" and typ not in ("radio", "checkbox")
            and re.search(r"how did you (hear|learn)|where did you (hear|learn)|referral source|how did you find (out )?about", label, re.I)):
        if dry:
            return ("select", label, f"auto: how-hear -> {HOW_HEAR}")
        if _wd_multiselect_pick(page, el, [HOW_HEAR] + HOW_HEAR_FALLBACKS):
            return ("select", label, "auto: how-hear (verified)")
        return ("choice", label, "how-hear: pick failed (menu dumped to log)")

    # how-did-you-hear as a native <select> (iCIMS etc.): the preferred source may not be an
    # option, so try HOW_HEAR then the fallbacks against the select's actual options.
    if (tag == "select"
            and re.search(r"how did you (hear|learn)|where did you (hear|learn)|referral source|how did you find (out )?about", label, re.I)):
        if dry:
            return ("select", label, f"auto: how-hear -> {HOW_HEAR}")
        for cand in [HOW_HEAR] + HOW_HEAR_FALLBACKS:
            if set_value(page, el, tag, typ, cand):
                return ("select", label, f"auto: how-hear -> {cand}")
        return ("choice", label, "how-hear: no matching option in the dropdown")

    # Location disambiguation: a "Location"/"Country" field asking for the COUNTRY of
    # residence must get the country, not the city. BUT many eligibility questions merely
    # MENTION "country" ("...legal right to work in the country where...") - those are
    # sponsorship/visa/authorization Yes/No questions, NOT country fields, so exclude them.
    if (re.search(r"\blocation\b|\bcountry\b|residing|country of residence", label, re.I)
            and not re.search(r"sponsor|visa|work authoriz|authori[sz]ation|legal right|"
                              r"eligib|right to work|require sponsorship|now or in the future|"
                              r"citizen|clearance", label, re.I)):
        try:
            _lc = el.evaluate(r"""e=>{
              let n=e, best='';
              for(let i=0;i<5&&n;i++){ n=n.parentElement; if(!n) break;
                const t=(n.innerText||'').trim();
                if(t && t.length<300){ best=t; if(/country|residing/i.test(t)) break; }
              }
              return best;
            }""") or ""
        except Exception:
            _lc = ""
        _lc = (_lc + " " + label).lower()
        if re.search(r"\bcountry\b|residing in|country of residence", _lc) and not re.search(r"\bcity\b|city/|/city", _lc):
            if dry:
                return ("field", label, "United States")
            # react-widgets dropdownlist (Paylocity): list-only, never type into it.
            # (_rw_pick is idempotent - a node whose widget already shows the value just
            # returns True - so two country fields both fill and duplicate nodes are safe.)
            if _is_rw_dropdown(el):
                if _rw_pick(page, el, "United States"):
                    return ("field", label, "United States")
                return ("skip", label, "country handled on another node")
            # Classify the control. Only a real typeahead/autocomplete gets the typing
            # driver; a PLAIN TEXT input must be replace-filled (el.fill), never typed into
            # - keyboard typing appends, so across the form's re-render passes a plain
            # "specify your country" box becomes "United StatesUnited StatesUnited States".
            try:
                _role = (el.get_attribute("role") or "").lower()
                _autoc = (el.get_attribute("aria-autocomplete") or "").lower()
                _pop = (el.get_attribute("aria-haspopup") or "").lower()
                _editable = el.evaluate(
                    "e=>{const t=e.tagName.toLowerCase(); return (t==='input'||t==='textarea') && !e.disabled && !e.readOnly;}")
            except Exception:
                _role = _autoc = _pop = ""; _editable = True
            _is_typeahead = (_role == "combobox" or _autoc == "list" or _pop in ("listbox", "true", "menu"))
            if _editable and _is_typeahead and _typeahead_pick(page, el, "United States"):
                return ("field", label, "United States")
            # plain text input OR non-editable combobox: set_value replaces (idempotent
            # across passes) - fill() for text, open+pick for a div/span combobox.
            if set_value(page, el, tag, typ, "United States"):
                return ("field", label, "United States")
            return ("skip", label, "country handled on another node")

    # ---- Ordered work-history fields (repeated once per block, chronological) ----
    # Forms like Paylocity reuse the same labels for every work-history block. Fill each
    # from work_history.yaml IN ORDER, tracked by a per-field-type counter. Company/
    # Position use ordered values so blocks 2..N are NOT overwritten with the current job.
    _WH = load_work_history() or []
    def _wh_take(field_key, ctr_key):
        i = state.get(ctr_key, 0)
        state[ctr_key] = i + 1
        return (_WH[i].get(field_key, "") if i < len(_WH) else "")

    if re.search(r"supervisor.?s?\s*name|name of (your )?supervisor|^manager.?s?\s*name|reference name", label, re.I) \
       and not re.search(r"phone|email|title|position", label, re.I):
        val = _wh_take("supervisor", "wh_sup")
        if val and not dry:
            ok = set_value(page, el, tag, typ, val)
            return ("field", label, val) if ok else ("pause", label, f"couldn't set '{val}'")
        if val:
            return ("field", label, val)
    if re.search(r"^company name|^employer( name)?\b|^company \(required\)|^company\*?$|^company name", label, re.I) \
       and not re.search(r"phone|email|address|url|website", label, re.I):
        val = _wh_take("company", "wh_co") or (dotted_profile("identity.current_company") if False else "")
        if val and not dry:
            ok = set_value(page, el, tag, typ, val)
            return ("field", label, val) if ok else ("pause", label, f"couldn't set '{val}'")
        if val:
            return ("field", label, val)
    if re.search(r"^position\b|^job title\b|^title\b|position title", label, re.I) \
       and not re.search(r"phone|email|are you|do you", label, re.I):
        val = _wh_take("title", "wh_title")
        if val and not dry:
            ok = set_value(page, el, tag, typ, val)
            return ("field", label, val) if ok else ("pause", label, f"couldn't set '{val}'")
        if val:
            return ("field", label, val)
    if re.search(r"^start date\b|employment start|date started|^from date\b", label, re.I) \
       and not re.search(r"available|earliest|when can you", label, re.I):
        val = _wh_take("start", "wh_start")
        if val:
            if not dry:
                ok = set_value(page, el, tag, typ, val)
                if not ok:
                    return ("pause", label, f"couldn't set start '{val}'")
            return ("field", label, val)
        return ("skip", label, "no start date in history")
    if re.search(r"^end date\b|employment end|date ended|^to date\b", label, re.I):
        val = _wh_take("end", "wh_end")
        if val:
            if not dry:
                ok = set_value(page, el, tag, typ, val)
                if not ok:
                    return ("pause", label, f"couldn't set end '{val}'")
            return ("field", label, val)
        return ("skip", label, "current job - no end date")
    if re.search(r"^responsibilities|job responsibilities|^duties\b|responsibilities & achievements|description of duties|job description|role description", label, re.I):
        val = _wh_take("description", "wh_desc")
        if val:
            val = re.sub(r"\s+", " ", val).strip()
            # respect a maxlength if the field has one (truncate on a word boundary)
            try:
                ml = el.get_attribute("maxlength")
                ml = int(ml) if ml and ml.isdigit() else 0
            except Exception:
                ml = 0
            if ml and len(val) > ml:
                cut = val[:ml]
                if " " in cut[-40:]:
                    cut = cut[:cut.rfind(" ")]
                val = cut.rstrip(" ,;") + "."
            if not dry:
                ok = set_value(page, el, tag, typ, val)
                if not ok:
                    return ("pause", label, "couldn't set responsibilities")
            return ("field", label, val)
        return ("skip", label, "no description in history")

    # Open-ended EXPERIENCE essay? Detected by QUESTION CONTENT only - never by "is a
    # textarea" (identity fields like LinkedIn/salary/contact are often textareas too, and
    # the model will FABRICATE if handed one). Company-interest / how-hear keep their own
    # paths below; this is strictly "describe / give an example / what challenges" prompts.
    _essay_q = (len(label) > 40 and bool(re.search(
        r"describe|provide (an?|one) example|give (an?|one) example|for example|example of|"
        r"explain how|tell us about|walk (us|me) through|what was your role|what challenges|"
        r"how did you (solve|identify|approach|ensure|handle|resolve)|please share|"
        r"describe your experience|describe a (project|time|situation)", label, re.I)))

    def _compose_here():
        # LOCAL-AI: compose a grounded answer to THIS specific question (work history +
        # closest template as fact bank). Templates are source facts, not the authority.
        rr = apply.compose(label, max_chars=maxlen, app=app)
        if rr.get("kind") == "answer" and rr.get("text"):
            val = rr["text"]
            if not dry:
                if len(val) > (maxlen or 10**9):
                    val = val[:maxlen]
                if not set_value(page, el, tag, typ, val):
                    return ("pause", label, "couldn't set answer")
            gaps = rr.get("gaps") or []
            if gaps:
                # filled with an honest answer, but the question probes tech the candidate
                # has no evidence of - flag for the human to verify before submitting.
                return ("review", label, "possible gap (" + ", ".join(gaps) + ") - verify: " + val[:60])
            return ("answer:" + (rr.get("method") or "compose"), label, val)
        return ("pause", label, rr.get("text", "no match"))

    # 1) identity field?  (deterministic rules win for real identity fields)
    r = apply.answer(field=label, required=_is_required(label, el))
    if re.search(r"sponsor", label, re.I) and not globals().get("_SPON_DBG"):
        print(f"    [spon-dbg] label={label!r}")
        print(f"    [spon-dbg] resolved kind={r.get('kind')!r} text={r.get('text')!r}")
        globals()["_SPON_DBG"] = True
    if r.get("kind") == "field":
        val = r["text"]
        # mis-route guard: an essay question that resolved to a SHORT scalar (e.g. years
        # "14+") dropped into a paragraph box -> compose the real answer instead.
        if _essay_q and tag == "textarea" and len(val) < 40:
            return _compose_here()
        if not dry:
            if (tag == "input" and typ not in ("radio", "checkbox", "file")
                    and re.search(r"phone|mobile|cell", label, re.I)
                    and len(re.sub(r"\D", "", val)) >= 7):
                ok = _fill_phone(page, el, val)
                if not ok:
                    return ("pause", label, f"phone didn't stick ('{val}')")
            elif (tag == "input" and typ not in ("radio", "checkbox", "file")
                    and re.search(r"\baddress\b|full address|street address|\blocation\b", label, re.I)):
                if state.get("address_done"):
                    return ("skip", label, "address already filled")
                # An "Address" line next to separate City/State/Zip fields wants the STREET,
                # not city/state/country (which fails geo-verification and reads oddly). Use
                # location_full only for a standalone single-box location field.
                if re.search(r"\baddress\b|street", label, re.I) and _has_separate_city_state(page):
                    _street = apply.dotted(apply.load("profile.yaml"), "identity.street") or ""
                    if _street:
                        val = _street
                elif "," not in val:
                    # a plain location field with a non-comma value: fall through to set_value
                    return ("field", label, val) if (dry or set_value(page, el, tag, typ, val)) else ("pause", label, f"couldn't set '{val}'")
                ok = _fill_address(page, el, val)   # geocode widgets append per input event
                state["address_done"] = True
                state["address_clean"] = val
                if not ok:
                    return ("pause", label, f"couldn't set '{val}'")
            else:
                ok = set_value(page, el, tag, typ, val)
                if not ok:
                    return ("pause", label, f"couldn't set '{val}' (dropdown option not found?)")
        return ("field", label, val)
    # identity gave nothing: an open-ended experience essay -> compose it locally
    if _essay_q:
        return _compose_here()
    # explicit skip rule (e.g. Website) -> leave blank, do NOT ask the LLM.
    # If a long essay-like value is already there (from an earlier mis-fill), clear it.
    if r.get("kind", "").startswith("pause:"):
        if not dry and tag in ("input", "textarea") and typ not in ("checkbox", "radio", "file"):
            try:
                cur = el.input_value(timeout=800)
                if cur and len(cur) > 40:
                    el.fill("")
            except Exception:
                pass
        return ("blank", label, r["kind"].split(":", 1)[1])

    # how-did-you-hear rendered as a dropdown -> auto-pick the preferred source
    if is_combo and re.search(r"how did you (hear|learn)|where did you (hear|learn)|referral source", label, re.I):
        if not dry:
            _open_and_pick(page, el, HOW_HEAR)
        return ("select", label, "auto: how-did-you-hear")

    # any other dropdown/combobox -> leave for the human, never write an essay
    if is_combo:
        return ("choice", label, "pick manually")

    # 2) treat as a question - but ONLY if the label actually reads like one. Short, generic
    # labels ("Login", "Number", "Type", "Please specify") are fields, not essay prompts;
    # essaying them drops a company-interest paragraph into a username or phone box.
    _looks_like_question = ("?" in label or len(label.split()) >= 5
                            or re.search(r"describe|why|how|what|tell us|experience|explain", label, re.I))
    if not _looks_like_question:
        return ("choice", label, "short/generic label - not an essay prompt")
    r = apply.answer(question=label, app=app, max_chars=maxlen)
    if r.get("kind") == "answer":
        val = r["text"]
        if not dry:
            ok = set_value(page, el, tag, typ, val)
            if not ok:
                return ("pause", label, "couldn't set answer")
        return ("answer:" + r.get("intent", "?"), label, val)
    return ("pause", label, r.get("text", "no match"))

def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()

def _norm_tight(s):
    """Alphanumeric-only, lowercase: 'U.S. Citizen' -> 'uscitizen', 'US citizen' -> 'uscitizen'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def clean_label(s):
    """Strip react-select's 'Select...' placeholder that leaks into the accessible
    name, so a combobox and its duplicate resolve to the same label for de-duping."""
    s = (s or "").strip()
    s = re.sub(r"\s*select\s*\.\.\.\s*$", "", s, flags=re.I)
    s = re.sub(r"\s*select\s*$", "", s, flags=re.I)
    return s.strip()

def _visible(o):
    try: return o.is_visible()
    except Exception: return False

def _pick_option(scope, value):
    """Click the visible listbox option matching value: exact, then containment,
    then a looser match on the first comma-chunk (e.g. 'Example City'). `scope` is the
    combobox's own frame (or the page) so iframe-hosted menus are found."""
    opts = [o for o in scope.query_selector_all(
                "[role=option], .select__option, li[role=option], [class*='option'], "
                "[data-automation-id='promptOption'], [data-automation-id*='promptOption'], "
                "[data-automation-id='menuItem'] , ul[role=listbox] li")
            if _visible(o)]
    tgt = _norm(value)
    def click(o):
        try: o.click(); return True
        except Exception:
            try: o.evaluate("e=>e.click()"); return True
            except Exception: return False
    for o in opts:  # exact
        if _norm(o.inner_text()) == tgt and click(o):
            return True
    # US preference: when several options contain the value (e.g. "Canada -- Eastern
    # Time" vs "United States -- Eastern Time"), pick the US one (user is US-based).
    _US = ("united states", "u.s.", "u.s ", "(us", "us &", "us/", "america/", "usa")
    for o in opts:
        t = _norm(o.inner_text())
        if tgt and tgt in t and any(u in t for u in _US) and click(o):
            return True
    for o in opts:  # containment either direction
        t = _norm(o.inner_text())
        if tgt and (tgt in t or t in tgt) and click(o):
            return True
    tvt = _norm_tight(value)  # punctuation/space-insensitive: "U.S. Citizen" ~ "US citizen"
    if len(tvt) >= 4:
        for o in opts:
            ot = _norm_tight(o.inner_text())
            if (ot == tvt or tvt in ot or ot in tvt) and click(o):
                return True
    chunk = _norm(value.split(",")[0])  # e.g. "example city"
    if len(chunk) >= 3:
        for o in opts:
            if chunk in _norm(o.inner_text()) and click(o):
                return True
    return False

def _is_rw_dropdown(el):
    """True if el is (or sits within) a react-widgets dropdownlist (Paylocity)."""
    try:
        return bool(el.evaluate(
            "e=>{let n=e;for(let i=0;i<5&&n;i++){if(n.classList&&n.classList.contains('rw-dropdownlist'))return true;n=n.parentElement;}return false;}"))
    except Exception:
        return False

def _rw_shown(el):
    """Current displayed value text of a react-widgets dropdownlist."""
    try:
        return (el.evaluate(
            "e=>{let n=e;for(let i=0;i<5&&n;i++){if(n.classList&&n.classList.contains('rw-dropdownlist'))break;n=n.parentElement;}"
            "if(!n)return'';const inp=n.querySelector('.rw-input,.rw-dropdownlist-input');"
            "return((inp?inp.textContent:n.textContent)||'').trim();}") or "").strip()
    except Exception:
        return ""

def _rw_pick(page, el, value):
    """Fill a react-widgets dropdownlist by opening it and clicking the matching option.
    Never types into it (typing corrupts these list-only widgets). Returns True when the
    control's displayed value matches `value` (including when it already did)."""
    tgt = _norm(value)
    def ok():
        s = _norm(_rw_shown(el))
        return bool(s) and (tgt in s or s in tgt or (len(tgt) >= 4 and tgt[:6] in s))
    if ok():
        return True
    try:
        root = el.evaluate_handle(
            "e=>{let n=e;for(let i=0;i<5&&n;i++){if(n.classList&&n.classList.contains('rw-dropdownlist'))return n;n=n.parentElement;}return e;}"
        ).as_element()
    except Exception:
        root = el
    for _ in range(2):
        try: root.click(timeout=1500)
        except Exception:
            try: root.evaluate("e=>e.click()")
            except Exception: pass
        page.wait_for_timeout(350)
        if _pick_option(page, value):
            page.wait_for_timeout(200)
            if ok():
                return True
        try: root.evaluate("e=>e.blur&&e.blur()")
        except Exception: pass
        page.wait_for_timeout(150)
    return ok()

def _pick_source_radio(page, el, prefs):
    """Click the radio in this group whose value best matches a preferred how-hear
    source. Returns the chosen value text, or '' on failure."""
    name = ""
    try: name = el.get_attribute("name") or ""
    except Exception: name = ""
    radios = []
    if name:
        try: radios = page.query_selector_all("input[type=radio][name=%s]" % json.dumps(name))
        except Exception: radios = []
    if not radios:
        radios = [r for r in page.query_selector_all("input[type=radio]") if _visible(r)]
    def rawval(r):
        try: return (r.get_attribute("value") or "").strip()
        except Exception: return ""
    for p in prefs:
        for r in radios:
            rv = _norm(rawval(r))
            if rv == p or (len(p) >= 4 and p in rv):
                try: r.evaluate("e=>e.click()")
                except Exception:
                    try: r.check()
                    except Exception:
                        try: r.click()
                        except Exception: continue
                return rawval(r) or p
    return ""

def _phone_visible_value(page, el, digits):
    """Truth check for a phone fill: blur, wait for React to re-render, then read the
    VISIBLE phone input(s) - by label and by proximity - and require the digits to have
    survived. el.input_value() alone lies (reads the property before the mask resets it)."""
    try: el.evaluate("e=>e.blur&&e.blur()")
    except Exception: pass
    page.wait_for_timeout(300)
    # PRIMARY truth: the element handle itself, read post-blur. The label-proximity
    # search below finds nothing on forms where the input isn't a sibling of the
    # phone label (e.g. TEKsystems generic), which falsely fails an OK digits-only fill.
    try:
        selfd = el.evaluate("e => (e.value||'').replace(/\\D/g,'')")
        if selfd and selfd == digits:
            return {"ok": True, "value": el.evaluate("e=>e.value"), "via": "self"}
    except Exception:
        pass
    try:
        return page.evaluate(r"""(want) => {
          const labs = [...document.querySelectorAll('label,div,span')]
            .filter(n => /telephone|phone|mobile|cell/i.test((n.innerText||'').slice(0,80)));
          const ins = [];
          for (const n of labs) if (n.parentElement) ins.push(...n.parentElement.querySelectorAll('input'));
          const seen = new Set();
          for (const e of ins) {
            if (seen.has(e) || e.type === 'hidden' || e.offsetParent === null) continue;
            seen.add(e);
            const d = (e.value||'').replace(/\D/g,'');
            if (d && d === want) return {ok:true, value:e.value, cls:e.className, type:e.type};
          }
          return {ok:false, value:'', count:seen.size};
        }""", digits)
    except Exception as ex:
        return {"ok": False, "err": str(ex)}

def _fill_phone(page, el, val):
    """Fill a masked/controlled phone input (Paylocity). DIGITS ONLY - punctuation empties
    a mask. Verify against the repainted visible node after blur, never el.input_value()."""
    raw = re.sub(r"\D", "", val or "")
    if raw.startswith("1") and len(raw) == 11:
        raw = raw[1:]
    digits = raw[:10]
    if len(digits) < 10:
        return False

    def _stuck():
        r = _phone_visible_value(page, el, digits)
        if not globals().get("_PHONE_DBG"):
            try: print(f"    [phone-verify] {r}")
            except Exception: pass
        return bool(r and r.get("ok"))

    # locate the live target (fall back to the given handle)
    target = el

    # LWC / masked tel (Salesforce slds-input type=tel, maxlength<=~14): the formatter
    # owns input and only trusts REAL keypresses. Type digits one key at a time (slow
    # enough that the re-render on each keystroke keeps up), Tab to commit, self-read.
    # NO parenthesized fallback - a dash mask (maxlength=12) shreds "(304) 553-3183".
    def _slow_type():
        try: target.click(timeout=1000)
        except Exception:
            try: target.evaluate("e=>e.focus()")
            except Exception: pass
        try:
            page.keyboard.press("Control+A"); page.keyboard.press("Delete")
        except Exception: pass
        page.wait_for_timeout(200)
        for ch in digits:
            try: page.keyboard.press(ch)
            except Exception: pass
            page.wait_for_timeout(90)   # 80-120ms; 50 is too fast for this mask
        try: page.keyboard.press("Tab")
        except Exception: pass
        page.wait_for_timeout(300)

    _slow_type()
    if _stuck(): return True
    _slow_type()                       # one retry, same way
    if _stuck(): return True

    # fallback: fill the already-masked string (exactly maxlength=12, no parens)
    try:
        formatted = "%s-%s-%s" % (digits[:3], digits[3:6], digits[6:])
        target.fill(formatted)
        page.keyboard.press("Tab"); page.wait_for_timeout(300)
        if _stuck(): return True
    except Exception:
        pass

    globals()["_PHONE_DBG"] = True
    return _stuck()

def _reassert_phones(page, phone):
    """Paylocity clears a filled phone whenever a LATER field triggers a form re-render,
    because our typed value never lands in React state. Fix: run as the FINAL action -
    push each personal phone into React state (native setter + InputEvent, which React's
    onChange reads) AND type it, then settle over a couple of rounds so filling Home
    doesn't wipe Mobile. Company/supervisor phones are left alone."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    digits = digits[:10]
    if len(digits) < 10:
        return 0

    def _candidates():
        out = []
        for e in page.query_selector_all("input"):
            try:
                if not _visible(e):
                    continue
                info = (e.evaluate(
                    "x=>((x.getAttribute('data-for')||'')+' '+((x.labels&&x.labels[0])?x.labels[0].innerText:'')+' '+(x.id||''))") or "")
            except Exception:
                continue
            il = info.lower()
            if not re.search(r"mobile|home phone|cell|phone", il):
                continue
            if re.search(r"company|supervisor|reference|employer|work", il):
                continue
            out.append(e)
        return out

    def _set_react(e):
        try:
            e.click(timeout=800)
            e.press("Control+A"); e.press("Delete")
        except Exception:
            pass
        # trusted keystrokes (fires React onChange the normal way)
        try:
            e.type(digits, delay=40)
        except Exception:
            pass
        # belt-and-suspenders: force the value into React state
        try:
            e.evaluate(r"""(el,v)=>{
              const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
              set.call(el,'');
              el.dispatchEvent(new Event('input',{bubbles:true}));
              set.call(el,v);
              el.dispatchEvent(new InputEvent('input',{bubbles:true,data:v,inputType:'insertText'}));
              el.dispatchEvent(new Event('change',{bubbles:true}));
              el.dispatchEvent(new Event('blur',{bubbles:true}));
            }""", digits)
        except Exception:
            pass

    cands = _candidates()
    if not cands:
        return 0
    # settle: fill all, then re-fill any that got wiped by a sibling's render
    for _ in range(3):
        for e in cands:
            try:
                cur = re.sub(r"\D", "", e.input_value(timeout=500) or "")
            except Exception:
                cur = ""
            if cur != digits:
                _set_react(e)
        page.wait_for_timeout(250)
        allok = True
        for e in cands:
            try:
                cur = re.sub(r"\D", "", e.input_value(timeout=500) or "")
            except Exception:
                cur = ""
            if cur != digits:
                allok = False
        if allok:
            break
    ok = 0
    for e in cands:
        try:
            cur = re.sub(r"\D", "", e.input_value(timeout=500) or "")
        except Exception:
            cur = ""
        if cur == digits:
            ok += 1
    print(f"    [phone-reassert] {ok}/{len(cands)} personal phone field(s) hold {digits}")
    return ok

def _cleanup_address(page, clean):
    """Some address fields (Workable) APPEND a geocoded copy on every input event, so the
    value ends up duplicated. As a final step, force the clean value straight onto the
    input via the native setter and fire only 'blur' (never 'input', which would retrigger
    the append). Workable submits input.value, so this leaves the submitted value clean."""
    try:
        n = page.evaluate(r"""(clean)=>{
          const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
          let fixed=0;
          const ins=[...document.querySelectorAll('input#address,input[name=address],input[data-ui=address]')];
          for(const e of ins){
            if((e.value||'')!==clean){ set.call(e, clean); e.dispatchEvent(new Event('blur',{bubbles:true})); fixed++; }
          }
          return fixed;
        }""", clean)
        if n:
            print(f"    [address-cleanup] collapsed {n} field(s) to a single clean value")
    except Exception as e:
        print(f"    [address-cleanup] failed: {e}")

def _has_separate_city_state(page):
    """True if the form exposes distinct City AND State fields (so a separate 'Address' line
    should hold the STREET, not city/state/country)."""
    js = r"""()=>{
      const lab=(e)=>{ let t='';
        const l=e.closest('label')||(e.id&&document.querySelector("label[for='"+e.id+"']"));
        if(l)t=l.innerText;
        if(!t){const a=e.getAttribute('aria-labelledby'); if(a){const n=document.getElementById(a); if(n)t=n.innerText;}}
        if(!t){let p=e.parentElement; for(let i=0;i<3&&p;i++){const lb=p.querySelector('label'); if(lb){t=lb.innerText;break;} p=p.parentElement;}}
        return (t||'').trim().toLowerCase(); };
      let city=false, st=false;
      for(const e of document.querySelectorAll('input,select')){
        const t=lab(e);
        if(/^city\b/.test(t)) city=true;
        if(/^state\b|state\/province|^province\b/.test(t)) st=true;
      }
      return city&&st;
    }"""
    try:
        for fr in [page] + list(getattr(page, "frames", []) or []):
            try:
                if fr.evaluate(js):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False

def _fill_address(page, el, val):
    """Idempotent fill for a free-text / autocomplete address field. Geocoding widgets
    reformat our text (adding a zip) and then PREPEND our value again on a second pass,
    yielding a doubled string. So: if the city is already present, leave it; otherwise
    clear, fill, and dismiss any suggestion dropdown so it can't append a formatted address."""
    city = (val.split(",")[0] or "").strip().lower()
    try:
        cur = (el.input_value(timeout=500) or "")
    except Exception:
        cur = ""
    if city and city in cur.lower():
        return True   # already filled - don't append again
    try:
        el.click(timeout=800); el.press("Control+A"); el.press("Delete")
    except Exception:
        pass
    try:
        el.fill(val)
    except Exception:
        try: el.type(val, delay=10)
        except Exception: return False
    try:
        page.wait_for_timeout(150); el.press("Escape")   # close autocomplete, no append
    except Exception:
        pass
    try:
        return bool((el.input_value(timeout=500) or "").strip())
    except Exception:
        return True

def _open_and_pick(page, el, value):
    try: el.click()
    except Exception:
        try: el.evaluate("e=>e.click()")
        except Exception: return False
    page.wait_for_timeout(300)
    try:  # filter by typing (react-select / location typeahead / Workday search box)
        el.fill(""); el.type(value[:30], delay=20)
    except Exception:
        # Workday widgets: type into a visible Search box if present
        try:
            sb = page.query_selector('input[type=text][placeholder="Search"], input[data-automation-id="searchBox"]')
            if sb and _visible(sb): sb.fill(value[:30])
        except Exception:
            pass
    page.wait_for_timeout(500)          # let async suggestions arrive
    try:
        scope = el.owner_frame() or page
    except Exception:
        scope = page
    picked = _pick_option(scope, value)
    if not picked:
        try:
            opts = scope.eval_on_selector_all(
                "[data-automation-id='promptOption'],[data-automation-id*='promptOption'],"
                "[role=option],li[role=option],[class*='option']",
                "els => [...new Set(els.map(e=>(e.innerText||'').trim()).filter(Boolean))].slice(0,30)")
            if opts:
                print(f"    [pick] '{value}' not matched; visible options: {opts}")
        except Exception:
            pass
    if not picked:
        # typeahead fallback: highlight first suggestion and commit it
        try:
            el.press("ArrowDown"); page.wait_for_timeout(150); el.press("Enter")
            picked = True
        except Exception:
            pass
    # close the menu without disturbing other fields: blur THIS element only
    # (a global Escape can land on and clear a neighbouring text field like Phone Number).
    try:
        page.wait_for_timeout(150)
        el.evaluate("e => e.blur && e.blur()")
    except Exception:
        pass
    return picked

def set_value(page, el, tag, typ, val):
    """Return True if the value was applied. Handles native select, custom
    react-select/listbox comboboxes, radio/checkbox, and plain inputs."""
    try:
        if tag == "select":
            # Read options and pick the best text match, then select THAT existing
            # option in one shot. Never call select_option with a value that might not
            # be an option: Playwright auto-waits ~30s on a miss, which stalls the whole
            # walk on any non-exact value (e.g. "US citizen" vs the option "U.S. Citizen").
            try:
                opts = [((o.inner_text() or "").strip(), o.get_attribute("value"))
                        for o in el.query_selector_all("option")]
            except Exception:
                opts = []
            opts = [(t, v) for (t, v) in opts if t and not re.match(r"^[-\s]*select", t, re.I)]
            tv, tvt = _norm(val), _norm_tight(val)
            pick = None
            for t, v in opts:
                if t == val: pick = (t, v); break
            if not pick:
                for t, v in opts:
                    if _norm(t) == tv: pick = (t, v); break
            if not pick:
                for t, v in opts:
                    if _norm_tight(t) == tvt: pick = (t, v); break
            if not pick and len(tvt) >= 4:
                for t, v in opts:
                    if tvt in _norm_tight(t) or _norm_tight(t) in tvt:
                        pick = (t, v); break
            if not pick:
                # leading-word match: "Associate"/"Associate Degree" -> "Associate's Degree",
                # "Bachelor" -> "Bachelor's Degree" (possessive breaks the tight match).
                vw = re.split(r"[\s']", val.strip())[0].lower()
                if len(vw) >= 4:
                    for t, v in opts:
                        tw = re.split(r"[\s']", (t or "").strip())[0].lower()
                        if tw and tw == vw:
                            pick = (t, v); break
            if not pick:
                return False
            t, v = pick
            try:
                if v is not None:
                    el.select_option(value=v); return True
            except Exception:
                pass
            try:
                el.select_option(label=t); return True
            except Exception:
                return False
        if typ in ("radio", "checkbox"):
            return bool(el.evaluate(
                "(e,v)=>{const l=e.closest('label')||document.querySelector(`label[for='${e.id}']`);"
                "if(l && l.innerText.trim().toLowerCase().startsWith(v.toLowerCase())){e.click();return true;}return false;}", val))
        # custom combobox?
        role = (el.get_attribute("role") or "").lower()
        haspopup = (el.get_attribute("aria-haspopup") or "").lower()
        readonly = el.get_attribute("readonly") is not None
        if role == "combobox" or haspopup in ("listbox", "true", "menu") or readonly:
            # already selected? don't re-open it (avoids leaving a menu hanging)
            try:
                cur = _norm(el.inner_text())
            except Exception:
                cur = ""
            if cur and "select one" not in cur and _norm(val) and _norm(val) in cur:
                return True
            return _open_and_pick(page, el, val)
        # plain text / textarea
        el.fill(val); return True
    except Exception:
        return False

def _status_init(page, title="Apply Autofill", subtitle="Autofilling this page..."):
    # Rendered inside a Shadow DOM so the host page's CSS can never distort it.
    try:
        page.evaluate(r"""([t,st])=>{
          let host=document.getElementById('__apply_panel');
          if(!host){
            host=document.createElement('div'); host.id='__apply_panel';
            host.style.cssText='position:fixed;top:14px;right:14px;z-index:2147483647;';
            const root=host.attachShadow({mode:'open'});
            root.innerHTML=
              '<style>'
              +':host{all:initial;}'
              +'*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,Segoe UI,Arial,sans-serif;}'
              +'.panel{width:290px;max-height:78vh;display:flex;flex-direction:column;background:#0f1420;'
              +'color:#e8ecf3;border:1px solid #263042;border-radius:12px;'
              +'box-shadow:0 10px 30px rgba(0,0,0,.45);overflow:hidden;font-size:12px;line-height:1.45;}'
              +'.hd{display:flex;justify-content:space-between;align-items:center;padding:11px 13px;'
              +'background:#151c2c;border-bottom:1px solid #263042;font-weight:600;font-size:13px;}'
              +'.x{cursor:pointer;opacity:.55;font-size:14px;line-height:1;}'
              +'.sub{padding:8px 13px 4px;color:#8fa1bd;font-size:11px;}'
              +'.body{padding:2px 13px 12px;overflow-y:auto;overflow-x:hidden;}'
              +'.row{display:flex;gap:7px;padding:3px 0;line-height:1.4;min-height:18px;align-items:flex-start;}'
              +'.ic{flex:0 0 auto;}'
              +'.tx{word-break:break-word;flex:1 1 auto;}'
              +'</style>'
              +'<div class="panel"><div class="hd"><span class="t"></span>'
              +'<span class="x">\u2715</span></div>'
              +'<div class="sub"></div><div class="body"></div></div>';
            document.body.appendChild(host);
            root.querySelector('.x').onclick=()=>host.remove();
          }
          const r=host.shadowRoot;
          r.querySelector('.t').textContent=t;
          r.querySelector('.sub').textContent=st;
          r.querySelector('.body').innerHTML='';
        }""", [title, subtitle])
    except Exception:
        pass

def _status_sub(page, text):
    try:
        page.evaluate("(t)=>{const h=document.getElementById('__apply_panel');"
                      "if(h&&h.shadowRoot){const e=h.shadowRoot.querySelector('.sub');if(e)e.textContent=t;}}", text)
    except Exception:
        pass

def _wd_errors(page):
    """Read Workday's own on-page validation errors: the top 'Errors Found' summary
    plus any inline field alerts. Returns a de-duplicated list of message strings."""
    try:
        return page.evaluate(r"""() => {
          const sels = "[data-automation-id='errorMessage'],[data-automation-id*='rror'],"
                     + "[data-automation-id='inputAlert'],[data-automation-id*='lert'],[role=alert]";
          const skip = /^errors? found$/i;
          const out = [];
          for (const e of document.querySelectorAll(sels)) {
            if (e.offsetParent === null) continue;
            let t = (e.innerText || '').trim().replace(/\s+/g,' ');
            if (!t || skip.test(t)) continue;
            out.push(t);
          }
          return [...new Set(out)].slice(0, 15);
        }""") or []
    except Exception:
        return []

def _status_line(page, text, st="ok"):
    try:
        page.evaluate(r"""([text,st])=>{
          const h=document.getElementById('__apply_panel'); if(!h||!h.shadowRoot)return;
          const b=h.shadowRoot.querySelector('.body'); if(!b)return;
          const ic={ok:'\u2705',warn:'\u26A0\uFE0F',skip:'\u00B7',run:'\u23F3',done:'\uD83C\uDFC1',info:'\u2022'}[st]||'\u2022';
          const c={ok:'#cfe8d4',warn:'#f4d58d',skip:'#5f6b80',run:'#cfe8d4',done:'#e8ecf3',info:'#cfd7e6'}[st]||'#cfd7e6';
          const row=document.createElement('div'); row.className='row'; row.style.color=c;
          const a=document.createElement('span'); a.className='ic'; a.textContent=ic;
          const t=document.createElement('span'); t.className='tx'; t.textContent=text;
          row.appendChild(a); row.appendChild(t); b.appendChild(row); b.scrollTop=b.scrollHeight;
        }""", [text, st])
    except Exception:
        pass

def _walk(page, app, dry, upload, state):
    results = []
    filled_now = 0
    seen = state.setdefault("seen", set())
    for el in controls(page):
        try:
            lp = clean_label(el.evaluate(LABEL_JS))
            tp = (el.get_attribute("type") or "").lower()
        except Exception:
            lp, tp = "", ""
        try:
            role = (el.get_attribute("role") or "").lower()
            tagn = el.evaluate("e=>e.tagName.toLowerCase()")
            haspopup = (el.get_attribute("aria-haspopup") or "").lower()
        except Exception:
            role = tagn = haspopup = ""
        is_combo = (role in ("combobox", "listbox") or tagn == "select"
                    or haspopup in ("listbox", "true", "menu"))
        try:
            vis = el.is_visible()
        except Exception:
            vis = False
        if not vis and tp != "file":     # allow HIDDEN file inputs (styled dropzones)
            continue
        # unlabeled junk control -- but KEEP comboboxes/selects: their label is often
        # a sibling above the widget and gets recovered inside fill_control.
        if not lp and tp != "file" and not is_combo:
            continue
        # Per-ELEMENT dedup (not per-label): forms like Paylocity reuse the SAME labels
        # ("Supervisor Name", "May We Contact", "Position") across every work-history
        # block, so label-dedup would fill only the first block. Mark each element after
        # handling and skip marked ones (survives re-scan passes; lost on React remount
        # which is fine -- fills are idempotent).
        try:
            if el.get_attribute("data-__applied"):
                continue
        except Exception:
            # fallback to label dedup only if we truly can't read the element
            if lp and lp in seen:
                continue
        kind, label, val = fill_control(page, el, app, dry, upload, state)
        results.append((kind, label, val))
        try:
            el.evaluate("e=>e.setAttribute('data-__applied','1')")
        except Exception:
            if label:
                seen.add(label.strip())
        is_fill = kind not in ("skip", "pause", "eeo", "blank", "choice") and not kind.startswith("pause")
        if is_fill:
            filled_now += 1
        short = (val[:70] + "…") if len(val) > 70 else val
        print(f"[{kind:14}] {label[:46]:46} {short}")
        if label and not dry:
            if is_fill:
                _status_line(page, label[:42], "ok")
            elif kind == "choice":
                _status_line(page, label[:42] + " -> needs you", "warn")
            elif kind == "pause":
                _status_line(page, label[:42] + " -> paused", "warn")
    pauses = [r for r in results if r[0] == "pause"]
    if pauses:
        for _, lab, why in pauses:
            print(f"    - paused: {lab[:60]}  ({why})")
    return results, filled_now

def _fill_lightning_comboboxes(page):
    """Salesforce lightning-combobox (e.g. State). controls() misses it (its trigger is
    a button deep in shadow), so handle it here with Playwright locators, which pierce
    shadow. For each visible combobox: read its label, resolve the answer from
    profile/fields.yaml, open it, click the matching option (options can render in a
    body portal as lightning-base-combobox-item / [role=option])."""
    try:
        boxes = page.locator("lightning-combobox")
        n = boxes.count()
    except Exception:
        return
    for i in range(n):
        box = boxes.nth(i)
        try:
            if not box.is_visible():
                continue
            lbl = clean_label(box.evaluate(LABEL_JS)) or clean_label(box.evaluate(QUESTION_JS)) or ""
        except Exception:
            lbl = ""
        if not lbl:
            continue
        try:
            cur = box.evaluate("e=>{const b=e.querySelector&&e.querySelector('button,[role=combobox],input');"
                               "return b?((b.value||b.textContent||'').trim()):'';}") or ""
        except Exception:
            cur = ""
        try:
            rr = apply.answer(field=lbl)
        except Exception:
            rr = {}
        val = rr.get("text", "") if rr.get("kind") == "field" else ""
        if not val or len(val) > 40:
            continue
        if cur and val.lower() in cur.lower():
            continue   # already set
        try:
            box.locator("button, [role=combobox], input").first.click(timeout=1500)
        except Exception:
            try: box.click(timeout=1500)
            except Exception: continue
        page.wait_for_timeout(350)
        picked = False
        for pat in ("^%s$" % re.escape(val), re.escape(val)):
            try:
                opt = page.locator('[role=option], .slds-listbox__option, lightning-base-combobox-item')\
                    .filter(has_text=re.compile(pat, re.I)).first
                if opt.count():
                    opt.click(timeout=1500); picked = True; break
            except Exception:
                pass
        page.wait_for_timeout(200)
        try: print("    [combobox      ] %-30s %s" % (lbl[:30], val if picked else "pick failed"))
        except Exception: pass


def _autofill(page, app, dry, upload):
    """Fill the page, re-scanning only while new fields actually get FILLED
    (so conditional fields like the race dropdown appear, but unlabeled junk
    doesn't trigger endless passes). Capped."""
    state = {}
    all_results = []
    # Clear stale per-element dedup markers from a PREVIOUS run: iCIMS (and other SPAs) keep
    # the same live DOM across re-attaches, so last run's data-__applied would skip every
    # field. Wipe them across all frames so a re-run re-evaluates the whole form.
    try:
        for fr in [page] + list(getattr(page, "frames", []) or []):
            try:
                fr.evaluate("()=>document.querySelectorAll('[data-__applied]')"
                            ".forEach(e=>e.removeAttribute('data-__applied'))")
            except Exception:
                pass
    except Exception:
        pass
    if not dry:
        _status_init(page, "Apply Autofill", "Autofilling this page...")
    for _ in range(5):
        results, filled_now = _walk(page, app, dry, upload, state)
        all_results += results
        if filled_now == 0:
            break
        page.wait_for_timeout(600)      # let conditional fields render
    total = sum(1 for r in all_results
                if r[0] not in ("skip", "pause", "eeo", "blank", "choice")
                and not r[0].startswith("pause"))
    npause = sum(1 for r in all_results if r[0] == "pause")
    nchoice = sum(1 for r in all_results if r[0] == "choice")
    reviews = [r for r in all_results if r[0] == "review"]
    print(f"\nDone: {total} filled, {nchoice} choice field(s) for you, {npause} paused, {len(reviews)} to review.")
    if reviews:
        print("\n  !! REVIEW BEFORE SUBMIT (answered, but the question probes tech with no evidence in your history):")
        for r in reviews:
            print(f"     - {r[1][:60]}  ->  {r[2]}")
    if not dry:
        _status_sub(page, f"{total} filled" + (f", {nchoice} need you" if nchoice else ""))
    return all_results

def _wait_for(page, ctx, cond, timeout_ms=900000, poll_ms=1500):
    """Poll (NO keyboard input) until cond(page) is true or timeout. Follows a
    newly-opened tab. Returns the active page (updated if a new tab opened)."""
    import time as _t
    deadline = _t.time() + timeout_ms / 1000.0
    while _t.time() < deadline:
        try:
            p = ctx.pages[-1] if len(ctx.pages) > 1 else page
        except Exception:
            p = page
        try:
            if cond(p):
                return p
        except Exception:
            pass
        try:
            p.wait_for_timeout(poll_ms)
        except Exception:
            _t.sleep(poll_ms / 1000.0)
    return page


def _wd_step_sig(page):
    """Signature of the current Workday wizard step, to detect real advancement."""
    try:
        return page.evaluate(
            "() => { const m=(document.body.innerText||'')"
            ".match(/current step\\s*\\d+\\s*of\\s*\\d+[^\\n]{0,30}/i); return m?m[0]:''; }") or ""
    except Exception:
        return ""


def _wd_on_form_step(page):
    """True if the tab is already on a real Workday wizard FORM step (My Information,
    My Experience, Application Questions, etc.) rather than the intro/resume/account
    gates. Prevents the pre-form gate loop from re-uploading the resume and clicking
    Continue when we attach mid-wizard."""
    try:
        return bool(page.evaluate(r"""() => {
          if (document.querySelector('input[id^="workExperience-"]')) return true;
          if (document.querySelector('[data-automation-id="formField-legalName--firstName"],'
             +'[data-automation-id="formField-source"]')) return true;
          const pw = [...document.querySelectorAll('input[type=password]')].some(e=>e.offsetParent!==null);
          if (pw) return false;
          const ff = document.querySelectorAll('[data-automation-id^="formField-"]').length;
          const completed = document.querySelectorAll('[data-automation-id="progressBarCompletedStep"]').length;
          return ff >= 2 || completed >= 1;
        }"""))
    except Exception:
        return False


def _wd_at_submit(page):
    """True when the current Workday step is Review / the footer action is Submit,
    so the auto-advancer stops instead of ever submitting."""
    try:
        if page.query_selector('[data-automation-id="pageFooterSubmitButton"], '
                               '[data-automation-id*="reviewSubmit"], '
                               '[data-automation-id="wd-CommandButton_uic_reviewSubmitButton"]'):
            return True
        b = page.query_selector('[data-automation-id="pageFooterNextButton"]')
        if b:
            t = (b.inner_text() or "").strip().lower()
            if "submit" in t:
                return True
        return bool(page.evaluate(
            "() => [...document.querySelectorAll('[aria-current]')]"
            ".some(x=>/review/i.test(x.textContent||''))"))
    except Exception:
        return False


ALLOW_SUBMIT = os.environ.get("APPLY_ALLOW_SUBMIT", "").strip().lower() in ("1", "true", "yes", "on")
# Opt-in: let iCIMS auto-advance through its multi-step wizard (clicks the page-advance
# "Submit" button ONLY while not on the last step). Off by default - the tool refuses to
# advance unless this is set AND it can confirm the current step is not the final one.
ICIMS_ADVANCE = os.environ.get("APPLY_ICIMS_ADVANCE", "").strip().lower() in ("1", "true", "yes", "on")

def _install_submit_guard(page):
    """BUILD-MODE SAFETY: block real submission so test runs never submit. Cancels
    native form submits (incl. Enter-in-a-field) and clicks on submit/apply buttons,
    at capture phase. Override by setting APPLY_ALLOW_SUBMIT=1 when ready for real
    submits. Re-run each pass (SPAs re-render)."""
    if ALLOW_SUBMIT:
        return
    try:
        page.evaluate(r"""() => {
          if (window.__applyNoSubmit) return;
          window.__applyNoSubmit = true;
          const stop = (e, why) => { try{e.preventDefault();e.stopPropagation();
            if(e.stopImmediatePropagation)e.stopImmediatePropagation();}catch(_){}
            console.log('[apply] blocked '+why); };
          document.addEventListener('submit', e => stop(e,'form-submit'), true);
          document.addEventListener('click', e => {
            const b = e.target && e.target.closest &&
              e.target.closest('button,[role=button],input[type=submit],a');
            if (!b) return;
            const t = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim();
            if (/^(submit|submit application|submit & continue|apply now|apply)\b/i.test(t)) {
              // one-shot bypass: a CONFIRMED page-advance (not final step) sets this flag
              // immediately before clicking; it auto-clears so nothing else slips through.
              if (window.__applyAllowNextClick) { window.__applyAllowNextClick = false; return; }
              stop(e,'submit-button:'+t.slice(0,30));
            }
          }, true);
          // Enter in a single-line input often submits: swallow it at capture.
          document.addEventListener('keydown', e => {
            if (e.key === 'Enter') {
              const el = e.target;
              if (el && el.tagName === 'INPUT' && (el.type||'text') !== 'textarea')
                { /* allow typeahead Enter to select, but stop form submit */ }
            }
          }, true);
        }""")
    except Exception as _e:
        print(f"    [submit-guard] {_e}")

def _icims_step(page):
    """Read the iCIMS progress stepper -> (current, total). (0,0) if it can't be read
    confidently. Completed steps show a checkmark; the current one is highlighted."""
    try:
        r = page.evaluate(r"""()=>{
          const navs=[...document.querySelectorAll('nav,ol,ul,[role=navigation],[class*=progress],[class*=Progress],[class*=step],[class*=Step]')];
          let items=[];
          for(const nav of navs){
            const kids=[...nav.children].filter(k=>k.offsetParent!==null);
            // a stepper is a row of 3-12 sibling markers, each a number or a check
            if(kids.length>=3 && kids.length<=12){
              const looksStep=kids.filter(k=>/^\s*(\d+|✓)/.test((k.textContent||'').trim()) || k.querySelector('svg,use,[class*=check],[class*=Check]'));
              if(looksStep.length>=3){ items=kids; break; }
            }
          }
          if(items.length<3) return [0,0];
          const total=items.length;
          let curIdx=-1;
          items.forEach((it,i)=>{
            if(/current|active|selected|is-active/i.test(it.className) || it.getAttribute('aria-current')) curIdx=i;
          });
          if(curIdx<0){
            // fall back to (completed checkmarks)+1
            let done=0;
            for(const it of items){
              const isDone=/✓/.test(it.textContent||'') || /complete|done|checked/i.test(it.className) || it.querySelector('svg,use,[class*=check],[class*=Check]');
              if(isDone) done++; else break;
            }
            curIdx=Math.min(done, total-1);
          }
          return [curIdx+1, total];
        }""")
        c, t = int(r[0]), int(r[1])
        return (c, t)
    except Exception:
        return (0, 0)

def _icims_looks_final(page):
    """True if the page looks like the terminal review/submit step - never auto-advance it."""
    try:
        return bool(page.evaluate(r"""()=>{
          const txt=(document.body.innerText||'').toLowerCase();
          return /review your application|by (clicking )?submit|please review your|ready to submit|submit your application/.test(txt);
        }"""))
    except Exception:
        return False

def _icims_advance(page):
    """Advance one iCIMS step, but ONLY when we can confirm it's not the last step.
    Refuses (returns False, no click) if the stepper can't be read, we're on the last
    step, or the page looks like a final review. Verifies the step actually advanced."""
    cur, total = _icims_step(page)
    if not total:
        print("    [icims-advance] cannot read the step indicator - NOT advancing (safe).")
        return False
    if cur >= total:
        print(f"    [icims-advance] on the last step ({cur}/{total}) - stopping before submit.")
        return False
    if _icims_looks_final(page):
        print("    [icims-advance] page reads like a final review - stopping before submit.")
        return False
    # find the advance button: prefer Continue/Save/Next; fall back to the primary Submit
    btn = None
    for sel in ("button:has-text('Save & Continue')", "button:has-text('Save and Continue')",
                "button:has-text('Continue')", "button:has-text('Next')",
                "button:has-text('Save')", "button:has-text('Submit')",
                "input[type=submit]"):
        try:
            b = page.query_selector(sel)
            if b and b.is_visible():
                btn = b; break
        except Exception:
            pass
    if not btn:
        print("    [icims-advance] no advance button found - stopping.")
        return False
    try:
        page.evaluate("window.__applyAllowNextClick = true")   # one-shot guard bypass
        btn.click()
    except Exception as e:
        print(f"    [icims-advance] click failed: {e}")
        try: page.evaluate("window.__applyAllowNextClick = false")
        except Exception: pass
        return False
    page.wait_for_timeout(1800)
    try: page.evaluate("window.__applyAllowNextClick = false")  # ensure it's cleared
    except Exception: pass
    cur2, total2 = _icims_step(page)
    if cur2 > cur:
        print(f"    [icims-advance] advanced {cur} -> {cur2} of {total2}")
        return True
    print(f"    [icims-advance] step did not advance ({cur}->{cur2}); stopping for review.")
    return False

def run(url, dry=False, upload=True, cli_company=None, cli_role=None, use_llm=True, job=None, attach=False, cdp_url="http://127.0.0.1:9222"):
    from playwright.sync_api import sync_playwright
    global HOW_HEAR, JOB_ANSWERS, CURRENT_ATS
    JOB_ANSWERS = (job or {}).get("answers", []) if job else []
    if JOB_ANSWERS:
        print(f"Per-job locked answers: {len(JOB_ANSWERS)}")
    try:
        _p = apply.load("profile.yaml")
        HOW_HEAR = (_p.get("application_defaults") or {}).get("how_did_you_hear", HOW_HEAR)
    except Exception:
        pass
    ats = detect_ats(url)
    CURRENT_ATS = ats
    print(f"ATS detected: {ats}")
    profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".browser-profile")
    with sync_playwright() as p:
        if attach:
            def _norm(u):
                try: return (u or "").split("#")[0].rstrip("/")
                except Exception: return u or ""
            browser = p.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = None
            for _pg in ctx.pages:
                try:
                    if _norm(_pg.url) == _norm(url):
                        page = _pg; break
                except Exception:
                    pass
            if page is None:
                page = ctx.pages[-1] if ctx.pages else ctx.new_page()
                if _norm(page.url) != _norm(url):
                    page.goto(url, wait_until="domcontentloaded")
            try: page.bring_to_front()
            except Exception: pass
            print(f">>> Attached to your Chrome tab: {page.url[:80]}")
        else:
            ctx = p.chromium.launch_persistent_context(profile_dir, headless=False,
                                                       viewport={"width": 1280, "height": 900})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        dismiss_cookies(page)

        # Workday: click the real Apply CTA -> "Start Your Application" modal ->
        # Autofill with Resume. Then Workday's own wizard handles Create Account /
        # Sign In as the FIRST step, so account comes before the form.
        if ats == "workday":
            if not looks_like_login(page):
                if click_by_role(page, [r"^apply$", r"^apply now$"]):
                    page.wait_for_timeout(1200)
                    if not click_by_role(page, [r"^autofill with resume$", r"autofill with resume"]):
                        click_by_role(page, [r"^apply manually$", r"manually"])
                    page.wait_for_timeout(1500)
                if len(ctx.pages) > 1:
                    page = ctx.pages[-1]; page.wait_for_timeout(1000)

            # Workday's pre-form gates can appear in any order and at any time: a
            # "Start Your Application" chooser, a Create Account / Sign In wall, and a
            # resume-autofill file step. Loop and handle whichever is showing until the
            # real form appears. Resilient to slow renders -- the old fixed 12s window
            # missed late-loading account pages and then waited forever for a form.
            _wd_resume_done = False
            _wd_continues = 0
            _wd_idle = 0
            _wd_max_idle = 3 if dry else (40 if attach else 240)
            _skip_gate = _wd_on_form_step(page)
            if _skip_gate:
                print(">>> Already on a Workday form step -- skipping intro/resume gate.")
            while not _skip_gate:
                # 1) "Start Your Application" chooser -> Autofill with Resume / Apply Manually
                if click_by_role(page, [r"^autofill with resume$", r"autofill with resume",
                                        r"^apply manually$", r"^apply with resume$"]):
                    page.wait_for_timeout(1500)
                    if len(ctx.pages) > 1:
                        page = ctx.pages[-1]
                    _wd_idle = 0
                    continue
                # 2) Account / sign-in wall -> fill + create/sign-in from secrets.yaml
                if looks_like_login(page):
                    outcome = workday_login(page, dry)
                    if outcome == "in":
                        print(">>> Workday: signed in / account created with stored credentials")
                        page.wait_for_timeout(1500)
                        _wd_idle = 0
                        continue
                    else:
                        print("\n>>> Workday account step needs you (stored creds didn't finish it,")
                        print("    e.g. an emailed verification code). Finish it in the browser --")
                        print("    no need to touch this terminal; I continue automatically.")
                        if dry or attach:
                            if attach:
                                print("    Finish that step in the browser, then click Apply")
                                print("    Autofill again and I take it from there.")
                            break
                        page = _wait_for(page, ctx, lambda p: not looks_like_login(p))
                        page.wait_for_timeout(800)
                        _wd_idle = 0
                        continue
                # 3) Resume-autofill file step -> set the dropzone's file input directly
                if upload and RESUME_PATH and not dry and not _wd_resume_done:
                    fin = None
                    for fi in page.query_selector_all('input[type=file]'):
                        try:
                            if fi.is_enabled():
                                fin = fi; break
                        except Exception:
                            pass
                    if fin is not None:
                        try:
                            fin.set_input_files(RESUME_PATH)
                            print(">>> Workday: resume uploaded for autofill")
                            _wd_resume_done = True
                            page.wait_for_timeout(3000)   # let Workday parse it
                            _wd_idle = 0
                            continue
                        except Exception as _e:
                            print("    [resume] upload failed: %s" % str(_e)[:80])
                # 4) The real application form -> stop and let the filler run
                if wait_for_form(page, 1200):
                    break
                # 4b) An intro/gate step with only a Continue button and no fillable
                # form yet (e.g. "Autofill with Resume") -> click Continue to advance.
                if not dry and _wd_continues < 6 and click_by_role(
                        page, [r"^continue$", r"^i agree.*continue$"]):
                    _wd_continues += 1
                    print(">>> advanced a Workday intro step (Continue)")
                    page.wait_for_timeout(1800)
                    _wd_idle = 0
                    continue
                _wd_idle += 1
                if dry or _wd_idle >= _wd_max_idle:
                    if not dry and not attach:
                        print(">>> Still waiting for the application form. Finish any step in")
                        print("    the browser; I pick it up automatically when fields appear.")
                        page = _wait_for(page, ctx, lambda p: looks_like_login(p) or wait_for_form(p, 1200))
                        if looks_like_login(page):
                            _wd_idle = 0
                            continue
                    elif attach:
                        print(">>> No fillable form on this step yet. If the page has moved on,")
                        print("    click Apply Autofill again to continue.")
                    break
                page.wait_for_timeout(500)

        # Non-Workday ATSs usually hide the application form behind an "Apply" CTA
        # (Rippling "Apply now", Workable "Apply for this job", Jobvite "Apply") or a
        # separate application route (Ashby /application). If no form is showing yet,
        # click the Apply button (or open /application) to reveal it, then re-check.
        if ats != "workday":
            def _n_controls():
                try:
                    return len([c for c in controls(page) if _visible(c)])
                except Exception:
                    return 0
            if _n_controls() < 3 and not looks_like_login(page):
                if click_by_role(page, APPLY_NAMES):
                    page.wait_for_timeout(1800)
                    if len(ctx.pages) > 1:
                        page = ctx.pages[-1]
                    dismiss_cookies(page)
                    page.wait_for_timeout(400)
                if ats == "ashby" and _n_controls() < 3 and "/application" not in page.url:
                    try:
                        page.goto(page.url.split("?")[0].rstrip("/") + "/application",
                                  wait_until="domcontentloaded")
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass

        # Non-Workday login/account walls (Rippling, ADP, etc.)
        if ats == "icims":
            _icims_login(page, dry)   # attempt credentialed sign-in (needs secrets.yaml icims)
        if ats != "workday" and looks_like_login(page):
            print("\n>>> Sign-in / account wall detected. Log in in the browser window;")
            print("    I continue automatically once the application form appears.")
            if ats == "icims":
                _probe_login(page)   # capture the login DOM so we can improve the auto-login
            if not dry and not attach:
                page = _wait_for(page, ctx, lambda p: not looks_like_login(p))
            elif attach:
                print("    Log in, then click Apply Autofill again to continue.")
            page.wait_for_timeout(800)

        company, role, jd = scrape_meta(page, ats, cli_company, cli_role)
        print(f"Company: {company or '(unknown)'}   Role: {role or '(unknown)'}")
        app = infer_context(company, role, jd, use_llm=use_llm)
        print(f"Interest style: {app['interest_style']}")
        print(f"Why-company basis: {app['company_description'] or '(generic)'}\n")

        # Workable uses an 'Import resume from' dropdown, not a plain file input.
        if ats == "workable" and upload and not dry:
            if upload_resume_workable(page, dry):
                print("[resume        ] Import resume from -> My computer          uploaded")
            else:
                print("[resume-manual ] Workable resume: click 'Import resume from > My computer' "
                      "and pick your PDF by hand")

        # Auto-fill loop. FULLY HANDS-OFF: no keyboard input anywhere. For Workday
        # we auto-advance the multi-step wizard, stopping BEFORE the Submit/Review
        # step. For single-page ATSs one _autofill pass (which itself re-scans for
        # conditional fields) fills everything.
        auto_steps = (ats == "workday" and not dry) or (ats == "icims" and ICIMS_ADVANCE and not dry)
        if not dry and (ats in ("rippling","paylocity","generic")
                        or os.environ.get("APPLY_DUMP","").strip() in ("1","true","yes","on")):
            _dump_form_dom(page)
        if not dry:
            _install_submit_guard(page)
            if not ALLOW_SUBMIT:
                print(">>> BUILD MODE: submit is BLOCKED (set APPLY_ALLOW_SUBMIT=1 to enable).")
        for _step in range(10):
            if not dry:
                _install_submit_guard(page)
            if ats == "workday":
                try:
                    on_exp = page.query_selector(
                        'input[id^="workExperience-"], '
                        'input[data-automation-id="jobTitle"], '
                        'input[aria-label^="Job Title"], '
                        '[data-automation-id^="workExperience"], '
                        '[data-automation-id="workExperienceSection"]')
                    if not on_exp:
                        on_exp = page.evaluate(
                            "() => !!([...document.querySelectorAll('h2,h3,h4,label,legend,div')]"
                            ".find(e => /work experience/i.test((e.textContent||'').slice(0,60))))")
                    if on_exp:
                        correct_workday_experience(page, dry)
                except Exception as _e:
                    print(f"    [experience] gate error: {_e}")
            _autofill(page, app, dry, upload)
            if not dry and ats == "generic":
                try: _fill_lightning_comboboxes(page)
                except Exception as _ce: print(f"    [combobox] {_ce}")
            if not dry and ats == "icims":
                _icims_create_login(page, dry)
                _icims_use_address_anyway(page)
                _icims_iform_eeo(page)
                _icims_tick_signature(page)
                if ICIMS_ADVANCE:
                    if _icims_advance(page):
                        page.wait_for_timeout(700)
                        continue   # next loop pass fills the new step
                    else:
                        break      # last step / unreadable / didn't advance -> stop for review
            if not dry and ats == "paylocity":
                try:
                    _ph = apply.dotted(apply.load("profile.yaml"), "identity.phone")
                    if _ph:
                        _reassert_phones(page, _ph)
                except Exception as _pe:
                    print(f"    [phone-reassert] error: {_pe}")
                _probe_phone(page)   # END-OF-RUN capture (post-fill truth)
            if not dry and ats == "workable":
                try:
                    _addr = apply.dotted(apply.load("profile.yaml"), "identity.location_full") or ""
                except Exception:
                    _addr = ""
                if _addr:
                    _cleanup_address(page, _addr)
                _probe_address(page)   # END-OF-RUN: verify the collapse
            if dry:
                print("[DRY RUN] nothing was typed into the page.")
                break
            if not auto_steps:
                break   # single-page form: filled in one pass, nothing to advance
            # Workday: auto-advance, but NEVER click Submit.
            if _wd_at_submit(page):
                print(">>> Reached the Review step -- stopping before Submit for your review.")
                break
            sig = _wd_step_sig(page)
            if workday_next(page):
                page.wait_for_timeout(1900)
                if _wd_step_sig(page) == sig:
                    print(">>> Could not advance (a required field may need you). "
                          "Leaving the form for your review.")
                    try:
                        wd_errs = _wd_errors(page)
                        if wd_errs:
                            print("    [workday-errors] " + " | ".join(wd_errs))
                            _status_sub(page, "Workday flagged " + str(len(wd_errs)) + " error(s):")
                            for _e in wd_errs[:8]:
                                _status_line(page, _e[:80], "warn")
                        else:
                            print("    [workday-errors] (none shown on page)")
                    except Exception as _e:
                        print(f"    [workday-errors] {_e}")
                    try:
                        blockers = page.evaluate(r"""() => {
                          const nearText = (e) => {
                            let n = e, hops = 0;
                            while (n && hops++ < 6) {
                              const lg = n.querySelector && n.querySelector('legend,h2,h3,h4,label');
                              if (lg) { const t=(lg.innerText||'').trim().split('\n')[0]; if(t) return t; }
                              const al = n.getAttribute && (n.getAttribute('aria-label')||'');
                              if (al) return al.trim();
                              n = n.parentElement;
                            }
                            return '';
                          };
                          const req = [...document.querySelectorAll(
                            'input[aria-required=true],select[aria-required=true],'
                            +'textarea[aria-required=true],input[required],'
                            +'[data-automation-id][aria-required=true],[role=group][aria-required=true],'
                            +'[data-automation-id*="required"]')];
                          const out = [];
                          for (const e of req) {
                            const isGrp = e.getAttribute('role')==='group' || e.tagName==='FIELDSET';
                            const v = (e.value||'').trim();
                            const checked = (e.type==='checkbox'||e.type==='radio') ? e.checked : true;
                            const grpAnswered = isGrp ? !!e.querySelector('input:checked,[aria-checked=true]') : true;
                            if ((!isGrp && (v==='' || !checked)) || (isGrp && !grpAnswered)) {
                              out.push({
                                aid: e.getAttribute('data-automation-id')||'',
                                tag: e.tagName.toLowerCase(),
                                type: e.getAttribute('type')||'',
                                name: e.getAttribute('name')||'',
                                al: e.getAttribute('aria-label')||'',
                                q: nearText(e)
                              });
                            }
                          }
                          // also enumerate radio/checkbox groups on this step
                          const grps = [...document.querySelectorAll('fieldset,[role=group],[role=radiogroup]')].map(g => ({
                            q: nearText(g),
                            aid: g.getAttribute('data-automation-id')||'',
                            opts: [...g.querySelectorAll('label,[role=radio],[data-automation-id]')]
                                    .map(o=>(o.innerText||o.getAttribute('data-automation-id')||'').trim())
                                    .filter(Boolean).slice(0,6),
                            answered: !!g.querySelector('input:checked,[aria-checked=true]')
                          })).filter(g=>g.opts.length).slice(0,10);
                          return {req: out.slice(0,10), grps};
                        }""")
                        import json as _json
                        print("    [blocker-scan] required-empty: " + _json.dumps(blockers.get("req", [])))
                        print("    [blocker-scan] groups: " + _json.dumps(blockers.get("grps", [])))
                    except Exception as _e:
                        print(f"    [blocker-scan] {_e}")
                    break
                print(">>> advanced to next step")
                continue
            print(">>> No Next button found. Leaving the form for your review.")
            break

        if attach:
            print("\n>>> Done. Everything I could fill is filled in your Chrome tab.")
            print("    Review it and click Submit yourself. (Your browser stays open.)")
            try:
                _status_sub(page, "Done - review & click Save/Continue")
                _status_line(page, "Review the page, then submit", "done")
            except Exception:
                pass
        elif dry:
            try: ctx.close()
            except Exception: pass
        else:
            print("\n>>> Done. Everything I could fill is filled. Review in the browser")
            print("    and click Submit yourself. The window stays open -- just close it")
            print("    when you are finished (no need to come back to this terminal).")
            try:
                while len(ctx.pages) > 0:
                    ctx.pages[0].wait_for_timeout(2000)
            except Exception:
                pass
            try: ctx.close()
            except Exception: pass

def load_queue():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "jobs.json")
    if not os.path.exists(p):
        return []
    try:
        return json.load(open(p, encoding="utf-8")).get("queue", [])
    except Exception as e:
        print(f"! could not read data/jobs.json: {e}")
        return []

def select_rows(queue, only):
    """--only matches an exact id, an id substring, OR a company substring (all case-insensitive)."""
    if not only:
        return [r for r in queue if r.get("status") in ("go", "verify")]
    o = only.lower()
    return [r for r in queue
            if o == r.get("id", "").lower()
            or o in r.get("id", "").lower()
            or o in r.get("company", "").lower()]

def print_queue(queue):
    for r in queue:
        st = r.get("status", "?")
        note = f"  ({r['note']})" if r.get("note") else ""
        print(f"  {st:7} {r.get('id',''):26} {r.get('company','')}{note}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="job URL (optional if using --only)")
    ap.add_argument("--only", help="pick queued job(s) by exact id or company substring")
    ap.add_argument("--list", action="store_true", help="print the queue, no browser")
    ap.add_argument("--dry", action="store_true", help="scrape + print, don't type into the page")
    ap.add_argument("--no-upload", action="store_true", help="skip resume upload")
    ap.add_argument("--company")
    ap.add_argument("--role")
    ap.add_argument("--no-llm", action="store_true", help="skip Ollama context inference")
    ap.add_argument("--attach", action="store_true", help="attach to your running Chrome (CDP) and fill the current tab")
    ap.add_argument("--cdp", default="http://127.0.0.1:9222", help="CDP endpoint of your running Chrome")
    args = ap.parse_args()

    queue = load_queue()
    if args.list:
        if not queue:
            print("queue empty (data/jobs.json missing or empty)")
        else:
            print_queue(queue)
        return

    if not RESUME_PATH:
        print("! RESUME_PATH not set in .env - resume upload will be skipped.")

    # Direct URL wins if given; otherwise pull from the queue.
    if args.url:
        targets = [{"company": args.company or "", "role": args.role or "", "url": args.url}]
    else:
        targets = select_rows(queue, args.only)
        if not targets:
            print("Nothing in queue" + (f" for --only={args.only}" if args.only else "") + ".")
            return
        if args.only:
            print(f"--only={args.only} -> {len(targets)} match(es): "
                  + ", ".join(t.get("id", t.get("company", "?")) for t in targets))

    for t in targets:
        if len(targets) > 1:
            print("\n" + "=" * 70)
            print(f"{t.get('company','?')} - {t.get('role','')}  [{t.get('status','')}]")
            print("=" * 70)
        run(t["url"], dry=args.dry, upload=not args.no_upload,
            cli_company=args.company or t.get("company") or None,
            cli_role=args.role or t.get("role") or None,
            use_llm=not args.no_llm, job=t,
            attach=args.attach, cdp_url=args.cdp)

if __name__ == "__main__":
    main()
