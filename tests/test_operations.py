from __future__ import annotations

import stat
from pathlib import Path

import pytest

from potato.operations import (
    FileOperationError,
    copy_path,
    move_path,
    rename_path,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Create an isolated filesystem root for each test."""

    directory = tmp_path / "allowed"
    directory.mkdir()

    return directory


def test_copy_file_preserves_contents_and_permissions(
    root: Path,
) -> None:
    source = root / "original.txt"
    destination = root / "copy.txt"

    source.write_bytes(b"Potato file operations")
    source.chmod(0o640)

    result = copy_path(source, destination, root)

    assert result == destination
    assert destination.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert source.exists()


def test_copy_directory_includes_nested_and_hidden_files(
    root: Path,
) -> None:
    source = root / "original"
    nested = source / "Documents" / "Projects"

    nested.mkdir(parents=True)

    (source / "readme.txt").write_text("hello", encoding="utf-8")
    (source / ".hidden").write_text("hidden", encoding="utf-8")
    (nested / "main.py").write_text(
        "print('Potato')",
        encoding="utf-8",
    )

    destination = root / "backup"

    copy_path(source, destination, root)

    assert (
        destination / "readme.txt"
    ).read_text(encoding="utf-8") == "hello"

    assert (
        destination / ".hidden"
    ).read_text(encoding="utf-8") == "hidden"

    assert (
        destination / "Documents" / "Projects" / "main.py"
    ).read_text(encoding="utf-8") == "print('Potato')"

    assert source.exists()


def test_copy_refuses_to_overwrite_existing_destination(
    root: Path,
) -> None:
    source = root / "source.txt"
    destination = root / "destination.txt"

    source.write_text("new content", encoding="utf-8")
    destination.write_text("existing content", encoding="utf-8")

    with pytest.raises(FileExistsError):
        copy_path(source, destination, root)

    assert destination.read_text(encoding="utf-8") == "existing content"


def test_copy_rejects_symbolic_links(
    root: Path,
) -> None:
    target = root / "target.txt"
    link = root / "link.txt"

    target.write_text("target", encoding="utf-8")
    link.symlink_to(target)

    with pytest.raises(FileOperationError):
        copy_path(link, root / "copy.txt", root)


def test_copy_rejects_nested_symbolic_links_and_cleans_up(
    root: Path,
) -> None:
    source = root / "source"
    source.mkdir()

    target = root / "target.txt"
    target.write_text("target", encoding="utf-8")

    (source / "link.txt").symlink_to(target)

    destination = root / "backup"

    with pytest.raises(FileOperationError):
        copy_path(source, destination, root)

    assert not destination.exists()

    assert not any(
        item.name.startswith(".potato-copy-")
        for item in root.iterdir()
    )


def test_copy_rejects_source_outside_root(
    root: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")

    with pytest.raises(PermissionError):
        copy_path(outside, root / "copy.txt", root)


def test_copy_rejects_destination_outside_root(
    root: Path,
    tmp_path: Path,
) -> None:
    source = root / "source.txt"
    source.write_text("test", encoding="utf-8")

    outside = tmp_path / "outside.txt"

    with pytest.raises(PermissionError):
        copy_path(source, outside, root)


def test_copy_rejects_copying_directory_into_itself(
    root: Path,
) -> None:
    source = root / "folder"
    source.mkdir()

    with pytest.raises(FileOperationError):
        copy_path(
            source,
            source / "backup",
            root,
        )


def test_move_file_preserves_contents(
    root: Path,
) -> None:
    source = root / "source.txt"
    destination = root / "Documents" / "moved.txt"

    destination.parent.mkdir()

    source.write_text("move test", encoding="utf-8")

    result = move_path(source, destination, root)

    assert result == destination
    assert destination.read_text(encoding="utf-8") == "move test"
    assert not source.exists()


def test_move_refuses_to_overwrite_existing_destination(
    root: Path,
) -> None:
    source = root / "source.txt"
    destination = root / "destination.txt"

    source.write_text("source", encoding="utf-8")
    destination.write_text("destination", encoding="utf-8")

    with pytest.raises(FileExistsError):
        move_path(source, destination, root)

    assert source.exists()
    assert destination.read_text(encoding="utf-8") == "destination"


def test_move_rejects_moving_directory_into_itself(
    root: Path,
) -> None:
    source = root / "folder"
    source.mkdir()

    with pytest.raises(FileOperationError):
        move_path(
            source,
            source / "nested",
            root,
        )


def test_rename_file(
    root: Path,
) -> None:
    source = root / "old-name.txt"
    destination = root / "new-name.txt"

    source.write_text("rename test", encoding="utf-8")

    result = rename_path(
        source,
        "new-name.txt",
        root,
    )

    assert result == destination
    assert destination.read_text(encoding="utf-8") == "rename test"
    assert not source.exists()


def test_rename_rejects_invalid_names(
    root: Path,
) -> None:
    source = root / "original.txt"
    source.write_text("test", encoding="utf-8")

    with pytest.raises(ValueError):
        rename_path(source, "../outside.txt", root)

    assert source.exists()


def test_rename_refuses_to_overwrite_existing_file(
    root: Path,
) -> None:
    source = root / "original.txt"
    destination = root / "existing.txt"

    source.write_text("original", encoding="utf-8")
    destination.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        rename_path(source, "existing.txt", root)

    assert source.exists()
    assert destination.read_text(encoding="utf-8") == "existing"


def test_move_rejects_symbolic_links(
    root: Path,
) -> None:
    target = root / "target.txt"
    link = root / "link.txt"

    target.write_text("target", encoding="utf-8")
    link.symlink_to(target)

    with pytest.raises(FileOperationError):
        move_path(link, root / "moved.txt", root)


def test_operations_reject_destination_inside_symlink_parent(
    root: Path,
    tmp_path: Path,
) -> None:
    source = root / "source.txt"
    source.write_text("test", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()

    link = root / "linked-directory"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileOperationError):
        copy_path(
            source,
            link / "copy.txt",
            root,
        )
