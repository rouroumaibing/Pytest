"""SharedFixtureGuard 单元测试(含跨进程并发验证)。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from atf.exceptions import FixtureGuardError
from atf.fixtures import SharedFixtureGuard

# 子进程需要显式 PYTHONPATH:pytest 的 pythonpath ini 只改当前进程 sys.path
_SRC = str(Path(__file__).resolve().parents[2] / "src")
_CHILD_ENV = {**os.environ,
              "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}


@pytest.fixture
def guard(tmp_path):
    return SharedFixtureGuard(
        tmp_path / "guard.json",
        lock_timeout=5,
        wait_ready_timeout=5,
        takeover_after=5,
        poll_interval=0.02,
    )


class TestShared:
    def test_first_is_creator_second_is_user(self, guard):
        outcomes = []
        with guard.shared("fx", lambda: {"v": 1}, owner="w0") as a:
            outcomes.append(a.role)
            with guard.shared("fx", lambda: {"v": 1}, owner="w1") as b:
                outcomes.append(b.role)
                assert b.value == {"v": 1}
        assert outcomes == ["creator", "user"]

    def test_create_runs_only_once_teardown_on_last_exit(self, guard):
        creates, teardowns = [], []

        def create():
            creates.append(1)
            return {"n": len(creates)}

        def teardown(value):
            teardowns.append(value)

        with guard.shared("fx", create, teardown, owner="w0"):
            with guard.shared("fx", create, teardown, owner="w1"):
                pass
        assert len(creates) == 1
        assert teardowns == [{"n": 1}]  # 最后退出者清理,仅一次
        assert "fx" not in guard.entries()

    def test_value_must_be_jsonable(self, guard):
        with pytest.raises(FixtureGuardError, match="JSON-serializable"):
            with guard.shared("fx", lambda: object()):
                pass

    def test_creator_failure_removes_entry(self, guard):
        def bad_create():
            raise RuntimeError("deploy failed")

        with pytest.raises(FixtureGuardError, match="deploy failed"):
            with guard.shared("fx", bad_create, owner="w0"):
                pass
        assert "fx" not in guard.entries()  # 不留死锁条目
        # 下一个进程可以重试创建
        with guard.shared("fx", lambda: "ok", owner="w1") as fx:
            assert fx.value == "ok"

    def test_teardown_failure_is_swallowed(self, guard):
        def bad_teardown(value):
            raise RuntimeError("cleanup boom")

        with guard.shared("fx", lambda: 1, bad_teardown, owner="w0"):
            pass  # 不抛异常
        assert "fx" not in guard.entries()

    def test_nested_different_names_independent(self, guard):
        with guard.shared("a", lambda: "A", owner="w0") as fa:
            with guard.shared("b", lambda: "B", owner="w0") as fb:
                assert (fa.value, fb.value) == ("A", "B")
        assert guard.entries() == {}


class TestTakeover:
    def test_takeover_when_creator_stale(self, tmp_path):
        state = tmp_path / "guard.json"
        # 写入一个“创建者停滞超时”的僵尸条目(时间维度接管,不依赖 PID)
        state.write_text(json.dumps({"version": 1, "fixtures": {"fx": {
            "state": "creating", "owner": "ghost", "pid": 2_000_000_000,
            "host": socket.gethostname(), "created_at": time.time() - 100,
            "value": None, "refcount": 0,
        }}}), encoding="utf-8")
        g = SharedFixtureGuard(state, lock_timeout=5, wait_ready_timeout=5,
                               takeover_after=0.05, poll_interval=0.02)
        with g.shared("fx", lambda: "reborn", owner="w0") as fx:
            assert fx.role == "creator"
            assert fx.value == "reborn"

    def test_takeover_after_stale_time(self, tmp_path):
        state = tmp_path / "guard.json"
        state.write_text(json.dumps({"version": 1, "fixtures": {"fx": {
            "state": "creating", "owner": "ghost", "pid": 2_000_000_000,
            "host": socket.gethostname(),
            "created_at": time.time() - 100, "value": None, "refcount": 0,
        }}}), encoding="utf-8")
        g = SharedFixtureGuard(state, lock_timeout=5, wait_ready_timeout=5,
                               takeover_after=0.05, poll_interval=0.02)
        with g.shared("fx", lambda: "fresh", owner="w0") as fx:
            assert fx.value == "fresh"

    def test_ready_with_zero_refcount_is_recycled(self, tmp_path):
        """ready 但引用计数为 0(无活跃持有者):条目被回收并重建。"""
        state = tmp_path / "guard.json"
        state.write_text(json.dumps({"version": 1, "fixtures": {"fx": {
            "state": "ready", "owner": "ghost", "pid": 2_000_000_000,
            "host": socket.gethostname(), "created_at": time.time() - 10,
            "value": {"v": 7}, "refcount": 0,
        }}}), encoding="utf-8")
        g = SharedFixtureGuard(state, lock_timeout=5, poll_interval=0.02)
        with g.shared("fx", lambda: "new", owner="w0") as fx:
            assert fx.role == "creator"
            assert fx.value == "new"

    def test_wait_timeout_raises(self, tmp_path):
        state = tmp_path / "guard.json"
        live_pid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]).pid
        try:
            state.write_text(json.dumps({"version": 1, "fixtures": {"fx": {
                "state": "creating", "owner": "other", "pid": live_pid,
                "host": socket.gethostname(), "created_at": time.time(),
                "value": None, "refcount": 0,
            }}}), encoding="utf-8")
            g = SharedFixtureGuard(state, lock_timeout=5, wait_ready_timeout=0.3,
                                   takeover_after=60, poll_interval=0.05)
            with pytest.raises(FixtureGuardError, match="waiting for fixture"):
                with g.shared("fx", lambda: "x", owner="w0"):
                    pass
        finally:
            subprocess.run(["kill", str(live_pid)], check=False)


class TestExclusive:
    def test_serializes_two_processes(self, tmp_path):
        """两个进程同名 exclusive 段:写入时间区间不重叠。"""
        state = tmp_path / "guard.json"
        snippet = textwrap.dedent(
            """
            import sys, time
            from atf.fixtures import SharedFixtureGuard
            log = sys.argv[2]
            g = SharedFixtureGuard(sys.argv[1], lock_timeout=10)
            with g.exclusive("critical"):
                open(log, "a").write(f"{time.time():.6f} enter\\n")
                time.sleep(0.3)
                open(log, "a").write(f"{time.time():.6f} exit\\n")
            """
        )
        logs = [tmp_path / f"log{i}.txt" for i in (1, 2)]
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", snippet, str(state), str(log)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=_CHILD_ENV,
            )
            for log in logs
        ]
        for p in procs:
            p.communicate(timeout=60)
        events = []
        for log in logs:
            for line in log.read_text().splitlines():
                ts, action = line.split()
                events.append((float(ts), action))
        events.sort()
        # 严格串行:任意时刻 enter 之后必须先 exit 才能再 enter
        depth = 0
        for _, action in events:
            depth += 1 if action == "enter" else -1
            assert depth in (0, 1)
        assert [a for _, a in events] == ["enter", "exit", "enter", "exit"]


_CONCURRENT_SNIPPET = textwrap.dedent(
    """
    import sys, time
    from atf.fixtures import SharedFixtureGuard
    state, marker, owner = sys.argv[1], sys.argv[2], sys.argv[3]

    def create():
        with open(marker, "a") as f:
            f.write("CREATE\\n")
        return {"v": 1}

    g = SharedFixtureGuard(state, lock_timeout=10, wait_ready_timeout=30,
                           takeover_after=30, poll_interval=0.05)
    with g.shared("env", create, owner=owner) as fx:
        time.sleep(0.4)  # 制造重叠持有窗口
        assert fx.value == {"v": 1}
    print("DONE")
    """
)


class TestConcurrentProcesses:
    def test_create_once_share_among_processes(self, tmp_path):
        """三进程并发 shared:create 只执行一次,全部 DONE。"""
        state = tmp_path / "guard.json"
        marker = tmp_path / "created.txt"
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _CONCURRENT_SNIPPET,
                 str(state), str(marker), f"proc-{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=_CHILD_ENV,
            )
            for i in range(3)
        ]
        outs = [p.communicate(timeout=60) for p in procs]
        for p, (out, err) in zip(procs, outs):
            assert p.returncode == 0, err
            assert out.strip() == "DONE"
        assert marker.read_text() == "CREATE\n"
        assert not (tmp_path / "guard.json").exists()  # 全部退出后条目清理


class TestRefCount:
    def test_refcount_increments_on_enter_and_decrements_on_exit(self, tmp_path):
        """多持有者进出应正确增减引用计数,归零才清理。"""
        state = tmp_path / "guard.json"
        g = SharedFixtureGuard(state, lock_timeout=5, poll_interval=0.02)
        with g.shared("fx", lambda: {"v": 1}, owner="w0") as a:
            assert a.role == "creator"
            with g.shared("fx", lambda: {"v": 1}, owner="w1") as b:
                assert b.role == "user"
                assert g.entries()["fx"]["refcount"] == 2
            # w1 退出:refcount 回到 1,条目仍在
            assert g.entries()["fx"]["refcount"] == 1
        # w0 退出:refcount 归零,条目删除
        assert "fx" not in g.entries()
