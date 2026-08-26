"""fixtures 子包:xdist 并发下的共享 fixture 守护。"""

from atf.fixtures.guard import SharedFixture, SharedFixtureGuard

__all__ = ["SharedFixture", "SharedFixtureGuard"]
