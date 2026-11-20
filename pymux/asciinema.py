"""
An asciinema server: where it is, who this machine is to it, and an upload.

Everything here reads the files the `asciinema` CLI reads, the way its 3.2
release reads them (`src/config.rs`, `src/api.rs`), so one `asciinema auth`
links both to an account. The install id is the password of every upload:
nothing here prints it or logs it, and only `connect_url` puts it in a
string, because opening that link is how a person proves it is theirs.
"""

from __future__ import annotations

import tomllib
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_SERVER",
    "Upload",
    "UploadError",
    "Visibility",
    "connect_url",
    "install_id",
    "server_url",
    "upload",
]

DEFAULT_SERVER: Final = "https://asciinema.org"
INSTALL_ID: Final = "install-id"
#: A slow link and a long recording: the CLI sets no limit at all.
UPLOAD_TIMEOUT: Final = 120.0


class Visibility(StrEnum):
    PUBLIC = "public"
    UNLISTED = "unlisted"
    PRIVATE = "private"


class UploadError(Exception):
    "The server refused a recording, with what it said."


def _home(env: Mapping[str, str], own: str, xdg: str, fallback: tuple[str, ...]) -> Path:
    if env.get(own):
        return Path(env[own])
    if env.get(xdg):
        return Path(env[xdg]) / "asciinema"
    if env.get("HOME"):
        return Path(env["HOME"]).joinpath(*fallback, "asciinema")
    raise UploadError("need $HOME, $%s or $%s" % (xdg, own))


def config_home(env: Mapping[str, str]) -> Path:
    return _home(env, "ASCIINEMA_CONFIG_HOME", "XDG_CONFIG_HOME", (".config",))


def state_home(env: Mapping[str, str]) -> Path:
    return _home(env, "ASCIINEMA_STATE_HOME", "XDG_STATE_HOME", (".local", "state"))


def server_url(env: Mapping[str, str], system_config: Path = Path("/etc/asciinema/config.toml")) -> str:
    """
    The server the CLI would upload to: `ASCIINEMA_SERVER_URL`, then the
    older `ASCIINEMA_API_URL`, then `[server] url` of the last config file
    that names one, then asciinema.org. The CLI asks a person where there
    is none; a server has nobody to ask.
    """
    for name in ("ASCIINEMA_SERVER_URL", "ASCIINEMA_API_URL"):
        if env.get(name):
            return env[name].rstrip("/")
    url = DEFAULT_SERVER
    home = config_home(env)
    for path in (system_config, home / "defaults.toml", home / "config.toml"):
        try:
            config = tomllib.loads(path.read_text())
        except FileNotFoundError:
            continue
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise UploadError("%s: %s" % (path, e))
        url = config.get("server", {}).get("url") or url
    return url.rstrip("/")


def install_id(env: Mapping[str, str]) -> str:
    """
    This machine's id on the server, from the state directory, else the
    config directory where asciinema 2 kept it, else a new one saved to
    the state directory: the CLI's own order.
    """
    path = state_home(env) / INSTALL_ID
    for candidate in (path, config_home(env) / INSTALL_ID):
        try:
            return candidate.read_text().strip()
        except FileNotFoundError:
            continue
    made = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(made)
    return made


def connect_url(env: Mapping[str, str]) -> str:
    "The link that ties this machine's install id to an account on the server."
    return "%s/connect/%s" % (server_url(env), install_id(env))


@dataclass(frozen=True, slots=True)
class Upload:
    cast: str
    filename: str
    title: str | None = None
    description: str | None = None
    visibility: Visibility | None = None


async def upload(
    what: Upload,
    env: Mapping[str, str],
    user_agent: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    "Send one asciicast to the server. Gives back the URL the server gave it."
    fields = {
        name: value
        for name, value in (
            ("title", what.title),
            ("description", what.description),
            ("visibility", what.visibility and str(what.visibility)),
        )
        if value
    }
    url = server_url(env)
    async with httpx.AsyncClient(transport=transport, timeout=UPLOAD_TIMEOUT) as client:
        try:
            response = await client.post(
                url + "/api/v1/recordings",
                files={"file": (what.filename, what.cast.encode(), "application/x-asciicast")},
                data=fields,
                auth=(env.get("USER", ""), install_id(env)),
                headers={"Accept": "application/json", "User-Agent": user_agent},
            )
        except httpx.HTTPError as e:
            raise UploadError("%s: %s" % (url, e))
    if response.is_success:
        try:
            return response.json()["url"]
        except ValueError, KeyError, TypeError:
            raise UploadError("%s answered %d with no url" % (url, response.status_code))
    raise UploadError(_refusal(response))


def _refusal(response: httpx.Response) -> str:
    try:
        message = response.json().get("message")
    except ValueError, AttributeError:
        message = None
    if not message and response.status_code == 413:
        message = "the recording is over the server's size limit"
    if response.status_code == 401:
        message = "%s; `record-auth` gives the link that ties this machine to an account" % (
            message or "not linked to an account"
        )
    return "%s answered %d: %s" % (response.url.host, response.status_code, message or response.reason_phrase)
