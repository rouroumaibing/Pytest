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
    def test_takeover_when_creator_pid_dead(self, tmp_path):
        state = tmp_path / "guard.json"
        # 写入一个“创建者已死”的僵尸条目
        state.write_text(json.dumps({"version": 1, "fixtures": {"fx": {
            "state": "creating", "owner": "ghost", "pid": 2_000_000_000,
            "host": socket.gethostname(), "created_at": time.time(),
            "value": None, "holders": [],
        }}}), encoding="utf-8")
        g = SharedFixtureGuard(state, lock_timeout=5, wait_ready_timeout=5,
                               takeover_after=60, poll_interval=0.02)
        with g.shared("fx", lambda: "reborn", owner="w0") as fx:
            assert fx.role == "creator"
            assert fx.value == "reborn"

    def test_takeover_after_stale_time(self, tmp_path):
        state = tmp_path / "guard.json"
        state.write_text(json.dumps({"version": 1, "fixtures": {"fx": {
            "state": "creating", "owner": "ghost", "pid": 2_000_000_000,
            "host": socket.gethostname(),
            "created_at": time.time() - 100, "value": None, "holders": [],
        }}}), encoding="utf-8")
        g = SharedFixtureGuard(state, lock_timeout=5, wait_ready_timeout=5,
                               takeover_after=0.05, poll_interval=0.02)
        with g.shared("fx", lambda: "fresh", owner="w0") as fx:
            assert fx.value == "fresh"

    def test_ready_with_dead_holders_is_recycled(self, tmp_path):
        """ready 但持有者全部死亡:条目被回收并重建,而不是沿用可疑旧值。"""
        state = tmp_path / "guard.json"
        state.write_text(json.dumps({"version": 1, "fixtures": {"fx": {
            "state": "ready", "owner": "ghost", "pid": 2_000_000_000,
            "host": socket.gethostname(), "created_at": time.time() - 10,
            "value": {"v": 7}, "holders": [
                {"owner": "ghost", "pid": 2_000_000_000,
                 "host": socket.gethostname(), "at": time.time()},
            ],
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
                "value": None, "holders": [],
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


class TestHeartbeat:
    def test_heartbeat_keeps_long_held_fixture_alive(self, tmp_path):
        """长持有(远超 holder_stale_after)且开启心跳时,应被续租而非误回收。"""
        g = SharedFixtureGuard(
            tmp_path / "hb.json", lock_timeout=5,
            holder_stale_after=0.2, poll_interval=0.02,
        )
        created: list = []

        def create():
            created.append(1)
            return {"v": 1}

        with g.shared("fx", create, owner="w0"):
            time.sleep(0.6)  # 远超 holder_stale_after(0.2)
            # 第二个持有者进入:心跳应保住 w0,使其被复用而非重建
            with g.shared("fx", create, owner="w1") as fx2:
                assert fx2.role == "user"
        assert created.count(1) == 1  # create 仅执行一次

    def test_no_heartbeat_allows_stale_pruning(self, tmp_path):
        """关闭心跳且持有远超 stale 时,持有者被判定失效并触发重建。"""
        g = SharedFixtureGuard(
            tmp_path / "hb2.json", lock_timeout=5,
            holder_stale_after=0.15, poll_interval=0.02,
        )
        created: list = []

        def create():
            created.append(1)
            return {"v": 1}

        with g.shared("fx", create, owner="w0", heartbeat=False):
            time.sleep(0.5)  # 远超 stale,且无心跳
            with g.shared("fx", create, owner="w1") as fx2:
                assert fx2.role == "creator"  # w0 被判定失效,重建
        assert created.count(1) == 2  # 重建导致 create 再跑一次
