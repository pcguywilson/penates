# Changelog

Penates uses [Semantic Versioning](https://semver.org/). Before 1.0, a minor version can
change behavior. The extension version in `penates-extension/manifest.json` matches the
release.

## [0.7.1] - 2026-11-19

### Fixed

- `fields.yaml` no longer ships one person's answers. Clearance, citizenship, work
  authorization, sponsorship, relocation, contract, on-call, education level, time zone,
  pronouns, "how did you hear" and the hands-on/AI screening answers now read from
  `profile.yaml`. A blank profile value leaves the question for you to answer.

### Changed

- `profile.example.yaml` adds the keys those rules read: `eligibility.work_authorized`,
  `us_citizen`, `needs_sponsorship`, `willing_to_relocate`, `has_clearance`,
  `open_to_contract`, `on_call`, `government_employee`, `located_in_nyc`;
  `education.highest_level`, `education.graduated`; `identity.timezone_label`,
  `identity.pronouns`; `screening.built_automation`, `monitoring_experience`,
  `code_review_ok`, `hands_on_infra`, `ai_use`. Copy them into your `profile.yaml`.

## [0.7.0] - 2026-11-19

First tagged release. Covers everything since the 2026-11-12 push.

### Added

- **Cloud and CLI models, per tier.** Answers and essays each pick a provider: Ollama
  (default), Claude Code CLI, Grok CLI, Anthropic API, or xAI API (`llm.py`, `/api/llm`,
  `/api/llm/test`). Keys come from `secrets.yaml` or `ANTHROPIC_API_KEY` / `XAI_API_KEY` and
  are never returned or logged. Optional local fallback when a cloud call fails.
- **Models page** in the gear menu with the Ollama card and the Cloud models card.
- **Cloud essays** use your profile (no identity, salary or EEO fields), work history, the
  best-matching stories and the posting, under the same honesty rules as local answers.
- **Follow-up questions.** On the single answer card with a cloud essay model, the model can
  ask up to three questions instead of guessing. Answer them, click **Draft with my
  answers** (`/api/story/followup`), and **Save as story** to add the result to your bank.
- **Resume tailor** (Knowledge > Resume, `tailor.py`, `/api/tailor`). Reorders bullets from
  `resume.yaml`, leads skills with matches, shows covered and missing terms, exports HTML or
  Markdown. Never adds a bullet or a skill. `resume.example.yaml` included.
- **Practice applications** at `/demo`: Meridian Cloud, Northwind Grid, Lattice Labs, Harbor
  Federal Systems and BrightPath Health Alliance. Fictional, nothing is submitted. Rebuild
  with `python demos/build_demos.py .`
- **QA bench** at `/qa`: ask any question and see the answer, the tier and the timing.
- Word limits in questions ("150 words or less", "100-word limit", "in 50 words") are read and
  enforced.

### Changed

- **Settings** keeps sources, search terms, retention and test pages. Model settings moved to
  **Models**.
- **Logs** (answers, fills, last refresh) are written to `logs/` and reloaded at startup.
- Fill-all sends long text boxes as essay questions so they take the essay path.
- The extension's Always-show list now beats the built-in deny list and accepts `host:port`.
- Extension and release share one version: `0.7.0`.

### Fixed

- A salary-range question ("The range is $98,000 to $112,000. Is that OK?") is answered Yes or
  No against your expected pay. It no longer types your number into a Yes/No field.
- Closed screening Yes/No questions get Yes, No on a stated gap, or blank for review. They no
  longer get an essay.
- AI-disclosure questions ("If you are an AI, write only ...") always pause for you, even in a
  text box.
- Rules marked `force: true` in `fields.yaml` win over the skip for open-ended questions.
