"""各ソースの取得を独立に実行し、結果をキャッシュする。

* 1ソースの失敗で他を止めない（ソースごとに try）
* 取得失敗時、前回成功分があれば ``stale``（旧データ）として明示して残す
* 失敗を 0 件・0 円に置き換えない（data は空のまま／旧データ）
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .config import Settings
from .models import SourceResult, failure
from .sources import batch_status, cashflow, flycka, slack, todo

logger = logging.getLogger(__name__)

CACHE_FILE = "sources.json"

FETCHERS: dict[str, tuple[str, Callable[[Settings], SourceResult]]] = {
    "todo": (todo.LABEL, todo.fetch),
    "cash": (cashflow.LABEL, cashflow.fetch),
    "flycka": (flycka.LABEL, flycka.fetch),
    "slack": (slack.LABEL, slack.fetch),
    "batch": (batch_status.LABEL, batch_status.fetch),
}


def load_cache(settings: Settings) -> dict[str, SourceResult]:
    """前回のソース結果を読む（無ければ空）。"""
    path = settings.cache_dir / CACHE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k: SourceResult.from_dict(v) for k, v in raw.items()}
    except (OSError, ValueError):
        return {}


def save_cache(settings: Settings, results: dict[str, SourceResult]) -> None:
    """ソース結果を保存する（原子的に置換）。"""
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    path = settings.cache_dir / CACHE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v.to_dict() for k, v in results.items()},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def merge_with_previous(new: SourceResult, prev: SourceResult | None) -> SourceResult:
    """取得失敗時に前回成功データを旧データとして引き継ぐ。"""
    if new.status not in ("error", "unconfigured") or new.data:
        return new
    if prev is None or not prev.data or prev.status == "error":
        return new
    merged = SourceResult.from_dict(prev.to_dict())
    merged.status = "stale"
    merged.fetched_at = new.fetched_at
    merged.errors = new.errors + [f"前回成功（{prev.last_success_at}）のデータを表示中"]
    merged.notes = list(dict.fromkeys(new.notes + prev.notes))
    return merged


def run_sync(settings: Settings, only: list[str] | None = None) -> dict[str, SourceResult]:
    """ソースを並行取得し、キャッシュを更新して全ソースの最新状態を返す。"""
    prev = load_cache(settings)
    targets = {k: v for k, v in FETCHERS.items() if not only or k in only}

    def run_one(key: str) -> tuple[str, SourceResult]:
        label, fn = targets[key]
        try:
            return key, fn(settings)
        except Exception as e:  # 想定外でも他ソースを止めない
            logger.exception("ソース %s で想定外の例外", key)
            return key, failure(key, label, e)

    results = dict(prev)
    with ThreadPoolExecutor(max_workers=len(targets) or 1) as ex:
        for key, res in ex.map(run_one, list(targets)):
            results[key] = merge_with_previous(res, prev.get(key))
    save_cache(settings, results)
    return results


def summary(results: dict[str, SourceResult]) -> list[dict[str, Any]]:
    """同期状態の一覧（画面表示用）。"""
    order = list(FETCHERS)
    return [{"name": r.name, "label": r.label, "status": r.status, "fetchedAt": r.fetched_at,
             "lastSuccessAt": r.last_success_at, "dataAsOf": r.data_as_of, "scope": r.scope,
             "errors": r.errors, "notes": r.notes}
            for r in sorted(results.values(), key=lambda x: order.index(x.name) if x.name in order else 99)]
