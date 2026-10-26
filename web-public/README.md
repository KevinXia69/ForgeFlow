# ForgeFlow Web Workbench

English-only React source for the ForgeFlow Cases workbench. This is the
public frontend copy: it keeps the full workbench flow while removing the
private bilingual locale data. There is no language switcher in this copy.

## What the workbench includes

- **Cases queue** with saved views for decisions, risk, execution, and verified
  work.
- **Case detail** with the order and disruption facts, timeline, proposed
  changes in table and diff form, policy checks, and a recovery plan preview.
- **Decision** panel with margin, spend, due-date, unit, and policy-check
  context, plus the approve-and-execute flow when a live product executor is
  available.
- **Ask this case** drawer with suggested questions and deterministic answers
  built from the current case fields. The optional case Q&A endpoint is used
  when the backend provides it.
- **Activity Trace** with a summarized work log, step status, elapsed time, and
  a receipt after execution.
- **Evidence** view for historical runs, plans, approvals, and verifier
  metadata.
- **Settings** for the platform model and connection testing. Hosted mode locks
  model overrides. Local/review mode may accept an optional browser-local
  OpenAI-compatible configuration; the platform key is never sent to a browser.
- **Login** with the invitation-code flow used by the local API.

When product execution is unavailable, the Cases view shows a clearly labelled
read-only demo case. The demo case never writes to an ERP.

## Requirements

- Node.js 20.19+ or 22.12+.
- npm.
- Google Chrome only if you want to run the optional browser smoke test.

## Build

```bash
cd web-public
npm ci
npm run typecheck
npm run build
```

`npm run build` writes the static bundle to `web-public/dist`. The Vite base path is
`/app/`, so serve `dist` at that path behind the ForgeFlow API. The frontend
uses same-origin requests under `/api/`.

## Local preview

```bash
cd web-public
npm run preview
```

The preview server prints the URL to open. API requests need the ForgeFlow API
on the same origin; without it, only the login shell is reachable unless API
responses are stubbed.

## Browser smoke test

The smoke test stubs the API, logs in, opens the Cases queue and demo case,
checks Decision, Inbox, Activity, Ask this case, Evidence, Settings, and
asserts that no Chinese characters appear in the rendered page.

```bash
cd web-public
npm run build
npm run smoke
```

The script starts its own local static server on port 4178, mounts the build
at `/app/`, and removes its temporary mount after the check. An optional
`FORGEFLOW_STATIC_DIR` must point to a static root containing `app/index.html`.
It uses the installed Chrome channel through `playwright-core`; it does not
download a browser or call a model or ERP.

## Source layout

```text
web-public/
  index.html
  package.json
  package-lock.json
  tsconfig.json
  vite.config.ts
  src/
    App.tsx            workbench shell and routing
    CasesView.tsx      queue, saved views, case loading/creation
    CaseDetail.tsx     detail, decision, inbox, receipt panels
    CaseChat.tsx       case-scoped Q&A drawer
    Assistant.tsx      Knowledge chat and shared chat surface
    Activity.tsx       activity trace panel
    SettingsDialog.tsx model settings
    i18n.tsx           the single English message dictionary
    caseUi.ts          case formatting and deterministic answers
    runLabels.ts       English task labels and safe fallbacks
```

## Policy

This copy is English-only and contains no Chinese source strings, comments, or
assets. The public repository checker in `scripts/check_public_repo.py` scans
the parent export, including this directory, for CJK text, blocked vendor
terms, user-home paths, private keys, and credentials.

## License

ForgeFlow is released under the Apache License 2.0. See the repository
`LICENSE` and `NOTICE` for the full text and third-party attribution.

## Attribution

The workbench layout and interaction language were inspired by WayRaft
(Apache-2.0). Chat components use assistant-ui (MIT). See the repository
`NOTICE` for the complete attribution.
