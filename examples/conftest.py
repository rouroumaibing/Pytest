"""Example conftest.py — configuration loading via ConfigRegistry.

Shows how to register YAML sections to Pydantic models and bind them to
pytest fixtures by name.
"""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel, Field

from testkit import ConfigRegistry


class CommonConfig(BaseModel):
    """The ``common`` section of config.yaml."""

    api_base_url: str
    timeout: float = 30.0
    admin_password: str = ""


class SshConfig(BaseModel):
    """The ``ssh`` section of config.yaml."""

    host: str
    port: int = 22
    username: str = "root"


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

REGISTRY = ConfigRegistry(CONFIG_PATH)
REGISTRY.register(["common"], CommonConfig, fixture_name="global_config")
REGISTRY.register(["ssh"], SshConfig, fixture_name="ssh_config")


@pytest.fixture(scope="session")
def global_config() -> CommonConfig:
    return REGISTRY.get("global_config")


@pytest.fixture(scope="session")
def ssh_config() -> SshConfig:
    return REGISTRY.get("ssh_config")
