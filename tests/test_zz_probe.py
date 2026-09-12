from prompt_toolkit.application.current import set_app
from session import create_session, in_a_loop


@in_a_loop
async def test_zz_probe():
    async with create_session() as (pymux, state):
        with set_app(state.app):
            def dump(label):
                print("PROBE", label, [(w.window_id, w.index, len(w.panes)) for w in pymux.arrangement.windows])
            dump("start")
            pymux.handle_command("split-window -v 'sleep 30'")
            dump("after split")
            pymux.handle_command("new-window 'sleep 30'")
            dump("after new-window")
            source = pymux.arrangement.windows[0]
            print("PROBE source panes:", [p.pane_id for p in source.panes])
            pane = source.panes[-1]
            destination = pymux.arrangement.windows[1]
            pymux.handle_command("move-pane -s %%%d -t @%d" % (pane.pane_id, destination.window_id))
            dump("after move-pane")
        assert False
