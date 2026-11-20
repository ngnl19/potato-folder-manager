from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from potato.security import canonical_root


@dataclass(frozen=True)
class ManualPathRequest:
    """Compatibility marker for manual destination requests."""


def resolve_destination_path(
    value: str,
    current: Path,
    root: Path,
) -> Path:
    """
    Resolve and validate a manually entered destination.

    Relative paths are resolved from the currently displayed folder.
    Symbolic links and paths outside the configured root are rejected.
    """

    if not value.strip():
        raise ValueError("Destination path cannot be empty.")

    resolved_root = canonical_root(root)

    candidate = Path(value).expanduser()

    if not candidate.is_absolute():
        candidate = current / candidate

    # Normalize the lexical path without following symbolic links.
    candidate = Path(os.path.abspath(candidate))

    if not candidate.is_relative_to(resolved_root):
        raise PermissionError(
            "Destination is outside the configured browsing root."
        )

    # Inspect every path component without following symbolic links.
    component = resolved_root
    relative_parts = candidate.relative_to(resolved_root).parts

    for part in relative_parts:
        component = component / part

        info = os.lstat(component)

        if stat.S_ISLNK(info.st_mode):
            raise PermissionError(
                f"Symbolic links are not allowed in destination paths: "
                f"{component}"
            )

        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(component)

    return candidate


def list_child_directories(
    directory: Path,
    root: Path,
) -> list[Path]:
    """List child directories without following symbolic links."""

    resolved_root = canonical_root(root)
    current = directory.resolve(strict=True)

    if not current.is_relative_to(resolved_root):
        raise PermissionError(
            "Destination browsing outside the root is blocked."
        )

    if not current.is_dir():
        raise NotADirectoryError(current)

    directories: list[Path] = []

    with os.scandir(current) as items:
        for item in items:
            try:
                if item.is_symlink():
                    continue

                if not item.is_dir(follow_symlinks=False):
                    continue

                path = Path(item.path)
                resolved = path.resolve(strict=True)

                if resolved.is_relative_to(resolved_root):
                    directories.append(resolved)

            except (OSError, RuntimeError):
                continue

    return sorted(
        directories,
        key=lambda path: path.name.casefold(),
    )


class DestinationItem(ListItem):
    """Selectable destination directory."""

    def __init__(self, path: Path) -> None:
        self.path = path

        super().__init__(
            Static(Text(f"[DIR] {path.name}"))
        )


class DirectoryPicker(ModalScreen[Path | ManualPathRequest | None]):
    """Keyboard-only destination folder picker."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("backspace", "go_parent", "Parent"),
        ("c", "choose_current", "Choose folder"),
        ("space", "choose_current", "Choose folder"),
        ("t", "toggle_manual_path", "Type path"),
        ("f4", "toggle_manual_path", "Type path"),
        ("q", "cancel", "Cancel"),
    ]

    CSS = """
    DirectoryPicker {
        align: center middle;
    }

    #picker-dialog {
        width: 85%;
        max-width: 100;
        height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }

    #picker-title {
        height: 2;
        text-style: bold;
    }

    #picker-location {
        height: 2;
        color: $text-muted;
    }

    #manual-path {
        display: none;
        height: 3;
        margin-bottom: 1;
    }

    #destination-list {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }

    #picker-status {
        height: 2;
        color: $text-muted;
    }

    #picker-help {
        height: 4;
        padding: 1 0;
        background: $panel;
    }
    """

    def __init__(
        self,
        root: Path,
        start_directory: Path,
    ) -> None:
        super().__init__()

        self.root = canonical_root(root)
        self.current = start_directory.resolve(strict=True)

        if not self.current.is_relative_to(self.root):
            raise PermissionError(
                "Starting directory is outside the root."
            )

        if not self.current.is_dir():
            raise NotADirectoryError(self.current)

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static(
                "SELECT DESTINATION FOLDER",
                id="picker-title",
            )

            yield Static(
                str(self.current),
                id="picker-location",
            )

            yield Input(
                placeholder=(
                    "Enter absolute or relative destination path..."
                ),
                id="manual-path",
            )

            yield ListView(id="destination-list")

            yield Static(
                "",
                id="picker-status",
            )

            yield Static(
                "↑↓ Navigate | Enter Open | Backspace Parent\n"
                "C / Space Choose folder | T / F4 Enter path\n"
                "Enter Submit path | Esc Cancel",
                id="picker-help",
            )

    async def on_mount(self) -> None:
        await self.refresh_directories()

        self.query_one(
            "#destination-list",
            ListView,
        ).focus()

    async def refresh_directories(self) -> None:
        """Refresh the child directory listing."""

        self.query_one("#picker-location", Static).update(
            Text(str(self.current))
        )

        directory_list = self.query_one(
            "#destination-list",
            ListView,
        )

        await directory_list.clear()

        try:
            directories = list_child_directories(
                self.current,
                self.root,
            )

        except (OSError, RuntimeError, ValueError) as error:
            self.query_one("#picker-status", Static).update(
                Text(f"Unable to browse folder: {error}")
            )
            return

        if not directories:
            self.query_one("#picker-status", Static).update(
                Text(
                    "No subdirectories. Press C to choose this folder."
                )
            )
            return

        await directory_list.extend(
            DestinationItem(path)
            for path in directories
        )

        directory_list.index = 0

        self.query_one("#picker-status", Static).update(
            Text(
                f"{len(directories)} folder(s). "
                "Press C to choose this folder."
            )
        )

    async def on_list_view_selected(
        self,
        event: ListView.Selected,
    ) -> None:
        """Open the highlighted destination folder."""

        if not isinstance(event.item, DestinationItem):
            return

        event.stop()

        try:
            resolved = event.item.path.resolve(strict=True)

            if not resolved.is_relative_to(self.root):
                raise PermissionError(
                    "Access outside root is blocked."
                )

            if not resolved.is_dir():
                raise NotADirectoryError(resolved)

            self.current = resolved

            await self.refresh_directories()

        except (OSError, RuntimeError, ValueError) as error:
            self.app.notify(
                f"Unable to open folder: {error}",
                severity="error",
            )

    async def action_go_parent(self) -> None:
        """Navigate to the parent without leaving the root."""

        if self.current == self.root:
            return

        parent = self.current.parent

        if parent.is_relative_to(self.root):
            self.current = parent
            await self.refresh_directories()

    def action_choose_current(self) -> None:
        """Choose the currently displayed directory."""

        manual_input = self.query_one("#manual-path", Input)

        if manual_input.styles.display != "none":
            return

        self.dismiss(self.current)

    def action_toggle_manual_path(self) -> None:
        """Show or hide the manual path input."""

        manual_input = self.query_one("#manual-path", Input)

        if manual_input.styles.display == "none":
            manual_input.value = ""
            manual_input.styles.display = "block"
            manual_input.focus()

        else:
            manual_input.styles.display = "none"
            manual_input.value = ""

            self.query_one(
                "#destination-list",
                ListView,
            ).focus()

    async def on_input_submitted(
        self,
        event: Input.Submitted,
    ) -> None:
        """Validate and select the manually entered destination."""

        if event.input.id != "manual-path":
            return

        try:
            destination = resolve_destination_path(
                event.value,
                self.current,
                self.root,
            )

            if not destination.is_dir():
                raise NotADirectoryError(destination)

            self.dismiss(destination)

        except (OSError, RuntimeError, ValueError) as error:
            self.query_one("#picker-status", Static).update(
                Text(f"Invalid destination: {error}")
            )

            event.input.focus()

    def action_cancel(self) -> None:
        """Cancel destination selection."""

        self.dismiss(None)
