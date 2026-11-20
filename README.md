# 🥔 Potato CLI

**A secure, local-first, keyboard-driven terminal file manager for Ubuntu and Linux.**

Potato makes navigating your filesystem easier without repeatedly typing `cd` and other shell commands. Browse directories, search for files recursively, preview file contents, inspect file information, and manage files through a keyboard-driven terminal interface.

## ✨ Features

- **Keyboard Navigation** – Navigate files and directories using arrow keys and shortcuts.
- **File Browsing** – Browse directories and inspect files directly from your terminal.
- **Recursive Search** – Find files across nested directories.
- **File Preview** – Preview text file contents without opening another application.
- **File Information** – Inspect file sizes, types, paths, and modification dates.
- **Hidden Files** – Toggle visibility of hidden files and directories.
- **Sorting** – Sort files by name, size, and modification date.
- **Multi-Selection** – Mark multiple files and directories for batch operations.
- **Batch Copy and Move** – Copy or move multiple selected items in one operation.
- **Operation Progress** – See progress while longer file operations are running.
- **Copy Directory Path** – Copy the current directory path to your system clipboard.
- **Local-First** – Designed to access your local filesystem without requiring cloud storage.

## 📦 Installation

Potato is distributed through GitHub Releases as a Python package.

### Requirements

- Ubuntu or another Linux distribution.
- Python 3.12 or newer.
- pipx.

### 1. Install pipx

```bash
sudo apt update
sudo apt install -y pipx
```

Configure your PATH:

```bash
pipx ensurepath
```

Open a new terminal after running this command.

### 2. Install Potato

Install version `0.2.0`:

```bash
pipx install "https://github.com/ngnl19/potato-folder-manager/releases/download/v0.2.0/potato_cli-0.2.0-py3-none-any.whl"
```

### 3. Verify installation

```bash
potato --help
```

## 🚀 Usage

Start Potato using:

```bash
potato start
```

By default, Potato starts browsing your home directory.

You can also specify a starting directory:

```bash
potato start --path ~/Downloads
```

Or use the short option:

```bash
potato start -p ~/Documents
```

## ⌨️ Keyboard Shortcuts

| Key | Action |
|---|---|
| `↑` / `↓` | Navigate files and directories |
| `Enter` | Open the selected directory |
| `Backspace` | Navigate to the parent directory |
| `Tab` | Switch focus to the preview panel |
| `Space` | Mark or unmark the highlighted item |
| `F1` | Search the current directory |
| `F2` | Search the configured root recursively |
| `Esc` | Cancel the current action or clear selection |
| `F3` | Toggle hidden files |
| `F4` | Change sorting method |
| `F5` | Copy the current directory path |
| `F6` | Copy selected item(s) |
| `F7` | Move selected item(s) |
| `F8` | Rename the highlighted item |
| `Ctrl + Q` | Exit Potato |

When no items are marked, copy and move operate on the highlighted item. Marked items are shown with `[x]`; unmarked items use `[ ]`. Entries retain their type labels, for example `[ ] [DIR] Documents` and `[ ] [FILE] report.txt`.

## 🔒 Security and Privacy

Potato is designed with local-first filesystem access and security in mind.

- Directory browsing is restricted to the configured browsing root.
- Symbolic links are skipped during directory browsing and recursive search.
- File previews have a 64 KB size limit.
- Special files are excluded from regular file browsing.
- No cloud storage or remote file indexing is required.

## 🛠️ Development

Clone the repository:

```bash
git clone https://github.com/ngnl19/potato-folder-manager.git
cd potato-folder-manager
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install development dependencies:

```bash
pip install -e ".[dev]"
```

Run code quality checks:

```bash
ruff check src tests
```

Run automated tests:

```bash
pytest -v
```

## 🔄 CI/CD

Potato uses GitHub Actions to automate:

- Code quality checks with Ruff.
- Automated testing with Pytest.
- Python compilation checks.
- Release package building and validation.
- Automated GitHub Releases when version tags are pushed.

## 📥 Releases

Download the latest version from:

[Potato CLI – GitHub Releases](https://github.com/ngnl19/potato-folder-manager/releases)

## 📋 Project Status

**Current version:** `0.2.0`

Potato is actively being developed. Features and functionality may change as the project evolves.

## 📄 License

See the repository for licensing information.