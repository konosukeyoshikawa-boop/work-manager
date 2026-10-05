"""定期バッチ（GitHub Actions）の実結果の取得 — 読み取りのみ。

``continue-on-error`` のステップは画面上 ✓ でも実際は失敗していることがある。
また終了コード 0 のまま登録に失敗していた例（自動仕訳・2026-10-04）もあるため、
ジョブログを読み、ステップごとの実結果と既知の失敗パターンを抽出する。
バッチの再実行（workflow_dispatch）は行わない。gh CLI の利用者認証を使う。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from ..config import Settings
from ..models import SourceResult, failure, iso, now_jst, redact

logger = logging.getLogger(__name__)

NAME = "batch"
LABEL = "定期バッチ（GitHub Actions）"

#: コマンド断片 → 表示名（backoffice daily-batch.yml）
BACKOFFICE_STEPS: tuple[tuple[str, str], ...] = (
    ("run_journal.py", "自動仕訳"), ("run_settlement.py", "入金/支払消込"),
    ("cashflow.scripts.run_daily", "資金繰りバッチ"), ("sync_actuals.py", "実績自動転記(R1)"),
    ("transform_", "DB変換"), ("run_sheet_classifier.py", "取引先自動分類(R3)"),
    ("run_slack_notify.py", "ファイナンス日次通知"), ("run_sheet_reminder.py", "シート更新リマインド"),
    ("report_batch_failures.py", "失敗集約通知"),
)
#: 既知の失敗パターン → 説明
KNOWN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"管理番号は20文字以内", "freee管理番号が20文字超過で取引登録が400失敗"),
    (r"\[429\]: Quota exceeded", "Google Sheets 読み取り上限（429）"),
    (r"取引登録失敗", "freee取引登録失敗"),
    (r"invalid_grant|refresh_token", "freee/Google トークン失効の可能性"),
)


def _gh(args: list[str], timeout: int = 60) -> str:
    """gh CLI を実行して標準出力を返す（失敗時は例外）。"""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(redact(proc.stderr.strip() or f"gh {' '.join(args[:2])} failed"))
    return proc.stdout


def parse_job_log(log: str, step_map: tuple[tuple[str, str], ...]) -> list[dict[str, Any]]:
    """ジョブログをステップ単位に分け、実結果を判定する（純関数）。"""
    steps: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in log.splitlines():
        body = line.split("Z ", 1)[1] if re.match(r"^\S+Z ", line) else line
        if body.startswith("##[group]Run "):
            cmd = body[len("##[group]Run "):]
            name = next((n for frag, n in step_map if frag in cmd), None)
            cur = {"name": name or cmd[:40], "known": name is not None, "failed": False,
                   "patterns": {}}
            steps.append(cur)
            continue
        if cur is None:
            continue
        if "##[error]Process completed with exit code" in body:
            cur["failed"] = True
        for pat, label in KNOWN_PATTERNS:
            if re.search(pat, body):
                cur["patterns"][label] = cur["patterns"].get(label, 0) + 1
    known = [s for s in steps if s["known"]]
    for s in known:
        # 終了コード0でもログにエラーがある＝「成功表示の失敗」
        s["hiddenFailure"] = (not s["failed"]) and bool(s["patterns"])
        s["outcome"] = "failure" if s["failed"] else ("warning" if s["hiddenFailure"] else "success")
    return known


def _latest_runs(repo: str, workflow: str, limit: int = 5) -> list[dict[str, Any]]:
    out = _gh(["run", "list", "--repo", repo, "--workflow", workflow, "--limit", str(limit),
               "--json", "databaseId,conclusion,status,createdAt,headSha,event,url"])
    return json.loads(out)


def _job_log(repo: str, run_id: int, cache_dir: Path) -> str:
    """ジョブログを取得（実行IDごとにキャッシュ。完了済みrunのログは不変）。"""
    path = cache_dir / "gh_logs" / f"{repo.replace('/', '_')}_{run_id}.log"
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    jobs = json.loads(_gh(["api", f"repos/{repo}/actions/runs/{run_id}/jobs", "--jq", "[.jobs[].id]"]))
    log = "\n".join(_gh(["api", f"repos/{repo}/actions/jobs/{j}/logs"], timeout=120) for j in jobs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact_log(log), encoding="utf-8")
    return log


def redact_log(log: str) -> str:
    """キャッシュ保存前にログ全体の秘密情報らしき文字列を伏せる。"""
    return re.sub(r"(xox[abposr]-|EAA)[A-Za-z0-9-]{10,}", "***", log)


def fetch(settings: Settings) -> SourceResult:
    """backoffice 日次バッチと flycka-d2c 定期処理の直近結果を返す。"""
    fetched = iso(now_jst())
    errors: list[str] = []
    data: dict[str, Any] = {"backoffice": None, "flycka": []}
    try:
        runs = [r for r in _latest_runs(settings.backoffice_repo, settings.backoffice_workflow)
                if r.get("status") == "completed"]
        if runs:
            latest = runs[0]
            steps = parse_job_log(_job_log(settings.backoffice_repo, latest["databaseId"], settings.cache_dir),
                                  BACKOFFICE_STEPS)
            data["backoffice"] = {
                "workflow": settings.backoffice_workflow, "runId": latest["databaseId"],
                "conclusion": latest["conclusion"], "createdAt": latest["createdAt"],
                "headSha": latest["headSha"], "url": latest["url"], "steps": steps,
                "recent": [{"conclusion": r["conclusion"], "createdAt": r["createdAt"], "url": r["url"]}
                           for r in runs],
            }
    except Exception as e:
        errors.append(redact(f"backoffice: {type(e).__name__}: {e}"))
    for wf in settings.flycka_workflows:
        try:
            runs = [r for r in _latest_runs(settings.flycka_repo, wf, 3) if r.get("status") == "completed"]
            if runs:
                data["flycka"].append({"workflow": wf, "conclusion": runs[0]["conclusion"],
                                       "createdAt": runs[0]["createdAt"], "url": runs[0]["url"]})
        except Exception as e:
            errors.append(redact(f"flycka {wf}: {type(e).__name__}: {e}"))
    if not data["backoffice"] and not data["flycka"]:
        return failure(NAME, LABEL, "; ".join(errors) or "実行履歴なし")
    return SourceResult(
        name=NAME, label=LABEL, status="partial" if errors else "ok", fetched_at=fetched,
        last_success_at=fetched, data_as_of=(data["backoffice"] or {}).get("createdAt"),
        scope=f"{settings.backoffice_repo} {settings.backoffice_workflow}（直近の完了run・ステップ別ログ判定）"
              f" / {settings.flycka_repo} {', '.join(settings.flycka_workflows)}",
        errors=errors,
        notes=["continue-on-error のステップはログの exit code で実結果を判定。",
               "flycka-d2c refresh は一部ステップ失敗でも成功扱いになる実装のため、データ鮮度は各ソース側で確認する。"],
        data=data)
