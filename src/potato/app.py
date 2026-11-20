from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Input, ListItem, ListView, Static

from potato.destination_picker import (
    DirectoryPicker,
    ManualPathRequest,
)
from potato.operations import copy_path, move_path, rename_path
from potato.security import (
    DirectorySize,
    FileEntry,
    calculate_directory_size,
    canonical_root,
    format_bytes,
    inspect_file,
    is_within_root,
    list_directory,
    read_preview,
    search_files,
    sort_entries,
)


def copy_to_system_clipboard(text: str, app: App) -> str:
    """Copy text through an available clipboard provider."""

    providers: list[tuple[str, list[str], list[str]]] = []

    wl_copy = shutil.which("wl-copy")
    wl_paste = shutil.which("wl-paste")
    xclip = shutil.which("xclip")
    xsel = shutil.which("xsel")

    if wl_copy and wl_paste:
        providers.append(
            ("Wayland", [wl_copy], [wl_paste, "--no-newline"])
        )

    if xclip:
        providers.append(
            (
                "X11",
                [xclip, "-selection", "clipboard", "-in"],
                [xclip, "-selection", "clipboard", "-out"],
            )
        )

    if xsel:
        providers.append(
            (
                "X11",
                [xsel, "--clipboard", "--input"],
                [xsel, "--clipboard", "--output"],
            )
        )

    errors: list[str] = []

    for provider, copy_command, paste_command in providers:
        try:
            subprocess.run(
                copy_command,
                input=text,
                text=True,
                capture_output=True,
                timeout=5,
                check=True,
            )

            verification = subprocess.run(
                paste_command,
                text=True,
                capture_output=True,
                timeout=5,
                check=True,
            )

            if verification.stdout == text:
                return provider

            errors.append(
                f"{provider}: clipboard content did not match"
            )

        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{provider}: {error}")

    is_vte = bool(
        os.environ.get("VTE_VERSION")
        or os.environ.get("GNOME_TERMINAL_SERVICE")
    )

    if not is_vte and not providers:
        app.copy_to_clipboard(text)
        return "OSC52-unverified"

    if errors:
        raise RuntimeError(
            "Clipboard operation failed: " + "; ".join(errors)
        )

    raise RuntimeError(
        "No supported system clipboard provider was found. "
        "Use wl-copy on Wayland or xclip/xsel on X11."
    )


@dataclass(frozen=True)
class PendingOperation:
    """An operation waiting for explicit confirmation."""

    operation: str
    source: Path
    destination: Path


class SearchCompleted(Message):
    """Message carrying background search results."""

    def __init__(
        self,
        request_id: int,
        query: str,
        results: list[FileEntry],
        error: str | None = None,
    ) -> None:
        super().__init__()

        self.request_id = request_id
        self.query = query
        self.results = results
        self.error = error


class DirectorySizeCompleted(Message):
    """Message carrying a directory-size result."""

    def __init__(
        self,
        request_id: int,
        path: Path,
        result: DirectorySize | None,
        error: str | None = None,
    ) -> None:
        super().__init__()

        self.request_id = request_id
        self.path = path
        self.result = result
        self.error = error


class FileOperationCompleted(Message):
    """Message carrying the result of a file operation."""

    def __init__(
        self,
        request_id: int,
        operation: str,
        source: Path,
        destination: Path,
        error: str | None = None,
    ) -> None:
        super().__init__()

        self.request_id = request_id
        self.operation = operation
        self.source = source
        self.destination = destination
        self.error = error


class BrowserItem(ListItem):
    """A selectable filesystem entry."""

    def __init__(self, entry: FileEntry, root: Path) -> None:
        self.entry = entry

        icon = "[DIR] " if entry.is_dir else "[FILE] "

        try:
            relative = entry.path.relative_to(root)
            parent = str(relative.parent)

            label = (
                f"{icon}{entry.name}"
                if parent == "."
                else f"{icon}{entry.name}  ({parent})"
            )

        except ValueError:
            label = f"{icon}{entry.name}"

        super().__init__(Static(Text(label)))


class PreviewPane(VerticalScroll):
    """Keyboard-scrollable file preview panel."""

    can_focus = True


class PotatoApp(App):
    """Keyboard-first local filesystem browser."""

    TITLE = "Potato"
    ENABLE_COMMAND_PALETTE = False

    SORT_MODES = ("name", "size", "modified")

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("f1", "focus_search", "Search here"),
        ("f2", "global_search", "Search all"),
        ("escape", "cancel_search", "Cancel / Back"),
        ("backspace", "go_parent", "Parent"),
        ("f3", "toggle_hidden", "Hidden"),
        ("f4", "cycle_sort", "Sort"),
        ("f5", "copy_current_directory", "Copy path"),
        ("f6", "begin_copy", "Copy item"),
        ("f7", "begin_move", "Move item"),
        ("f8", "begin_rename", "Rename item"),
        # Alternative keys when a terminal captures F-keys.
        ("c", "begin_copy", "Copy item"),
        ("m", "begin_move", "Move item"),
        ("r", "begin_rename", "Rename item"),
        ("p", "copy_current_directory", "Copy path"),
    ]

    CSS = """
    Screen {
        layout: vertical;
        padding: 0 1;
    }

    #status {
        height: 2;
        padding: 0 1;
        background: $panel;
    }

    #search {
        display: none;
        height: 3;
        margin: 0 0 1 0;
    }

    #main {
        height: 1fr;
        layout: horizontal;
    }

    #file-list {
        width: 45%;
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }

    #preview-pane {
        width: 55%;
        height: 1fr;
        border: round $secondary;
        padding: 1;
        overflow-y: auto;
    }

    #preview-content {
        width: 100%;
        height: auto;
    }

    
    #help {
       width: 100%;
       height: 4;
       min-height: 4;
       background: $panel;
    }

    .help-group {
        width: 100%;
        min-width: 16;
        height: 5;
        padding: 0 1;
        border: round $primary;
    }
    """

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()

        self.root = canonical_root(root or Path.home())
        self.current = self.root

        self.include_hidden = False
        self.sort_by = "name"

        self._entries: list[FileEntry] = []

        # Search state.
        self._search_request_id = 0
        self._search_cancel_event: Event | None = None
        self.search_all = False
        self._last_search_query = ""
        self._last_search_base = self.current

        # Directory-size state.
        self._directory_size_request_id = 0
        self._directory_size_cancel_event: Event | None = None
        self._previewed_entry: FileEntry | None = None

        # File-operation state.
        self._input_mode: str | None = None
        self._pending_source: Path | None = None
        self._pending_operation: PendingOperation | None = None

        self._operation_request_id = 0
        self._operation_busy = False

    
    
    def compose(self) -> ComposeResult:
        yield Static("", id="status")

        yield Input(
            placeholder="Enter a filename or operation value.",
            id="search",
        )

        with Horizontal(id="main"):
            yield ListView(id="file-list")

            with PreviewPane(id="preview-pane"):
                yield Static(
                    "Select a file or folder to inspect it.",
                    id="preview-content",
                )

        yield Static(
            "NAV: ↑/↓ Select | Enter Open | Backspace Parent | Tab Preview\n"
            "SEARCH: F1 Here | F2 All | Esc Cancel\n"
            "FILES: C/F6 Copy | M/F7 Move | R/F8 Rename | P/F5 Copy path\n"
            "VIEW: F3 Hidden | F4 Sort | Ctrl+Q Quit",
            id="help",
        )

    async def on_mount(self) -> None:
        await self.refresh_directory()
        self.query_one("#file-list", ListView).focus()

    def update_status(self, detail: str = "") -> None:
        """Display current directory and browsing settings."""

        hidden = "ON" if self.include_hidden else "OFF"

        message = (
            f"POTATO | {self.current} | "
            f"Hidden: {hidden} | Sort: {self.sort_by}"
        )

        if detail:
            message += f" | {detail}"

        self.query_one("#status", Static).update(Text(message))

    async def refresh_directory(self) -> None:
        """Reload the current directory."""

        if not is_within_root(self.current, self.root):
            self.current = self.root

        try:
            self._entries = list_directory(
                self.current,
                self.root,
                include_hidden=self.include_hidden,
            )

            self._entries = sort_entries(
                self._entries,
                self.sort_by,
            )

        except (OSError, RuntimeError, ValueError) as error:
            self._entries = []

            self.query_one("#preview-content", Static).update(
                Text(f"Directory access failed:\n{error}")
            )
            return

        self.update_status()
        await self.render_entries(self._entries)

    async def render_entries(
        self,
        entries: list[FileEntry],
    ) -> None:
        """Render directory entries or search results."""

        file_list = self.query_one("#file-list", ListView)

        await file_list.clear()

        if not entries:
            self.query_one("#preview-content", Static).update(
                Text("No matching files or folders.")
            )
            return

        await file_list.extend(
            BrowserItem(entry, self.root)
            for entry in entries
        )

        file_list.index = 0
        self.update_preview(entries[0])

    # ---------------------------------------------------------
    # Search
    # ---------------------------------------------------------

    def _invalidate_search(self) -> None:
        """Stop and invalidate the previous search."""

        self._search_request_id += 1

        if self._search_cancel_event is not None:
            self._search_cancel_event.set()
            self._search_cancel_event = None

    def _start_search(
        self,
        query: str,
        search_base: Path,
    ) -> None:
        """Start a background search."""

        self._invalidate_search()

        request_id = self._search_request_id
        cancel_event = Event()

        self._search_cancel_event = cancel_event
        self._last_search_query = query
        self._last_search_base = search_base

        self.update_status(f"Searching: {search_base}")

        self.query_one("#preview-content", Static).update(
            Text(f"Searching for: {query}")
        )

        self.run_search_worker(
            query,
            search_base,
            self.root,
            self.include_hidden,
            self.sort_by,
            request_id,
            cancel_event,
        )

    def action_focus_search(self) -> None:
        """Search the current directory tree."""

        self.search_all = False
        self._input_mode = "search"

        search = self.query_one("#search", Input)
        search.placeholder = (
            "Search current directory and subdirectories; press Enter."
        )
        search.value = ""
        search.styles.display = "block"
        search.focus()

    def action_global_search(self) -> None:
        """Search the entire configured root."""

        self.search_all = True
        self._input_mode = "search"

        search = self.query_one("#search", Input)
        search.placeholder = (
            "Search the entire Potato root; press Enter."
        )
        search.value = ""
        search.styles.display = "block"
        search.focus()

    async def on_input_submitted(
        self,
        event: Input.Submitted,
    ) -> None:
        """Handle search, destination path, rename, and confirmation."""

        if event.input.id != "search":
            return

        value = event.value.strip()
        mode = self._input_mode

        if mode == "search":
            if not value:
                await self.action_cancel_search()
                return

            search_base = (
                self.root
                if self.search_all
                else self.current
            )

            event.input.styles.display = "none"
            self._input_mode = None

            self.query_one("#file-list", ListView).focus()
            self._start_search(value, search_base)
            return

        if mode in {"copy_path", "move_path"}:
            operation = mode.removesuffix("_path")

            try:
                destination_directory = Path(value).expanduser()

                if not destination_directory.is_absolute():
                    destination_directory = (
                        self.current / destination_directory
                    )

                destination_directory = (
                    destination_directory.resolve(strict=True)
                )

                self._set_pending_destination(
                    operation,
                    destination_directory,
                )

            except (OSError, RuntimeError, ValueError) as error:
                self.notify(
                    f"Invalid destination: {error}",
                    severity="error",
                    timeout=5,
                )

            return

        if mode == "rename_name":
            source = self._pending_source

            if source is None:
                await self.action_cancel_search()
                return

            if (
                not value
                or value in {".", ".."}
                or "/" in value
                or "\x00" in value
                or (os.altsep is not None and os.altsep in value)
            ):
                self.notify(
                    "Invalid filename.",
                    severity="error",
                )
                return

            self._pending_operation = PendingOperation(
                operation="rename",
                source=source,
                destination=source.parent / value,
            )

            self._show_operation_confirmation()
            return

        
        if mode == "confirm_operation":
            pending = self._pending_operation

            if pending is None:
                await self.action_cancel_search()
                return

            # Enter confirms immediately; no text is required.
            self._input_mode = None
            self._pending_source = None
            self._pending_operation = None

            event.input.value = ""
            event.input.styles.display = "none"

            self.query_one("#file-list", ListView).focus()

            self._start_file_operation(pending)
            return


    @work(
        thread=True,
        group="recursive-search",
        exit_on_error=False,
    )
    def run_search_worker(
        self,
        query: str,
        search_base: Path,
        root: Path,
        include_hidden: bool,
        sort_by: str,
        request_id: int,
        cancel_event: Event,
    ) -> None:
        """Search without blocking the interface."""

        try:
            results = search_files(
                search_base,
                root,
                query,
                include_hidden=include_hidden,
                cancel_event=cancel_event,
            )

            if cancel_event.is_set():
                return

            results = sort_entries(results, sort_by)

            message = SearchCompleted(
                request_id,
                query,
                results,
            )

        except Exception as error:
            if cancel_event.is_set():
                return

            message = SearchCompleted(
                request_id,
                query,
                [],
                str(error),
            )

        try:
            self.call_from_thread(
                self.post_message,
                message,
            )
        except RuntimeError:
            return

    async def on_search_completed(
        self,
        event: SearchCompleted,
    ) -> None:
        """Display only the latest search result."""

        if event.request_id != self._search_request_id:
            return

        self._search_cancel_event = None

        if event.error:
            self.query_one("#preview-content", Static).update(
                Text(f"Search failed:\n{event.error}")
            )
            self.update_status("Search failed")
            return

        self.update_status(
            f"{len(event.results)} results for '{event.query}'"
        )

        await self.render_entries(event.results)

    # ---------------------------------------------------------
    # Directory size
    # ---------------------------------------------------------

    def _cancel_directory_size(self) -> None:
        """Cancel an obsolete size calculation."""

        self._directory_size_request_id += 1

        if self._directory_size_cancel_event is not None:
            self._directory_size_cancel_event.set()
            self._directory_size_cancel_event = None

    def _start_directory_size(self, path: Path) -> None:
        """Start background directory-size calculation."""

        request_id = self._directory_size_request_id
        cancel_event = Event()

        self._directory_size_cancel_event = cancel_event

        self.run_directory_size_worker(
            path,
            self.root,
            request_id,
            cancel_event,
        )

    @work(
        thread=True,
        group="directory-size",
        exit_on_error=False,
    )
    def run_directory_size_worker(
        self,
        path: Path,
        root: Path,
        request_id: int,
        cancel_event: Event,
    ) -> None:
        """Calculate directory size in a background thread."""

        try:
            result = calculate_directory_size(
                path,
                root,
                cancel_event=cancel_event,
            )

            if cancel_event.is_set():
                return

            message = DirectorySizeCompleted(
                request_id,
                path,
                result,
            )

        except Exception as error:
            if cancel_event.is_set():
                return

            message = DirectorySizeCompleted(
                request_id,
                path,
                None,
                str(error),
            )

        try:
            self.call_from_thread(
                self.post_message,
                message,
            )
        except RuntimeError:
            return

    async def on_directory_size_completed(
        self,
        event: DirectorySizeCompleted,
    ) -> None:
        """Display completed directory-size information."""

        if event.request_id != self._directory_size_request_id:
            return

        if (
            self._previewed_entry is None
            or self._previewed_entry.path != event.path
        ):
            return

        self._directory_size_cancel_event = None

        self.update_preview(
            self._previewed_entry,
            directory_size=event.result,
            directory_error=event.error,
            calculate_directory=False,
        )

    # ---------------------------------------------------------
    # File preview and metadata
    # ---------------------------------------------------------

    def on_list_view_highlighted(
        self,
        event: ListView.Highlighted,
    ) -> None:
        """Preview the highlighted entry."""

        if isinstance(event.item, BrowserItem):
            self.update_preview(event.item.entry)

    def update_preview(
        self,
        entry: FileEntry,
        *,
        directory_size: DirectorySize | None = None,
        directory_error: str | None = None,
        calculate_directory: bool = True,
    ) -> None:
        """Display size, permissions, and file contents."""

        self._cancel_directory_size()
        self._previewed_entry = entry

        kind = "Directory" if entry.is_dir else "File"

        details = [
            entry.name,
            "",
            f"Type: {kind}",
            f"Path: {entry.path}",
            "",
        ]

        try:
            metadata = inspect_file(entry.path, self.root)

            modified = datetime.fromtimestamp(
                metadata.modified
            ).astimezone().strftime("%Y-%m-%d %H:%M:%S")

            details.append(f"Modified: {modified}")

            if entry.is_dir:
                details.append(
                    "Directory record size: "
                    f"{format_bytes(metadata.logical_size_bytes)}"
                )

                if directory_size is not None:
                    prefix = (
                        "At least "
                        if directory_size.errors
                        else ""
                    )

                    details.extend([
                        "",
                        "DIRECTORY CONTENTS",
                        (
                            f"Logical total: {prefix}"
                            f"{format_bytes(directory_size.logical_size_bytes)}"
                        ),
                        (
                            "Allocated file data: "
                            + (
                                f"{prefix}"
                                f"{format_bytes(directory_size.allocated_size_bytes)}"
                                if directory_size.allocated_size_bytes is not None
                                else "Unavailable on this filesystem"
                            )
                        ),
                        f"Files: {directory_size.file_count:,}",
                        (
                            "Subdirectories: "
                            f"{directory_size.directory_count:,}"
                        ),
                    ])

                    if directory_size.errors:
                        details.append(
                            f"Warning: {directory_size.errors} item(s) "
                            "could not be inspected. Total is incomplete."
                        )

                elif directory_error:
                    details.extend([
                        "",
                        "DIRECTORY CONTENTS",
                        f"Size calculation failed: {directory_error}",
                    ])

                else:
                    details.extend([
                        "",
                        "DIRECTORY CONTENTS",
                        "Calculating directory contents...",
                    ])

            else:
                details.extend([
                    "Logical file size: "
                    f"{format_bytes(metadata.logical_size_bytes)}",
                    (
                        "Allocated on disk: "
                        + (
                            format_bytes(metadata.allocated_size_bytes)
                            if metadata.allocated_size_bytes is not None
                            else "Unavailable on this filesystem"
                        )
                    ),
                ])

            details.extend([
                "",
                "PERMISSIONS (READ-ONLY)",
                (
                    f"Mode: {metadata.mode_octal} "
                    f"({metadata.mode_symbolic})"
                ),
                (
                    f"Owner: {metadata.owner_name} "
                    f"(UID {metadata.owner_uid})"
                ),
                (
                    f"Group: {metadata.group_name} "
                    f"(GID {metadata.group_gid})"
                ),
                "Effective access for current process:",
                f"  Read: {'Yes' if metadata.can_read else 'No'}",
                f"  Write: {'Yes' if metadata.can_write else 'No'}",
                (
                    "  Execute / traverse: "
                    f"{'Yes' if metadata.can_execute else 'No'}"
                ),
                "",
                "Access checks are advisory.",
            ])

        except (OSError, RuntimeError, ValueError) as error:
            details.append(f"Metadata unavailable: {error}")

        if entry.is_dir:
            details.extend([
                "",
                "Press Enter to open this directory.",
            ])

        else:
            details.extend([
                "",
                "---------------- PREVIEW ----------------",
                "",
            ])

            try:
                preview, truncated = read_preview(
                    entry.path,
                    self.root,
                )

                details.append(preview)

                if truncated:
                    details.extend([
                        "",
                        "[Preview limited to 64 KB.]",
                    ])

            except (OSError, RuntimeError, ValueError) as error:
                details.append(f"Preview unavailable: {error}")

        self.query_one("#preview-content", Static).update(
            Text("\n".join(details))
        )

        self.query_one("#preview-pane", PreviewPane).scroll_y = 0

        if (
            entry.is_dir
            and directory_size is None
            and directory_error is None
            and calculate_directory
        ):
            self._start_directory_size(entry.path)

    # ---------------------------------------------------------
    # File operation selection
    # ---------------------------------------------------------

    def _get_selected_entry(self) -> FileEntry | None:
        """Return the highlighted file or directory."""

        file_list = self.query_one("#file-list", ListView)

        index = file_list.index

        if index is None:
            return None

        children = list(file_list.children)

        if index < 0 or index >= len(children):
            return None

        selected = children[index]

        if isinstance(selected, BrowserItem):
            return selected.entry

        return None

    def _begin_file_operation(self, operation: str) -> None:
        """Begin copying, moving, or renaming the selected entry."""

        if isinstance(self.focused, Input):
            return

        if self._operation_busy:
            self.notify(
                "Another file operation is running.",
                severity="warning",
            )
            return

        entry = self._get_selected_entry()

        if entry is None:
            self.notify(
                "Select a file or folder first.",
                severity="warning",
            )
            return

        if not is_within_root(entry.path, self.root):
            self.notify(
                "Source is outside the configured root.",
                severity="error",
            )
            return

        self._pending_source = entry.path

        if operation in {"copy", "move"}:
            self._input_mode = operation

            self.push_screen(
                DirectoryPicker(
                    self.root,
                    self.current,
                ),
                self._on_destination_selected,
            )
            return

        if operation == "rename":
            self._input_mode = "rename_name"

            search = self.query_one("#search", Input)
            search.value = ""
            search.placeholder = "Enter the new filename, then press Enter."
            search.styles.display = "block"
            search.focus()

    def action_begin_copy(self) -> None:
        """Copy the selected entry."""

        self._begin_file_operation("copy")

    def action_begin_move(self) -> None:
        """Move the selected entry."""

        self._begin_file_operation("move")

    def action_begin_rename(self) -> None:
        """Rename the selected entry."""

        self._begin_file_operation("rename")

    def _on_destination_selected(
        self,
        result: Path | ManualPathRequest | None,
    ) -> None:
        """Receive a destination chosen by the folder picker."""

        source = self._pending_source
        operation = self._input_mode

        if source is None or operation not in {"copy", "move"}:
            self._pending_source = None
            self._input_mode = None
            return

        if result is None:
            self._pending_source = None
            self._input_mode = None

            self.notify(
                "Destination selection cancelled.",
                timeout=2,
            )
            return

        if isinstance(result, ManualPathRequest):
            self._input_mode = f"{operation}_path"

            search = self.query_one("#search", Input)
            search.value = ""
            search.placeholder = (
                "Enter an existing destination directory path, then Enter."
            )
            search.styles.display = "block"
            search.focus()
            return

        
        try:
            self._set_pending_destination(
                operation,
                result,
            )

        except (OSError, RuntimeError, ValueError) as error:
            self._pending_source = None
            self._pending_operation = None
            self._input_mode = None

            self.notify(
                f"Invalid destination: {error}",
                severity="error",
                timeout=5,
            )


    def _set_pending_destination(
        self,
        operation: str,
        destination_directory: Path,
    ) -> None:
        """Validate a destination directory and prepare confirmation."""

        source = self._pending_source

        if source is None:
            raise RuntimeError("No source item is selected.")

        destination_directory = destination_directory.resolve(
            strict=True
        )

        if not destination_directory.is_relative_to(self.root):
            raise PermissionError(
                "Destination is outside the configured root."
            )

        if not destination_directory.is_dir():
            raise NotADirectoryError(destination_directory)

        destination = destination_directory / source.name

        if (
            destination.resolve(strict=False)
            == source.resolve(strict=True)
        ):
            raise ValueError(
                "Source and destination are the same."
            )

        self._pending_operation = PendingOperation(
            operation=operation,
            source=source,
            destination=destination,
        )

        self._show_operation_confirmation()

    
    def _show_operation_confirmation(self) -> None:
        """Ask for confirmation before executing an operation."""

        pending = self._pending_operation

        if pending is None:
            return

        self._input_mode = "confirm_operation"

        search = self.query_one("#search", Input)

        search.value = ""
        search.placeholder = (
            f"{pending.operation.upper()}: "
            f"{pending.source.name} → {pending.destination}\n"
            "Press ENTER to confirm or ESC to cancel."
        )

        search.styles.display = "block"
        search.focus()

    # ---------------------------------------------------------
    # File operation execution
    # ---------------------------------------------------------

    def _start_file_operation(
        self,
        pending: PendingOperation,
    ) -> None:
        """Start the confirmed operation in a background worker."""

        self._operation_request_id += 1

        request_id = self._operation_request_id
        self._operation_busy = True

        self.update_status(
            f"{pending.operation.capitalize()} in progress..."
        )

        self.run_file_operation_worker(
            request_id,
            pending,
            self.root,
        )

    @work(
        thread=True,
        group="file-operation",
        exit_on_error=False,
    )
    def run_file_operation_worker(
        self,
        request_id: int,
        pending: PendingOperation,
        root: Path,
    ) -> None:
        """Run the filesystem operation without blocking the interface."""

        try:
            if pending.operation == "copy":
                copy_path(
                    pending.source,
                    pending.destination,
                    root,
                )

            elif pending.operation == "move":
                move_path(
                    pending.source,
                    pending.destination,
                    root,
                )

            elif pending.operation == "rename":
                rename_path(
                    pending.source,
                    pending.destination.name,
                    root,
                )

            else:
                raise ValueError("Unknown file operation.")

            message = FileOperationCompleted(
                request_id,
                pending.operation,
                pending.source,
                pending.destination,
            )

        except Exception as error:
            message = FileOperationCompleted(
                request_id,
                pending.operation,
                pending.source,
                pending.destination,
                str(error),
            )

        try:
            self.call_from_thread(
                self.post_message,
                message,
            )
        except RuntimeError:
            return

    async def on_file_operation_completed(
        self,
        event: FileOperationCompleted,
    ) -> None:
        """Display operation result and refresh the browser."""

        if event.request_id != self._operation_request_id:
            return

        self._operation_busy = False

        if event.error:
            self.notify(
                f"{event.operation.capitalize()} failed: {event.error}",
                severity="error",
                timeout=6,
            )

        else:
            self.notify(
                f"{event.operation.capitalize()} completed: "
                f"{event.destination.name}",
                severity="information",
                timeout=4,
            )

        await self.refresh_directory()
        self.query_one("#file-list", ListView).focus()

    # ---------------------------------------------------------
    # Navigation
    # ---------------------------------------------------------

    async def on_list_view_selected(
        self,
        event: ListView.Selected,
    ) -> None:
        """Open a selected directory."""

        if not isinstance(event.item, BrowserItem):
            return

        entry = event.item.entry

        if not entry.is_dir:
            return

        if not is_within_root(entry.path, self.root):
            self.notify(
                "Access outside the browsing root is blocked.",
                severity="warning",
            )
            return

        self._invalidate_search()
        self._last_search_query = ""

        self._pending_source = None
        self._pending_operation = None
        self._input_mode = None

        search = self.query_one("#search", Input)
        search.value = ""
        search.styles.display = "none"

        try:
            self.current = entry.path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            self.notify(
                f"Unable to open directory: {error}",
                severity="error",
            )
            return

        await self.refresh_directory()
        self.query_one("#file-list", ListView).focus()

    async def action_go_parent(self) -> None:
        """Go to the parent without leaving the configured root."""

        if isinstance(self.focused, Input):
            return

        if self.current == self.root:
            return

        parent = self.current.parent

        if not is_within_root(parent, self.root):
            return

        self._invalidate_search()
        self._last_search_query = ""

        search = self.query_one("#search", Input)
        search.value = ""
        search.styles.display = "none"

        self.current = parent

        await self.refresh_directory()
        self.query_one("#file-list", ListView).focus()

    # ---------------------------------------------------------
    # Keyboard actions
    # ---------------------------------------------------------

    async def action_cancel_search(self) -> None:
        """Cancel searching or an operation prompt."""

        self._invalidate_search()
        self._last_search_query = ""

        self._pending_source = None
        self._pending_operation = None
        self._input_mode = None

        search = self.query_one("#search", Input)
        search.value = ""
        search.styles.display = "none"

        await self.refresh_directory()
        self.query_one("#file-list", ListView).focus()

    async def action_toggle_hidden(self) -> None:
        """Toggle visibility of hidden entries."""

        if isinstance(self.focused, Input):
            return

        self.include_hidden = not self.include_hidden

        if self._last_search_query:
            self._start_search(
                self._last_search_query,
                self._last_search_base,
            )
        else:
            await self.refresh_directory()

        self.notify(
            f"Hidden files {'shown' if self.include_hidden else 'hidden'}.",
            timeout=2,
        )

    async def action_cycle_sort(self) -> None:
        """Cycle between name, size, and modified-date sorting."""

        if isinstance(self.focused, Input):
            return

        index = self.SORT_MODES.index(self.sort_by)

        self.sort_by = self.SORT_MODES[
            (index + 1) % len(self.SORT_MODES)
        ]

        if self._last_search_query:
            self._start_search(
                self._last_search_query,
                self._last_search_base,
            )
        else:
            await self.refresh_directory()

        self.notify(
            f"Sorted by {self.sort_by}.",
            timeout=2,
        )

    def action_copy_current_directory(self) -> None:
        """Copy and verify the current directory path."""

        try:
            provider = copy_to_system_clipboard(
                str(self.current),
                self,
            )

        except RuntimeError as error:
            self.notify(
                str(error),
                severity="error",
                timeout=6,
            )
            return

        if provider == "OSC52-unverified":
            self.notify(
                "Clipboard request sent; terminal support is unverified.",
                severity="warning",
                timeout=4,
            )
        else:
            self.notify(
                f"Directory path copied and verified ({provider}).",
                severity="information",
                timeout=3,
            )
