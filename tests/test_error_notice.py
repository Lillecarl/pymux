"""
A corner that says an error was logged, and where to read it, for a
few seconds. Lillecarl/pymux#241.
"""

from __future__ import annotations

import threading

import anyio
import pytest
from prompt_toolkit.application.current import set_app
from session import create_session, once

from pymux import error_notice
from pymux.layout import Z_INDEX
from pymux.log import logger


@pytest.fixture
def quick(monkeypatch):
    "A notice that goes in a tenth of a second, so a test can see it go."
    monkeypatch.setattr(error_notice, "SHOWN_FOR", 0.1)


async def test_an_error_puts_up_the_notice():
    async with create_session() as (pymux, state):
        logger.error("something broke")

        assert pymux.error_notice.count == 1
        assert pymux.error_notice.text().startswith(" an error was logged: ")
        assert pymux.error_notice.text().rstrip().endswith(".log")


async def test_a_warning_does_not():
    async with create_session() as (pymux, state):
        logger.warning("only a warning")

        assert pymux.error_notice.count == 0
        assert pymux.error_notice.text() == ""


async def test_many_errors_are_one_notice_that_counts():
    async with create_session() as (pymux, state):
        for _ in range(100):
            logger.error("in a loop")

        assert pymux.error_notice.count == 100
        assert "100 errors were logged" in pymux.error_notice.text()


async def test_it_goes_when_the_errors_stop(quick):
    async with create_session() as (pymux, state):
        logger.error("once")

        await once(lambda: pymux.error_notice.count == 0, 5.0, "the notice never went")


async def test_an_error_on_another_thread_reaches_it():
    async with create_session() as (pymux, state):
        thread = threading.Thread(target=lambda: logger.error("from a worker"))
        thread.start()
        await anyio.to_thread.run_sync(thread.join)

        await once(lambda: pymux.error_notice.count == 1, 5.0, "the other thread's error was not noticed")


async def test_the_layout_draws_it_while_it_counts():
    async with create_session() as (pymux, state):
        floats = [one for one in state.layout_manager.layout.floats if one.z_index == Z_INDEX.ERROR_NOTICE]
        assert len(floats) == 1
        (notice,) = floats

        with set_app(state.app):
            assert not notice.content.filter()
        logger.error("now")
        with set_app(state.app):
            assert notice.content.filter()


async def test_it_is_gone_when_the_server_is():
    "The handler belongs to the server's scope, so a test's server leaves none behind."
    async with create_session() as (pymux, state):
        pass

    logger.error("after the server")
    assert pymux.error_notice.count == 0
