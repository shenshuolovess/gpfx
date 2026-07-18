"""统一的控制台与轮转文件日志配置。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pipeline_config import config_value, project_path


DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def default_log_dir() -> Path:
    return project_path(config_value("logging", "directory", "data/output/logs"))


def get_rotating_logger(
    name: str,
    log_file: str | Path,
    *,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATE_FORMAT)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    if console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        path,
        maxBytes=int(config_value("logging", "max_bytes", 5 * 1024 * 1024)),
        backupCount=int(config_value("logging", "backup_count", 5)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def configure_root_rotating_logging(log_file: str | Path, *, level: int = logging.INFO) -> None:
    logger = get_rotating_logger("__root_logging_setup__", log_file, level=level)
    handlers = list(logger.handlers)
    logger.handlers.clear()
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
