"""Deeper unit tests for :class:`ConfigRegistry` and template generation.

These complement ``test_config.py`` by exercising template generation,
dump/reload round-trips, ``used_fixtures`` filtering, env overrides, the
validation-error path and ``.env`` loading.
"""

from __future__ import annotations

import os

import pytest
import yaml
from pydantic import BaseModel, Field
from testkit import ConfigRegistry
from testkit.config.loader import load_env_file
from testkit.exceptions import ConfigError


class CommonConfig(BaseModel):
    api_base_url: str
    timeout: float = 30.0


class SshConfig(BaseModel):
    host: str
    port: int = 22


# -- template model fixtures ------------------------------------------------


class Inner(BaseModel):
    name: str = "inner-name"
    count: int = 0
    ratio: float = 1.0
    flag: bool = True


class Outer(BaseModel):
    label: str  # required -> emits the "<string>" placeholder
    inner: Inner  # nested model -> recursive template
    tags: list[str] = Field(default_factory=list)  # default_factory field


class PlaceholderConfig(BaseModel):
    s: str
    i: int
    f: float
    b: bool


class GlobalConfig(BaseModel):
    region: str = "default-region"


class ServiceAConfig(BaseModel):
    url: str = "http://a"


class ServiceBConfig(BaseModel):
    url: str = "http://b"


class ReqConfig(BaseModel):
    name: str  # required, omitted in the YAML section


# -- helpers ----------------------------------------------------------------


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


# -- register / get ----------------------------------------------------------


def test_register_returns_self_for_chaining(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    ret = registry.register(["common"], CommonConfig, fixture_name="c")
    assert ret is registry


def test_get_caches_model(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["common"], CommonConfig, fixture_name="c")
    m1 = registry.get("c")
    m2 = registry.get("c")
    assert m1 is m2  # same cached instance


def test_get_unknown_fixture_raises(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    with pytest.raises(ConfigError):
        registry.get("does_not_exist")


def test_get_validation_error_carries_errors(tmp_path):
    doc = {"default": {"req": {"timeout": 5.0}}}  # missing required `name`
    path = _write(tmp_path, doc)
    registry = ConfigRegistry(str(path))
    registry.register(["req"], ReqConfig, fixture_name="req")
    with pytest.raises(ConfigError) as exc_info:
        registry.get("req")
    assert "errors" in exc_info.value.context
    assert isinstance(exc_info.value.context["errors"], list)
    assert exc_info.value.original_exception is not None
    # The required-field name is reported in the pydantic errors.
    assert any(e["loc"] == ("name",) for e in exc_info.value.context["errors"])


# -- env override ------------------------------------------------------------


def test_env_override_via_constructor(tmp_path, monkeypatch):
    monkeypatch.delenv("TESTKIT_ENV", raising=False)
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path), env="staging")
    registry.register(["common"], CommonConfig, fixture_name="c")
    assert registry.get("c").api_base_url == "http://staging"


def test_env_override_via_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTKIT_ENV", "staging")
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["common"], CommonConfig, fixture_name="c")
    assert registry.get("c").api_base_url == "http://staging"


def test_explicit_env_argument_beats_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTKIT_ENV", "staging")
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path), env="default")
    registry.register(["common"], CommonConfig, fixture_name="c")
    assert registry.get("c").api_base_url == "http://default"


# -- generate_template -------------------------------------------------------


def test_generate_template_default_values_and_placeholders(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["outer"], Outer, fixture_name="outer")
    template = registry.generate_template()
    assert template["outer"]["label"] == "<string>"
    assert template["outer"]["inner"] == {
        "name": "inner-name",
        "count": 0,
        "ratio": 1.0,
        "flag": True,
    }
    assert template["outer"]["tags"] == []


def test_generate_template_scalar_placeholder_types(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["ph"], PlaceholderConfig, fixture_name="ph")
    template = registry.generate_template()
    assert template["ph"] == {
        "s": "<string>",
        "i": "<int>",
        "f": "<float>",
        "b": "<bool>",
    }


def test_generate_template_nested_recursion(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["outer"], Outer, fixture_name="outer")
    template = registry.generate_template()
    # The nested model is rendered recursively, not as a placeholder.
    assert isinstance(template["outer"]["inner"], dict)
    assert template["outer"]["inner"]["name"] == "inner-name"


def test_generate_template_default_factory_field(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["outer"], Outer, fixture_name="outer")
    template = registry.generate_template()
    # default_factory(list) -> empty list, not a placeholder.
    assert template["outer"]["tags"] == []


def test_generate_template_used_fixtures_filters_non_matching(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["globals"], GlobalConfig, fixture_name=None)  # always included
    registry.register(["svc", "a"], ServiceAConfig, fixture_name="a")
    registry.register(["svc", "b"], ServiceBConfig, fixture_name="b")

    template = registry.generate_template(used_fixtures={"a"})
    assert "globals" in template  # global section always emitted
    assert "svc" in template
    assert "a" in template["svc"]
    assert "b" not in template["svc"]


def test_generate_template_used_fixtures_none_includes_all(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["globals"], GlobalConfig, fixture_name=None)
    registry.register(["svc", "a"], ServiceAConfig, fixture_name="a")
    registry.register(["svc", "b"], ServiceBConfig, fixture_name="b")

    template = registry.generate_template(used_fixtures=None)
    assert "globals" in template
    assert "a" in template["svc"]
    assert "b" in template["svc"]


def test_generate_template_global_always_included_even_when_filtering(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["globals"], GlobalConfig, fixture_name=None)
    registry.register(["svc", "a"], ServiceAConfig, fixture_name="a")
    template = registry.generate_template(used_fixtures=set())
    # Empty fixture set: no-name global still present, named fixture absent.
    assert "globals" in template
    assert "svc" not in template


# -- dump_template (write + reload) -----------------------------------------


def test_dump_template_writes_under_default_key(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["globals"], GlobalConfig, fixture_name=None)
    registry.register(["outer"], Outer, fixture_name="outer")
    out = tmp_path / "dynamic.yaml"
    registry.dump_template(out)

    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert "default" in doc
    assert doc.get("envs") == {}
    assert "outer" in doc["default"]
    assert "globals" in doc["default"]


def test_dump_template_round_trips_back_into_registry(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["globals"], GlobalConfig, fixture_name=None)
    registry.register(["outer"], Outer, fixture_name="outer")
    out = tmp_path / "dynamic.yaml"
    registry.dump_template(out)

    # Re-load the dumped template with a fresh registry and validate it.
    reloaded = ConfigRegistry(str(out))
    reloaded.register(["globals"], GlobalConfig, fixture_name=None)
    reloaded.register(["outer"], Outer, fixture_name="outer")
    assert reloaded.get("outer").label == "<string>"
    assert reloaded.get("outer").inner.name == "inner-name"
    # Global (fixture_name=None) sections are looked up by model class name.
    assert reloaded.get("GlobalConfig").region == "default-region"


# -- load_env_file -----------------------------------------------------------


def test_load_env_file_injects_new_var(tmp_path, monkeypatch):
    monkeypatch.delenv("FROM_DOTENV", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FROM_DOTENV=hello\n# a comment\n\nKEY2=value2\n",
        encoding="utf-8",
    )
    injected = load_env_file(env_file)
    assert injected == {"FROM_DOTENV": "hello", "KEY2": "value2"}
    assert os.environ["FROM_DOTENV"] == "hello"
    assert os.environ["KEY2"] == "value2"


def test_load_env_file_skips_existing_var(tmp_path, monkeypatch):
    monkeypatch.setenv("EXISTING", "original")
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=overwritten\nNEW=added\n", encoding="utf-8")
    injected = load_env_file(env_file)
    assert injected == {"NEW": "added"}
    assert os.environ["EXISTING"] == "original"


def test_load_env_file_skips_comments_and_blanks(tmp_path, monkeypatch):
    monkeypatch.delenv("K1", raising=False)
    monkeypatch.delenv("K2", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# full line comment\n"
        "   \n"  # whitespace-only line (skipped)
        "K1=v1\n"
        "noequalsign\n"  # no '=' -> skipped
        "K2=v2\n",
        encoding="utf-8",
    )
    injected = load_env_file(env_file)
    assert injected == {"K1": "v1", "K2": "v2"}


def test_load_env_file_strips_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOTED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('QUOTED="some value"\n', encoding="utf-8")
    load_env_file(env_file)
    assert os.environ["QUOTED"] == "some value"


# -- ported from test_config.py (get-time placeholder substitution + error paths) --


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


def test_fixture_name_defaults_to_class_name(tmp_path):
    path = _write(tmp_path, _base_doc())
    registry = ConfigRegistry(str(path))
    registry.register(["ssh"], SshConfig)
    assert isinstance(registry.get("SshConfig"), SshConfig)


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
