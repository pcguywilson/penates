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
  const PENATES_BUILD = "build 10";   // shown in the report header; if you don't see it after a reload, the extension didn't update
  const IS_TOP = window.top === window;
  let currentEl = null, currentQuestion = "", lastEditable = null, chip = null;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const norm  = (s) => clean(s).toLowerCase().replace(/[^a-z0-9 ]+/g, "").trim();

  const isEditable = (el) =>
    !!el && (el.tagName === "TEXTAREA" ||
      (el.tagName === "INPUT" && /^(text|search|email|url|tel|number|)$/i.test(el.type || "")) ||
      el.isContentEditable);

  const inPenates = (el) => !!(el && el.closest && el.closest("[data-penates]"));

  // ===== surface gating (only show on real application forms) =====
  // User overrides live in chrome.storage.local "penatesSettings" and win over detection,
  // so if the heuristic ever guesses wrong the owner fixes it by site in the options page.
  let SETTINGS = { mode: "forms", alwaysHosts: [], neverHosts: [] };
  const DENY = [
    "mail.google.com", "docs.google.com", "drive.google.com", "calendar.google.com",
    "github.com", "gitlab.com", "bitbucket.org", "reddit.com", "x.com", "twitter.com",
    "youtube.com", "chatgpt.com", "chat.openai.com", "claude.ai", "stackoverflow.com",
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
    if (!IS_TOP) return;
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

  // ---- question / label text for ANY field (light DOM + shadow climb for SLDS) ----
  function questionFor(el) {
    const sel = clean(window.getSelection && String(window.getSelection()));
    if (el === (currentEl || lastEditable) && sel.length >= 12 && sel.length <= 500) return sel;
    const tries = [];
    const root = el.getRootNode ? el.getRootNode() : document;
    if (el.id) {
      let l = null;
      try { l = root.querySelector && root.querySelector('label[for="' + CSS.escape(el.id) + '"]'); } catch (_) {}
      if (l) tries.push(l.innerText);
    }
    const wrap = el.closest && el.closest("label"); if (wrap) tries.push(wrap.innerText);
    const lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) tries.push(lb.split(/\s+/).map((i) => (document.getElementById(i) || {}).innerText || "").join(" "));
    if (el.getAttribute) tries.push(el.getAttribute("aria-label"));
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
    for (const t of tries) { const c = clean(t); if (c && c.length >= 3) return c.slice(0, 400); }
    return "";
  }

  // For a radio/checkbox OPTION, questionFor() returns the option's own label (e.g. ">100"),
  // not the group's question. That sends the wrong text to the engine. Climb to the container
  // that holds the group's question heading and return that instead.
  function groupQuestion(el) {
    let p = el;
    for (let i = 0; i < 9 && p; i++) {
      p = p.parentElement; if (!p) break;
      const t = clean(p.innerText || "");
      if (t.length > 15 && /\?|do you|are you|have you|which|what|size of|authoriz|identify|select|how (did|do|would)|please (select|describe)|describe/i.test(t)) {
        return t.replace(/\s*(Yes\s*No|No\s*Yes)\s*$/i, "").slice(0, 240);
      }
    }
    return questionFor(el);   // fallback
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

  function roleGuess() {
    const t = clean(document.title);
    let m = t.match(/(?:application|apply)\s+(?:for|:)\s+(.+?)\s+(?:at|@|-|\|)\s+/i);
    if (m) return m[1].slice(0, 100);
    m = t.match(/^(.+?)\s+(?:at|@)\s+/i);
    if (m) return m[1].slice(0, 100);
    return t.split(/[|\-–—·]/)[0].trim().slice(0, 100);
  }

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
  async function askEngine(question, limit, fresh, timeoutMs, bulk) {
    const payload = { question, company: companyGuess(), url: location.href, page_context: pageContext(), limit: limit || null, fresh: !!fresh, bulk: !!bulk };
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

  const CONSENT_RE = /consent|i agree|i acknowledge|terms|certif|i have read|authorize|electronic signature|e-?sign|privacy/i;

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
  function skipQuestion(q) { return !!q && (CONDITIONAL_RE.test(q) || LEAVE_BLANK_RE.test(q)); }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function isCombobox(el) {
    if (!el || el.tagName !== "INPUT") return false;
    if ((el.getAttribute("role") || "") === "combobox") return true;
    if ((el.getAttribute("aria-autocomplete") || "") === "list") return true;
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
  function comboCommitted(el) {
    const ctrl = (el.closest && el.closest('[class*="control"], [class*="select"], [class*="combobox"]')) || el.parentElement;
    const sv = ctrl && ctrl.querySelector && ctrl.querySelector('[class*="single-value"], [class*="singleValue"], [class*="multi-value"], [class*="multiValue"]');
    return !!(sv && norm(sv.textContent));
  }
  function fireMouse(node) {
    for (const t of ["pointerdown", "mousedown", "mouseup", "click"]) {
      try { node.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })); } catch (_) {}
    }
  }
  async function fillCombobox(el, value) {
    // Ashby (and most react-select) comboboxes: open with ArrowDown, type to filter, then
    // click the option. Two things the old version missed and this handles: async typeahead
    // menus that show "Loading..." first (poll for real options), and picking the RIGHT
    // option among near-duplicates (token overlap, not first-substring - so "St. Albans,
    // West Virginia" beats the UK / Vermont "St Albans").
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    const query = (String(value).split(",")[0] || value).trim();
    const nv = norm(value);
    const vtokens = nv.split(" ").filter((w) => w.length >= 2);
    el.focus();
    try { el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })); } catch (_) {}
    await sleep(120);
    try { setter.call(el, query); el.dispatchEvent(new InputEvent("input", { bubbles: true, data: query, inputType: "insertText" })); } catch (_) {}
    let opts = [];
    for (let i = 0; i < 16; i++) {
      await sleep(250);
      const menu = document.getElementById(el.getAttribute("aria-controls") || "__none__") || document.querySelector('[role="listbox"]');
      opts = menu ? [...menu.querySelectorAll('[role="option"]')] : [];
      if (!opts.length) { try { opts = [...document.querySelectorAll('[role="option"]')].filter((o) => o.offsetParent); } catch (_) {} }
      if (opts.length) break;
    }
    if (!opts.length) return false;
    // pick the option sharing the most tokens with the wanted value (exact full match wins)
    let best = null, bestScore = -1;
    for (const o of opts) {
      const ot = norm(o.textContent);
      let score = 0;
      for (const t of vtokens) if ((" " + ot + " ").includes(" " + t + " ")) score++;
      if (ot === nv) score += 100;
      if (score > bestScore) { bestScore = score; best = o; }
    }
    const targetText = norm((best || opts[0]).textContent);
    // Synthetic mouse events don't reliably select in react-select v5; keyboard does. ArrowDown
    // until the intended option is the active descendant, then Enter. Only commit if we actually
    // reached it - never blind-Enter onto whatever happens to be highlighted.
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
    return el.getAttribute("aria-expanded") === "false" || comboCommitted(el) || norm(el.value) === targetText;
  }

  async function runFillAll() {
    if (fillBusy) return;
    fillBusy = true;
    openReport();
    setReport("Scanning form...", []);
    const fields = deepFields().filter((el) => {
      if (inPenates(el)) return false;
      const t = (el.type || el.tagName).toLowerCase();
      if (el.tagName === "INPUT" && SKIP_TYPES.test(el.type || "")) return false;
      if (el.disabled || el.readOnly) return false;
      return shown(el);
    });

    const rows = [];
    const groupsDone = new Set();
    let filled = 0, review = 0, skipped = 0;
    const t0 = Date.now();
    const elapsed = () => ((Date.now() - t0) / 1000).toFixed(0);
    let seen = 0;

    for (const el of fields) {
      const tag = el.tagName, typ = (el.type || "").toLowerCase();
      seen++;
      setReport("Filling " + seen + "/" + fields.length + " ... " + elapsed() + "s elapsed. NOTHING submitted.", rows);

      // ---- radio / checkbox: resolve on the GROUP question, click matching option ----
      if (tag === "INPUT" && (typ === "radio" || typ === "checkbox")) {
        // Button-backed Yes/No render as a display:none checkbox behind visible <button>s.
        // Skip anything with no layout box so it can't preempt (and mis-resolve) the group;
        // the button pass clicks the real buttons.
        const _r = el.getBoundingClientRect();
        if (el.offsetParent === null || getComputedStyle(el).display === "none" || (!_r.width && !_r.height)) continue;
        const q = groupQuestion(el);
        const gkey = (el.name || q || "").trim();
        if (!q) { continue; } // no question resolved: skip silently (avoid random toggles)
        if (gkey && groupsDone.has(gkey)) continue;
        // NOTE: no CONDITIONAL_RE skip here. A radio/checkbox group is the primary question,
        // even when its text contains "if so, what capacity" (the veteran question). Conditional
        // follow-ups that must be left blank are text boxes, handled in the text branch.

        // required consent / single acknowledgement checkbox -> tick it
        const optlab = optionLabel(el);
        if (typ === "checkbox" && CONSENT_RE.test(q + " " + optlab) && !/which|select all|open to|settings/i.test(q)) {
          const ok = commitOption(el);
          rows.push([q.slice(0, 60), ok ? "filled" : "skip", ok ? "checked" : "couldn't check"]);
          ok ? filled++ : skipped++;
          if (gkey) groupsDone.add(gkey);
          continue;
        }

        const data = await askEngine(q, 25, false, 40000, true);
        if (!data || data.__error) { rows.push([q.slice(0, 60), "skip", data && data.__error ? "engine: " + data.__error : "no answer"]); skipped++; continue; }
        const ans = (data.text || "").trim();
        if (!ans) { rows.push([q.slice(0, 60), "skip", "no grounded answer -> you"]); skipped++; if (gkey) groupsDone.add(gkey); continue; }
        // collect this group's option inputs (same name, or same question via shadow)
        const groupEls = fields.filter((o) => o.tagName === "INPUT" && (o.type || "").toLowerCase() === typ &&
          ((el.name && o.name === el.name) || (!el.name && questionFor(o) === q)));
        const na = norm(ans);
        // Score every option and pick the BEST, so an exact match always beats a loose
        // substring one. Without this, answer "1000-2499" (norm "10002499") wrongly matched
        // ">100" (norm "100") because "10002499" starts with "100", and "Man" could match
        // inside "Woman".
        const scoreOf = (ol) => {
          if (!ol) return -1;
          if (ol === na) return 100;
          if (na && ol.startsWith(na)) return 70;
          if (na && na.startsWith(ol) && ol.length >= 3) return 60;
          if (na.length >= 3 && (" " + ol + " ").includes(" " + na + " ")) return 50;  // whole word
          if (na.length >= 5 && ol.includes(na)) return 20;                              // loose, long only
          return -1;
        };
        let best = null, bestS = 0;
        for (const o of (groupEls.length ? groupEls : [el])) {
          const s = scoreOf(norm(optionLabel(o)));
          if (s > bestS) { bestS = s; best = o; }
        }
        let clicked = null;
        if (best && commitOption(best)) clicked = optionLabel(best);
        if (gkey) groupsDone.add(gkey);
        if (clicked) { rows.push([q.slice(0, 60), "filled", clicked]); filled++; }
        else { rows.push([q.slice(0, 60), "skip", "answer '" + ans + "' not among options"]); skipped++; }
        continue;
      }

      // ---- select ----
      if (tag === "SELECT") {
        const q = questionFor(el);
        if (!q) { continue; }
        if (skipQuestion(q)) { rows.push([q.slice(0, 60), "skip", "conditional/optional -> you"]); skipped++; continue; }
        if (el.value && el.selectedIndex > 0 && clean(el.options[el.selectedIndex].text)) { continue; } // already set
        const data = await askEngine(q, 40, false, 40000, true);
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
        if (skipQuestion(q)) { rows.push([q.slice(0, 60), "skip", "conditional/optional -> you"]); skipped++; continue; }
        const cur = (el.value || el.textContent || "").trim();
        if (cur) { continue; }                        // don't clobber prefilled
        const limit = el.maxLength && el.maxLength > 0 ? el.maxLength : null;
        const data = await askEngine(q, limit, false, 40000, true);
        if (data && data.method === "essay-skip") { rows.push([q.slice(0, 60), "review", "essay -> click the field to draft it"]); review++; continue; }
        if (!data || data.__error || !data.text) { rows.push([q.slice(0, 60), "skip", data && data.__error ? "engine: " + data.__error : "no answer -> you"]); skipped++; continue; }
        // a bare structured value (tier-1 field match) belongs in an input/select, never a prose
        // box: this is where a demographic token leaks into a free-text field. Leave it for you.
        if (tag === "TEXTAREA" && data.method === "field") { rows.push([q.slice(0, 60), "skip", "structured value not for a text box -> you"]); skipped++; continue; }
        const isCombo = isCombobox(el);
        const singleLine = tag !== "TEXTAREA" && !el.isContentEditable;
        const isEssay = data.method && data.method !== "field" && data.method !== "learned";
        // never write a generated essay into a one-line field (URL / short inputs)
        if (singleLine && !isCombo && isEssay) {
          rows.push([q.slice(0, 60), "skip", "no structured value -> you (won't essay a short field)"]); skipped++; continue;
        }
        const gap = data.gaps && data.gaps.length;
        if (isCombo) {
          const ok = await fillCombobox(el, data.text);
          if (ok) { rows.push([q.slice(0, 60), "filled", data.text.slice(0, 60)]); filled++; }
          else { rows.push([q.slice(0, 60), "skip", "combobox: '" + data.text.slice(0, 40) + "' not selectable -> you"]); skipped++; }
          continue;
        }
        setNative(el, data.text);
        if (gap) { rows.push([q.slice(0, 60), "review", "gap: " + data.gaps.join(", ") + " | " + data.text.slice(0, 40)]); review++; }
        else if (isEssay) { rows.push([q.slice(0, 60), "review", "essay (" + (data.method || "") + ") - read it | " + data.text.slice(0, 40)]); review++; }
        else { rows.push([q.slice(0, 60), "filled", data.text.slice(0, 60)]); filled++; }
        continue;
      }
    }

    // ---- second pass: choice questions rendered as <button> (Ashby Yes/No etc.) ----
    // deepFields only walks input/textarea/select, so button-based Yes/No groups are never
    // seen. Collect short-text option buttons, group them by their question, and click the
    // one matching the engine's answer (e.g. the deterministic COI "No").
    try {
      const BTN_SKIP = /^(submit|next|back|previous|continue|upload|choose file|browse|add another|add|remove|delete|apply|save|autofill|fill|regenerate|insert|done|close|cancel|\+|\-|×|x)$/i;
      const climbQ = (el) => {
        let p = el;
        for (let i = 0; i < 8 && p; i++) {
          p = p.parentElement; if (!p) break;
          const t = clean(p.innerText || "");
          if (t.length > 15 && /\?|describe|do you|are you|have you|which|what|size of|authoriz|sponsor/i.test(t)) {
            return t.replace(/\s*(Yes\s*No|No\s*Yes)\s*$/i, "").trim().slice(0, 160);
          }
        }
        return "";
      };
      // NOTE: do NOT exclude type="submit" here. Ashby's Yes/No option buttons are
      // <button type="submit"> (a button in a form defaults to submit), yet clicking one only
      // toggles the choice (Ashby prevents default). The real Submit button is excluded by
      // BTN_SKIP text ("Submit Application"), and only 2-8 button groups sharing a question are
      // ever clicked, so a lone submit button is never touched.
      const cand = [...document.querySelectorAll("button")].filter((b) => {
        if (inPenates(b) || b.disabled || !shown(b)) return false;
        const t = clean(b.textContent);
        return t && t.length <= 40 && !BTN_SKIP.test(t);
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
        if (skipQuestion(q)) { rows.push([q.slice(0, 60), "skip", "conditional/optional -> you"]); skipped++; continue; }
        // already answered (Ashby marks the picked option aria-pressed=true)?
        if (btns.some((b) => b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-checked") === "true")) continue;
        seen++;
        setReport("Filling " + seen + " (choice) ... " + elapsed() + "s elapsed. NOTHING submitted.", rows);
        const data = await askEngine(q, 25, false, 40000, true);
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

    fillBusy = false;
    const summary = "Filled " + filled + " . " + review + " to review . " + skipped + " left for you . " + elapsed() + "s total . NOTHING submitted.";
    setReport(summary, rows);
    try {
      chrome.runtime.sendMessage({ type: "FETCH_FILLLOG", payload: {
        url: location.href, company: companyGuess(), role: roleGuess(),
        summary: summary, filled: filled, review: review, skipped: skipped,
        rows: rows.slice(0, 40).map(function (r) { return { q: r[0], status: r[1], detail: r[2] }; })
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
    report.querySelector('[data-r="sum"]').textContent = summary || "";
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
    if (!IS_TOP || launcherMounted) return;
    if (!surfaceAllowed()) return;
    launcherMounted = true;
    const b = document.createElement("button");
    launcherBtn = b;
    b.setAttribute("data-penates", "launch");
    b.textContent = "Fill application";
    b.type = "button";
    b.style.cssText =
      "position:fixed;right:16px;bottom:16px;z-index:2147483646;font:600 13px system-ui,sans-serif;" +
      "padding:9px 14px;border:1px solid #1e40af;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;" +
      "box-shadow:0 2px 10px rgba(0,0,0,.35)";
    b.addEventListener("click", (e) => { e.preventDefault(); runFillAll(); });
    (document.body || document.documentElement).appendChild(b);
  }
  let _reevalT = 0;
  function reevaluateDebounced() { clearTimeout(_reevalT); _reevalT = setTimeout(reevaluate, 350); }
  function watchForForm() {
    if (!IS_TOP) return;
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
    if (!q) return toast("Could not read the question. Highlight it, then Alt+A.", true);
    currentEl = el; currentQuestion = q;
    openCard(); fetchAndFill(false);
  }
  async function fetchAndFill(fresh) {
    setCardBusy(true, fresh ? "Regenerating..." : "Drafting a local answer...");
    const limit = currentEl && currentEl.maxLength && currentEl.maxLength > 0 ? currentEl.maxLength : null;
    const data = await askEngine(currentQuestion, limit, fresh);
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
    if (card) { card.style.display = "block"; return; }
    card = document.createElement("div");
    card.setAttribute("data-penates", "card");
    card.style.cssText =
      "position:fixed;right:16px;bottom:64px;z-index:2147483647;width:400px;max-width:92vw;" +
      "background:#0f172a;color:#e5e7eb;border:1px solid #334155;border-radius:10px;" +
      "box-shadow:0 8px 30px rgba(0,0,0,.45);font:13px/1.45 system-ui,sans-serif;overflow:hidden";
    card.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;padding:9px 12px;background:#111827;border-bottom:1px solid #334155">' +
        '<span style="font-weight:600">Penates</span>' +
        '<span data-r="q" style="flex:1;color:#94a3b8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></span>' +
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
    els.q.textContent = currentQuestion;
  }
  function btnCss(bg) { return "background:" + bg + ";color:#fff;border:1px solid #334155;border-radius:6px;padding:6px 12px;cursor:pointer;font:13px system-ui,sans-serif"; }
  function setMeta(msg, warn) { if (els.meta) { els.meta.textContent = msg; els.meta.style.color = warn ? "#fbbf24" : "#94a3b8"; } }
  function setCardBusy(b, msg) { if (!els.insert) return; els.insert.disabled = b; els.regen.disabled = b; els.insert.style.opacity = b ? ".5" : "1"; els.regen.style.opacity = b ? ".5" : "1"; if (b && msg) setMeta(msg); }
  function autosize() { const t = els.text; if (!t) return; t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight + 2, Math.round(window.innerHeight * 0.46)) + "px"; }
  function closeCard() { if (card) card.style.display = "none"; currentEl = null; }

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
    else if (msg.type === "FILL_ALL" && IS_TOP) runFillAll();
  });
})();
