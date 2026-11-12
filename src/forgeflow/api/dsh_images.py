"""Publish owner images without following links or widening API-private stores."""
from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path


def save_scoped_image(workspace: Path, image_root: Path, data: bytes, suffix: str,
                      *, evidence_root: str = "") -> Path:
    workspace = workspace.resolve(strict=True)
    if (image_root.parent != workspace / "uploads" or image_root.name in {"", ".", ".."}
            or suffix not in {".png", ".jpg", ".webp", ".gif"}):
        raise ValueError("image_scope_unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        workspace_fd = os.open(workspace, flags)
        descriptors.append(workspace_fd)
        workspace_info = os.fstat(workspace_fd)
        shared = bool(evidence_root.strip())
        if shared:
            evidence = Path(evidence_root)
            if not evidence.is_absolute() or evidence.resolve() != evidence:
                raise ValueError("invalid_image_share")
            evidence_fd = os.open(evidence, flags)
            descriptors.append(evidence_fd)
            evidence_info = os.fstat(evidence_fd)
            group = workspace_info.st_gid
            if (group not in {os.getegid(), *os.getgroups()}
                    or evidence_info.st_gid != group
                    or any(info.st_uid != os.geteuid() or info.st_mode & 0o022
                           or not info.st_mode & stat.S_ISGID
                           for info in (workspace_info, evidence_info))):
                raise ValueError("invalid_image_share")
        directory_mode, file_mode = (0o2750, 0o640) if shared else (0o700, 0o600)
        parent_fd = workspace_fd
        for name in ("uploads", image_root.name):
            try:
                os.mkdir(name, mode=directory_mode, dir_fd=parent_fd)
            except FileExistsError:
                pass
            child_fd = os.open(name, flags, dir_fd=parent_fd)
            descriptors.append(child_fd)
            info = os.fstat(child_fd)
            if info.st_uid != os.geteuid() or (shared and info.st_gid != workspace_info.st_gid):
                raise ValueError("invalid_image_share")
            # Use the opened directory, never chmod a potentially replaced path.
            os.fchmod(child_fd, directory_mode)
            if stat.S_IMODE(os.fstat(child_fd).st_mode) != directory_mode:
                raise ValueError("invalid_image_share")
            parent_fd = child_fd
        name = f"{uuid.uuid4().hex}{suffix}"
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fchmod(handle.fileno(), file_mode)
        except BaseException:
            os.unlink(name, dir_fd=parent_fd)
            raise
        return image_root / name
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
