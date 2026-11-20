from pathlib import Path

from potato.app import (
    BrowserItem,
    PotatoApp,
    normalize_selected_entries,
)
from potato.security import FileEntry


def make_entry(path: Path, is_dir: bool) -> FileEntry:
    """Create an entry for selection tests."""

    return FileEntry(
        name=path.name,
        path=path,
        is_dir=is_dir,
        size=0,
        modified=0.0,
    )


def test_space_key_is_bound_to_selection() -> None:
    bindings = {
        binding[0]: binding[1]
        for binding in PotatoApp.BINDINGS
    }

    assert bindings["space"] == "toggle_selection"
    assert callable(PotatoApp.action_toggle_selection)


def test_parent_directory_selection_excludes_child(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "Documents"
    child = folder / "report.txt"

    entries = [
        make_entry(folder, True),
        make_entry(child, False),
    ]

    result = normalize_selected_entries(entries)

    assert result == (folder,)


def test_selection_deduplicates_entries(
    tmp_path: Path,
) -> None:
    first = tmp_path / "report.txt"

    entries = [
        make_entry(first, False),
        make_entry(first, False),
    ]

    result = normalize_selected_entries(entries)

    assert result == (first,)


def test_independent_files_remain_selected(
    tmp_path: Path,
) -> None:
    first = tmp_path / "report.txt"
    second = tmp_path / "image.png"

    entries = [
        make_entry(first, False),
        make_entry(second, False),
    ]

    result = normalize_selected_entries(entries)

    assert result == (first, second)


def test_browser_item_has_selection_marker(
    tmp_path: Path,
) -> None:
    target = tmp_path / "report.txt"
    target.write_text("test", encoding="utf-8")

    entry = make_entry(target, False)

    item = BrowserItem(
        entry,
        tmp_path,
        selected=False,
    )

    assert item._selected is False
    assert "[ ]" in item._build_label().plain

    item.set_selected(True)

    assert item._selected is True
    assert "[x]" in item._build_label().plain

    item.set_selected(False)

    assert item._selected is False
    assert "[ ]" in item._build_label().plain


def test_f6_f7_and_f8_bindings_are_preserved() -> None:
    bindings = {
        binding[0]: binding[1]
        for binding in PotatoApp.BINDINGS
    }

    assert bindings["f6"] == "begin_copy"
    assert bindings["f7"] == "begin_move"
    assert bindings["f8"] == "begin_rename"
