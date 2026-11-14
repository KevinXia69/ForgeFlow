"""Private local installation configuration; no secret values in diagnostics.

The configuration is protected by filesystem permissions, not encrypted at rest.
Only fixed provider profiles/endpoints are accepted by the first-run wizard.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import stat
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator
from urllib.parse import quote, urlsplit

from .providers import (
    CHAT_KEY_ENV,
    PROVIDER_PRESETS,
    ProviderConfigError,
    connection_environment,
    normalize_connection,
)

PROFILES = {
    "deepseek_flash": ("deepseek", "deepseek-flash", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "nemotron": ("nebius", "nvidia/Nemotron-3.5-Super-VL", "https://api.tokenfactory.nebius.com/v1", "NEBIUS_API_KEY"),
}
MAX_CONFIG_BYTES = 65_536


class ConfigError(ValueError):
    """A fixed, public error code that never includes input or credentials."""


def config_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get("FORGEFLOW_LOCAL_CONFIG", "/config/runtime.json"))


def _read_private(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ConfigError("unsafe_configuration_permissions")
            value = source.read(MAX_CONFIG_BYTES + 1)
        if len(value) > MAX_CONFIG_BYTES:
            raise ConfigError("configuration_too_large")
        return value
    except FileNotFoundError:
        raise ConfigError("configuration_missing") from None
    except OSError:
        raise ConfigError("configuration_unreadable") from None


def load_config(path: str | Path | None = None) -> dict:
    """Read schema 1. Missing, malformed and unsafe files fail closed."""
    try:
        value = json.loads(_read_private(config_path(path)))
        uuid.UUID(value["installation_id"])
        env, operator = value["env"], value["operator"]
        if type(value["schema_version"]) is not int or value["schema_version"] != 1 or not isinstance(env, dict):
            raise ValueError
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError
        required = ("FORGEFLOW_SESSION_SECRET", "FORGEFLOW_INVITE_CODES",
                    "FORGEFLOW_CONTROL_DATABASE_URL", "FORGEFLOW_ODOO20_PASSWORD",
                    "QDRANT_API_KEY", "FORGEFLOW_LOCAL_ODOO_DB_PASSWORD")
        if any(not env.get(key) for key in required):
            raise ValueError
        if not isinstance(operator, dict) or any(
            not isinstance(operator.get(key), str) or not operator[key]
            for key in ("login", "password", "display_name")
        ):
            raise ValueError
        if (operator["password"] != env["FORGEFLOW_ODOO20_PASSWORD"]
                or operator["login"] != env.get("FORGEFLOW_ODOO20_LOGIN")):
            raise ValueError
        return value
    except ConfigError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ConfigError("configuration_invalid") from None


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Replace one owned regular file; never follow a destination symlink."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigError("unsafe_configuration_path")
    fd, temporary = tempfile.mkstemp(prefix=".forgeflow-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def configuration_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path.parent / ".configuration.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise ConfigError("configuration_locked") from None
    with os.fdopen(fd, "wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _key(value: object, previous: str = "") -> str:
    if value in (None, "") and previous:
        return previous
    if not isinstance(value, str) or not 8 <= len(value) <= 512 or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise ConfigError("valid_provider_keys_required")
    return value


def _new_config(display_name: str) -> dict:
    installation = uuid.uuid4()
    control_password, odoo_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    operator = {"login": "local.operator@forgeflow.local", "password": secrets.token_urlsafe(32),
                "display_name": display_name}
    env = {
        "FORGEFLOW_DEPLOYMENT": "local", "FORGEFLOW_SECURE_COOKIE": "false",
        "FORGEFLOW_SESSION_SECRET": secrets.token_hex(32),
        "FORGEFLOW_INVITE_CODES": secrets.token_urlsafe(24),
        "FORGEFLOW_ENVIRONMENT_GENERATION": installation.hex,
        "FORGEFLOW_TRUSTED_HOSTS": "127.0.0.1:17800,localhost:17800,api:8000",
        "FORGEFLOW_CONTROL_TENANT_ID": "forgeflow-local",
        "FORGEFLOW_KNOWLEDGE_TENANT": "forgeflow-local",
        "FORGEFLOW_SOURCE_PRINCIPAL": "source-service",
        "FORGEFLOW_CONTROL_DATABASE_URL": f"postgresql://forgeflow:{quote(control_password)}@control-db:5432/forgeflow_control",
        "FORGEFLOW_LOCAL_CONTROL_DB_PASSWORD": control_password,
        "FORGEFLOW_LOCAL_ODOO_DB_PASSWORD": odoo_password,
        "FORGEFLOW_LOCAL_ODOO_ADMIN_PASSWORD": secrets.token_urlsafe(32),
        "FORGEFLOW_ODOO20_URL": "http://odoo:8069", "FORGEFLOW_ODOO20_DB": "forgeflow_local",
        "FORGEFLOW_ODOO20_LOGIN": operator["login"], "FORGEFLOW_ODOO20_PASSWORD": operator["password"],
        "FORGEFLOW_KNOWLEDGE_GATEWAY_URL": "http://api:8000",
        "FORGEFLOW_KNOWLEDGE_ROOT": "/data/knowledge", "FORGEFLOW_APPROVALS_ROOT": "/data/approvals",
        "FORGEFLOW_ATTACHMENTS_ROOT": "/data/attachments",
        "FORGEFLOW_DSH_HOME": "/data/dsh/home", "FORGEFLOW_DSH_WORKSPACE": "/data/dsh/workspace",
        "FORGEFLOW_DSH_PATCH": "/data/dsh/patch.json",
        "FORGEFLOW_DSH_ENABLED": "true", "FORGEFLOW_DSH_SDK": "true",
        "FORGEFLOW_DATA_ROOT": "/data/runs", "FORGEFLOW_RUNNER_ENABLED": "false",
        "QDRANT_URL": "https://qdrant:6333", "QDRANT_API_KEY": secrets.token_urlsafe(32),
        "QDRANT_COLLECTION": "forgeflow_knowledge_v1", "QDRANT_TENANT_FIELD": "tenant_id",
        "EMBEDDING_API_URL": "https://api.siliconflow.cn/v1/embeddings",
        "EMBEDDING_MODEL_ID": "Qwen/Qwen3-Embedding-8B",
        "RERANKER_API_URL": "https://api.siliconflow.cn/v1/rerank",
        "RERANKER_MODEL_ID": "Qwen/Qwen3-Reranker-8B",
        "SSL_CERT_FILE": "/config/ca-bundle.pem", "REQUESTS_CA_BUNDLE": "/config/ca-bundle.pem",
        "FORGEFLOW_SEMANTIC_CA_FILE": "/config/ca-bundle.pem",
    }
    return {"schema_version": 1, "installation_id": str(installation), "env": env, "operator": operator}


def _certificate_files(config_root: Path, qdrant_root: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    certificate, private_key = qdrant_root / "cert.pem", qdrant_root / "key.pem"
    bundle = config_root / "ca-bundle.pem"
    if all(path.is_file() and not path.is_symlink() for path in (certificate, private_key, bundle)):
        return  # Key updates preserve the installation's trust anchor.
    if any(path.exists() or path.is_symlink() for path in (certificate, private_key, bundle)):
        raise ConfigError("incomplete_local_tls_configuration")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ForgeFlow local Qdrant")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("qdrant")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
    atomic_write(certificate, cert_bytes, 0o644)
    atomic_write(private_key, key.private_bytes(serialization.Encoding.PEM,
                 serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), 0o640)
    import certifi
    atomic_write(bundle, Path(certifi.where()).read_bytes() + b"\n" + cert_bytes, 0o644)


def _write_service_files(value: dict, path: Path, mounts: dict[str, Path] | None) -> None:
    import yaml

    roots = mounts or {"control": Path("/control-secret"), "odoo": Path("/odoo-secret"),
                       "odoo_config": Path("/odoo-config"), "qdrant": Path("/qdrant-config")}
    env = value["env"]
    atomic_write(roots["control"] / "password", env["FORGEFLOW_LOCAL_CONTROL_DB_PASSWORD"].encode(), 0o444)
    atomic_write(roots["odoo"] / "password", env["FORGEFLOW_LOCAL_ODOO_DB_PASSWORD"].encode(), 0o444)
    odoo = ("[options]\ndb_host = odoo-db\ndb_port = 5432\ndb_user = odoo\n"
            f"db_password = {env['FORGEFLOW_LOCAL_ODOO_DB_PASSWORD']}\n"
            f"admin_passwd = {env['FORGEFLOW_LOCAL_ODOO_ADMIN_PASSWORD']}\n"
            "http_interface = 0.0.0.0\n"
            "db_name = forgeflow_local\ndbfilter = ^forgeflow_local$\nlist_db = False\n"
            "data_dir = /var/lib/odoo\naddons_path = /usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons\nproxy_mode = False\n")
    atomic_write(roots["odoo_config"] / "odoo.conf", odoo.encode(), 0o640)
    bootstrap = {"schema_version": 1, "installation_id": value["installation_id"],
                 "database": "forgeflow_local", "operator": value["operator"],
                 "admin_password": env["FORGEFLOW_LOCAL_ODOO_ADMIN_PASSWORD"]}
    atomic_write(roots["odoo_config"] / "bootstrap.json", json.dumps(bootstrap).encode(), 0o640)
    _certificate_files(path.parent, roots["qdrant"])
    qdrant = {"service": {"api_key": env["QDRANT_API_KEY"], "enable_tls": True,
                          "host": "0.0.0.0", "http_port": 6333, "grpc_port": 6334},
              "tls": {"cert": "/etc/forgeflow/cert.pem", "key": "/etc/forgeflow/key.pem"}}
    atomic_write(roots["qdrant"] / "config.yaml", yaml.safe_dump(qdrant).encode(), 0o640)


def previous_connection(previous: dict | None) -> dict | None:
    if not previous:
        return None
    if isinstance(previous.get("model_connection"), dict):
        return previous["model_connection"]
    env = previous["env"]
    profile = env.get("FORGEFLOW_ASSISTANT_PROFILE", "deepseek_flash")
    if profile not in PROVIDER_PRESETS:
        raise ConfigError("unsupported_model_profile")
    return normalize_connection({"preset": profile})


def shared_deepseek_search(connection: dict) -> bool:
    return (connection["preset"] == "deepseek_flash" and not connection["custom_endpoint"]
            and urlsplit(connection["base_url"]).hostname == "api.deepseek.com")


def prepare_model(payload: object, previous: dict | None = None) -> tuple[dict, str]:
    """Resolve a draft without writes; a new authority never inherits a saved key."""
    if not isinstance(payload, dict):
        raise ConfigError("invalid_configuration_fields")
    try:
        connection = normalize_connection(payload.get("model_connection") or {
            "preset": payload.get("profile", "deepseek_flash")})
    except ProviderConfigError as error:
        raise ConfigError(str(error)) from None
    prior = previous_connection(previous)
    old_key = ""
    if prior and (prior["provider"], urlsplit(prior["base_url"]).scheme, urlsplit(prior["base_url"]).netloc) == (
            connection["provider"], urlsplit(connection["base_url"]).scheme, urlsplit(connection["base_url"]).netloc):
        env = previous["env"]
        old_key = env.get(CHAT_KEY_ENV, "")
        if not old_key and prior["preset"] in PROFILES:
            old_key = env.get(PROFILES[prior["preset"]][3], "")
    key = payload.get("model_api_key")
    if connection["preset"] == "ollama" and not key:
        # Ollama ignores this public compatibility placeholder; it is not a secret.
        key = old_key or "ollama"
    elif not key:
        key = old_key
    if connection["preset"] == "ollama" and key == "ollama":
        return connection, key
    return connection, _key(key)


def save_configuration(payload: object, path: str | Path | None = None, *,
                       mounts: dict[str, Path] | None = None,
                       expected_connection: dict | None = None,
                       expected_key: str | None = None) -> dict:
    """Save one tested route, preserving installation and internal credentials."""
    if not isinstance(payload, dict) or set(payload) - {
        "profile", "model_connection", "model_api_key", "deepseek_search_api_key",
        "dashscope_api_key", "siliconflow_api_key", "display_name"
    }:
        raise ConfigError("invalid_configuration_fields")
    target = config_path(path)
    with configuration_lock(target):
        previous = load_config(target) if target.exists() or target.is_symlink() else None
        connection, model_key = prepare_model(payload, previous)
        if expected_connection is not None and (connection != expected_connection or model_key != expected_key):
            raise ConfigError("model_configuration_changed_retest")
        display = payload.get("display_name", (previous or {}).get("operator", {}).get("display_name", "Local operator"))
        if not isinstance(display, str) or not 1 <= len(display.strip()) <= 100 or any(ord(c) < 32 for c in display):
            raise ConfigError("invalid_display_name")
        value = previous or _new_config(display.strip())
        env = value["env"]
        search_key = (model_key if shared_deepseek_search(connection) else
                      _key(payload.get("deepseek_search_api_key"), env.get("DEEPSEEK_API_KEY", "")))
        dashscope = _key(payload.get("dashscope_api_key"), env.get("DASHSCOPE_API_KEY", ""))
        siliconflow = _key(payload.get("siliconflow_api_key"), env.get("SEMANTIC_API_KEY", ""))
        if not dashscope.startswith("sk-"):
            raise ConfigError("dashscope_api_key_format_invalid")
        # Dedicated chat credentials prevent a custom/OpenAI/etc. key from being
        # mistaken for the separate official DeepSeek web-search credential.
        env.pop("NEBIUS_API_KEY", None)
        env.update(connection_environment(connection, model_key))
        env.update({"DEEPSEEK_API_KEY": search_key, "DASHSCOPE_API_KEY": dashscope,
                    "SEMANTIC_API_KEY": siliconflow})
        if connection["preset"] == "nemotron":
            env["NEBIUS_API_KEY"] = model_key  # Legacy reviewed Nebius consumers.
        value["model_connection"] = connection
        value["operator"]["display_name"] = display.strip()
        _write_service_files(value, target, mounts)
        atomic_write(target, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
        return value
