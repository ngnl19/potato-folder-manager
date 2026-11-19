
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

MAX_PREVIEW_BYTES = 64 * 1024


@dataclass(frozen=True)
class FileEntry:
    """Information about a browsable filesystem entry."""

    name: str
    path: Path
    is_dir: bool
    size: int
    modified: float


def canonical_root(root: Path) -> Path:
    """Resolve and validate the permitted browsing root."""

    resolved = root.expanduser().resolve(strict=True)

    if not resolved.is_dir():
        raise NotADirectoryError(f"Not a directory: {resolved}")

    return resolved


def is_within_root(path: Path, root: Path) -> bool:
    """Check whether an existing path resolves inside the browsing root."""

    try:
        resolved_root = canonical_root(root)
        resolved_path = path.resolve(strict=True)
        return resolved_path.is_relative_to(resolved_root)
    except (OSError, RuntimeError):
        return False


def list_directory(directory: Path, root: Path) -> list[FileEntry]:
    """List regular files and directories without following symlinks."""

    resolved_root = canonical_root(root)
    current = directory.resolve(strict=True)

    if not current.is_relative_to(resolved_root):
        raise PermissionError("Access outside the browsing root is blocked.")

    if not current.is_dir():
        raise NotADirectoryError(current)

    entries: list[FileEntry] = []

    with os.scandir(current) as items:
        for item in items:
            try:
                # Do not browse symbolic links.
                if item.is_symlink():
                    continue

                info = item.stat(follow_symlinks=False)

                is_dir = stat.S_ISDIR(info.st_mode)
                is_file = stat.S_ISREG(info.st_mode)

                # Skip sockets, devices, pipes, and other special files.
                if not is_dir and not is_file:
                    continue

                path = Path(item.path)
                resolved_path = path.resolve(strict=True)

                # Reject paths outside the permitted root.
                if not resolved_path.is_relative_to(resolved_root):
                    continue

                entries.append(
                    FileEntry(
                        name=item.name,
                        path=path,
                        is_dir=is_dir,
                        size=info.st_size,
                        modified=info.st_mtime,
                    )
                )

            except (OSError, RuntimeError):
                # An inaccessible or disappearing entry is skipped.
                continue

    # Directories first, then files, alphabetically.
    entries.sort(
        key=lambda entry: (
            not entry.is_dir,
            entry.name.casefold(),
        )
    )

    return entries


def read_preview(path: Path, root: Path) -> tuple[str, bool]:
    """
    Read a bounded preview of a regular file.

    Returns:
        (preview_text, truncated)
    """

    resolved_root = canonical_root(root)
    resolved_path = path.resolve(strict=True)

    if not resolved_path.is_relative_to(resolved_root):
        raise PermissionError("File is outside the browsing root.")

    # O_NOFOLLOW prevents opening a symbolic link as the final path component.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)

    fd = os.open(resolved_path, flags)

    try:
        with os.fdopen(fd, "rb") as file:
            fd = -1

            info = os.fstat(file.fileno())

            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Only regular files can be previewed.")

            data = file.read(MAX_PREVIEW_BYTES + 1)

    finally:
        # Close the descriptor if fdopen did not take ownership.
        if fd != -1:
            os.close(fd)

    truncated = len(data) > MAX_PREVIEW_BYTES
    data = data[:MAX_PREVIEW_BYTES]

    # Basic binary-file detection.
    if b"\x00" in data[:8192]:
        return "[Binary file — preview is unavailable.]", False

    preview = data.decode("utf-8", errors="replace")

    return preview, truncated