"""FLYCKA 実績（Shopify＋Meta広告）の取得。

責務境界: 取得処理は flycka-d2c の既存実装を import して再利用する。
* Shopify: ``src.data.sheets_client.SheetsClient.fetch_order_costs``（Shopify_OrderData・gviz）
  を読み、注文単位へは ``src.data.shopify_processor.aggregate_orders`` で集約
* Meta: ``src.api.meta_client.MetaAdClient`` の account レベル日次インサイト。
  購入数・購入金額は ``src.data.models._get_action_value`` で omni_purchase を採用
  （flycka-d2c と同じ定義・帰属 7d_click+1d_ev+1d_view）

ここでは表示用の日次集計だけを行い、flycka-d2c の SQLite・シート・GCS には書かない。
顧客の個人情報（メール・住所等）は集計直後に破棄し、キャッシュに残さない。

指標定義:
* Shopify売上 = 注文の Total（税込・送料込・割引後）を注文作成日（JST）で集計。
  キャンセル注文は除外、返金額は別掲（「返金調整後」= 売上 − 返金額）
* Meta広告の購入金額は Meta の帰属値（広告主のピクセル計測）。Shopify売上とは別指標で、
  足し合わせない。ROAS = Meta購入金額 ÷ 広告費
* 当日分は未確定値として扱う
"""
from __future__ import annotations

import contextlib
import io
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any

from ..config import JST, Settings
from ..models import SourceResult, failure, iso, now_jst, redact

logger = logging.getLogger(__name__)

NAME = "flycka"
LABEL = "FLYCKA実績（Shopify＋Meta）"
DAYS = 15  # 当日 + 直近7日 + 比較7日


def _import_flycka(settings: Settings) -> None:
    """flycka-d2c をインポート可能にする（読み取り専用利用）。"""
    root = str(settings.flycka_dir)
    if not settings.flycka_dir.exists():
        raise FileNotFoundError(f"flycka-d2c が見つかりません: {settings.flycka_dir}")
    if root not in sys.path:
        sys.path.insert(0, root)


def summarize_shopify(orders: list[Any], cancelled: set[str], *, today: date) -> dict[str, Any]:
    """注文リストを日次集計する（純関数・個人情報を含まない出力）。

    Args:
        orders: flycka-d2c の Order（name/created_at/total/refunded_amount/financial_status）。
        cancelled: キャンセル済み注文名の集合。
        today: 基準日（JST）。
    """
    start = today - timedelta(days=DAYS - 1)
    daily: dict[str, dict[str, float]] = {}
    latest: datetime | None = None
    excluded = 0
    for o in orders:
        ts = o.created_at
        try:
            dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
        except (TypeError, ValueError):
            continue  # NaT・空欄は集計しない（0円扱いにしない）
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=JST)
        local = dt.astimezone(JST)
        latest = local if latest is None or local > latest else latest
        d = local.date()
        if d < start or d > today:
            continue
        if o.order_name.strip() in cancelled:
            excluded += 1
            continue
        row = daily.setdefault(d.isoformat(), {"sales": 0.0, "orders": 0, "refunds": 0.0})
        row["sales"] += float(o.total or 0)
        row["orders"] += 1
        row["refunds"] += float(o.refunded_amount or 0)
    days = []
    for i in range(DAYS):
        d = (start + timedelta(days=i)).isoformat()
        r = daily.get(d)
        days.append({"date": d, "sales": round(r["sales"]) if r else 0,
                     "orders": int(r["orders"]) if r else 0,
                     "refunds": round(r["refunds"]) if r else 0})
    return {"days": days, "latestOrderAt": iso(latest), "cancelledExcluded": excluded}


def summarize_meta(rows: list[dict[str, Any]], get_action_value: Any) -> dict[str, Any]:
    """Meta account レベル日次行を集計する（純関数）。"""
    days = []
    for r in sorted(rows, key=lambda x: x.get("date_start", "")):
        spend = float(r.get("spend") or 0)
        purchases = float(get_action_value(r.get("actions") or [], "omni_purchase"))
        value = float(get_action_value(r.get("action_values") or [], "omni_purchase"))
        days.append({"date": r.get("date_start"), "spend": round(spend),
                     "purchases": purchases, "purchaseValue": round(value)})
    return {"days": days}


def window(days: list[dict[str, Any]], end: date, n: int, keys: tuple[str, ...]) -> dict[str, float] | None:
    """end を含む過去 n 日の合計（データの無い日があれば None）。"""
    want = {(end - timedelta(days=i)).isoformat() for i in range(n)}
    rows = [d for d in days if d["date"] in want]
    if len(rows) < n:
        return None
    return {k: sum(float(r.get(k) or 0) for r in rows) for k in keys}


def build_comparison(shop: dict[str, Any] | None, meta: dict[str, Any] | None, *, today: date) -> dict[str, Any]:
    """前日・直近7日・比較7日を作る。当日は未確定として別扱い。"""
    y = today - timedelta(days=1)
    out: dict[str, Any] = {"today": today.isoformat(), "yesterday": y.isoformat(), "periods": {}}
    periods = {
        "today_partial": (today, 1), "yesterday": (y, 1), "day_before": (y - timedelta(days=1), 1),
        "last7": (y, 7), "prev7": (y - timedelta(days=7), 7),
    }
    for key, (end, n) in periods.items():
        p: dict[str, Any] = {"end": end.isoformat(), "days": n}
        if shop:
            s = window(shop["days"], end, n, ("sales", "orders", "refunds"))
            if s:
                s["aov"] = round(s["sales"] / s["orders"]) if s["orders"] else None
                s["salesNetOfRefunds"] = s["sales"] - s["refunds"]
            p["shopify"] = s
        if meta:
            m = window(meta["days"], end, n, ("spend", "purchases", "purchaseValue"))
            if m:
                m["cpa"] = round(m["spend"] / m["purchases"]) if m["purchases"] else None
                m["roas"] = round(m["purchaseValue"] / m["spend"], 2) if m["spend"] else None
            p["meta"] = m
        out["periods"][key] = p
    return out


def derive_checks(cmp: dict[str, Any], shop_meta: dict[str, Any], *, today: date) -> list[dict[str, Any]]:
    """数値の変化とデータ信頼性から「確認すべきこと」を作る（運用変更はしない）。"""
    checks: list[dict[str, Any]] = []
    l7 = cmp["periods"].get("last7", {})
    p7 = cmp["periods"].get("prev7", {})
    lm, pm = l7.get("meta"), p7.get("meta")
    if lm and pm and lm.get("roas") and pm.get("roas"):
        chg = (lm["roas"] - pm["roas"]) / pm["roas"]
        if chg <= -0.15:
            checks.append({"id": "meta_roas_drop", "severity": "medium",
                           "title": f"Meta ROAS（帰属値）が直近7日で{chg:+.0%}",
                           "detail": f"直近7日 {lm['roas']:.2f} / 比較7日 {pm['roas']:.2f}（Metaの購入金額÷広告費。Shopify売上ではない）",
                           "action": "広告セット別の消化と購入をflycka-d2cダッシュボードで確認"})
    ls, ps = l7.get("shopify"), p7.get("shopify")
    if ls and ps and ps.get("sales"):
        chg = (ls["sales"] - ps["sales"]) / ps["sales"]
        if chg <= -0.2:
            checks.append({"id": "shopify_sales_drop", "severity": "medium",
                           "title": f"Shopify売上が直近7日で{chg:+.0%}",
                           "detail": f"直近7日 ¥{ls['sales']:,.0f} / 比較7日 ¥{ps['sales']:,.0f}（税込・送料込・キャンセル除外）",
                           "action": "商品・在庫・広告配信の変化を確認"})
    latest = shop_meta.get("latestOrderAt")
    if latest and (now_jst() - datetime.fromisoformat(latest)) > timedelta(hours=36):
        checks.append({"id": "shopify_stale", "severity": "medium",
                       "title": "Shopify注文データが36時間以上更新されていない",
                       "detail": f"最新注文 {latest}。Shopify_OrderData の更新処理を確認",
                       "action": "注文データの同期元（Shopify_OrderData）の更新状況を確認"})
    return checks


def fetch(settings: Settings, *, today: date | None = None) -> SourceResult:
    """Shopify と Meta を個別に取得し、片方の失敗で他方を止めない。"""
    fetched = iso(now_jst())
    today = today or now_jst().date()
    try:
        _import_flycka(settings)
    except Exception as e:
        return failure(NAME, LABEL, e, status="unconfigured")

    errors: list[str] = []
    notes = [
        "Shopify売上＝注文Total（税込・送料込・割引後）を注文作成日（JST）で集計。キャンセル除外、返金は別掲。",
        "Meta購入金額＝Meta帰属値（omni_purchase、7日クリック＋1日エンゲージビュー＋1日ビュー）。Shopify売上とは別指標で合算しない。",
        "当日分は未確定。比較は前日・直近7日（昨日まで）・その前の7日。",
        "利益不明：原価・送料・決済手数料の定義済み集計をJARVISは未接続のため、売上−広告費を利益として扱わない。",
    ]
    shop: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink):
            from src.data.sheets_client import SheetsClient  # type: ignore[import-not-found]
            from src.data.shopify_processor import aggregate_orders  # type: ignore[import-not-found]
            df = SheetsClient().fetch_order_costs()
            if df is None or df.empty:
                raise RuntimeError("Shopify注文データが空（取得失敗の可能性。0円とは扱わない）")
            cancelled = set()
            if "Cancelled at" in df.columns:
                cancelled = {str(n).strip() for n in df.loc[df["Cancelled at"].notna()
                             & (df["Cancelled at"].astype(str).str.strip() != ""), "Name"]}
            cutoff = (today - timedelta(days=DAYS + 1)).isoformat()
            recent = df[df["Created at"].astype(str) >= cutoff].copy()
            orders = aggregate_orders(recent)
            del df, recent
        shop = summarize_shopify(orders, cancelled, today=today)
        del orders
    except Exception as e:
        logger.exception("Shopify取得失敗")
        errors.append(redact(f"Shopify: {type(e).__name__}: {e}"))
    try:
        with contextlib.redirect_stdout(sink):
            from src.api.meta_client import MetaAdClient  # type: ignore[import-not-found]
            from src.data.models import _get_action_value  # type: ignore[import-not-found]
            client = MetaAdClient()
            acct = client.graph_get(client._ad_account_id, {"fields": "currency,timezone_name"}) or {}
            rows = client._get_insights(
                "account", (today - timedelta(days=DAYS - 1), today),
                fields=["spend", "actions", "action_values", "date_start", "date_stop"])
        meta = summarize_meta([dict(r) for r in rows], _get_action_value)
        meta["currency"] = acct.get("currency")
        meta["timezone"] = acct.get("timezone_name")
        if meta["currency"] not in (None, "JPY") or meta["timezone"] not in (None, "Asia/Tokyo"):
            errors.append(f"Meta: 通貨/タイムゾーンが想定外（{meta['currency']}/{meta['timezone']}）。比較は要注意")
    except Exception as e:
        logger.exception("Meta取得失敗")
        errors.append(redact(f"Meta: {type(e).__name__}: {e}"))

    if shop is None and meta is None:
        res = failure(NAME, LABEL, "; ".join(errors))
        res.notes = notes
        return res
    cmp = build_comparison(shop, meta, today=today)
    checks = derive_checks(cmp, shop or {}, today=today)
    return SourceResult(
        name=NAME, label=LABEL, status="partial" if errors else "ok",
        fetched_at=fetched, last_success_at=fetched,
        data_as_of=(shop or {}).get("latestOrderAt") or fetched,
        scope=f"直近{DAYS}日（{(today - timedelta(days=DAYS - 1)).isoformat()}〜{today.isoformat()}）"
              f" Shopify={'取得' if shop else '失敗'} / Meta={'取得' if meta else '失敗'}",
        errors=errors, notes=notes,
        data={"shopify": shop, "meta": meta, "comparison": cmp, "checks": checks,
              "profit": {"status": "unknown",
                         "reason": "原価・送料・決済手数料の定義済み集計が未接続（flycka-d2c src/cost の出力連携は次段階）"},
              "links": {"opsSheet": f"https://docs.google.com/spreadsheets/d/{settings.flycka_ops_sheet_id}/edit"}})
