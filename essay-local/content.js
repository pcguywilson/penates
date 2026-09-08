// Essay Local AI - content script.
// Fills the focused essay field with a grounded answer from serve.py (/answer).
// Triggers: context menu "Answer with local AI", Alt+A, or the focus chip.
(() => {
  const ENDPOINT = "http://127.0.0.1:8765/answer";
  let lastEditable = null;

  const isEditable = (el) =>
    !!el && (el.tagName === "TEXTAREA" ||
      (el.tagName === "INPUT" && /^(text|search|email|url|tel|)$/i.test(el.type || "")) ||
      el.isContentEditable);

  document.addEventListener("focusin", (e) => {
    if (isEditable(e.target)) { lastEditable = e.target; showChip(e.target); }
  }, true);
  document.addEventListener("focusout", () => setTimeout(hideChip, 200), true);

  // ---- native value setter (plain .value = is ignored by React/Vue) ----
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

  // ---- question text: stacked probes, first non-empty wins ----
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  function questionFor(el) {
    // 0. user highlighted the question before triggering
    const sel = clean(window.getSelection && String(window.getSelection()));
    if (sel.length >= 12 && sel.length <= 500) return sel;
    const tries = [];
    // 1. label[for=id]
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) tries.push(l.innerText);
    }
    // 2. wrapping <label>
    const wrap = el.closest("label"); if (wrap) tries.push(wrap.innerText);
    // 3. aria-labelledby / aria-label
    const lb = el.getAttribute("aria-labelledby");
    if (lb) tries.push(lb.split(/\s+/).map((i) => (document.getElementById(i) || {}).innerText).join(" "));
    tries.push(el.getAttribute("aria-label"));
    // 4. fieldset > legend
    const fs = el.closest("fieldset"); if (fs) { const lg = fs.querySelector("legend"); if (lg) tries.push(lg.innerText); }
    // 5. common ATS label containers (Greenhouse #question_*, Workday data-automation-id)
    const cont = el.closest('[class*="question" i], .application-question, [data-automation-id]');
    if (cont) {
      const h = cont.querySelector('label, [class*="label" i], legend, h1,h2,h3,h4');
      if (h && !h.contains(el)) tries.push(h.innerText);
    }
    // 6. previous element/heading with sensible length
    let p = el.previousElementSibling, hops = 0;
    while (p && hops < 4) {
      const t = clean(p.innerText);
      if (t.length >= 12 && t.length <= 400) { tries.push(t); break; }
      p = p.previousElementSibling; hops++;
    }
    for (const t of tries) { const c = clean(t); if (c.length >= 8) return c.slice(0, 500); }
    return "";
  }

  function companyGuess() {
    const og = document.querySelector('meta[property="og:site_name"], meta[property="og:title"]');
    const t = clean((og && og.content) || document.title);
    return t.split(/[|\-–—·:]/)[0].trim().slice(0, 80);
  }

  // ---- run ----
  async function run(el) {
    el = el || lastEditable || (isEditable(document.activeElement) ? document.activeElement : null);
    if (!isEditable(el)) return toast("No essay field focused", true);
    const question = questionFor(el);
    if (!question) return toast("Could not read the question. Highlight it, then Alt+A.", true);
    toast("Local AI thinking...");
    const limit = el.maxLength && el.maxLength > 0 ? el.maxLength : null;
    const payload = { question, company: companyGuess(), url: location.href, limit };
    let resp;
    try {
      resp = await chrome.runtime.sendMessage({ type: "FETCH_ANSWER", payload });
    } catch (err) {
      return toast("extension error: " + err, true);
    }
    if (!resp || !resp.ok) return toast("serve.py not reachable on :8765 (is it running?)", true);
    const data = resp.data || {};
    if (!data.text) return toast("No answer: " + (data.method || "empty"), true);
    setNative(el, data.text);
    const gaps = (data.gaps && data.gaps.length) ? " [review: gap]" : "";
    toast((data.method || "done") + " / " + (data.chars || data.text.length) + " chars" + gaps, !!gaps);
  }

  chrome.runtime.onMessage.addListener((msg) => { if (msg && msg.type === "ANSWER_FIELD") run(); });

  // ---- focus chip (single floating control, not per-field buttons) ----
  let chip;
  function showChip(el) {
    if (!chip) {
      chip = document.createElement("button");
      chip.textContent = "AI ✨";
      chip.type = "button";
      chip.style.cssText =
        "position:absolute;z-index:2147483647;font:12px/1 sans-serif;padding:4px 8px;" +
        "border:1px solid #4b5563;border-radius:6px;background:#111827;color:#fff;" +
        "cursor:pointer;opacity:.9;box-shadow:0 1px 4px rgba(0,0,0,.3)";
      chip.addEventListener("mousedown", (e) => { e.preventDefault(); });
      chip.addEventListener("click", (e) => { e.preventDefault(); run(lastEditable); });
      document.body.appendChild(chip);
    }
    const r = el.getBoundingClientRect();
    chip.style.top = (window.scrollY + r.top - 26) + "px";
    chip.style.left = (window.scrollX + r.right - 44) + "px";
    chip.style.display = "block";
  }
  function hideChip() { if (chip && document.activeElement !== chip) chip.style.display = "none"; }

  // ---- toast ----
  let tEl, tTimer;
  function toast(msg, warn) {
    if (!tEl) {
      tEl = document.createElement("div");
      tEl.style.cssText =
        "position:fixed;bottom:16px;right:16px;z-index:2147483647;max-width:340px;" +
        "font:13px/1.4 sans-serif;padding:8px 12px;border-radius:6px;color:#fff;" +
        "box-shadow:0 2px 8px rgba(0,0,0,.35)";
      document.body.appendChild(tEl);
    }
    tEl.textContent = msg;
    tEl.style.background = warn ? "#b45309" : "#1f2937";
    tEl.style.display = "block";
    clearTimeout(tTimer);
    tTimer = setTimeout(() => (tEl.style.display = "none"), 4000);
  }
})();
