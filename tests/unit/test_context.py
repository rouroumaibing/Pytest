"""TestContext 单元测试。"""

from __future__ import annotations

import pytest

from atf.context import TestContext


class TestRegister:
    def test_register_returns_value(self):
        ctx = TestContext("t")
        resource = {"id": "r1"}
        assert ctx.register(resource, lambda r: None) is resource

    def test_register_after_cleanup_rejected(self):
        ctx = TestContext("t")
        ctx.cleanup()
        with pytest.raises(RuntimeError, match="already-cleaned"):
            ctx.register(object(), lambda o: None)

    def test_pending_count(self):
        ctx = TestContext("t")
        ctx.add_finalizer(lambda: None)
        ctx.add_finalizer(lambda: None)
        assert ctx.pending == 2
        ctx.cleanup()
        assert ctx.pending == 0


class TestCleanup:
    def test_lifo_order(self):
        order = []
        ctx = TestContext("t")
        ctx.add_finalizer(lambda: order.append("first"))
        ctx.add_finalizer(lambda: order.append("second"))
        ctx.cleanup()
        assert order == ["second", "first"]

    def test_errors_are_isolated_and_collected(self):
        order = []
        ctx = TestContext("t")
        ctx.add_finalizer(lambda: order.append("a"))
        ctx.add_finalizer(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        ctx.add_finalizer(lambda: order.append("c"))
        report = ctx.cleanup()
        assert order == ["c", "a"]  # b 失败但 a 仍执行
        assert not report.ok
        assert len(report.failures) == 1
        assert "boom" in report.failures[0][1]

    def test_cleanup_idempotent(self):
        calls = []
        ctx = TestContext("t")
        ctx.add_finalizer(lambda: calls.append(1))
        ctx.cleanup()
        ctx.cleanup()
        assert calls == [1]

    def test_default_finalizer_uses_close(self):
        class Conn:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        conn = Conn()
        TestContext("t").register(conn)
        ctx = TestContext("t2")
        ctx.register(conn)
        ctx.cleanup()
        assert conn.closed

    def test_register_without_finalizer_and_no_close_is_noop(self):
        ctx = TestContext("t")
        ctx.register({"plain": "dict"})
        report = ctx.cleanup()
        assert report.ok
        assert any("noop" in d for d in report.executed)


class TestProtocol:
    def test_with_statement_cleans_up(self):
        order = []
        with TestContext("t") as ctx:
            ctx.add_finalizer(lambda: order.append("x"))
            assert ctx.pending == 1
        assert order == ["x"]
        assert ctx.report is not None and ctx.report.ok

    def test_cleanup_runs_even_on_exception(self):
        order = []
        with pytest.raises(ZeroDivisionError):
            with TestContext("t") as ctx:
                ctx.add_finalizer(lambda: order.append("x"))
                1 / 0
        assert order == ["x"]

    def test_repr(self):
        ctx = TestContext("demo")
        assert "demo" in repr(ctx)
        assert "pending" in repr(ctx)
