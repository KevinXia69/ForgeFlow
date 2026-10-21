# Publishing the ForgeFlow public repository

This runbook describes the local-only export process for ForgeFlow. The toolkit
copies a curated English-only subset of the workspace, sanitizes recorded demo
evidence, and verifies the result before anyone publishes it. The process does
not create or rename a GitHub repository, does not push, and does not run
Docker, containers, or model requests.

## What becomes public

The exporter copies only this allowlist:

- `README.md` (the private English README when it passes the policy, otherwise a
  generated English public README).
- `LICENSE` and `NOTICE`.
- `pyproject.toml` and `uv.lock`.
- `src/` source files; CJK in `src/*.py` is emitted as Unicode escapes and blocked vendor terms are neutralized for the English-only export.
- `tests/` test files that pass the content policy.
- `web/` is a complete, buildable English-only React workbench copied from `web-public/`; the private bilingual `web/` source is never exported.
- `contracts/` JSON policy contracts.
- `scripts/export_public_repo.py` and `scripts/check_public_repo.py`.
- `demo-runs/` after location sanitization, unless `--no-demo-runs` is used.

A file that contains any blocked content is omitted and listed in the export
summary. The export is therefore a policy-clean subset, not a byte-for-byte
mirror. To publish more of the source tree, remove or translate the offending
content in the private workspace first; do not weaken the checker.

## What stays private

The following material must never be copied into the public export:

- Chinese-language locale strings, Chinese documentation, and source material
  for recorded demos and video.
- Internal planning notes, progress logs, experiment records, attribution
  audits, and historical archives.
- Environment files, API keys, session secrets, invitation codes, signing keys,
  and private credentials of any kind.
- Full run directories, agent traces, prompts, provider request logs, task
  fixtures, reference solutions, and implementation archives.
- Pinned upstream source snapshots, build inputs, model data packages, and
  container build context.
- Runtime state, approval state, local databases, caches, deployment secrets,
  and presentation or PDF material.
- Git metadata, dependency trees, generated frontend bundles, and the built
  static application bundle.
- The supplemental-materials directory and any organizer-provided contest
  files.

The exporter copies only the fixed allowlist above. It never walks the private
directories by default, so they cannot leak into the destination.

## Export command

Run from the private workspace root:

```bash
uv run python scripts/export_public_repo.py --out ../forgeflow-public
```

Omit demo evidence when it is not needed:

```bash
uv run python scripts/export_public_repo.py --out ../forgeflow-public --no-demo-runs
```

The destination must be outside the private workspace. Existing contents of the
destination are replaced only after the source preflight succeeds. The exporter
fails with exit code 2 if a required file is missing or unsafe, if a demo-run
JSON file cannot be sanitized safely, or if the finished tree still violates
the public policy.

## Scan command

Run the checker against the finished directory:

```bash
uv run python scripts/check_public_repo.py ../forgeflow-public
```

The checker exits 0 when the tree is clean. It exits 1 and prints one
`path:line: kind: message` line per finding when it detects:

- any CJK character in a file name or file body;
- a blocked model-vendor or product term, matched case-insensitively;
- an absolute user-home path in Unix or Windows form;
- a PEM private-key block;
- a common API-key, bearer-token, or invitation-code pattern;
- a missing or empty root `LICENSE`.

Binary files are skipped without decoding, and common VCS, cache, and
dependency directories are not walked. The checker never prints a matched
secret value.

## Demo-run sanitization

Each text file below `demo-runs/` is rewritten. JSON object keys named
`trial_uri`, `workspace_root`, and `trials_dir` are removed. Remaining strings
have the private workspace prefix and generic absolute user-home prefixes
replaced with relative references or a neutral home marker. JSON pointers that
describe benchmark data are not filesystem paths and are preserved. Verifier
results, scores, rule outcomes, and recovery-plan artifacts are kept.

Attestation signature files are retained as provenance, but rewriting the
signed JSON means the signatures no longer validate the rewritten payloads.
Do not present them as proof of the sanitized files.

If a value still contains CJK text, a blocked term, a user-home path, a private
key, or a credential pattern after sanitization, the export aborts with the
offending file and line. Only signature blobs may be copied as opaque bytes;
any other undecodable demo file also aborts the export. Never bypass a failure
by copying the raw file.

## Fresh-history procedure

Use a new empty directory and a new Git history. Do not copy the private `.git`
directory, and do not rewrite or transplant the private commit history.

```bash
uv run python scripts/export_public_repo.py --out ../forgeflow-public
uv run python scripts/check_public_repo.py ../forgeflow-public
cd ../forgeflow-public
git init
git add .
git commit -m "Initial public export"
git remote -v
git log --oneline
```

`git remote -v` must print nothing. The initial commit must contain only the
files produced by the exporter. If a later review changes the export, rerun the
exporter into a fresh directory and repeat the scan rather than editing the
published tree by hand.

Creating a remote repository, renaming one, and pushing are separate actions
that require explicit human approval. This toolkit deliberately performs none
of them. Do not run Docker, containers, or model requests as part of publishing.

## License

ForgeFlow is licensed under the Apache License 2.0. The full text is in
`LICENSE`. Third-party attribution is in `NOTICE`, including ERP-Bench (CC0),
Pi (MIT), Harbor (Apache-2.0), WayRaft and DeepTutor (Apache-2.0, layout and
interaction inspiration only), assistant-ui (MIT), and the Nebius and NVIDIA
model services.

The public snapshot does not redistribute upstream task data, model weights, or
container images. Third-party packages remain subject to their own licenses.
