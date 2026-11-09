"""
`browser.open via=client`: the SOCKS forward a browser goes out
through, and how each kind is told about it. Lillecarl/pyterm#975.

`test_mcp_browsers.py` loads a page through the proxy in each kind; this
judges the parts that need no browser.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from pymux_mcp import browser

PROXY = "127.0.0.1:41080"
ASKED = ["forward-port", "-R", "127.0.0.1:0"]


class Forwarding:
    "A server whose forwards table answers `rows`, and `then` once a forward was asked for."

    def __init__(self, rows: list[dict], then: list[dict] | None = None) -> None:
        self.rows = rows
        self.then = then
        self.asked: list[list[str]] = []

    async def sql(self, query: str, params=None) -> list[dict]:
        assert query == browser.SOCKS_FORWARDS
        return list(self.rows)

    async def cmd(self, command, pane_id=None, check=True):
        self.asked.append(list(command))
        if self.then is not None:
            self.rows = self.then


def _row(listening: str, error: str = "") -> dict:
    return {"listening": listening, "error": error}


async def test_a_socks_forward_that_stands_is_used_and_nothing_is_asked():
    "The person confirmed it once; the next browser asks them nothing."
    server = Forwarding([_row(PROXY)])

    with anyio.fail_after(5):
        assert await browser.through_client(server) == PROXY

    assert server.asked == []


async def test_with_none_the_client_is_asked_for_one_and_its_port_is_used():
    server = Forwarding([], then=[_row(PROXY)])

    with anyio.fail_after(5):
        assert await browser.through_client(server) == PROXY

    assert server.asked == [ASKED]


async def test_a_forward_that_failed_is_never_the_proxy(monkeypatch):
    "A failed forward reports the port it asked for, 0, and why; the browser does not start."
    monkeypatch.setattr(browser, "CONFIRM_WAIT", 0.3)
    server = Forwarding([], then=[_row("127.0.0.1:0", "port in use")])

    with anyio.fail_after(5), pytest.raises(RuntimeError, match="port in use"):
        await browser.through_client(server)


async def test_a_question_nobody_answers_says_so(monkeypatch):
    monkeypatch.setattr(browser, "CONFIRM_WAIT", 0.3)
    server = Forwarding([])

    with anyio.fail_after(5), pytest.raises(RuntimeError, match="did not confirm"):
        await browser.through_client(server)


def test_chromium_is_given_the_proxy_switch(tmp_path: Path):
    chromium = browser._chromium("chromium", browser.Seat.HEADLESS, tmp_path, (), (), PROXY)

    assert "--proxy-server=socks5://%s" % (PROXY,) in chromium
    assert not any("proxy" in one for one in browser._chromium("chromium", browser.Seat.HEADLESS, tmp_path, (), ()))


def test_qutebrowser_sets_its_own_proxy_after_the_persons_config(tmp_path: Path):
    """
    qutebrowser hands QtWebEngine a proxy of its own, `content.proxy`,
    which a `--qt-flag=proxy-server` does not win against.
    """
    browser._qutebrowser("qutebrowser", browser.Seat.HEADLESS, tmp_path, 9222, (), PROXY)
    written = (tmp_path / browser.QUTEBROWSER_CONFIG).read_text()

    assert written.rstrip().endswith("c.content.proxy = 'socks5://%s/'" % (PROXY,))


def test_firefox_resolves_names_at_the_proxy(tmp_path: Path):
    "Without remote DNS Firefox resolves here, and a name only the far side knows fails."
    browser._gecko("firefox", browser.Seat.HEADLESS, tmp_path, (), PROXY)
    written = (tmp_path / "user.js").read_text()

    assert 'user_pref("network.proxy.socks_port", 41080);' in written
    assert 'user_pref("network.proxy.socks_remote_dns", true);' in written
