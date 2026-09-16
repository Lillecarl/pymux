# tmux compatibility decisions

Every deliberate difference from tmux, one row per entry in
`tests/reference/tmux_compat/divergences.toml` -- the ledger, whose
rule is `unlisted_divergences_are_bugs`: a behavior that differs from
tmux and holds no row here is a bug, not a choice. The evidence
transcripts of the probes against the pinned tmux live beside the
tests, in `tests/fixtures/tmux37/`. Lillecarl/pymux#385.

| divergence | status | the choice | the reason |
| --- | --- | --- | --- |
| `swap-pane -U` / `-D` direction | intentional-divergence | pymux's `-U` swaps with the pane below and `-D` with the pane above; tmux's `-U` is up, `-D` down. Both flips recorded under one entry. | pymux's own help reads "below" and the pane-management mode follows it (#395); flipping is a break in one direction or the other, and the ledger holds the choice until #384's harness makes matching tmux the cheap default. Probe: tmux 3.7c. Lillecarl/pymux#400. |
