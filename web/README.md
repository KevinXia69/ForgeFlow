# ForgeFlow Web (React + assistant-ui)

The workbench front end. After sign-in you land on the ask surface; the case
queue lists exceptions, a case shows the timeline, proposed changes and policy
checks, and the Decision panel issues the single approval and shows the activity
trail and the acceptance receipt. Chat is a drawer attached to the current case;
the evidence view is a general read-only Q&A surface.

Stack:

- React 19.3.1 / React DOM 19.3.1
- `@assistant-ui/react` 0.15.30
- Vite 8.3.5 / TypeScript 5.9.3 / `@vitejs/plugin-react` 5.2.0
- `playwright-core` for browser smoke tests only (drives the local Chrome; no
  browser is downloaded)

## Build

```bash
cd web
npm ci
npm run typecheck
npm run build
```

Output goes to `../src/forgeflow/api/static/app` and is served by FastAPI's
`mount_web` under the `/app/` prefix. `GET /` returns the React page when a build
exists and falls back to the static page otherwise.

## Gates

```bash
npm run test:hermetic   # end-to-end against a mock API, no ERP
npm run test:a11y       # keyboard, structure, 720px reflow
npm run test:impact     # impact chain previews
npm run test:receipt    # receipt rendering
```
