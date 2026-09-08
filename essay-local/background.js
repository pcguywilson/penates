// Context menu (right-click in an editable) + Alt+A command tell the content
// script to answer the focused field. The content script sends the built
// payload back here to FETCH it: a background fetch uses host_permissions and
// is NOT subject to the page's CORS / mixed-content / Private-Network blocks.
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "answer-field",
    title: "Answer with local AI",
    contexts: ["editable"]
  });
});

function ping(tabId) {
  if (tabId == null) return;
  chrome.tabs.sendMessage(tabId, { type: "ANSWER_FIELD" }, () => void chrome.runtime.lastError);
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "answer-field") ping(tab && tab.id);
});

chrome.commands.onCommand.addListener((cmd) => {
  if (cmd !== "answer-field") return;
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => ping(tabs[0] && tabs[0].id));
});

// Content script asks us to hit serve.py.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "FETCH_ANSWER") return;
  fetch("http://127.0.0.1:8765/answer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(msg.payload)
  })
    .then((r) => r.json())
    .then((data) => sendResponse({ ok: true, data }))
    .catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true; // async response
});
