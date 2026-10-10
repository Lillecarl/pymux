"""
The `notify-send` shim: a program in a pane notifies the hub without
knowing pymux.

The shim directory holds one script, `notify-send`, which records
what it was given in the notification hub. It shadows the notifier
of the desktop on purpose: inside a pane a notification belongs to
the session first. Only a pane that starts afterwards sees it.
"""

from __future__ import annotations

import os
import stat
import subprocess

from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.main import Pymux


def test_shim_holds_notify_send_script():
    pymux = Pymux()
    pymux.notify_shim = True
    pymux._ensure_notify_shim()

    directory = pymux._notify_shim_dir
    script = os.path.join(directory, "notify-send")
    assert os.access(script, os.X_OK)
    with open(script) as f:
        assert f.read() == Pymux.NOTIFY_SHIM_SCRIPT

    # A pane that starts again asks for nothing new.
    pymux._ensure_notify_shim()
    assert pymux._notify_shim_dir == directory


def test_shim_rides_path_of_new_pane():
    pymux = Pymux()
    pymux.notify_shim = True
    pymux._ensure_notify_shim()

    environment = {"PATH": "/usr/bin"}
    pymux._shim_pane_environment(environment)

    assert environment["PATH"].startswith(pymux._notify_shim_dir + os.pathsep)


def test_shim_leaves_pane_alone_when_it_is_off():
    pymux = Pymux()

    environment = {"PATH": "/usr/bin"}
    pymux._shim_pane_environment(environment)

    assert environment == {"PATH": "/usr/bin"}


async def test_option_turns_shim_on_and_off():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            pymux.handle_command("set-option notify-shim on")
            assert pymux.notify_shim is True
            pymux.handle_command("show-options notify-shim")
            assert state.message == "on"

            pymux.handle_command("set-option notify-shim off")
            assert pymux.notify_shim is False


# ----------------------------------------------------------------------
# What the script passes on.
#
# A fake `pymux` records its arguments, and the script runs against
# it: the summary and the body reach `pymux notify`, `-u` picks the
# urgency, and every other flag is eaten or ignored, never mistaken
# for the summary.


def make_fake_pymux(tmp_path):
    """A `pymux` that writes every call to a file, one line per call."""
    record = tmp_path / "calls"
    fake = tmp_path / "pymux"
    fake.write_text(
        '#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\n' % (record,),
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return str(fake), str(record)


def run_script(pymux, arguments, tmp_path, monkeypatch):
    fake, record = make_fake_pymux(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    subprocess.run(
        [os.path.join(pymux._notify_shim_dir, "notify-send"), *arguments],
        check=False,
        capture_output=True,
    )
    with open(record) as f:
        return [line for line in f.read().splitlines()]


def test_summary_and_body_reach_notify(tmp_path, monkeypatch):
    pymux = Pymux()
    pymux.notify_shim = True
    pymux._ensure_notify_shim()

    assert run_script(pymux, ["Build done", "Three files"], tmp_path, monkeypatch) == [
        "notify -u normal -- Build done Three files"
    ]


def test_short_urgency_flag_picks_urgency(tmp_path, monkeypatch):
    pymux = Pymux()
    pymux.notify_shim = True
    pymux._ensure_notify_shim()

    assert run_script(pymux, ["-u", "critical", "Fire", "Down"], tmp_path, monkeypatch) == [
        "notify -u critical -- Fire Down"
    ]


def test_long_urgency_flag_picks_urgency(tmp_path, monkeypatch):
    pymux = Pymux()
    pymux.notify_shim = True
    pymux._ensure_notify_shim()

    assert run_script(pymux, ["--urgency=low", "Note"], tmp_path, monkeypatch) == ["notify -u low -- Note"]


def test_other_flags_are_eaten_not_summaries(tmp_path, monkeypatch):
    pymux = Pymux()
    pymux.notify_shim = True
    pymux._ensure_notify_shim()

    assert run_script(
        pymux,
        ["-t", "5000", "-i", "icon", "-a", "App", "--hint=INT:x:1", "Note", "Body"],
        tmp_path,
        monkeypatch,
    ) == ["notify -u normal -- Note Body"]


def test_dashes_end_flags(tmp_path, monkeypatch):
    pymux = Pymux()
    pymux.notify_shim = True
    pymux._ensure_notify_shim()

    assert run_script(pymux, ["--", "-not-a-flag"], tmp_path, monkeypatch) == ["notify -u normal -- -not-a-flag"]
