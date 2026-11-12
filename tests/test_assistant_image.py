from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from forgeflow.api.app import AssistantImage, _save_assistant_image, _with_attached_image

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z4gAAAABJRU5ErkJggg=="
)


def test_image_is_saved_under_the_harness_workspace(tmp_path) -> None:
    harness = SimpleNamespace(workspace=tmp_path, image_root=tmp_path / "uploads" / "owner")
    image = AssistantImage(data=base64.b64encode(PNG).decode(), mime_type="image/png", name="screen.png")
    path = _save_assistant_image(harness, image)
    assert path.parent == harness.image_root
    assert path.suffix == ".png"
    assert path.read_bytes() == PNG
    assert path.parent != tmp_path


def test_image_requires_an_owner_scoped_harness(tmp_path) -> None:
    image = AssistantImage(data=base64.b64encode(PNG).decode(), mime_type="image/png")
    with pytest.raises(HTTPException) as error:
        _save_assistant_image(SimpleNamespace(workspace=tmp_path), image)
    assert error.value.status_code == 503
    assert error.value.detail == "image_scope_unavailable"


def test_image_prompt_uses_read_image_and_hides_the_browser_name(tmp_path) -> None:
    prompt = _with_attached_image("Describe this.", tmp_path / "uploads" / "x.png")
    assert "Attached image path:" in prompt
    assert "read_image" in prompt
    assert "Describe this." in prompt


def test_rejects_a_payload_that_does_not_match_its_mime_type() -> None:
    image = AssistantImage(data=base64.b64encode(b"not a png").decode(), mime_type="image/png", name="x.png")
    with pytest.raises(HTTPException) as error:
        _save_assistant_image(SimpleNamespace(workspace=Path("/tmp")), image)
    assert error.value.status_code == 400
    assert error.value.detail == "image_invalid"


def test_rejects_invalid_base64() -> None:
    image = AssistantImage(data="!!!!not-base64!!!!", mime_type="image/png", name="x.png")
    with pytest.raises(HTTPException) as error:
        _save_assistant_image(SimpleNamespace(workspace=Path("/tmp")), image)
    assert error.value.status_code == 400
