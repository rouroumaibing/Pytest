"""ConfigLoader 单元测试。"""

from __future__ import annotations

import pytest
import yaml
from pydantic import BaseModel

from atf.config import ConfigLoader
from atf.exceptions import ConfigError


class _Db(BaseModel):
    host: str = "127.0.0.1"
    port: int = 5432
    debug: bool = False


class _Schema(BaseModel):
    db: _Db = _Db()
    replicas: int = 1


YAML_TEXT = """
default:
  db:
    host: base.example.com
    port: 5432
  replicas: 1

default_env: dev

envs:
  dev:
    db:
      host: dev.example.com
  prod:
    db:
      host: prod.example.com
      port: 6543
    replicas: 3
"""


@pytest.fixture
def cfg_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(YAML_TEXT, encoding="utf-8")
    return path


class TestLoad:
    def test_default_env_selected(self, cfg_file):
        cfg = ConfigLoader(_Schema, cfg_file).load()
        assert cfg.db.host == "dev.example.com"  # default_env: dev
        assert cfg.db.port == 5432  # 继承 default 段

    def test_explicit_env(self, cfg_file):
        cfg = ConfigLoader(_Schema, cfg_file).load("prod")
        assert cfg.db.host == "prod.example.com"
        assert cfg.db.port == 6543
        assert cfg.replicas == 3

    def test_unknown_env_rejected(self, cfg_file):
        with pytest.raises(ConfigError, match="not defined"):
            ConfigLoader(_Schema, cfg_file).load("staging")

    def test_list_envs(self, cfg_file):
        assert ConfigLoader(_Schema, cfg_file).list_envs() == ["dev", "prod"]

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            ConfigLoader(_Schema, tmp_path / "nope.yaml").load()


class TestValidation:
    def test_validation_error_wrapped(self, tmp_path):
        bad_yaml = YAML_TEXT.replace("port: 5432", "port: not-a-number")
        path = tmp_path / "c.yaml"
        path.write_text(bad_yaml)
        with pytest.raises(ConfigError, match="validation"):
            ConfigLoader(_Schema, path).load()

    def test_malformed_yaml(self, tmp_path):
        path = tmp_path / "c.yaml"
        # 冒号后紧跟换行且缩进非法 → 真正的 YAML 解析错误
        path.write_text("\n  bad indent:\n    - item1\n  - orphan\n")
        with pytest.raises(ConfigError, match="invalid YAML"):
            ConfigLoader(_Schema, path).load()

    def test_empty_file_uses_schema_defaults(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("")
        cfg = ConfigLoader(_Schema, path).load()
        assert cfg.db.host == "127.0.0.1"
        assert yaml.__name__ == "yaml"  # sanity:导入未被误删


class TestLoadRaw:
    def test_returns_merged_dict_without_validation(self, cfg_file):
        raw = ConfigLoader(_Schema, cfg_file).load_raw("prod")
        assert raw["db"]["host"] == "prod.example.com"
        assert raw["db"]["port"] == 6543
        assert raw["replicas"] == 3  # 合并结果,未做 schema 校验

    def test_raw_bypasses_validation(self, cfg_file):
        # load_raw 不做 Pydantic 校验,但返回的是合并后的真实值
        raw = ConfigLoader(_Schema, cfg_file).load_raw("prod")
        assert raw["db"]["port"] == 6543
        # 同样输入走 load() 通过校验
        cfg = ConfigLoader(_Schema, cfg_file).load("prod")
        assert cfg.replicas == 3
