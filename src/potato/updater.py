from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

GITHUB_API_URL = (
    "https://api.github.com/repos/ngnl19/potato-folder-manager/releases/latest"
)
PACKAGE_NAME = "potato-cli"
WHEEL_PREFIX = "potato_cli-"
WHEEL_SUFFIX = "-py3-none-any.whl"


class UpdateError(Exception):
    """An error occurred while checking or installing a Potato update."""


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    """Details needed to install a published Potato release."""

    version: str
    tag_name: str
    wheel_name: str
    wheel_url: str


def _version_parts(version: str) -> tuple[int, int, int]:
    """Parse a stable three-part version."""
    if not isinstance(version, str) or not re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)",
        version,
    ):
        raise UpdateError(f"Unsupported version: {version!r}")

    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def is_newer_version(candidate: str, current: str) -> bool:
    """Return whether candidate is newer than current."""
    return _version_parts(candidate) > _version_parts(current)


def _validate_wheel_url(url: str, tag_name: str, wheel_name: str) -> None:
    """Restrict downloads to the expected official GitHub release asset."""
    parsed = urlsplit(url)
    expected_path = (
        "/ngnl19/potato-folder-manager/releases/download/"
        f"{tag_name}/{wheel_name}"
    )

    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise UpdateError(
            f"Release contains an unexpected wheel download URL: {url!r}"
        )


def get_latest_release(*, timeout: float = 10.0) -> ReleaseInfo:
    """Fetch the latest stable GitHub release."""
    request = Request(
        GITHUB_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Potato-CLI-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            payload: Any = json.loads(response.read().decode("utf-8"))
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
    ) as error:
        raise UpdateError(
            f"Could not contact GitHub Releases: {error}"
        ) from error
    except json.JSONDecodeError as error:
        raise UpdateError(
            "GitHub returned invalid release JSON."
        ) from error

    if not isinstance(payload, dict):
        raise UpdateError(
            "GitHub returned an unexpected release response."
        )

    tag_name = payload.get("tag_name")
    assets = payload.get("assets")

    if not isinstance(tag_name, str) or not tag_name.startswith("v"):
        raise UpdateError(
            "Latest GitHub release has an invalid version tag."
        )

    version = tag_name[1:]
    _version_parts(version)

    if not isinstance(assets, list):
        raise UpdateError(
            "Latest GitHub release does not contain a wheel asset."
        )

    expected_wheel_name = f"{WHEEL_PREFIX}{version}{WHEEL_SUFFIX}"
    wheel_asset: dict[str, Any] | None = None

    for asset in assets:
        if (
            isinstance(asset, dict)
            and asset.get("name") == expected_wheel_name
        ):
            wheel_asset = asset
            break

    if wheel_asset is None:
        raise UpdateError(
            f"Latest GitHub release does not contain "
            f"{expected_wheel_name}."
        )

    wheel_url = wheel_asset.get("browser_download_url")

    if not isinstance(wheel_url, str):
        raise UpdateError(
            "Latest GitHub wheel asset has no download URL."
        )

    _validate_wheel_url(wheel_url, tag_name, expected_wheel_name)

    return ReleaseInfo(
        version=version,
        tag_name=tag_name,
        wheel_name=expected_wheel_name,
        wheel_url=wheel_url,
    )


def find_current_pipx_environment(
    *,
    prefix: Path | None = None,
) -> Path | None:
    """Return the current pipx venv if its metadata identifies potato-cli."""
    environment = Path(
        prefix if prefix is not None else sys.prefix
    ).resolve()

    metadata_path = environment / "pipx_metadata.json"

    if not environment.is_dir() or not metadata_path.is_file():
        return None

    try:
        metadata = json.loads(
            metadata_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(metadata, dict):
        return None

    main_package = metadata.get("main_package")

    if not isinstance(main_package, dict):
        return None

    if main_package.get("package") != PACKAGE_NAME:
        return None

    if not os.access(environment, os.W_OK):
        return None

    return environment


def install_release(
    release: ReleaseInfo,
    *,
    pipx_executable: str | None = None,
    runner: Callable[
        ..., subprocess.CompletedProcess[str]
    ] | None = None,
) -> None:
    """Reinstall Potato from a release wheel using the existing pipx install."""
    executable = pipx_executable or shutil.which("pipx")

    if executable is None:
        raise UpdateError(
            "Could not find the 'pipx' command on PATH."
        )

    run_command = runner or subprocess.run

    command = [
        executable,
        "install",
        "--force",
        release.wheel_url,
    ]

    try:
        result = run_command(command, check=False)
    except OSError as error:
        raise UpdateError(
            f"Could not start pipx: {error}"
        ) from error

    if result.returncode != 0:
        raise UpdateError(
            "pipx failed to install the update "
            f"(exit code {result.returncode})."
        )
