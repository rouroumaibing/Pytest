"""Configuration loading: YAML multi-environment merge, placeholder
substitution, ``.env`` injection and Pydantic validation.

The :class:`ConfigRegistry` maps YAML sections to Pydantic models and binds
them to pytest fixtures by name. Example::

    REGISTRY = ConfigRegistry("config.yaml")
    REGISTRY.register(["common"], CommonConfig, fixture_name="global_config")
    REGISTRY.register(["ssh"], SshConfig, fixture_name="ssh_config")
"""

from __future__ import annotations

import os
import re
import typing
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from testkit.exceptions import ConfigError
from testkit.logging_setup import get_logger

logger = get_logger("config")

_PLACEHOLDER_RE = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")

_ENV_OVERRIDE = "TESTKIT_ENV"


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into a copy of *base*.

    Dict values are merged recursively; every other type is replaced.
    """
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def substitute_placeholders(value: Any, env: Mapping[str, str]) -> Any:
    """Recursively replace ``${VAR}`` / ``${VAR:-default}`` with env values."""
    if isinstance(value, str):

        def _repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise ConfigError(
                "environment variable not set and no default provided",
                variable=name,
            )

        return _PLACEHOLDER_RE.sub(_repl, value)
    if isinstance(value, dict):
        return {k: substitute_placeholders(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_placeholders(v, env) for v in value]
    return value


def load_env_file(path: str | Path = ".env") -> dict[str, str]:
    """Load a simple ``KEY=VALUE`` ``.env`` file into ``os.environ``.

    Existing environment variables take precedence and are never overwritten.
    Returns the mapping of newly injected variables.
    """
    env_path = Path(path)
    if not env_path.exists():
        return {}

    injected: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value
            injected[key] = value
    return injected


# -- template generation helpers ---------------------------------------------

_PLACEHOLDERS = {
    str: "<string>",
    int: "<int>",
    float: "<float>",
    bool: "<bool>",
}


def _unwrap_optional(annotation: Any) -> Any:
    """Strip ``Optional``/``Union`` to the first non-None argument."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if non_none:
            return non_none[0]
    return annotation


def _scalar_placeholder(annotation: Any) -> str:
    unwrapped = _unwrap_optional(annotation)
    if unwrapped in _PLACEHOLDERS:
        return _PLACEHOLDERS[unwrapped]
    return "<value>"


def _annotation_to_template(annotation: Any) -> Any:
    """Render a single field's type into a template placeholder or sub-dict."""
    unwrapped = _unwrap_optional(annotation)
    if isinstance(unwrapped, type) and issubclass(unwrapped, BaseModel):
        return _model_to_template(unwrapped)
    return _scalar_placeholder(unwrapped)


def _model_to_template(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Recursively render a Pydantic model into a template dict."""
    result: dict[str, Any] = {}
    for field_name, field in model_cls.model_fields.items():
        if field.default_factory is not None and field.default_factory is not PydanticUndefined:
            try:
                result[field_name] = field.default_factory()
                continue
            except Exception:  # noqa: BLE001 - fall back to a placeholder
                pass
        if field.default is not PydanticUndefined:
            result[field_name] = field.default
        else:
            result[field_name] = _annotation_to_template(field.annotation)
    return result


def _set_path(root: dict[str, Any], path: list[str], value: Any) -> None:
    """Assign *value* at the nested *path* inside *root* (creating dicts)."""
    cur = root
    for part in path[:-1]:
        cur = cur.setdefault(part, {})
    cur[path[-1]] = value


class ConfigRegistry:
    """Registry of ``YAML section path -> Pydantic model`` bindings.

    Parameters
    ----------
    yaml_path:
        Path to the YAML file containing ``default`` and ``envs`` sections.
    env:
        Active environment name. Falls back to the ``TESTKIT_ENV`` environment
        variable, then to ``"default"`` (no per-env overlay).
    """

    def __init__(self, yaml_path: str | Path, env: str | None = None) -> None:
        self._yaml_path = Path(yaml_path)
        self._env = env or os.environ.get(_ENV_OVERRIDE) or "default"
        # Ordered registrations: (yaml_path, model_cls, original fixture_name).
        # ``original fixture_name`` is ``None`` for global sections that should
        # always be included in generated templates regardless of fixture usage.
        self._registrations: list[tuple[list[str], type[BaseModel], str | None]] = []
        self._by_fixture: dict[str, int] = {}
        self._models: dict[str, BaseModel] = {}
        self._raw: dict[str, Any] | None = None

    def register(
        self,
        yaml_path: list[str],
        model_cls: type[BaseModel],
        fixture_name: str | None = None,
    ) -> ConfigRegistry:
        """Register a YAML section path to a Pydantic model.

        Parameters
        ----------
        yaml_path:
            Path into the merged configuration, e.g. ``["common"]``.
        model_cls:
            Pydantic model used to validate the section.
        fixture_name:
            Optional name used by :meth:`get`. Defaults to ``model_cls.__name__``.
            When ``None``, the section is treated as global config and always
            included by :meth:`generate_template`.

        Returns
        -------
        ConfigRegistry
            ``self``, to allow chaining.
        """
        name = fixture_name or model_cls.__name__
        self._registrations.append((list(yaml_path), model_cls, fixture_name))
        self._by_fixture[name] = len(self._registrations) - 1
        logger.v2("registered config section %r -> %s", yaml_path, model_cls.__name__)
        return self

    def _load_raw(self) -> dict[str, Any]:
        """Load, merge and substitute placeholders in the YAML document."""
        if self._raw is not None:
            return self._raw

        if not self._yaml_path.exists():
            raise ConfigError("config file not found", path=str(self._yaml_path))

        load_env_file()
        with self._yaml_path.open("r", encoding="utf-8") as fh:
            document = yaml.safe_load(fh) or {}

        if not isinstance(document, dict):
            raise ConfigError("config root must be a mapping", path=str(self._yaml_path))

        default = document.get("default", {})
        envs = document.get("envs", {})
        if not isinstance(default, dict) or not isinstance(envs, dict):
            raise ConfigError(
                "config must contain 'default' and 'envs' mappings",
                path=str(self._yaml_path),
            )

        merged = default
        if self._env != "default":
            overlay = envs.get(self._env, {})
            if not isinstance(overlay, dict):
                raise ConfigError("env overlay must be a mapping", env=self._env)
            merged = _deep_merge(default, overlay)
            logger.v2("merged environment overlay env=%s", self._env)

        self._raw = substitute_placeholders(merged, os.environ)
        return self._raw

    def _extract(self, path: list[str]) -> Any:
        raw = self._load_raw()
        current: Any = raw
        for part in path:
            if not isinstance(current, dict) or part not in current:
                raise ConfigError("config section not found", path=path, missing=part)
            current = current[part]
        return current

    def get(self, fixture_name: str) -> BaseModel:
        """Return the validated model bound to *fixture_name*.

        Validation happens lazily on first access and the result is cached.
        """
        if fixture_name in self._models:
            return self._models[fixture_name]
        if fixture_name not in self._by_fixture:
            raise ConfigError("unknown fixture name", fixture_name=fixture_name)

        path, model_cls, _ = self._registrations[self._by_fixture[fixture_name]]
        section = self._extract(path)
        try:
            model = model_cls.model_validate(section)
        except ValidationError as exc:
            raise ConfigError(
                "config validation failed",
                fixture_name=fixture_name,
                path=path,
                errors=exc.errors(),
                original_exception=exc,
            ) from exc

        self._models[fixture_name] = model
        return model

    # -- template generation (reverse of validation) -------------------------

    def generate_template(self, used_fixtures: set[str] | None = None) -> dict[str, Any]:
        """Render a ``dynamic.yaml``-style template from registered models.

        Parameters
        ----------
        used_fixtures:
            Optional set of pytest fixture names in use. When provided, a
            section is included only if its registered ``fixture_name`` is in
            the set. Sections registered without a fixture name (global config)
            are always included.

        Returns
        -------
        dict[str, Any]
            Nested template dict mirroring the registered YAML paths.
        """
        template: dict[str, Any] = {}
        for path, model_cls, fixture_name in self._registrations:
            if used_fixtures is not None and fixture_name is not None:
                if fixture_name not in used_fixtures:
                    continue
            section = _model_to_template(model_cls)
            _set_path(template, path, section)
        return template

    def dump_template(
        self,
        path: str | Path,
        used_fixtures: set[str] | None = None,
    ) -> None:
        """Write the generated template to *path* as a loadable ``dynamic.yaml``.

        The template is wrapped under a ``default:`` key (with an empty ``envs:``)
        so the file can be fed straight back into :class:`ConfigRegistry`.
        """
        rendered = {"default": self.generate_template(used_fixtures=used_fixtures), "envs": {}}
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(rendered, fh, sort_keys=False, default_flow_style=False)
        logger.v2("dumped config template -> %s", path)
