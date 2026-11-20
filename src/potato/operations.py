from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
from pathlib import Path


class FileOperationError(Exception):
    """Raised when a file operation cannot be completed safely."""


def _absolute_path(path: Path) -> Path:
    """Convert a path to an absolute lexical path."""

    return Path(os.path.abspath(Path(path).expanduser()))


def _inspect_member(
    path: Path,
    root: Path,
) -> tuple[Path, os.stat_result]:
    """
    Validate an existing path inside the browsing root.

    Reject symbolic links in every path component.
    """

    resolved_root = root.expanduser().resolve(strict=True)
    candidate = _absolute_path(path)

    if not candidate.is_relative_to(resolved_root):
        raise PermissionError(
            "Access outside the browsing root is blocked."
        )

    relative = candidate.relative_to(resolved_root)
    current = resolved_root
    info = os.lstat(resolved_root)

    for index, part in enumerate(relative.parts):
        current = current / part
        info = os.lstat(current)

        if stat.S_ISLNK(info.st_mode):
            raise FileOperationError(
                f"Symbolic links are not supported: {current}"
            )

        is_last = index == len(relative.parts) - 1

        if not is_last and not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(current)

    if not (
        stat.S_ISREG(info.st_mode)
        or stat.S_ISDIR(info.st_mode)
    ):
        raise FileOperationError(
            f"Unsupported filesystem object: {candidate}"
        )

    return candidate, info


def _validate_destination(
    destination: Path,
    root: Path,
) -> Path:
    """Validate a new destination and its existing parent."""

    resolved_root = root.expanduser().resolve(strict=True)
    target = _absolute_path(destination)

    if not target.is_relative_to(resolved_root):
        raise PermissionError(
            "Destination is outside the browsing root."
        )

    if target == resolved_root:
        raise FileOperationError(
            "The browsing root cannot be replaced."
        )

    parent, parent_info = _inspect_member(target.parent, resolved_root)

    if not stat.S_ISDIR(parent_info.st_mode):
        raise NotADirectoryError(parent)

    if os.path.lexists(target):
        raise FileExistsError(
            f"Destination already exists: {target}"
        )

    return target


def _copy_regular_file(
    source: Path,
    destination: Path,
    expected_info: os.stat_result,
) -> None:
    """Copy a regular file without following its final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)

    source_fd = os.open(source, flags)
    destination_fd: int | None = None

    try:
        actual_info = os.fstat(source_fd)

        if not stat.S_ISREG(actual_info.st_mode):
            raise FileOperationError(
                "Source changed or is no longer a regular file."
            )

        if (
            actual_info.st_dev != expected_info.st_dev
            or actual_info.st_ino != expected_info.st_ino
        ):
            raise FileOperationError(
                "Source changed during the copy operation."
            )

        destination_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )

        # Start with private permissions until the copy completes.
        destination_fd = os.open(
            destination,
            destination_flags,
            0o600,
        )

        while True:
            chunk = os.read(source_fd, 1024 * 1024)

            if not chunk:
                break

            view = memoryview(chunk)

            while view:
                written = os.write(destination_fd, view)
                view = view[written:]

        # Preserve ordinary rwx permissions, but not setuid/setgid bits.
        os.fchmod(
            destination_fd,
            stat.S_IMODE(expected_info.st_mode) & 0o777,
        )

        os.fsync(destination_fd)

    except Exception:
        if destination_fd is not None:
            os.close(destination_fd)
            destination_fd = None

        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass

        raise

    finally:
        os.close(source_fd)

        if destination_fd is not None:
            os.close(destination_fd)

    try:
        os.utime(
            destination,
            ns=(
                expected_info.st_atime_ns,
                expected_info.st_mtime_ns,
            ),
            follow_symlinks=False,
        )
    except OSError:
        # Timestamp preservation is best-effort.
        pass


def _copy_node(
    source: Path,
    destination: Path,
) -> None:
    """Recursively copy regular files and directories."""

    info = os.lstat(source)

    if stat.S_ISLNK(info.st_mode):
        raise FileOperationError(
            f"Symbolic links cannot be copied: {source}"
        )

    if stat.S_ISREG(info.st_mode):
        _copy_regular_file(source, destination, info)
        return

    if not stat.S_ISDIR(info.st_mode):
        raise FileOperationError(
            f"Unsupported filesystem object: {source}"
        )

    # Keep the directory private while it is being populated.
    destination.mkdir(mode=0o700)

    with os.scandir(source) as entries:
        for entry in entries:
            child_source = source / entry.name
            child_destination = destination / entry.name

            child_info = entry.stat(follow_symlinks=False)

            if stat.S_ISLNK(child_info.st_mode):
                raise FileOperationError(
                    f"Directory contains a symbolic link: {child_source}"
                )

            if not (
                stat.S_ISREG(child_info.st_mode)
                or stat.S_ISDIR(child_info.st_mode)
            ):
                raise FileOperationError(
                    f"Unsupported filesystem object: {child_source}"
                )

            _copy_node(child_source, child_destination)

    os.chmod(
        destination,
        stat.S_IMODE(info.st_mode) & 0o777,
    )

    try:
        os.utime(
            destination,
            ns=(info.st_atime_ns, info.st_mtime_ns),
            follow_symlinks=False,
        )
    except OSError:
        pass


def copy_path(
    source: Path,
    destination: Path,
    root: Path,
) -> Path:
    """
    Copy a file or directory to a new destination.

    Existing destinations are never overwritten.

    A temporary staging directory is used so an incomplete copy
    does not appear as the final destination.
    """

    resolved_root = root.expanduser().resolve(strict=True)

    source_path, _ = _inspect_member(source, resolved_root)
    destination_path = _validate_destination(destination, resolved_root)

    if source_path == destination_path:
        raise FileOperationError(
            "Source and destination are identical."
        )

    if stat.S_ISDIR(os.lstat(source_path).st_mode):
        if destination_path.is_relative_to(source_path):
            raise FileOperationError(
                "A directory cannot be copied into itself."
            )

    staging_directory = Path(
        tempfile.mkdtemp(
            prefix=".potato-copy-",
            dir=destination_path.parent,
        )
    )

    staged_item = staging_directory / destination_path.name

    try:
        _copy_node(source_path, staged_item)

        # Check again before publishing the completed copy.
        if os.path.lexists(destination_path):
            raise FileExistsError(
                f"Destination already exists: {destination_path}"
            )

        os.rename(staged_item, destination_path)

        return destination_path

    finally:
        shutil.rmtree(staging_directory, ignore_errors=True)


def move_path(
    source: Path,
    destination: Path,
    root: Path,
) -> Path:
    """
    Move a file or directory inside the browsing root.

    Uses rename semantics. Cross-filesystem moves are not silently
    converted into copy-and-delete operations.
    """

    resolved_root = root.expanduser().resolve(strict=True)

    source_path, source_info = _inspect_member(
        source,
        resolved_root,
    )

    destination_path = _validate_destination(
        destination,
        resolved_root,
    )

    if source_path == destination_path:
        raise FileOperationError(
            "Source and destination are identical."
        )

    if stat.S_ISDIR(source_info.st_mode):
        if destination_path.is_relative_to(source_path):
            raise FileOperationError(
                "A directory cannot be moved into itself."
            )

    try:
        os.rename(source_path, destination_path)

    except OSError as error:
        if error.errno == errno.EXDEV:
            raise FileOperationError(
                "Cross-filesystem moves are not supported yet."
            ) from error

        raise

    return destination_path


def rename_path(
    source: Path,
    new_name: str,
    root: Path,
) -> Path:
    """Rename a file or directory within its current parent."""

    if (
        not new_name
        or new_name in {".", ".."}
        or "/" in new_name
        or "\x00" in new_name
        or (os.altsep is not None and os.altsep in new_name)
    ):
        raise ValueError("Invalid filename.")

    source_path, _ = _inspect_member(source, root)

    destination = source_path.parent / new_name

    return move_path(
        source_path,
        destination,
        root,
    )
