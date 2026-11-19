from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

MAX_PREVIEW_BYTES = 64 * 1024
MAX_SEARCH_DEPTH = 32
MAX_SEARCH_RESULTS = 500


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


def sort_entries(
    entries: list[FileEntry],
    sort_by: str = "name",
) -> list[FileEntry]:
    """Sort directories first, followed by files."""

    def name_key(entry: FileEntry) -> tuple[bool, str]:
        return (
            not entry.is_dir,
            entry.name.casefold(),
        )

    def size_key(entry: FileEntry) -> tuple[bool, int, str]:
        return (
            not entry.is_dir,
            entry.size,
            entry.name.casefold(),
        )

    def modified_key(entry: FileEntry) -> tuple[bool, float, str]:
        return (
            not entry.is_dir,
            entry.modified,
            entry.name.casefold(),
        )

    if sort_by == "name":
        return sorted(entries, key=name_key)

    if sort_by == "size":
        return sorted(entries, key=size_key)

    if sort_by == "modified":
        return sorted(entries, key=modified_key)

    raise ValueError(f"Unsupported sorting mode: {sort_by}")


def list_directory(
    directory: Path,
    root: Path,
    *,
    include_hidden: bool = False,
) -> list[FileEntry]:
    """List files and directories without following symbolic links."""

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
                if item.is_symlink():
                    continue

                if not include_hidden and item.name.startswith("."):
                    continue

                info = item.stat(follow_symlinks=False)

                is_dir = stat.S_ISDIR(info.st_mode)
                is_file = stat.S_ISREG(info.st_mode)

                if not is_dir and not is_file:
                    continue

                path = Path(item.path)
                resolved_path = path.resolve(strict=True)

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
                continue

    return sort_entries(entries)


def search_files(
    directory: Path,
    root: Path,
    query: str,
    *,
    include_hidden: bool = False,
    max_results: int = MAX_SEARCH_RESULTS,
    max_depth: int = MAX_SEARCH_DEPTH,
) -> list[FileEntry]:
    """
    Search recursively for filenames matching a substring.

    Search is case-insensitive and does not follow symbolic links.

    Results are limited by max_results and max_depth.
    """

    needle = query.casefold().strip()

    if not needle or max_results <= 0:
        return []

    if max_depth < 0:
        raise ValueError("max_depth cannot be negative.")

    resolved_root = canonical_root(root)
    start = directory.resolve(strict=True)

    if not start.is_relative_to(resolved_root):
        raise PermissionError("Access outside the browsing root is blocked.")

    if not start.is_dir():
        raise NotADirectoryError(start)

    results: list[FileEntry] = []

    # Each item contains the directory and its depth.
    stack: list[tuple[Path, int]] = [(start, 0)]

    while stack and len(results) < max_results:
        current, depth = stack.pop()

        try:
            with os.scandir(current) as children:
                for item in children:
                    try:
                        if item.is_symlink():
                            continue

                        if not include_hidden and item.name.startswith("."):
                            continue

                        info = item.stat(follow_symlinks=False)

                        is_dir = stat.S_ISDIR(info.st_mode)
                        is_file = stat.S_ISREG(info.st_mode)

                        if not is_dir and not is_file:
                            continue

                        path = Path(item.path)
                        resolved_path = path.resolve(strict=True)

                        if not resolved_path.is_relative_to(resolved_root):
                            continue

                        entry = FileEntry(
                            name=item.name,
                            path=path,
                            is_dir=is_dir,
                            size=info.st_size,
                            modified=info.st_mtime,
                        )

                        if needle in item.name.casefold():
                            results.append(entry)

                            if len(results) >= max_results:
                                break

                        if is_dir and depth < max_depth:
                            stack.append((resolved_path, depth + 1))

                    except (OSError, RuntimeError):
                        continue

        except (OSError, RuntimeError):
            continue

    return sort_entries(results)


def read_preview(path: Path, root: Path) -> tuple[str, bool]:
    """Read a bounded preview of a regular file."""

    resolved_root = canonical_root(root)
    resolved_path = path.resolve(strict=True)

    if not resolved_path.is_relative_to(resolved_root):
        raise PermissionError("File is outside the browsing root.")

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
        if fd != -1:
            os.close(fd)

    truncated = len(data) > MAX_PREVIEW_BYTES
    data = data[:MAX_PREVIEW_BYTES]

    if b"\x00" in data[:8192]:
        return "[Binary file — preview is unavailable.]", False

    preview = data.decode("utf-8", errors="replace")

    return preview, truncated
