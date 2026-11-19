
from pathlib import Path

import pytest

from potato.security import (
    FileEntry,
    list_directory,
    search_files,
    sort_entries,
)


def test_recursive_search_finds_nested_files(tmp_path: Path) -> None:
    root = tmp_path

    nested = root / "Documents" / "Projects"
    nested.mkdir(parents=True)

    target = nested / "potato_config.py"
    target.write_text("name = 'potato'")

    results = search_files(root, root, "potato")

    assert len(results) == 1
    assert results[0].name == "potato_config.py"


def test_search_is_case_insensitive(tmp_path: Path) -> None:
    target = tmp_path / "Potato_README.md"
    target.write_text("test")

    results = search_files(tmp_path, tmp_path, "potato")

    assert len(results) == 1
    assert results[0].name == "Potato_README.md"


def test_hidden_files_are_excluded_by_default(
    tmp_path: Path,
) -> None:
    visible = tmp_path / "visible.txt"
    hidden = tmp_path / ".secret.txt"

    visible.write_text("visible")
    hidden.write_text("hidden")

    results = list_directory(tmp_path, tmp_path)

    names = [entry.name for entry in results]

    assert "visible.txt" in names
    assert ".secret.txt" not in names


def test_hidden_files_can_be_included(tmp_path: Path) -> None:
    hidden = tmp_path / ".secret.txt"
    hidden.write_text("hidden")

    results = list_directory(
        tmp_path,
        tmp_path,
        include_hidden=True,
    )

    assert any(entry.name == ".secret.txt" for entry in results)


def test_search_can_include_hidden_files(tmp_path: Path) -> None:
    hidden = tmp_path / ".potato_secret.txt"
    hidden.write_text("secret")

    results = search_files(
        tmp_path,
        tmp_path,
        "potato",
        include_hidden=True,
    )

    assert any(entry.name == ".potato_secret.txt" for entry in results)


def test_search_respects_result_limit(tmp_path: Path) -> None:
    for number in range(5):
        (tmp_path / f"potato_{number}.txt").write_text("test")

    results = search_files(
        tmp_path,
        tmp_path,
        "potato",
        max_results=2,
    )

    assert len(results) == 2



def test_search_respects_depth_limit(tmp_path: Path) -> None:
    nested = tmp_path / "level1" / "level2"
    nested.mkdir(parents=True)

    (tmp_path / "level1" / "potato.txt").write_text("test")
    (nested / "potato_deep.txt").write_text("test")

    results = search_files(
        tmp_path,
        tmp_path,
        "potato",
        max_depth=1,
    )

    names = [entry.name for entry in results]

    assert "potato.txt" in names
    assert "potato_deep.txt" not in names
    assert len(results) == 1


def test_directory_outside_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"

    root.mkdir()
    outside.mkdir()

    with pytest.raises(PermissionError):
        list_directory(outside, root)


def test_symbolic_links_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"

    root.mkdir()
    outside.mkdir()

    secret = outside / "secret.txt"
    secret.write_text("private data")

    link = root / "secret_link"
    link.symlink_to(secret)

    results = list_directory(root, root)

    assert all(entry.name != "secret_link" for entry in results)


def test_sort_entries_by_size() -> None:
    large = FileEntry(
        name="large.txt",
        path=Path("large.txt"),
        is_dir=False,
        size=1000,
        modified=100,
    )

    small = FileEntry(
        name="small.txt",
        path=Path("small.txt"),
        is_dir=False,
        size=100,
        modified=200,
    )

    results = sort_entries(
        [large, small],
        sort_by="size",
    )

    assert results[0].name == "small.txt"


def test_invalid_sort_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        sort_entries([], sort_by="invalid")
