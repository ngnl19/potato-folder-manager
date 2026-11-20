from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

from potato.operations import (
    OperationCancelled,
    OperationProgress,
    copy_path,
    move_path,
    rename_path,
)


def test_copy_reports_byte_progress(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    source = root / "large.bin"
    destination = root / "copy.bin"

    source.write_bytes(b"x" * (2 * 1024 * 1024))

    updates: list[OperationProgress] = []

    copy_path(
        source,
        destination,
        root,
        progress_callback=updates.append,
    )

    assert updates

    assert any(
        update.phase == "preparing"
        for update in updates
    )

    assert any(
        update.phase == "copying"
        for update in updates
    )

    completed = updates[-1]

    assert completed.phase == "completed"
    assert completed.bytes_completed == source.stat().st_size
    assert completed.files_completed == 1
    assert completed.percentage == 100.0


def test_copy_reports_directory_progress(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    source = root / "source"

    nested = source / "Documents"
    nested.mkdir(parents=True)

    (source / "first.bin").write_bytes(b"a" * 1024)
    (nested / "second.bin").write_bytes(b"b" * 2048)

    updates: list[OperationProgress] = []

    copy_path(
        source,
        root / "backup",
        root,
        progress_callback=updates.append,
    )

    completed = updates[-1]

    assert completed.phase == "completed"
    assert completed.total_bytes == 3072
    assert completed.bytes_completed == 3072
    assert completed.total_files == 2
    assert completed.files_completed == 2

    assert (
        root / "backup" / "Documents" / "second.bin"
    ).exists()


def test_copy_can_be_cancelled_before_start(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    source = root / "source.bin"
    source.write_bytes(b"x" * 1000)

    destination = root / "copy.bin"

    cancel_event = Event()
    cancel_event.set()

    with pytest.raises(OperationCancelled):
        copy_path(
            source,
            destination,
            root,
            cancel_event=cancel_event,
        )

    assert not destination.exists()


def test_copy_cancellation_cleans_incomplete_destination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    source = root / "large.bin"
    source.write_bytes(b"x" * (4 * 1024 * 1024))

    destination = root / "copy.bin"

    cancel_event = Event()

    def cancel_after_first_chunk(
        progress: OperationProgress,
    ) -> None:
        if (
            progress.phase == "copying"
            and progress.bytes_completed > 0
        ):
            cancel_event.set()

    with pytest.raises(OperationCancelled):
        copy_path(
            source,
            destination,
            root,
            cancel_event=cancel_event,
            progress_callback=cancel_after_first_chunk,
        )

    assert source.exists()
    assert not destination.exists()

    assert not any(
        item.name.startswith(".potato-copy-")
        for item in root.iterdir()
    )


def test_move_reports_completion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    source = root / "source.txt"
    destination = root / "destination.txt"

    source.write_text("move test", encoding="utf-8")

    updates: list[OperationProgress] = []

    move_path(
        source,
        destination,
        root,
        progress_callback=updates.append,
    )

    assert updates[0].phase == "moving"
    assert updates[-1].phase == "completed"
    assert destination.exists()
    assert not source.exists()


def test_rename_reports_completion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    source = root / "old.txt"
    source.write_text("rename test", encoding="utf-8")

    updates: list[OperationProgress] = []

    rename_path(
        source,
        "new.txt",
        root,
        progress_callback=updates.append,
    )

    assert updates[0].phase == "renaming"
    assert updates[-1].phase == "completed"
    assert (root / "new.txt").exists()
    assert not source.exists()


def test_cancelled_move_does_not_change_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    source = root / "source.txt"
    destination = root / "destination.txt"

    source.write_text("test", encoding="utf-8")

    cancel_event = Event()
    cancel_event.set()

    with pytest.raises(OperationCancelled):
        move_path(
            source,
            destination,
            root,
            cancel_event=cancel_event,
        )

    assert source.exists()
    assert not destination.exists()


def test_progress_percentage_handles_zero_byte_files() -> None:
    progress = OperationProgress(
        phase="copying",
        current_path=Path("empty.txt"),
        bytes_completed=0,
        total_bytes=0,
        files_completed=0,
        total_files=1,
    )

    assert progress.percentage == 0.0
