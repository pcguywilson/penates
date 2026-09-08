# Launch-from-Chrome (one-click autofill)

Fill the job application on whatever page you're viewing in Chrome — no jobs
index, no URL pasting. One button.

## How it works
- **Launch Apply Chrome.bat** starts a dedicated Chrome (debug port 9222, its own
  profile in `chrome-profile\`). Sign into ATS sites here once; logins persist.
- **Apply Autofill** extension = a toolbar button. Click it on any job page.
- **Start Autofill Server.bat** runs a tiny local server (127.0.0.1:8765) that the
  button pings; it launches `run_url.py --attach "<that page's URL>"`.
- `--attach` connects to the Chrome you're already in and fills the current tab
  (your logins carry over). It never closes your browser — review and Submit.

## One-time setup
1. Double-click **Launch Apply Chrome.bat**. A fresh Chrome opens.
2. In that Chrome go to `chrome://extensions`, turn on **Developer mode** (top right),
   click **Load unpacked**, and pick the `extension` folder in this directory.
   Pin the "Apply Autofill" button to the toolbar.
3. Sign into the ATS sites you use (Workday tenants, iCIMS, etc.) in this Chrome.
   These stay logged in for next time.

## Daily use
1. Double-click **Start Autofill Server.bat** (leave the window open).
2. Double-click **Launch Apply Chrome.bat** (or reuse the one already open).
3. Browse to a job application, then click the **Apply Autofill** button.
   A console pops up showing progress; the form fills in the tab you're on.
   The button flashes `ok` (launched), `off` (server not running), or `x`
   (not a web page).

## Notes
- The button badge `off` means Start Autofill Server.bat isn't running.
- Ollama should be running for the judgment fields (same as before).
- Everything still works the old way too: `python run_url.py "<url>"` (own window),
  or `--only=<company>` from the queue.
