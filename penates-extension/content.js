// Project Penates - merged content script.
// Two ways to fill, both grounded on your LOCAL engine (serve.py /answer) and both
// stopping before Submit:
//   1. Whole-page "Fill application": scans the form (light DOM + open shadow roots +
//      same-origin iframes), asks the engine for each field's value, fills identity /
//      yes-no / radios / selects / short text and drafts essays, then shows a review
//      panel of what it filled, flagged, or skipped. Alt+F, the floating button, or the
//      right-click "Fill this application".
//   2. Per-field helper (focus chip / Alt+A / right-click "Answer with local AI"):
//      opens a review card for ONE field so you can read, edit, regenerate, insert. Edits
//      you keep are learned (serve.py /learn).
(() => {
  if (window.__penatesInit) return;   // avoid double-init (content_scripts + on-demand inject)
  window.__penatesInit = true;
  const PENATES_BUILD = "build 59";   // shown in the report header; if you don't see it after a reload, the extension didn't update
  const IS_TOP = window.top === window;
  // The content script is injected only on ATS hosts (manifest matches). When an ATS application
  // form is EMBEDDED as a cross-origin iframe inside a company careers page (e.g. a Greenhouse
  // iframe on acme.com/careers), this code runs in THAT subframe with IS_TOP=false. Let the
  // launcher + fill operate there too, so the button is not top-frame-only.
  const ATS_EMBED_HOST = /(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|workday\.com|icims\.com|paylocity\.com|paycomonline\.net|smartrecruiters\.com|jobvite\.com|rippling\.com|successfactors\.(com|eu)|breezy\.hr|workable\.com|bamboohr\.com|dayforcehcm\.com|recruitee\.com|applytojob\.com|hrmdirect\.com|zohorecruit\.com|trakstar\.com|pinpointhq\.com|adp\.com)$/i.test(location.hostname);
  const CAN_SURFACE = IS_TOP || ATS_EMBED_HOST;
  let currentEl = null, currentQuestion = "", lastEditable = null, chip = null;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const norm  = (s) => clean(s).toLowerCase().replace(/[^a-z0-9 ]+/g, "").trim();

  const isEditable = (el) =>
    !!el && (el.tagName === "TEXTAREA" ||
      (el.tagName === "INPUT" && /^(text|search|email|url|tel|number|)$/i.test(el.type || "")) ||
      el.isContentEditable);

  const inPenates = (el) => !!(el && el.closest && el.closest("[data-penates]"));
  function reFindField(q) {
    // Re-resolve the LIVE field for a question after an await: Ashby may have re-rendered and
    // detached the node we captured before the essay draft. Match questionFor over fresh refs.
    try {
      for (const o of deepFields()) {
        if ((o.tagName === "TEXTAREA" || o.tagName === "INPUT" || o.isContentEditable) &&
            !inPenates(o) && !o.disabled && questionFor(o) === q) return o;
      }
    } catch (_) {}
    return null;
  }

  // ===== surface gating (only show on real application forms) =====
  // User overrides live in chrome.storage.local "penatesSettings" and win over detection,
  // so if the heuristic ever guesses wrong the owner fixes it by site in the options page.
  let SETTINGS = { mode: "forms", alwaysHosts: [], neverHosts: [] };
  const DENY = [
    "mail.google.com", "docs.google.com", "drive.google.com", "calendar.google.com",
    "github.com", "gitlab.com", "bitbucket.org", "reddit.com", "x.com", "twitter.com",
    "youtube.com", "chatgpt.com", "chat.openai.com", "stackoverflow.com",
    "bankofamerica.com", "chase.com", "wellsfargo.com", "localhost", "127.0.0.1"
  ];
  const ATS_HOST_RE = /greenhouse|lever\.co|ashbyhq|myworkdayjobs|workday|icims|paylocity|smartrecruiters|jobvite|teksystems|taleo|breezy|workable|bamboohr|recruitee|applytojob|dayforce|pinpointhq|zohorecruit|paycomonline|successfactors|jobvite|hrmdirect/i;
  const hostIn = (list) => {
    const h = (location.hostname || "").toLowerCase();
    return (list || []).some((d) => h === d || h.endsWith("." + d));
  };
  const isAtsHost = () => ATS_HOST_RE.test(location.hostname || "");
  function applyUrlEvidence() {
    const u = (location.pathname + " " + location.search + " " + location.hash).toLowerCase();
    return /\/apply|\/application|applynow|apply-now|job-app|cx\/job|step=application|gh_jid|ashby_jid|jobid=|\/careers?\/.*\/apply/.test(u);
  }
  // Attribute-only form score (no layout reads, so strict-CSP pages don't fetch blocked fonts).
  function formScore() {
    let inputs = 0, hasIdentity = false, hasResume = false, hasScreening = false, els;
    try { els = document.querySelectorAll("input, textarea, select"); } catch (_) { return 0; }
    for (const e of els) {
      const t = (e.type || "").toLowerCase();
      if (e.tagName === "INPUT" && /^(hidden|submit|button|image|reset)$/.test(t)) continue;
      if (e.disabled || e.hidden || inPenates(e)) continue;
      const st = e.style; if (st && (st.display === "none" || st.visibility === "hidden")) continue;
      const lbl = ((e.getAttribute("aria-label") || e.placeholder || e.name || e.getAttribute("autocomplete") || "") + "").toLowerCase();
      if (t === "search" || /\bsearch\b/.test(lbl)) continue;                 // ignore site search boxes
      if (e.tagName === "INPUT" && /^(radio|checkbox)$/.test(t)) {
        if (/authoriz|sponsor|eeo|self.?identif|veteran|disabilit|hear about|cover letter|gender|\brace\b|ethnic|consent|acknowledg/.test(lbl)) hasScreening = true;
        continue;
      }
      inputs++;
      if (t === "email" || /first name|last name|full name|e-?mail|phone|linkedin/.test(lbl)) hasIdentity = true;
      if (t === "file" || /resume|\bcv\b|cover letter|attach/.test(lbl)) hasResume = true;
    }
    let score = 0;
    if (inputs >= 3) score++;
    if (hasIdentity) score++;
    if (hasResume) score++;
    if (hasScreening) score++;
    return score;
  }
  function isApplySurface() {
    if (isAtsHost()) return formScore() >= 2 || applyUrlEvidence();   // ATS host but not a bare JD/search
    if (applyUrlEvidence()) return formScore() >= 1;
    return formScore() >= 2;
  }
  function surfaceAllowed() {
    if (SETTINGS.mode === "never") return false;
    if (hostIn(SETTINGS.neverHosts)) return false;
    if (hostIn(DENY)) return false;
    if (hostIn(SETTINGS.alwaysHosts)) return true;
    if (SETTINGS.mode === "ats") return isAtsHost() || isApplySurface();  // opt-in: JD reminder too
    return isApplySurface();
  }
  function reevaluate() {
    if (!CAN_SURFACE) return;
    if (!surfaceAllowed()) {
      if (chip) chip.style.display = "none";
      if (launcherBtn) { try { launcherBtn.remove(); } catch (_) {} launcherBtn = null; launcherMounted = false; }
      return;
    }
    mountLauncher();
  }
  try {
    if (chrome.storage && chrome.storage.local) {
      chrome.storage.local.get("penatesSettings", (o) => {
        if (o && o.penatesSettings) SETTINGS = Object.assign(SETTINGS, o.penatesSettings);
        reevaluate();
      });
      chrome.storage.onChanged.addListener((ch, area) => {
        if (area === "local" && ch.penatesSettings) {
          SETTINGS = Object.assign({ mode: "forms", alwaysHosts: [], neverHosts: [] }, ch.penatesSettings.newValue || {});
          reevaluate();
        }
      });
    }
  } catch (_) {}

  document.addEventListener("focusin", (e) => {
    if (isEditable(e.target) && !inPenates(e.target) && surfaceAllowed()) { lastEditable = e.target; showChip(e.target); }
  }, true);
  document.addEventListener("focusout", () => setTimeout(hideChip, 200), true);

  // ---- native value setter (plain .value = is ignored by React/Vue/LWC) ----
  function setNative(el, text) {
    el.focus();
    if (el.isContentEditable) {
      document.execCommand("selectAll", false, null);
      document.execCommand("insertText", false, text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("blur", { bubbles: true }));
      return;
    }
    const proto = el.tagName === "TEXTAREA"
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    desc.set.call(el, text);
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertFromPaste", data: text }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  // ---- employment-history blocks (Paylocity / iCIMS style repeating "Work History N") ----
  // The generic scan fills by label, so every block's Address/City/Start Date got the HOME address
  // and the "when can you start" answer (2026-10-06 Paylocity). Fields inside a block that has ONE
  // "Company Name" label and a position/title label belong to an employer, not the candidate:
  // leave them for the user. The candidate's own section never has a company-name label beside
  // first name / email, so it is not caught.
  const _empCache = new WeakMap();
  function inEmploymentBlock(el) {
    let n = el, hops = 0;
    while (n && hops < 12) {
      n = n.parentElement || (n.getRootNode && n.getRootNode().host) || null; hops++;
      if (!n) break;
      if (_empCache.has(n)) return _empCache.get(n);
      const t = n.textContent || "";
      const c = (t.match(/company name|employer name|name of employer/gi) || []).length;
      if (c >= 1) {
        // Grok seq 206: on a flat page the deciding ancestor is the whole form. A real employer card
        // is small, and never holds the candidate's own name/email/personal-phone labels.
        let nIn = 0; try { nIn = n.querySelectorAll("input, select, textarea").length; } catch (_) {}
        const r = c === 1 && nIn <= 24 &&
          /position|title|occupation|role/i.test(t) &&     // textContent glues labels: no leading \b
          !/(first|given|legal|last|family|full) name|e-?mail|(mobile|cell|home|personal) (phone|number)|resume|cover letter|linkedin/i.test(t);
        _empCache.set(n, r);
        return r;
      }
    }
    return false;
  }

  // ---- address autocomplete (Paylocity geocoder swapped the home address for a Pittsburgh match) ----
  const ADDR_Q = /address line ?1|street address|^street$|^address$|\bcity\b|\bzip\b|postal|^state\b|state\s*\/|\bprovince\b/i;
  // Street value is set WITHOUT blur (a geocoder may commit its highlighted row on blur), the list
  // gets a moment to open, Escape closes it, THEN blur. Escape only for this field's own list
  // (aria-expanded / aria-controls) or a Google Places .pac-container (Grok seq 206).
  function setNativeNoBlur(el, text) {
    el.focus();
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertFromPaste", data: text }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  async function closeAutocomplete(el) {
    await sleep(450);
    try {
      let open = el.getAttribute("aria-expanded") === "true";
      const ctl = el.getAttribute("aria-controls") || el.getAttribute("aria-owns");
      if (!open && ctl) { const b = document.getElementById(ctl); open = !!(b && shown(b)); }
      if (!open) open = [...document.querySelectorAll(".pac-container")].some((b) => shown(b));
      if (open) {
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", keyCode: 27, bubbles: true }));
        el.dispatchEvent(new KeyboardEvent("keyup", { key: "Escape", code: "Escape", keyCode: 27, bubbles: true }));
      }
    } catch (_) {}
    try { el.dispatchEvent(new Event("blur", { bubbles: true })); } catch (_) {}
  }

  // ---- question / label text for ANY field (light DOM + shadow climb for SLDS) ----
  // Generic: some ATSes glue a section heading to the front of the first question in the
  // section (e.g. "Application Questions Do you have any close personal relationship..."). Strip
  // a leading section-heading phrase so the real question reaches the matcher/engine. This is
  // heading-shape based, not tied to any one form.
  const _SECTION_HEAD = /^\s*(application|screening|additional|general|voluntary|candidate|demographic|equal employment|eeo|self[- ]?identification|personal|contact|profile|background|compliance|legal|diversity)?[\s,]*(questions?|information|survey|self[- ]?identification|disclosures?|details?|section)\s+(?=(do|are|have|did|which|what|how|please|would|will|is|were|can|may)\b)/i;
  function stripSectionHead(s) {
    if (!s) return s;
    let out = s.replace(_SECTION_HEAD, "");
    return out.length >= 8 ? out.trim() : s;
  }

  // Lever: every question is an li.application-question with its own .application-label. Reading
  // that label beats the input placeholder ("Type your response") and keeps radio option labels
  // ("Female Male Non-binary ...") out of the question text.
  const EEO_Q_RE = /\b(gender|pronoun|ethnic|race|hispanic|latin[oax]|disabilit|veteran|sexual orientation|transgender)\b/i;
  function leverLabel(el) {
    try {
      const li = el.closest && el.closest("li.application-question, .application-question");
      const lab = li && li.querySelector(".application-label");
      const t = lab ? clean(lab.innerText || lab.textContent || "").replace(/\s*[\u2731*]+\s*$/, "").trim() : "";
      return t.length >= 3 && t.length <= 400 ? t : "";
    } catch (_) { return ""; }
  }

  const ADDR_PART_RE = /^\s*(city|town|state|province|state\s*\/\s*province|region|zip|zip\s*code|postal|postal\s*code|postcode|country|apt|suite|unit|address\s*(line)?\s*2)\b/i;
  function questionFor(el) {
    const sel = clean(window.getSelection && String(window.getSelection()));
    const selUsable = el === (currentEl || lastEditable) && sel.length >= 12 && sel.length <= 500;
    // A highlighted selection only speaks for the field when the target is open-ended (essay /
    // contentEditable). For a one-line identity input (tel/email/text/number) a stray page
    // selection must NOT override the real label - that drafted a project story into a Mobile
    // Phone field. For one-line inputs the selection is a last resort only, used below iff no
    // label resolves.
    const openEnded = el.tagName === "TEXTAREA" || el.isContentEditable === true;
    if (selUsable && openEnded) return sel;
    const tries = [];
    const root = el.getRootNode ? el.getRootNode() : document;
    const lv = leverLabel(el); if (lv) tries.push(lv);      // Lever's own question label beats label[for]
    if (el.id) {
      let l = null;
      try { l = root.querySelector && root.querySelector('label[for="' + CSS.escape(el.id) + '"]'); } catch (_) {}
      if (l) tries.push(l.innerText);
    }
    const wrap = el.closest && el.closest("label"); if (wrap) tries.push(wrap.innerText);
    const lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) tries.push(lb.split(/\s+/).map((i) => (document.getElementById(i) || {}).innerText || "").join(" "));
    if (el.getAttribute) tries.push(el.getAttribute("aria-label"));
    // Split address rows (JazzHR/applytojob): one "Address" label over street + City / State /
    // Postal inputs that only have placeholders. With no label of its own, a placeholder that names
    // an address part is the question; the shared group label would fill the street everywhere.
    if (!tries.some((t) => clean(t || "")) && el.placeholder && ADDR_PART_RE.test(el.placeholder)) return clean(el.placeholder);
    const fs = el.closest && el.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg) tries.push(lg.innerText); }
    // SLDS / LWC: the question lives in an ancestor shadow root (c-as-apply-question,
    // *-question, radio-group). Climb host boundaries, read each shadow root's own text.
    let n = el, hops = 0;
    while (n && hops < 8) {
      const r = n.getRootNode && n.getRootNode();
      if (!r || !r.host) break;
      const host = r.host, tag = (host.tagName || "").toLowerCase();
      if (/apply-question|-question|radio-group|checkbox-group/.test(tag)) {
        const t = clean(r.textContent || "");
        if (t.length >= 8 && t.length <= 400) tries.push(t);
      }
      n = host; hops++;
    }
    // Bootstrap / horizontal forms (e.g. clearcompany): input has no id/for/aria-label and its real
    // <label class="control-label"> is a preceding sibling of an ANCESTOR (beside the input's column,
    // not the input). Climb <=4 ancestors, read a preceding control-label; a bare <label> only at a
    // shallow hop (form-group shape) so a section-header label above a deeper group is not grabbed.
    let anc = el, ah = 0;
    while (anc && ah < 4) {
      const prev = anc.previousElementSibling;
      if (prev) {
        const classHit = /control-label|col-\S*label/i.test(prev.className || "");
        if (classHit || (prev.tagName === "LABEL" && ah <= 2)) {
          const t = clean(prev.innerText);
          if (t.length >= 2 && t.length <= 120) { tries.push(t); break; }
        }
      }
      anc = anc.parentElement; ah++;
    }
    const cont = el.closest && el.closest('[class*="question" i], .application-question, [data-automation-id], [class*="field" i]');
    if (cont) {
      const h = cont.querySelector('label, [class*="label" i], legend, h1,h2,h3,h4');
      if (h && !h.contains(el)) tries.push(h.innerText);
    }
    let p = el.previousElementSibling, hh = 0;
    while (p && hh < 4) {
      const t = clean(p.innerText);
      if (t.length >= 12 && t.length <= 400) { tries.push(t); break; }
      p = p.previousElementSibling; hh++;
    }
    if (el.placeholder) tries.push(el.placeholder);
    const _HELPER = /^(optional|required|\(optional\)|\(required\))$/i;
    for (const t of tries) { const c = stripSectionHead(clean(t)); if (c && c.length >= 3 && !_HELPER.test(c)) return c.slice(0, 400); }
    // EEO selects (Hispanic/Latino, Veteran, Disability) sit under long VEVRAA/CC-305 preamble with
    // no resolvable label -> derive a canonical question from the OPTION contents so fields.yaml maps it.
    if (el.tagName === "SELECT" && el.options && el.options.length) {
      const blob = norm([...el.options].map((o) => o.textContent).join(" "));
      if (/hispanic|latino/.test(blob)) return "Are you Hispanic or Latino?";
      if (/protected veteran|veteran/.test(blob)) return "Veteran status";
      if (/disabilit|disabled/.test(blob)) return "Disability status";
    }
    // Last resort for hook-less field groups (e.g. Gem/jobs.gem.com): no id/for/aria/label
    // anywhere, and the question is a leading text sibling of an ANCESTOR (the field group wraps a
    // label span + the input). Climb <=4 ancestors; take the nearest preceding sibling that holds
    // short label text and does not itself contain a form control.
    let a2 = el, h2 = 0;
    while (a2 && h2 < 4) {
      let sib = a2.previousElementSibling, g2 = 0;
      while (sib && g2 < 3) {
        try {
          if (!(sib.querySelector && sib.querySelector("input, textarea, select"))) {
            const c2 = stripSectionHead(clean(sib.innerText || sib.textContent));
            if (c2 && c2.length >= 3 && c2.length <= 160 && !_HELPER.test(c2)) return c2.slice(0, 400);
          }
        } catch (_) {}
        sib = sib.previousElementSibling; g2++;
      }
      a2 = a2.parentElement; h2++;
    }
    if (selUsable) return sel;   // last resort: no label resolved -> use highlighted selection
    return "";
  }

  // For a radio/checkbox OPTION, questionFor() returns the option's own label (e.g. ">100"),
  // not the group's question. That sends the wrong text to the engine. Climb to the container
  // that holds the group's question heading and return that instead.
  function groupQuestion(el) {
    const lv = leverLabel(el); if (lv) return stripSectionHead(lv).slice(0, 240);
    // ARIA group label first: Workable wraps checkbox/radio sets in div[role=group] /
    // fieldset[role=radiogroup] with aria-labelledby pointing at the real question. Climbing
    // innerText instead glued option text onto the question (or returned the option alone).
    try {
      const g = el.closest && el.closest('[role="group"][aria-labelledby], [role="radiogroup"][aria-labelledby], fieldset[aria-labelledby]');
      if (g) {
        const t = g.getAttribute("aria-labelledby").split(/\s+/).map((i) => (document.getElementById(i) || {}).innerText || "").join(" ").trim();
        if (t && t.length > 5) return stripSectionHead(clean(t)).slice(0, 240);
      }
    } catch (_) {}
    let p = el;
    for (let i = 0; i < 9 && p; i++) {
      p = p.parentElement; if (!p) break;
      const t = clean(p.innerText || "");
      if (t.length > 15 && /\?|do you|are you|have you|which|what|size of|authoriz|identify|select|how (did|do|would)|please (select|describe)|describe/i.test(t)) {
        return stripSectionHead(t.replace(/\s*(Yes\s*No|No\s*Yes)\s*$/i, "")).slice(0, 240);
      }
    }
    return questionFor(el);   // fallback (already section-stripped)
  }

  function _pageCompanyName() {
    // Correctly-cased company name from the posting page: og:site_name, or
    // "Role @ Company" / "Role at Company" in the title. Fixes slug casing (1password -> 1Password).
    const site = document.querySelector('meta[property="og:site_name"]');
    if (site && site.content) { const c = clean(site.content).trim(); if (c && c.length <= 40) return c; }
    const t = clean(document.title || "");
    let m = t.match(/@\s*([^|\u2013\u2014:]+?)\s*$/) || t.match(/\bat\s+([A-Z0-9][^|\u2013\u2014:]+?)\s*$/);
    if (m && m[1]) { const c = m[1].trim().slice(0, 40); if (c) return c; }
    return null;
  }
  function companyGuess() {
    // The ATS URL slug is the most reliable company signal (page titles are noisy).
    const h = location.hostname, path = location.pathname;
    let m = null;
    if (/ashbyhq\.com$/i.test(h)) m = path.match(/^\/([^\/]+)/);
    else if (/greenhouse\.io$/i.test(h)) m = path.match(/^\/(?:embed\/job_app\?for=)?([^\/?]+)/);
    else if (/lever\.co$/i.test(h)) m = path.match(/^\/([^\/]+)/);
    else if (/myworkdayjobs\.com$/i.test(h)) m = h.match(/^([^.]+)\./);
    if (m && m[1] && !/^(embed|www|jobs|job-boards|boards|apply)$/i.test(m[1])) {
      let sname = decodeURIComponent(m[1]).replace(/[-_]+/g, " ").trim();
      if (sname && sname.length <= 40) {
        const cased = _pageCompanyName();
        if (cased && cased.replace(/\s+/g, "").toLowerCase() === sname.replace(/\s+/g, "").toLowerCase()) return cased;
        if (sname === sname.toLowerCase()) sname = sname.replace(/\b\w/g, (c) => c.toUpperCase());
        return sname;
      }
    }
    const og = document.querySelector('meta[property="og:site_name"], meta[property="og:title"]');
    const t = clean((og && og.content) || document.title);
    return t.split(/[|\-\u2013\u2014\u00b7:]/)[0].trim().slice(0, 80);
  }

  // A capped snapshot of the posting text so the engine can ground "why this company"
  // answers in REAL page facts instead of the model's guesses. Prefers the main content.
  function pageContext() {
    try {
      const root = document.querySelector('main, [role="main"], article') || document.body;
      let txt = (root && root.innerText) ? root.innerText : "";
      const md = document.querySelector('meta[name="description"], meta[property="og:description"]');
      txt = clean(document.title + ". " + ((md && md.content) || "") + ". " + txt);
      return txt.slice(0, 4000);
    } catch (_) { return ""; }
  }

  const _ROLE_JUNK = /^(application|apply|apply now|application questions|job application|careers?|open positions?|overview|home|jobs?)$/i;
  function roleGuess() {
    const t = clean(document.title);
    let r = "";
    let m = t.match(/(?:application|apply)\s+(?:for|:)\s+(.+?)\s+(?:at|@|-|\|)\s+/i);
    if (m) r = m[1];
    else { m = t.match(/^(.+?)\s+(?:at|@)\s+/i); if (m) r = m[1]; }
    if (!r) r = t.split(/[|\-–—·]/)[0].trim();
    r = clean(r).slice(0, 100);
    return _ROLE_JUNK.test(r) ? "" : r;   // don't send form chrome as the role
  }

  // ---- auto "applied" on the ATS confirmation (build 57) ----
  // You review and submit yourself; Penates never submits. After a fill on this page, the
  // ATS "thank you / application submitted" screen (same tab, same origin) logs the job as
  // applied once. Text that was already on the page at fill time never counts.
  const SUBMIT_TEXT_RE = /\b(thanks?( you)? for (applying|your application|submitting|your interest and application)|application (was |has been )?(successfully )?(submitted|received)|we(?:'|\u2019)?ve received your application|we have received your application|your application (is|was|has been) (in|complete|sent|submitted))\b/i;
  const SUBMIT_URL_RE = /(\/confirmation\b|\/thanks\b|\/thank-?you\b|application[_-]?(submitted|confirmation)|[?&](submitted|applied)=(true|1))/i;
  const FILL_KEY = "penates.lastFill";
  function pageText() { try { return (document.body && document.body.innerText || "").slice(0, 20000); } catch (_) { return ""; } }
  function rememberFill() {
    try {
      sessionStorage.setItem(FILL_KEY, JSON.stringify({ url: location.href, company: companyGuess(), role: roleGuess(),
        t: Date.now(), pre: SUBMIT_TEXT_RE.test(pageText()) }));
    } catch (_) {}
    watchSubmitted();
  }
  function lastFill() {
    try { const f = JSON.parse(sessionStorage.getItem(FILL_KEY) || "null"); return (f && Date.now() - f.t < 3 * 3600e3) ? f : null; }
    catch (_) { return null; }
  }
  let _subObs = null, _subTimer = null;
  async function checkSubmitted() {
    const f = lastFill();
    if (!f) return;
    const urlHit = SUBMIT_URL_RE.test(location.href) && location.href !== f.url;
    const textHit = !f.pre && SUBMIT_TEXT_RE.test(pageText());
    if (!urlHit && !textHit) return;
    try { sessionStorage.removeItem(FILL_KEY); } catch (_) {}
    if (_subObs) { _subObs.disconnect(); _subObs = null; }
    const payload = { url: f.url, company: f.company || companyGuess(), role: f.role || roleGuess() };
    try {
      const resp = await chrome.runtime.sendMessage({ type: "FETCH_APPLIED", payload });
      if (resp && resp.ok && resp.data && resp.data.ok) toast("Penates logged this as applied: " + (resp.data.company || "this role"));
      else toast("Submitted, but Penates could not log it (is serve.py running?)", true);
    } catch (e) { toast("Submitted, but Penates could not log it: " + e, true); }
  }
  function watchSubmitted() {
    if (_subObs || !lastFill() || !document.body) return;
    _subObs = new MutationObserver(() => { clearTimeout(_subTimer); _subTimer = setTimeout(checkSubmitted, 800); });
    _subObs.observe(document.body, { childList: true, subtree: true, characterData: true });
    setTimeout(() => { if (_subObs) { _subObs.disconnect(); _subObs = null; } }, 45 * 60e3);
  }
  if (CAN_SURFACE) setTimeout(() => { checkSubmitted(); watchSubmitted(); }, 1500);

  async function markApplied() {
    const payload = { url: location.href, company: companyGuess(), role: roleGuess() };
    try {
      const resp = await chrome.runtime.sendMessage({ type: "FETCH_APPLIED", payload });
      if (resp && resp.ok && resp.data && resp.data.ok) {
        toast("Logged as applied: " + (resp.data.company || "this role") + " (" + (resp.data.applied_at || "") + ")");
      } else {
        toast((resp && resp.error) || "Could not log (is serve.py running on :8765?)", true);
      }
    } catch (e) { toast("Could not log: " + e, true); }
  }

  // ---- server call (via background: dodges CORS / mixed-content / private-network) ----
  async function askEngine(question, limit, fresh, timeoutMs, bulk, options, prev) {
    const payload = { question, company: companyGuess(), role: roleGuess(), url: location.href, page_context: pageContext(), limit: limit || null, fresh: !!fresh, bulk: !!bulk, options: (options && options.length) ? options : null };
    // per-field Regenerate only: the draft being rejected, so the server writes a different one
    if (fresh && !bulk && prev) payload.prev = String(prev).slice(0, 600);
    const call = (async () => {
      try {
        const resp = await chrome.runtime.sendMessage({ type: "FETCH_ANSWER", payload });
        if (resp && resp.ok) return resp.data || null;
        return { __error: (resp && resp.error) || "serve.py unreachable on :8765" };
      } catch (err) {
        return { __error: String(err) };
      }
    })();
    // A single field must never hang the whole fill. Cap it and move on.
    if (!timeoutMs) return call;
    return Promise.race([call, sleep(timeoutMs).then(() => ({ __error: "timeout (" + Math.round(timeoutMs / 1000) + "s) -> you" }))]);
  }

  // ---- batch resolve --------------------------------------------------------------------
  // One request resolves EVERY question, instead of one content->service-worker round-trip per
  // field. 28+ sequential SW round-trips were the failure class: any one could stall on an MV3
  // service-worker eviction and freeze the whole fill (the "What brought you" combobox hang). One
  // call is ~28x less exposed, carries page_context once, and Promise-races a hard timeout.
  // serve.py /answer-batch returns {ok, answers:[{q, ...do_answer}|{q,ok:false,__error}], n, ms},
  // answers[i] === questions[i]. background.js proxies it as FETCH_ANSWER_BATCH.
  let _answerCache = null;   // Map<question, answer-record> for this fill; verify/heal reuse it
  async function askEngineBatch(questions, timeoutMs) {
    const payload = { questions, company: companyGuess(), role: roleGuess(), url: location.href,
                      page_context: pageContext(), bulk: true, bulk_skip_essays: true };
    const call = (async () => {
      try {
        const resp = await chrome.runtime.sendMessage({ type: "FETCH_ANSWER_BATCH", payload });
        if (resp && resp.ok) return resp.data || null;
        return { __error: (resp && resp.error) || "serve.py unreachable on :8765" };
      } catch (err) {
        return { __error: String(err) };
      }
    })();
    if (!timeoutMs) return call;
    return Promise.race([call, sleep(timeoutMs).then(() => ({ __error: "batch timeout" }))]);
  }
  // Cache-first single ask: the batch pre-pass fills _answerCache, so the apply loop / verify pass
  // read it with NO round-trip. A miss (a question the pre-pass didn't collect, e.g. a Yes/No
  // button group) or fresh:true (per-field Regenerate) falls back to the single /answer call.
  async function askCached(q, limit, fresh, timeoutMs, bulk) {
    if (!fresh && _answerCache && _answerCache.has(q)) return _answerCache.get(q);
    return askEngine(q, limit, fresh, timeoutMs, bulk);
  }

  // ---- resume attach ----------------------------------------------------------------
  // A content script can't read the local disk, but the background worker fetches the resume
  // bytes from serve.py (/resume) and we build a File and set it on the form's file input via
  // DataTransfer + change/drop events (the only script-driven way a browser accepts a file).
  async function askResume() {
    try {
      const resp = await chrome.runtime.sendMessage({ type: "FETCH_RESUME" });
      if (resp && resp.ok && resp.data && resp.data.ok) return resp.data; // {filename, mime, b64}
    } catch (_) {}
    return null;
  }
  function _b64ToBytes(b64) {
    const bin = atob(b64), len = bin.length, bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }
  function _nearText(el) {
    try { return clean((el.closest("div,label,fieldset,section") || {}).innerText || "").slice(0, 90); }
    catch (_) { return ""; }
  }
  async function attachResume(rows) {
    // File inputs are usually visually hidden behind a styled dropzone, so do NOT require
    // shown(); just find every type=file across the doc + shadow roots.
    let inputs = [];
    try {
      inputs = deepFields().filter((el) => el.tagName === "INPUT" && (el.type || "").toLowerCase() === "file" && !inPenates(el) && !el.disabled);
    } catch (_) {}
    if (!inputs.length) return;
    const data = await askResume();
    if (!data || !data.b64) { rows.push(["Resume upload", "skip", "no resume on server -> attach manually"]); return; }
    let file;
    try { file = new File([_b64ToBytes(data.b64)], data.filename || "resume.pdf", { type: data.mime || "application/pdf" }); }
    catch (e) { rows.push(["Resume upload", "skip", "build file: " + String(e).slice(0, 40)]); return; }
    let done = 0, label = "";
    // Pick the right slot(s): PREFER a positively resume-labeled input. If none are labeled and
    // there are several, take a doc-accepting / non-cover slot (else the first) - and NEVER blast
    // the resume into every file input. That blast is how it landed in the Cover Letter slot on
    // Greenhouse, whose hidden file input carries no "cover letter" text near it.
    const _rlbl = (inp) => ((inp.getAttribute("aria-label") || "") + " " + (inp.name || "") + " " + _nearText(inp)).toLowerCase();
    const _isResume = (inp) => /resume|\bcv\b|curriculum/.test(_rlbl(inp));
    const _isCover = (inp) => /cover letter|transcript|portfolio|photo|headshot/.test(_rlbl(inp));
    let targets;
    const _resumeSlots = inputs.filter(_isResume);
    if (_resumeSlots.length) targets = _resumeSlots;
    else if (inputs.length === 1) targets = inputs;
    else {
      const _nonCover = inputs.filter((i) => !_isCover(i));
      const _docish = _nonCover.find((i) => /\.pdf|\.doc|application\/pdf|msword/.test((i.getAttribute("accept") || "").toLowerCase()));
      targets = [_docish || _nonCover[0] || inputs[0]];
    }
    targets = targets.filter((inp) => !(_isCover(inp) && !_isResume(inp)));
    for (const inp of targets) {
      if (inp.files && inp.files.length) { done++; continue; } // already attached
      try {
        const dt = new DataTransfer(); dt.items.add(file);
        inp.files = dt.files;
        inp.dispatchEvent(new Event("input", { bubbles: true }));
        inp.dispatchEvent(new Event("change", { bubbles: true }));
        // some React dropzones only listen to 'drop', not the input's change
        try {
          const dz = inp.closest('[class*="drop" i],[class*="upload" i],[data-testid*="upload" i]') || inp.parentElement;
          if (dz) { const dt2 = new DataTransfer(); dt2.items.add(file);
            dz.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt2 })); }
        } catch (_) {}
        // verify it took
        if (inp.files && inp.files.length) { done++; label = data.filename || "attached"; }
      } catch (e) { rows.push(["Resume upload", "skip", String(e).slice(0, 50)]); }
    }
    if (done) rows.push(["Resume upload", "filled", label || (data.filename || "attached")]);
    else rows.push(["Resume upload", "review", "found upload field but couldn't set file -> attach manually"]);
  }

  // ============================ WHOLE-PAGE FILL ============================
  function deepFields() {
    const out = [], seen = new Set();
    const visit = (root) => {
      if (!root || !root.querySelectorAll) return;
      let list;
      try { list = root.querySelectorAll("input, textarea, select"); } catch (_) { return; }
      for (const el of list) { if (!seen.has(el)) { seen.add(el); out.push(el); } }
      for (const host of root.querySelectorAll("*")) { if (host.shadowRoot) visit(host.shadowRoot); }
      for (const f of root.querySelectorAll("iframe")) {
        let d = null; try { d = f.contentDocument; } catch (_) {}
        if (d) visit(d);
      }
    };
    visit(document);
    return out;
  }

  const SKIP_TYPES = /^(hidden|submit|button|image|reset|file|password)$/i;
  function shown(el) {
    try {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return true;
    } catch (_) {}
    // radios/checkboxes are often visually hidden but functional (SLDS)
    return el.tagName === "INPUT" && /^(radio|checkbox)$/i.test(el.type || "");
  }

  // self-verifying option commit (radio/checkbox): try label -> faux -> host -> native
  function commitOption(input) {
    if (input.checked) return true;
    const root = input.getRootNode ? input.getRootNode() : document;
    let lab = null;
    if (input.id) { try { lab = root.querySelector && root.querySelector('label[for="' + CSS.escape(input.id) + '"]'); } catch (_) {} }
    if (!lab && input.closest) lab = input.closest("label");
    if (lab) { lab.click(); if (input.checked) return true; }
    let host = input, h = 0;
    while (host && h < 6) {
      if (/^C-TS-(RADIO|CHECK-BOX)$/i.test(host.tagName || "")) break;
      const r = host.getRootNode(); if (!r || !r.host) { host = null; break; } host = r.host; h++;
    }
    const scope = host && host.shadowRoot ? host.shadowRoot : root;
    const faux = scope && scope.querySelector
      ? scope.querySelector('.slds-radio__label, .slds-checkbox__label, .slds-radio_faux, .slds-checkbox_faux, [class*="faux"]') : null;
    if (faux) { faux.click(); if (input.checked) return true; }
    if (host) { host.click(); if (input.checked) return true; }
    // Ashby radios/checkboxes are the real native inputs styled opacity:0 but 24x24 with
    // pointer-events:auto (verified live on the fixture - NOT 0x0, so they are never layout-
    // skipped). A native input.click() toggles and fires the React change handler directly; this
    // is what actually ticks them when the label association above doesn't reach the input.
    try { input.click(); if (input.checked) return true; } catch (_) {}
    try {
      input.checked = true;
      input.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (_) {}
    return !!input.checked;
  }

  function optionLabel(input) {
    // label[for], wrapping label, host text, or aria-label -> the option's visible text
    const root = input.getRootNode ? input.getRootNode() : document;
    if (input.id) { try { const l = root.querySelector('label[for="' + CSS.escape(input.id) + '"]'); if (l) return clean(l.innerText); } catch (_) {} }
    const w = input.closest && input.closest("label"); if (w) return clean(w.innerText);
    let host = input, h = 0;
    while (host && h < 4) {
      if (/^C-TS-(RADIO|CHECK-BOX)$/i.test(host.tagName || "")) { const t = clean(host.textContent); if (t) return t; }
      const r = host.getRootNode(); if (!r || !r.host) break; host = r.host; h++;
    }
    return clean(input.getAttribute("aria-label") || input.value || "");
  }

  function _optClickable(el) {
    // Fillable if the input has a layout box, OR it's a visually-hidden native input (opacity:0 /
    // sr-only) whose clickable LABEL is visible. Rippling styles its Yes/No radios this way, so the
    // old input-box-only skip dropped them entirely (sponsorship never got answered).
    const r = el.getBoundingClientRect();
    const inputVisible = !(el.offsetParent === null || getComputedStyle(el).display === "none" || (!r.width && !r.height));
    if (inputVisible) return true;
    let lab = null;
    try { lab = (el.id && el.getRootNode().querySelector('label[for="' + CSS.escape(el.id) + '"]')) || (el.closest && el.closest("label")); } catch (_) {}
    return !!(lab && shown(lab));
  }

  function fillSelect(el, value) {
    const want = norm(value);
    const opts = [...el.options];
    let hit = opts.find((o) => norm(o.textContent) === want || norm(o.value) === want)
           || opts.find((o) => norm(o.textContent).startsWith(want) && want.length >= 2)
           || opts.find((o) => want && norm(o.textContent).includes(want));
    if (!hit) return false;
    el.value = hit.value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return norm((el.selectedOptions[0] || {}).textContent) === norm(hit.textContent);
  }

  const CONSENT_RE = /consent|i agree|i acknowledge|i understand|terms|certif|i have read|authorize|electronic signature|e-?sign|privacy|background check|conditional on/i;

  // Fields the bulk fill must NOT auto-answer. Two kinds:
  //  - CONDITIONAL: "If you responded 'yes'/'other'..." follow-ups. The controlling Yes/No is
  //    usually unset, and answering them blind produces wrong-context matches (a "describe the
  //    relationship" box pulled an unrelated AI-tools answer) and fabricated specifics (an
  //    invented "how I found this posting").
  //  - LEAVE_BLANK: open optional prompts (accommodations, "anything else", additional info).
  //    These have no grounded answer; matching leaks the wrong value (race "White" landed in an
  //    accommodations box that merely said "other than your ethnicity") or dumps a random story.
  // Both are left for the human; the per-field popup still drafts one on demand.
  const CONDITIONAL_RE = /\bif you (responded|answered|selected|indicated|checked|chose)\b|\bif (yes|no|other|so|applicable|not|the above|you did)\b/i;
  const LEAVE_BLANK_RE = /accommodat|other than your|is there anything|anything (else|you.?d like|we should know)|additional (information|comments|details)|feel free to (add|share|include)|anything you would like to (share|add|tell)/i;
  const JUNK_RE = /skip to main content|english settings|^\s*settings\s*$|cookie(s| policy| preferences)|privacy statement|back to job posting|sign ?out|log ?out|page is loaded/i;
  // A self-contained "Have you X? If so, briefly explain" is a real question, not a follow-up to a
  // PRIOR question - do not let the "if so/if yes" conditional guard skip it.
  const _SELF_ASK = /^\s*(have|do|did|are|were|will|would|can|could|has|is there)\b/i;
  function skipQuestion(q) {
    if (!q) return false;
    const qc = String(q).replace(/\(\s*if (applicable|any)\s*\)/ig, " ");   // "What is your notice period (if applicable)?" is a real ask
    const cond = CONDITIONAL_RE.test(qc) && !_SELF_ASK.test(qc);
    return cond || LEAVE_BLANK_RE.test(q) || JUNK_RE.test(q);
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function isCombobox(el) {
    if (!el || el.tagName !== "INPUT") return false;
    if ((el.getAttribute("role") || "") === "combobox") return true;
    if ((el.getAttribute("aria-autocomplete") || "") === "list") return true;
    if ((el.getAttribute("data-automation-id") || "") === "searchBox") return true;   // Workday multiselect search
    return /select__input|combobox|autocomplete/i.test(el.className || "");
  }
  function matchOption(want) {
    let opts = [];
    try { opts = [...document.querySelectorAll('[role="option"]')].filter((n) => { try { return n.offsetParent && (n.textContent || "").trim(); } catch (_) { return false; } }); } catch (_) {}
    return opts.find((o) => norm(o.textContent) === want)
        || opts.find((o) => want.length >= 2 && norm(o.textContent).startsWith(want))
        || opts.find((o) => want && norm(o.textContent).includes(want))
        || (opts.length === 1 ? opts[0] : null);
  }
  // Paylocity (verified live 2026-10-06): address suggestions are plain <li> in #<id>-autocomplete-list
  // (no role=option); the State select renders rows #<id>-select-row-N inside #<id>-dropdown-list-container,
  // opens on mousedown of its control, and shows the chosen value in the control, not in input.value.
  function comboMenu(el) {
    for (const id of [el.getAttribute("aria-controls"), el.id && el.id + "-dropdown-list-container", el.id && el.id + "-autocomplete-list"]) {
      if (!id) continue;
      const m = document.getElementById(id);
      if (m) return { m, own: true };
    }
    const m = document.querySelector('[role="listbox"]');
    return m ? { m, own: false } : null;
  }
  function comboOptions(el) {
    const r = comboMenu(el);
    if (!r) return [];
    let o = [...r.m.querySelectorAll('[role="option"]')];
    if (!o.length && r.own) o = [...r.m.querySelectorAll('li, [id*="-select-row-"]')];
    return o;
  }
  function comboShown(el) {   // value as displayed: input value, else the control's own short text
    const v = (el.value || "").trim();
    if (v) return v;
    let n = el;
    for (let i = 0; i < 4 && n; i++) {
      n = n.parentElement;
      const t = (n && n.textContent || "").trim();
      if (t && t.length <= 60) return t;
      if (t.length > 60) break;
    }
    return "";
  }
  function comboCommitted(el) {
    try {   // Ant Design: the chosen value renders in .ant-select-selection-item on the .ant-select root
      const ant = el.closest && el.closest(".ant-select");
      if (ant) { const it = ant.querySelector(".ant-select-selection-item"); return !!(it && norm(it.getAttribute("title") || it.textContent)); }
    } catch (_) {}
    const ctrl = (el.closest && el.closest('[class*="control"], [class*="select"], [class*="combobox"]')) || el.parentElement;
    const sv = ctrl && ctrl.querySelector && ctrl.querySelector('[class*="single-value"], [class*="singleValue"], [class*="multi-value"], [class*="multiValue"]');
    return !!(sv && norm(sv.textContent));
  }
  function fireMouse(node) {
    for (const t of ["pointerdown", "mousedown", "mouseup", "click"]) {
      try { node.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })); } catch (_) {}
    }
  }
  const FIELD_Q = /field of study|area of study|course of study|\bmajor\b|discipline|concentration/i;
  const FIELD_ALTS = ["Computer Information Systems", "Computer and Information Science", "Information Systems",
                      "Computer Science", "Information Science", "Computer Information Technology"];
  async function fillCombobox(el, value) {
    // Ashby (and most react-select) comboboxes: open with ArrowDown, type to filter, then
    // click the option. Two things the old version missed and this handles: async typeahead
    // menus that show "Loading..." first (poll for real options), and picking the RIGHT
    // option among near-duplicates (token overlap, not first-substring - so a full
    // "City, State, Country" match beats a same-named city in a different state/country).
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    const query = (String(value).split(",")[0] || value).trim();
    const nv = norm(value);
    const vtokens = [...new Set(
      String(value).toLowerCase().split(/[\s/,|]+/).map((w) => w.replace(/[^a-z0-9]+/g, "")).filter((w) => w.length >= 2)
        .concat(nv.split(" ").filter((w) => w.length >= 2))
    )];
    el.focus();
    try {   // Ant selects open on mouse-down of the selector (read-only, non-searchable ones ignore typing)
      const antSel = el.closest && el.closest(".ant-select");
      if (antSel && el.getAttribute("aria-expanded") !== "true") { const sel = antSel.querySelector(".ant-select-selector") || antSel; sel.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window })); }
    } catch (_) {}
    try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })); } catch (_) {}
    await sleep(120);
    if (!el.readOnly) try { setter.call(el, query); el.dispatchEvent(new InputEvent("input", { bubbles: true, data: query, inputType: "insertText" })); } catch (_) {}
    // Workday multiselect search boxes (Field of Study, Skills, Country) only search on Enter.
    if (WD_HOST && !el.readOnly) { await sleep(150); try { for (const t of ["keydown", "keypress", "keyup"]) el.dispatchEvent(new KeyboardEvent(t, { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true })); } catch (_) {} }
    let opts = [];
    for (let i = 0; i < 16; i++) {
      await sleep(250);
      opts = comboOptions(el);
      if (!opts.length) { try { opts = [...document.querySelectorAll('[role="option"]')].filter((o) => o.offsetParent); } catch (_) {} }
      if (opts.length) break;
    }
    if (!opts.length) {
      // Typing the query can filter the menu to empty (e.g. "He/Him" is not a prefix of the option
      // text) -> reopen without a query and scan the full option list, then token-match.
      try { setter.call(el, ""); el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" })); } catch (_) {}
      try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })); } catch (_) {}
      if (!comboOptions(el).length) {   // selects that open only on mousedown of their control
        let n = el;
        for (let i = 0; i < 4 && n; i++) {
          n = n.parentElement; if (!n) break;
          try { n.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })); } catch (_) {}
          await sleep(200);
          if (comboOptions(el).length) break;
        }
      }
      for (let i = 0; i < 12; i++) {
        await sleep(200);
        opts = comboOptions(el);
        if (!opts.length) opts = [...document.querySelectorAll('[role="option"]')].filter((o) => o.offsetParent);
        if (opts.length) break;
      }
      if (!opts.length) return false;
    }
    // Ant Design selects (Dayforce and others): the [role=option] list is a hidden a11y shadow
    // (a few items, clicks do nothing). The real clickable rows are .ant-select-item-option.
    try {
      const ant = [...document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option')];
      if (ant.length) opts = ant;
    } catch (_) {}
    // pick the option sharing the most tokens with the wanted value (exact full match wins)
    let best = null, bestScore = 0;
    for (const o of opts) {
      const ot = norm(o.textContent);
      // Tokenize the OPTION the same way as the value (split on / , | and space) so a slashed
      // option like "He/him/his" -> [he,him,his] can overlap he/him. norm() alone collapses the
      // slashes into "hehimhis" and the whole-word match then fails - that is how "He/Him" got
      // committed as "She/her/hers" (all options scored 0, so it clicked the first one).
      const otokens = String(o.textContent).toLowerCase().split(/[\s/,|]+/).map((w) => w.replace(/[^a-z0-9]+/g, "")).filter(Boolean);
      let score = 0;
      for (const t of vtokens) { if (otokens.includes(t)) score += 2; else if (t.length >= 3 && ot.includes(t)) score += 1; }
      if (ot === nv) score += 100;
      // Tie-break on fewer extra words: "United States" -> "United States of America", not
      // "United States Minor Outlying Islands" (same overlap, more unrelated words).
      const extra = otokens.filter((t) => !vtokens.includes(t)).length;
      if (score > bestScore || (score === bestScore && score > 0 && extra < (best ? best.__extra : 1e9))) { bestScore = score; best = o; o.__extra = extra; }
    }
    // Nothing overlapped the wanted value -> do NOT blind-click the first option. Leave it for you.
    // Also reject weak overlap: need about half the value's tokens (one loose "use" hit inside
    // "No usage" is not a match; exact text gets +100).
    if (!best || bestScore < Math.max(2, vtokens.length)) { try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (_) {} return false; }
    const targetText = norm(best.textContent);
    // Commit order matters. Ashby's own autocomplete (role=combobox, class
    // ashby-application-form-input-autocomplete) renders results as clickable [role=option] divs
    // and leaves aria-activedescendant null, so the react-select keyboard path never "reaches" the
    // option and we would blind-skip it (this is why Current Location stayed empty). A real mouse
    // click on the chosen option element commits it, and the token-scored pick above avoids the
    // default-active foreign match (e.g. a same-named city abroad) - both verified live on the fixture. Click
    // first; fall back to keyboard nav for true react-select v5 widgets that ignore synthetic clicks.
    const _scope = (el.closest && el.closest("form")) || document;
    const _before = new Map([...(_scope.querySelectorAll ? _scope.querySelectorAll("input") : [])].filter((x) => x !== el).map((x) => [x, x.value]));
    const clickTarget = best;
    fireMouse(clickTarget);
    await sleep(260);
    // A closed menu alone is NOT success (menu also closes on blur with nothing picked). Accept a
    // committed chip, the input now showing the option (Workable/Ashby), or closed + input not
    // holding our typed query.
    const _v = norm(el.value || "");
    let _others = false;
    try { for (const [x, v0] of _before) if (x.isConnected && x.value !== v0) { _others = true; break; } } catch (_) {}
    if (_others && (!_v || targetText.startsWith(_v))) return true;   // picking it filled city/zip/etc.
    if (!_v && norm(comboShown(el)) === targetText) return true;      // value shows in the control
    if (comboCommitted(el) || _v === targetText || (el.getAttribute("aria-expanded") === "false" && (!_v || targetText.startsWith(_v)) && _v !== norm(query))) return true;
    // fallback: keyboard nav until the intended option is the active descendant, then Enter. Only
    // commit if we actually reached it - never blind-Enter onto whatever happens to be highlighted.
    let found = false;
    for (let k = 0; k < opts.length + 3; k++) {
      el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
      await sleep(110);
      const act = el.getAttribute("aria-activedescendant");
      const actEl = act ? document.getElementById(act) : null;
      if (actEl && norm(actEl.textContent) === targetText) { found = true; break; }
    }
    if (!found) { try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (_) {} return false; }
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await sleep(240);
    const _v2 = norm(el.value || "");
    return comboCommitted(el) || _v2 === targetText || (el.getAttribute("aria-expanded") === "false" && (!_v2 || targetText.startsWith(_v2)) && _v2 !== norm(query));
  }

  async function fillAriaCombobox(el, value) {
    // A div[role=combobox] with a listbox popup and NO inner <input> (Rippling / react-aria).
    // fillCombobox targets an <input> and types to filter; here we click to open, read the
    // controlled [role=option] list, token-score (same slash-aware scorer as the pronoun fix),
    // and click the best match. Never blind-clicks: no overlap -> Escape + false.
    const nv = norm(value);
    const vtokens = [...new Set(String(value).toLowerCase().split(/[\s/,|]+/).map((w) => w.replace(/[^a-z0-9]+/g, "")).filter((w) => w.length >= 2))];
    try { el.focus(); } catch (_) {}
    fireMouse(el);
    await sleep(150);
    if (el.getAttribute("aria-expanded") !== "true") { try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })); } catch (_) {} }
    let opts = [];
    for (let i = 0; i < 16; i++) {
      await sleep(200);
      const menu = document.getElementById(el.getAttribute("aria-controls") || "__none__") || document.querySelector('[role="listbox"]');
      opts = menu ? [...menu.querySelectorAll('[role="option"]')] : [...document.querySelectorAll('[role="option"]')].filter((o) => o.offsetParent);
      if (opts.length) break;
    }
    if (!opts.length) return false;
    let best = null, bestScore = 0;
    for (const o of opts) {
      const ot = norm(o.textContent);
      const otokens = String(o.textContent).toLowerCase().split(/[\s/,|]+/).map((w) => w.replace(/[^a-z0-9]+/g, "")).filter(Boolean);
      let score = 0;
      for (const t of vtokens) { if (otokens.includes(t)) score += 2; else if (t.length >= 3 && ot.includes(t)) score += 1; }
      if (ot === nv) score += 100;
      // Tie-break on fewer extra words: "United States" -> "United States of America", not
      // "United States Minor Outlying Islands" (same overlap, more unrelated words).
      const extra = otokens.filter((t) => !vtokens.includes(t)).length;
      if (score > bestScore || (score === bestScore && score > 0 && extra < (best ? best.__extra : 1e9))) { bestScore = score; best = o; o.__extra = extra; }
    }
    if (!best || bestScore < Math.max(2, vtokens.length)) { try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (_) {} return false; }
    fireMouse(best);
    await sleep(250);
    const after = norm(el.textContent || "");
    return el.getAttribute("aria-expanded") === "false" || (!!after && after !== "select");
  }

  // ---- Workday single-select "prompt" button (<button aria-haspopup="listbox">Select One</button>) --
  // Not role=combobox, so the ARIA combobox/radiogroup passes miss it. Open, read the real
  // promptOption labels, ask the engine constrained to them, click the match, VERIFY the button
  // text moved off "Select One". Single-select: no search box, no hierarchy.
  async function wdSelectFill(btn, q) {
    const vis = () => [...document.querySelectorAll('[data-automation-id="promptOption"], [role="option"], li[role="option"]')]
      .filter((o) => { try { return o.offsetParent !== null; } catch (_) { return false; } });
    const label = (o) => clean(o.getAttribute("data-automation-label") || o.textContent || "");
    const curText = () => clean(btn.textContent || "");
    const isPlaceholder = (t) => !t || /^select( one)?( required)?\.{0,3}$/i.test(t);
    try { fireMouse(btn); } catch (_) {}
    await sleep(400);
    let opts = vis(); if (!opts.length) { await sleep(400); opts = vis(); }
    const optLabels = [...new Set(opts.map(label).filter(Boolean))].slice(0, 30);
    const data = await askEngine(q, 25, false, 40000, false, optLabels.length ? optLabels : ["Yes", "No"]);
    if (!data || data.__error || !data.text) {
      try { btn.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (_) {}
      return { ok: false, detail: data && data.__error ? data.__error : "no value -> you" };
    }
    const na = norm(data.text);
    let hit = vis().find((o) => norm(label(o)) === na)
           || vis().find((o) => na && norm(label(o)).startsWith(na))
           || vis().find((o) => na && norm(label(o)).length >= 2 && na.startsWith(norm(label(o))));
    if (hit) { try { hit.scrollIntoView({ block: "center" }); } catch (_) {} try { fireMouse(hit); } catch (_) {} await sleep(300); }
    if (!isPlaceholder(curText())) return { ok: true, answer: data.text };
    try { btn.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (_) {}
    return { ok: false, detail: "'" + clean(data.text).slice(0, 20) + "' not selectable -> you" };
  }

  // Middle name (and similar optional identity bits) should only fill when the field is REQUIRED.
  // Bias toward NOT required: fill only on a clear signal, so an optional middle-name box stays blank.
  function _fieldRequired(el) {
    try {
      if (el.required) return true;
      if (el.getAttribute && el.getAttribute("aria-required") === "true") return true;
      const wrap = el.closest && el.closest('[data-automation-id^="formField-"], [class*="form-group" i], label');
      if (wrap) {
        const t = clean(wrap.innerText || wrap.textContent || "");
        if (t.length <= 120 && /(^|\s)\*|\brequired\b/i.test(t)) return true;
      }
    } catch (_) {}
    return false;
  }

  // ===== Workday multi-instance Work Experience filler =========================
  // Workday's live-verified id scheme: workExperience-<N>--<field>, where <N>
  // is a per-candidate counter (NOT 1-based). We read <N> off the DOM, never hardcode it.
  // Fills the repeating grid from your saved work_history (title/company/location/dates/desc/
  // currently-work-here) - the identity fields the generic per-field loop cannot map per row.
  const WD_HOST = /(^|\.)myworkdayjobs\.com$/i.test(location.hostname) || /(^|\.)workday\.com$/i.test(location.hostname);
  function wdExpIndices() {
    const out = [];
    for (const el of document.querySelectorAll('input[id^="workExperience-"][id$="--jobTitle"]')) {
      const m = (el.id || "").match(/^workExperience-(\d+)--jobTitle$/);
      if (m) out.push(m[1]);
    }
    return out;
  }
  function wdSetById(id, val) {
    if (val == null || val === "") return false;
    const el = document.getElementById(id);
    if (!el) return false;
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    try {
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, String(val));
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("blur", { bubbles: true }));
      return true;
    } catch (_) { return false; }
  }
  function wdSetCurrent(idx, want) {
    const el = document.getElementById("workExperience-" + idx + "--currentlyWorkHere");
    if (!el) return false;
    try { if (!!el.checked !== !!want) el.click(); return !!el.checked === !!want; } catch (_) { return false; }
  }
  async function wdFillDate(idx, which, mmYYYY) {
    if (!mmYYYY || !/\//.test(String(mmYYYY))) return false;
    let [mm, yyyy] = String(mmYYYY).split("/").map((x) => x.trim());
    mm = (mm || "").padStart(2, "0");
    const base = "workExperience-" + idx + "--" + which;
    const monthIn = document.getElementById(base + "-dateSectionMonth-input");
    const yearIn  = document.getElementById(base + "-dateSectionYear-input");
    if (!monthIn || !yearIn) return false;
    const setSpin = (el, v) => {
      try {
        el.focus();
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, v);
        el.dispatchEvent(new InputEvent("input", { bubbles: true, data: v, inputType: "insertText" }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        el.dispatchEvent(new Event("blur", { bubbles: true }));
      } catch (_) {}
    };
    setSpin(monthIn, mm); await sleep(70);
    setSpin(yearIn, yyyy); await sleep(70);
    try {
      const md = ((document.getElementById(base + "-dateSectionMonth-display") || {}).textContent || monthIn.value || "").trim();
      const yd = ((document.getElementById(base + "-dateSectionYear-display")  || {}).textContent || yearIn.value  || "").trim();
      return md.replace(/^0+/, "") === mm.replace(/^0+/, "") && yd === yyyy;
    } catch (_) { return false; }
  }
  function wdAddExperienceBtn() {
    for (const b of document.querySelectorAll('button[data-automation-id="add-button"]')) {
      let n = b, h = "";
      for (let k = 0; k < 12 && n; k++) { n = n.parentElement; if (!n) break;
        const el = n.querySelector && n.querySelector("h2,h3,h4,[role=heading]");
        if (el) { h = el.textContent || ""; break; } }
      if (/work experience/i.test(h)) return b;
    }
    return null;
  }
  function _wdYamlVal(v) {
    let x = (v == null ? "" : String(v)).trim();
    if ((x.startsWith("'") && x.endsWith("'")) || (x.startsWith('"') && x.endsWith('"'))) x = x.slice(1, -1).replace(/''/g, "'");
    if (x === "true") return true;
    if (x === "false") return false;
    return x;
  }
  // ---- home address from profile.yaml (identity.street/city_only/state/zip) ----
  const US_STATE = { alabama: "AL", alaska: "AK", arizona: "AZ", arkansas: "AR", california: "CA", colorado: "CO",
    connecticut: "CT", delaware: "DE", "district of columbia": "DC", florida: "FL", georgia: "GA", hawaii: "HI",
    idaho: "ID", illinois: "IL", indiana: "IN", iowa: "IA", kansas: "KS", kentucky: "KY", louisiana: "LA",
    maine: "ME", maryland: "MD", massachusetts: "MA", michigan: "MI", minnesota: "MN", mississippi: "MS",
    missouri: "MO", montana: "MT", nebraska: "NE", nevada: "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", ohio: "OH", oklahoma: "OK",
    oregon: "OR", pennsylvania: "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    tennessee: "TN", texas: "TX", utah: "UT", vermont: "VT", virginia: "VA", washington: "WA",
    "west virginia": "WV", wisconsin: "WI", wyoming: "WY" };
  const stateAbbr = (v) => US_STATE[norm(v || "")] || "";
  let _homeAddr = null;
  async function homeAddress() {
    if (_homeAddr) return _homeAddr;
    let y = "";
    try { const r = await chrome.runtime.sendMessage({ type: "FETCH_PROFILE" }); y = (r && r.ok && r.data && r.data.profile) || ""; } catch (_) {}
    const g = (k) => { const m = y.match(new RegExp("^\\s+" + k + ":\\s*(.+)$", "m")); return m ? m[1].replace(/\s+#.*$/, "").replace(/^['"]|['"]$/g, "").trim() : ""; };
    _homeAddr = { street: g("street"), city: g("city_only"), state: g("state"), zip: g("zip") };
    _homeAddr.abbr = stateAbbr(_homeAddr.state);
    return _homeAddr;
  }
  // "Saint Louis" (geocoder) == "St. Louis" (profile); same for Mount/Fort.
  const _addrNorm = (v) => norm(v).replace(/\bsaint\b/g, "st").replace(/\bmount\b/g, "mt").replace(/\bfort\b/g, "ft");
  const sameAddr = (a, b) => { a = _addrNorm(a); b = _addrNorm(b); return !!a && !!b && (a === b || a.startsWith(b) || b.startsWith(a)); };

  function parseWorkHistoryYaml(text) {
    const jobs = []; let cur = null;
    for (const raw of String(text || "").split(/\r?\n/)) {
      if (!raw.trim() || /^\s*#/.test(raw) || /^jobs:\s*$/.test(raw)) continue;
      const mNew = raw.match(/^-\s+([a-z_]+):\s?(.*)$/i);
      const mKey = raw.match(/^\s+([a-z_]+):\s?(.*)$/i);
      if (/^-\s*$/.test(raw)) { cur = {}; jobs.push(cur); continue; }
      if (mNew) { cur = {}; jobs.push(cur); cur[mNew[1]] = _wdYamlVal(mNew[2]); continue; }
      if (mKey && cur) { cur[mKey[1]] = _wdYamlVal(mKey[2]); }
    }
    return jobs.filter((j) => j.title || j.company);
  }
  async function fetchWorkHistory() {
    let resp = null;
    try { resp = await chrome.runtime.sendMessage({ type: "FETCH_PROFILE" }); } catch (_) { return []; }
    if (!resp || !resp.ok || !resp.data) return [];
    const d = resp.data;
    if (Array.isArray(d.work_history_json)) return d.work_history_json;   // preferred, if serve.py provides it
    return parseWorkHistoryYaml(d.work_history || "");
  }
  // ---- References block (Dayforce reference_<n>_<field>, or any section headed "References") ----
  // These fields ask about SOMEONE ELSE. They must never get the candidate's own name/email/phone,
  // so every field in a reference block is claimed here (skipped by the generic scan) and filled
  // only from profile.references[n]. No saved reference -> left blank for you.
  function refIndex(el) {
    const m = ((el.id || "") + " " + (el.name || "")).match(/references?[_\-\[\.]*(\d+)/i);
    return m ? +m[1] : null;
  }
  function inReferenceBlock(el) {
    if (refIndex(el) !== null) return true;
    if (/(^|[_\-\.\[])references?([_\-\.\]]|$)/i.test((el.id || "") + " " + (el.name || ""))) return true;
    let n = el;
    for (let i = 0; i < 10 && n; i++) {
      n = n.parentElement; if (!n) break;
      const h = n.querySelector(":scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > legend, :scope > header h2, :scope > header h3");
      if (h) return /^\s*(professional\s+|personal\s+)?references?\b/i.test(h.innerText || "") && !/referr/i.test(h.innerText || "");
    }
    return false;
  }
  function _refLabel(el) {
    const lab = (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest("label");
    return norm((lab && lab.innerText) || el.getAttribute("aria-label") || el.placeholder || (el.id || "").replace(/.*_/, "")) + " " + norm((el.id || "").replace(/.*_/, "").replace(/([a-z])([A-Z])/g, "$1 $2"));
  }
  function _refValue(ref, label) {
    const name = String(ref.name || "").trim(), parts = name.split(/\s+/);
    if (/middle/.test(label)) return "";
    if (/first\s*name|given/.test(label)) return parts[0] || "";
    if (/last\s*name|surname|family/.test(label)) return parts.length > 1 ? parts.slice(1).join(" ") : "";
    if (/full\s*name|\bname\b/.test(label)) return name;
    if (/relationship/.test(label)) return ref.relationship || "";
    if (/company|employer|organi[sz]ation/.test(label)) return ref.company || "";
    if (/title|position/.test(label)) return ref.title || "";
    if (/e\s*mail/.test(label)) return ref.email || "";
    if (/phone|mobile|cell|telephone/.test(label)) return ref.phone || "";
    if (/years|how long|known/.test(label)) return String(ref.years_known || "");
    return "";
  }
  function _refFields() {
    return deepFields().filter((el) => !inPenates(el) && shown(el) && /^(INPUT|TEXTAREA)$/.test(el.tagName)
      && !/^(hidden|submit|button|file|checkbox|radio)$/i.test(el.type || "") && inReferenceBlock(el));
  }
  function _refBlocks(els) {
    const blocks = new Map();
    els.forEach((el) => { const i = refIndex(el) ?? 0; if (!blocks.has(i)) blocks.set(i, []); blocks.get(i).push(el); });
    return blocks;
  }
  function _refAddButton(anyRefEl) {
    // The "Add Reference" control lives in the same section as the reference fields.
    let n = anyRefEl;
    for (let i = 0; i < 12 && n; i++) {
      n = n.parentElement; if (!n) break;
      const b = [...n.querySelectorAll('button, a, [role="button"]')].find((x) => /^\s*\+?\s*add\s+(another\s+)?reference\b/i.test(x.innerText || x.textContent || ""));
      if (b) return b;
    }
    return null;
  }
  async function fillReferences(rows, handled, late) {
    let els = _refFields();
    if (!els.length) return { filled: 0 };
    els.forEach((el) => handled && handled.add(el));            // never let the generic scan touch them
    let refs = [];
    try { const r = await chrome.runtime.sendMessage({ type: "FETCH_PROFILE" }); refs = (r && r.ok && r.data && r.data.references) || []; } catch (_) {}
    refs = refs.filter((x) => x && String(x.name || "").trim());
    // Add blocks until there is one per saved reference (first pass only; max 5 clicks).
    if (refs.length) {                                           // late pass too: redraws can drop added blocks
      for (let tries = 0; tries < 5 && _refBlocks(els).size < refs.length; tries++) {
        const btn = _refAddButton(els[0]);
        if (!btn) break;
        const before = els.length;
        fireMouse(btn);
        for (let w = 0; w < 15 && _refFields().length <= before; w++) await sleep(200);
        els = _refFields();
        els.forEach((el) => handled && handled.add(el));
        if (els.length <= before) break;                            // the button did nothing: stop
      }
    }
    const blocks = _refBlocks(els);
    let filled = 0;
    for (const [i, list] of [...blocks.entries()].sort((a, b) => a[0] - b[0])) {
      const ref = refs[i];
      if (!ref) { if (!late) rows.push(["Reference " + (i + 1), "skip", refs.length ? "only " + refs.length + " saved -> you" : "no references in your profile -> you"]); continue; }
      let n = 0;
      for (const el of list) {
        if (clean(el.value)) continue;
        const v = _refValue(ref, _refLabel(el));
        if (v && _adSetText(el, v)) n++;
      }
      if (late) { if (n) rows.push(["Reference " + (i + 1), "filled", ref.name + " (re-filled " + n + " after page redraw)"]); }
      else rows.push(["Reference " + (i + 1), n ? "filled" : "skip", n ? ref.name + " (" + n + " fields)" : "nothing to fill"]);
      if (n) filled++;
    }
    return { filled };
  }
  // ===== Start-date fields ======================================================
  // Rule (the user): free text -> "2 weeks from offer" (profile, server side); an actual DATE field ->
  // the second Monday from today. Covers Workday Month/Day/Year spinners, <input type=date> and
  // MM/DD/YYYY text boxes whose label asks when you can start.
  const START_Q = /when (could|can|would) you (start|begin)|start date|earliest (start|available|date)|available to start|availability date|date (you are )?available|begin work|able to start|join date/i;
  function secondMonday() {
    const d = new Date(); d.setHours(0, 0, 0, 0);
    const add = ((8 - d.getDay()) % 7) || 7;          // days to the next Monday (today Monday -> +7)
    d.setDate(d.getDate() + add + 7);
    return d;
  }
  const _p2 = (n) => String(n).padStart(2, "0");
  function _setVal(el, v) {
    try {
      el.focus();
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, v);
      el.dispatchEvent(new InputEvent("input", { bubbles: true, data: v, inputType: "insertText" }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("blur", { bubbles: true }));
      return true;
    } catch (_) { return false; }
  }
  function _fieldLabel(el) {
    const ff = el.closest && (el.closest('[data-automation-id^="formField"]') || el.closest("fieldset") || el.closest('[role="group"]'));
    if (ff) {
      const l = ff.querySelector("label, legend, [data-automation-id='richText']");
      const t = clean((l && l.innerText) || (ff.innerText || "").split("\n")[0]);
      if (t) return t;
    }
    try { return questionFor(el) || ""; } catch (_) { return ""; }
  }
  async function fillStartDates(rows, handled) {
    const d = secondMonday();
    const mm = _p2(d.getMonth() + 1), dd = _p2(d.getDate()), yyyy = String(d.getFullYear());
    const shownDate = mm + "/" + dd + "/" + yyyy;
    let n = 0;
    // Workday spinners (skip the work-experience / education grids, filled elsewhere)
    for (const mIn of document.querySelectorAll('input[id$="dateSectionMonth-input"]')) {
      if (/^(workExperience|education)-/.test(mIn.id) || inPenates(mIn)) continue;
      const q = _fieldLabel(mIn);
      if (!START_Q.test(q)) continue;
      const base = mIn.id.replace(/dateSectionMonth-input$/, "");
      const dIn = document.getElementById(base + "dateSectionDay-input");
      const yIn = document.getElementById(base + "dateSectionYear-input");
      if (clean(mIn.value) && yIn && clean(yIn.value)) { [mIn, dIn, yIn].forEach((e) => e && handled && handled.add(e)); continue; }
      _setVal(mIn, mm); await sleep(70);
      if (dIn) { _setVal(dIn, dd); await sleep(70); }
      if (yIn) { _setVal(yIn, yyyy); await sleep(70); }
      [mIn, dIn, yIn].forEach((e) => e && handled && handled.add(e));
      rows.push([q.slice(0, 80), "filled", shownDate + " (2nd Monday from today)"]); n++;
    }
    // Native date inputs and MM/DD/YYYY text boxes
    for (const el of deepFields()) {
      if (inPenates(el) || (handled && handled.has(el)) || el.tagName !== "INPUT" || clean(el.value) || inEmploymentBlock(el)) continue;
      const typ = (el.type || "").toLowerCase();
      const ph = (el.getAttribute("placeholder") || "") + " " + (el.getAttribute("aria-label") || "");
      const isDate = typ === "date" || /\b(mm|dd)\s*[\/\-.]\s*(dd|mm)\s*[\/\-.]\s*(yyyy|yy)\b/i.test(ph);
      if (!isDate) continue;
      let q = ""; try { q = questionFor(el) || ""; } catch (_) {}
      if (!START_Q.test(q)) continue;
      const v = typ === "date" ? yyyy + "-" + mm + "-" + dd : (/^\s*dd/i.test(ph) ? dd + "/" + mm + "/" + yyyy : shownDate);
      if (_setVal(el, v)) { handled && handled.add(el); rows.push([q.slice(0, 80), "filled", v + " (2nd Monday from today)"]); n++; }
    }
    return n;
  }

  async function fillWorkdayExperience(rows, handled) {
    if (!WD_HOST) return { filled: 0, review: 0 };
    let jobs = [];
    try { jobs = await fetchWorkHistory(); } catch (_) {}
    if (!jobs || !jobs.length) return { filled: 0, review: 0 };
    let existing = wdExpIndices();
    if (!existing.length && !wdAddExperienceBtn()) return { filled: 0, review: 0 };   // no WE section on this form
    let filled = 0, review = 0;
    for (let i = 0; i < jobs.length; i++) {
      const j = jobs[i]; let idx;
      try {
        if (i < existing.length) { idx = existing[i]; }
        else {
          const before = new Set(wdExpIndices());
          const addBtn = wdAddExperienceBtn(); if (!addBtn) break;
          try { addBtn.scrollIntoView({ block: "center" }); addBtn.click(); } catch (_) { break; }
          await sleep(900);
          const fresh = wdExpIndices().filter((x) => !before.has(x));
          if (!fresh.length) break;
          idx = fresh[fresh.length - 1]; existing = wdExpIndices();
        }
        const setF = (suffix, val) => {
          const id = "workExperience-" + idx + "--" + suffix;
          const ok = wdSetById(id, val);
          const el = document.getElementById(id); if (el && handled) handled.add(el);
          return ok;
        };
        setF("jobTitle", j.title);
        setF("companyName", j.company);
        setF("location", j.location);
        setF("roleDescription", (j.description || "").toString().trim());
        const sd = await wdFillDate(idx, "startDate", j.start);
        let ed;
        if (j.current) { wdSetCurrent(idx, true); ed = true; }
        else { wdSetCurrent(idx, false); ed = await wdFillDate(idx, "endDate", j.end); }
        for (const suf of ["startDate-dateSectionMonth-input", "startDate-dateSectionYear-input",
                           "endDate-dateSectionMonth-input", "endDate-dateSectionYear-input", "currentlyWorkHere"]) {
          const el = document.getElementById("workExperience-" + idx + "--" + suf); if (el && handled) handled.add(el);
        }
        const dOK = sd && ed;
        if (dOK) filled++; else review++;
        rows.push(["Work Exp " + (i + 1) + ": " + clean(j.title || "") + " @ " + clean(j.company || ""),
                   dOK ? "filled" : "review", dOK ? "from your history" : "text set - check the dates"]);
      } catch (e) {
        rows.push(["Work Exp " + (i + 1), "skip", String(e).slice(0, 50)]); 
      }
    }
    // Workday's "autofill with resume" pre-creates blocks from its own resume parse. Blocks past
    // our saved history are duplicates: delete them (form edit only, nothing submitted); if no
    // Delete control is found, fence their fields off so the generic scan doesn't grind on them.
    const extra = wdExpIndices().slice(jobs.length).reverse();
    let removed = 0, left = 0;
    for (const idx of extra) {
      const title = document.getElementById("workExperience-" + idx + "--jobTitle");
      let box = title, del = null;
      for (let k = 0; k < 14 && box && !del; k++) {
        box = box.parentElement; if (!box) break;
        const others = [...box.querySelectorAll('[id^="workExperience-"]')].some((x) => !x.id.startsWith("workExperience-" + idx + "--"));
        if (others) break;
        del = [...box.querySelectorAll('button, [role="button"]')].find((x) =>
          /\bdelete\b|\bremove\b/i.test((x.getAttribute("aria-label") || "") + " " + (x.innerText || x.textContent || "")));
      }
      if (del) {
        try { del.scrollIntoView({ block: "center" }); del.click(); await sleep(500); } catch (_) {}
        if (!document.getElementById("workExperience-" + idx + "--jobTitle")) { removed++; continue; }
      }
      left++;
      document.querySelectorAll('[id^="workExperience-' + idx + '--"]').forEach((el) => handled && handled.add(el));
    }
    if (removed) rows.push(["Extra Workday work-experience blocks", "filled", "removed " + removed + " duplicate(s) from Workday's resume parse"]);
    if (left) rows.push(["Extra Workday work-experience blocks", "review", left + " left from Workday's resume parse -> delete them"]);
    return { filled, review };
  }

  // Workday Education often renders collapsed (just an "Add" button, no fields) until you add an
  // entry. Materialize one block so the generic loop's education field mappings can fill it.
  // Existence is checked by field LABEL (not id prefix) so we never create a duplicate block.
  async function wdEnsureEducation() {
    if (!WD_HOST) return false;
    const _hasEduField = () => {
      try {
        for (const el of deepFields()) {
          if (inPenates(el) || !shown(el)) continue;
          const q = questionFor(el) || "";
          if (/\b(school|university|institution|college|degree|field of study|area of study)\b/i.test(q)) return true;
        }
      } catch (_) {}
      return false;
    };
    if (_hasEduField()) return false;                     // a block is already present
    let btn = null;
    for (const b of document.querySelectorAll('button[data-automation-id="add-button"]')) {
      let n = b, h = "";
      for (let k = 0; k < 12 && n; k++) { n = n.parentElement; if (!n) break;
        const el = n.querySelector && n.querySelector("h2,h3,h4,[role=heading]");
        if (el) { h = el.textContent || ""; break; } }
      if (/education/i.test(h)) { btn = b; break; }
    }
    if (!btn) return false;
    try { btn.scrollIntoView({ block: "center" }); btn.click(); } catch (_) { return false; }
    await sleep(900);
    return _hasEduField();
  }


  // ===== ATS ADAPTERS (Greenhouse, Workable) ===================================================
  // Dedicated path, the way Simplify-class tools work: read the form's own SCHEMA (every question,
  // required flag, field id and option list) from the ATS's public API, send ONE /answer-batch
  // with kind + options per field, then fill each field by its known id and verify it took.
  // Choice fields never go through the essay ladder (server constrains to the options). No DOM
  // guessing of labels, no per-field engine round-trips, one report row per question.
  async function _bgJson(url) {
    try {
      const resp = await chrome.runtime.sendMessage({ type: "FETCH_JSON", url });
      return resp && resp.ok ? resp.data : null;
    } catch (_) { return null; }
  }
  async function adapterSchema() {
    const href = location.href;
    // --- Greenhouse (job-boards / boards / embed?for=&token=) ---
    let gh = null;
    const m1 = href.match(/greenhouse\.io\/([^\/?#]+)\/jobs\/(\d+)/i);
    if (m1 && !/^embed$/i.test(m1[1])) gh = [m1[1], m1[2]];
    if (!gh && /greenhouse\.io$/i.test(location.hostname)) {
      try { const u = new URL(href); const f = u.searchParams.get("for"), t = u.searchParams.get("token") || u.searchParams.get("gh_jid"); if (f && t) gh = [f, t]; } catch (_) {}
    }
    if (gh) {
      const r = await _bgJson("https://boards-api.greenhouse.io/v1/boards/" + gh[0] + "/jobs/" + gh[1] + "?questions=true");
      if (!r || !Array.isArray(r.questions)) return null;
      const items = [];
      const add = (q) => {
        for (const f of (q && q.fields) || []) {
          if (f.type === "input_file" || /^(resume_text|cover_letter_text)$/.test(f.name)) continue;
          const kind = { input_text: "text", textarea: "textarea", multi_value_single_select: "select", multi_value_multi_select: "checkbox" }[f.type];
          if (!kind) continue;
          items.push({ ats: "greenhouse", id: f.name, q: clean(q.label || ""), required: !!q.required, kind,
                       options: (f.values || []).map((v) => clean(String(v.label))).filter(Boolean) });
        }
      };
      (r.questions || []).forEach(add);
      (r.location_questions || []).forEach(add);
      // Greenhouse renders a Hispanic/Latino Yes/No select (id hispanic_ethnicity) that the API's
      // compliance list omits; Race only appears after it is answered. Ask it first.
      if (document.getElementById("hispanic_ethnicity") && !items.some((i) => i.id === "hispanic_ethnicity"))
        items.push({ ats: "greenhouse", id: "hispanic_ethnicity", q: "Are you Hispanic/Latino?", required: false, kind: "select", options: null });
      (r.compliance || []).forEach((c) => (c.questions || []).forEach(add));
      // Page-level widgets the schema does not list (phone country, location typeahead, education).
      const extra = [["country", "Country"], ["candidate-location", "Location (City)"], ["school--0", "School"], ["degree--0", "Degree"], ["discipline--0", "Discipline"]];
      for (const [id, q] of extra) {
        const el = document.getElementById(id);
        if (el && !items.some((i) => i.id === id)) items.push({ ats: "greenhouse", id, q, required: _fieldRequired(el), kind: "combobox", options: null });
      }
      return items;
    }
    // --- Workable ---
    const m2 = location.pathname.match(/\/j\/([A-Z0-9]{6,})/i);
    if (/(^|\.)workable\.com$/i.test(location.hostname) && m2) {
      let s = null;
      try { s = await fetch("/api/v1/jobs/" + m2[1] + "/form", { credentials: "include" }).then((x) => (x.ok ? x.json() : null)); } catch (_) {}
      if (!Array.isArray(s)) return null;
      const items = [];
      for (const sec of s) for (const f of (sec.fields || [])) {
        let kind = { text: "text", email: "text", phone: "text", paragraph: "textarea", boolean: "radio", dropdown: "select", multiple: "checkbox",
                     location: "location", address: "location" }[f.type];
        if (!kind && /\b(address|location|city)\b/i.test(f.label || "")) kind = "location";   // Places typeahead
        if (!kind) continue;
        if (f.type === "multiple" && f.singleOption) kind = "radio";
        const options = f.type === "boolean" ? ["Yes", "No"] : (f.options || []).map((o) => clean(String(o.value || ""))).filter(Boolean);
        items.push({ ats: "workable", id: f.id, q: clean(f.label || ""), required: !!f.required, kind, options: options.length ? options : null,
                     optIds: (f.options || []).map((o) => String(o.name)) });
      }
      return items;
    }
    return null;
  }
  function _adEl(it) {
    const id = it.id;
    return document.getElementById(id)
      || document.getElementById("input_" + id + "_input")
      || document.querySelector('[name="' + CSS.escape(id) + '"]:not([type="hidden"])')
      || document.querySelector('[data-ui="' + CSS.escape(id) + '"] input:not([type="hidden"]), [data-ui="' + CSS.escape(id) + '"] textarea');
  }
  function _adSetText(el, val) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    try {
      el.focus();
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, String(val));
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("blur", { bubbles: true }));
    } catch (_) {}
    const a = clean(el.value), b = clean(String(val));
    if (a === b) return true;
    // Phone widgets (intl-tel-input) reformat the number: compare digits.
    const da = a.replace(/\D+/g, ""), db = b.replace(/\D+/g, "");
    return db.length >= 7 && (da === db || da.endsWith(db) || db.endsWith(da));
  }
  // Known-option select (Greenhouse react-select, Workable combobox): open by mouse-down on the
  // control, click the EXACT option, verify the shown value. No typing, no keyboard nav.
  async function _adFillSelect(el, pick) {
    const want = norm(pick);
    const shownVal = () => {
      const box = el.closest('.select__container, .select-shell, .select, [data-ui]') || el.parentElement;
      const sv = box && box.querySelector('[class*="single-value"], [class*="singleValue"]');
      return norm((sv && sv.textContent) || el.value || "");
    };
    if (shownVal() === want) return true;
    try { const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; if (el.value) { setter.call(el, ""); el.dispatchEvent(new Event("input", { bubbles: true })); } } catch (_) {}
    const ctrl = el.closest('.select__control, [class*="control"]') || el.parentElement;
    for (let attempt = 0; attempt < 2; attempt++) {
      try { el.focus(); } catch (_) {}
      fireMouse(attempt ? el : ctrl);
      let opts = [];
      for (let i = 0; i < 20; i++) {
        await sleep(60);
        const lb = document.getElementById(el.getAttribute("aria-controls") || el.getAttribute("aria-owns") || "__none__");
        opts = lb ? [...lb.querySelectorAll('[role="option"]')] : [];
        if (opts.length) break;
      }
      let o = opts.find((x) => norm(x.textContent) === want);
      if (!o && want) {
        // Word-level match for free answers vs fixed menus ("Associate" -> "Associate's Degree",
        // "United States" -> "United States +1"): EVERY answer word must start an option word;
        // fewest extra words wins. Never a partial overlap.
        const wt = want.split(" ").filter(Boolean);
        let best = null, bestExtra = 1e9;
        for (const x of opts) {
          const ot = norm(x.textContent).split(" ").filter(Boolean);
          if (wt.every((w) => ot.some((t) => t.startsWith(w))) && ot.length - wt.length < bestExtra) { best = x; bestExtra = ot.length - wt.length; }
        }
        o = best;
      }
      if (o) { const _ot = norm(o.textContent); fireMouse(o); await sleep(180); const _sv = shownVal(); if (_sv === want || _sv === _ot) return true; }
      try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (_) {}
    }
    return fillCombobox(el, pick);   // last resort: the generic typeahead path
  }
  function _adPickOptions(it, text) {
    // Map the engine's text (one option, or several joined by " | ") to option labels. Exact
    // (case/punct-normalized) only: never a prefix, so "No" can't hit "No Kubernetes: ...".
    const wanted = String(text || "").split(/\s*\|\s*/).map(norm).filter(Boolean);
    return (it.options || []).filter((o) => wanted.includes(norm(o)));
  }
  // ---- Location / Places typeahead (Workable "Address", others) ---------------------------------
  // The profile value is "City, State, Country". ATS place pickers (Google Places-style) list
  // "Saint Louis, MO, USA" or "St Louis, Missouri, United States": match on the city core
  // AND the state (full name or USPS code), never on "United States" alone. A resume-parse guess
  // already in the box (e.g. a nearby town) is replaced when it does not contain the city.
  const US_STATES = {alabama:"al",alaska:"ak",arizona:"az",arkansas:"ar",california:"ca",colorado:"co",connecticut:"ct",delaware:"de",
    florida:"fl",georgia:"ga",hawaii:"hi",idaho:"id",illinois:"il",indiana:"in",iowa:"ia",kansas:"ks",kentucky:"ky",louisiana:"la",maine:"me",
    maryland:"md",massachusetts:"ma",michigan:"mi",minnesota:"mn",mississippi:"ms",missouri:"mo",montana:"mt",nebraska:"ne",nevada:"nv",
    "new hampshire":"nh","new jersey":"nj","new mexico":"nm","new york":"ny","north carolina":"nc","north dakota":"nd",ohio:"oh",oklahoma:"ok",
    oregon:"or",pennsylvania:"pa","rhode island":"ri","south carolina":"sc","south dakota":"sd",tennessee:"tn",texas:"tx",utah:"ut",
    vermont:"vt",virginia:"va",washington:"wa","west virginia":"wv",wisconsin:"wi",wyoming:"wy","district of columbia":"dc"};
  const _cityCore = (c) => norm(c).replace(/^(st|saint|ste|sainte|ft|fort|mt|mount)\.?\s+/, "").trim();
  function _placeMatches(optText, city, state) {
    const t = " " + norm(optText).replace(/[.,]/g, " ") + " ";
    const core = _cityCore(city);
    if (!core || !t.includes(" " + core.split(" ")[0])) return false;
    if (!state) return true;
    const st = norm(state), code = US_STATES[st] || (st.length === 2 ? st : "");
    return t.includes(" " + st + " ") || (code && t.includes(" " + code + " "));
  }
  async function fillLocation(el, value) {
    const parts = String(value || "").split(",").map((x) => x.trim()).filter(Boolean);
    const city = parts[0] || "", state = parts[1] || "";
    if (!el || !city) return { ok: false };
    if (clean(el.value) && _placeMatches(el.value, city, state)) return { ok: true, already: true };
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    const query = city + (state ? ", " + state : "");
    el.focus();
    try { setter.call(el, ""); el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" })); } catch (_) {}
    await sleep(120);
    try { setter.call(el, query); el.dispatchEvent(new InputEvent("input", { bubbles: true, data: query, inputType: "insertText" })); } catch (_) {}
    let opts = [];
    for (let i = 0; i < 16; i++) {
      await sleep(250);
      opts = [...document.querySelectorAll('[role="option"], .pac-item, [data-ui="option"], li[id*="option"]')].filter((o) => { try { return o.offsetParent && clean(o.textContent); } catch (_) { return false; } });
      if (opts.length) break;
    }
    const hit = opts.find((o) => _placeMatches(o.textContent, city, state));
    if (hit) {
      try { hit.scrollIntoView({ block: "nearest" }); } catch (_) {}
      fireMouse(hit); await sleep(300);
      const shown = clean(el.value) || clean(hit.textContent);
      return { ok: true, shown, loose: !_placeMatches(shown, city, state) };
    }
    try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); } catch (_) {}
    // No matching place in the list: leave our typed value so the field isn't the wrong town, flag it.
    return { ok: !!clean(el.value), shown: clean(el.value), loose: true };
  }
  async function adapterFill(items, rows, _late) {
    const out = { filled: 0, review: 0, skipped: 0 };
    const todo = [], unrendered = [];
    for (const it of items) {
      const el = _adEl(it);
      it.el = el;
      if (/cover.?letter/i.test(it.id + " " + it.q)) { if (it.required) { rows.push([it.q.slice(0, 60), "skip", "cover letter required -> dashboard Cover"]); out.skipped++; } continue; }
      if (!el && it.kind !== "radio" && it.kind !== "checkbox") { unrendered.push(it); continue; }   // not rendered (yet)
      if (el && (it.kind === "text" || it.kind === "textarea") && clean(el.value)) continue;  // prefilled
      if (it.kind === "location" && !el) continue;
      if (el && (it.kind === "select" || it.kind === "combobox") && (comboCommitted(el) || (clean(el.value) && (it.options || []).some((o) => norm(o) === norm(el.value))))) continue;
      todo.push(it);
    }
    setReport("Answering " + todo.length + " questions in one batch...", rows);
    const batch = await askEngineBatch(todo.map((it) => ({ q: it.kind === "location" ? "Current location" : it.q,
      kind: it.kind === "location" ? "text" : it.kind, options: it.options, required: it.required,
      limit: it.el && it.el.maxLength > 0 ? it.el.maxLength : null })), 90000);
    const answers = (batch && !batch.__error && Array.isArray(batch.answers)) ? batch.answers : [];
    if (!answers.length) { rows.push(["Answer engine", "skip", (batch && batch.__error) || "no batch answers"]); }
    let i = 0;
    for (const it of todo) {
      const a = answers[i++] || {};
      const label = it.q.slice(0, 60);
      setReport("Filling " + i + "/" + todo.length + "...", rows);
      try {
        let text = (a && a.ok !== false) ? String(a.text || "").trim() : "";
        // Required long textarea that the fast batch deferred: draft it now (local model), review.
        if (!text && it.kind === "textarea" && it.required && a && a.method === "essay-skip") {
          const d = await askEngine(it.q, it.el && it.el.maxLength > 0 ? it.el.maxLength : null, false, 60000, false);
          if (d && !d.__error && d.text) { text = String(d.text).trim(); a.method = d.method || "essay"; a.review = true; }
        }
        if (!text) { rows.push([label, "skip", (it.required ? "required, " : "") + "no answer -> you"]); out.skipped++; continue; }
        const isReview = !!(a.review || (a.method && !/^(field|learned|options|literal|choice|profile|guard)/i.test(a.method)));
        let ok = false, shownVal = text;
        if (it.kind === "location") {
          const r = await fillLocation(it.el, text);
          if (r.already) { rows.push([label, "filled", clean(it.el.value).slice(0, 60) + " (already set)"]); out.filled++; continue; }
          ok = r.ok; shownVal = r.shown || text;
          if (ok && r.loose) { rows.push([label, "review", shownVal.slice(0, 60) + " (check the place)"]); out.review++; continue; }
        } else if (it.kind === "text" || it.kind === "textarea") {
          if (it.kind === "text" && text.length > 200) { rows.push([label, "skip", "long answer for a one-line field -> you"]); out.skipped++; continue; }
          ok = _adSetText(it.el, text);
        } else if (it.kind === "select" || it.kind === "combobox") {
          const pick = it.options ? _adPickOptions(it, text)[0] : text;
          if (!pick) { rows.push([label, "skip", "'" + text.slice(0, 30) + "' not an option -> you"]); out.skipped++; continue; }
          shownVal = pick;
          ok = await _adFillSelect(it.el, pick);   // exact/word match on the open menu, typeahead fallback inside
        } else if (it.kind === "radio" || it.kind === "checkbox") {
          const picks = _adPickOptions(it, text);
          if (!picks.length) { rows.push([label, "skip", "'" + text.slice(0, 30) + "' not an option -> you"]); out.skipped++; continue; }
          const scope = document.querySelector('[data-ui="' + CSS.escape(it.id) + '"]') || (it.el && (it.el.closest('fieldset, [role="group"], [role="radiogroup"]')));
          let n = 0;
          for (const p of (it.kind === "radio" ? picks.slice(0, 1) : picks)) {
            // ARIA radio (Workable YES/NO) or native checkbox/radio by option label / option id.
            const idx = (it.options || []).findIndex((o) => norm(o) === norm(p));
            const oid = it.optIds && idx >= 0 ? it.optIds[idx] : null;
            let tgt = null;
            if (scope) {
              tgt = [...scope.querySelectorAll('[role="radio"], [role="checkbox"], label')].find((x) => norm(x.innerText) === norm(p))
                 || (oid ? scope.querySelector('input[name="' + CSS.escape(oid) + '"], input[value="' + CSS.escape(oid) + '"]') : null)
                 || [...scope.querySelectorAll("input")].find((x) => norm(optionLabel(x)) === norm(p));
            }
            if (!tgt) continue;
            const inp = tgt.tagName === "INPUT" ? tgt : tgt.querySelector("input");
            if (inp && inp.checked) { n++; continue; }
            if (tgt.getAttribute("role") === "radio" || tgt.getAttribute("role") === "checkbox") fireMouse(tgt);
            else if (inp) commitOption(inp); else fireMouse(tgt);
            await sleep(120);
            const done = (inp && inp.checked) || tgt.getAttribute("aria-checked") === "true";
            if (done) n++;
          }
          ok = n > 0; shownVal = picks.join(", ");
        }
        if (ok) { rows.push([label, isReview ? "review" : "filled", shownVal.slice(0, 60)]); isReview ? out.review++ : out.filled++; }
        else { rows.push([label, "skip", "couldn't set '" + shownVal.slice(0, 30) + "' -> you"]); out.skipped++; }
      } catch (e) { rows.push([label, "skip", "error: " + String(e).slice(0, 40)]); out.skipped++; }
    }
    // Conditional fields that render only after an earlier answer (Greenhouse Race after
    // Hispanic/Latino = No): one more pass over the ones that have appeared since.
    if (!_late && unrendered.length) {
      await sleep(400);
      const now = unrendered.filter((it) => _adEl(it));
      if (now.length) {
        const r2 = await adapterFill(now, rows, true);
        out.filled += r2.filled; out.review += r2.review; out.skipped += r2.skipped;
      }
    }
    return out;
  }

  async function runFillAll() {
    if (fillBusy) return;
    fillBusy = true;
    openReport();
    rememberFill();

    // Keep the MV3 service worker alive for the whole fill. An open port prevents Chrome's idle
    // shutdown of the worker, so the batch resolve (and any per-field fallback) can't stall on a
    // mid-fill eviction - the class of hang that froze earlier builds. Cheap insurance; content
    // side only. Disconnected in the finally-style cleanup at the end of the fill.
    let _keepPort = null;
    try { _keepPort = chrome.runtime.connect({ name: "penates-fill" }); if (_keepPort) _keepPort.onDisconnect.addListener(() => {}); } catch (_) {}

    const rows = [];
    const groupsDone = new Set();
    const wdHandled = new Set();
    const healMap = [];   // {q, value, kind} for text/radio fills, so a heal pass can re-fill
    let filled = 0, review = 0, skipped = 0;                     // any that an Ashby re-render race cleared
    const t0 = Date.now();
    const elapsed = () => ((Date.now() - t0) / 1000).toFixed(0);
    const _tm = []; let _tLast = Date.now();
    const mark = (k) => { const n = Date.now(); _tm.push(k + " " + ((n - _tLast) / 1000).toFixed(1) + "s"); _tLast = n; };
    let seen = 0;

    // Attach the resume FIRST. Setting a file makes Ashby re-render the WHOLE form (every input
    // element is replaced). If we filled first, that re-render would intermittently wipe
    // just-set fields (phone especially); if we captured field refs first, they'd be detached.
    // So: attach resume, let the re-render settle, THEN capture fresh field refs and fill. No
    // mass re-render happens after that (per-field edits only re-render their own control).
    setReport("Attaching resume...", rows);
    try {
      const before = rows.length;
      await attachResume(rows);
      if (rows.length > before && rows[rows.length - 1][1] === "filled") filled++;
    } catch (e) { rows.push(["Resume upload", "skip", String(e).slice(0, 50)]); }
    await sleep(700); // let the file-triggered re-render finish before we grab element refs
    mark("resume");

    // Dedicated ATS adapter (Greenhouse / Workable): schema -> one batch -> fill by id. When it
    // runs, the generic heuristic passes below are skipped entirely (no double processing).
    let _adapterRan = false;
    try {
      const _items = await adapterSchema();
      if (_items && _items.length) {
        const _r = await adapterFill(_items, rows);
        filled += _r.filled; review += _r.review; skipped += _r.skipped;
        _adapterRan = true;
      }
    } catch (e) { rows.push(["ATS adapter", "skip", "fell back to generic: " + String(e).slice(0, 40)]); }
    penGeneric: {
    if (_adapterRan) break penGeneric;

    // Workday repeating Work Experience grid (from your saved work_history). Marks the
    // fields it sets so the generic scan below skips them.
    try {
      const _wr = await fillWorkdayExperience(rows, wdHandled);
      filled += _wr.filled; review += _wr.review;
      if (_wr.filled || _wr.review) setReport("Work experience filled...", rows);
      // Materialize a Workday Education block if the section is collapsed, so the scan below fills it.
      if (await wdEnsureEducation()) setReport("Education block added...", rows);
    } catch (e) { rows.push(["Work Experience", "skip", String(e).slice(0, 60)]); }
    mark("work-exp+edu");

    try { const _rr = await fillReferences(rows, wdHandled); filled += _rr.filled; } catch (e) { rows.push(["References", "skip", String(e).slice(0, 50)]); }
    try { filled += await fillStartDates(rows, wdHandled); } catch (e) { rows.push(["Start date", "skip", String(e).slice(0, 50)]); }
    mark("refs+dates");
    setReport("Scanning form...", rows);
    let empSkipped = 0;
    const fields = deepFields().filter((el) => {
      if (inPenates(el)) return false;
      if (wdHandled.has(el)) return false;
      if (inEmploymentBlock(el)) { if (!clean(el.value || "")) empSkipped++; return false; }
      const t = (el.type || el.tagName).toLowerCase();
      if (el.tagName === "INPUT" && SKIP_TYPES.test(el.type || "")) return false;
      if (el.disabled) return false;
      // Read-only is skipped EXCEPT a dropdown's input: non-searchable Ant/react selects render a
      // readOnly role=combobox input, and the dropdown filler opens + clicks them without typing.
      if (el.readOnly && (el.getAttribute("role") || "") !== "combobox") return false;
      return shown(el);
    });

    if (empSkipped) rows.push(["Work history blocks", "skip", empSkipped + " empty employer fields (address/dates/reason) -> you"]);

    // ---- resolve EVERY question in ONE batch (kills the per-field SW round-trip stalls) --------
    // Walk the fields, collect the questions the apply loop below will ask (same resolvers, same
    // skip rules), and ask them all in a single /answer-batch. _answerCache holds question->answer;
    // the apply loop and the verify/heal passes read it with NO round-trip (askCached). A question
    // the collector missed, or a per-field Regenerate (fresh:true), falls back to a single /answer -
    // so a collect/apply mismatch is at worst slower, never wrong. essays are skipped in the batch
    // (bulk_skip_essays) and stay click-to-draft, exactly as before.
    _answerCache = new Map();
    try {
      const want = new Set();
      const seenG = new Set();
      for (const el of fields) {
        const tag = el.tagName, typ = (el.type || "").toLowerCase();
        if (tag === "INPUT" && (typ === "radio" || typ === "checkbox")) {
          if (!_optClickable(el)) continue;
          const q = groupQuestion(el);
          if (!q || seenG.has(q)) continue; seenG.add(q);
          const optlab = optionLabel(el);
          if (typ === "checkbox" && CONSENT_RE.test(q + " " + optlab) && !/which|select all|open to|settings/i.test(q)) continue; // consent tick is deterministic, no engine
          want.add(q);
        } else if (tag === "SELECT") {
          const q = questionFor(el);
          if (!q || skipQuestion(q)) continue;
          if (el.value && el.selectedIndex > 0 && clean(el.options[el.selectedIndex].text)) continue;
          want.add(q);
        } else if (tag === "TEXTAREA" || (tag === "INPUT" && /^(text|email|tel|url|search|number|)$/i.test(typ)) || el.isContentEditable) {
          const q = questionFor(el);
          if (!q || skipQuestion(q)) continue;
          if ((el.value || el.textContent || "").trim()) continue;
          want.add(q);
        }
      }
      const questions = [...want];
      if (questions.length) {
        setReport("Resolving " + questions.length + " fields in one batch...", rows);
        const batch = await askEngineBatch(questions, 60000);
        const answers = (batch && batch.answers) || [];
        for (let i = 0; i < questions.length; i++) {
          if (answers[i]) _answerCache.set(questions[i], answers[i]);   // answers[i] === questions[i]
        }
        if (batch && batch.__error) rows.push(["Batch resolve", "skip", batch.__error + " - falling back per-field"]);
        if (batch && batch.ms != null) _tm.push("(server " + (batch.ms / 1000).toFixed(1) + "s/" + questions.length + "q)");
      }
    } catch (e) { rows.push(["Batch resolve", "skip", String(e).slice(0, 50)]); }
    mark("batch");

    for (const el of fields) {
      const tag = el.tagName, typ = (el.type || "").toLowerCase();
      seen++;
      setReport("Filling " + seen + "/" + fields.length + " ... " + elapsed() + "s elapsed.", rows);

      // One field must NEVER abort the whole fill. A throw here (usually touching an element the
      // Ashby re-render just detached) would otherwise skip the rest of the loop AND the heal +
      // verify passes, leaving earlier fields wiped and unrestored. Isolate every field.
      try {
      // ---- radio / checkbox: resolve on the GROUP question, click matching option ----
      if (tag === "INPUT" && (typ === "radio" || typ === "checkbox")) {
        // Skip inputs with no layout box (Ashby's hidden Yes/No backing checkboxes); the button
        // pass clicks the real visible buttons.
        if (!_optClickable(el)) continue;
        const q = groupQuestion(el);
        if (!q) { continue; } // no question resolved: skip silently (avoid random toggles)
        // Group by the QUESTION, so a "select all that apply" checkbox set (each option is a
        // uniquely-named checkbox) is resolved ONCE, not one skip row per option. No
        // CONDITIONAL_RE skip here: a radio/checkbox group is the primary question even when its
        // text has "if so, what capacity" (veteran); conditional TEXT follow-ups are skipped in
        // the text branch.
        if (groupsDone.has(q)) continue;

        // required consent / single acknowledgement checkbox -> tick it
        const optlab = optionLabel(el);
        if (typ === "checkbox" && CONSENT_RE.test(q + " " + optlab) && !/which|select all|open to|settings/i.test(q)) {
          const ok = commitOption(el);
          rows.push([q.slice(0, 60), ok ? "filled" : "skip", ok ? "checked" : "couldn't check"]);
          ok ? filled++ : skipped++;
          groupsDone.add(q);
          continue;
        }

        const data = await askCached(q, 25, false, 40000, true);
        groupsDone.add(q);
        if (!data || data.__error) { rows.push([q.slice(0, 60), "skip", data && data.__error ? "engine: " + data.__error : "no answer"]); skipped++; continue; }
        const ans = (data.text || "").trim();
        if (!ans) { rows.push([q.slice(0, 60), "skip", "no grounded answer -> you"]); skipped++; continue; }
        // ALL options of this group (same radio name, or same climbed question - covers Ashby's
        // uniquely-named checkboxes).
        const groupEls = fields.filter((o) => o.tagName === "INPUT" && (o.type || "").toLowerCase() === typ &&
          ((o.name && el.name && o.name === el.name) || groupQuestion(o) === q));
        const na = norm(ans);
        // Exact match beats a loose substring one ("1000-2499" must not lose to ">100", "Man"
        // must not match inside "Woman").
        const scoreOf = (ol) => {
          if (!ol) return -1;
          if (ol === na) return 100;
          // A bare Yes/No/True/False answer must match an option EXACTLY. Prefix/whole-word would
          // tick "No Kubernetes: Hasn't worked with it" for an engine "No" (false claim on the form).
          if (/^(yes|no|true|false|n a|none)$/.test(na)) return -1;
          if (na && ol.startsWith(na)) return 70;
          if (na && na.startsWith(ol) && ol.length >= 3) return 60;
          if (na.length >= 3 && (" " + ol + " ").includes(" " + na + " ")) return 50;  // whole word
          if (na.length >= 5 && ol.includes(na)) return 20;                              // loose, long only
          return -1;
        };
        const opts = (groupEls.length ? groupEls : [el]);
        if (typ === "checkbox") {
          // select-all: check every option that matches the answer, log one row for the group
          let n = 0, last = "";
          for (const o of opts) { if (scoreOf(norm(optionLabel(o))) >= 50 && commitOption(o)) { n++; last = optionLabel(o); } }
          if (n) { rows.push([q.slice(0, 60), "filled", n > 1 ? (n + " selected") : last]); filled++; }
          else { rows.push([q.slice(0, 60), "skip", "answer '" + ans + "' not among options"]); skipped++; }
        } else {
          let best = null, bestS = 0;
          for (const o of opts) { const s = scoreOf(norm(optionLabel(o))); if (s > bestS) { bestS = s; best = o; } }
          if (best && commitOption(best)) { rows.push([q.slice(0, 60), "filled", optionLabel(best)]); filled++; healMap.push({ q, value: ans, kind: "radio" }); }
          else { rows.push([q.slice(0, 60), "skip", "answer '" + ans + "' not among options"]); skipped++; }
        }
        continue;
      }

      // ---- select ----
      if (tag === "SELECT") {
        const q = questionFor(el);
        if (!q) { continue; }
        if (skipQuestion(q)) { if (!JUNK_RE.test(q)) { rows.push([q.slice(0, 60), "skip", "conditional/optional -> you"]); skipped++; } continue; }
        if (el.value && el.selectedIndex > 0 && clean(el.options[el.selectedIndex].text)) { continue; } // already set
        const data = await askCached(q, 40, false, 40000, true);
        if (!data || data.__error || !data.text) { rows.push([q.slice(0, 60), "skip", "no value -> you"]); skipped++; continue; }
        const ok = fillSelect(el, data.text);
        rows.push([q.slice(0, 60), ok ? "filled" : "skip", ok ? data.text : "'" + data.text + "' not an option"]);
        ok ? filled++ : skipped++;
        continue;
      }

      // ---- text / textarea / email / tel / number ----
      if (tag === "TEXTAREA" || (tag === "INPUT" && /^(text|email|tel|url|search|number|)$/i.test(typ)) || el.isContentEditable) {
        const q = questionFor(el);
        if (!q) { continue; }                        // unlabeled (site search etc.) -> skip
        if (skipQuestion(q)) { if (!JUNK_RE.test(q)) { rows.push([q.slice(0, 60), "skip", "conditional/optional -> you"]); skipped++; } continue; }
        const cur = (el.value || el.textContent || "").trim();
        if (cur) { continue; }                        // don't clobber prefilled
        if (/\bcover\s*letter\b/i.test(q) || /cover_?letter/i.test((el.name || "") + " " + (el.id || ""))) {
          // A STAR story is not a cover letter. Never auto-write this box.
          rows.push([q.slice(0, 60), "skip", _fieldRequired(el) ? "cover letter required -> dashboard Cover" : "cover letter optional -> left blank"]); skipped++; continue;
        }
        const limit = el.maxLength && el.maxLength > 0 ? el.maxLength : null;
        let data = await askCached(q, limit, false, 40000, true);
        if (data && data.method === "essay-skip") {
          // Essays are skipped in the fast batch so it never blocks on a 20s draft. Draft this
          // one now, inline (single /answer, bulk off, composes the essay), one at a time; it
          // gets filled and marked "review" below so you read it before submitting.
          data = await askEngine(q, limit, false, 60000, false);
        }
        if (!data || data.__error || !data.text) { rows.push([q.slice(0, 60), "skip", data && data.__error ? "engine: " + data.__error : "no answer -> you"]); skipped++; continue; }
        // a bare structured value (tier-1 field match) belongs in an input/select, never a prose
        // box: this is where a demographic token leaks into a free-text field. Leave it for you.
        // Middle name only when the field is actually required (the user preference).
        if (/\bmiddle\s+(name|initial)\b/i.test(q) && !_fieldRequired(el)) {
          rows.push([q.slice(0, 60), "skip", "middle name optional -> left blank"]); skipped++; continue;
        }
        if (tag === "TEXTAREA" && data.method === "field") {
          // A demographic token must never leak into a prose box, but a LinkedIn/GitHub/website URL
          // legitimately belongs in its labeled textarea. Allow only URL/handle field values through.
          const fkey = (data.field || "").toLowerCase();
          const urlish = /identity\.(linkedin|github|gitlab|website|portfolio|url)/.test(fkey)
            || /\b(linkedin|github|gitlab|portfolio|personal (site|website)|website|url)\b/i.test(q)
            || /https?:\/\/|(?:linkedin|github|gitlab)\.com|^[\w.-]+\.(?:com|io|dev|net|org|me)\b/i.test(String(data.text || "").trim());
          // A salary/compensation number legitimately belongs in its labeled box (Workday makes
          // "salary expectations" a textarea). Allow compensation field values + salary questions through.
          const salaryish = /^compensation\./.test(fkey)
            || /salary|compensation|desired pay|pay expectation|expected pay/i.test(q);
          // A literal prose answer (no profile key, or a long multi-word sentence) belongs in a prose box.
          const t = String(data.text || "").trim();
          const prose = t.length >= 40 && /\s/.test(t) && /[a-z]{3,}\s+[a-z]{3,}/i.test(t) && !/^(compensation|eeo|identity|eligibility)\./.test(fkey);
          if (!urlish && !salaryish && !prose) { rows.push([q.slice(0, 60), "skip", "structured value not for a text box -> you"]); skipped++; continue; }
        }
        const isCombo = isCombobox(el);
        const singleLine = tag !== "TEXTAREA" && !el.isContentEditable;
        const isEssay = data.method && data.method !== "field" && data.method !== "learned";
        // never write a generated essay into a one-line field (URL / short inputs)
        if (singleLine && !isCombo && isEssay) {
          rows.push([q.slice(0, 60), "skip", "no structured value -> you (won't essay a short field)"]); skipped++; continue;
        }
        const gap = data.gaps && data.gaps.length;
        if (isCombo) {
          let pickText = data.text;
          let ok = false;
          // Field of study rarely lists "Computer Information Technology" verbatim. Try the closest
          // common catalog names in order (the user: "Information Technology" first), then the exact value.
          let alts = FIELD_Q.test(q) ? [...new Set(["Information Technology", data.text, ...FIELD_ALTS])] : [data.text];
          const _ab = stateAbbr(data.text);
          const _cur = comboShown(el);
          // Already showing the wanted value (Country defaulted to United States): leave it, count it filled.
          if (_cur.length >= 2 && (sameAddr(_cur, data.text) || (_ab && norm(_cur) === _ab.toLowerCase()))) {
            rows.push([q.slice(0, 60), "filled", _cur.slice(0, 60) + " (already set)"]); filled++;
            continue;
          }
          if (_ab && /\bstate\b|province/i.test(q)) alts = [...new Set([data.text, _ab])];   // lists of codes (WV)
          const _street = /address line ?1|street|^address$/i.test(q);
          if (_street) {
            // Address autocomplete: score suggestions on the FULL home address so a same-street match in
            // another city (123 Main St, Pittsburgh) loses; the query typed is still the street.
            const h = await homeAddress();
            if (h.city) alts = [[h.street || data.text, h.city, ((h.abbr || h.state) + " " + h.zip).trim()].join(", ")];
          }
          for (const v of alts) {
            ok = await fillCombobox(el, v);
            if (ok) { pickText = v; break; }
            await sleep(150);
          }
          if (!ok && _street) {
            // No suggestion for the real address: type the street as plain text and close the list.
            try { setNativeNoBlur(el, data.text); await closeAutocomplete(el); } catch (_) {}
            rows.push([q.slice(0, 60), "filled", data.text.slice(0, 50) + " (typed, no matching suggestion)"]); filled++;
            continue;
          }
          if (ok) { rows.push([q.slice(0, 60), "filled", pickText.slice(0, 60)]); filled++; if (ADDR_Q.test(q)) healMap.push({ q, value: pickText, kind: "combo" }); }
          else { rows.push([q.slice(0, 60), "skip", "combobox: '" + data.text.slice(0, 40) + "' not selectable -> you"]); skipped++; }
          continue;
        }
        // Long prose (essay/gap) is written 6-20s after its ref was captured, so Ashby's async
        // resume-autofill re-render can have DETACHED our node - the write then lands on a dead
        // element and the live field stays empty (report says "review", DOM is blank). Re-resolve
        // the live field, verify the write stuck, retry once, and register it for the heal pass so
        // a still-later re-render is repaired too. Structured single-line values heal as before.
        const isProse = (tag === "TEXTAREA" || el.isContentEditable) && (isEssay || gap);
        if (isProse) {
          let tgt = (el.isConnected ? el : (reFindField(q) || el));
          setNative(tgt, data.text);
          if (!((tgt.value || tgt.textContent || "").trim())) {
            const r2 = reFindField(q); if (r2) { tgt = r2; setNative(tgt, data.text); }
          }
          healMap.push({ q, value: data.text, kind: "text" });
        } else {
          const _addr = singleLine && ADDR_Q.test(q);
          if (_addr && /address line ?1|street|^address$/i.test(q)) { setNativeNoBlur(el, data.text); await closeAutocomplete(el); }
          else setNative(el, data.text);
          if ((singleLine || tag === "TEXTAREA" || el.isContentEditable) && !gap && !isEssay) healMap.push({ q, value: data.text, kind: "text", addr: _addr });
        }
        if (gap) { rows.push([q.slice(0, 60), "review", "gap: " + data.gaps.join(", ") + " | " + data.text.slice(0, 40)]); review++; }
        else if (isEssay) { rows.push([q.slice(0, 60), "review", "essay (" + (data.method || "") + ") - read it | " + data.text.slice(0, 40)]); review++; }
        else { rows.push([q.slice(0, 60), "filled", data.text.slice(0, 60)]); filled++; }
        continue;
      }
      } catch (e) {
        try { rows.push([(questionFor(el) || el.name || "field").slice(0, 60), "skip", "error, skipped: " + String(e).slice(0, 40)]); skipped++; } catch (_) {}
      }
    }

    // ---- second pass: choice questions rendered as <button> (Ashby Yes/No etc.) ----
    // deepFields only walks input/textarea/select, so button-based Yes/No groups are never
    // seen. Collect short-text option buttons, group them by their question, and click the
    // one matching the engine's answer (e.g. the deterministic COI "No").
    try {
      // Anchored words to skip, PLUS any button whose text contains "submit"/"apply"
      // (covers "Submit Application") so dropping the type=submit filter below can't ever
      // click the real form-submit control.
      const BTN_SKIP = /^(submit|next|back|previous|continue|upload|choose file|browse|add another|add|remove|delete|apply|save|autofill|fill|regenerate|insert|done|close|cancel|\+|\-|×|x)$/i;
      const BTN_SUBMITTY = /submit|^apply\b|apply now|application/i;
      const climbQ = (el) => {
        let p = el;
        for (let i = 0; i < 8 && p; i++) {
          p = p.parentElement; if (!p) break;
          const t = clean(p.innerText || "");
          if (t.length > 15 && /\?|describe|do you|are you|have you|which|what|size of|authoriz|sponsor/i.test(t)) {
            return stripSectionHead(t.replace(/\s*(Yes\s*No|No\s*Yes)\s*$/i, "").trim()).slice(0, 160);
          }
        }
        return "";
      };
      const cand = [...document.querySelectorAll("button")].filter((b) => {
        // Ashby renders Yes/No choice buttons as type="submit" too, so we do NOT exclude by
        // type; the real "Submit Application" button is kept out by text (BTN_SKIP / BTN_SUBMITTY)
        // and by the 2-8-per-question group check below.
        if (inPenates(b) || b.disabled || !shown(b)) return false;
        const t = clean(b.textContent);
        return t && t.length <= 40 && !BTN_SKIP.test(t) && !BTN_SUBMITTY.test(t);
      });
      const groups = new Map();
      for (const b of cand) {
        const q = climbQ(b); if (!q) continue;
        if (!groups.has(q)) groups.set(q, []);
        groups.get(q).push(b);
      }
      for (const [q, btns] of groups) {
        if (btns.length < 2 || btns.length > 8) continue;      // a real choice set only
        if (groupsDone.has(q)) continue;
        groupsDone.add(q);
        if (skipQuestion(q)) { if (!JUNK_RE.test(q)) { rows.push([q.slice(0, 60), "skip", "conditional/optional -> you"]); skipped++; } continue; }
        // already answered (Ashby marks the picked option aria-pressed=true)?
        if (btns.some((b) => b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-checked") === "true")) continue;
        seen++;
        setReport("Filling " + seen + " (choice) ... " + elapsed() + "s elapsed.", rows);
        const data = await askCached(q, 25, false, 40000, true);
        if (!data || data.__error || !data.text) { rows.push([q.slice(0, 60), "skip", data && data.__error ? data.__error : "no answer -> you"]); skipped++; continue; }
        const na = norm(data.text);
        let clicked = null;
        for (const b of btns) {
          const bl = norm(b.textContent);
          if (bl && (bl === na || (na.length >= 2 && bl === na) || (bl.length >= 2 && na.startsWith(bl)) || (na.length >= 2 && bl.startsWith(na)))) {
            b.click(); clicked = clean(b.textContent); break;
          }
        }
        if (clicked) { rows.push([q.slice(0, 60), "filled", clicked]); filled++; }
        else { rows.push([q.slice(0, 60), "skip", "answer '" + clean(data.text).slice(0, 20) + "' not a choice -> you"]); skipped++; }
      }
    } catch (e) { rows.push(["(button choice pass)", "skip", String(e).slice(0, 60)]); }

    // ---- ARIA-role widget pass (Rippling etc.): div[role=combobox] + div[role=radiogroup] -----
    // deepFields only walks input/textarea/select, so react-aria DIV widgets with no native control
    // are invisible to every pass above. Resolve the question from aria-labelledby, ask the engine,
    // and commit by clicking the ARIA option / radio. Self-verifying; never blind-clicks.
    try {
      const ariaQuestion = (el) => {
        const lb = el.getAttribute("aria-labelledby");
        if (lb) {
          const t = lb.split(/\s+/).map((i) => (document.getElementById(i) || {}).innerText || "").join(" ").trim();
          if (t) return stripSectionHead(clean(t)).slice(0, 240);
        }
        return groupQuestion(el); // climb ancestors for the question heading (radiogroups here have no
                                  // aria-labelledby); groupQuestion strips a trailing "Yes No" and falls
                                  // back to questionFor itself. Fixes the Rippling auth/sponsorship radios.
      };

      // A) ARIA comboboxes: div[role=combobox] with a listbox popup, no native input
      const combos = [...document.querySelectorAll('[role="combobox"][aria-haspopup="listbox"], [role="combobox"][aria-controls]')]
        .filter((el) => el.tagName !== "INPUT" && !inPenates(el) && shown(el) && el.getAttribute("aria-disabled") !== "true");
      for (const el of combos) {
        try {
          const q = ariaQuestion(el);
          if (!q || groupsDone.has(q)) continue;
          if (skipQuestion(q)) { groupsDone.add(q); if (!JUNK_RE.test(q)) { rows.push([q.slice(0, 60), "skip", "conditional/optional -> you"]); skipped++; } continue; }
          const shownVal = clean(el.textContent || el.getAttribute("aria-label") || "");
          if (shownVal && !/^select\.{0,3}$/i.test(shownVal)) { groupsDone.add(q); continue; }   // already answered
          groupsDone.add(q); seen++;
          setReport("Filling " + seen + " (choice) ... " + elapsed() + "s elapsed.", rows);
          const data = await askCached(q, 40, false, 40000, true);
          if (!data || data.__error || !data.text) { rows.push([q.slice(0, 60), "skip", data && data.__error ? data.__error : "no value -> you"]); skipped++; continue; }
          const ok = await fillAriaCombobox(el, data.text);
          rows.push([q.slice(0, 60), ok ? "filled" : "skip", ok ? data.text.slice(0, 60) : "'" + data.text.slice(0, 30) + "' not selectable -> you"]);
          ok ? filled++ : skipped++;
        } catch (_) {}
      }

      // B) ARIA radiogroups: div[role=radiogroup] > div[role=radio][data-value]
      for (const g of [...document.querySelectorAll('[role="radiogroup"]')].filter((g) => !inPenates(g) && shown(g))) {
        try {
          const q = ariaQuestion(g);
          if (!q || groupsDone.has(q)) continue;
          const radios = [...g.querySelectorAll('[role="radio"]')].filter((r) => shown(r));
          if (radios.length < 2 || radios.length > 8) continue;
          if (skipQuestion(q)) { groupsDone.add(q); if (!JUNK_RE.test(q)) { rows.push([q.slice(0, 60), "skip", "conditional/optional -> you"]); skipped++; } continue; }
          if (radios.some((r) => r.getAttribute("aria-checked") === "true")) { groupsDone.add(q); continue; }
          groupsDone.add(q); seen++;
          setReport("Filling " + seen + " (choice) ... " + elapsed() + "s elapsed.", rows);
          const labelOf = (r) => clean(r.getAttribute("data-value") || r.textContent || "");
          const opts = radios.map(labelOf).filter(Boolean);
          const data = await askEngine(q, 25, false, 40000, false, opts);   // options: serve constrains a choice to Yes/No
          if (!data || data.__error || !data.text) { rows.push([q.slice(0, 60), "skip", data && data.__error ? data.__error : "no answer -> you"]); skipped++; continue; }
          const na = norm(data.text);
          let hit = null;
          for (const r of radios) { const rl = norm(labelOf(r)); if (rl && (rl === na || rl.startsWith(na) || na.startsWith(rl))) { hit = r; break; } }
          if (hit) {
            fireMouse(hit); await sleep(140);
            rows.push([q.slice(0, 60), "filled", labelOf(hit)]); filled++;
            healMap.push({ q, value: labelOf(hit), kind: "ariaRadio" });
          } else { rows.push([q.slice(0, 60), "skip", "answer '" + clean(data.text).slice(0, 20) + "' not a choice -> you"]); skipped++; }
        } catch (_) {}
      }
      // C) Workday single-select prompt buttons: button[aria-haspopup=listbox] showing "Select One"
      for (const btn of [...document.querySelectorAll('button[aria-haspopup="listbox"]')]
             .filter((b) => !inPenates(b) && shown(b) && b.getAttribute("aria-disabled") !== "true")) {
        try {
          const q = ariaQuestion(btn);
          if (!q || groupsDone.has(q)) continue;
          if (skipQuestion(q)) { groupsDone.add(q); if (!JUNK_RE.test(q)) { rows.push([q.slice(0, 60), "skip", "conditional/optional -> you"]); skipped++; } continue; }
          const sv = clean(btn.textContent || "");
          if (sv && !/^select( one)?( required)?\.{0,3}$/i.test(sv)) { groupsDone.add(q); continue; }   // already chosen
          groupsDone.add(q); seen++;
          setReport("Filling " + seen + " (choice) ... " + elapsed() + "s elapsed.", rows);
          const r = await wdSelectFill(btn, q);
          rows.push([q.slice(0, 60), r.ok ? "filled" : "skip", r.ok ? String(r.answer).slice(0, 40) : r.detail]);
          r.ok ? filled++ : skipped++;
        } catch (_) {}
      }
    } catch (e) { rows.push(["(aria widget pass)", "skip", String(e).slice(0, 60)]); }

    // ---- heal pass ----------------------------------------------------------------
    // Ashby intermittently re-renders the form (a stray effect, the file attach, a sibling
    // update) and wipes a value we already set - phone was the repeat offender. Re-scan on
    // FRESH element refs and re-apply any text/radio value that is now empty. No server calls
    // (we saved the values), so it's cheap and can't make anything worse.
    if (healMap.length) {
      try {
        await sleep(500);
        const fresh = deepFields();
        let healed = 0, addrFixed = 0;
        for (const item of healMap) {
          try {
            if (item.kind === "text") {
              const el = fresh.find((o) => (o.tagName === "TEXTAREA" || o.tagName === "INPUT") &&
                !inPenates(o) && !o.disabled && questionFor(o) === item.q);
              const cur = el ? (el.value || "").trim() : "";
              if (el && !cur) { setNative(el, item.value); healed++; }
              else if (el && item.addr && cur !== item.value.trim()) { setNative(el, item.value); healed++; addrFixed++; }
            } else if (item.kind === "combo") {
              const el = fresh.find((o) => !inPenates(o) && !o.disabled && questionFor(o) === item.q);
              const cur = el ? norm(el.value || el.textContent || "") : "";
              if (el && cur && cur !== norm(item.value) && !cur.startsWith(norm(item.value)) && !norm(item.value).startsWith(cur)) {
                if (await fillCombobox(el, item.value)) { healed++; addrFixed++; }
              }
            } else if (item.kind === "radio") {
              const opts = fresh.filter((o) => o.tagName === "INPUT" && (o.type || "").toLowerCase() === "radio" && groupQuestion(o) === item.q);
              if (opts.length && !opts.some((o) => o.checked)) {
                const na = norm(item.value);
                let best = null, bestS = 0;
                for (const o of opts) {
                  const ol = norm(optionLabel(o));
                  let s = -1;
                  if (ol === na) s = 100; else if (ol && ol.startsWith(na)) s = 70;
                  else if (na && na.startsWith(ol) && ol.length >= 3) s = 60;
                  if (s > bestS) { bestS = s; best = o; }
                }
                if (best) { commitOption(best); healed++; }
              }
            }
          } catch (_) {}
        }
        if (healed) rows.push(["Re-filled after re-render", "filled", healed + " field(s)"]);
        if (addrFixed) rows.push(["Address autocomplete", "review", "site overwrote " + addrFixed + " address field(s); restored. Check County."]);
      } catch (_) {}
    }

    // ---- final verify-and-retry pass ------------------------------------------------
    // The Ashby form re-renders during filling (resume attach + each per-field commit), so a
    // control can be transiently DETACHED (offsetParent null) exactly when the main loop reaches
    // it. The radio/checkbox skip-guard then drops it with NO row, and healMap never learned about
    // it, so it is never re-tried - this is how company size, the "I understand" acknowledgement,
    // and the location combobox were silently lost. This pass runs once the form has settled,
    // re-scans on FRESH refs, and re-attempts anything required that is still empty/unchecked. It
    // only ACTS on a control that is currently unsatisfied, so it can never clobber a good value.
    try {
      await sleep((CAN_SURFACE && !IS_TOP) ? 1200 : 600);   // embeds render/settle slower
      const fresh2 = deepFields().filter((el) => {
        if (inPenates(el)) return false;
        if (inEmploymentBlock(el)) return false;   // employer cards are the user's (Grok seq 206)
        if (el.tagName === "INPUT" && SKIP_TYPES.test(el.type || "")) return false;
        if (el.disabled || (el.readOnly && (el.getAttribute("role") || "") !== "combobox")) return false;
        return shown(el);
      });
      const verifiedGroups = new Set();
      let fixed = 0;
      for (const el of fresh2) {
        const tag = el.tagName, typ = (el.type || "").toLowerCase();
        try {
          // radio / checkbox still unsatisfied in its group
          if (tag === "INPUT" && (typ === "radio" || typ === "checkbox")) {
            const r = el.getBoundingClientRect();
            if (el.offsetParent === null || getComputedStyle(el).display === "none" || (!r.width && !r.height)) continue;
            const q = groupQuestion(el);
            if (!q || verifiedGroups.has(q)) continue;
            verifiedGroups.add(q);
            const group = fresh2.filter((o) => o.tagName === "INPUT" && (o.type || "").toLowerCase() === typ &&
              ((o.name && el.name && o.name === el.name) || groupQuestion(o) === q));
            if (group.some((o) => o.checked)) continue;          // already satisfied - leave it
            const optlab = optionLabel(el);
            // lone required acknowledgement checkbox (e.g. "I understand ...conditional on
            // background check"). Its groupQuestion can bleed into a neighboring block, so tick on
            // CONSENT_RE BEFORE the conditional skip check, keyed on the option's own label.
            if (typ === "checkbox" && CONSENT_RE.test(q + " " + optlab) && !/which|select all|open to|settings/i.test(q)) {
              if (commitOption(el)) { fixed++; rows.push([(optlab || q).slice(0, 60), "filled", "checked (verify)"]); }
              continue;
            }
            if (skipQuestion(q)) continue;                        // genuinely meant for you
            // EEO / identity radios are never guessed in the verify pass (Lever gender got "Non-binary"
            // from a prefix match on "No"). The main pass fills them from the profile or leaves them.
            if (EEO_Q_RE.test(q)) continue;
            const data = await askCached(q, 25, false, 40000, true);
            if (!data || data.__error || !data.text) continue;
            const na = norm(data.text);
            const scoreOf = (ol) => {
              if (!ol) return -1;
              if (ol === na) return 100;
              const wb = (a, b) => a.startsWith(b) && (a.length === b.length || !/[a-z0-9]/i.test(a.charAt(b.length)));
              if (na && wb(ol, na)) return 70;                       // "no" must not match "non-binary"
              if (na && ol.length >= 3 && wb(na, ol)) return 60;
              if (na.length >= 3 && (" " + ol + " ").includes(" " + na + " ")) return 50;
              if (na.length >= 5 && ol.includes(na)) return 20;
              return -1;
            };
            if (typ === "checkbox") {
              let n = 0; for (const o of group) { if (scoreOf(norm(optionLabel(o))) >= 50 && commitOption(o)) n++; }
              if (n) { fixed++; rows.push([q.slice(0, 60), "filled", (n > 1 ? n + " selected" : optionLabel(el)) + " (verify)"]); }
            } else {
              let best = null, bestS = 0; for (const o of group) { const s = scoreOf(norm(optionLabel(o))); if (s > bestS) { bestS = s; best = o; } }
              if (best && commitOption(best)) { fixed++; rows.push([q.slice(0, 60), "filled", optionLabel(best) + " (verify)"]); }
            }
            continue;
          }
          // combobox still empty (autocomplete/react-select that lost the fill-time timing race,
          // common in an embedded iframe). Read the DISPLAYED value, not el.value: a react-select
          // keeps its selection in a single-value element and leaves the input empty.
          if (tag === "INPUT" && isCombobox(el)) {
            if (comboCommitted(el) || (el.value || "").trim()) continue;
            const q = questionFor(el);
            if (!q || skipQuestion(q)) continue;
            const data = await askCached(q, null, false, 40000, true);
            if (!data || data.__error || !data.text) continue;
            if (await fillCombobox(el, data.text)) { fixed++; rows.push([q.slice(0, 60), "filled", data.text.slice(0, 50) + " (verify)"]); }
          }
          // native <select> still on its placeholder (some Country / yes-no fields) - re-pick.
          if (tag === "SELECT" && (el.selectedIndex <= 0 || !(el.value || "").trim())) {
            const q = questionFor(el);
            if (q && !skipQuestion(q) && !EEO_Q_RE.test(q)) {     // EEO selects are never guessed here
              const data = await askCached(q, null, false, 40000, true);
              if (data && !data.__error && data.text) {
                const want = norm(data.text);
                const wbStart = (a, b) => a.startsWith(b) && (a.length === b.length || !/[a-z0-9]/i.test(a.charAt(b.length)));
                const opt = [...el.options].find((o) => norm(o.textContent) === want || norm(o.value) === want)
                          || [...el.options].find((o) => want.length >= 2 && wbStart(norm(o.textContent), want));
                if (opt) { el.value = opt.value; el.dispatchEvent(new Event("change", { bubbles: true }));
                  fixed++; rows.push([q.slice(0, 60), "filled", (opt.textContent || "").trim().slice(0, 50) + " (verify)"]); }
              }
            }
          }
        } catch (_) {}
      }
      if (fixed) { filled += fixed; rows.push(["Verify pass recovered", "filled", fixed + " control(s)"]); }
    } catch (_) {}

    // Late reference pass: some ATSs (Dayforce) parse the uploaded resume and redraw the form
    // seconds later, wiping fields filled early. Refill any reference field that is empty again.
    try { await sleep(400); await fillReferences(rows, null, true); } catch (_) {}

    // Last address guard (2026-10-06 Paylocity): the site's address autocomplete can answer AFTER
    // the heal pass and swap city/state/zip/county for a different street match. Check the candidate's
    // own address fields (never employer blocks) against profile.yaml directly, up to 3 rounds. If
    // anything had to be put back, clear a County the wrong match filled and flag it.
    try {
      const h = await homeAddress();
      const want = [
        { re: /address line ?1|street address|^street$/i, v: h.street, street: true },
        { re: /\bcity\b/i, v: h.city },
        { re: /^\s*state\b|\bstate\s*\/|^\s*province\b/i, v: h.state, abbr: h.abbr },
        { re: /\bzip\b|postal/i, v: h.zip },
      ].filter((w) => w.v);
      let restored = 0;
      for (let round = 0; want.length && round < 3; round++) {
        await sleep(round ? 900 : 600);
        const live = deepFields().filter((o) => !inPenates(o) && !o.disabled && !inEmploymentBlock(o) &&
          (o.tagName === "INPUT" || o.tagName === "SELECT") && shown(o));
        let bad = 0;
        for (const w of want) {
          const el = live.find((o) => w.re.test(questionFor(o) || ""));
          if (!el) continue;
          const cur = el.tagName === "SELECT" ? ((el.selectedOptions[0] || {}).textContent || "") : (isCombobox(el) ? comboShown(el) : (el.value || ""));
          if (sameAddr(cur, w.v) || (w.abbr && norm(cur) === w.abbr.toLowerCase())) continue;
          if (el.tagName === "SELECT") {
            const opt = [...el.options].find((o) => sameAddr(o.textContent, w.v) || (w.abbr && norm(o.textContent) === w.abbr.toLowerCase()));
            if (opt) { el.value = opt.value; el.dispatchEvent(new Event("change", { bubbles: true })); bad++; }
          } else if (isCombobox(el) && !w.street) {
            let ok = await fillCombobox(el, w.v);
            if (!ok && w.abbr) ok = await fillCombobox(el, w.abbr);
            if (ok) bad++;
          } else if (w.street) {
            setNativeNoBlur(el, w.v); await closeAutocomplete(el); bad++;
          } else { setNative(el, w.v); bad++; }
        }
        restored += bad;
        if (!bad) break;
      }
      if (restored) {
        const live = deepFields().filter((o) => !inPenates(o) && !o.disabled && !inEmploymentBlock(o));
        const county = live.find((o) => o.tagName === "INPUT" && /\bcounty\b/i.test(questionFor(o) || ""));
        if (county && (county.value || "").trim()) setNative(county, "");
        rows.push(["Address guard", "review", "site autocomplete changed " + restored + " address field(s); put back" +
                   (county ? ", County cleared -> enter it" : "")]);
        review++;
      }
    } catch (_) {}
    } // end penGeneric
    try { if (_keepPort) _keepPort.disconnect(); } catch (_) {}   // release the service-worker keep-alive
    _answerCache = null;
    fillBusy = false;
    mark("fields+verify");
    rows.push(["Timing", "info", _tm.join(" · ")]);
    try { console.log("[penates] timing", _tm.join(" | ")); } catch (_) {}
    const summary = "\u2713 DONE  \u2014  Filled " + filled + " . " + review + " to review . " + skipped + " left for you . " + elapsed() + "s total.";
    setReport(summary, rows);
    try {
      chrome.runtime.sendMessage({ type: "FETCH_FILLLOG", payload: {
        url: location.href, company: companyGuess(), role: roleGuess(),
        summary: summary, filled: filled, review: review, skipped: skipped,
        rows: rows.slice(0, 300).map(function (r) { return { q: r[0], status: r[1], detail: r[2] }; })
      } }).catch(function () {});
    } catch (_) {}
  }

  // Auto-fill when the dashboard opens an apply page with #penates-fill.
  function autoFillIfRequested() {
    if (!IS_TOP) return;
    if (!/penates-fill/i.test((location.hash || "") + (location.search || ""))) return;
    var tries = 0;
    var iv = setInterval(function () {
      if (looksLikeApplication() || ++tries > 15) {
        clearInterval(iv);
        if (looksLikeApplication()) runFillAll();
      }
    }, 1000);
  }

  // ---- report panel ----
  let report, fillBusy = false;
  function openReport() {
    if (report) { report.style.display = "block"; return; }
    report = document.createElement("div");
    report.setAttribute("data-penates", "report");
    // Defensive reset so the host page's CSS (global *-rules, !important line-height/position/
    // white-space) can't squish the panel. We reset only the box/inheritance props a hostile
    // page overrides and leave layout (display/flex/color/font-size/padding set inline) alone.
    if (!document.getElementById("penates-report-css")) {
      const st = document.createElement("style");
      st.id = "penates-report-css";
      st.textContent =
        '[data-penates="report"], [data-penates="report"] *{' +
          'box-sizing:border-box!important;line-height:1.5!important;white-space:normal!important;' +
          'float:none!important;transform:none!important;letter-spacing:normal!important;' +
          'text-indent:0!important;vertical-align:baseline!important;min-height:0!important;' +
          'max-width:none!important;margin:0!important;text-shadow:none!important;' +
          'position:static!important;font-family:system-ui,sans-serif!important}' +
        '[data-penates="report"]{position:fixed!important}';   // container itself must stay fixed
      (document.head || document.documentElement).appendChild(st);
    }
    report.style.cssText =
      "position:fixed;right:16px;top:16px;z-index:2147483647;width:460px;max-width:94vw;max-height:82vh;overflow:auto;" +
      "background:#0f172a;color:#e5e7eb;border:1px solid #334155;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.45);" +
      "font:12.5px/1.5 system-ui,sans-serif";
    report.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;padding:10px 12px;background:#111827;border-bottom:1px solid #334155;position:sticky;top:0;z-index:2">' +
        '<span style="font-weight:700;font-size:13px">Penates</span>' +
        '<span style="color:#64748b;font-size:11px">' + PENATES_BUILD + '</span>' +
        '<span style="flex:1"></span>' +
        '<button data-r="applied" title="Log this as an application after you submit" style="background:#166534;color:#fff;border:1px solid #14532d;border-radius:6px;padding:4px 9px;cursor:pointer;font:11px system-ui,sans-serif">Mark applied</button>' +
        '<span data-r="x" title="Close" style="cursor:pointer;color:#94a3b8;padding:0 4px;font-size:14px">✕</span>' +
      '</div>' +
      '<div data-r="sum" style="padding:8px 12px;background:#0b1220;border-bottom:1px solid #1e293b;color:#fbbf24;font-weight:600"></div>' +
      '<div data-r="body" style="padding:4px 10px 10px"></div>';
    document.documentElement.appendChild(report);
    report.querySelector('[data-r="x"]').onclick = () => (report.style.display = "none");
    report.querySelector('[data-r="applied"]').onclick = markApplied;
  }
  function setReport(summary, rows) {
    if (!report) return;
    // Remix/React boards (Greenhouse job-boards) can re-render the whole document mid-fill and drop
    // foreign nodes; put the panel back instead of silently losing the report.
    if (!report.isConnected) { try { document.documentElement.appendChild(report); } catch (_) {} }
    const _sumEl = report.querySelector('[data-r="sum"]');
    _sumEl.textContent = summary || "";
    _sumEl.style.color = /^\u2713 DONE/.test(summary || "") ? "#34d399" : "#fbbf24";  // green=done, amber=working
    const color = { filled: "#34d399", review: "#fbbf24", skip: "#94a3b8" };
    report.querySelector('[data-r="body"]').innerHTML = (rows || []).map((r) =>
      '<div style="display:flex;gap:9px;padding:6px 2px;border-bottom:1px solid #1e293b;align-items:flex-start">' +
        '<span style="width:48px;flex:none;color:' + (color[r[1]] || "#e5e7eb") + ';font-weight:700;text-transform:uppercase;font-size:10px;padding-top:2px;letter-spacing:.03em">' + escapeHtml(r[1]) + '</span>' +
        '<span style="flex:1;min-width:0">' +
          '<span style="color:#e2e8f0;display:block;word-break:break-word">' + escapeHtml(r[0]) + '</span>' +
          (r[2] ? '<span style="color:#94a3b8;display:block;font-size:11px;word-break:break-word">' + escapeHtml(r[2]) + '</span>' : '') +
        '</span>' +
      '</div>').join("") || '<div style="color:#94a3b8;padding:6px">No fields resolved.</div>';
  }
  function escapeHtml(s) { return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  // ---- floating launcher (top frame only, and only on application-like pages) ----
  let launcherMounted = false, launcherBtn = null;
  const ATS_HOST = /greenhouse|lever\.co|ashbyhq|myworkdayjobs|workday|icims|paylocity|smartrecruiters|jobvite|teksystems|taleo|breezy|workable|bamboohr|recruitee|applytojob|dayforce|jobs\.|careers?\./i;
  function looksLikeApplication() {
    if (ATS_HOST.test(location.hostname)) return true;
    // NOTE: no getBoundingClientRect / innerText here on purpose - those force a
    // layout that makes strict-CSP pages (e.g. GitHub) try to fetch a blocked web
    // font, spamming the console. Attribute-only checks below never force layout.
    let fillable = 0, hasIdentity = false, els;
    try { els = document.querySelectorAll("input, textarea, select"); } catch (_) { return false; }
    for (const e of els) {
      const t = (e.type || "").toLowerCase();
      if (e.tagName === "INPUT" && /^(hidden|submit|button|image|reset|checkbox|radio)$/.test(t)) continue;
      if (e.disabled || e.hidden || inPenates(e)) continue;
      const st = e.style;   // reading inline style is cheap; does not force reflow
      if (st && (st.display === "none" || st.visibility === "hidden")) continue;
      fillable++;
      const lbl = ((e.getAttribute("aria-label") || e.placeholder || e.name || "") + "").toLowerCase();
      if (t === "email" || /first name|last name|full name|e-?mail|phone|resume|cover letter|linkedin/.test(lbl)) hasIdentity = true;
    }
    return fillable >= 4 && hasIdentity;
  }
  function mountLauncher() {
    if (!CAN_SURFACE || launcherMounted) return;
    if (!surfaceAllowed()) return;
    launcherMounted = true;
    const b = document.createElement("button");
    launcherBtn = b;
    b.setAttribute("data-penates", "launch");
    b.textContent = IS_TOP ? "Fill application" : "Fill in new tab";
    b.type = "button";
    b.style.cssText =
      "position:fixed;inset:auto;right:16px;bottom:16px;z-index:2147483646;margin:0;font:600 13px system-ui,sans-serif;" +
      "padding:9px 14px;border:1px solid #1e40af;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;" +
      "box-shadow:0 2px 10px rgba(0,0,0,.35)";
    b.addEventListener("click", (e) => {
      e.preventDefault();
      if (IS_TOP) { runFillAll(); return; }
      // Embedded ATS form: its dropdowns are a cross-origin react-select that reverts synthetic
      // selections in the iframe. Open the SAME form standalone (top frame) and auto-fill it there.
      // window.open is blocked inside a sandboxed iframe, so ask the background worker to open it.
      try {
        const u = new URL(location.href); u.hash = "penates-fill";
        chrome.runtime.sendMessage({ type: "OPEN_FILL_TAB", url: u.toString() });
      } catch (_) { try { runFillAll(); } catch (__) {} }
    });
    (document.body || document.documentElement).appendChild(b);
    // Same re-render hazard for the launcher: re-attach if the page drops it.
    try { setInterval(() => { if (!b.isConnected) { (document.body || document.documentElement).appendChild(b); try { if (b.hasAttribute("popover")) b.showPopover(); } catch (_) {} } }, 2000); } catch (_) {}
    // Some ATS pages put a transform/filter/overflow-clip on <body>, which "contains" a
    // position:fixed button and cuts off its hit-area. The top layer ignores all that.
    try {
      if (typeof b.showPopover === "function") { b.setAttribute("popover", "manual"); b.showPopover(); }
    } catch (_) { try { b.removeAttribute("popover"); } catch (__) {} }
  }
  let _reevalT = 0;
  function reevaluateDebounced() { clearTimeout(_reevalT); _reevalT = setTimeout(reevaluate, 350); }
  function watchForForm() {
    if (!CAN_SURFACE) return;
    reevaluate();
    let obs = null;
    try { obs = new MutationObserver(reevaluateDebounced); obs.observe(document.documentElement, { childList: true, subtree: true }); } catch (_) {}
    window.addEventListener("popstate", reevaluateDebounced);
    window.addEventListener("hashchange", reevaluateDebounced);
    let tries = 0;
    const iv = setInterval(() => { reevaluate(); if (++tries > 20) clearInterval(iv); }, 1000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { watchForForm(); autoFillIfRequested(); });
  else { watchForForm(); autoFillIfRequested(); }

  // ============================ PER-FIELD CARD ============================
  function run(el) {
    el = el || lastEditable || (isEditable(document.activeElement) ? document.activeElement : null);
    if (!isEditable(el)) return toast("No answer field focused", true);
    const q = questionFor(el);
    currentEl = el; currentQuestion = q || "";
    openCard();
    if (!q) { startPick(); return; }
    fetchAndFill(false);
  }
  async function fetchAndFill(fresh) {
    const typed = clean(els.q && els.q.value);
    let qChanged = false;
    if (typed && typed !== currentQuestion) { currentQuestion = typed; fresh = true; qChanged = true; }
    if (!currentQuestion) { setMeta("Click the question on the page, or type it in the box above.", true); return; }
    stopPick();
    setCardBusy(true, fresh ? "Regenerating..." : "Drafting a local answer...");
    const limit = currentEl && currentEl.maxLength && currentEl.maxLength > 0 ? currentEl.maxLength : null;
    const prevDraft = fresh && !qChanged && els.text ? (els.text.value || "").trim() : "";
    const data = await askEngine(currentQuestion, limit, fresh, undefined, false, null, prevDraft);
    setCardBusy(false);
    if (!data || data.__error) { setMeta(data && data.__error ? data.__error : "serve.py not reachable on :8765", true); return; }
    els.text.value = data.text || "";
    els.method = data.method;
    const gap = data.gaps && data.gaps.length;
    const chk = data.checks && data.checks.length;
    const parts = [];
    if (data.genre) parts.push(data.genre);
    if (data.grounding && data.grounding !== "none") parts.push("grounded: " + data.grounding);
    else if (data.grounding === "none" && (data.genre === "why_company" || data.genre === "why_role")) parts.push("no company facts");
    if (data.story) parts.push("story: " + data.story);
    parts.push((data.words ? data.words + "w" : ((data.chars || (data.text || "").length) + " chars")));
    if (chk) parts.push("review: " + data.checks.join(", "));
    else if (gap) parts.push("gap: " + data.gaps.join(", "));
    else if (!data.genre) parts.unshift(data.method || "done");
    setMeta(parts.join("  .  "), !!(gap || chk));
    autosize(); els.text.focus();
  }
  function doInsert() {
    if (!currentEl) return closeCard();
    const val = els.text.value;
    setNative(currentEl, val);
    if (els.method && els.method !== "field" && val.trim().length >= 20) {
      chrome.runtime.sendMessage({ type: "FETCH_LEARN",
        payload: { question: currentQuestion, answer: val, company: companyGuess() } }).catch(() => {});
    }
    closeCard();
    toast("Inserted" + (els.method === "learned" ? " (your saved answer)" : ""));
  }
  let card, els = {};
  function openCard() {
    if (card) { card.style.display = "block"; if (els && els.q) els.q.value = currentQuestion; return; }
    card = document.createElement("div");
    card.setAttribute("data-penates", "card");
    card.style.cssText =
      "position:fixed;right:16px;bottom:64px;z-index:2147483647;width:400px;max-width:92vw;" +
      "background:#0f172a;color:#e5e7eb;border:1px solid #334155;border-radius:10px;" +
      "box-shadow:0 8px 30px rgba(0,0,0,.45);font:13px/1.45 system-ui,sans-serif;overflow:hidden";
    card.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;padding:9px 12px;background:#111827;border-bottom:1px solid #334155">' +
        '<span style="font-weight:600">Penates</span>' +
        '<input data-r="q" placeholder="Question (click it on the page, or type it here)" title="The question Penates answers. Edit it, then Regenerate." ' +
          'style="flex:1;min-width:0;background:transparent;color:#cbd5e1;border:1px solid #1f2937;border-radius:4px;padding:2px 4px;font:12px system-ui,sans-serif">' +
        '<span data-r="x" title="Close" style="cursor:pointer;color:#94a3b8;padding:0 4px">✕</span></div>' +
      '<div style="padding:10px 12px">' +
        '<div data-r="meta" style="font-size:11px;color:#94a3b8;margin-bottom:6px;min-height:14px"></div>' +
        '<textarea data-r="text" spellcheck="true" style="width:100%;box-sizing:border-box;min-height:120px;max-height:46vh;' +
          'resize:vertical;background:#0b1220;color:#e5e7eb;border:1px solid #334155;border-radius:6px;padding:8px;' +
          'font:13px/1.5 ui-monospace,Menlo,monospace"></textarea>' +
        '<div style="display:flex;gap:8px;margin-top:10px;justify-content:flex-end">' +
          '<button data-r="regen" style="' + btnCss("#1f2937") + '">Regenerate</button>' +
          '<button data-r="cancel" style="' + btnCss("#1f2937") + '">Cancel</button>' +
          '<button data-r="insert" style="' + btnCss("#2563eb") + ';font-weight:600">Insert</button>' +
        '</div></div>';
    document.documentElement.appendChild(card);
    const q = (s) => card.querySelector('[data-r="' + s + '"]');
    els = { q: q("q"), meta: q("meta"), text: q("text"), insert: q("insert"), regen: q("regen"), cancel: q("cancel"), close: q("x") };
    els.insert.onclick = doInsert;
    els.regen.onclick = () => fetchAndFill(true);
    els.cancel.onclick = closeCard;
    els.close.onclick = closeCard;
    els.text.addEventListener("input", autosize);
    els.text.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doInsert(); }
      else if (e.key === "Escape") { e.preventDefault(); closeCard(); }
    });
    els.q.value = currentQuestion;
    els.q.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); fetchAndFill(true); }
      else if (e.key === "Escape") { e.preventDefault(); closeCard(); }
    });
  }
  function btnCss(bg) { return "background:" + bg + ";color:#fff;border:1px solid #334155;border-radius:6px;padding:6px 12px;cursor:pointer;font:13px system-ui,sans-serif"; }
  function setMeta(msg, warn) { if (els.meta) { els.meta.textContent = msg; els.meta.style.color = warn ? "#fbbf24" : "#94a3b8"; } }
  function setCardBusy(b, msg) { if (!els.insert) return; els.insert.disabled = b; els.regen.disabled = b; els.insert.style.opacity = b ? ".5" : "1"; els.regen.style.opacity = b ? ".5" : "1"; if (b && msg) setMeta(msg); }
  function autosize() { const t = els.text; if (!t) return; t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight + 2, Math.round(window.innerHeight * 0.46)) + "px"; }
  function closeCard() { stopPick(); if (card) card.style.display = "none"; currentEl = null; }

  // ---- pick mode: label unreadable -> click the question text on the page (no shortcut) ----
  let picking = false, pickHover = null, pickHoverOutline = "";
  function pickTarget(e) {
    const t = (e.composedPath ? e.composedPath()[0] : e.target);
    return t && t.nodeType === 1 ? t : (t && t.parentElement) || null;
  }
  function pickUnhover() { if (pickHover) { pickHover.style.outline = pickHoverOutline; pickHover = null; } }
  function onPickOver(e) {
    const t = pickTarget(e);
    if (!t || inPenates(t) || t === pickHover) return;
    pickUnhover(); pickHover = t; pickHoverOutline = t.style.outline; t.style.outline = "2px dashed #f59e0b";
  }
  function onPickBlock(e) {
    const t = pickTarget(e);
    if (!t || inPenates(t)) return;
    e.preventDefault(); e.stopPropagation();   // no focus change, link nav, or selection
  }
  function onPickClick(e) {
    const t = pickTarget(e);
    if (!t || inPenates(t)) return;
    e.preventDefault(); e.stopPropagation();
    const txt = clean(t.innerText || t.textContent || "");
    if (txt.length < 8) { setMeta("Too little text there. Click the question itself.", true); return; }
    if (txt.length > 500) { setMeta("That block is too big. Click closer to the question.", true); return; }
    pickUnhover();
    const old = t.style.outline; t.style.outline = "2px solid #22c55e";
    setTimeout(() => { t.style.outline = old; }, 900);
    currentQuestion = txt; if (els.q) els.q.value = txt;
    fetchAndFill(false);
  }
  function onPickKey(e) { if (e.key === "Escape") { e.preventDefault(); closeCard(); } }
  function startPick() {
    if (picking) return;
    picking = true;
    setMeta("Click the question text on the page (or type it above).", true);
    document.addEventListener("mouseover", onPickOver, true);
    document.addEventListener("pointerdown", onPickBlock, true);
    document.addEventListener("mousedown", onPickBlock, true);
    document.addEventListener("click", onPickClick, true);
    document.addEventListener("keydown", onPickKey, true);
  }
  function stopPick() {
    if (!picking) return;
    picking = false; pickUnhover();
    document.removeEventListener("mouseover", onPickOver, true);
    document.removeEventListener("pointerdown", onPickBlock, true);
    document.removeEventListener("mousedown", onPickBlock, true);
    document.removeEventListener("click", onPickClick, true);
    document.removeEventListener("keydown", onPickKey, true);
  }

  // ---- focus chip ----
  function showChip(el) {
    if (card && card.style.display !== "none") return;
    if (!chip) {
      chip = document.createElement("button");
      chip.textContent = "Penates"; chip.type = "button"; chip.setAttribute("data-penates", "chip");
      chip.style.cssText = "position:absolute;z-index:2147483646;font:12px/1 system-ui,sans-serif;padding:4px 8px;" +
        "border:1px solid #334155;border-radius:6px;background:#111827;color:#fff;cursor:pointer;opacity:.9;box-shadow:0 1px 4px rgba(0,0,0,.3)";
      chip.addEventListener("mousedown", (e) => e.preventDefault());
      chip.addEventListener("click", (e) => { e.preventDefault(); run(lastEditable); });
      (document.body || document.documentElement).appendChild(chip);
    }
    const r = el.getBoundingClientRect();
    chip.style.top = (window.scrollY + r.top - 26) + "px";
    chip.style.left = (window.scrollX + r.right - 64) + "px";
    chip.style.display = "block";
  }
  function hideChip() { if (chip && document.activeElement !== chip) chip.style.display = "none"; }

  // ---- toast ----
  let tEl, tTimer;
  function toast(msg, warn) {
    if (!tEl) {
      tEl = document.createElement("div"); tEl.setAttribute("data-penates", "toast");
      tEl.style.cssText = "position:fixed;bottom:16px;left:16px;z-index:2147483647;max-width:340px;font:13px/1.4 system-ui,sans-serif;" +
        "padding:8px 12px;border-radius:6px;color:#fff;box-shadow:0 2px 8px rgba(0,0,0,.35)";
      document.body.appendChild(tEl);
    }
    tEl.textContent = msg; tEl.style.background = warn ? "#b45309" : "#1f2937"; tEl.style.display = "block";
    clearTimeout(tTimer); tTimer = setTimeout(() => (tEl.style.display = "none"), 3500);
  }

  // ---- messages from background ----
  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg) return;
    if (msg.type === "ANSWER_FIELD") run();
    else if (msg.type === "FILL_ALL" && CAN_SURFACE) runFillAll();
  });
})();
