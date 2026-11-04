"""
OSC 7501 through a server: a client's terminal that answers the query
holds every pane's records, each under the pane it came from.
Lillecarl/pymux#927.
"""

from __future__ import annotations

from session import once
from test_a_session_on_a_pyte_screen import attached, pymux, shows  # noqa: F401
from test_an_osc_goes_with_the_frame import written

QUERY_ANSWER = "\x1b]7501;?:states=idle,working,done,blocked,error\x1b\\"


def report(body: str) -> str:
    return "\x1b]7501;%s\x1b\\" % body


async def test_a_terminal_that_keeps_records_gets_the_panes(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        connection = pymux.connections[-1]
        assert "\x1b]7501;?\x1b\\" in written(session.packets)
        connection.client.replies.arrived(QUERY_ANSWER)
        (pane,) = pymux.panes_by_id.values()
        relay = "pane%d" % pane.pane_id

        pane.terminal.terminal_control.feed_output(report("state=blocked:kind=permission:app=claude-code"))
        sent = report("state=blocked:id=%s:kind=permission:app=claude-code" % relay)
        await once(lambda: sent in written(session.packets), 10.0, "the record did not go out")
        # The first send clears what a server before this one left.
        wire = written(session.packets)
        assert wire.index(report("state=clear")) < wire.index(sent)

        pane.terminal.terminal_control.feed_output(
            report("state=working:id=build/test") + report("state=clear:id=build")
        )
        cleared = report("state=clear:id=%s/build/test" % relay)
        await once(lambda: cleared in written(session.packets), 10.0, "the clear did not go out")


async def test_a_prompt_takes_the_work_and_leaves_what_finished(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        pymux.connections[-1].client.replies.arrived(QUERY_ANSWER)
        (pane,) = pymux.panes_by_id.values()
        relay = "pane%d" % pane.pane_id

        pane.terminal.terminal_control.feed_output(report("state=working") + report("state=done:id=job"))
        pane.terminal.terminal_control.feed_output("\x1b]133;A\x1b\\")
        cleared = report("state=clear:id=%s" % relay)
        await once(lambda: cleared in written(session.packets), 10.0, "the prompt cleared nothing")
        # The child under the cleared root goes again.
        wire = written(session.packets)
        assert wire.rindex(report("state=done:id=%s/job" % relay)) > wire.index(cleared)


async def test_a_pyte_terminal_answers_the_query_itself(pymux):
    "The test's client draws on a pyte screen, which keeps records too."
    async with pymux.running(), attached(pymux):
        replies = pymux.connections[-1].client.replies
        await once(lambda: replies.program_status, 10.0, "the client's terminal did not answer")


async def test_a_terminal_that_did_not_answer_hears_no_record(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client = pymux.connections[-1].client
        # Its own answer first, so none can arrive after this.
        await once(lambda: client.replies.program_status, 10.0, "no answer")
        client.replies.program_status = False
        (pane,) = pymux.panes_by_id.values()
        before = len(written(session.packets))
        pane.terminal.terminal_control.feed_output(report("state=blocked") + "after-the-report")
        await shows(session, "after-the-report")
        assert "7501;state" not in written(session.packets)[before:]
        assert pane.screen.program_status.records
