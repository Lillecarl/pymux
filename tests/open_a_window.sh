# Run a Wayland program on the compositor of the pymux server this pane
# belongs to: the arguments, or foot running `cat` when there are none,
# so what is typed at the window comes back on it.
#
# The session of a client that attached from a desktop keeps that
# desktop's WAYLAND_DISPLAY, so the server's own is named here: it sits
# beside the server's socket, which $PYMUX names. `pymux/wayland.py`,
# `socket_path_of`. Lillecarl/pymux#620.
socket=${PYMUX%,*}
WAYLAND_DISPLAY="$(dirname "$socket")/wayland-$(basename "$socket").sock"
export WAYLAND_DISPLAY
if [ $# -gt 0 ]; then
    exec "$@"
fi
exec foot --override=main.font="DejaVu Sans Mono:size=11" \
    sh -c 'printf "a Wayland window in a pymux pane\n"; exec cat'
