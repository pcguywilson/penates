const SERVER = "http://127.0.0.1:8765/apply";

chrome.action.onClicked.addListener(async (tab) => {
  const url = tab && tab.url;
  if (!url || !/^https?:/i.test(url)) { badge("x", "#cc0000"); return; }
  try {
    const r = await fetch(SERVER + "?url=" + encodeURIComponent(url), { method: "POST" });
    badge(r.ok ? "ok" : "!", r.ok ? "#0a7d00" : "#cc6600");
  } catch (e) {
    badge("off", "#cc0000");   // server not running
  }
});

function badge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 2500);
}
