# testkit

一个**业务无关**的通用 Python 测试框架，面向「REST API 管理面 + SSH 节点面」架构的系统。每个模块均可独立使用——包内不存在任何预设业务概念（没有 cluster / node / organization）。

[English](README.md) · [设计文档](docs/DESIGN.md)

## 特性

| 模块 | 职责 |
|------|------|
| `ResourcePool` | 跨进程安全的资源池（YAML 持久化 + `FileLock`、重试、批量分配） |
| `SSHExecutor` | SSH 命令执行——直连或跳板机（`direct-tcpip` 隧道）、架构探测 |
| `HTTPClient` + `AuthStrategy` | REST 客户端，可插拔认证、双 Token 刷新、multipart 上传 |
| `ConcurrentFixtureGuard` | 跨进程协调，仅一个 worker 构建共享 fixture |
| `ConfigRegistry` | YAML 多环境合并 + `${VAR}` 占位符 + Pydantic 校验 |
| `ResourceCleanup` | LIFO 逆序清理 + 重试 + 失败隔离 |
| `WaitHelper` | 泛型轮询，异常容忍 + 轮询中 Token 刷新 |
| `Pipeline` | 串行、可断点续跑的多阶段编排 |
| 异常 / 日志 | 统一异常层级 + k8s 风格 `--v=N` 日志级别 |

## 安装

要求 Python ≥ 3.10。若想在任何位置把 `testkit` 当作可导入库使用：

```bash
pip install -e .
```

> 可编辑安装同时会注册 `pytest11` 入口点，因此任何安装了 `testkit` 的项目都会自动获得其 fixture 与 CLI 选项（无需 `conftest.py`）。详见下方插件测试。

## 快速开始

```python
from testkit import (
    HTTPClient,
    TokenAuth,
    SSHExecutor,
    ResourcePool,
    WaitHelper,
)

# 1. HTTP 客户端（主动 + 被动双 Token 刷新）
client = HTTPClient("https://api.example.com/v1", auth=TokenAuth("your-token"))

# 2. SSH 执行器（直连模式）
with SSHExecutor("10.0.0.5", username="root", password="***") as ssh:
    result = ssh.execute("uname -m")
    print(result.stdout, result.exit_code)

# 3. 资源池（可跨 pytest-xdist worker 共享）
pool = ResourcePool("/tmp/resources.yaml")
pool.add({"id": "r1"})
allocated = pool.acquire(count=1)

# 4. 轮询等待条件满足
WaitHelper(timeout=60).until(lambda: client.get("/health").status_code == 200)
```

## 目录结构

```
testkit/
├── exceptions.py        # 统一异常层级
├── logging_setup.py     # --v=N 日志级别 + 敏感数据脱敏
├── config/              # ConfigRegistry（YAML + env + Pydantic）
├── pool/                # ResourcePool
├── ssh/                 # SSHExecutor / SSHResult
├── http/                # HTTPClient + 认证策略
├── fixture/             # ConcurrentFixtureGuard
├── cleanup/             # ResourceCleanup
├── utils/               # WaitHelper
├── model/               # Builder + BaseModel
└── pipeline/            # Pipeline / StageResult
```

## 运行测试

```bash
pip install -e ".[dev]"   # 安装运行时 + 开发依赖，并注册插件入口点
pytest                    # 全量：库 + 示例 + 插件（171 个测试）
pytest -n auto            # 并发（pytest-xdist）
```

### 覆盖率门禁（85%）

pytest 插件通过 `pytest11` 入口点自动注册，会在 pytest 启动时（coverage 开始**之前**）导入 `testkit`，导致插件的 import 期代码被记为"未覆盖"、拉低覆盖率。诚实地度量库覆盖率，请在覆盖率运行中禁用插件，再单独测试插件：

```bash
pytest -n auto tests/ --cov=testkit --cov-fail-under=85 -p no:testkit --ignore=tests/test_plugin.py
pytest tests/test_plugin.py    # 插件自动注册，经 pytester 验证
```

### 代码检查与类型检查

```bash
ruff check .
ruff format --check .
mypy testkit
```

## 清理临时文件

每次运行都会产生可再生的临时产物（`__pycache__/`、`.pytest_cache/`、`.pytest_tmp/`、`*.egg-info/`、`.coverage`）。这些已被 git 忽略，但你可以用一条命令从磁盘清除：

```bash
./scripts/clean.sh            # 删除全部临时产物
./scripts/clean.sh --dry      # 只列出将要删除的内容，不实际删除
```

脚本只匹配明确的临时模式，并**显式跳过 `.git/` 与 `.workbuddy/`**——绝不触碰源码与项目数据。被删的一切都会在下次 `pytest` / `pip install -e .` 时自动重建。

## 许可证

MIT
