"""判断層・同期・チャットのテスト。"""
from __future__ import annotations

from datetime import date
from typing import Any

from jarvis import chat, engine, sync
from jarvis.models import SourceResult, failure

from .conftest import make_task

TODAY = date(2026, 10, 5)


def todo_result(tasks: list[dict], plan_rows: list | None = None) -> SourceResult:
    return SourceResult(name="todo", label="Todo", status="ok", data={"tasks": tasks, "plan": {"rows": plan_rows or []}})


def cash_result(checks: list[dict]) -> SourceResult:
    return SourceResult(name="cash", label="資金", status="ok", data={"checks": checks, "actual": {}})


def rec(results: dict, settings: Any, **kw: Any) -> dict:
    return engine.recommend(results, settings, today=TODAY, **kw)


def test_excluded_and_waiting_are_not_candidates(settings: Any) -> None:
    tasks = [make_task(id="T-1", status="完了"), make_task(id="T-2", status="中止"),
             make_task(id="T-3", priority="不要｜統合"), make_task(id="T-4", ownership="D｜重複削除"),
             make_task(id="T-5", status="撤退"), make_task(id="T-6", status="外部待ち", waitingFor="税務署"),
             make_task(id="T-7", priority="保留｜判断待ち"), make_task(id="T-8")]
    r = rec({"todo": todo_result(tasks)}, settings)
    assert [c["id"] for c in r["top"]] == ["T-8"]
    assert {w["id"] for w in r["waiting"]} == {"T-6", "T-7"}
    assert len(r["notToday"]["excluded"]) == 5


def test_stale_task_becomes_status_check_not_urgent(settings: Any) -> None:
    stale = make_task(id="T-OLD", priority="緊急｜今日中", due="2026/07/30", updated="2026/07/30", duration="3時間")
    fresh = make_task(id="T-NEW", priority="緊急｜今日中", due="2026/10/05", updated="2026/10/04")
    r = rec({"todo": todo_result([stale, fresh])}, settings)
    old = next(c for c in r["top"] if c["id"] == "T-OLD")
    assert old["timeType"] == "確認" and old["minutes"] == 10 and old["action"].startswith("現状確認")
    assert any("半減" in e["label"] for e in old["evidence"])
    assert r["top"][0]["id"] == "T-NEW"


def test_no_padding_and_budget_split(settings: Any) -> None:
    tasks = [make_task(id="T-A", duration="50分"), make_task(id="T-B", duration="50分", priority="")]
    r = rec({"todo": todo_result(tasks)}, settings, budget=60)
    assert [c["id"] for c in r["top"]] == ["T-A"]
    assert r["deferred"][0]["deferReason"].startswith("時間不足")
    assert "無理に埋めていません" in r["notice"]
    assert r["used"] == {"確認": 0, "実作業": 50}


def test_cash_check_merged_into_one_task_not_duplicated(settings: Any) -> None:
    tasks = [make_task(id="T-022", title="直近のキャッシュ状況を確認する", category="資金繰り"),
             make_task(id="T-041", title="資金管理・経理オペレーションを再設計する", category="資金繰り")]
    checks = [{"id": "forecast_negative", "title": "保存予測にマイナス", "detail": "d", "severity": "high",
               "evidence": [], "action": "a", "taskKeywords": ["資金繰り", "キャッシュ"]},
              {"id": "tax_payment", "title": "税金", "detail": "d", "severity": "high", "evidence": [],
               "action": "a", "taskKeywords": ["納付"]}]
    r = rec({"todo": todo_result(tasks), "cash": cash_result(checks)}, settings)
    by = {c["id"]: c for c in r["top"]}
    assert any("マイナス" in e["label"] for e in by["T-022"]["evidence"])
    assert not any("マイナス" in e["label"] for e in by["T-041"]["evidence"])
    assert "CASH:forecast_negative" not in by  # 統合したので別候補を作らない
    assert "CASH:tax_payment" in by  # 対応Todoが無い確認事項は独立候補（データ由来）
    assert by["CASH:tax_payment"]["confirmed"] is False


def test_slack_does_not_override_confirmed_task(settings: Any) -> None:
    t = make_task(id="T-9", status="未着手", due="2026/10/20")
    items = [{"key": "C:1", "status": "未対応", "todoRefs": ["T-9"], "link": "https://x.slack.com/a", "isBot": False,
              "text": "T-9 は完了しました？今日中に", "types": ["request"], "postedAt": "2026-10-05T09:00:00+09:00"}]
    sl = SourceResult(name="slack", label="Slack", status="ok", data={"items": items})
    r = rec({"todo": todo_result([t]), "slack": sl}, settings)
    c = next(c for c in r["top"] if c["id"] == "T-9")
    assert c["status"] == "未着手" and c["due"] == "2026/10/20"  # Slack発言で上書きしない
    assert any("Slack" in e["source"] for e in c["evidence"])
    assert any(x["id"] == "SLACK:open" and x["confirmed"] is False for x in r["top"])


def test_plan_month_reads_normalized_labels() -> None:
    rows = [["経営ゴール", "", "経営期限 2028/01｜EXIT目標 3億円"], ["管理項目", "定義", "2026/10"],
            ["営業利益｜目標", "", "200.0"], ["営業利益｜実績", "", ""], ["FLYCKA｜月次方針", "", "KEEP｜採算棚卸"]]
    p = engine.plan_month({"rows": rows}, TODAY)
    assert p["profitTarget"] == "200.0" and p["profitActual"] is None  # 空欄は未入力のまま
    assert p["policies"] == [{"axis": "FLYCKA", "value": "KEEP|採算棚卸"}]


def test_missing_sources_go_to_holds_not_zero(settings: Any) -> None:
    results = {"todo": failure("todo", "Todo", "権限なし", status="unconfigured"),
               "cash": failure("cash", "資金", "timeout")}
    r = rec(results, settings)
    assert r["top"] == []
    assert {h["status"] for h in r["holds"]} == {"unconfigured", "error"}
    assert "Todo管理表を読めていない" in r["notice"]


def test_sync_isolates_failures_and_keeps_stale(settings: Any, monkeypatch: Any) -> None:
    good = SourceResult(name="cash", label="資金", status="ok", last_success_at="2026-10-04T09:00:00+09:00", data={"x": 1})
    sync.save_cache(settings, {"cash": good})
    def boom(_s: Any) -> SourceResult:
        raise RuntimeError("network down")
    monkeypatch.setattr(sync, "FETCHERS", {
        "cash": ("資金", boom),
        "todo": ("Todo", lambda s: SourceResult(name="todo", label="Todo", status="ok", data={"tasks": []})),
    })
    out = sync.run_sync(settings)
    assert out["todo"].status == "ok"  # 他ソースは止まらない
    assert out["cash"].status == "stale" and out["cash"].data == {"x": 1}
    assert any("前回成功" in e for e in out["cash"].errors)
    assert sync.load_cache(settings)["cash"].status == "stale"


def test_chat_intents_use_state(settings: Any) -> None:
    tasks = [make_task(id="T-022", title="直近のキャッシュ状況を確認する"), make_task(id="T-6", status="外部待ち")]
    results = {"todo": todo_result(tasks)}
    def state_fn(budget: int | None) -> dict:
        return {"rec": rec(results, settings, budget=budget if budget is not None else 180),
                "sources": {}, "cash": None, "flycka": None, "slack": None}
    a = chat.answer("今日は60分しかない", state_fn)
    assert a["budget"] == 60 and "60分で再計算" in a["text"]
    assert "T-022" in chat.answer("T-022の詳細", state_fn)["text"]
    assert "相手待ち" in chat.answer("私がやらなくていい仕事は？", state_fn)["text"]
    assert "点数" in chat.answer("なぜこの順番？", state_fn)["text"]
    assert "取得できていません" in chat.answer("資金繰りはどこまで信用できる？", state_fn)["text"]
    assert "未接続" in chat.answer("天気は？", state_fn)["text"]
