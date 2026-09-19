"""Generic utilities package."""

from testkit.utils.parallel import parallel_map
from testkit.utils.wait import WaitHelper, WaitTimeout

__all__ = ["WaitHelper", "WaitTimeout", "parallel_map"]
