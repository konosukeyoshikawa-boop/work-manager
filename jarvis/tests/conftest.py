"""共通フィクスチャ。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """キャッシュを一時ディレクトリに向けた設定。"""
    return Settings(cache_dir=tmp_path / "cache", google_sa_path=tmp_path / "none.json",
                    slack_env_path=tmp_path / ".env")


def make_task(**kw: str) -> dict:
    """Todo 1件（既定は新鮮な未着手）。"""
    base = {"id": "T-001", "business": "共通経営基盤", "area": "経営管理", "project": "案件",
            "title": "タスク", "category": "", "priority": "P1｜今週", "status": "未着手",
            "due": "", "done": "", "first5": "資料を開く", "duration": "30分", "assignee": "本人",
            "ownership": "本人のみ｜経営判断", "waitingFor": "", "origin": "", "updated": "2026/10/04",
            "notes": "", "row": 2, "url": "https://docs.google.com/x"}
    base.update(kw)
    return base
