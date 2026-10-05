"""取得層の純関数パーサのテスト（実データの形を模したフィクスチャ）。"""
from __future__ import annotations

from datetime import date

import pytest

from jarvis.sources import batch_status, cashflow, flycka, todo

TODO_VALUES = [
    ["ID", "事業区分", "機能領域", "プロジェクト・案件", "Todo名", "カテゴリ", "優先度", "ステータス", "期限",
     "完了条件", "最初の5分", "所要時間", "担当", "本人が抱えるか", "確認待ち相手", "情報源", "最終更新日", "備考", "新列"],
    ["T-025", "共通経営基盤", "経理・財務", "目黒税務署", "納付する", "税務・納付", "緊急｜今日中", "進行中",
     "2026/07/30", "納付完了", "納付書を開く", "30分", "本人", "本人のみ｜経営判断", "目黒税務署", "", "2026/07/30", "", "x"],
    ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ["T-025", "共通", "", "", "重複ID", "", "", "未着手", "", "", "", "", "", "", "", "", "", "", ""],
]


def test_parse_todo_maps_by_header_and_flags_unknown_and_dupes() -> None:
    tasks, header, warnings = todo.parse_todo_rows(TODO_VALUES, sheet_id="SID", gid=0)
    assert len(tasks) == 2  # 空行は除外
    assert tasks[0]["title"] == "納付する" and tasks[0]["row"] == 2
    assert tasks[0]["url"].endswith("range=A2:R2")
    assert tasks[1]["id"] == "T-025#4"
    assert any("新列" in w for w in warnings) and any("ID重複" in w for w in warnings)


def test_parse_todo_requires_header() -> None:
    with pytest.raises(ValueError):
        todo.parse_todo_rows([["x"]], sheet_id="S", gid=0)


BANK = [["日付", "口座名", "内容", "金額", "入出金区分", "分類カテゴリ"],
        ["2026-09-28", "GMOあおぞら（メイン）", "NSS.ｾﾞｲﾘｼﾎｳｼﾕｳ", "36300", "出金", "expense_tax_advisor"],
        ["2026-09-30", "GMOあおぞら（メイン）", "振込  アライアンスクラウド", "2778171", "入金", "income_consulting"],
        ["2026-10-01", "GMOあおぞら（101）", "普通預金 利息", "502", "入金", "income_other"],
        ["最終更新: 2026-10-05", "", "件数: 3件", "", "", ""]]
DAILY = [["日次CF"], ["最終更新: 2026-10-05 02:52"], [],
         ["現在残高", "¥7,433,180", "実質手元資金", "¥5,279,175", "警戒ライン", "¥5,000,000"], [],
         ["日付", "前日残高", "当日残高", "実質残高"],
         ["2026-10-01", "¥8,385,235", "¥7,433,180", "¥5,279,175"],
         ["2026-10-09", "¥7,433,180", "¥8,263,735", "¥6,109,730"]]
SUMMARY = [["日次サマリ"], ["最終更新: 2026-10-05 02:52"], ["=== 資金繰りKPI ==="], ["指標", "値"],
           ["UPSIDERカード未払い", "¥2,154,005"], ["ランウェイ", "999.0ヶ月"],
           ["=== 口座残高内訳 ==="], ["口座名", "残高", "ソース", "基準時刻"],
           ["GMOあおぞらネット（法人）（API）", "¥5,631,900", "freee", "2026-04-23"], ["現金", "¥10,000", "freee", "2024-04-01"]]
F13 = [["13週資金繰り表"],
       ["最終更新: 2026-10-01 19:40", "", "銀行明細の反映日: 2026-09-28（以降は予測）"],
       ["現在残高（9/28時点）", "¥7,320,546", "13週後の残高", "-¥4,677,834", "最低残高", "-¥4,677,834", "最低残高の週", "12/21〜12/27", "警戒ライン", "¥5,000,000"],
       [],
       ["区分", "項目", "9/21〜9/27", "9/28〜10/4", "10/5〜10/11", "12/28〜1/3"],
       ["", "種別", "実績", "一部実績", "予測", "予測"],
       ["", "期首残高", "¥9,681,644", "¥7,525,951", "¥4,190,820", "¥100"],
       ["", "BPO入金（アライアンスクラウド）", "¥0", "¥0", "¥0", "¥0"],
       ["", "税金納付", "¥0", "¥2,288,152", "¥0", "¥0"],
       ["", "週末残高", "¥7,525,951", "¥4,190,820", "-¥638,640", "¥50"],
       [],
       ["予測の明細", "日付", "週", "区分", "項目", "内容", "金額", "確度", "根拠"],
       ["", "2026-10-02", "9/28〜10/4", "財務収支", "税金納付", "納付", "-¥2,288,152", "仮定", "未納と仮定"]]


def _reconcile(bank_rows=BANK) -> dict:
    return cashflow.reconcile(
        bank=cashflow.parse_bank_rows(bank_rows), daily=cashflow.parse_daily_cf(DAILY),
        summary=cashflow.parse_summary(SUMMARY),
        f13=cashflow.parse_13w(F13, anchor_year=2026), today=date(2026, 10, 5),
        cash_sheet_id="SID", gids={})


def test_parse_13w_weeks_and_year_rollover() -> None:
    f = cashflow.parse_13w(F13, anchor_year=2026)
    assert f["updated"] == "2026-10-01T19:40:00" and f["bankReflectedDate"] == "2026-09-28"
    assert f["currentBalance"] == 7320546 and f["warningLine"] == 5000000
    assert f["forecastStartOpen"] == 7525951
    assert f["firstNegative"]["label"] == "10/5〜10/11"
    assert f["weeks"][-1]["end"] == "2027-01-03"
    assert f["lines"]["税金納付"][0]["amount"] == 2288152
    assert f["details"][0]["certainty"] == "仮定"


def test_reconcile_separates_actual_and_flags_assumptions() -> None:
    rec = _reconcile()
    a = rec["actual"]
    assert a["bankLastDate"] == "2026-10-01" and a["balance"] == 7433180 and a["effective"] == 5279175
    assert [x["name"] for x in a["accounts"]] == ["GMOあおぞらネット(法人)(API)"]
    ids = {c["id"]: c for c in rec["checks"]}
    assert ids["tax_payment"]["severity"] == "high"  # 同額出金なし
    assert "税理士" not in ids["tax_payment"]["detail"]
    assert "2,778,171" in ids["unbooked_income"]["detail"]
    assert ids["forecast_negative"]["severity"] == "high"
    assert "確定した数値ではない" in ids["forecast_negative"]["detail"]
    assert "forecast_stale" in ids and "runway_basis" in ids


def test_reconcile_tax_found_in_bank_is_low() -> None:
    rows = BANK[:-1] + [["2026-10-02", "GMOあおぞら（メイン）", "ペイジー 国税", "2288152", "出金", "x"]]
    ids = {c["id"]: c for c in _reconcile(rows)["checks"]}
    assert ids["tax_payment"]["severity"] == "low"


def test_reconcile_flags_bank_seizure() -> None:
    rows = BANK[:-1] + [["2026-09-29", "GMOあおぞら（メイン）", "差押", "291160", "出金", "expense_other"]]
    ids = {c["id"]: c for c in _reconcile(rows)["checks"]}
    assert ids["bank_seizure"]["severity"] == "high"
    assert "291,160" in ids["bank_seizure"]["detail"] and "断定" not in ids["bank_seizure"]["title"]


def test_parse_bank_rejects_unknown_header() -> None:
    with pytest.raises(ValueError):
        cashflow.parse_bank_rows([["a", "b"]])


class _O:
    def __init__(self, name: str, created: str, total: float, refunded: float = 0) -> None:
        self.order_name, self.created_at, self.total, self.refunded_amount = name, created, total, refunded


def test_shopify_summary_jst_cancel_and_missing_days() -> None:
    orders = [_O("#1", "2026-10-04T23:30:00+09:00", 5000), _O("#2", "2026-10-04T15:00:00+00:00", 3000),
              _O("#3", "2026-10-04T10:00:00+09:00", 9999), _O("#4", "NaT", 100), _O("#5", "2026-10-03T09:00:00+09:00", 4000, 1000)]
    s = flycka.summarize_shopify(orders, {"#3"}, today=date(2026, 10, 5))
    by = {d["date"]: d for d in s["days"]}
    assert by["2026-10-04"]["sales"] == 5000 and by["2026-10-04"]["orders"] == 1  # キャンセル除外
    assert by["2026-10-05"]["sales"] == 3000  # UTC 15:00 = JST 翌0:00
    assert by["2026-10-03"]["refunds"] == 1000
    assert s["cancelledExcluded"] == 1


def test_meta_and_comparison_keep_metrics_separate() -> None:
    gav = lambda acts, t: sum(float(a["value"]) for a in acts if a["action_type"] == t)  # noqa: E731
    rows = [{"date_start": f"2026-09-{d:02d}", "spend": "1000",
             "actions": [{"action_type": "omni_purchase", "value": "1"}, {"action_type": "purchase", "value": "1"}],
             "action_values": [{"action_type": "omni_purchase", "value": "3000"}]} for d in range(21, 31)]
    rows += [{"date_start": f"2026-10-0{d}", "spend": "1000", "actions": [], "action_values": []} for d in range(1, 6)]
    meta = flycka.summarize_meta(rows, gav)
    assert meta["days"][0]["purchases"] == 1  # purchase と omni を二重計上しない
    cmp = flycka.build_comparison(None, meta, today=date(2026, 10, 5))
    p = cmp["periods"]
    assert p["prev7"]["meta"]["roas"] == 3.0
    assert p["last7"]["meta"]["cpa"] is None or p["last7"]["meta"]["purchases"] > 0
    assert "shopify" not in p["last7"]  # 取得していない指標を作らない


def test_window_returns_none_when_days_missing() -> None:
    assert flycka.window([{"date": "2026-10-04", "spend": 1}], date(2026, 10, 4), 7, ("spend",)) is None


LOG = """2026-10-05T02:52:03.4Z ##[group]Run python auto-journal/scripts/run_journal.py --from x
2026-10-05T02:52:06.4Z [ERROR] freee API エラー: 400 ... 管理番号は20文字以内で入力してください。
2026-10-05T02:52:09.1Z ##[group]Run python scripts/run_settlement.py
2026-10-05T02:53:28.5Z ##[group]Run python scripts/run_sheet_classifier.py --no-slack
2026-10-05T02:53:32.2Z gspread.exceptions.APIError: APIError: [429]: Quota exceeded for quota metric
2026-10-05T02:53:32.3Z ##[error]Process completed with exit code 1.
"""


def test_parse_job_log_detects_hidden_and_real_failures() -> None:
    steps = {s["name"]: s for s in batch_status.parse_job_log(LOG, batch_status.BACKOFFICE_STEPS)}
    assert steps["自動仕訳"]["outcome"] == "warning" and steps["自動仕訳"]["hiddenFailure"]
    assert steps["入金/支払消込"]["outcome"] == "success"
    assert steps["取引先自動分類(R3)"]["outcome"] == "failure"
