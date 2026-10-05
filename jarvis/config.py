"""JARVIS の設定（SSoT）。

接続先ID・タブ名・パスはここだけに置く。認証情報の「値」は持たず、
既存プロジェクトが正規に管理しているファイル・環境変数の「場所」だけを参照する。
環境変数 ``JARVIS_*`` で上書きできる。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

#: work-manager/jarvis/
JARVIS_DIR = Path(__file__).resolve().parent
#: ユーザーホーム（既存プロジェクトの配置先）
HOME = Path(os.environ.get("JARVIS_HOME_DIR", str(Path.home())))


def _env_path(name: str, default: Path) -> Path:
    """環境変数があればそのパス、なければ既定パスを返す。"""
    value = os.environ.get(name)
    return Path(value) if value else default


@dataclass(frozen=True)
class Settings:
    """JARVIS 全体の設定値。"""

    # ── 保存先（.gitignore 済み） ──
    cache_dir: Path = field(default_factory=lambda: _env_path(
        "JARVIS_CACHE_DIR", JARVIS_DIR / ".cache"))

    # ── Google Sheets（読み取り専用スコープで接続） ──
    #: backoffice が正規に管理する SA 鍵（.credentials/ 集約ルール）
    google_sa_path: Path = field(default_factory=lambda: _env_path(
        "JARVIS_GOOGLE_SA", HOME / "backoffice" / ".credentials" / "service_account.json"))
    todo_sheet_id: str = os.environ.get(
        "JARVIS_TODO_SHEET_ID", "1hF0D7XkuW9tb50u4ovm9c6mDMINU149Wh3JVBcqT1Vc")
    cash_sheet_id: str = os.environ.get(
        "JARVIS_CASH_SHEET_ID", "1kfMd5zHiy56t8SVKLU4WfiitUCxLJIGZcIFFzU5DLAY")
    #: SA 未共有時に使う「手動取得スナップショット」（任意）。自動同期扱いにしない
    todo_snapshot_path: Path | None = field(default_factory=lambda: (
        Path(os.environ["JARVIS_TODO_SNAPSHOT"]) if os.environ.get("JARVIS_TODO_SNAPSHOT") else None))

    # ── FLYCKA（flycka-d2c の既存取得処理を再利用） ──
    flycka_dir: Path = field(default_factory=lambda: _env_path(
        "JARVIS_FLYCKA_DIR", HOME / "flycka-d2c"))
    flycka_ops_sheet_id: str = "1M8vTiqJrWhJcyt-n0dBYn2Uzd4R_Mj7z_jef9IhnBp4"

    # ── Slack（読み取り専用トークン。値は .env / 環境変数のみ） ──
    slack_env_path: Path = field(default_factory=lambda: _env_path(
        "JARVIS_SLACK_ENV", JARVIS_DIR / ".env"))
    slack_initial_days: int = int(os.environ.get("JARVIS_SLACK_INITIAL_DAYS", "14"))
    slack_thread_watch_days: int = int(os.environ.get("JARVIS_SLACK_THREAD_DAYS", "30"))
    slack_workspace_domain: str = os.environ.get(
        "JARVIS_SLACK_DOMAIN", "growth-and-technology")

    # ── GitHub Actions（gh CLI の利用者認証で読むだけ） ──
    backoffice_repo: str = "konosukeyoshikawa-boop/backoffice"
    backoffice_workflow: str = "daily-batch.yml"
    flycka_repo: str = "konosukeyoshikawa-boop/flycka-d2c"
    flycka_workflows: tuple[str, ...] = ("refresh.yml", "daily-report.yml")

    # ── 判断ルール ──
    #: 最終更新からこの日数を超えたTodoは「現状確認」扱い
    stale_task_days: int = 14
    #: 現状確認の枠（分）
    stale_check_minutes: int = 10
    #: 所要時間が不明なときの仮置き（分）
    unknown_duration_minutes: int = 30
    #: 資金データがこの日数より古ければ「古い」と表示
    cash_stale_days: int = 3

    # ── サーバー ──
    host: str = "127.0.0.1"
    port: int = int(os.environ.get("JARVIS_PORT", "8787"))


def load_settings() -> Settings:
    """現在の環境変数から設定を作る。"""
    return Settings()


# ── 資金繰り原本のタブ名（backoffice/cashflow/sheet_names.py と同一の正本名） ──
TAB_DAILY_CF = "03_日次CF"
TAB_DAILY_SUMMARY = "03_日次サマリ"
TAB_INPUT_BANK = "01_入力_銀行入出金"
TAB_UPDATE_LOG = "06_設定_更新履歴"
#: バッチ管理外の保存予測（作成コードがリポジトリに存在しない手動スナップショット）
TAB_13W = "03_13週資金繰り"

# ── Todo 管理表のタブ名 ──
TAB_TODO = "Todoマスター"
TAB_PLAN = "16か月経営計画"
TAB_STRUCTURE = "事業構造"
TAB_WEEK_BOARD = "今日・今週"
TAB_INBOX = "Inbox"
