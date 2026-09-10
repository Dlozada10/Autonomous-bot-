"""Config + credential loading."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader so the project has no python-dotenv dependency."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Existing environment always wins over the file.
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Config:
    def __init__(self, path: str | Path = ROOT / "config.yaml"):
        _load_dotenv()
        self.path = Path(path)
        self.data: dict[str, Any] = yaml.safe_load(self.path.read_text())

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        """Fetch a nested value: cfg.get('video.fps')."""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # --- paths -------------------------------------------------------
    @property
    def out_dir(self) -> Path:
        p = ROOT / self.get("paths.out_dir", "out")
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def assets_dir(self) -> Path:
        p = ROOT / self.get("paths.assets_dir", "assets")
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return ROOT / self.get("paths.db", "state.db")

    # --- credentials -------------------------------------------------
    @staticmethod
    def env(name: str, default: str = "") -> str:
        return os.environ.get(name, default) or default

    def require(self, name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(
                f"{name} is not set. Copy .env.example to .env and fill it in."
            )
        return value
