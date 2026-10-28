"""
Tags on sessions, windows and panes, which a caller sets and finds
again. Jobs carry the same model; `test_job_tags.py` judges theirs.

The rules: `set-tag` puts a bare key or `key=value` on what `-s`,
`-w`, `-p` or `-j` names, and `-u` takes keys off; `show-tags` prints
them; `--tag` on a listing keeps what carries every tag, a bare key
taking any value, and looks through the whole server; `--tag` on a
command that makes something tags what it made; the formats read
them; and `sql` reads them under `live`.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.answer import Answer
from pymux.commands import handle_command
from pymux.query import ask


async def say(pymux, state, line: str) -> tuple[list[str], list[str]]:
    "What one command line printed, and the errors it gave."
    errors: list[str] = []
    pymux.add_command_error = errors.append
    pymux.answer = Answer()
    try:
        with set_app(state.app):
            answer = handle_command(pymux, line)
            if answer is not None:
                await answer
        return list(pymux.answer.output), errors
    finally:
        pymux.answer = None


def active(pymux, state):
    "The session, window and pane a command with no target means."
    with set_app(state.app):
        pane = pymux.arrangement.get_active_pane()
    return pymux.session_holding(pane), pymux._window_holding(pane), pane


async def test_a_pane_is_tagged_and_shows_its_tags():
    async with create_session() as (pymux, state):
        _session, _window, pane = active(pymux, state)
        target = "%%%d" % pane.pane_id
        await say(pymux, state, "set-tag -t %s agent=a1 bare empty=" % target)
        assert pane.tags == {"agent": "a1", "bare": None, "empty": ""}
        printed, errors = await say(pymux, state, "show-tags -t %s" % target)
        assert not errors, errors
        assert printed == ["agent=a1\nbare\nempty="]

        await say(pymux, state, "set-tag -u -t %s bare missing" % target)
        assert pane.tags == {"agent": "a1", "empty": ""}


async def test_a_window_and_a_session_are_tagged_without_a_target():
    async with create_session() as (pymux, state):
        session, window, _pane = active(pymux, state)
        _printed, errors = await say(pymux, state, "set-tag -w role=logs")
        assert not errors, errors
        _printed, errors = await say(pymux, state, "set-tag -s owner=agent")
        assert not errors, errors
        assert (window.tags, session.tags) == ({"role": "logs"}, {"owner": "agent"})


async def test_a_job_is_tagged_by_its_id():
    async with create_session() as (pymux, state):
        job = pymux.jobs.submit("true", None, [("first", None)])
        await say(pymux, state, "set-tag -j -t %d second=2" % job.job_id)
        assert job.tags == {"first": None, "second": "2"}
        _printed, errors = await say(pymux, state, "set-tag -j x")
        assert errors and "job id" in errors[0], errors


async def test_a_listing_keeps_what_carries_every_tag():
    async with create_session() as (pymux, state):
        await say(pymux, state, "new-window -d --tag role=build --tag mine")
        await say(pymux, state, "new-window -d --tag role=logs")
        await say(pymux, state, "new-session -d -s other --tag owner=agent")
        printed, errors = await say(pymux, state, "list-windows --tag role -F #{window_tags}")
        assert not errors, errors
        assert sorted(printed) == ["role=build,mine", "role=logs"]
        printed, _errors = await say(pymux, state, "list-windows --tag role=build --tag mine -F #{window_tags}")
        assert printed == ["role=build,mine"]
        printed, _errors = await say(pymux, state, "list-sessions --tag owner=agent -F #{session_name}")
        assert printed == ["other"]
        printed, _errors = await say(pymux, state, "list-sessions --tag owner=person -F #{session_name}")
        assert printed == []


async def test_a_pane_tag_finds_it_in_any_session():
    async with create_session() as (pymux, state):
        await say(pymux, state, "new-session -d -s other")
        other = pymux.get_session("other")
        (pane,) = other.arrangement.windows[0].panes
        pane.tags["agent"] = "a1"
        printed, errors = await say(pymux, state, "list-panes --tag agent")
        assert not errors, errors
        assert printed == ["%%%d" % pane.pane_id]


async def test_split_window_tags_the_new_pane():
    async with create_session() as (pymux, state):
        printed, errors = await say(pymux, state, "split-window -d -P -F #{pane_tags} --tag agent=a1")
        assert not errors, errors
        assert printed == ["agent=a1"]


async def test_a_template_reads_the_tags_as_a_dictionary():
    async with create_session() as (pymux, state):
        _session, _window, pane = active(pymux, state)
        pane.tags.update({"agent": "a1"})
        printed, errors = await say(pymux, state, "display-message -p '{{ pane_tags.agent }}'")
        assert not errors, errors
        assert printed == ["a1"]


async def test_sql_reads_the_tags_under_live():
    async with create_session() as (pymux, state):
        session, window, pane = active(pymux, state)
        session.tags["s"] = None
        window.tags["w"] = "1"
        pane.tags["p"] = "2"
        for table, column, wanted in (
            ("session_tags", "session_id", [(session.session_id, "s", None)]),
            ("window_tags", "window_id", [(window.window_id, "w", "1")]),
            ("pane_tags", "pane_id", [(pane.pane_id, "p", "2")]),
        ):
            answer = await ask(pymux, "SELECT %s, key, value FROM live.%s" % (column, table), {}, 0, 0)
            assert answer.rows == wanted, table
