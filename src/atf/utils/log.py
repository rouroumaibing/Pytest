"""统一日志工厂。

xdist 并发下多 worker 的日志会交错,因此默认格式带进程号 ``%(process)d``,
可直接区分不同 worker 的输出。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional, Union

_LOG_FORMAT = "%(asctime)s [%(process)d] %(name)s %(levelname)s %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_configured = False


def setup_logging(
    level: Union[int, str] = logging.INFO,
    log_file: Optional[Union[str, Path]] = None,
) -> None:
    """初始化根日志(整个进程只需调用一次)。

    Args:
        level: 日志级别,支持 ``"DEBUG"`` 等字符串或 ``logging.DEBUG`` 等常量。
        log_file: 可选的日志文件路径;设置后同时输出到 stderr 与该文件。
    """
    global _configured
    if _configured:
        return
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt=_DATE_FORMAT, handlers=handlers)
    _configured = True


def get_logger(name: str) -> logging.Logger:  # pragma: no cover - 保留兼容薄封装
    """按名称获取 logger,直接委托标准库(向后兼容旧调用点)。"""
    return logging.getLogger(name)
