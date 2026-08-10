from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_dir: Path
    analysis_dir: Path
    host: str = "127.0.0.1"
    port: int = 4180


def load_settings() -> Settings:
    package_dir = Path(__file__).resolve().parent
    project_root = package_dir.parents[1]
    data_env = os.environ.get("BID_AUDIT_DATA_DIR")
    data_dir = Path(data_env).expanduser().resolve() if data_env else project_root / "data"
    analysis_env = os.environ.get("BID_AUDIT_ANALYSIS_DIR")
    analysis_dir = Path(analysis_env).expanduser().resolve() if analysis_env else project_root / "data" / "analysis"
    return Settings(project_root=project_root, data_dir=data_dir, analysis_dir=analysis_dir)
