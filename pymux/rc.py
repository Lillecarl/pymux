"""
Initial configuration.
"""

__all__ = ["STARTUP_COMMANDS"]

STARTUP_COMMANDS = """
bind-key '"' split-window -v
bind-key % split-window -h
bind-key c new-window
bind-key Right select-pane -R
bind-key Left select-pane -L
bind-key Up select-pane -U
bind-key Down select-pane -D
bind-key C-l select-pane -R
bind-key C-h select-pane -L
bind-key C-j select-pane -D
bind-key C-k select-pane -U
bind-key ; last-pane
bind-key ! break-pane
bind-key d detach-client
bind-key t clock-mode
bind-key Space next-layout
bind-key C-z suspend-client

bind-key z resize-pane -Z
bind-key k resize-pane -U 2
bind-key j resize-pane -D 2
bind-key h resize-pane -L 2
bind-key l resize-pane -R 2
bind-key q display-panes
bind-key C-Up resize-pane -U 2
bind-key C-Down resize-pane -D 2
bind-key C-Left resize-pane -L 2
bind-key C-Right resize-pane -R 2
bind-key M-Up resize-pane -U 5
bind-key M-Down resize-pane -D 5
bind-key M-Left resize-pane -L 5
bind-key M-Right resize-pane -R 5

# An overlay pane in the middle of the screen. "g" opens one with the
# default shell, and Escape closes it. (tmux binds neither.)
bind-key g display-popup
bind-key Escape close-popup

bind-key : command-prompt
bind-key 0 select-window -t :0
bind-key 1 select-window -t :1
bind-key 2 select-window -t :2
bind-key 3 select-window -t :3
bind-key 4 select-window -t :4
bind-key 5 select-window -t :5
bind-key 6 select-window -t :6
bind-key 7 select-window -t :7
bind-key 8 select-window -t :8
bind-key 9 select-window -t :9
bind-key n next-window
bind-key p previous-window
bind-key w choose-window
bind-key o select-pane -t :.+
bind-key { swap-pane -U
bind-key } swap-pane -D
bind-key x confirm-before -p "kill-pane #P?" kill-pane
bind-key & confirm-before -p "kill-window #W?" kill-window
bind-key C-o rotate-window
bind-key M-o rotate-window -D
bind-key C-b send-prefix
bind-key . command-prompt "move-window -t '%%'"
bind-key [ copy-mode
bind-key ] paste-buffer
bind-key ? list-keys
bind-key PPage copy-mode -u

# The strip. tmux has none of these, so there is no muscle memory to
# keep and the keys are a choice. Lillecarl/pymux#212.
#
# niri's own keys are Mod+BracketLeft and Mod+BracketRight for
# consume-or-expel, which behind a prefix are `{` and `}`. Those are
# tmux's `swap-pane`, and tmux's keys stay tmux's, so the pane moves on
# `<` and `>` instead: they point the way the pane goes.
#
# A column moves on `H` and `L`, which are vim's far left and far
# right, one case up from the `h` and `l` that resize a pane.
#
# `W` cycles the width of a column. One key is enough, because it is a
# cycle of three presets and not a step, and niri binds one key for the
# same reason.
bind-key < consume-or-expel -L
bind-key > consume-or-expel -R
bind-key H move-column -L
bind-key L move-column -R
bind-key W switch-column-width

# Layouts.
bind-key M-1 select-layout even-horizontal
bind-key M-2 select-layout even-vertical
bind-key M-3 select-layout main-horizontal
bind-key M-4 select-layout main-vertical
bind-key M-5 select-layout tiled

# Renaming stuff.
bind-key , command-prompt -I #W "rename-window '%%'"
#bind-key "'" command-prompt -I #W "rename-pane '%%'"
bind-key "'" command-prompt -p index "select-window -t ':%%'"

# The pane-management mode. One `M` enters it, and while it lasts the
# keys of moving a pane around are one press each, no prefix in
# between: hjkl and the arrows move the focus, ctrl moves the edges.
# The resize step is the prefix table's own, so the two feel the same.
# A key the table does not name still reaches the pane, so a person
# can keep working while they manage; q and Escape leave.
# Lillecarl/pymux#395.
bind-key M enter-mode pane-management
bind-key -T pane-management h select-pane -L
bind-key -T pane-management l select-pane -R
bind-key -T pane-management k select-pane -U
bind-key -T pane-management j select-pane -D
bind-key -T pane-management Left select-pane -L
bind-key -T pane-management Right select-pane -R
bind-key -T pane-management Up select-pane -U
bind-key -T pane-management Down select-pane -D
bind-key -T pane-management C-h resize-pane -L 2
bind-key -T pane-management C-l resize-pane -R 2
bind-key -T pane-management C-k resize-pane -U 2
bind-key -T pane-management C-j resize-pane -D 2
bind-key -T pane-management C-Left resize-pane -L 2
bind-key -T pane-management C-Right resize-pane -R 2
bind-key -T pane-management C-Up resize-pane -U 2
bind-key -T pane-management C-Down resize-pane -D 2
bind-key -T pane-management z resize-pane -Z
bind-key -T pane-management s swap-pane -U
bind-key -T pane-management o select-pane -t :.+
bind-key -T pane-management x confirm-before -p "kill-pane #P?" kill-pane
bind-key -T pane-management [ copy-mode
bind-key -T pane-management q leave-mode
bind-key -T pane-management Escape leave-mode
"""
