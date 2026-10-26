# Persistent ERP-Bench Odoo runtime

Build from the ForgeFlow repository root:

```sh
docker build -f deploy/vps/Dockerfile.odoo -t forgeflow-odoo:vps3 .
```

The Dockerfile pins Odoo 19 by its multi-platform image digest and verifies the
ERP-Bench archive at commit `ceba3880af555129b5278e056a0c20f2fb5a0ba9` against
`upstream.lock.json`. It preserves task `2280_medium_repair_plan_medium`'s setup,
`tests/test.sh`, and `tests/checks.py` bytes, plus the untouched original scenario
under `/opt/forgeflow/upstream`. The executed scenario uses the repository's
`runner._planning_metadata_projection` (`planning-metadata-v1`), the same profile
as the local workbench: existing workcenter planning fields are exposed in notes,
and every non-note business value is proved unchanged. The source/execution/diff
hashes, changed JSON pointers and runner hash are recorded in
`/opt/forgeflow/task-derivation`; its manifest and diff persist with the generation
and are included in the database binding. The known entrypoint is wrapped to skip
first-boot operations on recovery. PostgreSQL is major 18;
PGDG security/minor package updates are intentionally not a byte-identical build
lock. Record the built image ID/digest in each deployed generation.

For first initialization only, the original setup runs through a small bootstrap
that increases the JSON-2 client timeout from 60 to 900 seconds. Installing the
original seven Odoo modules exceeded 60 seconds in the first VPS attempt. Odoo's
first-boot CPU/wall-clock limits are also raised; a normal recovery uses the base
configuration limits again. The setup, original scenario copy and verifier source
bytes stay unchanged; the executed scenario has only the documented note
projection. The verifier process does not inherit the setup timeout override.

## Mount and environment contract

Create one empty, root-owned host directory per generation. For example:

```sh
install -d -m 0700 /srv/forgeflow/instances/demo-1/odoo
docker run -d --name forgeflow-odoo-demo-1 \
  --restart no --stop-timeout 120 \
  --mount type=bind,src=/srv/forgeflow/instances/demo-1/odoo,dst=/var/lib/forgeflow \
  --env FORGEFLOW_INSTANCE_ID=demo \
  --env FORGEFLOW_GENERATION=1 \
  --env FORGEFLOW_ALLOW_INITIALIZE=1 \
  --publish 127.0.0.1:18069:8069 \
  forgeflow-odoo:vps3
```

The root runtime supervisor owns recovery and detects an unexpected container
restart; Docker must not independently restart this container. The real launcher
additionally applies its own container ownership labels. Keep
the instance ID and generation identical to the launcher's lifecycle manifest.
Do not reuse this directory for another generation or mount it into two running
containers. `FORGEFLOW_ALLOW_INITIALIZE=1` permits initialization only on an empty
volume. A ready volume always restores, even if that flag remains set. For a
container recreation use the same mount, IDs, and built image, and omit the flag.

The launcher must skip its legacy `_preserving_entrypoint` patch for this image:
the image entrypoint is `python3 /opt/forgeflow/vps_odoo.py`. Existing readiness
checks for `/tmp/forgeflow_local_ready` remain compatible. Startup can take up to
30 minutes for the original module installation. Use a 120-second Docker stop
timeout to permit the wrapper's bounded shutdown and PostgreSQL fast shutdown.

Only publish Odoo to host loopback. This retains the benchmark's local database
roles and admin credentials; it is not an Internet-facing Odoo installation.
The ForgeFlow authenticated API is the public entry point. Do not publish 5432.

## Durable state and recovery

`/var/lib/forgeflow` contains:

- `postgresql/`: PostgreSQL cluster, including Odoo data and a separate runtime
  binding table in the `postgres` database.
- `odoo/`: Odoo data directory, including filestore.
- `api_key`, `master_password`, `repair_seeded_records.json`: immutable generation
  credentials and the original seed receipt, with hashes in the manifest and DB
  binding. Credentials never belong in diagnostics or API artifacts.
- `state.json`: schema, instance, generation, task, upstream commit, random nonce,
  artifact digests, the planning data profile and initialization phase.
- `task-derivation.json`, `planning-metadata-v1.diff`: provenance for the public
  note projection; normal restart checks the persisted profile against its image.
- `logs/`: entrypoint, PostgreSQL and original verifier outputs/history.

On startup the wrapper checks the manifest, PostgreSQL version, artifact hashes,
and PostgreSQL binding before restoring `/etc/odoo/api_key`, the exact original
`/tmp/repair_seeded_records.json`, and `/tmp/saas_setup_complete`. It publishes a
current-boot readiness receipt only after Odoo becomes available. A missing seed
receipt is an error; reconstructing it from already-mutated ERP records would
change the meaning of the original verifier.

A crash during first initialization leaves `phase=initializing` and startup
refuses to reseed. Preserve that volume for diagnosis. Start a fresh, explicitly
authorized generation in a new empty directory if initialization must be retried.
Never delete a marker, change IDs in JSON, or turn a partially initialized volume
into a ready one to force startup.

## Required deployment checks

The unit tests cover state and generation boundaries, not a live database. Before
opening the public application, perform these checks on the VPS:

1. Build the image and initialize a fresh generation. Confirm readiness and
   retained seed receipt. Confirm the exact upstream verifier scripts by digest.
2. Record database identity/business counts, seed/key hashes and filestore
   presence without printing credentials. Stop/start, then remove/recreate only
   the container with the same volume. Confirm unchanged state and no reseeding.
3. Run `/tests/test.sh` through `OriginalProductVerifier` after a genuine repair.
   A shell exit code alone is not a pass: inspect its raw reward and strict rule
   projection. Do not replace original tests with a database readback.
4. Test restore into a separate host directory and confirm the full original
   workflow. Verify that a wrong generation or missing receipt refuses startup.

## Consistent backup and maintenance

For the first deployment use a short maintenance window: stop new API execution,
wait for the active operation, stop API/executor, then stop this Odoo container
cleanly. Back up the **whole** Odoo state directory together with the matching
ForgeFlow case store, approval state, execution journal, runtime manifest and
root secrets. Encrypt before uploading to external storage. A directory copy of
a running PostgreSQL cluster is not a valid database backup method.

Preserve file ownership/modes and restore all parts from the same backup point.
The original key expires 90 days after generation by upstream design. Key
rotation is a separate maintenance operation that must update the ERP key and
both artifact bindings atomically; ordinary restart intentionally does not
rotate or reset it. Do not treat VM snapshots alone as a tested restore plan.

## Manual encrypted logical backups

`scripts/vps_backup.py` implements the maintenance backup. There is deliberately
no installed/enabled timer until a tested drain/stop/resume lifecycle exists.
`forgeflow-backup.service` is an optional manually invoked oneshot. Its working
directory and interpreter path must match the actual installation.

Create `/etc/forgeflow/backup.json` outside the repository, root-owned mode 0600:

```json
{
  "project_root": "/opt/forgeflow",
  "odoo_container": "forgeflow-odoo",
  "odoo_state": "/var/lib/forgeflow/sandbox/odoo",
  "runtime_root": "/var/lib/forgeflow/sandbox",
  "case_root": "/var/lib/forgeflow/api/cases",
  "api_data_root": "/var/lib/forgeflow/api/runs",
  "extra_state_roots": {
    "assistant": "/var/lib/forgeflow/assistant",
    "api-home": "/var/lib/forgeflow/api/home"
  },
  "maintenance_units": ["forgeflow-runtime.service"],
  "secret_files": {
    "runtime.json": "/etc/forgeflow/runtime.json",
    "runtime.env": "/etc/forgeflow/runtime.env"
  },
  "encryption_key_file": "/etc/forgeflow/backup.key",
  "output_root": "/var/backups/forgeflow"
}
```

The backup encryption key must be 32 cryptographically random binary bytes,
root-owned mode 0400 or 0600. Generate it once outside the repository and keep an
independent safe recovery copy. Losing that key makes the backup unrecoverable.
The archive excludes its encryption key and SSH private keys. Add any separately
stored application signing/encryption keys to `secret_files` by explicit path;
the example does not claim to discover all deployment-specific secrets.

After preventing new work and waiting for active work to finish, stop
`forgeflow-runtime.service`, then stop `forgeflow-odoo` with the documented
120-second grace period. The backup command requires both to be stopped; it also
checks the systemd main PID/cgroup, any managed writer PID records, lifecycle
locks, execution scope lock and Odoo-volume lock.
It also holds the VPS provisioner's `.provision.lock`, so a normal concurrent
service start cannot recreate or resume the Odoo generation during the backup.

```sh
/opt/forgeflow/.venv/bin/python /opt/forgeflow/scripts/vps_backup.py create \
  --config /etc/forgeflow/backup.json
```

It starts a temporary, network-disabled container from the running generation's
exact image ID. This container starts only PostgreSQL, verifies its generation
binding, writes `bench.dump`, `runtime-binding.dump` and `globals.sql`, and stops
PostgreSQL. It never starts Odoo or seed code. The stopped filestore and explicit
runtime/API/approval state are archived with those dumps; raw PostgreSQL files
are excluded, including when the Odoo volume is nested below the runtime root.
The archive is streamed through AES-256-GCM, then decrypted privately and checked
against every archived file's hash/size/mode/owner. The command leaves application
services stopped so the operator can inspect the result before resuming them.

```sh
/opt/forgeflow/.venv/bin/python /opt/forgeflow/scripts/vps_backup.py restore-staging \
  --archive /var/backups/forgeflow/EXACT-FILENAME.ffbackup \
  --key-file /etc/forgeflow/backup.key \
  --destination /var/lib/forgeflow-restore-test-NEW
```

The destination must not exist. Authentication and all file digests are checked
before any archive member is extracted. Restoring to staging proves archive
integrity, **not yet database/workflow recovery**. An isolated restore drill must
create an empty PostgreSQL 18 cluster, restore globals, restore `bench.dump` with
`pg_restore --create`, restore `runtime-binding.dump` into `postgres`, and use the
matched `odoo/` filestore and seed/key/state files. The globals dump includes the
already-existing PostgreSQL bootstrap role; handle that known duplicate
explicitly instead of ignoring all SQL restore errors. Check the DB binding and
run the complete original application/verifier workflow before claiming recovery.

The isolated logical-database part of this drill is available as:

```sh
/opt/forgeflow/.venv/bin/python /opt/forgeflow/scripts/vps_backup.py restore-db-check \
  --staging /var/lib/forgeflow-restore-test-NEW \
  --destination /var/lib/forgeflow-db-check-NEW
```

This rechecks staged file hashes, creates a separate empty PostgreSQL cluster,
restores only there using a network-disabled temporary container, and checks the
database generation binding plus business row counts. It preserves the resulting
Odoo state for a later isolated application/verifier test, and never replaces the
live state or starts/stops the live runtime. Its receipt explicitly distinguishes
database restoration from the still-required complete application workflow test.

After a known successful original verifier result has been backed up, complete
the isolated Odoo recovery check with:

```sh
/opt/forgeflow/.venv/bin/python /opt/forgeflow/scripts/vps_backup.py restore-app-check \
  --staging /var/lib/forgeflow-restore-test-NEW \
  --database-check /var/lib/forgeflow-db-check-NEW
```

This checks the database-restore receipt, immutable image ID, generation and
archived successful verifier baseline. It refuses a state directory already
attached to any container, then creates a new container with `--network none`, no
published ports and no initialization permission. The normal persistent entrypoint
must restore the same generation. It authenticates with the restored API key and
checks every `ir.attachment.store_fname` against the restored file's SHA-1 checksum.
It invokes the unchanged `/tests/test.sh` through `OriginalProductVerifier`; both
the original result and the strict rule projection must pass. API key/seed/state
bindings must remain unchanged. The resulting container is stopped and retained
with its volume/logs and `application-restore-check.json` as evidence. A rerun needs
a new database-check directory so it cannot silently reuse a previous run.

This proves restored Odoo authentication, attachments and original business
verification. It does not restart the public ForgeFlow API/approval service; its
matching archived state remains in staging and still requires the final public
service recovery drill. Do not call that broader recovery complete based only on
this Odoo check.

## Independent recovery of the exact runtime image

A database archive's image ID is not sufficient if the original VPS is lost.
Export each deployed immutable image once, separately from recurring business
data backups. Keep the matching deployed source/commit manifest as well.

```sh
/opt/forgeflow/.venv/bin/python /opt/forgeflow/scripts/vps_backup.py export-image \
  --image forgeflow-odoo:vps3 --key-file /etc/forgeflow/backup.key \
  --destination /var/backups/forgeflow/odoo-vps3.ffimage
```

This saves the digest-selected local image, checks the Docker archive without
extracting its paths, compresses it, encrypts with AES-256-GCM using a fresh random nonce, and
authenticates/verifies the resulting archive. Its parent directory must be private.
The same backup key can decrypt the image and business archives; it remains outside
both archives. The operation does not stop or access the business container's data.

On the replacement host, before restoring the logical database:

```sh
/opt/forgeflow/.venv/bin/python /opt/forgeflow/scripts/vps_backup.py restore-image \
  --archive /var/backups/forgeflow/odoo-vps3.ffimage \
  --key-file /etc/forgeflow/backup.key
```

The importer requires successful authentication and all file hashes, one untagged
Docker image, safe outer tar members, and an image configuration matching the
recorded immutable ID (config digest or containerd OCI manifest digest, including
its referenced blob digests). It then loads that image and checks the ID again. Tagged
archives are refused to avoid overwriting an existing deployment's tag. Image
availability plus staged data is still separate from the database/application
restore checks above. No offsite upload is implied by local export success.

Google Drive upload is optional. After a separate OAuth authorization, add:

```json
"google_drive": {
  "config_file": "/etc/forgeflow/rclone.conf",
  "remote": "forgeflow-drive",
  "folder": "ForgeFlow/backups"
}
```

The rclone config must be a private root-owned file with a `drive` remote. The
script uploads only the encrypted archive, then verifies remote size and MD5.
Without this configuration its receipt explicitly reports
`not_configured_local_only`; upload failure retains the verified local archive
and reports failure. No Google authorization, upload or remote retention policy
is performed merely by installing this script.
