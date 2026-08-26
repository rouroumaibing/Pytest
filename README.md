# ATF — 通用 REST API + SSH 自动化测试框架

零业务耦合:六大模块可独立使用,也可通过 `examples/conftest.py` 示例组装成完整的 pytest 工程。适用于 "REST API + SSH" 被测系统,原生支持 pytest-xdist 多进程并发。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e .          # 或手装依赖(见 pyproject.toml)
.venv/bin/python -m pytest tests/unit tests/integration    # 离线测试
.venv/bin/python -m pytest tests/unit tests/integration -n 4   # xdist 并发
cd examples && ATF_SUT=1 ATF_ENV=qa ../.venv/bin/python -m pytest -c pytest.ini -m sut   # 真实环境
```

无需手动设置 `PYTHONPATH`(`pyproject.toml` 已配 `pythonpath = ["src"]`;`examples/` 用自带的 `pytest.ini`)。

## 六大模块

| 模块 | 位置 | 一句话 |
|---|---|---|
| ResourcePool | `src/atf/resources/pool.py` | YAML 持久化资源池:FileLock 跨进程互斥、任意条件过滤、批量分配、僵尸回收 |
| SSHExecutor | `src/atf/ssh/executor.py` | paramiko 封装:直连/跳板隧道、命令执行、SFTP、架构探测、日志脱敏 |
| BaseClient | `src/atf/http/` | requests 封装:可插拔认证(401 自动刷新)、状态码重试、摘要日志、统一异常 |
| SharedFixtureGuard | `src/atf/fixtures/guard.py` | xdist 下 fixture 创建/清理互斥:JSON 状态、引用计数、超时接管防死锁 |
| ConfigLoader | `src/atf/config/loader.py` | YAML 多环境 + `ATF_` 环境变量覆盖 + Pydantic 校验 |
| TestContext | `src/atf/context.py` | 用例级资源登记簿:LIFO 自动清理、错误隔离 |

### ResourcePool

```python
from atf.resources import ResourcePool

pool = ResourcePool("config/resource_pool.yaml", stale_timeout=1800)
host = pool.acquire(query={"role": "compute"}, owner="gw0", retries=10, interval=1)
hosts = pool.acquire_batch(2, filter=lambda r: "gpu" in r.get("tags", []))
pool.release(host["id"], owner="gw0")        # owner 不符会拒绝
pool.release_all(owner="gw0")                # 收尾兜底
```

### BaseClient

```python
from atf.http import BaseClient, TokenAuth

client = BaseClient("https://api.example.com",
                    auth=TokenAuth(tok, refresh_fn=lambda: login()),
                    extra_headers={"X-Tenant": "t1"}, timeout=10)
resp = client.post("/items", json_body={"name": "x"})   # 非2xx抛ApiHTTPStatusError
resp = client.get("/x", raise_for_status=False)          # 负向用例
```

### SSHExecutor

```python
from atf.ssh import SSHExecutor, SSHTarget

jump = SSHTarget(host="10.0.0.1", username="jump")
target = SSHTarget(host="10.0.0.2", via=jump)             # 跳板隧道
with SSHExecutor(target) as ssh:
    print(ssh.run("uname -a").stdout)                     # 口令/令牌自动脱敏入日志
    print(ssh.detect_arch())                              # x86_64 / aarch64
    ssh.upload("local.bin", "/tmp/remote.bin")
```

### SharedFixtureGuard

```python
from atf.fixtures import SharedFixtureGuard

guard = SharedFixtureGuard(".state/guard.json", takeover_after=300)
with guard.shared("topology", create_env, teardown_env, owner=worker_id) as fx:
    use(fx.value)      # creator/user 角色,create 全局仅执行一次
```

### ConfigLoader + TestContext

```python
from atf.config import ConfigLoader
cfg = ConfigLoader(AppConfig, "config/config.yaml").load("qa")   # ATF_ENV 亦可

from atf.context import TestContext
with TestContext("case-1") as ctx:
    h = pool.acquire(query={"role": "compute"})
    ctx.register(h, lambda r: pool.release(r["id"]))   # 断言失败也清理
```

## 配置说明

`config/config.yaml`:`default` 基线段 + `envs.{dev,qa,prod}` 覆盖段,深合并。
环境变量覆盖:`ATF_` 前缀、`__` 作层级分隔,如 `ATF_HTTP__TIMEOUT=30`;
环境选择优先级:显式入参 > `ATF_ENV` > `default_env`。业务配置结构由
`examples/schemas.py` 的 Pydantic 模型自定义,框架零预设。

## 作为库使用

框架以 pip 包形式安装,业务工程只需引入 `atf.*`,你自己的 conftest / schemas / config 放在你自己的工程里:

```bash
pip install -e .          # 或发布后 pip install atf
```

```
your-sut-project/                你自己的被测工程(非本仓库)
├── conftest.py                  组装六大模块(仿 examples/conftest.py)
├── schemas.py                   业务侧 Pydantic 模型(仿 examples/schemas.py)
├── config/
│   ├── config.yaml             ConfigLoader 多环境配置(仿 examples/config/config.yaml)
│   └── resource_pool.yaml       ResourcePool 初始库存(仿 examples/config/resource_pool.yaml)
└── tests/
    └── test_sut.py             真实用例(标记 @pytest.mark.sut,ATF_SUT=1 才跑)
```

最小引入示例:

```python
from atf.resources import ResourcePool
from atf.http import BaseClient, TokenAuth

pool = ResourcePool("config/resource_pool.yaml", stale_timeout=1800)
host = pool.acquire(query={"role": "compute"}, owner="gw0", retries=10)
client = BaseClient("https://api.example.com",
                    auth=TokenAuth(tok, refresh_fn=login))
```

可直接把本仓库的 `examples/` 目录 `git clone` 出来作为起点改造——它自带 `pytest.ini`(独立 `pythonpath`、内置 `sut` marker),与框架自测 `tests/` 物理隔离。

## 更多

架构细节、决策记录、路线图见 `DEV_PLAN.md`。
