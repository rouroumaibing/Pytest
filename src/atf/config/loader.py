"""ConfigLoader:YAML + 环境变量 + Pydantic 校验,多环境支持。

设计要点(零业务耦合):

- 框架不预设任何配置项:调用方提供自己的 Pydantic Schema,
  Loader 只负责“读取 → 选环境 → 深合并 → 环境变量覆盖 → 校验”;
- YAML 约定结构::

      default: {...}          # 所有环境共享的基线
      default_env: dev        # 未指定环境时的默认环境(可省略,缺省取 envs 第一个)
      envs:
        dev: {...}
        qa: {...}
        prod: {...}

  合并顺序(后者覆盖前者):``default`` → 指定环境段 → 环境变量覆盖;

- 环境变量覆盖规则:前缀 ``ATF_``(可配)即视为配置覆盖项,
  双下划线 ``__`` 作为层级分隔符,值按 YAML 标量解析::

      ATF_HTTP__TIMEOUT=30        ->  {"http": {"timeout": 30}}
      ATF_SSH__JUMP__HOST=1.2.3.4 ->  {"ssh": {"jump": {"host": "1.2.3.4"}}}

- 环境选择优先级:显式入参 > 环境变量 ``ATF_ENV``(可配)>
  YAML ``default_env`` > ``envs`` 的第一个 key。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar, Union

import yaml
from pydantic import BaseModel, ValidationError

from atf.exceptions import ConfigError
from atf.utils.log import get_logger

T = TypeVar("T", bound=BaseModel)
_logger = get_logger("atf.config")


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并两个字典并返回新字典,``override`` 中的值优先。

    嵌套 dict 深合并;其余类型(含 list)整体替换。
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _parse_scalar(raw: str) -> Any:
    """把环境变量字符串按 YAML 标量解析(支持数字/布尔/null)。"""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


class ConfigLoader(Generic[T]):
    """泛型配置加载器,绑定调用方提供的 Pydantic Schema。"""

    def __init__(
        self,
        schema: Type[T],
        path: Union[str, Path] = "config/config.yaml",
        *,
        env_prefix: str = "ATF",
        env_separator: str = "__",
        env_var: str = "ATF_ENV",
    ) -> None:
        """初始化加载器。

        Args:
            schema: Pydantic ``BaseModel`` 子类,定义调用方自己的配置结构。
            path: YAML 配置文件路径。
            env_prefix: 环境变量覆盖项的前缀。
            env_separator: 环境变量中的层级分隔符。
            env_var: 选择环境的环境变量名。
        """
        self._schema = schema
        self._path = Path(path)
        self._env_prefix = env_prefix.upper() + "_"
        self._env_separator = env_separator
        self._env_var = env_var

    # ------------------------------------------------------------------ load

    def load(self, env: Optional[str] = None) -> T:
        """加载并校验配置。

        Args:
            env: 显式指定环境名;缺省时按环境变量 / YAML 默认值解析。

        Returns:
            校验通过的 Schema 实例。

        Raises:
            ConfigError: 文件缺失/非法、环境不存在或 Pydantic 校验失败。
        """
        raw = self._read_yaml()
        chosen = self._resolve_env(env, raw)
        merged = self._merge_raw(raw, chosen)
        try:
            return self._schema.model_validate(merged)
        except ValidationError as exc:
            raise ConfigError(
                f"config '{self._path}' failed schema validation (env={chosen}):\n{exc}"
            ) from exc

    def load_raw(self, env: Optional[str] = None) -> Dict[str, Any]:
        """返回合并 + 环境变量覆盖后、Pydantic 校验**前**的原始 dict。

        适用于不希望绑定 Pydantic Schema 的场景(纯 dict 配置、动态字段,
        或仅想读取某环境合并结果做二次处理)。

        Args:
            env: 显式指定环境名;缺省时按环境变量 / YAML 默认值解析。

        Returns:
            合并 + 覆盖后的配置 dict(未做 schema 校验)。
        """
        raw = self._read_yaml()
        chosen = self._resolve_env(env, raw)
        return self._merge_raw(raw, chosen)

    def _merge_raw(self, raw: Dict[str, Any], chosen: str) -> Dict[str, Any]:
        """读取 → 选环境 → 深合并 → 环境变量覆盖,返回校验前的 dict。"""
        merged = self._merge(raw, chosen)
        overrides = self._collect_env_overrides()
        if overrides:
            _logger.debug("env overrides: %s", list(overrides.keys()))
            merged = deep_merge(merged, overrides)
        return merged

    def list_envs(self) -> List[str]:
        """列出配置文件中定义的全部环境名。"""
        raw = self._read_yaml()
        return sorted((raw.get("envs") or {}).keys())

    # ------------------------------------------------------------- internals

    def _read_yaml(self) -> Dict[str, Any]:
        if not self._path.is_file():
            raise ConfigError(f"config file not found: {self._path}")
        try:
            data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in '{self._path}': {exc}") from exc
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"top-level YAML in '{self._path}' must be a mapping")
        return data

    def _resolve_env(self, env: Optional[str], raw: Dict[str, Any]) -> str:
        envs = raw.get("envs") or {}
        candidates = (
            env,
            os.environ.get(self._env_var),
            raw.get("default_env"),
            next(iter(envs), None) if envs else None,
        )
        chosen = next((c for c in candidates if c), None)
        if chosen is None:
            return "default"
        if envs and chosen not in envs:
            raise ConfigError(
                f"env '{chosen}' not defined in '{self._path}', available: {sorted(envs)}"
            )
        return chosen

    @staticmethod
    def _merge(raw: Dict[str, Any], env: str) -> Dict[str, Any]:
        base = copy.deepcopy(raw.get("default") or {})
        section = (raw.get("envs") or {}).get(env)
        if isinstance(section, dict):
            base = deep_merge(base, section)
        return base

    def _collect_env_overrides(self) -> Dict[str, Any]:
        tree: Dict[str, Any] = {}
        for name, value in os.environ.items():
            if not name.startswith(self._env_prefix) or name == self._env_var:
                continue
            keys = name[len(self._env_prefix):].lower().split(self._env_separator)
            node = tree
            for key in keys[:-1]:
                child = node.setdefault(key, {})
                if not isinstance(child, dict):
                    raise ConfigError(
                        f"environment variable '{name}' conflicts with a non-mapping config node"
                    )
                node = child
            node[keys[-1]] = _parse_scalar(value)
        return tree
