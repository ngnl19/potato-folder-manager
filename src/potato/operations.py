from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event


class FileOperationError(Exception):
    """Raised when a file operation cannot complete safely."""


class OperationCancelled(FileOperationError):
    """Raised when an operation is cancelled."""


@dataclass(frozen=True)
class OperationProgress:
    """Progress information for a filesystem operation."""

    phase: str
    current_path: Path
    bytes_completed: int
    total_bytes: int
    files_completed: int
    total_files: int

    @property
    def percentage(self) -> float:
        """Calculate progress percentage."""

        if self.phase == "completed":
            return 100.0

        if self.total_bytes > 0:
            return min(
                100.0,
                self.bytes_completed * 100.0 / self.total_bytes,
            )

        if self.total_files > 0:
            return min(
                100.0,
                self.files_completed * 100.0 / self.total_files,
            )

        return 0.0


ProgressCallback = Callable[[OperationProgress], None]


def _check_cancel(cancel_event: Event | None) -> None:
    """Raise when an operation has been cancelled."""

    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Operation cancelled.")


def _report(
    callback: ProgressCallback | None,
    *,
    phase: str,
    path: Path,
    bytes_completed: int,
    total_bytes: int,
    files_completed: int,
    total_files: int,
) -> None:
    """Send a progress update when a callback is supplied."""

    if callback is None:
        return

    callback(
        OperationProgress(
            phase=phase,
            current_path=path,
            bytes_completed=bytes_completed,
            total_bytes=total_bytes,
            files_completed=files_completed,
            total_files=total_files,
        )
    )


def _absolute_path(path: Path) -> Path:
    """Convert a path into an absolute lexical path."""

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

    parent, parent_info = _inspect_member(
        target.parent,
        resolved_root,
    )

    if not stat.S_ISDIR(parent_info.st_mode):
        raise NotADirectoryError(parent)

    if os.path.lexists(target):
        raise FileExistsError(
            f"Destination already exists: {target}"
        )

    return target


def _scan_source(
    source: Path,
    cancel_event: Event | None,
) -> tuple[int, int]:
    """
    Inspect a source tree before copying.

    Return total logical bytes and number of regular files.
    Reject links and special files before staging begins.
    """

    info = os.lstat(source)

    if stat.S_ISREG(info.st_mode):
        return info.st_size, 1

    if not stat.S_ISDIR(info.st_mode):
        raise FileOperationError(
            f"Unsupported source: {source}"
        )

    total_bytes = 0
    total_files = 0

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current_path, dirnames, filenames in os.walk(
        source,
        topdown=True,
        onerror=raise_walk_error,
        followlinks=False,
    ):
        _check_cancel(cancel_event)

        current = Path(current_path)
        safe_directories: list[str] = []

        for name in dirnames:
            _check_cancel(cancel_event)

            child = current / name
            child_info = os.lstat(child)

            if stat.S_ISLNK(child_info.st_mode):
                raise FileOperationError(
                    f"Symbolic link in source tree: {child}"
                )

            if not stat.S_ISDIR(child_info.st_mode):
                raise FileOperationError(
                    f"Unsupported directory entry: {child}"
                )

            safe_directories.append(name)

        dirnames[:] = safe_directories

        for name in filenames:
            _check_cancel(cancel_event)

            child = current / name
            child_info = os.lstat(child)

            if stat.S_ISLNK(child_info.st_mode):
                raise FileOperationError(
                    f"Symbolic link in source tree: {child}"
                )

            if not stat.S_ISREG(child_info.st_mode):
                raise FileOperationError(
                    f"Unsupported filesystem object: {child}"
                )

            total_bytes += child_info.st_size
            total_files += 1

    return total_bytes, total_files


def _copy_regular_file(
    source: Path,
    destination: Path,
    expected_info: os.stat_result,
    *,
    state: dict[str, int],
    total_bytes: int,
    total_files: int,
    progress_callback: ProgressCallback | None,
    cancel_event: Event | None,
) -> None:
    """Copy a regular file in cancellable chunks."""

    _check_cancel(cancel_event)

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)

    source_fd = os.open(source, flags)
    destination_fd: int | None = None

    try:
        actual_info = os.fstat(source_fd)

        if not stat.S_ISREG(actual_info.st_mode):
            raise FileOperationError(
                "Source is no longer a regular file."
            )

        if (
            actual_info.st_dev != expected_info.st_dev
            or actual_info.st_ino != expected_info.st_ino
        ):
            raise FileOperationError(
                "Source changed during copying."
            )

        destination_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )

        destination_fd = os.open(
            destination,
            destination_flags,
            0o600,
        )

        while True:
            _check_cancel(cancel_event)

            chunk = os.read(source_fd, 1024 * 1024)

            if not chunk:
                break

            view = memoryview(chunk)

            while view:
                _check_cancel(cancel_event)

                written = os.write(destination_fd, view)

                if written <= 0:
                    raise OSError("Unable to write destination file.")

                state["bytes_completed"] += written
                view = view[written:]

            _report(
                progress_callback,
                phase="copying",
                path=source,
                bytes_completed=state["bytes_completed"],
                total_bytes=total_bytes,
                files_completed=state["files_completed"],
                total_files=total_files,
            )

        _check_cancel(cancel_event)

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
        pass

    state["files_completed"] += 1

    _report(
        progress_callback,
        phase="copying",
        path=source,
        bytes_completed=state["bytes_completed"],
        total_bytes=total_bytes,
        files_completed=state["files_completed"],
        total_files=total_files,
    )


def _copy_node(
    source: Path,
    destination: Path,
    *,
    state: dict[str, int],
    total_bytes: int,
    total_files: int,
    progress_callback: ProgressCallback | None,
    cancel_event: Event | None,
) -> None:
    """Recursively copy regular files and directories."""

    _check_cancel(cancel_event)

    info = os.lstat(source)

    if stat.S_ISLNK(info.st_mode):
        raise FileOperationError(
            f"Symbolic links cannot be copied: {source}"
        )

    if stat.S_ISREG(info.st_mode):
        _copy_regular_file(
            source,
            destination,
            info,
            state=state,
            total_bytes=total_bytes,
            total_files=total_files,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
        return

    if not stat.S_ISDIR(info.st_mode):
        raise FileOperationError(
            f"Unsupported filesystem object: {source}"
        )

    destination.mkdir(mode=0o700)

    with os.scandir(source) as entries:
        for entry in entries:
            _check_cancel(cancel_event)

            child_source = source / entry.name
            child_destination = destination / entry.name

            _copy_node(
                child_source,
                child_destination,
                state=state,
                total_bytes=total_bytes,
                total_files=total_files,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
            )

    _check_cancel(cancel_event)

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
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> Path:
    """
    Copy a file or directory to a new destination.

    Reports byte and file progress when a callback is supplied.

    Existing destinations are never intentionally overwritten.
    Incomplete copies are cleaned up when cancellation occurs.
    """

    _check_cancel(cancel_event)

    resolved_root = root.expanduser().resolve(strict=True)

    source_path, _ = _inspect_member(
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

    source_info = os.lstat(source_path)

    if stat.S_ISDIR(source_info.st_mode):
        if destination_path.is_relative_to(source_path):
            raise FileOperationError(
                "A directory cannot be copied into itself."
            )

    total_bytes, total_files = _scan_source(
        source_path,
        cancel_event,
    )

    _check_cancel(cancel_event)

    _report(
        progress_callback,
        phase="preparing",
        path=source_path,
        bytes_completed=0,
        total_bytes=total_bytes,
        files_completed=0,
        total_files=total_files,
    )

    staging_directory = Path(
        tempfile.mkdtemp(
            prefix=".potato-copy-",
            dir=destination_path.parent,
        )
    )

    staged_item = staging_directory / destination_path.name

    state = {
        "bytes_completed": 0,
        "files_completed": 0,
    }

    try:
        _copy_node(
            source_path,
            staged_item,
            state=state,
            total_bytes=total_bytes,
            total_files=total_files,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )

        _check_cancel(cancel_event)

        # Check again before publishing the completed copy.
        if os.path.lexists(destination_path):
            raise FileExistsError(
                f"Destination already exists: {destination_path}"
            )

        os.rename(staged_item, destination_path)

        _report(
            progress_callback,
            phase="completed",
            path=destination_path,
            bytes_completed=state["bytes_completed"],
            total_bytes=total_bytes,
            files_completed=state["files_completed"],
            total_files=total_files,
        )

        return destination_path

    finally:
        shutil.rmtree(staging_directory, ignore_errors=True)


def _move_path(
    source: Path,
    destination: Path,
    root: Path,
    *,
    operation: str,
    progress_callback: ProgressCallback | None,
    cancel_event: Event | None,
) -> Path:
    """Perform a same-filesystem rename-based move or rename."""

    _check_cancel(cancel_event)

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

    _report(
        progress_callback,
        phase=operation,
        path=source_path,
        bytes_completed=0,
        total_bytes=source_info.st_size,
        files_completed=0,
        total_files=1,
    )

    _check_cancel(cancel_event)

    try:
        os.rename(source_path, destination_path)

    except OSError as error:
        if error.errno == errno.EXDEV:
            raise FileOperationError(
                "Cross-filesystem moves are not supported yet."
            ) from error

        raise

    _report(
        progress_callback,
        phase="completed",
        path=destination_path,
        bytes_completed=source_info.st_size,
        total_bytes=source_info.st_size,
        files_completed=1,
        total_files=1,
    )

    return destination_path


def move_path(
    source: Path,
    destination: Path,
    root: Path,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> Path:
    """Move a file or directory within the browsing root."""

    return _move_path(
        source,
        destination,
        root,
        operation="moving",
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )


def rename_path(
    source: Path,
    new_name: str,
    root: Path,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
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

    return _move_path(
        source_path,
        destination,
        root,
        operation="renaming",
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
