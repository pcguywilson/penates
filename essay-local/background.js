// Penates background: context menus + keyboard commands -> tell the content script to
// open the per-field card (ANSWER_FIELD) or run the whole-page fill (FILL_ALL). Also
// proxies the content script's fetches to serve.py (host_permissions + a background
// fetch dodge the page's CORS / mixed-content / private-network blocks).
const BASE = "http://127.0.0.1:8765";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "answer-field", title: "Answer with local AI (Penates)", contexts: ["editable"] });
    chrome.contextMenus.create({ id: "fill-application", title: "Fill this application (Penates)", contexts: ["page", "selection", "editable"] });
  });
});

async function send(tabId, type) {
  if (tabId == null) return;
  // Ensure the content script is present (on ATS hosts it already is; the guard makes a
  // re-inject a no-op). activeTab grants this tab on the user's gesture - no all-sites perm.
  try {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["content.js"] });
  } catch (e) { /* not injectable (chrome://, web store, pdf) - ignore */ }
  chrome.tabs.sendMessage(tabId, { type }, () => void chrome.runtime.lastError);
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const id = tab && tab.id;
  if (info.menuItemId === "answer-field") send(id, "ANSWER_FIELD");
  else if (info.menuItemId === "fill-application") send(id, "FILL_ALL");
});

chrome.commands.onCommand.addListener((cmd) => {
  const type = cmd === "fill-application" ? "FILL_ALL" : cmd === "answer-field" ? "ANSWER_FIELD" : null;
  if (!type) return;
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => send(tabs[0] && tabs[0].id, type));
});

// Content script -> serve.py: /answer for a value/draft, /learn to remember an edit, and
// /resume (GET) for the resume bytes so the content script can attach the file.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  // Resume is a GET that returns {ok, filename, mime, b64}; the content script builds a File.
  if (msg.type === "FETCH_RESUME") {
    fetch(BASE + "/resume", { method: "GET" })
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, data }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true; // async
  }
  let path = null;
  if (msg.type === "FETCH_ANSWER") path = "/answer";
  else if (msg.type === "FETCH_LEARN") path = "/learn";
  else if (msg.type === "FETCH_APPLIED") path = "/applied";
  else if (msg.type === "FETCH_FILLLOG") path = "/fill-log";
  else return;
  fetch(BASE + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(msg.payload) })
    .then((r) => r.json())
    .then((data) => sendResponse({ ok: true, data }))
    .catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true; // async
});
