"""项目统一配置与输入文件发现工具。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from stock_utils import latest_matching_file


PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_DIR / "pipeline_config.toml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path).expanduser() if path else CONFIG_FILE
    if not config_path.is_absolute():
        config_path = PROJECT_DIR / config_path
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


CONFIG = load_config()


def config_value(section: str, key: str, default: Any = None) -> Any:
    return CONFIG.get(section, {}).get(key, default)


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def latest_file(pattern: str, *, required: bool = True) -> Path | None:
    return latest_matching_file(PROJECT_DIR, pattern, required=required)


def resolve_input(
    explicit: str | Path | None,
    *,
    config_key: str | None = None,
    pattern_key: str | None = None,
) -> Path:
    """显式路径优先，否则从配置的固定路径或通配模式解析输入。"""
    if explicit:
        path = project_path(explicit)
    elif pattern_key:
        pattern = config_value("files", pattern_key)
        if not pattern:
            raise KeyError(f"配置缺少 files.{pattern_key}")
        return latest_file(str(pattern))
    elif config_key:
        value = config_value("files", config_key)
        if not value:
            raise KeyError(f"配置缺少 files.{config_key}")
        path = project_path(value)
    else:
        raise ValueError("必须指定 config_key 或 pattern_key")

    if not path.is_file():
        raise FileNotFoundError(f"输入文件不存在：{path}")
    return path
