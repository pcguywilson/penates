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

  function companyGuess() {
    const og = document.querySelector('meta[property="og:site_name"], meta[property="og:title"]');
    const t = clean((og && og.content) || document.title);
    return t.split(/[|\-–—·:]/)[0].trim().slice(0, 80);
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
  async function askEngine(question, limit, fresh) {
    const payload = { question, company: companyGuess(), url: location.href, limit: limit || null, fresh: !!fresh };
    try {
      const resp = await chrome.runtime.sendMessage({ type: "FETCH_ANSWER", payload });
      if (resp && resp.ok) return resp.data || null;
      return { __error: (resp && resp.error) || "serve.py unreachable on :8765" };
    } catch (err) {
      return { __error: String(err) };
    }
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
    const want = norm(value);
    const ctrl = (el.closest && el.closest('[class*="control"]')) || el.parentElement;
    // react-select v5 opens on mousedown (not click); open, then read rendered options
    for (const t of ["pointerdown", "mousedown", "mouseup"]) {
      try { (ctrl || el).dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })); } catch (_) {}
    }
    el.focus();
    await sleep(300);
    let hit = matchOption(want);
    if (!hit) {
      // long lists (e.g. Country): type to filter, then re-scan
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      try { setter.call(el, value); el.dispatchEvent(new InputEvent("input", { bubbles: true, data: value, inputType: "insertText" })); } catch (_) {}
      await sleep(550);
      hit = matchOption(want);
    }
    if (hit) { fireMouse(hit); await sleep(240); if (comboCommitted(el)) return true; }
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await sleep(160);
    return comboCommitted(el);
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

    for (const el of fields) {
      const tag = el.tagName, typ = (el.type || "").toLowerCase();

      // ---- radio / checkbox: resolve on the GROUP question, click matching option ----
      if (tag === "INPUT" && (typ === "radio" || typ === "checkbox")) {
        const q = questionFor(el);
        const gkey = (el.name || q || "").trim();
        if (!q) { continue; } // no question resolved: skip silently (avoid random toggles)
        if (gkey && groupsDone.has(gkey)) continue;

        // required consent / single acknowledgement checkbox -> tick it
        const optlab = optionLabel(el);
        if (typ === "checkbox" && CONSENT_RE.test(q + " " + optlab) && !/which|select all|open to|settings/i.test(q)) {
          const ok = commitOption(el);
          rows.push([q.slice(0, 60), ok ? "filled" : "skip", ok ? "checked" : "couldn't check"]);
          ok ? filled++ : skipped++;
          if (gkey) groupsDone.add(gkey);
          continue;
        }

        const data = await askEngine(q, 25);
        if (!data || data.__error) { rows.push([q.slice(0, 60), "skip", data && data.__error ? "engine: " + data.__error : "no answer"]); skipped++; continue; }
        const ans = (data.text || "").trim();
        if (!ans) { rows.push([q.slice(0, 60), "skip", "no grounded answer -> you"]); skipped++; if (gkey) groupsDone.add(gkey); continue; }
        // collect this group's option inputs (same name, or same question via shadow)
        const groupEls = fields.filter((o) => o.tagName === "INPUT" && (o.type || "").toLowerCase() === typ &&
          ((el.name && o.name === el.name) || (!el.name && questionFor(o) === q)));
        const na = norm(ans);
        let clicked = null;
        for (const o of (groupEls.length ? groupEls : [el])) {
          const ol = norm(optionLabel(o));
          if (ol === na || (na && ol.startsWith(na)) || (na.length >= 3 && ol.includes(na)) || (ol.length >= 3 && na.includes(ol))) {
            if (commitOption(o)) { clicked = optionLabel(o); break; }
          }
        }
        if (gkey) groupsDone.add(gkey);
        if (clicked) { rows.push([q.slice(0, 60), "filled", clicked]); filled++; }
        else { rows.push([q.slice(0, 60), "skip", "answer '" + ans + "' not among options"]); skipped++; }
        continue;
      }

      // ---- select ----
      if (tag === "SELECT") {
        const q = questionFor(el);
        if (!q) { continue; }
        if (el.value && el.selectedIndex > 0 && clean(el.options[el.selectedIndex].text)) { continue; } // already set
        const data = await askEngine(q, 40);
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
        const cur = (el.value || el.textContent || "").trim();
        if (cur) { continue; }                        // don't clobber prefilled
        const limit = el.maxLength && el.maxLength > 0 ? el.maxLength : null;
        const data = await askEngine(q, limit);
        if (!data || data.__error || !data.text) { rows.push([q.slice(0, 60), "skip", data && data.__error ? "engine: " + data.__error : "no answer -> you"]); skipped++; continue; }
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

    fillBusy = false;
    const summary = "Filled " + filled + " . " + review + " to review . " + skipped + " left for you . NOTHING submitted.";
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
      "position:fixed;right:16px;top:16px;z-index:2147483647;width:430px;max-width:94vw;max-height:80vh;overflow:auto;" +
      "background:#0f172a;color:#e5e7eb;border:1px solid #334155;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.45);" +
      "font:12px/1.45 system-ui,sans-serif";
    report.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;padding:9px 12px;background:#111827;border-bottom:1px solid #334155;position:sticky;top:0">' +
        '<span style="font-weight:600">Penates . Fill report</span>' +
        '<span data-r="sum" style="flex:1;color:#94a3b8"></span>' +
        '<button data-r="applied" title="Log this as an application after you submit" style="background:#166534;color:#fff;border:1px solid #14532d;border-radius:6px;padding:4px 8px;cursor:pointer;font:11px system-ui,sans-serif">Mark applied</button>' +
        '<span data-r="x" title="Close" style="cursor:pointer;color:#94a3b8;padding:0 4px">✕</span>' +
      '</div><div data-r="body" style="padding:8px 10px"></div>';
    document.documentElement.appendChild(report);
    report.querySelector('[data-r="x"]').onclick = () => (report.style.display = "none");
    report.querySelector('[data-r="applied"]').onclick = markApplied;
  }
  function setReport(summary, rows) {
    if (!report) return;
    report.querySelector('[data-r="sum"]').textContent = summary || "";
    const color = { filled: "#34d399", review: "#fbbf24", skip: "#94a3b8" };
    report.querySelector('[data-r="body"]').innerHTML = (rows || []).map((r) =>
      '<div style="display:flex;gap:8px;padding:4px 2px;border-bottom:1px solid #1e293b">' +
        '<span style="width:64px;flex:none;color:' + (color[r[1]] || "#e5e7eb") + '">' + r[1] + '</span>' +
        '<span style="width:150px;flex:none;color:#cbd5e1">' + escapeHtml(r[0]) + '</span>' +
        '<span style="flex:1;color:#94a3b8">' + escapeHtml(r[2] || "") + '</span>' +
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
