"""
A server that shows the panes of another server, and the server that
lets it. Lillecarl/pymux#759.

The other server owns each pane it lends: its screen, its program and
its size. A peer holds a mirror, a pane whose screen takes only what the
owner sends: a freeze to start from, then every read the owner's screen
takes, with the ground timer's decision beside it. A mirror is a pane
for everything else, so it can be lent on again, and servers chain.
"""
