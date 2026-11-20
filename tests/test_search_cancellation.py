from pathlib import Path
from threading import Event

from potato.security import search_files


def test_search_stops_when_cancelled(tmp_path: Path) -> None:
    for number in range(10):
        (tmp_path / f"potato_{number}.txt").write_text("test")

    cancel_event = Event()
    cancel_event.set()

    results = search_files(
        tmp_path,
        tmp_path,
        "potato",
        cancel_event=cancel_event,
    )

    assert results == []
