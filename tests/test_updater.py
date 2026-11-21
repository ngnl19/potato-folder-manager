import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import potato.cli as cli
from potato import updater
from potato.updater import ReleaseInfo, UpdateError


def make_release_payload(
    *,
    tag_name: str = "v0.3.0",
    wheel_url: str | None = None,
) -> dict[str, object]:
    wheel_name = f"potato_cli-{tag_name.removeprefix('v')}-py3-none-any.whl"
    return {
        "tag_name": tag_name,
        "assets": [
            {
                "name": wheel_name,
                "browser_download_url": wheel_url
                or (
                    "https://github.com/ngnl19/potato-folder-manager/releases/"
                    f"download/{tag_name}/{wheel_name}"
                ),
            }
        ],
    }


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_version_comparison() -> None:
    assert updater.is_newer_version("0.3.0", "0.2.9")
    assert updater.is_newer_version("1.0.0", "0.9.9")
    assert not updater.is_newer_version("0.2.0", "0.2.0")
    assert not updater.is_newer_version("0.1.9", "0.2.0")


def test_invalid_version_is_rejected() -> None:
    with pytest.raises(UpdateError, match="Unsupported version"):
        updater.is_newer_version("latest", "0.2.0")


def test_get_latest_release(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = make_release_payload()
    monkeypatch.setattr(
        updater,
        "urlopen",
        lambda request, timeout: FakeResponse(payload),
    )

    release = updater.get_latest_release()

    assert release == ReleaseInfo(
        version="0.3.0",
        tag_name="v0.3.0",
        wheel_name="potato_cli-0.3.0-py3-none-any.whl",
        wheel_url=(
            "https://github.com/ngnl19/potato-folder-manager/releases/"
            "download/v0.3.0/potato_cli-0.3.0-py3-none-any.whl"
        ),
    )


def test_get_latest_release_rejects_non_github_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = make_release_payload(wheel_url="https://example.com/potato.whl")
    monkeypatch.setattr(
        updater,
        "urlopen",
        lambda request, timeout: FakeResponse(payload),
    )

    with pytest.raises(UpdateError, match="unexpected wheel download URL"):
        updater.get_latest_release()


def test_get_latest_release_requires_wheel_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        updater,
        "urlopen",
        lambda request, timeout: FakeResponse({"tag_name": "v0.3.0", "assets": []}),
    )

    with pytest.raises(UpdateError, match="does not contain"):
        updater.get_latest_release()


def test_find_current_pipx_environment(tmp_path: Path) -> None:
    environment = tmp_path / "potato-cli"
    environment.mkdir(parents=True)
    metadata = {
        "main_package": {
            "package": "potato-cli",
            "package_version": "0.2.0",
        }
    }
    (environment / "pipx_metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )

    result = updater.find_current_pipx_environment(prefix=environment)

    assert result == environment


def test_find_current_pipx_environment_rejects_other_prefix(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "potato-cli"
    environment.mkdir(parents=True)
    (environment / "pipx_metadata.json").write_text(
        json.dumps({"main_package": {"package": "potato-cli"}}),
        encoding="utf-8",
    )

    result = updater.find_current_pipx_environment(
        prefix=tmp_path / "another-venv",
    )

    assert result is None


def test_find_current_pipx_environment_rejects_other_package(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "something-else"
    environment.mkdir(parents=True)
    (environment / "pipx_metadata.json").write_text(
        json.dumps({"main_package": {"package": "other-package"}}),
        encoding="utf-8",
    )

    result = updater.find_current_pipx_environment(prefix=environment)

    assert result is None


def test_install_release_uses_pipx_force_install() -> None:
    release = ReleaseInfo(
        version="0.3.0",
        tag_name="v0.3.0",
        wheel_name="potato_cli-0.3.0-py3-none-any.whl",
        wheel_url="https://github.com/ngnl19/potato-folder-manager/releases/"
        "download/v0.3.0/potato_cli-0.3.0-py3-none-any.whl",
    )
    commands: list[list[str]] = []

    def fake_runner(
        command: list[str], *, check: bool
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert check is False
        return subprocess.CompletedProcess(command, 0)

    updater.install_release(
        release,
        pipx_executable="pipx",
        runner=fake_runner,
    )

    assert commands == [["pipx", "install", "--force", release.wheel_url]]


def test_install_release_reports_pipx_failure() -> None:
    release = ReleaseInfo(
        version="0.3.0",
        tag_name="v0.3.0",
        wheel_name="potato_cli-0.3.0-py3-none-any.whl",
        wheel_url="https://github.com/ngnl19/potato-folder-manager/releases/"
        "download/v0.3.0/potato_cli-0.3.0-py3-none-any.whl",
    )

    def fake_runner(
        command: list[str], *, check: bool
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1)

    with pytest.raises(UpdateError, match="exit code 1"):
        updater.install_release(
            release,
            pipx_executable="pipx",
            runner=fake_runner,
        )


def test_install_release_requires_pipx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updater.shutil, "which", lambda name: None)
    release = ReleaseInfo(
        version="0.3.0",
        tag_name="v0.3.0",
        wheel_name="potato_cli-0.3.0-py3-none-any.whl",
        wheel_url="https://github.com/ngnl19/potato-folder-manager/releases/"
        "download/v0.3.0/potato_cli-0.3.0-py3-none-any.whl",
    )

    with pytest.raises(UpdateError, match="Could not find the 'pipx' command"):
        updater.install_release(release)


RELEASE = ReleaseInfo(
    version="0.3.0",
    tag_name="v0.3.0",
    wheel_name="potato_cli-0.3.0-py3-none-any.whl",
    wheel_url=(
        "https://github.com/ngnl19/potato-folder-manager/releases/"
        "download/v0.3.0/potato_cli-0.3.0-py3-none-any.whl"
    ),
)


def test_update_command_cancel_does_not_install(
    monkeypatch,
    tmp_path: Path,
) -> None:
    installed: list[ReleaseInfo] = [] # type: ignore
    monkeypatch.setattr(cli, "get_latest_release", lambda: RELEASE)
    monkeypatch.setattr(
        cli,
        "find_current_pipx_environment",
        lambda: tmp_path,
    )
    monkeypatch.setattr(cli, "install_release", installed.append)

    result = CliRunner().invoke(cli.app, ["update"], input="n\n")

    assert result.exit_code == 0
    assert "Update cancelled" in result.stdout
    assert installed == []


def test_update_command_installs_after_confirmation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    installed: list[ReleaseInfo] = [] # type: ignore
    monkeypatch.setattr(cli, "get_latest_release", lambda: RELEASE)
    monkeypatch.setattr(
        cli,
        "find_current_pipx_environment",
        lambda: tmp_path,
    )
    monkeypatch.setattr(cli, "install_release", installed.append)

    result = CliRunner().invoke(cli.app, ["update"], input="y\n")

    assert result.exit_code == 0
    assert "Successfully updated Potato to 0.3.0" in result.stdout
    assert installed == [RELEASE]


def test_update_command_stops_when_current_version_is_latest(
    monkeypatch,
) -> None:
    release = ReleaseInfo(
        version=cli.__version__,
        tag_name=f"v{cli.__version__}",
        wheel_name=f"potato_cli-{cli.__version__}-py3-none-any.whl",
        wheel_url="https://github.com/ngnl19/potato-folder-manager/releases/"
        f"download/v{cli.__version__}/potato_cli-{cli.__version__}-py3-none-any.whl",
    )
    monkeypatch.setattr(cli, "get_latest_release", lambda: release)

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 0
    assert "already up to date" in result.stdout


def test_update_command_requires_pipx_installation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "get_latest_release", lambda: RELEASE)
    monkeypatch.setattr(cli, "find_current_pipx_environment", lambda: None)

    result = CliRunner().invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "only for writable pipx-managed installations" in result.output
