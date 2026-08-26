"""SharedFixtureGuard:xdist 并发下 fixture 的“创建/清理”互斥。

解决的两个经典问题:

1. **重复创建**:session 级 fixture 在每个 xdist worker 各执行一次,
   导致环境被建 N 遍。``shared()`` 保证同一时刻只有一个进程执行
   ``create``,其余进程等待并复用其结果(通过 JSON 状态文件传递);
2. **崩溃死锁**:创建者进程半路被杀(OOM / Ctrl-C),状态停留在
   ``creating``,其他进程无限等待。守卫对 ``creating`` 停留超过
   ``takeover_after`` 秒(或持有者 PID 已死)的条目自动**接管重建**;
   对 ``ready`` 条目,持有者退场(PID 死亡 / 超时)时自动核减引用,
   防止最后一个真实使用者永远等不到清理。

状态文件(JSON)::

    {"version": 1, "fixtures": {"<name>": {
        "state": "creating|ready",
        "owner": "gw0", "pid": 12345, "host": "ci-01",
        "created_at": 1690000000.0,
        "value": <JSON 值,创建结果,如资源 ID/URL>,
        "holders": [{"owner": "gw0", "pid": 12345, "host": "ci-01", "at": ...}]
    }}}

与 pytest 集成:fixture 内 ``with guard.shared("db", create, teardown) as fx``
即可;建议配合 :class:`~atf.context.TestContext` 保证用例失败也能走到清理。
"""

from __future__ import annotations

import errno
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

from filelock import FileLock, Timeout

from atf.exceptions import FixtureGuardError
from atf.utils.log import get_logger

_logger = get_logger("atf.guard")

Creator = Callable[[], Any]
Teardown = Callable[[Any], None]


def _pid_alive(pid: int, host: str) -> bool:
    """判断 PID 是否存活;跨主机时无法探测,返回 True(交给超时接管)。"""
    if host != socket.gethostname():
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM  # 存在但无权限 → 仍活着
    return True


@dataclass
class SharedFixture:
    """一次 ``shared()`` 占用的结果。

    Attributes:
        name: fixture 名。
        role: 本进程角色:``creator``(执行了 create)/ ``user``(复用)。
        value: create 的返回值(JSON 序列化往返后的副本)。
    """

    name: str
    role: str
    value: Any


@dataclass
class _Holder:
    owner: str
    pid: int
    host: str
    at: float

    def to_dict(self) -> Dict[str, Any]:
        return {"owner": self.owner, "pid": self.pid, "host": self.host, "at": self.at}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "_Holder":
        return _Holder(owner=str(d.get("owner", "?")), pid=int(d.get("pid", 0)),
                       host=str(d.get("host", "?")), at=float(d.get("at", 0.0)))


@dataclass
class _Entry:
    state: str
    owner: str
    pid: int
    host: str
    created_at: float
    value: Any = None
    holders: List[_Holder] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state, "owner": self.owner, "pid": self.pid,
            "host": self.host, "created_at": self.created_at, "value": self.value,
            "holders": [h.to_dict() for h in self.holders],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "_Entry":
        return _Entry(
            state=str(d.get("state", "creating")),
            owner=str(d.get("owner", "?")),
            pid=int(d.get("pid", 0)),
            host=str(d.get("host", "?")),
            created_at=float(d.get("created_at", 0.0)),
            value=d.get("value"),
            holders=[_Holder.from_dict(h) for h in d.get("holders", [])],
        )


class SharedFixtureGuard:
    """基于 FileLock + JSON 状态文件的跨进程共享 fixture 守护。"""

    STATE_VERSION = 1

    def __init__(
        self,
        state_file: Union[str, Path],
        *,
        lock_timeout: float = 30.0,
        wait_ready_timeout: float = 600.0,
        takeover_after: Optional[float] = None,
        holder_stale_after: float = 3600.0,
        poll_interval: float = 0.2,
    ) -> None:
        """初始化守卫。

        Args:
            state_file: JSON 状态文件路径(自动创建父目录)。
            lock_timeout: 单次文件锁获取超时。
            wait_ready_timeout: 等待 creator 完成 create 的最长时间。
            takeover_after: ``creating`` 停留超过该秒数即接管重建;
                缺省取 ``wait_ready_timeout``。
            holder_stale_after: ``ready`` 持有者存活但长期未释放的核减阈值。
            poll_interval: 等待 ready 的轮询间隔。
        """
        self._path = Path(state_file)
        self._lock_timeout = lock_timeout
        self._wait_ready_timeout = wait_ready_timeout
        self._takeover_after = takeover_after if takeover_after is not None else wait_ready_timeout
        self._holder_stale_after = holder_stale_after
        self._poll_interval = poll_interval
        self._lock = FileLock(str(self._path) + ".lock")
        self._local: Dict[str, Any] = {}

    # ---------------------------------------------------------------- shared

    @contextmanager
    def shared(
        self,
        name: str,
        create: Creator,
        teardown: Optional[Teardown] = None,
        *,
        owner: Optional[str] = None,
        heartbeat: bool = True,
    ) -> Iterator[SharedFixture]:
        """“创建一次、多方复用、最后退出者清理”的 fixture 守护。

        Args:
            name: fixture 唯一名(同名即视为同一共享资源)。
            create: 无参创建函数,返回值必须是 JSON 可序列化的
                (如资源 ID / URL / 配置字典),会写入状态文件传给等待方。
            teardown: 清理函数,入参为 create 的返回值;最后一个持有者
                退出时执行。执行失败仅记录日志,不再向上抛。
            owner: 持有者标识(xdist 下建议传 worker id)。
            heartbeat: 是否启用持有续租(默认开)。长持有(超过
                ``holder_stale_after``)时周期刷新本地 holder 的 ``at``,
                防止被误判失效而过早回收 / 重复 teardown。

        Yields:
            :class:`SharedFixture`(role=creator|user, value=create 结果)。

        Raises:
            FixtureGuardError: 等待 ready 超时、状态文件损坏或 create 失败。
        """
        ident = _Ident(owner)
        mode, fx = self._enter(name, ident)
        if mode == "creator":
            fx = self._run_creator(name, create, ident)
        elif mode == "waiter":
            fx = self._wait_ready(name, create, ident)
        assert fx is not None
        hb = None
        if heartbeat:
            hb = _Heartbeat(self, name, ident, self._holder_stale_after)
            hb.start()
        try:
            yield fx
        finally:
            if hb is not None:
                hb.stop()
            self._release(name, fx, teardown, ident)

    def _enter(self, name: str, ident: _Ident) -> tuple[str, Optional[SharedFixture]]:
        """第一次接触状态:成为 user / creator,或返回 waiter 去等待。"""
        with self._critical():
            entry = self._load().get(name)
            entry = self._normalize(entry, name, ident)
            if entry.state == "ready":
                entry.holders.append(ident.holder())
                self._write_entry(name, entry)
                return "user", SharedFixture(name, "user", entry.value)
            if self._is_mine(entry, ident):
                entry.state = "creating"
                entry.owner, entry.pid, entry.host = ident.owner, ident.pid, ident.host
                entry.created_at = time.time()
                entry.holders = [ident.holder()]
                self._write_entry(name, entry)
                return "creator", None
            return "waiter", None  # 其余进程正在创建,转入等待

    def _wait_ready(self, name: str, create: Creator, ident: _Ident) -> SharedFixture:
        """轮询等待 creator 发布 ready;创建者死亡/超时则接管重建。"""
        deadline = time.monotonic() + self._wait_ready_timeout
        while True:
            mode, fx = self._enter(name, ident)
            if mode == "user":
                return fx if fx is not None else self._run_creator(name, create, ident)
            if mode == "creator":
                return self._run_creator(name, create, ident)
            if time.monotonic() > deadline:
                raise FixtureGuardError(
                    f"timeout ({self._wait_ready_timeout}s) waiting for fixture '{name}' "
                    f"to become ready"
                )
            time.sleep(self._poll_interval)

    def _run_creator(self, name: str, create: Creator, ident: _Ident) -> SharedFixture:
        """执行 create 并发布 ready;失败则回滚条目避免死锁。"""
        started = time.monotonic()
        try:
            value = create()
        except BaseException as exc:
            with self._critical():
                entry = self._load().get(name)
                if entry is not None and self._is_mine(entry, ident):
                    self._delete_entry(name)
            if isinstance(exc, Exception):
                raise FixtureGuardError(f"creator of '{name}' failed: {exc}") from exc
            raise  # KeyboardInterrupt / SystemExit 原样抛出
        _jsonable(value)  # 提前校验,避免发布时写状态失败
        with self._critical():
            entry = self._load().get(name)
            if entry is None or not self._is_mine(entry, ident):
                # 等待期间被接管且对方已重建完成 → 复用对方的
                if entry is not None and entry.state == "ready":
                    _logger.warning("'%s' was taken over during create; reusing", name)
                    return SharedFixture(name, "user", entry.value)
                raise FixtureGuardError(f"guard state for '{name}' vanished unexpectedly")
            entry.state = "ready"
            entry.value = value
            self._write_entry(name, entry)
        _logger.info("fixture '%s' created by %s in %.1fs", name, ident.owner,
                     time.monotonic() - started)
        return SharedFixture(name, "creator", value)

    def _release(
        self,
        name: str,
        fx: SharedFixture,
        teardown: Optional[Teardown],
        ident: _Ident,
    ) -> None:
        """退出持有:核减引用;最后一个持有者在锁内执行 teardown 后删除条目。"""
        should_teardown = False
        value: Any = None
        with self._critical():
            entry = self._load().get(name)
            if entry is None:
                return
            entry.holders = self._prune_dead(entry)
            entry.holders = [
                h for h in entry.holders
                if not (h.owner == ident.owner and h.host == ident.host and h.pid == ident.pid)
            ]
            if not entry.holders:
                value = entry.value
                should_teardown = teardown is not None
                self._delete_entry(name)
            else:
                self._write_entry(name, entry)
        if should_teardown and teardown is not None:
            try:
                teardown(value)
                _logger.info("fixture '%s' torn down by %s", name, ident.owner)
            except Exception as exc:  # noqa: BLE001 - 清理失败不阻断测试收尾
                _logger.error("teardown of '%s' failed: %s", name, exc)

    # ------------------------------------------------------------ exclusive

    @contextmanager
    def exclusive(self, name: str, *, owner: Optional[str] = None) -> Iterator[None]:
        """简单互斥段:同名段同一时刻只有一个进程进入(如环境清理脚本)。

        与 ``shared`` 相互独立:``exclusive`` 不记录引用计数,只做按名字的
        临界区互斥。``owner`` 仅用于日志,不影响语义。

        Raises:
            FixtureGuardError: 超过 ``lock_timeout`` 仍拿不到锁。
        """
        lock = FileLock(f"{self._path}.{name}.exlock")
        try:
            lock.acquire(timeout=self._lock_timeout)
        except Timeout as exc:
            raise FixtureGuardError(
                f"exclusive('{name}') lock timeout ({self._lock_timeout}s)"
            ) from exc
        try:
            yield
        finally:
            lock.release()

    # -------------------------------------------------------------- inspection

    def entries(self) -> Dict[str, Dict[str, Any]]:
        """当前状态文件快照(name → entry dict,调试用)。"""
        with self._critical():
            return {k: v.to_dict() for k, v in self._load().items()}

    def reset(self, name: str) -> None:
        """强制删除一个条目(状态异常时的人工兜底)。"""
        with self._critical():
            self._delete_entry(name)

    # -------------------------------------------------------------- internals

    def _normalize(self, entry: Optional[_Entry], name: str, ident: _Ident) -> _Entry:
        """对空缺/僵尸条目做接管决策,返回可继续使用的 entry。"""
        now = time.time()
        if entry is None:
            return _Entry("creating", ident.owner, ident.pid, ident.host, now)
        if entry.state == "creating":
            stale_by_time = now - entry.created_at > self._takeover_after
            dead = not _pid_alive(entry.pid, entry.host)
            if stale_by_time or dead:
                _logger.warning(
                    "taking over '%s' (owner=%s pid=%d stuck %.0fs%s)",
                    name, entry.owner, entry.pid, now - entry.created_at,
                    ", pid dead" if dead else "",
                )
                return _Entry("creating", ident.owner, ident.pid, ident.host, now)
            return entry
        # ready:核减死亡/超时持有者,避免泄漏引用卡死清理
        entry.holders = self._prune_dead(entry)
        if not entry.holders:
            _logger.warning("'%s' has no live holders; recycling", name)
            return _Entry("creating", ident.owner, ident.pid, ident.host, now)
        return entry

    def _prune_dead(self, entry: _Entry) -> List[_Holder]:
        now = time.time()
        alive: List[_Holder] = []
        for h in entry.holders:
            if _pid_alive(h.pid, h.host) and now - h.at < self._holder_stale_after:
                alive.append(h)
            else:
                _logger.warning("pruning holder %s (pid=%d host=%s)", h.owner, h.pid, h.host)
        return alive

    @staticmethod
    def _is_mine(entry: _Entry, ident: "_Ident") -> bool:
        return entry.owner == ident.owner and entry.pid == ident.pid and entry.host == ident.host

    @contextmanager
    def _critical(self) -> Iterator[None]:
        try:
            self._lock.acquire(timeout=self._lock_timeout)
        except Timeout as exc:
            raise FixtureGuardError(
                f"guard lock timeout ({self._lock_timeout}s): {self._lock}"
            ) from exc
        try:
            yield
        finally:
            self._lock.release()

    def _load(self) -> Dict[str, _Entry]:
        """读取状态文件(须在临界区内)。"""
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FixtureGuardError(f"corrupted guard state '{self._path}': {exc}") from exc
        fixtures = data.get("fixtures", {})
        return {k: _Entry.from_dict(v) for k, v in fixtures.items()}

    def _write_entry(self, name: str, entry: _Entry) -> None:
        state = self._load()
        state[name] = entry
        self._save(state)

    def _delete_entry(self, name: str) -> None:
        state = self._load()
        state.pop(name, None)
        self._save(state)

    def _save(self, state: Dict[str, _Entry]) -> None:
        # 状态为空时删除文件,避免留下一堆空壳 JSON(仍在锁内,无竞态)
        if not state:
            self._path.unlink(missing_ok=True)
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"version": self.STATE_VERSION,
                 "fixtures": {k: v.to_dict() for k, v in state.items()}},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)


def _jsonable(value: Any) -> None:
    """校验 create 返回值可 JSON 序列化,失败即抛错。"""
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise FixtureGuardError(
            f"create() return value must be JSON-serializable: {exc}"
        ) from exc


@dataclass
class _Ident:
    """一次 shared() 调用的进程身份。"""

    owner: Optional[str]
    pid: int = field(default_factory=os.getpid)
    host: str = field(default_factory=socket.gethostname)

    def __post_init__(self) -> None:
        if not self.owner:
            self.owner = os.environ.get("PYTEST_XDIST_WORKER") or f"main-{self.pid}"

    def holder(self) -> _Holder:
        return _Holder(owner=self.owner, pid=self.pid, host=self.host, at=time.time())


class _Heartbeat:
    """共享 fixture 持有期间的续租守护线程:周期性刷新本地 holder 的 ``at``。

    防止长持有(超过 ``holder_stale_after``)被 :meth:`SharedFixtureGuard._prune_dead`
    误判为失效而过早回收 / 重复 teardown。线程仅在共享上下文存活期间运行,退出时
    由 :meth:`SharedFixtureGuard.shared` 调用 :meth:`stop` 回收。
    """

    def __init__(
        self, guard: "SharedFixtureGuard", name: str, ident: "_Ident", stale_after: float
    ) -> None:
        self._guard = guard
        self._name = name
        self._ident = ident
        self._interval = max(0.05, stale_after / 3.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"atf-guard-hb-{name}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval + 1.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self._interval):
                break
            self._touch()

    def _touch(self) -> None:
        try:
            with self._guard._critical():
                entry = self._guard._load().get(self._name)
                if entry is None:
                    return
                for h in entry.holders:
                    if (
                        h.owner == self._ident.owner
                        and h.host == self._ident.host
                        and h.pid == self._ident.pid
                    ):
                        h.at = time.time()
                self._guard._write_entry(self._name, entry)
        except Exception:  # noqa: BLE001 - 续租失败不应打断测试收尾
            pass
