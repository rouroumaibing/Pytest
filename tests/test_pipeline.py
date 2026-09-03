"""Unit tests for multi-stage pipeline orchestration (Pipeline / StageResult)."""

from __future__ import annotations

import pytest

from testkit import Pipeline
from testkit.exceptions import PipelineError
from testkit.pipeline.stage import PASSED, FAILED, SKIPPED, StageResult


def test_stages_run_in_order():
    order = []
    p = Pipeline()
    p.add_stage("s1", lambda: order.append("s1"))
    p.add_stage("s2", lambda: order.append("s2"))
    p.add_stage("s3", lambda: order.append("s3"))
    results = p.run()
    assert [r.status for r in results] == [PASSED, PASSED, PASSED]
    assert order == ["s1", "s2", "s3"]
    assert p.success is True


def test_failure_skips_subsequent_stages():
    order = []
    p = Pipeline()
    p.add_stage("s1", lambda: order.append("s1"))

    def boom():
        order.append("s2")
        raise RuntimeError("boom")

    p.add_stage("s2", boom)
    p.add_stage("s3", lambda: order.append("s3"))
    results = p.run()
    assert [r.status for r in results] == [PASSED, FAILED, SKIPPED]
    assert p.success is False
    assert order == ["s1", "s2"]


def test_resume_from_skips_earlier_stages():
    order = []
    p = Pipeline()
    p.add_stage("s1", lambda: order.append("s1"))
    p.add_stage("s2", lambda: order.append("s2"))
    p.add_stage("s3", lambda: order.append("s3"))
    results = p.run(resume_from="s2")
    assert [r.status for r in results] == [SKIPPED, PASSED, PASSED]
    assert order == ["s2", "s3"]
    assert p.success is True


def test_resume_from_unknown_stage_raises():
    p = Pipeline()
    p.add_stage("s1", lambda: None)
    with pytest.raises(PipelineError):
        p.run(resume_from="nope")


def test_duplicate_stage_name_raises():
    p = Pipeline()
    p.add_stage("s1", lambda: None)
    with pytest.raises(PipelineError):
        p.add_stage("s1", lambda: None)


def test_stage_decorator():
    p = Pipeline()

    @p.stage("decorated")
    def my_stage():
        return 42

    results = p.run()
    assert results[0].status == PASSED
    assert results[0].data == 42


def test_stage_receives_context_when_accepts_arg():
    seen = {}

    def stage_fn(ctx):
        seen["ctx"] = ctx

    p = Pipeline()
    p.add_stage("s1", stage_fn)
    p.run(context={"k": "v"})
    assert seen["ctx"] == {"k": "v"}


def test_stage_ignores_context_when_zero_arg():
    p = Pipeline()
    p.add_stage("s1", lambda: None)
    p.run(context={"k": "v"})  # must not raise


def test_stage_result_properties():
    assert StageResult("x", PASSED).passed is True
    assert StageResult("x", FAILED).failed is True
    assert StageResult("x", SKIPPED).skipped is True


def test_skipped_stage_does_not_affect_success():
    p = Pipeline()
    p.add_stage("s1", lambda: None)
    p.add_stage("s2", lambda: None)
    p.run(resume_from="s2")
    assert p.success is True
