"""
The body hashes the arrangement once a render, and on every ask between
renders, when a handler may have changed it.
"""

from __future__ import annotations

from types import SimpleNamespace

from prompt_toolkit.application import DummyApplication
from prompt_toolkit.application.current import set_app

from pymux.layout import DynamicBody


class Arrangement:
    def __init__(self):
        self.shape = ("one pane",)
        self.asked = 0

    def invalidation_hash(self):
        self.asked += 1
        return self.shape


def make_body(monkeypatch):
    arrangement = Arrangement()
    body = DynamicBody(SimpleNamespace(arrangement=arrangement))
    monkeypatch.setattr(body, "_build_layout", object)
    return body, arrangement


def test_a_render_hashes_the_arrangement_once(monkeypatch):
    body, arrangement = make_body(monkeypatch)
    app = DummyApplication()
    with set_app(app):
        body.frame_started(app)
        for _ in range(5):
            body._get_body()
        body.frame_ended(app)
    assert arrangement.asked == 1


def test_a_change_between_renders_is_seen(monkeypatch):
    "A handler that splits a pane asks for the panes before the next render."
    body, arrangement = make_body(monkeypatch)
    app = DummyApplication()
    with set_app(app):
        body.frame_started(app)
        before = body._get_body()
        body.frame_ended(app)

        arrangement.shape = ("two panes",)
        assert body._get_body() is not before


def test_the_next_render_hashes_again(monkeypatch):
    body, arrangement = make_body(monkeypatch)
    app = DummyApplication()
    with set_app(app):
        body.frame_started(app)
        before = body._get_body()
        body.frame_ended(app)

        arrangement.shape = ("two panes",)
        body.frame_started(app)
        assert body._get_body() is not before
        body.frame_ended(app)
