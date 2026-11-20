from __future__ import annotations

import stat
from pathlib import Path

import pytest

from potato.security import format_bytes, inspect_file


def test_format_bytes_preserves_exact_byte_count() -> None:
    assert format_bytes(1234) == "1,234 bytes (1.21 KiB)"
    assert format_bytes(1) == "1 byte"
    assert format_bytes(0) == "0 bytes"


def test_format_bytes_rejects_negative_values() -> None:
    with pytest.raises(ValueError):
        format_bytes(-1)


def test_inspect_file_reports_exact_logical_size(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.bin"
    target.write_bytes(b"x" * 1234)

    metadata = inspect_file(target, tmp_path)

    assert metadata.logical_size_bytes == 1234


def test_inspect_file_reports_allocated_size_when_available(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.bin"
    target.write_bytes(b"x" * 4096)

    metadata = inspect_file(target, tmp_path)
    info = target.stat()

    if hasattr(info, "st_blocks"):
        assert metadata.allocated_size_bytes == info.st_blocks * 512
    else:
        assert metadata.allocated_size_bytes is None


def test_inspect_file_reports_octal_and_symbolic_permissions(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("permission test", encoding="utf-8")
    target.chmod(0o640)

    metadata = inspect_file(target, tmp_path)

    assert metadata.mode_octal == "0640"
    assert metadata.mode_symbolic == stat.filemode(target.stat().st_mode)


def test_inspect_file_reports_owner_and_group_ids(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("ownership test", encoding="utf-8")

    metadata = inspect_file(target, tmp_path)
    info = target.stat()

    assert metadata.owner_uid == info.st_uid
    assert metadata.group_gid == info.st_gid
    assert metadata.owner_name
    assert metadata.group_name


def test_permission_inspection_does_not_change_permissions(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("read-only inspection", encoding="utf-8")
    target.chmod(0o640)

    before = stat.S_IMODE(target.stat().st_mode)

    inspect_file(target, tmp_path)

    after = stat.S_IMODE(target.stat().st_mode)

    assert before == after == 0o640


def test_inspect_file_reports_effective_access_as_booleans(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("access test", encoding="utf-8")

    metadata = inspect_file(target, tmp_path)

    assert isinstance(metadata.can_read, bool)
    assert isinstance(metadata.can_write, bool)
    assert isinstance(metadata.can_execute, bool)


def test_inspect_file_rejects_paths_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside.txt"

    root.mkdir()
    outside.write_text("private", encoding="utf-8")

    with pytest.raises(PermissionError):
        inspect_file(outside, root)


def test_inspect_file_rejects_symbolic_links(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")

    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="Symbolic links"):
        inspect_file(link, tmp_path)


def test_directory_size_is_entry_size_not_recursive_total(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()

    (folder / "large.txt").write_bytes(b"x" * 10000)

    metadata = inspect_file(folder, tmp_path)

    assert metadata.logical_size_bytes == folder.stat().st_size
    assert metadata.logical_size_bytes != 10000
