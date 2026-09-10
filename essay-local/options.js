// Penates Apply - settings page. Persists to chrome.storage.local under "penatesSettings".
// Shape: { mode: "forms"|"ats"|"never", alwaysHosts: string[], neverHosts: string[] }
const DEFAULTS = { mode: "forms", alwaysHosts: [], neverHosts: [] };

function parseHosts(text) {
  return (text || "")
    .split(/[\s,]+/)
    .map((s) => s.trim().toLowerCase()
      .replace(/^https?:\/\//, "")
      .replace(/\/.*$/, "")
      .replace(/^\*\./, ""))
    .filter((s) => s && s.includes("."));
}

function load() {
  chrome.storage.local.get("penatesSettings", (o) => {
    const s = Object.assign({}, DEFAULTS, (o && o.penatesSettings) || {});
    const r = document.querySelector('input[name="mode"][value="' + s.mode + '"]');
    if (r) r.checked = true;
    document.getElementById("always").value = (s.alwaysHosts || []).join("\n");
    document.getElementById("never").value = (s.neverHosts || []).join("\n");
  });
}

function save() {
  const mode = (document.querySelector('input[name="mode"]:checked') || {}).value || "forms";
  const settings = {
    mode,
    alwaysHosts: parseHosts(document.getElementById("always").value),
    neverHosts: parseHosts(document.getElementById("never").value),
  };
  chrome.storage.local.set({ penatesSettings: settings }, () => {
    document.getElementById("always").value = settings.alwaysHosts.join("\n");
    document.getElementById("never").value = settings.neverHosts.join("\n");
    const el = document.getElementById("saved");
    el.textContent = "Saved. Reload the application tab to apply.";
    setTimeout(() => (el.textContent = ""), 4000);
  });
}

document.getElementById("save").addEventListener("click", save);
document.getElementById("addthis").addEventListener("click", () => {
  navigator.clipboard.writeText("careers.example.com").catch(() => {});
  const el = document.getElementById("saved");
  el.textContent = "Copied 'careers.example.com' - paste and edit.";
  setTimeout(() => (el.textContent = ""), 4000);
});
load();
