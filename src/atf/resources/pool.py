"""ResourcePool:YAML 持久化资源池。

面向"多进程(xdist worker / CI 任务)争抢同一批被测资源(主机、账号、
License、容器……)"的场景,提供:

- **持久化**:状态落在单个 YAML 文件,进程崩溃不丢;
- **跨进程互斥**:所有读写都在 ``filelock.FileLock`` 临界区内完成,
  读-改-写是原子的;
- **任意条件过滤**:``query`` 做子集匹配(适合配置驱动),
  ``filter`` 传任意谓词函数(适合复杂条件),可叠加;
- **重试等待**:资源被占满时按 ``retries`` + ``interval`` 轮询,
  而不是立刻失败;
- **批量分配**:``acquire_batch`` 在同一临界区内一次性分配 N 个,
  不会出现"两个 worker 各拿到半批"的交错;
- **僵尸接管**:某资源被占用超过 ``stale_timeout``(典型:持有者崩溃
  后来不及 release),下一个 acquire 会强制回收再分配。

记录结构(每条资源,业务字段平铺在保留字段之外)::

    id: host-01            # 唯一标识(池内分配或用户指定)
    state: free            # free | busy | disabled
    owner: null            # 当前持有者标识(acquire 时可传 worker id)
    locked_at: "..."       # ISO8601,用于僵尸检测
    host: 10.0.0.11        # ↓ 以下均为任意业务字段,过滤条件作用于整条记录
    role: storage
    tags: [ssd, 10g]

.. note::
    僵尸回收是**默认开启**的保守行为(``stale_timeout`` 默认 1800s):
    持有者进程崩溃、来不及 ``release`` 时,下一个 ``acquire`` 会强制回收
    再分配。这对"创建成本高、不可幂等"的资源(如申请到的 License、临时实例)
    有风险——被误回收后,若原持有者恢复会凭空多占一份。若资源昂贵或不幂等,
    请显式调大 ``stale_timeout``(例如数小时),或将该资源置为 ``disabled``
    由运维人工摘取。该默认并非提示词强制要求,而是为通用场景选的安全值。

.. note::
    跨进程互斥依赖 ``filelock`` 的 POSIX ``fcntl`` 锁,**仅在同机、同一
    文件系统**上保证互斥。跨主机、NFS/网络盘、容器内外的锁不保证生效;
    多机并发请改用中心化锁(数据库 / Redis / 分布式锁服务)。
"""

from __future__ import annotations

import copy
import datetime as _dt
import os
import socket
import uuid
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

import yaml
from filelock import FileLock, Timeout

from atf.exceptions import (
    ResourceExhaustedError,
    ResourceNotFoundError,
    ResourcePoolError,
    ResourceStateError,
)
from atf.utils.log import get_logger
from atf.utils.retry import RetryExhaustedError, RetryPolicy

_logger = get_logger("atf.pool")

Predicate = Callable[[Dict[str, Any]], bool]


class ResourceState(str, Enum):
    """资源状态机:``free`` ⇄ ``busy``,``disabled`` 为运维摘除态。"""

    FREE = "free"
    BUSY = "busy"
    DISABLED = "disabled"


def default_owner() -> str:
    """生成默认持有者标识:``主机名:进程号``(xdist 场景建议显式传 worker id)。"""
    return f"{socket.gethostname()}:{os.getpid()}"


class ResourcePool:
    """基于 YAML 文件 + 文件锁的跨进程资源池。"""

    RESERVED_KEYS = frozenset({"id", "state", "owner", "locked_at"})
    _TOP_KEY = "resources"

    def __init__(
        self,
        path: Union[str, Path],
        *,
        stale_timeout: float = 1800.0,
        lock_timeout: float = 30.0,
    ) -> None:
        """打开(或延迟创建)一个资源池。

        Args:
            path: YAML 池文件路径;不存在时视为空池,首次写入时创建。
            stale_timeout: 资源被占用超过该秒数视为僵尸,可被强制回收。
            lock_timeout: 获取文件锁的超时秒数(超时抛 :class:`ResourcePoolError`)。
        """
        self._path = Path(path)
        self._stale_timeout = stale_timeout
        self._lock_timeout = lock_timeout
        self._lock = FileLock(str(self._path) + ".lock")

    # ------------------------------------------------------------- 公共查询

    def find(
        self,
        *,
        query: Optional[Dict[str, Any]] = None,
        filter: Optional[Predicate] = None,
        sort_by: Optional[str] = None,
        sort_reverse: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询满足条件的全部资源(不限状态)。

        Args:
            query: 子集匹配条件,逐层比较 ``data`` 内的字段。
            filter: 任意谓词,入参为整条记录(含 id/state/owner/data)。
            sort_by: 按该业务字段排序;字段缺失或值为 ``None`` 的记录永远排在
                末尾(无论 ``sort_reverse`` 取值)。
            sort_reverse: 排序方向(默认升序)。
            limit: 返回条数上限;``None`` 表示不限制。
            offset: 排序后跳过前 ``offset`` 条。

        Returns:
            命中的记录列表(深拷贝,修改不影响池内状态)。
        """
        with self._critical():
            records = self._load()
            matched = [copy_record(r) for r in records if self._match(r, query, filter)]
        if sort_by is not None:
            self._sort_records(matched, sort_by, sort_reverse)
        if offset or limit is not None:
            end = len(matched) if limit is None else offset + limit
            matched = matched[offset:end]
        return matched

    @staticmethod
    def _sort_records(
        records: List[Dict[str, Any]], key: str, reverse: bool
    ) -> None:
        """原地按业务字段排序;字段缺失/``None`` 永远置末尾(与 ``reverse`` 无关)。

        同类型值直接比较;异构值(如 int 与 str)按类型名排序,避免 ``TypeError``;
        业务字段通常同构,此兜底仅为守护排序不炸。
        """
        present = [r for r in records if key in r and r[key] is not None]
        missing = [r for r in records if key not in r or r[key] is None]
        present.sort(key=lambda r: (type(r[key]).__name__, r[key]), reverse=reverse)
        records[:] = present + missing if not reverse else missing + present

    def get(self, resource_id: str) -> Dict[str, Any]:
        """按 ID 取单条资源记录。

        Raises:
            ResourceNotFoundError: ID 不存在。
        """
        with self._critical():
            return copy_record(self._locate(self._load(), resource_id))

    def stats(self) -> Dict[str, int]:
        """按状态统计资源数量,如 ``{"free": 2, "busy": 1, "disabled": 0}``。"""
        with self._critical():
            counts = {s.value: 0 for s in ResourceState}
            for r in self._load():
                counts[r.get("state", ResourceState.FREE.value)] += 1
            return counts

    # ------------------------------------------------------------- 资源管理

    def add(self, data: Dict[str, Any], *, resource_id: Optional[str] = None) -> Dict[str, Any]:
        """向池中新增一个资源。

        Args:
            data: 任意业务字段(不可占用保留键 id/state/owner/locked_at)。
            resource_id: 指定 ID;缺省自动生成。

        Returns:
            新增后的完整记录。
        """
        clash = self.RESERVED_KEYS & set(data)
        if clash:
            raise ResourcePoolError(f"resource data cannot use reserved keys: {sorted(clash)}")
        rid = resource_id or f"res-{uuid.uuid4().hex[:8]}"
        with self._critical():
            records = self._load()
            if any(r.get("id") == rid for r in records):
                raise ResourcePoolError(f"duplicate resource id: {rid}")
            record = {"id": rid, "state": ResourceState.FREE.value, "owner": None,
                      "locked_at": None, **data}
            records.append(record)
            self._save(records)
            _logger.info("resource added: %s", rid)
            return copy_record(record)

    def remove(self, resource_id: str, *, force: bool = False) -> Dict[str, Any]:
        """从池中移除资源。

        Args:
            resource_id: 目标资源 ID。
            force: 允许移除 busy 状态的资源;否则 busy 资源必须先 release。

        Raises:
            ResourceNotFoundError: ID 不存在。
            ResourceStateError: 资源处于 busy 且未指定 force。
        """
        with self._critical():
            records = self._load()
            record = self._locate(records, resource_id)
            if record.get("state") == ResourceState.BUSY.value and not force:
                raise ResourceStateError(
                    f"resource '{resource_id}' is busy (owner={record.get('owner')}); "
                    "release it first or pass force=True"
                )
            records.remove(record)
            self._save(records)
            _logger.info("resource removed: %s", resource_id)
            return copy_record(record)

    def set_enabled(self, resource_id: str, enabled: bool) -> None:
        """运维开关:禁用的资源不参与分配。"""
        with self._critical():
            records = self._load()
            record = self._locate(records, resource_id)
            record["state"] = ResourceState.FREE.value if enabled else ResourceState.DISABLED.value
            self._save(records)

    # --------------------------------------------------------------- 分配

    def acquire(
        self,
        *,
        query: Optional[Dict[str, Any]] = None,
        filter: Optional[Predicate] = None,
        owner: Optional[str] = None,
        retries: int = 0,
        interval: float = 1.0,
    ) -> Dict[str, Any]:
        """获取一个满足条件的空闲资源,并将其标记为 busy。

        Args:
            query: 子集匹配条件(作用于 ``data`` 字段)。
            filter: 任意谓词(作用于整条记录),与 ``query`` 叠加(AND)。
            owner: 持有者标识;缺省 ``主机名:进程号``。release 时校验。
            retries: 池中暂时无可用资源时的额外尝试次数(0=只试一次)。
            interval: 重试间隔秒数。

        Returns:
            分配到的资源记录(含 id/data,owner 已置为本次持有者)。

        Raises:
            ResourceExhaustedError: 重试耗尽仍无满足条件的空闲资源。
        """
        holder = owner or default_owner()
        policy = RetryPolicy(retries=retries, interval=interval, exceptions=(ResourcePoolError,))
        try:
            return policy.execute(
                lambda: self._try_acquire(query, filter, holder),
                description=f"pool.acquire({self._path.name})",
                is_retryable=lambda r: r is None,
            )
        except RetryExhaustedError as exc:
            raise ResourceExhaustedError(
                f"no free resource matching condition in '{self._path}' "
                f"after {retries + 1} attempt(s)"
            ) from exc

    def acquire_batch(
        self,
        count: int,
        *,
        query: Optional[Dict[str, Any]] = None,
        filter: Optional[Predicate] = None,
        owner: Optional[str] = None,
        retries: int = 0,
        interval: float = 1.0,
    ) -> List[Dict[str, Any]]:
        """批量获取 ``count`` 个满足条件的空闲资源。

        在单个临界区内完成挑选与标记:要么一次拿满,要么一个不拿,
        避免与其他进程交错分配导致"半批"。

        Raises:
            ResourceExhaustedError: 重试耗尽仍凑不齐 ``count`` 个。
        """
        if count < 1:
            raise ResourcePoolError("count must be >= 1")
        holder = owner or default_owner()
        policy = RetryPolicy(retries=retries, interval=interval, exceptions=(ResourcePoolError,))
        try:
            return policy.execute(
                lambda: self._try_acquire_batch(count, query, filter, holder),
                description=f"pool.acquire_batch({self._path.name}, n={count})",
                is_retryable=lambda r: r is None,
            )
        except RetryExhaustedError as exc:
            raise ResourceExhaustedError(
                f"cannot allocate {count} matching resources from '{self._path}' "
                f"after {retries + 1} attempt(s)"
            ) from exc

    def release(self, resource_id: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
        """释放资源(free + 清空 owner)。

        Args:
            resource_id: 目标资源 ID。
            owner: 期望的持有者;传入且与记录不符时抛错,防止误释放他人资源。

        Raises:
            ResourceNotFoundError: ID 不存在。
            ResourceStateError: 资源不处于 busy,或 owner 校验失败。
        """
        with self._critical():
            records = self._load()
            record = self._locate(records, resource_id)
            if record.get("state") != ResourceState.BUSY.value:
                raise ResourceStateError(
                    f"resource '{resource_id}' is not busy (state={record.get('state')})"
                )
            if owner is not None and record.get("owner") != owner:
                raise ResourceStateError(
                    f"resource '{resource_id}' owned by {record.get('owner')!r}, not {owner!r}"
                )
            record["state"] = ResourceState.FREE.value
            record["owner"] = None
            record["locked_at"] = None
            self._save(records)
            _logger.info("resource released: %s (owner=%s)", resource_id, owner)
            return copy_record(record)

    def release_all(self, owner: str) -> List[str]:
        """释放指定持有者占用的全部资源(进程收尾时调用)。

        Returns:
            本次实际释放的资源 ID 列表。
        """
        with self._critical():
            records = self._load()
            released: List[str] = []
            for r in records:
                if r.get("state") == ResourceState.BUSY.value and r.get("owner") == owner:
                    r["state"] = ResourceState.FREE.value
                    r["owner"] = None
                    r["locked_at"] = None
                    released.append(r["id"])
            if released:
                self._save(records)
                _logger.info("released %d resource(s) for owner=%s", len(released), owner)
            return released

    # ------------------------------------------------------------- internals

    def _try_acquire(
        self,
        query: Optional[Dict[str, Any]],
        filter: Optional[Predicate],
        owner: str,
    ) -> Optional[Dict[str, Any]]:
        with self._critical():
            records = self._load()
            self._reap_stale(records)
            for record in records:
                if record.get("state") != ResourceState.FREE.value:
                    continue
                if not self._match(record, query, filter):
                    continue
                self._mark(record, owner)
                self._save(records)
                _logger.info("resource acquired: %s (owner=%s)", record["id"], owner)
                return copy_record(record)
            return None

    def _try_acquire_batch(
        self,
        count: int,
        query: Optional[Dict[str, Any]],
        filter: Optional[Predicate],
        owner: str,
    ) -> Optional[List[Dict[str, Any]]]:
        with self._critical():
            records = self._load()
            self._reap_stale(records)
            picked = [
                r for r in records
                if r.get("state") == ResourceState.FREE.value and self._match(r, query, filter)
            ]
            if len(picked) < count:
                return None
            for record in picked[:count]:
                self._mark(record, owner)
            self._save(records)
            _logger.info("batch acquired %d resource(s) (owner=%s)", count, owner)
            return [copy_record(r) for r in picked[:count]]

    def _reap_stale(self, records: List[Dict[str, Any]]) -> None:
        """就地回收被占用超过 stale_timeout 的僵尸资源(须在临界区内调用)。"""
        now = _dt.datetime.now(_dt.timezone.utc)
        for record in records:
            if record.get("state") != ResourceState.BUSY.value:
                continue
            locked_at = _parse_ts(record.get("locked_at"))
            if locked_at is None:
                continue
            if (now - locked_at).total_seconds() > self._stale_timeout:
                _logger.warning(
                    "reaping stale resource %s (owner=%s, locked_at=%s)",
                    record.get("id"), record.get("owner"), record.get("locked_at"),
                )
                record["state"] = ResourceState.FREE.value
                record["owner"] = None
                record["locked_at"] = None

    @staticmethod
    def _mark(record: Dict[str, Any], owner: str) -> None:
        record["state"] = ResourceState.BUSY.value
        record["owner"] = owner
        record["locked_at"] = _utcnow_iso()

    @staticmethod
    def _match(
        record: Dict[str, Any],
        query: Optional[Dict[str, Any]],
        filter: Optional[Predicate],
    ) -> bool:
        if query is not None and not _subset_match(query, record):
            return False
        if filter is not None and not filter(record):
            return False
        return True

    @staticmethod
    def _locate(records: List[Dict[str, Any]], resource_id: str) -> Dict[str, Any]:
        for r in records:
            if r.get("id") == resource_id:
                return r
        raise ResourceNotFoundError(f"resource not found: {resource_id}")

    @contextmanager
    def _critical(self) -> Iterator[None]:
        """进入文件锁临界区;获取锁超时转成领域异常。"""
        try:
            self._lock.acquire(timeout=self._lock_timeout)
        except Timeout as exc:
            raise ResourcePoolError(
                f"failed to acquire pool file lock within {self._lock_timeout}s: {self._lock}"
            ) from exc
        try:
            yield
        finally:
            self._lock.release()

    def _load(self) -> List[Dict[str, Any]]:
        """读取池文件(须在临界区内调用)。"""
        if not self._path.is_file():
            return []
        data = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        records = data.get(self._TOP_KEY, [])
        if not isinstance(records, list):
            raise ResourcePoolError(f"malformed pool file '{self._path}': resources must be a list")
        return records

    def _save(self, records: List[Dict[str, Any]]) -> None:
        """原子写入池文件:先写临时文件再 rename(须在临界区内调用)。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            yaml.safe_dump({self._TOP_KEY: records}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)


def copy_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """深拷贝一条资源记录,防止调用方改写污染池内状态。"""
    return copy.deepcopy(record)


def _subset_match(query: Dict[str, Any], record: Dict[str, Any]) -> bool:
    """判断 query 的每个键值对是否都与记录一致(嵌套 dict 递归)。"""

    def walk(q: Any, r: Any) -> bool:
        if isinstance(q, dict) and isinstance(r, dict):
            return all(k in r and walk(v, r[k]) for k, v in q.items())
        return q == r

    return walk(query, record)


def _utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _parse_ts(value: Any) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
