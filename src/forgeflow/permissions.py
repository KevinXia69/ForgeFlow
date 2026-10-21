"""Provision a read-only Odoo credential for the evaluated agent.

The current ERP-Bench task hands the agent the Odoo admin API key, so a prompt
that says "bash is read-only" cannot stop a direct RPC write. This module
builds the replacement: a dedicated Odoo user whose access rights are read-only
by construction, plus a self-check that fails loudly if any write gate is open.

It produces an Odoo shell script rather than making RPC calls, because two of
the checks (``check_access``) are server-side and must not create test records.
The script is idempotent: re-running it reuses the group, the ACL rows, the
user, and only minting a fresh API key is repeated.

Nothing here talks to a live Odoo; callers run the returned script inside the
task environment and parse the printed markers.

Timing matters: Odoo caches a user's group ids per worker. Provision the user
before the server starts serving, or restart the server after provisioning,
otherwise a newly added group is ignored until the cache is rebuilt.
"""

from __future__ import annotations

import re

READ_ONLY_GROUP = "ForgeFlow Read Only"
READ_ONLY_LOGIN = "forgeflow_ro"
KEY_NAME = "forgeflow-read-only"

# Models the prepare/baseline/readback path reads. Keep this list tied to the
# code that collects the snapshot, the change set and the final readback.
READ_ONLY_MODELS = (
    "sale.order",
    "sale.order.line",
    "purchase.order",
    "purchase.order.line",
    "mrp.production",
    "mrp.workorder",
    "mrp.workcenter",
    "mrp.bom",
    "mrp.bom.line",
    "mrp.routing.workcenter",
    "product.product",
    "product.supplierinfo",
    "stock.quant",
    "stock.move",
    "stock.picking",
    "stock.route",
    "stock.location",
    "res.partner",
    "res.currency",
)

# Models where a write must be refused. The self-check asserts this instead of
# trusting the ACL rows it just wrote, because a broad implied group could
# silently re-grant write access.
WRITE_GATED_MODELS = READ_ONLY_MODELS

# Optional groups that only widen readability:
# * api_doc.group_allow_doc: odoolib introspects through /doc-bearer/<model>.json
#   when an ad-hoc agent script passes positional arguments; ForgeFlow itself
#   calls JSON-2 with keyword arguments and never introspects.
# * product.group_discount_per_so_line: sale.order.line.discount is a
#   group-gated field; without it prepare fails closed when reading SO lines.
# None of these grants write access to the models above.
AUXILIARY_GROUPS = ("api_doc.group_allow_doc", "product.group_discount_per_so_line")

MARKER_KEY = "RO_KEY:"
MARKER_UID = "RO_UID:"
MARKER_SELFCHECK = "SELFCHECK:"
MARKER_MISSING = "MISSING:"


def _literal(values: tuple[str, ...]) -> str:
    return "(\n" + "".join(f"    {value!r},\n" for value in values) + ")"


def provision_script(
    *,
    login: str = READ_ONLY_LOGIN,
    group_name: str = READ_ONLY_GROUP,
    models: tuple[str, ...] = READ_ONLY_MODELS,
    write_gated: tuple[str, ...] = WRITE_GATED_MODELS,
    auxiliary_groups: tuple[str, ...] = AUXILIARY_GROUPS,
    key_days: int = 89,
) -> str:
    """Return an idempotent Odoo shell script that creates the read-only user.

    The script prints ``RO_KEY:<key>``, ``RO_UID:<id>`` and ``SELFCHECK:ok`` (or
    the failed checks) so the caller can verify the credential before use.
    """
    if not 0 < key_days <= 90:
        raise ValueError("key_days must be between 1 and 90.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", login):
        raise ValueError("login must be a simple identifier.")
    if not group_name or re.search(r"['\"\r\n]", group_name):
        raise ValueError("group_name must not contain quotes or newlines.")
    return f"""
import datetime

LOGIN = {login!r}
GROUP_NAME = {group_name!r}
KEY_DAYS = {key_days}
MODELS = {_literal(models)}
WRITE_GATED = {_literal(write_gated)}
AUXILIARY = {_literal(auxiliary_groups)}

group = env["res.groups"].search([("name", "=", GROUP_NAME)], limit=1)
if not group:
    group = env["res.groups"].create({{"name": GROUP_NAME}})

present = []
for name in MODELS:
    model = env["ir.model"].search([("model", "=", name)], limit=1)
    if not model:
        print("MISSING:" + name)
        continue
    present.append(name)
    acl = env["ir.model.access"].search(
        [("name", "=", "forgeflow ro " + name), ("group_id", "=", group.id)], limit=1)
    values = {{
        "name": "forgeflow ro " + name,
        "model_id": model.id,
        "group_id": group.id,
        "perm_read": True,
        "perm_write": False,
        "perm_create": False,
        "perm_unlink": False,
    }}
    if acl:
        acl.write(values)
    else:
        env["ir.model.access"].create(values)

user = env["res.users"].search([("login", "=", LOGIN)], limit=1)
group_ids = [env.ref("base.group_user").id, group.id]
for xmlid in AUXILIARY:
    try:
        group_ids.append(env.ref(xmlid).id)
    except ValueError:
        print("MISSING:" + xmlid)
values = {{
    "name": "ForgeFlow Read Only",
    "login": LOGIN,
    "password": "read-only-not-for-login",
    "group_ids": [(6, 0, group_ids)],
}}
if user:
    user.write(values)
else:
    user = env["res.users"].create(values)

env.cr.commit()

try:
    from odoo.exceptions import AccessError as _AccessError
except Exception:
    _AccessError = None


def _check(model, operation):
    target = env[model].with_user(user)
    check = getattr(target, "check_access", None)
    if check is None:
        return "no_check_access_api"
    try:
        check(operation)
    except Exception as error:
        if _AccessError is not None and isinstance(error, _AccessError):
            return "denied"
        return "error:" + type(error).__name__
    return "allowed"


failures = []
if _AccessError is None:
    failures.append("no_access_error_class")
for name in present:
    result = _check(name, "read")
    if result != "allowed":
        failures.append("read:" + name + ":" + result)
for name in present:
    if name not in WRITE_GATED:
        continue
    for operation in ("write", "create", "unlink"):
        result = _check(name, operation)
        if result != "denied":
            failures.append(operation + ":" + name + ":" + result)

# The key is minted only after every check passes, so a failed run never
# leaves a usable credential behind.
key = None
if not failures:
    key = env["res.users.apikeys"].sudo().with_user(user.id)._generate(
        "rpc", "forgeflow-read-only",
        datetime.datetime.now() + datetime.timedelta(days=KEY_DAYS))
    env.cr.commit()
print("RO_UID:%s" % user.id)
if key is not None:
    print("RO_KEY:%s" % key)
print("SELFCHECK:" + ("ok" if not failures else ",".join(failures)))
"""


def parse_provision_output(text: str) -> dict[str, object]:
    """Read the markers printed by :func:`provision_script`."""
    key = None
    uid = None
    missing: list[str] = []
    selfcheck: list[str] = []
    selfcheck_seen = False
    key_count = 0
    selfcheck_count = 0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(MARKER_KEY):
            key_count += 1
            value = line[len(MARKER_KEY):].strip()
            key = value if re.fullmatch(r"[0-9a-fA-F]{16,128}", value) else None
        elif line.startswith(MARKER_UID):
            value = line[len(MARKER_UID):].strip()
            uid = int(value) if value.isdigit() else None
        elif line.startswith(MARKER_MISSING):
            missing.append(line[len(MARKER_MISSING):].strip())
        elif line.startswith(MARKER_SELFCHECK):
            selfcheck_count += 1
            selfcheck_seen = True
            value = line[len(MARKER_SELFCHECK):].strip()
            selfcheck = [] if value == "ok" else [item for item in value.split(",") if item]
    return {
        "uid": uid,
        "key_present": key is not None,
        "missing": missing,
        "selfcheck": selfcheck,
        "selfcheck_seen": selfcheck_seen,
        "ok": (
            key is not None and uid is not None
            and selfcheck_count == 1 and not selfcheck and not missing
            and key_count == 1
        ),
    }
