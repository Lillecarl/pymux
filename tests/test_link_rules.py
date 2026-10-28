"""
Link rules: text the nearest `pymux.toml` makes a link of, which
`show-urls` labels with the other links. Lillecarl/pymux#776.
"""

from __future__ import annotations

from prompt_toolkit.application.current import set_app
from session import once
from test_a_session_on_a_pyte_screen import attached, pymux, said, shows  # noqa: F401

from pymux.link_rules import find_config, rules_for

ISSUES = """
[[links]]
match = '(?<![\\w/])#(\\d+)\\b'
url = "https://h.io/o/r/issues/{1}"
"""


def test_the_nearest_file_up_from_the_directory_holds_the_rules(tmp_path):
    (tmp_path / "repo" / "a" / "b").mkdir(parents=True)
    (tmp_path / "repo" / ".jj").mkdir()
    (tmp_path / "repo" / "pymux.toml").write_text(ISSUES)
    assert find_config(str(tmp_path / "repo" / "a" / "b")) == tmp_path / "repo" / "pymux.toml"
    (rule,) = rules_for(str(tmp_path / "repo" / "a")).rules
    assert rule.url == "https://h.io/o/r/issues/{1}"


def test_the_search_stops_at_the_top_of_a_repository(tmp_path):
    "A file above the repository is another project's."
    (tmp_path / "pymux.toml").write_text(ISSUES)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    assert find_config(str(tmp_path / "repo")) is None


def test_a_broken_rule_is_said_and_the_others_still_apply(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pymux.toml").write_text(
        ISSUES + '\n[[links]]\nmatch = "(unclosed"\nurl = "x"\n\n[[links]]\nmatch = "#(\\\\d+)"\nurl = "{2}"\n'
    )
    found = rules_for(str(tmp_path))
    assert len(found.rules) == 1
    assert [error.split(": ")[1] for error in found.errors] == ["link 2", "link 3"]


def test_a_file_that_changed_is_read_again(tmp_path):
    (tmp_path / ".git").mkdir()
    config = tmp_path / "pymux.toml"
    config.write_text(ISSUES)
    assert len(rules_for(str(tmp_path)).rules) == 1
    config.write_text("")
    assert rules_for(str(tmp_path)).rules == []


async def test_show_urls_labels_an_issue_number_and_opens_it(pymux, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pymux.toml").write_text(ISSUES)
    opened = []
    monkeypatch.setattr(pymux, "open_url", lambda url, confirmed=False: opened.append(url))
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        with set_app(pymux.connections[-1].client_state.app):
            pane = pymux.arrangement.get_active_pane()
        pane.current_directory = str(tmp_path)
        pane.terminal.terminal_control.feed_output("\r\nsee #579 now\r\n")
        await shows(session, "see #579 now")
        said(session, type="input", keys="C-b o")
        await shows(session, "see s579 now")
        said(session, type="input", keys="s")
        await once(lambda: opened, 10.0, "nothing was opened")
        assert opened == ["https://h.io/o/r/issues/579"]
