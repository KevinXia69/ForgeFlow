"""Local Pi worker runner through the pinned Docker image."""
from __future__ import annotations

import os
import subprocess


class PiDockerRunner:
    def __init__(self, *, docker_host: str, image: str, api_key: str,
                 provider: str = "deepseek", model: str = "deepseek-v4-flash",
                 timeout: float = 180.0) -> None:
        if not docker_host or not image or not api_key:
            raise ValueError("pi_docker_configuration_required")
        self.docker_host = docker_host
        self.image = image
        self.api_key = api_key
        self.provider = provider
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "PiDockerRunner":
        return cls(
            docker_host=os.environ.get("DOCKER_HOST", ""),
            image=os.environ.get("FORGEFLOW_PI_IMAGE", "forgeflow-pi:test"),
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            provider=os.environ.get("FORGEFLOW_PI_PROVIDER", "deepseek"),
            model=os.environ.get("FORGEFLOW_PI_MODEL", "deepseek-v4-flash"),
            timeout=float(os.environ.get("FORGEFLOW_PI_TIMEOUT", "180")),
        )

    def ask(self, prompt: str) -> str:
        return self._run(prompt, json_stream=False).stdout

    def complete(self, prompt: str, *, run_token: str | None = None):
        """Run Pi and return the final message with a real usage receipt.

        ``--mode json`` makes Pi emit one JSON event per line, which carries the
        provider's own token usage.  Without it a caller can only estimate usage
        from the prompt length, which is not a receipt.
        """
        del run_token
        from .model_runners import ModelReply, _final_text, parse_pi_usage

        completed = self._run(prompt, json_stream=True)
        usage = parse_pi_usage(completed.stdout)
        usage.provider = self.provider
        return ModelReply(_final_text(completed.stdout), usage.as_dict(), usage.complete)

    def _run(self, prompt: str, *, json_stream: bool):
        command = [
            "docker", "run", "--rm",
            "-e", "DEEPSEEK_API_KEY",
            self.image,
            "node", "/opt/forgeflow/pi/packages/coding-agent/dist/bundle/cli.js",
            "--provider", self.provider, "--model", self.model,
            "--print", "--no-session",
            "--system-prompt", "You are a read-only worker. Return only one JSON object.",
        ]
        if json_stream:
            command.extend(["--mode", "json"])
        command.append(prompt)
        env = os.environ.copy()
        env["DOCKER_HOST"] = self.docker_host
        env["DEEPSEEK_API_KEY"] = self.api_key
        completed = subprocess.run(command, env=env, capture_output=True, text=True,
                                   timeout=self.timeout, check=False)
        if completed.returncode != 0:
            raise RuntimeError("pi_worker_failed")
        return completed
