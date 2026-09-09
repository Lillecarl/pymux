"""
What every run of this suite shares: the examples a property test draws.

A property test with no seed is a different test every run, and a gate
that is red by luck teaches everybody to run it again instead of to
look. `pyte` learned that the hard way (Lillecarl/pymux#180), so the
gate here takes a pinned profile from the first property test this
suite has.

`pyte/tests/conftest.py` is the same section, with the groups of that
suite around it.
"""

from hypothesis import HealthCheck, settings

# The gate. `derandomize` seeds each property test from its own source,
# so a run draws the examples the run before it drew, and a green gate
# means the same thing twice.
#
# This is hypothesis's own `ci` profile, which it loads by itself when
# it recognises the runner. A nix sandbox is not one it recognises, so
# the profile is named and loaded here.
settings.register_profile(
    "pinned",
    derandomize=True,
    print_blob=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# The hunt. Fresh examples, which is what finds the case nobody has
# written down. `--hypothesis-profile=roaming` picks it. No check runs
# it yet: `checks.pyte-roaming` is the shape one takes when this suite
# has enough property tests to be worth a hunt of its own.
#
# `database=None` on purpose. A sandbox throws its `.hypothesis`
# directory away, so a database there only pretends to remember. What
# a suite remembers is the `@example` decorators on its property
# tests, and those run under both profiles.
settings.register_profile(
    "roaming",
    derandomize=False,
    database=None,
    print_blob=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# **Here, and not in a fixture.** A `@settings(...)` decorator reads
# the profile that is loaded when the decorator runs, which is when
# pytest imports the test module. pytest imports a conftest before
# that, and calls a fixture long after it.
settings.load_profile("pinned")
