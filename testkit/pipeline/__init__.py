"""Pipeline orchestration package."""

from testkit.pipeline.stage import PASSED, FAILED, SKIPPED, Pipeline, StageResult

__all__ = ["Pipeline", "StageResult", "PASSED", "FAILED", "SKIPPED"]
