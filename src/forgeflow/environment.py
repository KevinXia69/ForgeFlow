"""Pinned Harbor Docker adapter with host-owned startup registration."""
from __future__ import annotations

import asyncio
import os

from harbor.environments.docker.docker import (
    DockerEnvironment,
    _sanitize_docker_compose_project_name,
)

from forgeflow.research_containers import register_environment


class GuardedDockerEnvironment(DockerEnvironment):
    async def start(self, force_build: bool):
        # Record ownership before even Harbor's pre-start `down` is permitted.
        await asyncio.to_thread(
            register_environment, self.trial_paths.trial_dir,
            self.environment_dir.resolve(), _sanitize_docker_compose_project_name(self.session_id),
            dict(os.environ),
        )
        return await super().start(force_build=force_build)
