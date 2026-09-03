# testkit — 通用自动化测试框架设计文档

> 面向「REST API 管理面 + SSH 节点面」系统的通用 Python 测试框架。
> 零业务耦合，每个模块可独立使用。

---

## 1. 概览

| 维度 | 说明 |
|------|------|
| 定位 | 业务无关的测试基础设施层（testkit），不预设任何业务概念（node/cluster/organization） |
| 语言 / 运行时 | Python ≥ 3.10 |
| 技术栈 | pytest、pytest-xdist、requests、paramiko、pydantic、PyYAML、filelock |
| 核心抽象 | 管理面 = HTTP Client + 资源池 + 配置；节点面 = SSH Executor + 架构探测 |
| 交付形态 | 纯 Python 包，`pip install -e .` 后 `import testkit` 即可 |

### 目录结构

```
pytest/
├── pyproject.toml              # 打包元数据 + pytest 配置
├── docs/
│   ├── prompt.txt              # 原始需求提示词
│   └── DESIGN.md               # 本设计文档
├── testkit/                    # 框架包（零业务耦合）
│   ├── __init__.py             # 顶层导出 27 个符号
│   ├── exceptions.py           # §3 异常层级
│   ├── logging_setup.py        # §10 日志 verbosity
│   ├── config/
│   │   └── loader.py           # §7 配置加载 + ConfigRegistry
│   ├── pool/
│   │   └── resource_pool.py    # §2 资源池
│   ├── ssh/
│   │   └── executor.py         # §4 SSH 执行器
│   ├── http/
│   │   ├── auth.py             # §5 可插拔认证策略
│   │   └── client.py           # §5 HTTP 客户端
│   ├── fixture/
│   │   └── guard.py            # §6 并发 Fixture Guard
│   ├── cleanup/
│   │   └── resource_cleanup.py # §8 资源清理
│   ├── utils/
│   │   └── wait.py             # §9 WaitHelper
│   ├── model/
│   │   └── base.py             # §11 数据模型层
│   └── pipeline/
│       └── stage.py            # §12 多阶段 Pipeline
└── examples/
    ├── conftest.py             # ConfigRegistry fixture 绑定示例
    ├── config.yaml             # 多环境 YAML 示例
    └── test_example.py         # 17 个冒烟/单测示例
```

---

## 2. 资源池（Resource Pool）

**文件**：`testkit/pool/resource_pool.py` · **类**：`ResourcePool`

| 需求 | 实现要点 |
|------|---------|
| YAML 持久化 | 每次 `add`/`acquire`/`release` 后立即原子写回文件（tmp + `os.replace`） |
| 跨进程互斥 | `FileLock(str(path) + ".lock")`，pytest-xdist 多 worker 共享同一把锁 |
| 预操作 reload | 加锁后 `_read()` 重新从磁盘读取，**无内存缓存**，杜绝陈旧状态 |
| 重试 + 批量分配 | `acquire(count, predicate, retries, interval)`；锁内按 `count` 批量标记 |
| 通用结构 | 资源为通用 `dict`，仅约定 `status` 字段，其余字段自由（`id_field` 可配置） |
| **CRITICAL status** | 状态严格只有 `"free"` / `"allocated"`，`_validate_status` 拒绝一切其他取值 |

```python
class ResourcePool:
    def __init__(self, path: str | Path, retries: int = 3,
                 interval: float = 1.0, id_field: str = "id") -> None
    def add(self, resource: dict) -> None                      # 新增 free 资源
    def acquire(self, count: int = 1, predicate: Callable | None = None,
                retries: int | None = None, interval: float | None = None) -> list[dict]
    def release(self, resources: list[dict]) -> None           # 归还为 free
    def release_all(self) -> None
    def remove(self, resource_id: Any) -> None
    def list(self) -> list[dict]                               # 磁盘快照
```

---

## 3. 异常层级（Exception Hierarchy）

**文件**：`testkit/exceptions.py`

```
TestKitError（框架基类，携带 original_exception 保留异常链）
├── ConfigError        # 配置加载/校验
├── PoolError          # 资源池
├── HTTPError          # HTTP 客户端（含 status_code/response_data）
│   └── ResourceNotFoundError  # 404 自动转换（含 resource_type/resource_id）
├── SSHError           # SSH（含 command/exit_code/stderr）
├── FixtureError       # 并发 Fixture Guard
├── CleanupError       # 资源清理
└── PipelineError      # Pipeline 编排
```

关键机制：

- **context 字典**：`HTTPError` 自动附带 `status_code`、`response_data`、`method`、`path`；`SSHError` 附带 `command`、`exit_code`、`stderr`。异常 message 由 context 拼接生成。
- **404 → ResourceNotFoundError**：`HTTPClient` 在状态码 == 404 时抛出携带 `resource_type`/`resource_id` 的专用异常。
- **链式保留**：基类 `__init__` 接收 `original_exception`，`__str__` 追加 `caused by: ...`。

---

## 4. SSH 执行器（SSH Executor）

**文件**：`testkit/ssh/executor.py` · **类**：`SSHExecutor` / `SSHResult`

| 需求 | 实现要点 |
|------|---------|
| 双模式 | `mode="direct"` 直连；`mode="jump"` 跳板机 |
| 跳板机 direct-tcpip | 通过跳板机 `SSHClient.get_transport().open_channel("direct-tcpip", dest, src)` 建立隧道，**无中间 shell** |
| 结构化结果 | `SSHResult(command, stdout, stderr, exit_code, duration, ok)` |
| 日志脱敏 | `_sanitize` 将 password 替换为 `***` |
| 架构探测缓存 | `detect_arch()` 执行 `uname -m`，结果缓存在实例字段，仅首次执行 |

```python
@dataclass
class SSHResult:
    command: str; stdout: str; stderr: str
    exit_code: int; duration: float
    @property
    def ok(self) -> bool  # exit_code == 0

class SSHExecutor:
    def __init__(self, host, port=22, username=None, password=None,
                 key_filename=None, timeout=10.0,
                 jump_host=None, jump_port=22, jump_username=None,
                 jump_password=None, jump_key_filename=None) -> None
    def connect(self) -> None
    def execute(self, command: str, timeout: float | None = None,
                raise_on_error: bool = False) -> SSHResult
    def get_architecture(self, refresh: bool = False) -> str  # uname -m，结果缓存
    def close(self) -> None
```

---

## 5. HTTP 客户端与认证策略

**文件**：`testkit/http/auth.py` + `testkit/http/client.py`

### 认证策略（可插拔，统一接口）

```python
class AuthStrategy(ABC):
    def get_headers(self) -> dict[str, str]  # 每次请求实时读取
    def is_expired(self) -> bool             # 主动过期判断
    def refresh(self) -> None                # 主动/被动刷新
    def should_refresh_on_401(self, response) -> bool  # 被动刷新门控

class TokenAuth(AuthStrategy)     # access_token + expires_at + buffer_time + token_provider
class CookieAuth(AuthStrategy)    # cookie 键值对或原始 Cookie 字符串
class ApiKeyAuth(AuthStrategy)    # api key header（可自定义 header 名）
class CustomAuth(AuthStrategy)    # 用户自定义 header provider
```

### HTTP 客户端

| 需求 | 实现要点 |
|------|---------|
| 双 Token 刷新 | 主动：`auth.is_expired()` 含 `buffer_time` 提前判断 → `refresh()`；被动：401 时 `refresh()` 后重试一次 |
| 实时凭据 | 每次 `_request` 前调 `auth.get_credentials()`，**不缓存初始化时快照** |
| extra_headers | `_merge_headers` 按请求合并，大小写不敏感覆盖/追加，**不污染 Session** |
| 统一异常 | 区分 404 / 网络 / 超时 / 其他状态码，映射到 §3 层级 |
| **CRITICAL `_request_with_files`** | multipart 上传；**自动剥离 Content-Type**（交给 requests 生成 boundary）；支持 `base_url_override` 调用外部服务 |

```python
class HTTPClient:
    def __init__(self, base_url: str, auth: AuthStrategy | None = None,
                 session: requests.Session | None = None, timeout: float = 30.0) -> None
    def request(self, method, path, **kwargs) -> requests.Response
    def get/post/put/patch/delete(self, path, **kwargs)
    # CRITICAL: multipart + 自定义 base_url
    def _request_with_files(self, method, path, file=None, data=None,
                            extra_headers=None, base_url_override=None) -> requests.Response
```

---

## 6. 并发 Fixture Guard（Concurrent Fixture Guard）

**文件**：`testkit/fixture/guard.py` · **类**：`ConcurrentFixtureGuard`

| 需求 | 实现要点 |
|------|---------|
| JSON state + FileLock | `state.json` + `state.json.lock`，跨进程协调 |
| 首 worker 创建，其余等待复用 | `should_create` 返回 `True` 仅首个调用者 |
| 超时接管 | `wait_until_created` 内检查 `created_at` 超过阈值 → 视为崩溃 → `mark_failed` 触发接管 |
| 仅创建者清理 | `cleanup` 检查 `is_creator()`，非创建者不删 |
| 独占临界区 | `exclusive()` 上下文管理器，多进程串行执行 |

**CRITICAL 核心方法**（全部显式提供）：

```python
class ConcurrentFixtureGuard:
    def should_create(self, metadata: dict | None = None) -> bool   # 首调用 True，其余 False
    def mark_created(self, metadata: dict | None = None) -> None    # 创建者资源构建完成后调用
    def mark_failed(self, metadata: dict | None = None) -> None     # 创建失败调用（REQUIRED）
    def is_creator(self) -> bool                                     # 本进程是否创建者
    def wait_until_created(self, check_fn: Callable[[], bool], timeout: float) -> bool
        # 非创建者轮询用户定义的 readiness 检查，check_fn 非仅查 state 文件
        # 例如：lambda: api.get_cluster_status() == "Available"
    def cleanup(self) -> None                                        # 删除 state 文件（仅创建者）
    def exclusive(self) -> "ConcurrentFixtureGuard"                  # 独占临界区
```

---

## 7. 配置加载（ConfigRegistry）

**文件**：`testkit/config/loader.py` · **类**：`ConfigRegistry`

| 需求 | 实现要点 |
|------|---------|
| YAML 多环境合并 | `default` + `envs.<env>` deep merge |
| `${VAR}` 占位符 | 递归替换为环境变量值 |
| .env 加载 | 注入 `os.environ`，**已存在环境变量优先** |
| Pydantic 校验 | 类型安全 + 默认值 + 必填校验 |
| **CRITICAL ConfigRegistry** | `register()` 建立 `yaml_path → Pydantic model` 映射，绑定 pytest fixture |

```python
class ConfigRegistry:
    def __init__(self, config_path: str, env: str | None = None) -> None
    def register(self, yaml_path: list[str], model_cls: type[BaseModel],
                 fixture_name: str | None = None) -> None
        # yaml_path=["common"] 加载 dynamic.yaml 的 common 段
        # model_cls: Pydantic 校验模型；fixture_name: 绑定 fixture
    def get(self, name: str) -> BaseModel       # 按 fixture_name 取已加载配置
    def load(self, yaml_path: list[str]) -> dict
```

conftest 用法（见 §14）：

```python
REGISTRY = ConfigRegistry("config.yaml")
REGISTRY.register(["common"], CommonConfig, fixture_name="global_config")
REGISTRY.register(["ssh"], SshConfig, fixture_name="ssh_config")

@pytest.fixture(scope="session")
def global_config() -> CommonConfig:
    return REGISTRY.get("global_config")
```

---

## 8. 资源清理（Resource Cleanup）

**文件**：`testkit/cleanup/resource_cleanup.py` · **类**：`ResourceCleanup`

| 需求 | 实现要点 |
|------|---------|
| LIFO | 后注册先清理，保持依赖逆序 |
| 异步资源等待 | 清理函数返回 `Awaitable`/`(obj, done_predicate)` 时先等待完成 |
| 自动重试 | `retry_count` + `retry_interval` |
| 失败不阻断 | 单资源失败仅记录，继续清理后续资源 |
| skip_cleanup_on_failure | 测试失败时跳过清理，保留现场 |

```python
class ResourceCleanup:
    def __init__(self, retry_count: int = 3, retry_interval: float = 1.0,
                 skip_cleanup_on_failure: bool = False) -> None
    def register(self, cleanup_fn: Callable[[], Any],
                 wait_fn: Callable[[], bool] | None = None,
                 name: str | None = None, timeout: float = 300.0) -> "ResourceCleanup"
    def cleanup(self, failed: bool = False) -> list[CleanupResult]
```

---

## 9. WaitHelper（异步轮询）

**文件**：`testkit/utils/wait.py` · **类**：`WaitHelper` / `WaitTimeout`

| 需求 | 实现要点 |
|------|---------|
| 泛型 `until()` | 轮询条件函数直至返回期望值或超时 |
| 异常容忍 | 条件抛异常不中断轮询，继续等待 |
| 轮询中刷新凭据 | 周期性调用 `refresh_fn`（`refresh_interval` 秒一次，如刷新即将过期的 Token） |
| 就绪判断外置 | 框架不预设业务状态机，由调用方定义 |

```python
class WaitHelper:
    def __init__(self, timeout: float = 60.0, interval: float = 1.0,
                 refresh_fn: Callable[[], None] | None = None,
                 refresh_interval: float = 300.0) -> None
    def until(self, condition: Callable[[], Any], expected: Any = <sentinel>,
              timeout: float | None = None, interval: float | None = None) -> Any
    def until_true(self, condition: Callable[[], Any],
                   timeout: float | None = None, interval: float | None = None) -> Any
```

---

## 10. 日志 Verbosity

**文件**：`testkit/logging_setup.py`

| 需求 | 实现要点 |
|------|---------|
| k8s `--v=N` 模式 | `log_verbosity` 单点控制 |
| 自定义级别 | `V2`（API 摘要）、`V4`（完整响应体）、`V5`（trace） |
| pytest handler = NOTSET | 级别设为 `0`，避免二次过滤丢弃低于 DEBUG 的自定义级别 |
| 敏感脱敏 | password/token/key 替换为 `***` |

```python
def setup_logging(log_verbosity: int = 0, level: int | None = None,
                  fmt: str | None = None) -> logging.Logger
def get_logger(name: str | None = None) -> logging.Logger
def sanitize(text: Any) -> Any
```

---

## 11. 数据模型层（Data Model）

**文件**：`testkit/model/base.py` · **类**：`BaseModel` / `Builder`

| 需求 | 实现要点 |
|------|---------|
| Builder 链式 | `with_xxx` 返回 `self`，`build()` 输出标准 dict |
| from_api_response | API dict → 对象属性自动映射 + 便捷属性（如 `id = metadata.uid`） |
| Client-Model 分离 | 模型不持有 Client 引用 |
| 不吞异常 | `from_api_response` 类型错误正常抛出，不静默重置 |

```python
class Builder:
    def __init__(self) -> None
    def _with(self, key: str, value: Any) -> "Builder"   # 子类 with_xxx 委托于此
    def build(self) -> dict[str, Any]                      # 返回新 dict 快照

class BaseModel:
    _fields: ClassVar[list[str]] = []          # 直接从响应拷贝的属性
    _aliases: ClassVar[dict[str, str]] = {}    # attr -> "metadata.uid" 嵌套路径

    @classmethod
    def from_api_response(cls, response: dict) -> "BaseModel"
    def to_dict(self) -> dict[str, Any]
```

---

## 12. 多阶段 Pipeline 编排

**文件**：`testkit/pipeline/stage.py` · **类**：`Pipeline` / `StageResult`

| 需求 | 实现要点 |
|------|---------|
| 串行执行 | 按序执行，任一失败后续 stage 跳过 |
| resume_from 断点续跑 | 从指定 stage 继续，跳过已完成 stage |
| skipped 标记 | 跳过结果标 `"skipped"`，不影响最终成功判定 |
| 无状态 | 不引入 checkpoint 文件，恢复点由用户指定 |

```python
@dataclass
class StageResult:
    name: str
    status: str  # "passed" | "failed" | "skipped"
    error: Exception | None = None
    data: Any = None

    @property
    def passed(self) -> bool
    @property
    def failed(self) -> bool
    @property
    def skipped(self) -> bool

class Pipeline:
    def __init__(self, name: Optional[str] = None) -> None
    def add_stage(self, name: str, fn: Callable[..., Any]) -> "Pipeline"
    def stage(self, name: str) -> Callable  # 装饰器形式
    def run(self, resume_from: str | None = None,
            context: Any = None) -> list[StageResult]
    @property
    def success(self) -> bool
```

---

## 13. 技术栈与通用约束

- **依赖**：pytest、pytest-xdist、requests、paramiko、pydantic、PyYAML、filelock
- **零业务耦合**：全包无 node/cluster/organization 等预设概念
- **完整类型注解 + 完整 docstring**：所有公开方法与模块均覆盖

---

## 14. 示例用法

### conftest.py（ConfigRegistry fixture 绑定）

```python
from pydantic import BaseModel
from testkit import ConfigRegistry

class CommonConfig(BaseModel):
    api_base: str
    timeout: int = 30

REGISTRY = ConfigRegistry("config.yaml")
REGISTRY.register(["common"], CommonConfig, fixture_name="global_config")

@pytest.fixture(scope="session")
def global_config() -> CommonConfig:
    return REGISTRY.get("global_config")
```

### config.yaml（多环境合并）

```yaml
default:
  common:
    api_base: "https://api.example.com"
    timeout: 30
envs:
  staging:
    common:
      api_base: "https://staging-api.example.com"
```

### 测试用例（综合各模块）

```python
def test_create_cluster(global_config, ssh_config):
    client = HTTPClient(global_config.api_base, auth=TokenAuth("${TOKEN}"))
    guard = ConcurrentFixtureGuard("/tmp/cluster_fixture.json")

    if guard.should_create():
        try:
            resp = client.post("/clusters", json={...})
            cluster_id = resp.json()["id"]
            guard.mark_created(metadata={"id": cluster_id})
        except Exception:
            guard.mark_failed()
            raise
    else:
        assert guard.wait_until_created(
            lambda: client.get("/clusters").json()["status"] == "Available",
            timeout=120,
        )
```

---

## 15. 验证结果（Verification Gates）

| 门禁 | 命令 | 结果 |
|------|------|------|
| 语法编译 | `python -m py_compile testkit/**/*.py tests/**/*.py` | ✅ PY_COMPILE_OK |
| 单元测试 | `pytest` | ✅ **156 passed**（tests/ 139 + examples/ 17） |
| 并发运行 | `pytest -n 4` | ✅ **156 passed**（4 workers） |
| 包导入 | `import testkit` | ✅ version 0.1.0，27 符号可解析 |

> 说明：本项目为纯 Python 框架，验证门禁为 `py_compile` + `pytest`（含 xdist 并发）；不涉及 `go build / vet / test` 或 `tsc`（Go/TS gate 仅适用于 sounds-great-ai 主仓库）。

---

## 16. 代码规模统计

| 模块 | 文件 | 行数 |
|------|------|------|
| HTTP Client | `http/client.py` | 305 |
| SSH Executor | `ssh/executor.py` | 273 |
| Fixture Guard | `fixture/guard.py` | 216 |
| Config Loader | `config/loader.py` | 214 |
| Resource Pool | `pool/resource_pool.py` | 204 |
| Logging | `logging_setup.py` | 179 |
| Resource Cleanup | `cleanup/resource_cleanup.py` | 166 |
| HTTP Auth | `http/auth.py` | 159 |
| Pipeline | `pipeline/stage.py` | 158 |
| Exceptions | `exceptions.py` | 145 |
| WaitHelper | `utils/wait.py` | 127 |
| Data Model | `model/base.py` | 89 |
| 顶层导出 + 各 `__init__` | `__init__.py` 等 | 148 |
| **框架包合计** | | **2,379 行** |
| 示例（conftest + config + test） | `examples/*` | 296 |
| 单元测试 | `tests/*` | 1,394 |
