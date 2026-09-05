"""Unit tests for configuration loading (ConfigRegistry)."""

from __future__ import annotations

import os

import pytest
import yaml
from pydantic import BaseModel
from testkit import ConfigRegistry
from testkit.exceptions import ConfigError


class CommonConfig(BaseModel):
    api_base_url: str
    timeout: float = 30.0
    admin_password: str = ""


class SshConfig(BaseModel):
    host: str
    port: int = 22


def _write(tmp_path, document: dict, name: str = "config.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _base_doc():
    return {
        "default": {
            "common": {"api_base_url": "http://default", "timeout": 30.0},
            "ssh": {"host": "1.2.3.4"},
        },
        "envs": {
            "staging": {"common": {"api_base_url": "http://staging"}},
        },
    }


def test_register_and_get_default(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["common"], CommonConfig, fixture_name="global_config")
    model = registry.get("global_config")
    assert isinstance(model, CommonConfig)
    assert model.api_base_url == "http://default"
    assert model.timeout == 30.0


def test_register_is_chainable(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["common"], CommonConfig, fixture_name="c").register(
        ["ssh"], SshConfig, fixture_name="s"
    )
    assert registry.get("c").api_base_url == "http://default"
    assert registry.get("s").host == "1.2.3.4"


def test_fixture_name_defaults_to_class_name(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["ssh"], SshConfig)
    assert isinstance(registry.get("SshConfig"), SshConfig)


def test_env_overlay_deep_merge(tmp_path, monkeypatch):
    path = _write(tmp_path, _base_doc())
    monkeypatch.setenv("TESTKIT_ENV", "staging")
    registry = ConfigRegistry(str(path))
    registry.register(["common"], CommonConfig, fixture_name="c")
    model = registry.get("c")
    assert model.api_base_url == "http://staging"  # overridden
    assert model.timeout == 30.0  # inherited from default


def test_explicit_env_constructor_wins_over_env_var(tmp_path, monkeypatch):
    path = _write(tmp_path, _base_doc())
    monkeypatch.setenv("TESTKIT_ENV", "staging")
    registry = ConfigRegistry(str(path), env="default")
    registry.register(["common"], CommonConfig, fixture_name="c")
    assert registry.get("c").api_base_url == "http://default"


def test_placeholder_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "tok-123")
    doc = {"default": {"common": {"api_base_url": "${MY_TOKEN}", "timeout": 30.0}}}
    path = _write(tmp_path, doc)
    registry = ConfigRegistry(str(path))
    registry.register(["common"], CommonConfig, fixture_name="c")
    assert registry.get("c").api_base_url == "tok-123"


def test_placeholder_default_value(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    doc = {"default": {"common": {"api_base_url": "${MISSING_VAR:-fallback}", "timeout": 1.0}}}
    path = _write(tmp_path, doc)
    registry = ConfigRegistry(str(path))
    registry.register(["common"], CommonConfig, fixture_name="c")
    assert registry.get("c").api_base_url == "fallback"


def test_placeholder_missing_without_default_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    doc = {"default": {"common": {"api_base_url": "${MISSING_VAR}", "timeout": 1.0}}}
    path = _write(tmp_path, doc)
    registry = ConfigRegistry(str(path))
    registry.register(["common"], CommonConfig, fixture_name="c")
    with pytest.raises(ConfigError):
        registry.get("c")


def test_config_file_not_found_raises(tmp_path):
    registry = ConfigRegistry(str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError):
        registry.get("x")


def test_section_not_found_raises(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["no_such_section"], CommonConfig, fixture_name="c")
    with pytest.raises(ConfigError):
        registry.get("c")


def test_unknown_fixture_name_raises(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    with pytest.raises(ConfigError):
        registry.get("unknown")


def test_validation_error_is_wrapped(tmp_path):
    doc = {"default": {"common": {"timeout": 30.0}}}  # missing required api_base_url
    path = _write(tmp_path, doc)
    registry = ConfigRegistry(str(path))
    registry.register(["common"], CommonConfig, fixture_name="c")
    with pytest.raises(ConfigError) as exc_info:
        registry.get("c")
    assert exc_info.value.original_exception is not None


def test_env_file_loading(tmp_path, monkeypatch):
    monkeypatch.delenv("FROM_DOTENV", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("FROM_DOTENV=hello\n# comment\n", encoding="utf-8")
    # ConfigRegistry calls load_env_file() with the default ".env"; we test the
    # loader directly via the module-level function for isolation.
    from testkit.config.loader import load_env_file

    injected = load_env_file(env_file)
    assert injected == {"FROM_DOTENV": "hello"}
    assert os.environ["FROM_DOTENV"] == "hello"


def test_env_file_existing_var_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("EXISTING", "original")
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=overwritten\n", encoding="utf-8")
    from testkit.config.loader import load_env_file

    injected = load_env_file(env_file)
    assert injected == {}  # not injected because already present
    assert os.environ["EXISTING"] == "original"
