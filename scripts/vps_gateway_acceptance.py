#!/usr/bin/env python3
"""Read-only operator checks for private invites through the reviewer gateway.

Creates login sessions, never cases, approvals, model calls or ERP writes.
The private index and cookie values are not included in the result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from forgeflow.vps_runtime import trusted_json


class AcceptanceFailure(ValueError):
    """A fixed diagnostic label owned by this acceptance script."""


def check(condition, label):
    if not condition:
        raise AcceptanceFailure(label)


def run(base, names, *, fresh=False):
    allowed_names = {*("judge" + str(n) for n in range(1, 6)), "qa1", "qa2", "qa3"}
    check(bool(names) and len(set(names)) == len(names)
          and all(name in allowed_names for name in names), "invalid_reviewer_names")
    index = trusted_json(Path("/etc/forgeflow/reviewers.json"))
    parsed = urlsplit(base)
    check(parsed.username is None and parsed.password is None
          and not parsed.query and not parsed.fragment and parsed.path in {"", "/"},
          "invalid_acceptance_base_url")
    trusted_names = {urlsplit("//" + host).hostname for host in index["trusted_hosts"]}
    check((parsed.scheme == "https" and parsed.hostname in trusted_names)
          or (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"),
          "trusted_https_or_loopback_required")
    host = index["trusted_hosts"][0]
    headers = {"Host": host, "Origin": "https://" + host, "X-Forwarded-Proto": "https"}
    sessions, results = {}, []
    with httpx.Client(base_url=base, headers=headers, timeout=30, trust_env=False) as client:
        def request(method, path, *, cookie=None, **kwargs):
            # No shared cookie jar: every request supplies its own cookie or none.
            client.cookies.clear()
            extra = kwargs.pop("headers", {})
            if cookie:
                extra["Cookie"] = cookie
            return client.request(method, path, headers=extra, **kwargs)

        check(request("GET", "/api/health").status_code == 200, "health_failed")
        page = request("GET", "/app/")
        check(page.status_code == 200 and "<html" in page.text.lower(), "frontend_failed")
        check(request("GET", "/api/environment").status_code == 401, "anonymous_environment_exposed")
        for name in names:
            item = index["instances"][name]
            before = time.monotonic()
            login = request("POST", "/api/auth/session", json={"invite_code": item["invite_code"]})
            check(login.status_code == 201, "login_failed:" + name)
            cookies = login.cookies
            check(all(cookies.get(key) for key in ("ff_session", "__Host-ff_route")), "missing_cookies")
            check(all("Secure" in value and "HttpOnly" in value and "samesite=lax" in value.lower()
                      for value in login.headers.get_list("set-cookie")), "unsafe_cookie_flags")
            cookie = "; ".join(key + "=" + cookies[key] for key in ("ff_session", "__Host-ff_route"))
            sessions[name] = {key: cookies[key] for key in ("ff_session", "__Host-ff_route")}
            principal = request("GET", "/api/auth/me", cookie=cookie)
            check(principal.status_code == 200 and principal.json().get("scope_id"), "principal_unavailable")
            environment = request("GET", "/api/environment", cookie=cookie)
            check(environment.status_code == 200, "environment_unavailable:" + name)
            environment = environment.json()
            config = trusted_json(Path(item["config"]))
            instance = trusted_json(Path(config["sandbox_root"]) / "vps-instance.json")
            check(environment["generation"] == instance["generation"], "wrong_backend_generation:" + name)
            capabilities = request("GET", "/api/capabilities", cookie=cookie)
            check(capabilities.status_code == 200, "capabilities_unavailable")
            capability = capabilities.json()
            check(capability["environment"]["execution"] == "live_sandbox", "live_executor_missing")
            if fresh:
                check(environment["available"] and not environment["owned_by_me"], "reviewer_not_fresh:" + name)
                cases = request("GET", "/api/product/cases", cookie=cookie)
                check(cases.status_code == 200 and cases.json() == [], "reviewer_has_cases:" + name)
            invalid = request("POST", "/api/auth/session", cookie=cookie,
                              json={"invite_code": "invalid-reviewer-acceptance-invite"})
            check(invalid.status_code == 401 and "set-cookie" not in invalid.headers, "failed_login_changes_session")
            check(request("GET", "/api/auth/me", cookie=cookie).json() == principal.json(), "identity_changed")
            check(request("GET", "/api/environment", cookie=cookie,
                          headers={"Origin": "https://untrusted.example"}).status_code == 403, "origin_not_checked")
            check(request("GET", "/api/environment", cookie="ff_session=" + cookies["ff_session"]).status_code == 401,
                  "missing_route_authenticated")
            mutated = cookies["__Host-ff_route"] + "x"
            check(request("GET", "/api/environment", cookie="ff_session=" + cookies["ff_session"] +
                          "; __Host-ff_route=" + mutated).status_code == 401, "forged_route_authenticated")
            logout = request("DELETE", "/api/auth/session", cookie=cookie)
            check(logout.status_code == 204 and len(logout.headers.get_list("set-cookie")) == 2, "logout_failed")
            results.append({"instance": name, "generation": environment["generation"],
                "available": environment["available"], "owned_by_me": environment["owned_by_me"],
                "live_executor": True, "authenticated_checks_seconds": round(time.monotonic() - before, 3),
                "cookie_origin_and_route_checks": True})
        check(len({row["generation"] for row in results}) == len(results), "shared_generation")
        if len(names) >= 2:
            first, second = sessions[names[0]], sessions[names[1]]
            cookie = "ff_session=" + first["ff_session"] + "; __Host-ff_route=" + second["__Host-ff_route"]
            check(request("GET", "/api/environment", cookie=cookie).status_code == 401, "cross_workspace_cookie_pair_accepted")
    return {"checked_at": datetime.now(UTC).isoformat(), "base_url": base,
            "mode": "fresh_reviewers" if fresh else "read_only_authenticated_gateway",
            "frontend_sha256": hashlib.sha256(page.content).hexdigest(), "instances": results,
            "distinct_generations": True, "cross_workspace_cookie_pair_rejected": len(names) >= 2,
            "business_writes": False, "model_calls": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--names", nargs="+", required=True)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = run(args.base_url, args.names, fresh=args.fresh)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"passed": True, "instances": len(result["instances"]),
                          "fresh": args.fresh, "business_writes": False}))
        return 0
    except (ValueError, KeyError, OSError, TypeError, IndexError, AttributeError,
            httpx.HTTPError, httpx.InvalidURL, httpx.CookieConflict) as error:
        # Error text is limited to our labels; HTTP errors can contain cookies/URLs.
        label = str(error) if type(error) is AcceptanceFailure else type(error).__name__
        print(json.dumps({"passed": False, "error": label}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
