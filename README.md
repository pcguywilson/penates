# Project Penates

**Local answers. Locked facts. You submit.**

A local helper for job applications. It fills what it can from a profile you own, and it
drafts the written questions the paid extensions meter (why this company, tell us about a
time, screening essays) with a model running on your own machine. It never submits the
form.

No account. No cloud LLM required. Nothing leaves the machine by default.

> The everyday, supported path is the right-click answer helper, which works in a normal
> browser next to a commercial autofiller. Full-page structured autofill (Playwright plus
> a debug Chrome) exists but is **work in progress**, so treat it as a sharp edge.

## Why it exists

Applying today means filling the same form far more times than it used to, just to reach
a human. Browser autofill already handles name, email, and phone. The unique written
questions are the part the paid tools either skip or answer by shipping your history to a
cloud API and charging you for it.

Project Penates is the other half: your facts live in YAML on disk, the model runs
locally through Ollama, and it is not allowed to claim a credential you didn't put in the
profile. A fluent lie on your application is worse than a blank box.

## Why "Penates"

The Penates were Rome's household gods: guardians of the inner home, kept at your own
hearth and carried with the family when it moved, not a temple you rent. Fitting for a
tool whose whole point is that your data stays home and belongs to you.

## What works today

- Right-click (or a page action) on a written-answer field gives you a draft from the local answer engine.
- Deterministic identity fields (name, email, phone, city, yes/no) from a locked profile.
- Answers grounded on your real work history: employer, dates, stack.
- **Gap guard:** a tool or cert you didn't list stays unclaimed; the field is answered honestly and flagged for review.
- It stops before Submit.

Full ATS walks (Greenhouse, Lever, Ashby, Workable, Paylocity, Workday, iCIMS, Salesforce
Lightning, and their comboboxes) are **in progress**. Rely on the answer path; treat the
full autofill as experimental.

## What it is not

- Not an auto-apply bot.
- Not a résumé that grows extra degrees.
- Not a substitute for reading the posting.

## How it's split

| Layer | Job |
|---|---|
| `profile.yaml` + `data/work_history.yaml` | Source of truth. Not the model. |
| `fields.yaml` | Deterministic rules: yes/no, work authorization, city, salary, EEO you chose. |
| `answers.yaml` + `apply.py` | Classify the question, write a short answer from your facts plus the job blurb. |
| Browser | A local server plus an MV3 right-click menu (normal Chrome); a CDP runner for the WIP full-fill. |
| You | Captchas, MFA, account creation, the final read, and Submit. |

Every answer resolves to one of three states: **AUTO** (deterministic, filled),
**REVIEW** (generated but touches a limited-experience area; filled and flagged), or
**PAUSE** (not enough grounded evidence; left for you). Two guards run first: the **gap
guard** (never claim a listed gap) and a **cross-contamination guard** (a "why this
company" answer must name the company on *this* page, never one pulled from another posting).

## Honesty

The model may rephrase work you actually did and use a company blurb you supplied. It may
not add degrees, years, clearances, or tools that aren't in your profile. If you don't
list Security+, it won't claim Security+. A missing fact becomes a review flag, never a
confident lie. You review every answer, and the tool never submits unless you explicitly
turn it on.

## Requirements

- Windows 10/11 (primary); macOS and Linux work for the answer path.
- Python 3.11 or 3.12, Google Chrome, [Ollama](https://ollama.com), Git.

```bash
ollama pull llama3.2:3b          # classify + short answers
ollama pull qwen2.5:7b-instruct  # grounded essays
```

Everything stays on localhost: Ollama `11434`, the tool's server `8765`, and (only for
the WIP runner) Chrome's debug port `9222`. No paid API, Docker, or database.

## Quickstart: the answer helper (supported)

1. Start Ollama and pull the two models above.
2. `pip install -r requirements.txt`
3. Copy the example configs and fill in your own facts (the real ones are git-ignored):
   ```
   cp profile.example.yaml profile.yaml
   cp data/work_history.example.yaml data/work_history.yaml
   cp application.example.yaml application.yaml
   ```
4. Start the local server: **Start Autofill Server.bat**, or `python serve.py`
   (serves `/answer` on `127.0.0.1:8765`).
5. In `chrome://extensions` (Developer mode, then Load unpacked), load the `essay-local/`
   folder. It works in your normal Chrome.
6. Focus a written-answer box, then right-click **Answer with local AI**, or press
   **Alt+A**. The draft is inserted; a toast shows the method and any review flag.

You can run this next to a commercial autofiller: let it do the structured fields, turn
off its AI on the unique questions, and let Penates handle those.

## Quickstart: WIP full-page autofill (lab)

The Playwright runner drives a Chrome started with `--remote-debugging-port=9222`
(**Launch Apply Chrome.bat**), attaches over CDP, fills a whole application, and stops
before Submit. Per-ATS handling for the sites listed above. Experimental.

```
python run_url.py --attach "<application URL>"
```

Submit stays blocked unless you set `APPLY_ALLOW_SUBMIT=1`. `APPLY_ICIMS_ADVANCE=1` lets
the iCIMS runner page through, still stopping before the last step.

## Configuration

| File | What it is | In repo |
|---|---|---|
| `profile.yaml` | Your locked facts (source of truth) | `.example` only; real is git-ignored |
| `data/work_history.yaml` | Dated employment history | `.example` only; real is git-ignored |
| `data/jobs.json` | Your application queue | `.example` only; real is git-ignored |
| `application.yaml` | The current job's company blurb | `.example` only; real is git-ignored |
| `fields.yaml` | Field-label to profile mapping rules | shipped |
| `answers.yaml` | Intent templates for essays | shipped |
| `secrets.yaml` | Workday login (WIP runner only) | `.example` only; real is git-ignored |

## Related projects

| Project | Local LLM | Autofill | Essays | You submit | Note |
|---|---|---|---|---|---|
| Offlyn Apply | Ollama | wide ATS | rewrite menus | yes | Closest UX; strong context-menu verbs |
| AgentMan | Ollama | page agent | via agent | yes | Multi-step page walking |
| ApplyEase | Ollama / LM Studio | common fields | custom answers | yes | React + FastAPI + Postgres/pgvector |
| AI-Job-Applier | Ollama | one-click | context files | yes | Learn-as-you-go into cache |
| Persona | Ollama | none | resume builder | n/a | Same manifesto, different artifact |
| Simplify Copilot | cloud | strong | paid/cloud | yes | Great for structure; not local |

Penates' distinct position: local, plus a hard anti-fabrication contract, plus a
right-click control that works in a normal browser.

## Roadmap

- **Now (answer path):** better question-label detection (aria, headings, selected text), native value setter with `InputEvent` for React/LWC fields, per-intent length caps, a cache-hit indicator.
- **Next (structured fill without lying):** identity fields for Greenhouse/Rippling/Ashby directly in the extension (no CDP), strict Yes/No polarity, never pick "Decline to self-identify" when a real answer exists, Workday account and experience from `work_history.yaml`, Salesforce Flow shadow-DOM handling.
- **Later:** optional cloud OpenAI-compatible endpoint behind the same compose contract, a PII-free application log, cover letters from the same templates.
- **Not planned:** auto-submit as default, mass-apply, hosted SaaS, or inventing skills to raise a match score.

## Privacy

No telemetry; nothing leaves your machine by default. Never commit `profile.yaml`,
`secrets.yaml`, résumés, or the contents of `data/`; they're git-ignored. The public tree
ships only fake example configs.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE): free to use, study, and modify for
noncommercial purposes; selling a hosted clone is not permitted.

## Disclaimer

This tool drafts and fills; **you** review and submit. You are responsible for the
accuracy of everything you send. It is built to keep your answers truthful, so keep them
that way.
