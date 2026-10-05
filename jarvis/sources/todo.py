"""事業横断Todo・経営計画の取得（読み取り専用）。

列は位置ではなく見出し名で対応付ける（列の追加・並べ替えに耐える）。
件数・列は固定しない。古い「今日中」「今週」の解釈は判断層（engine）が行う。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..config import TAB_INBOX, TAB_PLAN, TAB_TODO, TAB_WEEK_BOARD, Settings
from ..models import SourceResult, failure, iso, now_jst
from ..parsing import norm, parse_date
from .sheets import SheetAccessError, open_readonly, service_account_email, sheet_url

logger = logging.getLogger(__name__)

NAME = "todo"
LABEL = "Todo・経営計画"

#: 見出し名 → 内部キー（試作 v0.1 のデータ契約と同じキー）
HEADER_MAP: dict[str, str] = {
    "ID": "id", "事業区分": "business", "機能領域": "area",
    "プロジェクト・案件": "project", "Todo名": "title", "カテゴリ": "category",
    "優先度": "priority", "ステータス": "status", "期限": "due", "完了条件": "done",
    "最初の5分": "first5", "所要時間": "duration", "担当": "assignee",
    "本人が抱えるか": "ownership", "確認待ち相手": "waitingFor", "情報源": "origin",
    "最終更新日": "updated", "備考": "notes",
}
REQUIRED_KEYS = ("id", "title", "status")


def parse_todo_rows(values: list[list[str]], *, sheet_id: str, gid: int | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Todoマスターの全行を内部形式に変換する。

    Args:
        values: ``get_all_values()`` の結果。
        sheet_id: スプレッドシートID（リンク生成用）。
        gid: タブID（リンク生成用）。

    Returns:
        (タスク一覧, 見出し一覧, 警告一覧)。

    Raises:
        ValueError: 見出し行や必須列が見つからない場合。
    """
    header_idx = next((i for i, r in enumerate(values[:10]) if r and norm(r[0]) == "ID"), None)
    if header_idx is None:
        raise ValueError("Todoマスターの見出し行（A列=ID）が見つかりません")
    header = [norm(h) for h in values[header_idx]]
    col: dict[str, int] = {}
    unknown: list[str] = []
    for i, h in enumerate(header):
        if h in HEADER_MAP:
            col[HEADER_MAP[h]] = i
        elif h:
            unknown.append(h)
    missing = [k for k in REQUIRED_KEYS if k not in col]
    if missing:
        raise ValueError(f"必須列が見つかりません: {missing}")

    warnings: list[str] = []
    if unknown:
        warnings.append(f"未知の列（判断には未使用）: {', '.join(unknown)}")
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for offset, row in enumerate(values[header_idx + 1:], start=header_idx + 2):
        cells = {k: norm(row[i]) if i < len(row) else "" for k, i in col.items()}
        if not cells.get("id") and not cells.get("title"):
            continue
        tid = cells.get("id") or f"ROW-{offset}"
        if tid in seen:
            warnings.append(f"ID重複: {tid}（{offset}行目）— 後の行を別IDとして扱います")
            tid = f"{tid}#{offset}"
        seen.add(tid)
        task = {k: cells.get(k, "") for k in HEADER_MAP.values()}
        task["id"] = tid
        task["row"] = offset
        task["url"] = sheet_url(sheet_id, gid, f"A{offset}:R{offset}")
        tasks.append(task)
    return tasks, header, warnings


def parse_week_board(values: list[list[str]]) -> dict[str, Any]:
    """「今日・今週」ボードの更新日を取り出す（古い指示の転用防止に使う）。"""
    title_note = norm(values[0][1]) if values and len(values[0]) > 1 else ""
    return {"note": title_note, "updated": iso_date(parse_date(title_note)),
            "rows": max(len(values) - 2, 0)}


def iso_date(d: Any) -> str | None:
    """date を ISO 文字列に。"""
    return d.isoformat() if d else None


def fetch(settings: Settings) -> SourceResult:
    """Todo管理表を読み取る。

    SA 未共有など権限不足の場合は ``unconfigured`` を返し、設定済みなら
    手動スナップショットを ``stale`` として返す（自動同期扱いにしない）。
    """
    fetched = iso(now_jst())
    try:
        sh = open_readonly(settings.todo_sheet_id, settings.google_sa_path)
        ws = sh.worksheet(TAB_TODO)
        tasks, header, warnings = parse_todo_rows(
            ws.get_all_values(), sheet_id=settings.todo_sheet_id, gid=ws.id)
        data: dict[str, Any] = {"tasks": tasks, "columns": header,
                                "sheetUrl": sheet_url(settings.todo_sheet_id)}
        errors: list[str] = []
        for tab, key in ((TAB_PLAN, "plan"), (TAB_WEEK_BOARD, "weekBoard"), (TAB_INBOX, "inbox")):
            try:
                w = sh.worksheet(tab)
                vals = w.get_all_values()
                if key == "plan":
                    data["plan"] = {"rows": vals, "url": sheet_url(settings.todo_sheet_id, w.id)}
                elif key == "weekBoard":
                    data["weekBoard"] = parse_week_board(vals)
                else:
                    data["inbox"] = {"pending": sum(
                        1 for r in vals[2:] if r and norm(r[0]) and "Todoマスターへ移動" not in "".join(r)),
                        "url": sheet_url(settings.todo_sheet_id, w.id)}
            except Exception as e:  # 1タブの失敗で全体を止めない
                errors.append(f"{tab}: {type(e).__name__}: {e}")
        return SourceResult(
            name=NAME, label=LABEL, status="partial" if errors else "ok",
            fetched_at=fetched, last_success_at=fetched, data_as_of=fetched,
            scope=f"{TAB_TODO} {len(tasks)}件 / 列{len(header)} / {TAB_PLAN} / {TAB_WEEK_BOARD} / {TAB_INBOX}",
            errors=errors, notes=warnings, data=data)
    except SheetAccessError as e:
        email = service_account_email(settings.google_sa_path) or "（SA鍵なし）"
        res = failure(NAME, LABEL, f"{e}。共有設定に閲覧者として {email} を追加してください",
                      status="unconfigured", scope=TAB_TODO)
        return _with_snapshot(res, settings)
    except Exception as e:
        logger.exception("Todo取得失敗")
        return _with_snapshot(failure(NAME, LABEL, e, scope=TAB_TODO), settings)


def _with_snapshot(res: SourceResult, settings: Settings) -> SourceResult:
    """手動スナップショットが設定されていれば旧データとして付ける。"""
    path: Path | None = settings.todo_snapshot_path
    if not path or not path.exists():
        return res
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
        if snap.get("schemaVersion") != 1 or not isinstance(snap.get("tasks"), list):
            res.errors.append("スナップショットの形式が不正のため未使用")
            return res
        res.status = "stale"
        res.data = {"tasks": snap["tasks"], "plan": snap.get("plan"),
                    "sheetUrl": sheet_url(settings.todo_sheet_id), "snapshot": True}
        res.data_as_of = snap.get("readAt")
        res.last_success_at = snap.get("readAt")
        res.notes.append(
            f"手動取得スナップショット（{snap.get('readAt')} 時点）を表示中。自動同期ではありません")
    except (OSError, ValueError) as e:
        res.errors.append(f"スナップショット読込失敗: {e}")
    return res
