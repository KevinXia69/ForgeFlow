# Native DSH Ask: historical developer surface

Updated 2026-10-22 for v9. The business entry is now Cases in ForgeFlow's own
React workbench. Read-only questions use the DSH SDK/CLI bridge, with a
deterministic fallback. The full Native Web shell is no longer the default Ask
page or the application's navigation surface.

Native startup scripts remain as developer tooling. The API withholds the
native URL by default; explicit local developer configuration is required even
to expose it to an authenticated session. Review always withholds it. A returned
URL does not make the business UI mount the old iframe. Do not enable shell or
filesystem writes to make a developer control work.

The authenticated host supplies an owner-scoped evidence snapshot to the MCP.
No identity sent by the model grants access. Public review has no DSH runtime
and uses its read-only fallback. The current runtime was checked with real Nebius requests and synthetic
owner-scoped cases: exactly six read-only MCP tools, cross-owner denial, session
resume and SSE reconnect. This is not an ERP
execution or a new public deployment. Earlier SDK and Native Web observations
below remain historical evidence.

The current business profile disables session log and feedback modules; telemetry
is DISABLED and no exporter is installed. This follows an audit of the pinned
upstream defaults, not evidence that older Nebius requests uploaded logs.
Browser acceptance covered in-flight refresh, Stop followed by a different
question, navigation back, and case evidence links. Stop ends delivery, not a
confirmed provider interruption. Replay jobs are process-local; a missing job
returns an explicit error rather than generating a new answer automatically.

## Historical operator instructions (pre-v9; not executed in this iteration)

The local Ask surface can run the full DeepSeek Harness Web shell instead of
ForgeFlow's hybrid chat. ForgeFlow keeps its own navigation, Cases, approval,
execution and system-of-record views.

### Start the sidecar (historical)

Use the supervisor so the rotating auth URL is handled automatically:

```bash
nohup .venv/bin/python scripts/dsh_web_sidecar.py \
  > .runtime/dsh-sidecar.out 2>&1 &
```

It starts DSH Web with the ForgeFlow patch and writes the authenticated URL to
the ignored `.runtime/dsh-web-url` file (mode 0600). ForgeFlow reads that file
on each `GET /api/config`; do not copy the token into source control.

If the sidecar restarts, the supervisor replaces the URL file. Refresh the
browser to reconnect; the ForgeFlow API does not need a restart. If the file is
missing, the Ask route falls back to the read-only ForgeFlow surface.

`GET /api/config` exposes `dsh_web_url` only when the deployment is local;
review deployments always return an empty value.

### Behavior (historical)

- Local Ask: full DSH Web shell inside the ForgeFlow branded bar.
- “Cases →” exits the shell into the ForgeFlow Cases workbench.
- Review/absent URL: ForgeFlow's read-only chat fallback remains.
- The DSH URL token is a local credential. Never commit it; `.runtime/` is
  ignored. The supervisor owns rotation.
- If DSH shows the preview notice, click “Continue” once; the DSH shell stores
  its own local preference.
