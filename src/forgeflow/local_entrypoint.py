"""Pinned local-only startup repair; autonomous task images remain untouched.

The upstream entrypoint initializes every time, then removes its setup program.
Its persistent setup marker is therefore not proof that a restart has completed.
Keep all upstream bytes and only guard first-boot operations and add a per-boot
readiness receipt. Never return source or credential-bearing lines in an error.
"""
from __future__ import annotations

import hashlib

from forgeflow.runtime_lifecycle import LifecycleError

PATH = "/usr/local/bin/harbor-entrypoint.sh"
UPSTREAM_SHA256 = "17081066295528704b6c9af5c66864884ea682640bcc9a154b03e862fe8ccbc5"
MARKER = "/tmp/forgeflow_local_ready"
BOOT_ID = 'cat /proc/sys/kernel/random/boot_id; awk \'{print $22}\' /proc/1/stat'
READY_CHECK = (
    'test -f /tmp/saas_setup_complete && '
    f'test "$(cat {MARKER} 2>/dev/null)" = "$({BOOT_ID})"'
)

PREFIX = b'''# ForgeFlow local preserving startup v1
rm -f /tmp/forgeflow_local_ready
if [ -e /tmp/saas_setup_complete ]; then
  test -f /tmp/saas_setup_complete && test -s /etc/odoo/api_key || exit 1
elif [ -e /etc/odoo/api_key ] || [ ! -f /setup/setup_scenario.py ]; then
  echo "Incomplete initialization retained; explicit diagnosis required"
  exit 1
fi
'''
OPEN = b'if [ ! -f /tmp/saas_setup_complete ]; then # ForgeFlow first boot only\n'
CLOSE = b'fi # ForgeFlow first boot only\n'
RECEIPT = (f'\n# ForgeFlow current boot readiness\n({BOOT_ID}) > {MARKER}\n').encode()


def preserving_source(source: bytes) -> tuple[bytes, dict]:
    """Accept exactly the reviewed original or our reversible transformation."""
    if len(source) > 65536:
        raise LifecycleError("local_entrypoint_unrecognized")
    original = source
    if PREFIX in original:
        if (original.count(PREFIX) != 1 or original.count(OPEN) != 2
                or original.count(CLOSE) != 2 or original.count(RECEIPT) != 1):
            raise LifecycleError("local_entrypoint_unrecognized")
        original = original.replace(PREFIX, b"").replace(OPEN, b"").replace(CLOSE, b"")
        original = original.replace(RECEIPT, b"")
    if hashlib.sha256(original).hexdigest() != UPSTREAM_SHA256:
        raise LifecycleError("local_entrypoint_unrecognized")
    anchors = (b"pg_ctlcluster 18 main start\n", b"su postgres -s /bin/bash -c ",
               b"export HOST=127.0.0.1\n", b"# Create database via HTTP POST",
               b"\nwhile true; do\n")
    if (any(original.count(anchor) != 1 for anchor in (anchors[0], *anchors[2:]))
            or not original.count(anchors[1])):
        raise LifecycleError("local_entrypoint_unrecognized")
    patched = original.replace(anchors[0], PREFIX + anchors[0])
    # The reviewed source has two PostgreSQL role commands; both belong inside
    # one first-boot block, starting at the first command and ending at HOST.
    patched = patched.replace(anchors[1], OPEN + anchors[1], 1)
    patched = patched.replace(anchors[2], CLOSE + anchors[2])
    patched = patched.replace(anchors[3], OPEN + anchors[3])
    patched = patched.replace(anchors[4], CLOSE + RECEIPT + anchors[4])
    if source not in (original, patched):
        raise LifecycleError("local_entrypoint_unrecognized")
    return patched, {
        "schema_version": 1, "mechanism": "local-preserving-entrypoint-v1",
        "original_sha256": UPSTREAM_SHA256,
        "installed_sha256": hashlib.sha256(patched).hexdigest(),
    }
