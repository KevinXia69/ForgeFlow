"""Derived entrypoint and Dockerfile patches for the permission profile.

The read-only Odoo user must exist before the agent runs, but the database it
lives in only exists after the pinned entrypoint bootstraps Odoo. The patch
therefore runs after scenario setup, stops Odoo, provisions the user through
``odoo shell`` (which needs no running server), restarts Odoo, and only then
writes the setup marker. Restarting here is safe because the pinned watchdog
has not started yet.

The read key is world-readable on purpose: it is the credential the agent is
meant to use, and it can only read. The admin key and ``odoo.conf`` become
root-only so the agent cannot obtain the write credential or the database
password.
"""

from __future__ import annotations

import re

PROVISION_FILE = "environment/forgeflow_provision.py"
ENTRYPOINT_FILE = "environment/entrypoint.sh"
DOCKERFILE_FILE = "environment/Dockerfile"
MARKER_LINE = "touch /tmp/saas_setup_complete"
COPY_ANCHOR = "COPY setup_scenario.py /setup/setup_scenario.py\n"
COPY_LINE = "COPY forgeflow_provision.py /setup/forgeflow_provision.py\n"


def provision_block() -> str:
    """Return the shell block inserted before the setup marker."""
    return "\n".join((
        "# --- ForgeFlow permission profile: read-only Odoo credential ---",
        'kill "${odoo_pid}" 2>/dev/null || true',
        "for _ in $(seq 1 60); do",
        "  if ! curl -sf http://127.0.0.1:8069/web/database/selector >/dev/null 2>&1; then break; fi",
        "  sleep 1",
        "done",
        'wait "${odoo_pid}" 2>/dev/null || true',
        "PYTHONUNBUFFERED=1 timeout -k 10 90 odoo shell -d \"$DB_NAME\" --no-http "
        "< /setup/forgeflow_provision.py > /logs/agent/forgeflow-provision.log 2>&1 || true",
        "grep -qx 'SELFCHECK:ok' /logs/agent/forgeflow-provision.log || {",
        "  tail -n 20 /logs/agent/forgeflow-provision.log >&2 || true",
        '  echo "ForgeFlow read-only self-check failed" >&2; exit 1; }',
        "sleep 2",
        "RO_KEY=$(sed -n 's/^RO_KEY://p' /logs/agent/forgeflow-provision.log | tail -n 1)",
        '[ -n "$RO_KEY" ] || { echo "ForgeFlow read-only key missing" >&2; exit 1; }',
        'printf \'%s\\n\' "$RO_KEY" > /etc/odoo/api_key.read',
        "chown root:root /etc/odoo/api_key.read",
        "chmod 0644 /etc/odoo/api_key.read",
        "chmod 0600 /etc/odoo/api_key /etc/odoo/odoo.conf",
        "chmod 0700 /usr/local/bin/harbor-entrypoint.sh",
        "rm -f /logs/agent/forgeflow-provision.log",
        'odoo --config /etc/odoo/odoo.conf --http-port=8069 >>/logs/agent/entrypoint.log 2>&1 &',
        'odoo_pid="$!"',
        "for _ in $(seq 1 120); do",
        "  if curl -sf http://127.0.0.1:8069/web/database/selector >/dev/null 2>&1; then break; fi",
        "  sleep 1",
        "done",
        "curl -sf http://127.0.0.1:8069/web/database/selector >/dev/null 2>&1 || {",
        "  tail -n 20 /logs/agent/entrypoint.log >&2 || true",
        '  echo "ForgeFlow Odoo restart failed" >&2; exit 1; }',
        "RO_READ=$(curl -s -o /dev/null -w '%{http_code}' -X POST "
        "http://127.0.0.1:8069/json/2/sale.order/search "
        "-H \"Authorization: bearer $RO_KEY\" -H 'Content-Type: application/json' "
        "-d '{\"domain\":[]}')",
        "RO_WRITE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "
        "http://127.0.0.1:8069/json/2/purchase.order/create "
        "-H \"Authorization: bearer $RO_KEY\" -H 'Content-Type: application/json' "
        "-d '{\"vals_list\":[{\"partner_id\":999999}]}')",
        '[ "$RO_READ" = "200" ] && [ "$RO_WRITE" = "403" ] || {',
        '  echo "ForgeFlow live read/write probe failed: read=$RO_READ write=$RO_WRITE" >&2',
        "  exit 1; }",
        "RO_HASH=$(printf '%s' \"$RO_KEY\" | sha256sum | cut -d' ' -f1)",
        "printf '{\"selfcheck\":\"ok\",\"ro_read\":\"%s\",\"ro_write\":\"%s\","
        "\"read_key_sha256\":\"%s\",\"api_key_mode\":\"%s\",\"read_key_mode\":\"%s\"}\n' "
        "\"$RO_READ\" \"$RO_WRITE\" \"$RO_HASH\" "
        "\"$(stat -c '%a' /etc/odoo/api_key)\" \"$(stat -c '%a' /etc/odoo/api_key.read)\" "
        "> /logs/agent/forgeflow-provision.json",
        "# --- end permission profile ---",
        "",
    ))


def patch_entrypoint(text: str) -> str:
    """Insert the provisioning block immediately before the setup marker."""
    matches = list(re.finditer(r"(?m)^" + re.escape(MARKER_LINE) + r"[ \t]*$", text))
    if len(matches) != 1:
        raise ValueError("entrypoint: expected exactly one setup marker line")
    if "api_key.read" in text:
        raise ValueError("entrypoint: already provisioned")
    position = matches[0].start()
    return text[:position] + provision_block() + text[position:]


def patch_dockerfile(text: str) -> str:
    """Copy the provisioning script into the image's setup directory."""
    if text.count(COPY_ANCHOR) != 1:
        raise ValueError("dockerfile: expected exactly one setup COPY anchor")
    if COPY_LINE in text:
        raise ValueError("dockerfile: already patched")
    return text.replace(COPY_ANCHOR, COPY_ANCHOR + COPY_LINE, 1)
