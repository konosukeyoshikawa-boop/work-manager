"""Google Sheets の読み取り専用クライアント。

* スコープは ``spreadsheets.readonly`` に限定する（書き込み API を物理的に呼べない）
* 429/5xx は gspread の ``BackOffHTTPClient`` で指数バックオフ再試行
* 鍵は backoffice が正規に管理する SA ファイルを参照するだけで、内容は扱わない
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

READONLY_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetAccessError(RuntimeError):
    """シートを開けない（未共有・鍵なし等）。"""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        #: permission / missing_credentials / not_found / other
        self.kind = kind


def open_readonly(spreadsheet_id: str, sa_path: Path) -> Any:
    """読み取り専用でスプレッドシートを開く。

    Args:
        spreadsheet_id: スプレッドシートID。
        sa_path: サービスアカウント鍵ファイルのパス。

    Returns:
        gspread.Spreadsheet。

    Raises:
        SheetAccessError: 鍵が無い・SA に共有されていない・存在しない場合。
    """
    import gspread
    from google.oauth2.service_account import Credentials

    if not sa_path.exists():
        raise SheetAccessError(
            f"サービスアカウント鍵が見つかりません（{sa_path.name}）", kind="missing_credentials")
    creds = Credentials.from_service_account_file(str(sa_path), scopes=READONLY_SCOPES)
    client = gspread.authorize(creds, http_client=gspread.BackOffHTTPClient)
    try:
        return client.open_by_key(spreadsheet_id)
    except PermissionError as e:
        raise SheetAccessError(
            "サービスアカウントに閲覧権限がありません（シート未共有）", kind="permission") from e
    except gspread.exceptions.SpreadsheetNotFound as e:
        raise SheetAccessError("スプレッドシートが見つかりません", kind="not_found") from e
    except gspread.exceptions.APIError as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (403, 404):
            raise SheetAccessError(
                f"閲覧権限がないか存在しません（HTTP {code}）", kind="permission") from e
        raise


def service_account_email(sa_path: Path) -> str | None:
    """共有設定の案内用に SA のメールアドレスだけを返す（鍵本体は読まない側に出さない）。"""
    import json

    try:
        return json.loads(sa_path.read_text(encoding="utf-8")).get("client_email")
    except (OSError, ValueError):
        return None


def sheet_url(spreadsheet_id: str, gid: int | None = None, a1: str | None = None) -> str:
    """元データへのリンクURLを作る。"""
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    if gid is not None:
        url += f"#gid={gid}"
        if a1:
            url += f"&range={a1}"
    return url
