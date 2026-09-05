"""日志模块 — 将运行日志写入配置指定的目录（默认 ./logs）。

日志文件按日期命名: <log_dir>/imgcompressor_YYYY-MM-DD.log
控制台不输出（TUI 占用终端），仅写文件。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

# 默认 logs 目录（本模块所在工程根目录下的 ./logs）
_DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"

_LOGGER_NAME = "imgcompressor"
_logger: logging.Logger | None = None


def get_logger(log_dir: str | Path | None = None) -> logging.Logger:
    """返回配置好的单例 logger（文件输出到 log_dir，默认 ./logs）。

    Args:
        log_dir: 日志目录。可为相对路径（相对于程序目录解析）或绝对路径。
                 传 None 时回退到工程根目录下的 ./logs。
    """
    global _logger
    if _logger is not None:
        return _logger

    if log_dir is None:
        base = _DEFAULT_LOG_DIR
    else:
        base = Path(log_dir)
        if not base.is_absolute():
            base = Path(__file__).resolve().parent / base

    base.mkdir(parents=True, exist_ok=True)
    log_file = base / f"imgcompressor_{datetime.now():%Y-%m-%d}.log"

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 不冒泡到 root，避免污染 TUI 终端

    # 避免重复添加 handler（多次导入 / 重启）
    if not logger.handlers:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    _logger = logger
    logger.info("=" * 60)
    logger.info("日志已初始化 — 文件: %s", log_file)
    return logger


def get_log_dir(log_dir: str | Path | None = None) -> Path:
    """返回日志目录路径。

    Args:
        log_dir: 同 get_logger；传 None 时回退到工程根目录下的 ./logs。
    """
    if log_dir is None:
        return _DEFAULT_LOG_DIR
    base = Path(log_dir)
    if not base.is_absolute():
        base = Path(__file__).resolve().parent / base
    return base
