from __future__ import annotations

import os
import shutil
import subprocess
import time
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
from potato.operations import (
    OperationCancelled,
    OperationProgress,
    copy_path,
    move_path,
    rename_path,
)
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
    """One or more filesystem operations awaiting confirmation."""

    operation: str
    sources: tuple[Path, ...]
    destinations: tuple[Path, ...]



def normalize_selected_entries(
    entries: list[FileEntry],
) -> tuple[Path, ...]:
    """Remove duplicate paths and child entries covered by a selected folder."""

    unique_paths = tuple(dict.fromkeys(entry.path for entry in entries))

    selected_paths = set(unique_paths)

    normalized: list[Path] = []

    for path in unique_paths:
        parent = path.parent
        covered_by_parent = False

        while parent != path:
            if parent in selected_paths:
                covered_by_parent = True
                break

            next_parent = parent.parent
            if next_parent == parent:
                break
            parent = next_parent

        if not covered_by_parent:
            normalized.append(path)

    return tuple(normalized)



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
        sources: tuple[Path, ...],
        destinations: tuple[Path, ...],
        completed_items: int,
        error: str | None = None,
    ) -> None:
        super().__init__()

        self.request_id = request_id
        self.operation = operation
        self.sources = sources
        self.destinations = destinations
        self.completed_items = completed_items
        self.total_items = len(sources)
        self.error = error



class FileOperationProgress(Message):
    """Progress update from a background file operation."""

    def __init__(
        self,
        request_id: int,
        operation: str,
        progress: OperationProgress,
        item_number: int,
        total_items: int,
    ) -> None:
        super().__init__()

        self.request_id = request_id
        self.operation = operation
        self.progress = progress
        self.item_number = item_number
        self.total_items = total_items


class BrowserItem(ListItem):
    """A selectable filesystem entry."""

    @property
    def selected(self) -> bool:
        return self._selected

    @selected.setter
    def selected(self, value: bool) -> None:
        self._selected = value

    def __init__(
    self,
    entry: FileEntry,
    root: Path,
    *,
    selected: bool = False,
) -> None:
        self.entry = entry
        self.root = root
        self._selected = selected

        label_widget = Static(self._build_label())
        super().__init__(label_widget)

        self.label_widget = label_widget

    def _build_label(self) -> Text:
        marker = "[x]" if self.selected else "[ ]"
        icon = "📁" if self.entry.path.is_dir() else "📄"

        return Text(f"{marker} {icon} {self.entry.name}")

    def set_selected(self, selected: bool) -> None:
        self._selected = selected

        if self.is_mounted:
            self.label_widget.update(self._build_label())

    def on_mount(self) -> None:
        self.label_widget.update(self._build_label())



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
        ("space", "toggle_selection", "Mark item"),
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

    #operation-progress {
        height: 2;
        padding: 0 1;
        display: none;
        background: $panel;
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
        self._visible_entries: list[FileEntry] = []
        self._selected_paths: set[Path] = set()

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
        self._pending_sources: tuple[Path, ...] = ()
        self._pending_operation: PendingOperation | None = None

        self._operation_request_id = 0
        self._operation_busy = False
        self._operation_cancel_event: Event | None = None



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

        yield Static("", id="operation-progress")

        yield Static(
            "NAV: ↑/↓ Select | Space Mark | Enter Open | Backspace Parent | Tab Preview"
            "\nSEARCH: F1 Here | F2 All | Esc Cancel\n"
            "FILES: F6 Copy selected | F7 Move selected | F8 Rename one | F5 Copy path"
            "\nVIEW: F3 Hidden | F4 Sort | Ctrl+Q Quit",
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

        if self._selected_paths:
            message += f" | Selected: {len(self._selected_paths)}"

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

        self._visible_entries = list(entries)

        visible_paths = {
            entry.path for entry in entries
        }

        self._selected_paths.intersection_update(visible_paths)

        self._visible_entries = list(entries)

        visible_paths = {
            entry.path for entry in entries
        }

        self._selected_paths.intersection_update(visible_paths)

        await file_list.extend(
            BrowserItem(
                entry,
                self.root,
                selected=entry.path in self._selected_paths,
            )
            for entry in entries
        )

        file_list.index = 0

        self.update_preview(entries[0])
        self.update_status()

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
            source = (
                self._pending_sources[0]
                if self._pending_sources
                else None
            )

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
                sources=(source,),
                destinations=(source.parent / value,),
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
            self._pending_sources = ()
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

    def _get_operation_sources(self) -> tuple[Path, ...]:
        """Get marked entries or fall back to the highlighted entry."""

        if self._selected_paths:
            selected_entries = [
                entry
                for entry in self._visible_entries
                if entry.path in self._selected_paths
            ]
        else:
            selected = self._get_selected_entry()

            selected_entries = (
                [selected] if selected is not None else []
            )

        return normalize_selected_entries(selected_entries)

    def _clear_selection(self) -> None:
        """Clear all marked entries."""

        self._selected_paths.clear()

        file_list = self.query_one("#file-list", ListView)

        for child in file_list.children:
            if isinstance(child, BrowserItem):
                child.set_selected(False)

        self.update_status()

    def action_toggle_selection(self) -> None:
        """Mark or unmark the highlighted entry."""

        if isinstance(self.focused, Input):
            return

        entry = self._get_selected_entry()

        if entry is None:
            return

        if entry.path in self._selected_paths:
            self._selected_paths.remove(entry.path)
            selected = False
        else:
            self._selected_paths.add(entry.path)
            selected = True

        file_list = self.query_one("#file-list", ListView)

        for child in file_list.children:
            if (
                isinstance(child, BrowserItem)
                and child.entry.path == entry.path
            ):
                child.set_selected(selected)
                break

        self.update_status()

        self.notify(
            f"{len(self._selected_paths)} item(s) selected.",
            timeout=1,
        )


    def _begin_file_operation(self, operation: str) -> None:
        """Begin copying, moving, or renaming selected entries."""

        if isinstance(self.focused, Input):
            return

        if self._operation_busy:
            self.notify(
                "Another file operation is running.",
                severity="warning",
            )
            return

        sources = self._get_operation_sources()

        if not sources:
            self.notify(
                "Select or mark at least one item.",
                severity="warning",
            )
            return

        if operation == "rename" and len(sources) != 1:
            self.notify(
                "Rename supports one item at a time.",
                severity="warning",
            )
            return

        for source in sources:
            if not is_within_root(source, self.root):
                self.notify(
                    f"Source outside browsing root: {source}",
                    severity="error",
                )
                return

        self._pending_sources = sources

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
            search.placeholder = (
                "Enter the new filename, then press Enter."
            )
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
        """Receive the destination selected by the folder picker."""

        operation = self._input_mode

        if not self._pending_sources or operation not in {"copy", "move"}:
            self._pending_sources = ()
            self._input_mode = None
            return

        if result is None:
            self._pending_sources = ()
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
                "Enter an existing destination directory path."
            )
            search.styles.display = "block"
            search.focus()
            return

        try:
            self._set_pending_destination(operation, result)

        except (OSError, RuntimeError, ValueError) as error:
            self._pending_sources = ()
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
        """Validate one destination directory for all selected sources."""

        sources = self._pending_sources

        if not sources:
            raise RuntimeError("No source items are selected.")

        destination_directory = destination_directory.resolve(
            strict=True
        )

        if not destination_directory.is_relative_to(self.root):
            raise PermissionError(
                "Destination is outside the configured root."
            )

        if not destination_directory.is_dir():
            raise NotADirectoryError(destination_directory)

        destinations = tuple(
            destination_directory / source.name
            for source in sources
        )

        names = [destination.name for destination in destinations]

        if len(names) != len(set(names)):
            raise FileExistsError(
                "Selected items contain duplicate names. "
                "Choose items with unique names for this destination."
            )

        for source, destination in zip(
            sources,
            destinations,
            strict=True,
        ):
            resolved_source = source.resolve(strict=True)

            if destination.resolve(strict=False) == resolved_source:
                raise ValueError(
                    f"Source and destination are the same: {source.name}"
                )

            if os.path.lexists(destination):
                raise FileExistsError(
                    f"Destination already exists: {destination}"
                )

            if source.is_dir() and destination.is_relative_to(source):
                raise ValueError(
                    f"Cannot copy or move a directory into itself: "
                    f"{source.name}"
                )

        self._pending_operation = PendingOperation(
            operation=operation,
            sources=sources,
            destinations=destinations,
        )

        self._show_operation_confirmation()

    def _show_operation_confirmation(self) -> None:
        """Ask for confirmation before executing the operation."""

        pending = self._pending_operation

        if pending is None:
            return

        count = len(pending.sources)

        names = ", ".join(
            source.name for source in pending.sources[:3]
        )

        if count > 3:
            names += f", and {count - 3} more"

        if pending.operation == "rename":
            target = str(pending.destinations[0])
        else:
            target = str(pending.destinations[0].parent)

        self._input_mode = "confirm_operation"

        search = self.query_one("#search", Input)
        search.value = ""
        search.placeholder = (
            f"{pending.operation.upper()} {count} item(s): "
            f"{names} → {target}. "
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

        cancel_event = Event()

        self._operation_cancel_event = cancel_event
        self._operation_busy = True

        progress_widget = self.query_one(
            "#operation-progress",
            Static,
        )

        progress_widget.styles.display = "block"
        progress_widget.update(
            Text(f"{pending.operation.upper()} preparing...")
        )

        self.update_status(
            f"{pending.operation.capitalize()} in progress..."
        )

        self.run_file_operation_worker(
            request_id,
            pending,
            self.root,
            cancel_event,
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
        cancel_event: Event,
    ) -> None:
        """Execute selected operations and aggregate progress."""

        completed_items = 0
        completed_bytes = 0
        total_items = len(pending.sources)

        try:
            # Calculate sizes before starting so progress has a stable total.
            item_sizes: list[int] = []

            for source in pending.sources:
                if cancel_event.is_set():
                    raise OperationCancelled("Operation cancelled.")

                info = os.lstat(source)

                if os.path.isdir(source):
                    size_info = calculate_directory_size(
                        source,
                        root,
                        cancel_event=cancel_event,
                    )

                    if size_info.errors:
                        raise OSError(
                            f"Cannot calculate a complete size for {source}."
                        )

                    item_sizes.append(
                        size_info.logical_size_bytes
                    )

                else:
                    item_sizes.append(info.st_size)

            total_bytes = sum(item_sizes)

            last_emit_time = 0.0
            last_percent = -1
            last_phase = ""

            for index, (source, destination) in enumerate(
                zip(
                    pending.sources,
                    pending.destinations,
                    strict=True,
                ),
                start=1,
            ):
                if cancel_event.is_set():
                    raise OperationCancelled("Operation cancelled.")

                item_size = item_sizes[index - 1]

                def report_progress(
                        local: OperationProgress,
                        *,
                        item_number: int = index,
                        byte_offset: int = completed_bytes,
                        current_item_size: int = item_size,
                        completed_before: int = completed_items,
                    ) -> None:
                    nonlocal last_emit_time
                    nonlocal last_percent
                    nonlocal last_phase

                    if cancel_event.is_set():
                        return

                    if pending.operation == "copy":
                        global_bytes = (
                            byte_offset + local.bytes_completed
                        )

                    elif local.phase == "completed":
                        global_bytes = (
                            byte_offset + current_item_size
                        )

                    else:
                        global_bytes = byte_offset

                    files_completed = (
                        completed_before
                        + (1 if local.phase == "completed" else 0)
                    )

                    aggregate_phase = local.phase

                    if (
                        local.phase == "completed"
                        and item_number < total_items
                    ):
                        aggregate_phase = "copying"

                    progress = OperationProgress(
                        phase=aggregate_phase,
                        current_path=local.current_path,
                        bytes_completed=global_bytes,
                        total_bytes=total_bytes,
                        files_completed=files_completed,
                        total_files=total_items,
                    )

                    now = time.monotonic()
                    percent = int(progress.percentage)

                    # Avoid overwhelming the UI with progress messages.
                    if (
                        percent == last_percent
                        and local.phase == last_phase
                        and now - last_emit_time < 0.15
                    ):
                        return

                    last_emit_time = now
                    last_percent = percent
                    last_phase = aggregate_phase

                    message = FileOperationProgress(
                        request_id,
                        pending.operation,
                        progress,
                        item_number,
                        total_items,
                    )

                    try:
                        self.call_from_thread(
                            self.post_message,
                            message,
                        )
                    except RuntimeError:
                        return

                if pending.operation == "copy":
                    copy_path(
                        source,
                        destination,
                        root,
                        progress_callback=report_progress,
                        cancel_event=cancel_event,
                    )

                elif pending.operation == "move":
                    move_path(
                        source,
                        destination,
                        root,
                        progress_callback=report_progress,
                        cancel_event=cancel_event,
                    )

                elif pending.operation == "rename":
                    rename_path(
                        source,
                        destination.name,
                        root,
                        progress_callback=report_progress,
                        cancel_event=cancel_event,
                    )

                else:
                    raise ValueError("Unknown file operation.")

                completed_items += 1
                completed_bytes += item_size

            message = FileOperationCompleted(
                request_id,
                pending.operation,
                pending.sources,
                pending.destinations,
                completed_items,
            )

        except Exception as error:
            message = FileOperationCompleted(
                request_id,
                pending.operation,
                pending.sources,
                pending.destinations,
                completed_items,
                str(error),
            )

        try:
            self.call_from_thread(
                self.post_message,
                message,
            )
        except RuntimeError:
            return

    def on_file_operation_progress(
        self,
        event: FileOperationProgress,
    ) -> None:
        """Display throttled background operation progress."""

        if event.request_id != self._operation_request_id:
            return

        progress = event.progress
        percentage = progress.percentage

        width = 24
        filled = round(width * percentage / 100)

        bar = (
            "█" * filled
            + "░" * (width - filled)
        )

        detail = (
            f"{event.operation.upper()} [{bar}] "
            f"{percentage:5.1f}% | "
            f"Item {event.item_number}/{event.total_items} | "
            f"{progress.current_path.name}"
        )

        self.query_one(
            "#operation-progress",
            Static,
        ).update(Text(detail))

    async def on_file_operation_completed(
        self,
        event: FileOperationCompleted,
    ) -> None:
        """Report the result and refresh the browser."""

        if event.request_id != self._operation_request_id:
            return

        self._operation_busy = False
        self._operation_cancel_event = None
        self._pending_sources = ()
        self._pending_operation = None

        progress_widget = self.query_one(
            "#operation-progress",
            Static,
        )

        progress_widget.styles.display = "none"
        progress_widget.update("")

        self._clear_selection()

        if event.error:
            self.notify(
                f"{event.completed_items}/{event.total_items} item(s) "
                f"completed before stopping: {event.error}",
                severity="error",
                timeout=8,
            )

        else:
            self.notify(
                f"{event.operation.capitalize()} completed: "
                f"{event.completed_items} item(s).",
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

        self._pending_sources = ()
        self._pending_operation = None
        self._input_mode = None

        search = self.query_one("#search", Input)
        search.value = ""
        search.styles.display = "none"
        self._clear_selection()

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
        self._clear_selection()

        self.current = parent

        await self.refresh_directory()
        self.query_one("#file-list", ListView).focus()

    # ---------------------------------------------------------
    # Keyboard actions
    # ---------------------------------------------------------

    async def action_cancel_search(self) -> None:
        """Cancel searching or an operation prompt."""

        if (
            self._operation_busy
            and self._operation_cancel_event is not None
        ):
            self._operation_cancel_event.set()

            self.notify(
                "Cancellation requested. Stopping the current operation...",
                severity="warning",
                timeout=3,
            )
            return

        self._invalidate_search()
        self._last_search_query = ""

        self._pending_sources = ()
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
