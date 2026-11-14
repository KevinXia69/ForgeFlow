"""Local connection contracts for the pinned DSH 0.2.1-rc.2 / pi-ai 1.4.0.

DSH's explicit protocol table contains exactly the first three APIs below.
Google native is available only by reusing the built-in ``google`` catalog
provider with no profile.api override. Presets describe routes, not a promise
that every account or model supports tools/images; connection tests decide that.
"""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit, urlunsplit

EXPLICIT_APIS = ("openai-completions", "openai-responses", "anthropic-messages")
SUPPORTED_APIS = (*EXPLICIT_APIS, "google-generative-ai")
CHAT_KEY_ENV = "FORGEFLOW_CHAT_API_KEY"
_LOCAL_HOSTS = {"localhost", "host.docker.internal", "host.containers.internal"}

PROVIDER_PRESETS = {
    "deepseek_flash": {"label": "DeepSeek", "provider": "deepseek", "api": "openai-completions",
        "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash", "input": ["text", "image"],
        "endpoints": {"openai-completions": "https://api.deepseek.com/v1",
                      "openai-responses": "https://api.deepseek.com/v1",
                      "anthropic-messages": "https://api.deepseek.com/anthropic"}},
    "nemotron": {"label": "Nebius", "provider": "nebius", "api": "openai-completions",
        "base_url": "https://api.tokenfactory.nebius.com/v1", "model": "nvidia/Nemotron-3.5-Super-VL", "input": ["text", "image"],
        "endpoints": {"openai-completions": "https://api.tokenfactory.nebius.com/v1"}},
    "openai": {"label": "OpenAI", "provider": "openai", "api": "openai-responses",
        "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini", "input": ["text", "image"],
        "endpoints": {"openai-responses": "https://api.openai.com/v1", "openai-completions": "https://api.openai.com/v1"}},
    "anthropic": {"label": "Anthropic", "provider": "anthropic", "api": "anthropic-messages",
        "base_url": "https://api.anthropic.com", "model": "claude-sonnet-4-6", "input": ["text", "image"],
        "endpoints": {"anthropic-messages": "https://api.anthropic.com"}},
    "google": {"label": "Google / Gemini", "provider": "google", "api": "google-generative-ai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta", "model": "gemini-2.5-flash", "input": ["text", "image"],
        "endpoints": {"google-generative-ai": "https://generativelanguage.googleapis.com/v1beta",
                      "openai-completions": "https://generativelanguage.googleapis.com/v1beta/openai"}},
    "openrouter": {"label": "OpenRouter", "provider": "openrouter", "api": "openai-completions",
        "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4.1-mini", "input": ["text", "image"],
        "endpoints": {"openai-completions": "https://openrouter.ai/api/v1", "anthropic-messages": "https://openrouter.ai/api"}},
    "dashscope": {"label": "DashScope / Qwen", "provider": "dashscope", "api": "openai-completions",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "input": ["text"],
        "endpoints": {"openai-completions": "https://dashscope.aliyuncs.com/compatible-mode/v1"}},
    "siliconflow": {"label": "SiliconFlow", "provider": "siliconflow", "api": "openai-completions",
        "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3-8B", "input": ["text"],
        "endpoints": {"openai-completions": "https://api.siliconflow.cn/v1"}},
    "ollama": {"label": "Ollama (local, OpenAI-compatible)", "provider": "ollama", "api": "openai-completions",
        "base_url": "http://host.docker.internal:11434/v1", "model": "qwen3:8b", "input": ["text"],
        "endpoints": {"openai-completions": "http://host.docker.internal:11434/v1"}},
    "custom": {"label": "Custom compatible endpoint", "provider": "forgeflow-custom", "api": "openai-completions",
        "base_url": "", "model": "", "input": ["text"],
        "endpoints": {api: "" for api in EXPLICIT_APIS}},
}


class ProviderConfigError(ValueError):
    """A stable public code; input, endpoints and credentials never enter it."""


def _local_hostname(hostname: str) -> bool:
    if hostname.rstrip(".").lower() in _LOCAL_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(hostname)
        return address.is_loopback or bool(getattr(address, "ipv4_mapped", None)
                                          and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


def normalize_base_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(
        char.isspace() or ord(char) < 32 or char == "\\" for char in value
    ):
        raise ProviderConfigError("invalid_model_base_url")
    try:
        url = urlsplit(value)
        hostname, port = url.hostname, url.port
    except ValueError:
        raise ProviderConfigError("invalid_model_base_url") from None
    if (url.scheme not in {"https", "http"} or not hostname or url.username is not None
            or url.password is not None or url.query or url.fragment
            or (port is not None and not 1 <= port <= 65535)):
        raise ProviderConfigError("invalid_model_base_url")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_link_local or address.is_multicast or address.is_unspecified):
        raise ProviderConfigError("invalid_model_base_url")
    if url.scheme == "http" and not _local_hostname(hostname):
        raise ProviderConfigError("model_https_required")
    try:
        host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ProviderConfigError("invalid_model_base_url") from None
    if ":" in host:
        host = "[" + host + "]"
    authority = host + (f":{port}" if port is not None else "")
    return urlunsplit((url.scheme, authority, url.path.rstrip("/"), "", ""))


def normalize_connection(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) - {
        "preset", "api", "base_url", "model", "custom_endpoint", "image_input"
    }:
        raise ProviderConfigError("invalid_model_connection_fields")
    preset_id = payload.get("preset", "deepseek_flash")
    if not isinstance(preset_id, str) or preset_id not in PROVIDER_PRESETS:
        raise ProviderConfigError("unsupported_model_provider")
    preset = PROVIDER_PRESETS[preset_id]
    api = payload.get("api", preset["api"])
    if not isinstance(api, str) or api not in preset["endpoints"]:
        raise ProviderConfigError("unsupported_model_protocol")
    custom = payload.get("custom_endpoint", False)
    if type(custom) is not bool:
        raise ProviderConfigError("custom_endpoint_confirmation_required")
    base_url = normalize_base_url(payload.get("base_url", preset["endpoints"][api]))
    canonical = preset["endpoints"][api]
    if (not canonical or base_url != normalize_base_url(canonical)) and not custom:
        raise ProviderConfigError("custom_endpoint_confirmation_required")
    model = payload.get("model", preset["model"])
    if not isinstance(model, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+~-]{0,159}", model) is None:
        raise ProviderConfigError("invalid_model_id")
    # A custom endpoint or unknown model must never inherit a vision claim.
    default_image = not custom and model == preset["model"] and "image" in preset["input"]
    image = payload.get("image_input", default_image)
    if type(image) is not bool:
        raise ProviderConfigError("invalid_model_capability")
    if preset_id == "ollama" and not _local_hostname(urlsplit(base_url).hostname or ""):
        raise ProviderConfigError("ollama_local_endpoint_required")
    return {"preset": preset_id, "provider": preset["provider"], "api": api,
            "base_url": base_url, "model": model, "input": ["text", "image"] if image else ["text"],
            "catalog_native": api == "google-generative-ai", "custom_endpoint": custom}


def connection_environment(connection: dict, api_key: str) -> dict[str, str]:
    """Map one validated route into shared chat/worker configuration."""
    return {"FORGEFLOW_ASSISTANT_PROFILE": connection["preset"],
            "FORGEFLOW_ASSISTANT_PROVIDER": connection["provider"],
            "FORGEFLOW_ASSISTANT_API": connection["api"],
            "FORGEFLOW_ASSISTANT_MODEL": connection["model"],
            "FORGEFLOW_ASSISTANT_BASE_URL": connection["base_url"],
            "FORGEFLOW_ASSISTANT_API_KEY_ENV": CHAT_KEY_ENV,
            "FORGEFLOW_MODEL_PROVIDER": connection["provider"], "FORGEFLOW_MODEL_API": connection["api"],
            "FORGEFLOW_MODEL": connection["model"], "FORGEFLOW_MODEL_BASE_URL": connection["base_url"],
            "FORGEFLOW_MODEL_API_KEY": api_key, CHAT_KEY_ENV: api_key,
            "FORGEFLOW_MODEL_INPUT": json.dumps(connection["input"], separators=(",", ":")),
            "FORGEFLOW_MODEL_CATALOG_NATIVE": "true" if connection["catalog_native"] else "false",
            "FORGEFLOW_DSH_MODEL": connection["model"], "FORGEFLOW_DSH_PROVIDER": connection["provider"]}
