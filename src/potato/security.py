from __future__ import annotations

import grp
import os
import pwd
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import Event

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


@dataclass(frozen=True)
class FileMetadata:
    """Read-only filesystem metadata and effective access information."""

    logical_size_bytes: int
    allocated_size_bytes: int | None
    mode_octal: str
    mode_symbolic: str
    owner_name: str
    owner_uid: int
    group_name: str
    group_gid: int
    can_read: bool
    can_write: bool
    can_execute: bool
    modified: float


@dataclass(frozen=True)
class DirectorySize:
    """Recursive size information for directory contents."""

    logical_size_bytes: int
    allocated_size_bytes: int | None
    file_count: int
    directory_count: int
    errors: int


def format_bytes(size: int) -> str:
    """Format an exact byte count using IEC units."""

    if size < 0:
        raise ValueError("File size cannot be negative.")

    if size < 1024:
        unit = "byte" if size == 1 else "bytes"
        return f"{size:,} {unit}"

    value = float(size)

    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        value /= 1024

        if value < 1024 or unit == "PiB":
            return f"{size:,} bytes ({value:.2f} {unit})"

    return f"{size:,} bytes"


def _lookup_owner(uid: int) -> str:
    """Resolve a UID to a username."""

    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OSError):
        return str(uid)


def _lookup_group(gid: int) -> str:
    """Resolve a GID to a group name."""

    try:
        return grp.getgrgid(gid).gr_name
    except (KeyError, OSError):
        return str(gid)


def _has_effective_access(path: Path, mode: int) -> bool:
    """Inspect access for the effective process credentials."""

    if os.access in getattr(os, "supports_effective_ids", set()):
        return os.access(path, mode, effective_ids=True)

    return os.access(path, mode)


def inspect_file(path: Path, root: Path) -> FileMetadata:
    """Inspect a regular file or directory without changing permissions."""

    resolved_root = canonical_root(root)
    candidate = Path(path)
    resolved_path = candidate.resolve(strict=True)

    if not resolved_path.is_relative_to(resolved_root):
        raise PermissionError("Path is outside the browsing root.")

    info = os.lstat(candidate)

    if stat.S_ISLNK(info.st_mode):
        raise ValueError("Symbolic links cannot be inspected.")

    if not (
        stat.S_ISREG(info.st_mode)
        or stat.S_ISDIR(info.st_mode)
    ):
        raise ValueError(
            "Only regular files and directories are supported."
        )

    allocated_blocks = getattr(info, "st_blocks", None)

    allocated_size = (
        int(allocated_blocks) * 512
        if allocated_blocks is not None
        else None
    )

    permission_bits = stat.S_IMODE(info.st_mode)

    return FileMetadata(
        logical_size_bytes=info.st_size,
        allocated_size_bytes=allocated_size,
        mode_octal=f"{permission_bits:04o}",
        mode_symbolic=stat.filemode(info.st_mode),
        owner_name=_lookup_owner(info.st_uid),
        owner_uid=info.st_uid,
        group_name=_lookup_group(info.st_gid),
        group_gid=info.st_gid,
        can_read=_has_effective_access(resolved_path, os.R_OK),
        can_write=_has_effective_access(resolved_path, os.W_OK),
        can_execute=_has_effective_access(resolved_path, os.X_OK),
        modified=info.st_mtime,
    )


def calculate_directory_size(
    directory: Path,
    root: Path,
    *,
    cancel_event: Event | None = None,
) -> DirectorySize:
    """
    Calculate recursive directory contents without reading file contents.

    - Includes hidden files and directories.
    - Skips symbolic links.
    - Counts only regular files.
    - Does not cross the configured root boundary.
    - Calculates logical and allocated file sizes.
    - Reports inaccessible entries through the errors field.
    - Supports cooperative cancellation.

    Allocated size is based on st_blocks * 512 where available.
    """

    resolved_root = canonical_root(root)
    start = directory.resolve(strict=True)

    if not start.is_relative_to(resolved_root):
        raise PermissionError(
            "Directory is outside the browsing root."
        )

    if not start.is_dir():
        raise NotADirectoryError(start)

    logical_size = 0
    allocated_size = 0

    file_count = 0
    directory_count = 0
    errors = 0

    allocated_available = True

    def handle_walk_error(error: OSError) -> None:
        """Count directories that could not be accessed."""

        nonlocal errors
        errors += 1

    for current_path, dirnames, filenames in os.walk(
        start,
        topdown=True,
        onerror=handle_walk_error,
        followlinks=False,
    ):
        if cancel_event is not None and cancel_event.is_set():
            break

        current = Path(current_path)

        try:
            resolved_current = current.resolve(strict=True)

            if not resolved_current.is_relative_to(resolved_root):
                dirnames[:] = []
                continue

        except (OSError, RuntimeError):
            errors += 1
            dirnames[:] = []
            continue

        safe_directories: list[str] = []

        # Include hidden directories in the total, regardless of
        # whether the user currently displays hidden files in Potato.
        for name in dirnames:
            if cancel_event is not None and cancel_event.is_set():
                break

            candidate = current / name

            try:
                info = candidate.lstat()

                # Exclude symlinks and special filesystem objects.
                if not stat.S_ISDIR(info.st_mode):
                    continue

                safe_directories.append(name)
                directory_count += 1

            except (OSError, RuntimeError):
                errors += 1

        # Only descend into validated directories.
        dirnames[:] = safe_directories

        if cancel_event is not None and cancel_event.is_set():
            break

        # Inspect files and accumulate their actual sizes.
        for name in filenames:
            if cancel_event is not None and cancel_event.is_set():
                break

            candidate = current / name

            try:
                info = candidate.lstat()

                # Do not count symlinks, sockets, devices, or pipes.
                if not stat.S_ISREG(info.st_mode):
                    continue

                logical_size += info.st_size
                file_count += 1

                blocks = getattr(info, "st_blocks", None)

                if blocks is None:
                    allocated_available = False
                else:
                    allocated_size += blocks * 512

            except (OSError, RuntimeError):
                errors += 1

        if cancel_event is not None and cancel_event.is_set():
            break

    return DirectorySize(
        logical_size_bytes=logical_size,
        allocated_size_bytes=(
            allocated_size if allocated_available else None
        ),
        file_count=file_count,
        directory_count=directory_count,
        errors=errors,
    )


def canonical_root(root: Path) -> Path:
    """Resolve and validate the permitted browsing root."""

    resolved = root.expanduser().resolve(strict=True)

    if not resolved.is_dir():
        raise NotADirectoryError(f"Not a directory: {resolved}")

    return resolved


def is_within_root(path: Path, root: Path) -> bool:
    """Check whether a path resolves inside the browsing root."""

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
        return not entry.is_dir, entry.name.casefold()

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
        raise PermissionError(
            "Access outside the browsing root is blocked."
        )

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
    cancel_event: Event | None = None,
) -> list[FileEntry]:
    """Recursively search filenames using os.walk."""

    needle = query.casefold().strip()

    if not needle or max_results <= 0:
        return []

    if max_depth < 0:
        raise ValueError("max_depth cannot be negative.")

    resolved_root = canonical_root(root)
    start = directory.resolve(strict=True)

    if not start.is_relative_to(resolved_root):
        raise PermissionError(
            "Access outside the browsing root is blocked."
        )

    if not start.is_dir():
        raise NotADirectoryError(start)

    results: list[FileEntry] = []

    def ignore_walk_error(error: OSError) -> None:
        return None

    for current_path, dirnames, filenames in os.walk(
        start,
        topdown=True,
        onerror=ignore_walk_error,
        followlinks=False,
    ):
        if cancel_event is not None and cancel_event.is_set():
            break

        current = Path(current_path)

        try:
            resolved_current = current.resolve(strict=True)

            if not resolved_current.is_relative_to(resolved_root):
                dirnames[:] = []
                continue

            depth = len(current.relative_to(start).parts)

        except (OSError, RuntimeError, ValueError):
            dirnames[:] = []
            continue

        safe_directories: list[str] = []

        for name in dirnames:
            if cancel_event is not None and cancel_event.is_set():
                break

            if not include_hidden and name.startswith("."):
                continue

            candidate = current / name

            try:
                info = candidate.lstat()

                if not stat.S_ISDIR(info.st_mode):
                    continue

                if needle in name.casefold():
                    results.append(
                        FileEntry(
                            name=name,
                            path=candidate,
                            is_dir=True,
                            size=info.st_size,
                            modified=info.st_mtime,
                        )
                    )

                if len(results) >= max_results:
                    break

                if depth < max_depth:
                    safe_directories.append(name)

            except (OSError, RuntimeError):
                continue

        dirnames[:] = safe_directories

        if (
            cancel_event is not None and cancel_event.is_set()
        ) or len(results) >= max_results:
            break

        for name in filenames:
            if cancel_event is not None and cancel_event.is_set():
                break

            if not include_hidden and name.startswith("."):
                continue

            if needle not in name.casefold():
                continue

            candidate = current / name

            try:
                info = candidate.lstat()

                if not stat.S_ISREG(info.st_mode):
                    continue

                results.append(
                    FileEntry(
                        name=name,
                        path=candidate,
                        is_dir=False,
                        size=info.st_size,
                        modified=info.st_mtime,
                    )
                )

                if len(results) >= max_results:
                    break

            except (OSError, RuntimeError):
                continue

        if len(results) >= max_results:
            break

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
                raise ValueError(
                    "Only regular files can be previewed."
                )

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
