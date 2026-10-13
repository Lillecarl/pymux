"""
A browser a program on the server drives: the client's half, against a
stand-in browser that answers the way Chrome does. Lillecarl/pymux#653.
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

from pymux.client import browser

#: Writes `DevToolsActivePort` the way Chrome does once it listens, and
#: the command line it was given beside it, then stays up. With
#: `--exit`, it ends at once instead, the way Chrome does when another
#: instance holds the profile.
FAKE = """#!%s
import sys, time
from pathlib import Path
args = dict(one.split("=", 1) for one in sys.argv[1:] if "=" in one)
profile = Path(args["--user-data-dir"])
(profile / "argv").write_text("\\n".join(sys.argv[1:]))
if "--exit" in sys.argv:
    sys.exit(0)
(profile / "DevToolsActivePort").write_text(args["--remote-debugging-port"] + "\\n/devtools/browser/fake\\n")
time.sleep(60)
"""


@pytest.fixture
def fake(tmp_path):
    path = tmp_path / "chromium"
    path.write_text(FAKE % sys.executable)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_the_browser_says_where_it_listens_and_keeps_our_profile(fake, tmp_path):
    profile = tmp_path / "profile"
    process, path, kind, _where = browser.start(
        fake, str(profile), 9333, ["--user-data-dir=/elsewhere", "--headless=new"]
    )
    try:
        assert (path, kind) == ("/devtools/browser/fake", browser.Remote.CDP)
        argv = (profile / "argv").read_text().splitlines()
        assert argv[:2] == ["--remote-debugging-port=9333", "--user-data-dir=%s" % profile]
        assert "--headless=new" in argv and "--user-data-dir=/elsewhere" not in argv
    finally:
        process.kill()
        process.wait()


def test_a_browser_that_ends_before_it_listens_says_so(fake, tmp_path):
    with pytest.raises(browser.BrowserError, match="ended with 0 before it listened"):
        browser.start(fake, str(tmp_path / "profile"), 9334, ["--exit"])


def test_a_browser_that_is_not_here_says_so(tmp_path):
    with pytest.raises(browser.BrowserError, match="is not here"):
        browser.start(str(tmp_path / "nothing"), str(tmp_path / "profile"), 9335, [])


def test_the_line_on_stderr_says_where_it_listens(tmp_path):
    "Chromium 153 writes no `DevToolsActivePort` for a port it was given."
    (tmp_path / browser.LOG).write_text("noise\nDevTools listening on ws://127.0.0.1:9002/devtools/browser/x\nmore\n")
    assert browser.endpoint_in(tmp_path, 9002, browser.Remote.CDP) == "/devtools/browser/x"
    assert browser.endpoint_in(tmp_path, 9003, browser.Remote.CDP) is None


def test_a_firefox_speaks_bidi_and_says_so_its_own_way(tmp_path):
    assert browser.kind_of("/Applications/Firefox.app/Contents/MacOS/firefox") == browser.Remote.BIDI
    assert browser.kind_of("/usr/bin/chromium") == browser.Remote.CDP
    (tmp_path / browser.LOG).write_text("WebDriver BiDi listening on ws://127.0.0.1:9004\n")
    assert browser.endpoint_in(tmp_path, 9004, browser.Remote.BIDI) == "/session"
    argv = browser.argv_of("firefox", browser.Remote.BIDI, 9004, tmp_path, ["--headless=new", "--profile=/x"])
    assert argv == [
        "firefox",
        "--remote-debugging-port",
        "9004",
        "--profile",
        str(tmp_path),
        "--no-remote",
        "--headless",
    ]


def test_a_port_another_browser_wrote_is_not_this_one(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("9000\n/devtools/browser/old\n")
    assert browser.endpoint_in(tmp_path, 9001, browser.Remote.CDP) is None
    assert browser.endpoint_in(tmp_path, 9000, browser.Remote.CDP) == "/devtools/browser/old"


async def test_the_client_with_a_loop_starts_one_too(fake, tmp_path):
    process, path, _kind, _where = await browser.start_async(fake, str(tmp_path / "profile"), 9336, [])
    try:
        assert path == "/devtools/browser/fake"
    finally:
        process.kill()
        await process.wait()


def test_a_mac_app_starts_through_open_as_its_own_app(tmp_path, monkeypatch):
    "Firefox started as a child of the client could not find its profile. Lillecarl/pymux#653."
    monkeypatch.setattr(sys, "platform", "darwin")
    program = "/Applications/Firefox.app/Contents/MacOS/firefox"
    monkeypatch.setattr(browser, "program_of", lambda _asked: program)
    argv, where, kind = browser._prepared("", str(tmp_path), 9337, ["--headless"])
    assert kind == browser.Remote.BIDI
    assert argv[:8] == [
        "open",
        "-n",
        "-W",
        "-a",
        "/Applications/Firefox.app",
        "--stderr",
        str(where / browser.LOG),
        "--args",
    ]
    assert argv[8:12] == ["--remote-debugging-port", "9337", "--profile", str(tmp_path)]
    assert browser.bundle_of("/usr/bin/chromium") is None


def test_the_default_profile_is_ours(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    assert browser.profile_of("", browser.Remote.CDP) == tmp_path / "pymux" / "browser"
    assert browser.profile_of("", browser.Remote.BIDI) == tmp_path / "pymux" / "browser-firefox"
    assert os.path.isdir(tmp_path / "pymux" / "browser")
