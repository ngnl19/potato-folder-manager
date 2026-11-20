from __future__ import annotations

from pathlib import Path

import pytest

from potato.destination_picker import (
    list_child_directories,
    resolve_destination_path,
)


def test_picker_lists_only_child_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    (root / "Documents").mkdir()
    (root / "Pictures").mkdir()
    (root / "notes.txt").write_text("test")

    result = list_child_directories(root, root)

    assert [path.name for path in result] == [
        "Documents",
        "Pictures",
    ]


def test_picker_sorts_directories_alphabetically(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    (root / "Zebra").mkdir()
    (root / "apple").mkdir()
    (root / "Banana").mkdir()

    result = list_child_directories(root, root)

    assert [path.name for path in result] == [
        "apple",
        "Banana",
        "Zebra",
    ]


def test_picker_skips_symbolic_link_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"

    root.mkdir()
    outside.mkdir()

    try:
        (root / "linked-folder").symlink_to(
            outside,
            target_is_directory=True,
        )
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable.")

    result = list_child_directories(root, root)

    assert all(
        path.name != "linked-folder"
        for path in result
    )


def test_picker_rejects_directory_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"

    root.mkdir()
    outside.mkdir()

    with pytest.raises(PermissionError):
        list_child_directories(outside, root)


def test_picker_includes_hidden_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    (root / ".config").mkdir()
    (root / "Documents").mkdir()

    result = list_child_directories(root, root)

    names = [path.name for path in result]

    assert ".config" in names
    assert "Documents" in names


def test_manual_path_accepts_absolute_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    destination = root / "Documents"

    destination.mkdir(parents=True)

    result = resolve_destination_path(
        str(destination),
        root,
        root,
    )

    assert result == destination


def test_manual_path_accepts_relative_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    current = root / "Downloads"
    destination = root / "Documents"

    current.mkdir(parents=True)
    destination.mkdir()

    result = resolve_destination_path(
        "../Documents",
        current,
        root,
    )

    assert result == destination


def test_manual_path_rejects_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"

    root.mkdir()
    outside.mkdir()

    with pytest.raises(PermissionError):
        resolve_destination_path(
            str(outside),
            root,
            root,
        )


def test_manual_path_rejects_symbolic_link(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"

    root.mkdir()
    outside.mkdir()

    try:
        (root / "linked").symlink_to(
            outside,
            target_is_directory=True,
        )
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable.")

    with pytest.raises(PermissionError):
        resolve_destination_path(
            "linked",
            root,
            root,
        )


def test_manual_path_rejects_empty_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError):
        resolve_destination_path(
            "",
            root,
            root,
        )
