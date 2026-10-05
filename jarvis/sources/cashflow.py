"""資金繰りの取得（backoffice の成果物を読むだけ。再計算しない）。

責務境界: 財務計算は backoffice が正本。JARVIS は資金繰り原本スプレッドシートの
バッチ生成タブ（03_日次CF / 03_日次サマリ / 01_入力_銀行入出金 / 06_設定_更新履歴）と、
バッチ管理外の保存予測（03_13週資金繰り）を読み、

* 実残高（銀行明細の最終反映日時点）
* 保存予測（作成日時・前提つき）
* 確認待ちの前提（税金・未算入入金・未同期期間）

を分けて返す。13週予測を最新値として扱わず、「安全」「不足確定」と断定しない。
旧名称タブ（「日次CF」「週次ダッシュボード」等）は 2026-08-10 以降更新がなく、
二重集計を避けるため参照しない。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from ..config import (TAB_13W, TAB_DAILY_CF, TAB_DAILY_SUMMARY, TAB_INPUT_BANK,
                      TAB_UPDATE_LOG, Settings)
from ..models import SourceResult, failure, iso, now_jst
from ..parsing import norm, parse_date, parse_datetime, parse_yen
from .sheets import SheetAccessError, open_readonly, sheet_url

logger = logging.getLogger(__name__)

NAME = "cash"
LABEL = "資金繰り（backoffice成果物）"

#: 現役タブ / 旧名称タブ（参照しない。更新停止の確認にだけ使う）
LEGACY_TABS = ("日次CF", "週次ダッシュボード", "月次CF", "管理会計サマリ",
               "【自動更新】銀行入出金", "【自動更新】upsider", "Shopify Payouts")


# ── 純関数パーサ ─────────────────────────────────────

def parse_bank_rows(values: list[list[str]]) -> list[dict[str, Any]]:
    """01_入力_銀行入出金 を明細リストにする（日付のない行・フッタは除外）。"""
    if not values:
        return []
    header = [norm(h) for h in values[0]]
    idx = {h: i for i, h in enumerate(header) if h}
    need = ("日付", "口座名", "内容", "金額", "入出金区分")
    if any(k not in idx for k in need):
        raise ValueError(f"銀行入出金の見出しが想定と異なります: {header[:8]}")
    out: list[dict[str, Any]] = []
    for r in values[1:]:
        d = parse_date(r[idx["日付"]] if len(r) > idx["日付"] else "")
        amt = parse_yen(r[idx["金額"]] if len(r) > idx["金額"] else "")
        if d is None or amt is None:
            continue
        side = norm(r[idx["入出金区分"]])
        out.append({"date": d.isoformat(), "account": norm(r[idx["口座名"]]),
                    "desc": norm(r[idx["内容"]]), "amount": abs(amt),
                    "side": "in" if side == "入金" else "out"})
    return out


def parse_daily_cf(values: list[list[str]]) -> dict[str, Any]:
    """03_日次CF の見出しKPIと日次行を読む。"""
    meta: dict[str, Any] = {"updated": None, "kpi": {}}
    if len(values) > 1:
        dt = parse_datetime(values[1][0] if values[1] else "")
        meta["updated"] = dt.isoformat() if dt else None
    header_idx = next((i for i, r in enumerate(values) if r and norm(r[0]) == "日付"), None)
    for r in values[:header_idx or 0]:
        for j in range(0, len(r) - 1, 2):
            label, val = norm(r[j]), parse_yen(r[j + 1])
            if label and val is not None:
                meta["kpi"][label] = val
    rows: list[dict[str, Any]] = []
    if header_idx is not None:
        header = [norm(h) for h in values[header_idx]]
        bal_i = header.index("当日残高") if "当日残高" in header else None
        eff_i = header.index("実質残高") if "実質残高" in header else None
        for r in values[header_idx + 1:]:
            d = parse_date(r[0] if r else "")
            if d is None:
                continue
            rows.append({
                "date": d.isoformat(),
                "balance": parse_yen(r[bal_i]) if bal_i is not None and bal_i < len(r) else None,
                "effective": parse_yen(r[eff_i]) if eff_i is not None and eff_i < len(r) else None,
            })
    meta["rows"] = rows
    return meta


def parse_summary(values: list[list[str]]) -> dict[str, Any]:
    """03_日次サマリ から KPI 表と口座内訳を読む。"""
    kpis: dict[str, str] = {}
    accounts: list[dict[str, Any]] = []
    section = ""
    for r in values:
        a = norm(r[0]) if r else ""
        if a.startswith("==="):
            section = a.strip("= ")
            continue
        if not a or a in ("指標", "口座名", "レベル", "日付"):
            continue
        if section == "資金繰りKPI" and len(r) > 1:
            kpis[a] = norm(r[1])
        elif section == "口座残高内訳" and len(r) > 3:
            accounts.append({"name": a, "balance": parse_yen(r[1]),
                             "source": norm(r[2]), "asOf": norm(r[3])})
    updated = None
    if len(values) > 1 and values[1]:
        dt = parse_datetime(values[1][0])
        updated = dt.isoformat() if dt else None
    return {"updated": updated, "kpis": kpis, "accounts": accounts}


_WEEK_RE = re.compile(r"(\d{1,2})/(\d{1,2})\s*[〜~\-]\s*(\d{1,2})/(\d{1,2})")


def _week_dates(label: str, anchor_year: int) -> tuple[date, date] | None:
    """``9/28〜10/4`` を日付範囲に（年跨ぎ対応）。"""
    m = _WEEK_RE.search(label)
    if not m:
        return None
    sm, sd, em, ed = map(int, m.groups())
    sy = anchor_year
    start = date(sy, sm, sd)
    end = date(sy + (1 if em < sm else 0), em, ed)
    return start, end


def parse_13w(values: list[list[str]], *, anchor_year: int) -> dict[str, Any]:
    """03_13週資金繰り（保存予測）を週次データと前提に分解する。"""
    def row_by_label(label: str) -> list[str] | None:
        for r in values:
            if len(r) > 1 and norm(r[1]) == label:
                return r
        return None

    head2 = " ".join(norm(x) for x in (values[1] if len(values) > 1 else []))
    updated = parse_datetime(head2)
    m_bank = re.search(r"銀行明細の反映日[:：]\s*(\d{4}-\d{2}-\d{2})", head2)
    head3 = values[2] if len(values) > 2 else []
    def _pair(label: str) -> int | None:
        for j, x in enumerate(head3[:-1]):
            if norm(x).startswith(label):
                return parse_yen(head3[j + 1])
        return None
    current_label = next((norm(x) for x in head3 if norm(x).startswith("現在残高")), "")

    header_row = next((r for r in values if len(r) > 1 and norm(r[0]) == "区分"), None)
    kind_row = row_by_label("種別") or []
    if header_row is None:
        raise ValueError("13週資金繰りの見出し行（A列=区分）が見つかりません")
    cols = [j for j, x in enumerate(header_row) if j >= 2 and _WEEK_RE.search(norm(x))]
    def series(label: str) -> list[int | None]:
        r = row_by_label(label)
        return [parse_yen(r[j]) if r and j < len(r) else None for j in cols]

    open_bal, close_bal = series("期首残高"), series("週末残高")
    weeks: list[dict[str, Any]] = []
    for k, j in enumerate(cols):
        label = norm(header_row[j])
        rng = _week_dates(label, anchor_year)
        weeks.append({"label": label, "kind": norm(kind_row[j]) if j < len(kind_row) else "",
                      "start": rng[0].isoformat() if rng else None,
                      "end": rng[1].isoformat() if rng else None,
                      "open": open_bal[k], "close": close_bal[k]})
    forecast = [w for w in weeks if w["kind"] in ("予測", "一部実績") and w["close"] is not None]
    min_week = min(forecast, key=lambda w: w["close"]) if forecast else None
    first_neg = next((w for w in forecast if w["close"] < 0), None)

    lines = {lbl: series(lbl) for lbl in (
        "税金納付", "BPO入金（アライアンスクラウド）", "Meta広告（UPSIDER AdBoost振込）",
        "UPSIDERカード引落（通常カード）")}
    details: list[dict[str, Any]] = []
    det_idx = next((i for i, r in enumerate(values) if r and norm(r[0]) == "予測の明細"), None)
    if det_idx is not None:
        for r in values[det_idx + 1:]:
            d = parse_date(r[1] if len(r) > 1 else "")
            if d is None:
                continue
            details.append({"date": d.isoformat(), "kind": norm(r[3]) if len(r) > 3 else "",
                            "item": norm(r[4]) if len(r) > 4 else "",
                            "desc": norm(r[5]) if len(r) > 5 else "",
                            "amount": parse_yen(r[6]) if len(r) > 6 else None,
                            "certainty": norm(r[7]) if len(r) > 7 else "",
                            "basis": norm(r[8]) if len(r) > 8 else ""})
    return {
        "updated": updated.isoformat() if updated else None,
        "bankReflectedDate": m_bank[1] if m_bank else None,
        "currentLabel": current_label,
        "currentBalance": _pair("現在残高"),
        "warningLine": _pair("警戒ライン"),
        "weeks": weeks,
        "forecastStartOpen": forecast[0]["open"] if forecast else None,
        "min": min_week, "firstNegative": first_neg,
        "lines": {k: [{"week": weeks[i]["label"], "start": weeks[i]["start"], "amount": v}
                      for i, v in enumerate(vals) if v] for k, vals in lines.items()},
        "details": details,
    }


# ── 照合（確認事項の抽出） ────────────────────────────

def _bizdays_between(a: date, b: date) -> int:
    """a の翌日から b までの平日数。"""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def reconcile(*, bank: list[dict[str, Any]], daily: dict[str, Any], summary: dict[str, Any],
              f13: dict[str, Any] | None, today: date, cash_sheet_id: str,
              gids: dict[str, int]) -> dict[str, Any]:
    """実残高・保存予測・前提を照合し、確認事項を作る（判定を断定しない）。"""
    link = lambda tab: sheet_url(cash_sheet_id, gids.get(tab))  # noqa: E731
    bank_last = max((date.fromisoformat(b["date"]) for b in bank), default=None)
    actual_row = None
    if bank_last:
        actual_row = next((r for r in daily.get("rows", []) if r["date"] == bank_last.isoformat()), None)
    actual = {
        "bankLastDate": bank_last.isoformat() if bank_last else None,
        "balance": actual_row["balance"] if actual_row else None,
        "effective": actual_row["effective"] if actual_row else None,
        "upsiderPayable": parse_yen(summary.get("kpis", {}).get("UPSIDERカード未払い")),
        "accounts": [a for a in summary.get("accounts", []) if "GMO" in a["name"]],
        "scope": "freee登録の銀行口座（GMOあおぞら2口座）の明細ベース。現金・PayPal・UPSIDER残高・Shopify入金待ちは含まない",
        "dailyCfUpdated": daily.get("updated"),
        "link": link(TAB_DAILY_CF),
    }
    checks: list[dict[str, Any]] = []

    def add(cid: str, title: str, detail: str, severity: str, evidence: list[dict[str, Any]],
            action: str, task_keywords: list[str]) -> None:
        checks.append({"id": cid, "title": title, "detail": detail, "severity": severity,
                       "evidence": evidence, "action": action, "taskKeywords": task_keywords})

    if bank_last:
        gap = _bizdays_between(bank_last, today)
        if gap >= 2:
            add("bank_sync_gap", "銀行明細の未同期の可能性",
                f"銀行明細の最終反映日は {bank_last}（今日まで平日{gap}日）。それ以降の入出金は実残高に未反映の可能性。",
                "medium", [{"source": TAB_INPUT_BANK, "value": f"最終明細日 {bank_last}", "url": link(TAB_INPUT_BANK)}],
                "GMOあおぞらの直近明細とfreee同期状況を開いて確認", ["資金繰り", "キャッシュ"])

    if f13:
        wl = f13.get("warningLine")
        ref = f13.get("bankReflectedDate")
        ev13 = {"source": TAB_13W, "value": f"作成 {f13.get('updated')} / 銀行反映 {ref}", "url": link(TAB_13W)}
        if ref and bank_last and date.fromisoformat(ref) < bank_last:
            add("forecast_stale", "13週予測が実績より古い",
                f"保存予測は銀行反映 {ref} 時点。{bank_last} までの実績が予測に反映されていない。自動更新の仕組みはリポジトリに無い（手動作成）。",
                "medium", [ev13], "13週予測を最新明細で作り直すか、差分だけ確認する", ["資金繰り", "キャッシュ"])
        if f13.get("min") and f13["min"]["close"] < 0:
            neg = f13.get("firstNegative") or f13["min"]
            add("forecast_negative", "保存予測にマイナス残高（前提未確認）",
                f"保存予測では {neg['label']} に週末残高 ¥{neg['close']:,}、最低 ¥{f13['min']['close']:,}（{f13['min']['label']}）。"
                "下記の前提が未確認のため、資金不足を確定した数値ではない。",
                "high", [ev13], "前提（税金・未算入入金・広告費）を照合して予測を更新", ["資金繰り", "キャッシュ"])
        # 税金: 予測に計上された納付と銀行出金の照合
        for t in f13.get("lines", {}).get("税金納付", []):
            amt = t["amount"]
            hits = [b for b in bank if b["side"] == "out" and b["amount"] == amt]
            tax_words = [b for b in bank if b["side"] == "out" and re.search(r"税|ｾﾞｲ|ゼイ|国庫|ペイジー|ﾍﾟｲｼﾞｰ", b["desc"])
                         and b["date"] >= (t["start"] or "") and "ｾﾞｲﾘｼ" not in b["desc"]]
            if hits:
                detail = f"予測の税金 ¥{amt:,}（{t['week']}）と同額の出金が銀行明細にある（{hits[0]['date']}）。納付済みの可能性。"
                sev = "low"
            else:
                detail = (f"予測は税金 ¥{amt:,} を未納と仮定し {t['week']} に計上。銀行明細（{bank_last} まで）に同額の出金は無い"
                          + ("（税関連の出金: " + ", ".join(f"{b['date']} ¥{b['amount']:,}" for b in tax_words[:3]) + "）" if tax_words else "")
                          + "。未納・別口座での納付・明細未同期のどれかを確認する必要がある。税額・申告方法は推測で変更しない。")
                sev = "high"
            add("tax_payment", "税金の納付状況が未確認", detail, sev,
                [ev13, {"source": TAB_INPUT_BANK, "value": f"同額出金 {len(hits)}件", "url": link(TAB_INPUT_BANK)}],
                "納付書・納付記録（ペイジー等）を開き、納付日・金額・口座を確認", ["税", "納付", "換価"])
        # 予測に未算入の入金（アライアンスクラウド）
        ac_line = f13.get("lines", {}).get("BPO入金（アライアンスクラウド）", [])
        if not ac_line:
            since = f13.get("weeks", [{}])[0].get("start") or ""
            got = [b for b in bank if b["side"] == "in" and "アライアンス" in b["desc"] and b["date"] >= since]
            detail = "保存予測はアライアンスクラウドの入金を0円で置いている（不定期のため未算入）。"
            if got:
                detail += " 実績では " + "、".join(f"{b['date']} ¥{b['amount']:,}" for b in got) + " の入金がある。"
            add("unbooked_income", "予測に未算入の入金がある", detail, "medium",
                [ev13, {"source": TAB_INPUT_BANK, "value": f"該当入金 {len(got)}件", "url": link(TAB_INPUT_BANK)}],
                "次回の入金予定日・金額を先方の支払条件で確認し予測へ反映", ["資金繰り", "アライアンス", "入金"])
        if wl and actual["effective"] is not None and actual["effective"] < wl:
            add("below_warning", "実質手元資金が警戒ラインを下回る",
                f"実質手元資金 ¥{actual['effective']:,}（{bank_last} 時点）< 警戒ライン ¥{wl:,}。",
                "high", [{"source": TAB_DAILY_CF, "value": f"実質残高 ¥{actual['effective']:,}", "url": link(TAB_DAILY_CF)}],
                "支払予定と入金予定を突き合わせる", ["資金繰り", "キャッシュ"])

    runway = summary.get("kpis", {}).get("ランウェイ", "")
    if runway.startswith("999"):
        add("runway_basis", "「ランウェイ999か月」は資金の安全を意味しない",
            "日次サマリのランウェイは直近3か月の営業利益平均（P/L）から算出しており、税金・カード引落・仕入のタイミングを含まない。"
            "Slack通知の「黒字（枯渇リスクなし）」表示も同じ値に基づく（backoffice修正ブランチで表示を是正済み・未反映）。",
            "low", [{"source": TAB_DAILY_SUMMARY, "value": f"ランウェイ {runway}", "url": link(TAB_DAILY_SUMMARY)}],
            "資金判断は13週予測と実残高で行う", [])
    return {"actual": actual, "checks": checks}


def fetch(settings: Settings, *, today: date | None = None) -> SourceResult:
    """資金繰り原本を読み取り、実残高・保存予測・確認事項を返す。"""
    fetched = iso(now_jst())
    today = today or now_jst().date()
    try:
        sh = open_readonly(settings.cash_sheet_id, settings.google_sa_path)
    except SheetAccessError as e:
        return failure(NAME, LABEL, e, status="unconfigured")
    except Exception as e:
        logger.exception("資金繰りシート接続失敗")
        return failure(NAME, LABEL, e)

    errors: list[str] = []
    gids: dict[str, int] = {}
    def read(tab: str, rng: str | None = None) -> list[list[str]] | None:
        try:
            ws = sh.worksheet(tab)
            gids[tab] = ws.id
            return ws.get_values(rng) if rng else ws.get_all_values()
        except Exception as e:
            errors.append(f"{tab}: {type(e).__name__}: {e}")
            return None

    bank_v, daily_v = read(TAB_INPUT_BANK), read(TAB_DAILY_CF)
    summary_v, log_v = read(TAB_DAILY_SUMMARY, "A1:F120"), read(TAB_UPDATE_LOG)
    f13_v = read(TAB_13W, "A1:T300")
    if bank_v is None or daily_v is None:
        return failure(NAME, LABEL, "; ".join(errors) or "必須タブを読めません")
    try:
        bank = parse_bank_rows(bank_v)
        daily = parse_daily_cf(daily_v)
        summary = parse_summary(summary_v or [])
    except ValueError as e:
        return failure(NAME, LABEL, e)
    f13 = None
    if f13_v:
        try:
            f13 = parse_13w(f13_v, anchor_year=today.year if today.month > 2 else today.year - 1)
        except ValueError as e:
            errors.append(f"{TAB_13W}: {e}")
    update_log = [{"tab": norm(r[0]), "updated": norm(r[1])} for r in (log_v or [])[1:]
                  if len(r) > 1 and norm(r[0]) and norm(r[0]) != "シート名"]
    legacy = [u for u in update_log if u["tab"] in LEGACY_TABS]
    rec = reconcile(bank=bank, daily=daily, summary=summary, f13=f13, today=today,
                    cash_sheet_id=settings.cash_sheet_id, gids=gids)
    notes = [
        "実残高・保存予測・確認待ちの前提を分けて扱う。13週予測は手動作成の保存値で、自動更新されない。",
        "日次CFの先日付行（予測）はShopify入金予定のみで、カード引落・AdBoost振込・税金を含まない。",
        f"旧名称タブ（{len(legacy)}件、最終更新 {legacy[0]['updated'] if legacy else '不明'}）は二重集計防止のため参照しない。",
    ]
    if f13 is None:
        notes.append("13週予測を読めないため、保存予測は表示しない。")
    bank_last = rec["actual"]["bankLastDate"]
    stale = bool(bank_last and (today - date.fromisoformat(bank_last)).days > settings.cash_stale_days)
    return SourceResult(
        name=NAME, label=LABEL, status="partial" if errors else "ok",
        fetched_at=fetched, last_success_at=fetched, data_as_of=bank_last,
        scope=f"{TAB_INPUT_BANK}（{len(bank)}明細）/ {TAB_DAILY_CF} / {TAB_DAILY_SUMMARY} / {TAB_13W} / {TAB_UPDATE_LOG}",
        errors=errors, notes=notes,
        data={**rec, "forecast": f13, "summary": summary, "dailyKpi": daily.get("kpi", {}),
              "updateLog": update_log, "bankDataStale": stale,
              "sheetUrl": sheet_url(settings.cash_sheet_id)})
