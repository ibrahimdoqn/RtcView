import os
from pathlib import Path
from typing import Optional, Tuple

import yaml


def resolve_go2rtc_paths() -> Tuple[Path, Path]:
    """(bin_path, config_path) under INSTALL_DIR/go2rtc/. Same RTCVIEW_HOME
    env var app/main.py's resolve_paths() reads, kept as a small local copy
    rather than an import to avoid a circular import (main.py imports this
    module)."""
    install_dir = Path(os.environ.get("RTCVIEW_HOME", "/opt/rtcview"))
    go2rtc_dir = install_dir / "go2rtc"
    return go2rtc_dir / "go2rtc", go2rtc_dir / "go2rtc.yaml"


def read_config() -> str:
    _, config_path = resolve_go2rtc_paths()
    try:
        return config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_config(text: str) -> None:
    """Validates text is well-formed YAML before writing -- go2rtc won't
    start on unparseable config, so a bad save here shouldn't be able to
    take the whole service down on next restart. Atomic write (.tmp +
    os.replace), same idiom as ConfigStore._save_locked() in config.py."""
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"Geçersiz YAML: {e}") from e

    _, config_path = resolve_go2rtc_paths()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, config_path)


def request_restart(touch_trigger) -> None:
    """touch_trigger is app/main.py's _touch_trigger(name) helper, passed
    in rather than imported to avoid a circular import."""
    touch_trigger("go2rtc-restart.trigger")
