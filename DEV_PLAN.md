# ATF — 通用 REST API + SSH 自动化测试框架 设计文档

> **状态**:§9 库化重构已完成,§7 技术债路线图 7 项全部完成。122/122 框架自测通过(单进程 + xdist -n 4);`pip install -e .` 可用;`examples/` 为可 clone 改造的使用模板(业务示范 + 它自己的 pytest.ini)。
> **备注**:原"唯一待办"§9 与 §7 路线图均已收口,框架进入稳定维护态。

---

## 1. 定位与原则

- **目标系统**:REST API + SSH 被测系统,多进程(xdist)并发测试
- **零业务耦合**:六个模块均可 `from atf.xxx import Y` 独立使用
- **技术栈**:pytest 9.1 / pytest-xdist 3.8 / requests 2.34 / paramiko 5.0 / pydantic 2.13 / PyYAML / filelock 3.32
- **Python**:代码兼容 ≥3.9,当前验证在 3.14
- **交付形态**(§9 详述):**独立框架库**(pip 包),业务示范在 examples/ 不在 tests/

---

## 2. 目标目录结构(§9 重构后)

```
atf-project/                        本仓库 = 框架库 + 框架测试
├── pyproject.toml                 [库] testpaths=["tests"], pythonpath=["src"]
├── DEV_PLAN.md                    本文档
├── README.md                      用户指南
├── src/atf/                       [库] 框架源码(§3 详述)
│   ├── __init__.py                导出 ATFError, TestContext, __version__
│   ├── exceptions.py              异常树(§5)
│   ├── context.py                 TestContext(§3.7)
│   ├── config/loader.py           ConfigLoader(§3.5)
│   ├── resources/pool.py          ResourcePool(§3.1)
│   ├── ssh/executor.py            SSHExecutor(§3.2)
│   ├── http/{auth,client,exceptions}.py  BaseClient(§3.3)
│   ├── fixtures/guard.py          SharedFixtureGuard(§3.4)
│   └── utils/{log,sanitizer,retry}.py    工具层(§3.6)
├── tests/                         [库] 框架自身测试(104 个,§6 矩阵)
│   ├── unit/                      100 个离线单元测试
│   └── integration/               4 个 xdist 集成测试(种子数据内联)
├── examples/                      [文档] 可 clone 改造的使用模板
│   ├── pytest.ini                 examples 独立 pytest 配置
│   ├── conftest.py                六模块总装示范(session/function fixtures)
│   ├── schemas.py                 业务侧 Pydantic 模型示范(AppConfig)
│   ├── tests/test_sut_examples.py 真实环境示例(ATF_SUT=1 门控,否则 skip)
│   └── config/{config.yaml,resource_pool.yaml}
└── .venv/                         虚拟环境(依赖已装齐)
```

---

## 3. 模块详细设计

### 3.1 ResourcePool(`src/atf/resources/pool.py`)

**用途**:跨进程争抢同一批被测资源(主机/账号/License/容器)。

**YAML 结构**(每条资源扁平,保留键 `id/state/owner/locked_at`,业务字段平铺):
```yaml
resources:
  - id: node-01
    state: free                    # free | busy | disabled
    owner: null
    locked_at: null                # ISO8601,僵尸检测用
    host: 10.0.0.11               # ↓ 任意业务字段
    role: storage
    tags: [ssd, 10g]
```

**公开 API**:
```python
class ResourceState(str, Enum):
    FREE = "free"; BUSY = "busy"; DISABLED = "disabled"

def default_owner() -> str             # "hostname:pid"

class ResourcePool:
    def __init__(self, path, *, stale_timeout: float = 1800.0, lock_timeout: float = 30.0)
    def find(self, *, query=None, filter=None) -> List[Dict]
    def get(self, resource_id: str) -> Dict
    def stats(self) -> Dict[str, int]  # {"free": n, "busy": n, "disabled": n}
    def add(self, data: Dict, *, resource_id=None) -> Dict
    def remove(self, resource_id: str, *, force: bool = False) -> Dict
    def set_enabled(self, resource_id: str, enabled: bool) -> None
    def acquire(self, *, query=None, filter=None, owner=None,
                retries: int = 0, interval: float = 1.0) -> Dict
    def acquire_batch(self, count: int, *, query=None, filter=None, owner=None,
                      retries=0, interval=1.0) -> List[Dict]
    def release(self, resource_id: str, *, owner=None) -> Dict
    def release_all(self, owner: str) -> List[str]
```

**acquire 完整流程**:
1. `_critical()` 获取 FileLock(超时 → ResourcePoolError)
2. `_load()` 读 YAML → `_reap_stale()`:遍历 BUSY 记录,`locked_at` 距今超过 `stale_timeout` 则置 FREE 并清 owner(僵尸回收)
3. 遍历,取第一条 `state==FREE` 且 query/filter 都匹配的记录
4. `_mark(record, owner)`:置 BUSY/owner/locked_at=utcnow
5. `_save()`:原子写入(tmp + os.replace)
6. 若无可用且 retries>0 → 释放锁 → RetryPolicy(结果谓词 `r is None`)→ 回到 1
7. 耗尽 → `ResourceExhaustedError`

**acquire_batch 原子性**:同一临界区内挑选 N 个;凑不齐则**一个都不占**,返回 None 进重试。

**query/filter 语义**:`query` 为子集匹配(嵌套 dict 递归比较),`filter` 为任意谓词 `Callable[[Dict], bool]`,两者 AND。

**release owner 校验**:owner 不匹配 → `ResourceStateError`,防误释放他人资源。

**⚠️ 边界**:FileLock 基于 POSIX fcntl,仅保证**同机同文件系统**互斥;NFS/网络盘不保证。

### 3.2 SSHExecutor(`src/atf/ssh/executor.py`)

**公开 API**:
```python
class SSHConnectError(SSHError): ...    # 连接失败
class SSHCommandError(SSHError):        # check=True 且 exit≠0; .result: CommandResult
class SSHTransferError(SSHError): ...   # SFTP 失败
class SSHTimeoutError(SSHError): ...    # 命令超时

class SSHTarget:                        # dataclass
    host: str; port: int = 22; username: str = "root"
    password=None; key_file=None; key_passphrase=None; timeout: float = 10.0
    via: SSHTarget | None = None        # 跳板(仅一层,不支持链式)

class CommandResult:
    command; exit_code; stdout; stderr; duration
    ok -> bool                          # exit_code == 0
    lines -> List[str]                  # stdout 按行切分

class SSHExecutor:
    def __init__(self, target, *, sanitizer=None, keepalive: int = 15)
    connected -> bool                   # transport.is_active()
    def connect(self) -> SSHExecutor    # 幂等
    def close(self) -> None             # 幂等
    def run(self, command, *, timeout=None, check=True, env=None) -> CommandResult
    def upload(self, local_path, remote_path) -> None
    def download(self, remote_path, local_path) -> None
    def detect_arch(self) -> str        # uname -m
    def probe_system(self) -> Dict[str, str]   # hostname/kernel/arch/os
    __enter__ / __exit__

def shell_quote(text: str) -> str       # POSIX 单引号转义
```

**跳板隧道**:跳板机 `open_channel("direct-tcpip", (目标, 端口), ("127.0.0.1", 0))` → 该 channel 作为 socket 传给目标机 `client.connect(sock=chan)`。

**命令执行**:`bash -lc <shell_quote(命令)>`;env 注入为 `export K='v'` 前缀(值加引号);select 轮询 stdout/stderr;deadline 超时 → SSHTimeoutError 并关闭 channel。

**脱敏**:命令进入日志前过 Sanitizer(输出不脱敏,仅命令本身)。

**⚠️ 边界**:线程不安全(每线程一个实例);跳板仅一层。

### 3.3 BaseClient(`src/atf/http/`)

**auth.py — 可插拔认证**:
```python
class AuthStrategy(ABC):
    def apply(self, session) -> None         # 每次请求前注入凭证
    def refresh(self, session) -> bool       # 401 时续期;False=不支持

class TokenAuth(AuthStrategy):   # token, header="Authorization", scheme="Bearer"|None, refresh_fn
class CookieAuth(AuthStrategy):  # cookies: Mapping, refresh_fn
class ApiKeyAuth(AuthStrategy):  # key, header="X-API-Key"
class CustomAuth(AuthStrategy):  # hook: (Session)->None, refresh_hook
def build_auth(spec: Mapping) -> Optional[AuthStrategy]   # 从 YAML dict 构造
```

**client.py**:
```python
class BaseClient:
    def __init__(self, base_url, *, auth=None, extra_headers=None, timeout=10.0,
                 retry=None, verify=True, session=None, sanitizer=None,
                 log_body_bytes: int = 512)
    def request(self, method, path, *, params=None, json_body=None, data=None,
                headers=None, timeout=None, retry=None,
                raise_for_status=None, **kwargs) -> requests.Response
    def get/post/put/patch/delete(path, **kw)
    def close(); __enter__/__exit__
```

**request() 流程**:拼 URL → `_send_with_retry`(认证注入 + 网络重试)→ 若 401 且 `auth.refresh()` 成功则重发一次(独立于普通重试)→ `_log_summary` 一行摘要(方法/URL/状态/耗时/截断脱敏的请求响应体)→ `raise_for_status`(默认开,可按请求关)。

**重试语义(默认 retries=2, interval=0.5, backoff=2.0)**:触发 = 网络异常(ApiTransportError)+ 状态码 {408, 425, 429, 502, 503, 504};其余 4xx/5xx 不重试。可按请求覆盖 `retry=`。

**异常**:`ApiTransportError`(.reason 原始异常)/ `ApiTimeoutError` / `ApiHTTPStatusError`(.response 完整响应)。

### 3.4 SharedFixtureGuard(`src/atf/fixtures/guard.py`)

**JSON 状态文件**(空状态直接删文件):
```json
{"version": 1, "fixtures": {"<name>": {
  "state": "creating|ready", "owner": "gw0", "pid": 12345, "host": "ci-01",
  "created_at": 1690000000.0, "value": <create返回值>,
  "holders": [{"owner","pid","host","at"}]
}}}
```

**公开 API**:
```python
class SharedFixture:      # name, role("creator"|"user"), value

class SharedFixtureGuard:
    def __init__(self, state_file, *, lock_timeout=30.0, wait_ready_timeout=600.0,
                 takeover_after=None, holder_stale_after=3600.0, poll_interval=0.2)
    def shared(self, name, create, teardown=None, *, owner=None) -> Iterator[SharedFixture]
    def exclusive(self, name, *, owner=None) -> Iterator[None]
    def entries(self) -> Dict[str, Dict];  def reset(self, name)
```

**shared() 状态机**:
```
_enter():
  ├─ 无条目 / holders 全死        → creator
  ├─ state=ready                  → user(登记 holder)
  ├─ creating 且 owner==自己      → creator
  └─ 其余(他人在创建)           → waiter

creator: 执行 create → 校验 JSON 可序列化 → 锁内发布 ready
         create 失败 → 删条目防死锁(下个进程可重建)
waiter : 轮询 _enter() 直到 user/creator 或超时(wait_ready_timeout → FixtureGuardError)
exit   : 锁内移除自己的 holder → 空则在锁内执行 teardown 并删条目
```

**接管(_normalize)**:creating + PID 死(`os.kill(pid,0)`,跨主机视为活)→ 重建;creating + 超 `takeover_after` → 重建;ready + holders 全死/超 `holder_stale_after` → 回收重建。

**exclusive()**:`{state_file}.{name}.exlock` 独立 FileLock,无引用计数。

### 3.5 ConfigLoader(`src/atf/config/loader.py`)

```python
class ConfigLoader(Generic[T]):        # T = 业务侧 Pydantic BaseModel
    def __init__(self, schema, path="config/config.yaml",
                 *, env_prefix="ATF", env_separator="__", env_var="ATF_ENV")
    def load(self, env=None) -> T
    def list_envs(self) -> List[str]
def deep_merge(base, override) -> Dict  # 递归合并,list 整体替换
```

**YAML**:`default`(基线)+ `default_env`(可选)+ `envs.{name}`(覆盖段)。
**合并顺序**:default → 环境段 → `ATF_` 环境变量(`__` 分层,值按 YAML 标量解析,`ATF_HTTP__TIMEOUT=30` → http.timeout=30)。
**环境优先级**:入参 > `ATF_ENV` > `default_env` > envs 第一个。校验失败 → ConfigError(附 Pydantic 详情)。

### 3.6 Utils

- **Sanitizer**:`mask_text`(kv / JSON / Bearer-Basic / PEM 四类正则)、`mask_mapping`(深拷贝遮蔽)、`add_keywords` 运行期扩词根;默认词根含 password/token/secret/api_key 等。`DEFAULT_SANITIZER` 进程级实例。
- **RetryPolicy**:`execute(func, description=, is_retryable=)`,异常通道(exceptions 匹配)+ 结果谓词通道双通道;退避 `min(interval*backoff^(n-1), max_interval)`;耗尽抛 `RetryExhaustedError(ATFError)`。
- **log**:`setup_logging(level, log_file)`(幂等)+ `get_logger(name)`;格式含 `[%(process)d]` 供 xdist 排障。

### 3.7 TestContext(`src/atf/context.py`)

```python
class CleanupReport:    # executed: List[str], failures: List[(desc, err)], duration; ok -> bool

class TestContext:      # __test__ = False(防 pytest 误收集)
    def register(self, value, finalizer=None, *, description="") -> T   # 返回 value
    def add_finalizer(self, func, *, description="") -> None
    def cleanup(self) -> CleanupReport   # LIFO、错误隔离收集、幂等
    __enter__/__exit__
```

**finalizer 缺省**:依次尝试 `value.close()` → `value.release()`,都没有则 noop 登记。显式 finalizer 签名为 `Callable[[T], Any]`;默认 finalizer 已包装为无参闭包(统一 `()->None` 调用约定)。清理失败**不抛**,收集进 report 由调用方裁决;closed 后再 register 抛 RuntimeError。

**与 guard 配合范式**:`ctx.register(host, lambda r: pool.release(r["id"], owner=wid))`。

---

## 4. 模块依赖关系(无循环)

```
exceptions(无依赖)
utils.log ← context, 以及各模块
utils.retry ← exceptions; 被 pool/client 使用
utils.sanitizer ← 无; 被 ssh/http 使用
config.loader ← yaml, pydantic, exceptions
resources.pool ← yaml, filelock, exceptions, utils.{log,retry}
ssh.executor ← paramiko, exceptions, utils.{log,sanitizer}
http.* ← requests, exceptions, utils.{log,retry,sanitizer}
fixtures.guard ← json, filelock, exceptions, utils.log
顶层 __init__ 仅导入 exceptions + context(轻量入口)
```

---

## 5. 异常树(全部继承 ATFError)

```
ATFError
├── ConfigError
├── ResourcePoolError
│   ├── ResourceNotFoundError    # get/remove:ID 不存在
│   ├── ResourceExhaustedError   # acquire/batch:重试耗尽
│   └── ResourceStateError       # release:非 busy 或 owner 不符
├── FixtureGuardError            # 状态损坏/等待超时/JSON校验
├── SSHError
│   ├── SSHConnectError / SSHCommandError(.result) / SSHTransferError / SSHTimeoutError
├── ApiError
│   ├── ApiTransportError(.reason) → ApiTimeoutError
│   └── ApiHTTPStatusError(.response)
└── RetryExhaustedError
```

---

## 6. 测试矩阵(104 个,`python -m pytest tests` 全绿,`-n 4` 全绿)

| 文件(数量) | 关键覆盖 |
|---|---|
| test_resource_pool.py (22) | CRUD/保留键/重复ID(7);query/filter/组合/耗尽/重试等到释放/owner校验/release_all/记录深拷贝(11);batch 原子性(3);僵尸回收与新鲜不回收(2);双进程互斥 + 跨实例持久化(2) |
| test_client.py (20) | 请求与头覆盖(5);异常体系/status/transport/timeout(5);503重试/不重试(2);401刷新/不刷新(2);认证策略(4) |
| test_config_loader.py (13) | 环境选择四通道(5);env覆盖 nested/scalar/无前缀(4);缺失/坏YAML/空文件/校验失败/选择器不污染(5) |
| test_guard.py (15) | creator-user/create一次teardown一次/JSON校验/失败清条目/teardown吞异常/嵌套(7);PID死/超时/死holders/等待超时(4);exclusive两进程串行(1);3进程并发create一次(1) |
| test_context.py (11) | register/pending(3);LIFO/错误隔离/幂等/默认close/noop(5);with/异常也清理/repr(3) |
| test_ssh.py (9) | shell_quote 三态(3);target默认/跳板(2);拒连/close幂等/上下文(3);CommandResult(1) |
| test_utils.py (18) | Sanitizer 九类(9);RetryPolicy 九项(9) |
| integration/test_pool_xdist.py (4) | 4 worker 争抢:分配/释放/状态/不超卖 |

跨进程子进程测试通过 `_CHILD_ENV`(PYTHONPATH 指向 src 绝对路径)传入 Popen。

---

## 7. 技术债与路线图

| # | 项 | 难度 | 状态 | 落地说明 |
|---|---|---|---|---|
| 1 | 多层跳板链(递归 via) | 中 | 已完成 | `connect()` 新增 `_hop_chain`/`_build_tunnel`,`via` 可嵌套;含环检测;单跳行为不变 |
| 2 | ResourcePool find 分页/排序 | 低 | 已完成 | `find()` 加 `sort_by/sort_reverse/limit/offset`,默认全量向后兼容 |
| 3 | BaseClient 摘要日志接 caplog | 低 | 已完成 | `atf.http` 默认向上传播,补 `TestSummaryLogging` 验证 caplog 可捕获 |
| 4 | guard holders 心跳续租 | 中 | 已完成 | `shared()` 加 `heartbeat=True` 守护线程续租本地 holder 的 `at`,防长持有被误核减 |
| 5 | SCP 传输变体 | 低 | 已完成 | `upload/download` 加 `transfer="sftp"\|"scp"`,手写 SCP 协议(零新依赖) |
| 6 | ConfigLoader.load_raw() | 低 | 已完成 | 抽 `_merge_raw`,新增 `load_raw(env)` 返回校验前原始 dict |
| 7 | `X | Y` 注解 → Union | 低 | 已完成 | `utils/log.py` 仅 2 处(`int\|str`、`str\|Path\|None`)改为 Union,无行为变化 |

---

## 8. 提示词未定事项裁决(2026-08-26,钢人论证后)

| # | 不确定点 | 裁决 | 风险 | 处置 |
|---|---|---|---|---|
| 1 | 交付形态 | **独立框架库** | 结构 | §9 重构 |
| 2 | 资源池僵尸回收(未要求) | 默认开启 1800s | 安全 | 保留;文档警示昂贵不幂等资源应调大 |
| 3 | 清理失败 vs 用例失败 | TestContext 收集不抛,调用方裁决 | 语义 | 保留 |
| 4 | FileLock 跨机前提 | 仅同机 | 正确性 | docstring + README 边界声明 |
| 5 | 非幂等请求重试 | 仅网络错 + 408/425/429/502/503/504 | 语义 | 保留;按请求可覆盖 |
| 6 | 跳板层数 | 单层 | 默认值 | 路线图 #1 |
| 7 | 被测系统类型 | 不预设 | 默认值 | 保留 |

---

## 9. 库化重构实施方案(唯一待办,预计 ≤30 分钟)

**目标**:tests/ 收窄为框架自身测试;业务示范迁 examples/;不动 `src/atf/` 任何一行代码。

### 9.1 文件移动表

| 现路径 | 目标路径 |
|---|---|
| `tests/conftest.py` | `examples/conftest.py` |
| `tests/schemas.py` | `examples/schemas.py` |
| `tests/sut/`(整目录内容) | `examples/tests/` |
| `config/config.yaml` | `examples/config/config.yaml` |
| `config/resource_pool.yaml` | `examples/config/resource_pool.yaml` |
| `tests/unit/`、`tests/integration/` | **不动**(框架自身测试) |

### 9.2 操作步骤

1. **移动**:`mkdir -p examples/tests examples/config && mv tests/conftest.py examples/ && mv tests/schemas.py examples/ && mv tests/sut/test_sut_examples.py examples/tests/ && rmdir tests/sut && mv config/*.yaml examples/config/ && rmdir config`
2. **pyproject.toml**:`pythonpath = ["src"]`(去掉 `"tests"`);testpaths 不变。
3. **新建 `examples/pytest.ini`**:
   ```ini
   [pytest]
   testpaths = tests
   pythonpath = ..
   markers =
       sut: 需要真实被测环境(REST API / SSH)的示例用例
   ```
   examples/conftest.py 里 `ROOT = Path(__file__).resolve().parents[1]`(原 parents[1] 仍是 examples/,config 路径 `ROOT/"config"` 不变,无需改)。
4. **tests/integration/test_pool_xdist.py 种子内联**:删除 `POOL_FILE` 常量与对旧 `config/` 的 `yaml.safe_load`,改为在 fixture 里直接写种子:
   ```python
   seed = {"resources": [
       {"id": "node-1", "state": "free", "owner": None, "locked_at": None,
        "host": "192.168.1.11", "role": "compute", "tags": ["gpu"]},
       {"id": "node-2", "state": "free", "owner": None, "locked_at": None,
        "host": "192.168.1.12", "role": "compute", "tags": ["gpu"]},
       {"id": "node-3", "state": "free", "owner": None, "locked_at": None,
        "host": "192.168.1.13", "role": "compute", "tags": ["gpu", "25g"]},
   ]}
   pool_file.write_text(yaml.safe_dump(seed), encoding="utf-8")
   ```
   同步删除顶部 `from pathlib import Path` 与 `POOL_FILE` 行(若仅此处使用)。
5. **tests/ 根**确认无残留 `import schemas`(现 tests/conftest.py 移走后,tests/ 下仅 `__init__.py` 与 unit/integration)。
6. **文档**:
   - `src/atf/resources/pool.py` 模块 docstring 追加两段:僵尸回收安全警示(stale_timeout 默认 1800s,昂贵不幂等资源应调大;提示词未强制此行为,属保守默认)+ FileLock 仅同机互斥边界。
   - README 追加"作为库使用"章节:pip install → 使用者工程目录示意(自己的 conftest/schemas/config,引用 atf)。
7. **终验**:
   ```bash
   .venv/bin/python -m pytest tests            # 104 passed
   .venv/bin/python -m pytest tests -n 4       # 104 passed
   cd examples && ../.venv/bin/python -m pytest -c pytest.ini   # 8 skipped
   .venv/bin/pip install -e . && .venv/bin/python -c "import atf; print(atf.__version__)"
   ```

### 9.3 明确不做

- 不拆 monorepo、不发 PyPI(另行决定)
- 不改任何已验证的运行时行为(§8 裁决全部维持,仅补文档)
- 不修改 `src/atf/` 源码(除 §9.2-6 的 docstring 追加)

---

## 10. 续作指引(新会话)

1. 读本文档 §9,按 9.2 步骤 1→7 执行(全部命令可直接复制)。
2. 验证以 §9.2-7 的四条命令为准。
3. 重构完成后把本文档头部状态改为"§9 已完成",并在 §7 勾销对应项。
