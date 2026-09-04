Pymux
=====

*A terminal multiplexer (like tmux) in Python*

Pymux is written on top of `prompt_toolkit 3.0
<https://github.com/jonathanslenders/python-prompt-toolkit>`_ and requires
Python 3.11 or newer.

.. image :: https://raw.githubusercontent.com/jonathanslenders/pymux/master/images/pymux.png


Issues, questions, wishes, comments, feedback, remarks? Please create a GitHub
issue, I appreciate it.


Driving pymux from python
-------------------------

``libpymux`` drives a running server over its socket, with the object
model that libtmux uses: a server holds sessions, a session holds
windows, a window holds panes.

.. code:: python

    from libpymux import Server

    server = Server.first()
    pane = server.session.active_window.active_pane
    pane.send_keys("echo hello")
    print(pane.capture())

See ``libpymux/README.md``. Pymux also implements the tmux command line,
so libtmux itself can drive it through a ``tmux`` shim; see
``tests/drive_with_libtmux.py``.


Installation
------------

Simply install ``pymux`` using pip:

::

    pip install pymux

Start it by typing ``pymux``.

When using `Nix <https://nixos.org/>`_, build it with:

::

    nix build --file . package
    ./result/bin/pymux

The command line is tmux compatible. Tools like `libtmux
<https://github.com/tmux-python/libtmux>`_ can drive pymux:

::

    from libtmux import Server
    server = Server(tmux_bin="pymux", socket_path="/tmp/pymux.sock")
    session = server.new_session(session_name="work", attach=False)


What does it do?
----------------

A terminal multiplexer makes it possible to run multiple applications in the
same terminal. It does this by emulating a vt100 terminal for each application.
There are serveral programs doing this. The most famous are `GNU Screen
<https://www.gnu.org/software/screen/>`_ and `tmux <https://tmux.github.io/>`_.

Pymux is written entirely in Python. It doesn't need any C extension. It
requires Python 3.11+. It works on OS X and Linux.


Compared to tmux
----------------

To some extent, pymux is a clone of tmux. This means that all the default
shortcuts are the same; the commands are the same or very similar, and even a
simple configuration file could be the same. (There are some small
incompatibilities.) However, we definitely don't intend to create a fully
compatible clone. Right now, only a subset of the command options that tmux
provides are supported.

Pymux implements a few improvements over tmux:

- There is a completion menu for the command line. (At the bottom of the screen.)
- The command line has `fish-style <http://fishshell.com/>`_ suggestions.
- Both Emacs and Vi key bindings for the command line and copy buffer are well
  developed, thanks to all the effort we have put earlier in `prompt_toolkit
  <https://github.com/jonathanslenders/python-prompt-toolkit>`_.
- Search in the copy buffer is highlighted while searching.
- Every pane has its own titlebar.
- When several clients are attached to the same session, each client can watch
  a different window. When clients are watching different windows, every client
  uses the full terminal size.
- Support for 24bit true color. (Disabled by default: not all terminals support
  it. Use the ``--truecolor`` option at startup or during attach in order to
  enable it.)
- Support for unicode input and output. Pymux correctly understands utf-8
  encoded double width characters. (Also for the titlebars.)

About the performance:

- Tmux is written in C, which is obviously faster than Python. This is
  noticeable when applications generate a lot of output. Where tmux is able to
  give fast real-time output for, for instance ``find /`` or ``yes``, pymux
  will process the output slightly slower, and in this case render the output
  only a few times per second to the terminal. Usually, this should not be an
  issue. If it is, `Pypy <http://pypy.org/>`_ should provide a significant
  speedup.

The big advantage of using Python and `prompt_toolkit
<https://github.com/jonathanslenders/python-prompt-toolkit>`_ is that the
implementation of new features becomes very easy.


More screenshots
----------------

24 bit color support and the autocompletion menu:

.. image :: https://raw.githubusercontent.com/jonathanslenders/pymux/master/images/menu-true-color.png

What happens if another client with a smaller screen size attaches:

.. image :: https://raw.githubusercontent.com/jonathanslenders/pymux/master/images/multiple-clients.png

When a pane enters copy mode, search results are highlighted:

.. image :: https://raw.githubusercontent.com/jonathanslenders/pymux/master/images/copy-mode.png


Why create a tmux clone?
------------------------

For several reasons. Having a terminal multiplexer in Python makes it easy to
experiment and implement new features. While C is a good language, it's not as
easy to develop as Python.

Just like `pyvim <https://github.com/jonathanslenders/pyvim>`_ (A ``Vi`` clone
in Python.), it started as another experiment. A project to challenge the
design of prompt_toolkit. At this point, however, pymux should be stable and
usable for daily work.

The development resulted in many improvements in prompt_toolkit, especially
performance improvements, but also some functionality improvements.

Further, the development is especially interesting, because it touches so many
different areas that are unknown to most Python developers. It also proves that
Python is a good tool to create terminal applications.


The roadmap
-----------

There is no official roadmap, the code is mostly written for the fun and of
course, time is limited, but I use pymux professionally and I'm eager to
implement new ideas.

Some ideas:

- Support for color schemes.
- Support for extensions written in Python.
- Better support for scripting. (Right now, it's already possible to run pymux
  commands from inside the shell of a pane. E.g. ``pymux split-window``.
  However, status codes and feedback aren't transferred yet.)
- Improved mouse support. (Reporting of mouse movement.)
- Parts of pymux could become a library, so that any prompt_toolkit application
  can embed a vt100 terminal. (Imagine a terminal emulator embedded in `pyvim
  <https://github.com/jonathanslenders/pyvim>`_.)
- Maybe some cool widgets to traverse the windows and panes.
- Better autocompletion.


Configuring
-----------

Create a file ``~/.pymux.conf``, and populate it with commands, like you can
enter at the command line. There is an `example config
<https://github.com/jonathanslenders/pymux/blob/master/examples/example-config.conf>`_
in the examples directory.


What if it crashes?
-------------------

If for some reason pymux crashes, it will attempt to write a stack trace to a
file with a name like ``/tmp/pymux.crash-*``. It is possible that the user
interface freezes. Please create a GitHub issue with this stack trace.


Special thanks
--------------

- `Pyte <https://github.com/selectel/pyte>`_, for providing a working vt100
  parser. (This one is extended in order to support some xterm extensions.)
- `docopt-ng <https://github.com/jazzband/docopt-ng>`_, for parsing the
  options of the pymux commands.
- `prompt_toolkit
  <https://github.com/jonathanslenders/python-prompt-toolkit>`_, for the UI
  toolkit.
- `wcwidth <https://github.com/jquast/wcwidth>`_: for better unicode support
  (support of double width characters).
- `tmux <https://tmux.github.io/>`_, for the inspiration.
