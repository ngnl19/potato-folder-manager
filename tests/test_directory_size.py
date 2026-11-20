from pathlib import Path
from threading import Event

import pytest

from potato.security import calculate_directory_size


def test_directory_size_includes_nested_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    nested = root / "Documents" / "Projects"

    nested.mkdir(parents=True)

    (root / "first.bin").write_bytes(b"a" * 1024)
    (nested / "second.bin").write_bytes(b"b" * 2048)

    result = calculate_directory_size(root, root)

    assert result.logical_size_bytes == 3072
    assert result.file_count == 2
    assert result.directory_count == 2
    assert result.errors == 0


def test_directory_size_includes_hidden_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    hidden_directory = root / ".hidden"

    hidden_directory.mkdir(parents=True)

    (root / ".secret.txt").write_bytes(b"a" * 100)
    (hidden_directory / "secret.bin").write_bytes(b"b" * 200)

    result = calculate_directory_size(root, root)

    assert result.logical_size_bytes == 300
    assert result.file_count == 2
    assert result.directory_count == 1


def test_directory_size_skips_symbolic_links(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"

    root.mkdir()
    outside.mkdir()

    secret_file = outside / "secret.bin"
    secret_file.write_bytes(b"x" * 5000)

    secret_directory = outside / "secret_directory"
    secret_directory.mkdir()

    (secret_directory / "hidden.bin").write_bytes(b"x" * 5000)

    try:
        (root / "linked-file").symlink_to(secret_file)
        (root / "linked-directory").symlink_to(
            secret_directory,
            target_is_directory=True,
        )
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable.")

    result = calculate_directory_size(root, root)

    assert result.logical_size_bytes == 0
    assert result.file_count == 0
    assert result.directory_count == 0


def test_directory_size_supports_cancellation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    (root / "file.bin").write_bytes(b"x" * 1000)

    cancel_event = Event()
    cancel_event.set()

    result = calculate_directory_size(
        root,
        root,
        cancel_event=cancel_event,
    )

    assert result.logical_size_bytes == 0
    assert result.file_count == 0


def test_directory_size_rejects_paths_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"

    root.mkdir()
    outside.mkdir()

    (outside / "file.bin").write_bytes(b"private")

    with pytest.raises(PermissionError):
        calculate_directory_size(outside, root)


def test_directory_size_reports_allocated_bytes_when_available(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    target = root / "sample.bin"
    target.write_bytes(b"x" * 4096)

    result = calculate_directory_size(root, root)

    info = target.stat()

    if hasattr(info, "st_blocks"):
        assert result.allocated_size_bytes == info.st_blocks * 512
    else:
        assert result.allocated_size_bytes is None
