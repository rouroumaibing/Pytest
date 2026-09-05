"""testkit — a generic, business-agnostic Python test framework.

The framework targets systems composed of two planes:

* a **REST API management plane** (HTTP client, authentication, resource pool)
* an **SSH node plane** (command executor over direct or jump-host connections)

Every module is designed to be usable independently with zero business
coupling: there are no preset concepts such as "cluster", "node", or
"organization" anywhere in the package.
"""

from testkit.cleanup.resource_cleanup import ResourceCleanup
from testkit.config.loader import ConfigRegistry
from testkit.exceptions import (
    CleanupError,
    ConfigError,
    FixtureError,
    HTTPError,
    PipelineError,
    PoolError,
    ResourceNotFoundError,
    SSHError,
    TestKitError,
)
from testkit.fixture.guard import ConcurrentFixtureGuard
from testkit.http.auth import (
    ApiKeyAuth,
    AuthStrategy,
    CookieAuth,
    CustomAuth,
    TokenAuth,
)
from testkit.http.client import HTTPClient
from testkit.model.base import BaseModel, Builder
from testkit.pipeline.stage import Pipeline, StageResult
from testkit.pool.resource_pool import ResourcePool
from testkit.ssh.executor import SSHExecutor, SSHResult
from testkit.utils.wait import WaitHelper, WaitTimeout

__all__ = [
    # exceptions
    "TestKitError",
    "ConfigError",
    "PoolError",
    "HTTPError",
    "HttpTimeoutError",
    "NetworkError",
    "ResourceNotFoundError",
    "SSHError",
    "FixtureError",
    "CleanupError",
    "PipelineError",
    # config
    "ConfigRegistry",
    # pool
    "ResourcePool",
    # ssh
    "SSHExecutor",
    "SSHResult",
    # http
    "AuthStrategy",
    "TokenAuth",
    "CookieAuth",
    "ApiKeyAuth",
    "CustomAuth",
    "HTTPClient",
    # fixture
    "ConcurrentFixtureGuard",
    # cleanup
    "ResourceCleanup",
    # utils
    "WaitHelper",
    "WaitTimeout",
    # model
    "BaseModel",
    "Builder",
    # pipeline
    "Pipeline",
    "StageResult",
]

__version__ = "0.1.0"
