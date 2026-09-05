"""Pipeline orchestration package."""

from testkit.pipeline.stage import FAILED, PASSED, SKIPPED, Pipeline, StageResult

__all__ = ["Pipeline", "StageResult", "PASSED", "FAILED", "SKIPPED"]
