"""Compose the opt-in local, full-contract review profile without migrating budget instances."""

from pathlib import Path

from forgeflow.api.app import create_app
from forgeflow.config import load_config
from forgeflow.contract import contract_from_payload
from forgeflow.product.environment import ManagedOdooEnvironment
from forgeflow.product.handoff import REVIEW_PROFILE, HandoffProductExecutor, HandoffRegistry
from forgeflow.recovery.storage import read_json

from .review_session import ReviewSession
from .worker import IMAGE, PiCaseWorker


def build_review_workbench(settings, environment_config, config):
    if config["review_profile"] != REVIEW_PROFILE or settings.deployment != "local":
        raise ValueError("unsupported_local_review_profile")
    root = Path(config["root"])
    if (root / "runtime-binding.json").exists():
        raise ValueError("existing_continuation_runtime")
    environment = ManagedOdooEnvironment(environment_config)
    contract = contract_from_payload(read_json(Path(config["original_contract"])))
    if contract.task_type != "supplier_cancellation":
        raise ValueError("unsupported_review_task")
    registry = HandoffRegistry(
        root / "product-registry",
        socket_root=Path(config["socket_root"]),
        generation=environment.generation,
        executor_factory=environment.executor_factory,
    )
    worker = PiCaseWorker(
        config=load_config(environment_config.repository, require_model=True),
        source_factory=environment.source_factory,
        connection_factory=environment.connection_factory,
        bundle=environment_config.bundle,
        image=config.get("image", IMAGE),
        docker_host=environment_config.docker_host,
    )
    return create_app(
        settings,
        product_executor=HandoffProductExecutor(registry),
        review_session_factory=lambda product: ReviewSession(
            root / "review",
            contract=contract,
            event=read_json(Path(config["event"])),
            notices=config.get("review_notices", []),
            environment=environment,
            worker=worker,
            product=product,
        ),
    )
