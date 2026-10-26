# Rotate the single reviewer ERP environment

Hosted mode has one writable ERP environment. The first principal creating a case
owns that generation persistently; completing a case, logging out, restarting the
API, or changing an environment variable does not release it. Other principals
receive `environment_busy`. Existing cases remain private to their original owner.
This deployment supports scheduled reviewer turns, not simultaneous independent
ERP tenants.

An administrator uses `scripts/vps_rotate.py` to provision a fresh ERP baseline for
the next reviewer. Rotation retains the previous sandbox directory, stopped Docker
container, and all product cases. Historical cases remain readable but cannot
prepare or approve changes against the new generation. Retained environments use
additional disk space; the command never deletes them.

## Preconditions and operation

Run as root on the VPS with the repository virtual environment. Keep
`/etc/forgeflow/runtime.json` root owned and private. Use the current configuration,
not a saved container name from a previous deployment.

```sh
cd /opt/forgeflow
systemctl stop forgeflow-runtime.service
old_container=$(.venv/bin/python -c 'import json; print(json.load(open("/etc/forgeflow/runtime.json"))["container"])')
docker stop "$old_container"
```

The rotation command itself does not stop an active application. It requires the
runtime unit to be strictly `inactive`, `MainPID=0`, an empty service cgroup, no
live managed API/executor PID, and a stopped old Odoo container with the expected
bind mount and immutable image ID. Take a stopped-writer backup using
[odoo-runtime.md](odoo-runtime.md) before rotation when a recovery copy is needed.

```sh
.venv/bin/python scripts/vps_rotate.py --config /etc/forgeflow/runtime.json
```

The command holds the supervisor's `workbench.lock`, the heavy operation lock, and
the old sandbox's provision, store, and Odoo locks throughout. The new provisioner
takes the distinct new sandbox's provision lock. Starting the supervisor or a
backup concurrently fails on the shared maintenance lock.

Rotation creates a unique sibling `sandbox-<operation ID>` and a new container on
the same loopback Odoo port, using the old instance's immutable image digest. It
waits for a validated `ready` record, then explicitly changes the persistent lease
to the new generation and atomically publishes the new runtime configuration.
The case and run roots stay unchanged. New initialization is disabled in the
published configuration. The public API is never automatically started.

Successful output has `phase: "committed"`, old/new generation, sandbox and
container identifiers, and a root-private journal path. It also reports
`backup_configuration_review_required: true`. Before the next backup, update
these three fields in `/etc/forgeflow/backup.json` from the new runtime config:

| Backup field | Runtime configuration value |
| --- | --- |
| `runtime_root` | `sandbox_root` |
| `odoo_state` | `sandbox_root` plus `/odoo` |
| `odoo_container` | `container` |

Keep the backup encryption key, secret-file references and destination unchanged.
If intentionally moving other roots, also reconcile `project_root`, `case_root`
and `api_data_root`. Backup creation checks these bindings against the actual
runtime configuration and ERP generation; stale settings fail closed.

After reviewing the committed output and updating backup configuration:

```sh
systemctl start forgeflow-runtime.service
systemctl is-active forgeflow-runtime.service
```

Check the HTTPS login and authenticated `/api/environment`: it should report the
new generation and `available: true`, with no previous owner's identity. The first
new case claims this generation. No automatic time-based rotation or release is
enabled.

## Interrupted rotation and explicit recovery

Every operation writes `/etc/forgeflow/rotations/<operation ID>.json` before
provisioning. A failure reports the journal path and an error class/phase without
copying child-process diagnostics or credentials. Both service and old container
remain stopped; a partially initialized new container may be running on the
loopback port. Keep it and the journal for inspection.

Read-only inspection does not contact Docker or systemd:

```sh
.venv/bin/python scripts/vps_rotate.py --status /etc/forgeflow/rotations/OPERATION_ID.json
```

Inspect the recorded paths, phase and cause before resuming. Correct the underlying
failure while the public runtime remains stopped, then explicitly resume the same
operation instead of creating another fresh rotation:

```sh
.venv/bin/python scripts/vps_rotate.py --config /etc/forgeflow/runtime.json \
  --resume /etc/forgeflow/rotations/OPERATION_ID.json
```

Resume validates that the current configuration still equals the old or intended
new configuration and that the old environment binding has not changed. It reuses
the same new sandbox. If interrupted after the lease switch, it requires the new
unpublished lease to remain unclaimed and completes publication without resetting
it again. Repeating a committed journal is read-only and preserves the new owner.
Unexpected configuration or generation changes require manual inspection.

The command never rolls the lease back, reuses the old ERP for another principal,
starts the API, or deletes an environment after failure. If configuration publication
fails after the lease has changed, the old configuration deliberately fails its
generation check; finish the recorded recovery before starting the service. Do not
manually clear the lease or set a new generation environment variable as a shortcut.

## Validation

On Linux as root, the following uses synthetic ERP records and temporary
directories; it does not call real Docker or systemd:

```sh
.venv/bin/python -m pytest tests/test_vps_rotate.py tests/test_product_environment_lease.py -q
```

The tests exercise the real maintenance flocks and atomic lease/config files,
cross-owner exclusion, old data preservation, failed initialization, stale
configuration, crash recovery, and API file ownership before any case exists.
