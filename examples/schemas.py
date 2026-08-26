"""示例测试工程的配置 Schema(业务侧自定义,框架不预设任何字段)。

演示 ConfigLoader 的用法:业务项目按自己的被测系统定义 Pydantic 模型,
Loader 负责 YAML 多环境合并 + 环境变量覆盖 + 校验。
"""

from __future__ import annotations

from typing import Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AuthConfig(BaseModel):
    """HTTP 认证配置,字段与 atf.http.auth.build_auth 对齐。"""

    model_config = ConfigDict(extra="ignore")

    type: Literal["none", "token", "cookie", "apikey", "custom"] = "none"
    token: Optional[str] = None
    key: Optional[str] = None
    header: Optional[str] = None
    scheme: Optional[str] = None
    cookies: Dict[str, str] = Field(default_factory=dict)


class HttpConfig(BaseModel):
    """REST API 客户端配置。"""

    base_url: str = "http://127.0.0.1:8000"
    timeout: float = 10.0
    max_retries: int = 2
    retry_backoff: float = 0.5
    verify_ssl: bool = True
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)


class SSHJumpConfig(BaseModel):
    """SSH 跳板机配置。"""

    host: str
    port: int = 22
    username: str = "root"
    password: Optional[str] = None
    key_file: Optional[str] = None


class SSHConfig(BaseModel):
    """SSH 被测机配置(jump 为跳板隧道)。"""

    host: str = "127.0.0.1"
    port: int = 22
    username: str = "root"
    password: Optional[str] = None
    key_file: Optional[str] = None
    timeout: float = 10.0
    jump: Optional[SSHJumpConfig] = None


class PoolConfig(BaseModel):
    """资源池配置。"""

    path: str = "config/resource_pool.yaml"
    stale_timeout: float = 1800.0
    lock_timeout: float = 30.0


class GuardConfig(BaseModel):
    """共享 fixture 守护配置。"""

    state_file: str = ".state/guard.json"
    wait_ready_timeout: float = 600.0
    takeover_after: float = 300.0


class AppConfig(BaseModel):
    """示例工程总配置(由 ConfigLoader[AppConfig] 加载)。"""

    http: HttpConfig = Field(default_factory=HttpConfig)
    ssh: SSHConfig = Field(default_factory=SSHConfig)
    pool: PoolConfig = Field(default_factory=PoolConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)
