"""
A client's input after its pipe has closed. prompt_toolkit reads it once
more while the application exits: its reader's wrapper removes the
reader and calls it anyway. Under six suites the detach test met that
read and raised. Lillecarl/pyterm#1000, Lillecarl/pyterm#111.
"""

from __future__ import annotations

from pymux.connection.client_io import ClientInput


def test_a_closed_input_reads_no_keys():
    client_input = ClientInput(lambda _message: None)
    client_input.send_text("a")
    client_input.close()

    assert client_input.closed
    assert client_input.read_keys() == []
    assert client_input.flush_keys() == []
