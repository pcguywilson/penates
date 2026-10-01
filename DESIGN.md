# Penates DESIGN.md

Status: accepted 2026-09-30; dashboard migrated (shell, Jobs, Search, Knowledge, gear menu). Extension overlay restyle is phase 2.

Penates is a local tool: it runs on `127.0.0.1:8765`, answers with a local model, and keeps facts in YAML on disk. The UI should read like a well-made admin for a tool you run yourself. It should not look like a SaaS marketing page.

---

## 0. Inventory (current state)

| Surface | File | Notes |
|---|---|---|
| Shell + all tabs | `dashboard.html` (1 file, inline CSS/JS) | Tabs: Jobs, Search, Logs, Settings, Profile, Stories, Resume. Theme switcher (6 themes) in header. |
| Job drawer | `dashboard.html` `.drawer` | Apply / Open / Mark applied / Hide employer. |
| Stories (legacy page) | `stories.html` | Superseded by the Stories tab; keep as redirect or delete later. |
| Test pages | `demo.html`, `qa.html` | Linked from Settings. Keep, restyle last. |
| Fill overlay | `penates-extension/content.js` report panel + review card | Inline styles (`btnCss`, color map filled/review/skip). Phase 2. |
| Theme tokens | `:root[data-theme=...]` in `dashboard.html` | `--bg --panel --panel2 --line --text --muted --accent --accent2 --onaccent --hi --mid --lo`. |
| Search persistence | `config.json` `google_search` via `POST /config` | Sites, roles, include, exclude, clearance, days, variants. |

Workflow the UI must serve: **discover** (Refresh, Search, paste links) → **triage** (Jobs list, drawer) → **apply** (open posting, extension fills) → **review** (fill report, review card) → **mark applied**.

Dead or misplaced today: Logs and Settings sit as peer tabs next to daily work; Stories/Resume/Profile are three tabs for one idea (what Penates knows about you).

---

## 1. Visual theme and atmosphere

Quiet, dense, dark-first. Flat surfaces separated by 1px lines, not shadows. One restrained accent. Text does the work; color is reserved for status.

References are used for **principles only**: Linear (density, calm hierarchy, keyboard-first), Raycast (tight rows, compact controls), Resend (mono captions). Do not borrow purple accents, neon gradients, or black-marketing hero layouts.

## 2. Color palette and roles

Default theme **Stone** (dark). Values are the proposal; the preview renders them.

| Token | Value | Role |
|---|---|---|
| `--bg` | `#0f1317` | page |
| `--surface-1` | `#151a20` | chrome, table header, cards |
| `--surface-2` | `#1b2129` | hover row, inputs, chips |
| `--surface-3` | `#242b35` | pressed, selected, drawer header |
| `--line` | `#262e38` | dividers |
| `--line-strong` | `#34404d` | input borders, focus-adjacent |
| `--text` | `#e4e7eb` | primary text |
| `--text-2` | `#a3acb8` | secondary text, labels |
| `--text-3` | `#6b7684` | captions, placeholders, disabled |
| `--accent` | `#5aa89f` | primary action, focus ring, selected nav underline |
| `--accent-strong` | `#72bdb3` | accent hover |
| `--on-accent` | `#0a1715` | text on accent |

Accent is a muted teal-stone. It is used for one primary button per view, focus, and the active tab. It is never used for decoration.

## 3. Typography

- UI: `system-ui, -apple-system, "Segoe UI", sans-serif`. No web font downloads (local tool).
- Mono: `ui-monospace, "Cascadia Mono", Consolas, monospace` for queries, paths, logs, scores.
- Scale (px): 11 caption/mono meta, 12 table secondary, 13 body/table, 15 section title, 18 drawer title. Weights 400 and 600 only.
- Numbers in tables are tabular (`font-variant-numeric: tabular-nums`). The Georgia serif on scores and stats goes away.
- Headings are sentence case. No uppercase letter-spaced card titles except table column headers (11px, `--text-3`).

## 4. Components

- **Button**: 28px high, 12px horizontal padding, 6px radius, 13px/600. Variants: `primary` (accent fill), `secondary` (surface-2 + line-strong border), `quiet` (text only, surface-2 on hover), `danger` (red text, red fill only in a confirm step).
- **Icon button**: 24px square, quiet, row actions only. Hidden until row hover or keyboard focus.
- **Input / textarea / select**: surface-2 fill, line-strong border, 6px radius, 28px high (inputs), accent focus ring 2px.
- **Chip input**: editable list (roles, excludes). Chip = surface-2, 22px, remove ×. Enter adds.
- **Toggle**: 28×16 switch for booleans (Remote, US only, packs). Checkboxes only inside lists.
- **Segmented control**: 2 to 4 mutually exclusive options (look-back window, job status segment).
- **Status dot + label**: 6px dot + 12px text. See section 11.
- **Table row**: 36px, 13px, hover surface-2, click opens drawer. Row actions live at the right edge.
- **Drawer**: right side, 560px, surface-1, header surface-3, sticky footer with the view's one primary action.
- **Query row** (Search diagnostics): label, truncated mono query, Copy, Open. One line each.

## 5. Layout / shell

```
[mark] Penates   Jobs   Search   Knowledge            ● LOCAL  qwen2.5:7b  ·  profile on disk   [⚙]
```

- Chrome 44px, surface-1, bottom line. Mark 20px (existing `penates.svg` geometry, `currentColor`), wordmark 15px/600 in `--text`. The mark is not recolored with the accent.
- Local chip: dot `--status-local`; turns `--status-error` and reads `ENGINE DOWN` when `/version` or Ollama fails. Never hidden.
- Gear menu: Settings, Logs, Theme, Test pages.
- Content max width none (tables use the full width); side padding 20px. Desktop first, 1280px target.

## 6. Elevation

Flat. Only two elevated layers: drawer (shadow `0 0 0 1px var(--line), -12px 0 32px rgba(0,0,0,.35)`) and menus/popovers (`0 8px 24px rgba(0,0,0,.4)`). No card shadows.

## 7. Do / don't

Do: dense rows, one primary button per view, status shown with dot + word, mono for anything machine-generated, keyboard focus visible.

Don't: gradients, glass, hero blocks, stat cards with big serif numbers, rainbow badges, "strong fit" green on answers, decorative motion, per-row Hide buttons in the name column, raw 400-character Boolean strings as body text.

## 8. Responsive

Desktop-first. Below 1100px the Location and Salary columns collapse into the role cell's secondary line. Below 820px the drawer goes full width. No phone layout.

## 9. Agent prompt guide (restyling a new page)

1. Use only the tokens in section 2 and the components in section 4.
2. One primary action per view. Everything else secondary or quiet.
3. Status uses the section 11 vocabulary and colors, nothing new.
4. Machine output (queries, logs, paths, JSON) in mono, truncated with Copy.
5. No new colors, no shadows beyond section 6, no motion beyond 120ms opacity/transform.

## 10. Information architecture

| Top level | Contains | Why |
|---|---|---|
| **Jobs** | queue with segments: New · Review · Applied · All; filters Remote, US only, Min $, Closed | Single list in `jobs.json`; segments are filters, not pages. |
| **Search** | intent form, query packs, LinkedIn links, paste links | How jobs enter when there is no ATS API. Stays first-class. |
| **Knowledge** | Profile · Work history · Stories · Resume (sub-tabs) | Everything Penates knows about you, one place. |
| gear | Settings (models, sources, retention, search terms), Logs, Theme, Test pages | Setup and diagnostics, not daily work. |

Employer grouping (shipped): one head row per employer, `+N more` as a quiet inline control after the name, Hide as a row-hover icon in the actions column and in the drawer. Never a button in the name column.

## 11. Status language

| Token | Color | Meaning |
|---|---|---|
| `new` | `#8d9aab` | unseen job |
| `match` | `#6fbf8e` | rank/fit above threshold |
| `review` | `#d6a24a` | a human must read it (essay, gap, unsure pick) |
| `applied` | `#7c8794` | owner marked applied |
| `skip` / `blocked` | `#6b7684` | no grounded answer, left for owner |
| `gap` | `#c98a5e` | honest missing skill |
| `error` | `#e0706c` | engine down, fill failed |
| `local` | `#5aa89f` | processing on this machine |
| `verified` | `#86add3` | fact from the posting or YAML |

Scores render as plain tabular numbers; color only via the `match` dot when above threshold. Unscored rows show an em-space, not 0.

## 12. Local-identity chrome

The chip row is the privacy story; there is no banner. It reports facts: engine up/down, model name, profile file on disk. Wording is lowercase except `LOCAL` / `ENGINE DOWN`.

## 13. Search: a query generator for any user

Search builds Google searches aimed at job sites Penates cannot scan. Nothing personal is shipped as a default.

- Inputs (all chips, all removable): **Roles**, **Include keywords**, **Exclude keywords**, **Sites** (host names). Look back is a segmented control (24h, 3d, 1w, 2w, 1m, custom).
- Optional modifiers, off by default: **Level words** (mode Any / Must match one / Exclude these, plus chips the user types; nothing prefilled, the placeholder only suggests senior, staff, principal, lead, junior; Any applies nothing even if chips remain) and **Clearance terms** (a toggle; the user types the terms). No level taxonomy.
- **Query preview** sits above the result rows and updates on every chip, look-back, site and level change, no Save needed. One mono block per site group (labeled with site names only when there is more than one group). Missing roles/keywords or sites replace the preview with a one-line hint; no stale query is shown.
- Output: one Google query per site group. Sites are grouped only when a single query would get too long (more than 6 sites or about 1500 characters). The row title is the human site names ("Greenhouse, Lever, Ashby"), the caption is a plain summary (roles, window, includes, excludes). The Boolean is never the row body: Open launches Google, Copy copies the query, "Query" expands the string.
- Empty states: no roles and no keywords, or no sites, show a one-line hint instead of a query.
- One "Also on LinkedIn" row with the same roles and look back. LinkedIn is not a second builder.
- "Add job links" is a separate card below the generator (Penates ingesting URLs, not query generation).
- Saved to `config.json` `google_search` = `{roles[], include[], exclude[], sites[], days, level:{mode, terms[]}, clearance:{on, terms[]}}`. Older saved shapes (comma site groups, include string, variants, seniority) are read and converted on load.

## 14. Extension overlay mapping (phase 2)

Overlay keeps its behavior. Styles move to the same tokens with a `pn-` prefix so a later pass is copy-paste CSS:

| Overlay element | Class | Token |
|---|---|---|
| panel | `pn-panel` | surface-1, line, 10px radius |
| summary line | `pn-summary` | `--status-match` when done, `--status-review` while working |
| row status | `pn-row[data-s=filled|review|skip|gap|error]` | section 11 colors |
| buttons (Mark applied, Regenerate, Insert) | `pn-btn`, `pn-btn--primary` | section 4 |
| timing row | `pn-meta` | mono 11px `--text-3` |

## 15. Token mapping from current themes

| Current | New |
|---|---|
| `--bg` | `--bg` |
| `--panel` | `--surface-1` |
| `--panel2` | `--surface-2` |
| `--line` | `--line` |
| `--text` | `--text` |
| `--muted` | `--text-2` |
| `--accent` | `--accent` |
| `--accent2` | `--accent-strong` |
| `--onaccent` | `--on-accent` |
| `--hi` / `--mid` / `--lo` | `--status-match` / `--status-review` / `--text-3` |

Theme switcher stays (moves to the gear menu). Stone becomes default; Slate, Nord, Light are kept and re-expressed in the new token names. Emerald, Amber and Dracula are candidates to drop (owner decides). Old names stay as aliases during migration so nothing breaks mid-way.

## 16. Source of truth: tokens and chrome as shipped

Copied from `dashboard.html` so a later pass restyles against this, not against memory. If you change one, change both. Do not add a third accent.

```css
  /* Penates design system (DESIGN.md). New token names + legacy aliases so older inline styles keep working. */
  :root,:root[data-theme="stone"]{color-scheme:dark;
    --bg:#0f1317;--surface-1:#151a20;--surface-2:#1b2129;--surface-3:#242b35;--line:#262e38;--line-strong:#34404d;
    --text:#e4e7eb;--text-2:#a3acb8;--text-3:#6b7684;--accent:#5aa89f;--accent-strong:#72bdb3;--on-accent:#0a1715;
    --s-new:#8d9aab;--s-match:#6fbf8e;--s-review:#d6a24a;--s-applied:#7c8794;--s-skip:#6b7684;--s-gap:#c98a5e;--s-error:#e0706c;--s-local:#5aa89f;--s-verified:#86add3;}
  :root[data-theme="slate"]{color-scheme:dark;
    --bg:#0f172a;--surface-1:#141d2e;--surface-2:#1c2740;--surface-3:#24314d;--line:#26324a;--line-strong:#334155;
    --text:#e5e7eb;--text-2:#a3b0c2;--text-3:#6b7a90;--accent:#38bdf8;--accent-strong:#7dd3fc;--on-accent:#04222e;
    --s-new:#94a3b8;--s-match:#34d399;--s-review:#fbbf24;--s-applied:#7c8aa0;--s-skip:#6b7a90;--s-gap:#f59e0b;--s-error:#f87171;--s-local:#38bdf8;--s-verified:#93c5fd;}
  :root[data-theme="nord"]{color-scheme:dark;
    --bg:#2e3440;--surface-1:#353c4a;--surface-2:#3b4252;--surface-3:#434c5e;--line:#434c5e;--line-strong:#4c566a;
    --text:#eceff4;--text-2:#c0c6d2;--text-3:#8b93a4;--accent:#88c0d0;--accent-strong:#8fbcbb;--on-accent:#1b2430;
    --s-new:#a6adba;--s-match:#a3be8c;--s-review:#ebcb8b;--s-applied:#8b93a4;--s-skip:#8b93a4;--s-gap:#d08770;--s-error:#bf616a;--s-local:#88c0d0;--s-verified:#81a1c1;}
  :root[data-theme="light"]{color-scheme:light;
    --bg:#f5f5f3;--surface-1:#ffffff;--surface-2:#f0f0ec;--surface-3:#e7e7e2;--line:#e2e1dc;--line-strong:#cfcdc6;
    --text:#1c2128;--text-2:#525c68;--text-3:#8a929c;--accent:#2f7f76;--accent-strong:#276b63;--on-accent:#ffffff;
    --s-new:#6b7684;--s-match:#2f8a57;--s-review:#a86f12;--s-applied:#8a929c;--s-skip:#8a929c;--s-gap:#a35f2e;--s-error:#c0392b;--s-local:#2f7f76;--s-verified:#3d6f9e;}
  :root{--panel:var(--surface-1);--panel2:var(--surface-2);--muted:var(--text-2);--accent2:var(--accent-strong);--onaccent:var(--on-accent);
    --hi:var(--s-match);--mid:var(--s-review);--lo:var(--text-3);
    --ui:system-ui,-apple-system,"Segoe UI",sans-serif;--mono:ui-monospace,"Cascadia Mono",Consolas,monospace}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:13px/1.5 var(--ui)}
  button,input,select,textarea{font:inherit;color:inherit}
  :focus-visible{outline:2px solid var(--accent);outline-offset:1px}
  a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
  code,.mono{font-family:var(--mono);font-size:12px}
  /* chrome */
  header.chrome{height:44px;display:flex;align-items:center;gap:18px;padding:0 20px;border-bottom:1px solid var(--line);background:var(--surface-1);position:sticky;top:0;z-index:20}
  .brand{display:flex;align-items:center;gap:8px;font-weight:600;font-size:15px;color:var(--text)}
  .brand-mark{width:20px;height:20px;color:var(--text);display:block}
  #topnav{display:flex;gap:2px;height:100%}
  #topnav button{height:100%;background:none;border:0;border-bottom:2px solid transparent;color:var(--text-2);font-weight:600;padding:0 10px;cursor:pointer}
  #topnav button:hover{color:var(--text)}
  #topnav button.on{color:var(--text);border-bottom-color:var(--accent)}
  .grow{flex:1}
  .ts{color:var(--text-3);font-size:12px;white-space:nowrap}
  .localchip{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-3);white-space:nowrap}
  .localchip b{color:var(--text-2);font-weight:600;letter-spacing:.04em}
  .localchip.down b{color:var(--s-error)}
  .localchip .sep{opacity:.5}
  .dot{width:6px;height:6px;border-radius:50%;display:inline-block;flex:none;background:var(--s-new)}
  .dot.m{background:var(--s-match)} .dot.l{background:var(--s-local)} .dot.e{background:var(--s-error)} .dot.r{background:var(--s-review)}
  .gearwrap{position:relative}
  .gear{width:28px;height:28px;border:0;background:none;border-radius:6px;color:var(--text-2);cursor:pointer;display:flex;align-items:center;justify-content:center}
  .gear:hover,.gear.on{background:var(--surface-2);color:var(--text)}
  .menu{position:absolute;right:0;top:34px;min-width:210px;background:var(--surface-1);border:1px solid var(--line-strong);border-radius:8px;padding:6px;box-shadow:0 8px 24px rgba(0,0,0,.4);z-index:30}
  .menu button,.menu a{display:flex;width:100%;align-items:center;gap:8px;text-align:left;background:none;border:0;border-radius:6px;padding:6px 8px;color:var(--text);cursor:pointer;font-size:13px;text-decoration:none}
  .menu button:hover,.menu a:hover{background:var(--surface-2);text-decoration:none}
  .menu .sep{height:1px;background:var(--line);margin:6px 2px}
  .menu label{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:4px 8px;color:var(--text-2);font-size:12px}
  .subnav{display:flex;gap:4px;padding:10px 20px 0}
  .subnav button{height:28px;padding:0 12px;border-radius:6px;border:1px solid transparent;background:none;color:var(--text-2);font-weight:600;cursor:pointer}
  .subnav button:hover{color:var(--text)}
  .subnav button.on{background:var(--surface-2);border-color:var(--line-strong);color:var(--text)}
  /* controls */
  select{height:28px;padding:0 8px;border-radius:6px;border:1px solid var(--line-strong);background:var(--surface-2);color:var(--text);cursor:pointer}
  .btn{height:28px;padding:0 12px;border-radius:6px;font-weight:600;border:1px solid var(--accent);background:var(--accent);color:var(--on-accent);cursor:pointer;display:inline-flex;align-items:center;gap:6px}
  .btn:hover{background:var(--accent-strong);border-color:var(--accent-strong)}
  .btn:disabled{opacity:.5;cursor:default}
  .btn.ghost{background:var(--surface-2);color:var(--text);border-color:var(--line-strong)}
  .btn.ghost:hover{border-color:var(--text-3);background:var(--surface-2)}
  .btn.quiet{background:none;border-color:transparent;color:var(--text-2)}
  .btn.quiet:hover{background:var(--surface-2);color:var(--text)}
  .btn.sm{height:24px;padding:0 9px;font-size:12px}
  input[type=search],input[type=text],input[type=number],input[type=url]{height:28px;background:var(--surface-2);border:1px solid var(--line-strong);border-radius:6px;color:var(--text);padding:0 10px;min-width:220px}
  input::placeholder,textarea::placeholder{color:var(--text-3)}
  textarea{width:100%;background:var(--surface-2);border:1px solid var(--line-strong);border-radius:6px;color:var(--text);padding:8px 10px;font:12px/1.5 var(--mono);min-height:120px;resize:vertical}
  pre{white-space:pre-wrap;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:12px;font:12px/1.5 var(--mono);max-height:60vh;overflow:auto}
  .toggle,.ftchk{display:inline-flex;gap:8px;align-items:center;color:var(--text);white-space:nowrap;cursor:pointer;user-select:none}
  .toggle input[type=checkbox]{appearance:none;width:28px;height:16px;border-radius:8px;background:var(--line-strong);position:relative;cursor:pointer;margin:0;flex:none;transition:background .12s}
  .toggle input[type=checkbox]::after{content:"";position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;background:var(--text);transition:transform .12s}
  .toggle input[type=checkbox]:checked{background:var(--accent)}
  .toggle input[type=checkbox]:checked::after{transform:translateX(12px);background:var(--on-accent)}
  .ftchk input[type=number]{width:90px;min-width:0}
  .seg{display:inline-flex;align-self:flex-start;border:1px solid var(--line-strong);border-radius:6px;overflow:hidden;background:var(--surface-2)}
  .seg button{height:26px;padding:0 10px;border:0;background:none;color:var(--text-2);cursor:pointer;font-weight:600;font-size:12px}
  .seg button+button{border-left:1px solid var(--line)}
  .seg button.on{background:var(--surface-3);color:var(--text)}
  label.src{display:inline-flex;align-items:center;gap:7px;margin-right:18px}
  .muted{color:var(--text-2)} .hint{color:var(--text-3);font-size:12px;margin:4px 0}
  .cap{color:var(--text-3);font-size:11px}
  [hidden]{display:none!important}
  main{padding:16px 20px 60px}
```
