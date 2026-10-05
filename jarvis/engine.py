"""判断層（純関数）— 今日のTop・理由・委任・保留を組み立てる。

取得層の SourceResult だけを入力にし、元データを変更しない。各候補の点数は
``evidence``（加点理由・出典・リンク）として必ず残し、「なぜこの順番か」を追えるようにする。

主なルール:
* 完了・中止・撤退・不要は実行候補から外す。相手待ち・保留は別枠
* 最終更新から14日超のTodoは「現状確認（10分枠）」にし、古い「今日中/今週」を現在の緊急指示に転用しない
* 資金・バッチ・FLYCKA の確認事項は、関連する確定Todoがあればそこへ根拠として統合（重複タスクを作らない）
* Slack 由来は推定。確定Todoを上書きしない
* 候補が少ない・時間が足りないときは Top を無理に埋めない。確認時間と実作業時間を分ける
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .models import SourceResult
from .parsing import norm, parse_date, parse_minutes

EXCLUDED_STATUS = re.compile(r"^(完了|中止|取消|キャンセル|撤退|不要|終了)")
WAITING_STATUS = re.compile(r"^(外部待ち|確認待ち|相手待ち|返答待ち|保留)")


# ── 分類 ────────────────────────────────────────────

def classify(task: dict[str, Any]) -> str:
    """excluded / waiting / delegate / active のいずれか。"""
    s, o, p = norm(task.get("status")), norm(task.get("ownership")), norm(task.get("priority"))
    if EXCLUDED_STATUS.search(s) or re.match(r"^(不要|D(｜|\||$))", o) or p.startswith("不要"):
        return "excluded"
    if WAITING_STATUS.search(s) or p.startswith("保留"):
        return "waiting"
    if re.match(r"^C(｜|\||$)", o):
        return "delegate"
    return "active"


def task_age(task: dict[str, Any], today: date) -> int | None:
    """最終更新からの日数（不明は None）。"""
    upd = parse_date(task.get("updated"))
    return (today - upd).days if upd else None


# ── 経営計画 ─────────────────────────────────────────

def plan_month(plan: dict[str, Any] | None, today: date) -> dict[str, Any]:
    """16か月経営計画から今月の目標・実績・方針を取り出す（空欄は未入力のまま）。"""
    month = f"{today.year}/{today.month:02d}"
    rows: list[list[str]] = (plan or {}).get("rows") or []
    hdr_i = next((i for i, r in enumerate(rows) if r and norm(r[0]) == "管理項目"), None)
    if hdr_i is None:
        return {"month": month, "available": False}
    col = next((j for j, v in enumerate(rows[hdr_i]) if norm(v) == month), None)
    if col is None:
        return {"month": month, "available": False}
    def val(label: str) -> str | None:
        r = next((r for r in rows if r and norm(r[0]) == norm(label)), None)
        v = norm(r[col]) if r and col < len(r) else ""
        return v or None
    suffix = norm("｜月次方針")
    policies = [{"axis": norm(r[0]).replace(suffix, ""), "value": norm(r[col])}
                for r in rows if r and norm(r[0]).endswith(suffix) and col < len(r) and norm(r[col])]
    goal_row = next((r for r in rows if r and norm(r[0]) == "経営ゴール"), None)
    return {
        "month": month, "available": True,
        "profitTarget": val("営業利益｜目標"), "profitActual": val("営業利益｜実績"),
        "cashMinInput": val("資金繰り｜13週最低残高"), "cashAlert": val("資金繰り｜アラート"),
        "policies": policies, "goal": norm(goal_row[2]) if goal_row and len(goal_row) > 2 else None,
        "unit": "万円", "url": (plan or {}).get("url"),
    }


AXIS_RULES: tuple[tuple[str, str], ...] = (
    (r"FLYCKA", "FLYCKA"), (r"営業代行|BPO", "営業代行/BPO"), (r"人材|HR", "人材"),
    (r"PMO|コンサル", "PMO/コンサル"), (r"AI\s*Web|Web制作", "AI Web制作"),
    (r"SEO|AIO", "SEO/AIO"),
)


def plan_axis(task: dict[str, Any]) -> str | None:
    """Todoが属する事業軸（経営計画の行名）を推定する。"""
    text = " ".join(norm(task.get(k)) for k in ("business", "area", "project", "title"))
    return next((axis for pat, axis in AXIS_RULES if re.search(pat, text)), None)


# ── 候補 ────────────────────────────────────────────

@dataclass
class Ctx:
    """スコアリングの文脈。"""

    today: date
    settings: Settings
    plan: dict[str, Any]
    cash_checks: list[dict[str, Any]]
    flycka_checks: list[dict[str, Any]]
    slack_items: list[dict[str, Any]]
    #: 確認事項ID → 統合先TodoID（1確認事項は1Todoにだけ統合する）
    assign: dict[str, str]


#: 確認事項の統合先から外す語（仕組み作りのTodoは「今日の確認」と別物）
_NOT_A_CHECK = re.compile(r"再設計|構築|仕組み|基盤整備|オペレーション設計|自動化")


def assign_checks(tasks: list[dict[str, Any]], cash_checks: list[dict[str, Any]],
                  flycka_checks: list[dict[str, Any]]) -> dict[str, str]:
    """データ由来の確認事項を、最も直接的な確定Todo 1件へ割り当てる。"""
    out: dict[str, str] = {}
    def text(t: dict[str, Any]) -> str:
        return " ".join(norm(t.get(k)) for k in ("title", "category", "project", "notes"))
    for chk in cash_checks:
        kws = chk.get("taskKeywords") or []
        if chk["severity"] not in ("high", "medium") or not kws:
            continue
        hits = [t for t in tasks if any(k in text(t) for k in kws) and not _NOT_A_CHECK.search(norm(t.get("title")))]
        if hits:
            best = max(hits, key=lambda t: (sum(k in text(t) for k in kws), "確認" in norm(t.get("title")),
                                            norm(t.get("updated"))))
            out[chk["id"]] = best["id"]
    for chk in flycka_checks:
        hits = [t for t in tasks if "FLYCKA" in text(t) and re.search(r"広告|運用|ROAS|モニタリング", text(t))
                and not _NOT_A_CHECK.search(norm(t.get("title")))]
        if hits:
            out[chk["id"]] = max(hits, key=lambda t: norm(t.get("updated")))["id"]
    return out


def _ev(points: int, label: str, source: str, url: str | None = None) -> dict[str, Any]:
    return {"points": points, "label": label, "source": source, "url": url}


def _role(task: dict[str, Any]) -> dict[str, Any]:
    """本人の役割・AI/人への委任候補。"""
    own, who = norm(task.get("ownership")), norm(task.get("assignee"))
    text = " ".join(norm(task.get(k)) for k in ("title", "category", "done"))
    ai = None
    if re.search(r"資料|提案|分析|調査|レポート|整理|可視化|集計|ドラフト|メモ", text):
        ai = "AIに初稿・データ整理を任せ、本人は判断と最終確認だけ行う"
    person = None
    if re.match(r"^B(｜|\|)", own):
        person = "将来移管候補（実行手順を残して担当者へ）"
    others = [w for w in re.split(r"[+＋/／、,]", who) if w and w not in ("本人", "AI", "―", "-")]
    if others:
        person = f"担当: {'・'.join(others)}"
    if "AI" in who and not ai:
        ai = "AIが下準備（元表の担当に「AI」あり）"
    me = "本人のみ（経営判断）" if "本人のみ" in own else ("本人が判断" if not person else "本人は判断・確認のみ")
    return {"me": me, "ai": ai, "person": person}


def score_task(task: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """確定Todo 1件を評価する。"""
    ev: list[dict[str, Any]] = []
    src = "Todoマスター"
    url = task.get("url")
    age = task_age(task, ctx.today)
    stale = age is None or age > ctx.settings.stale_task_days
    pri = norm(task.get("priority"))
    base = 30 if re.search(r"緊急|P0", pri) else 20 if re.search(r"重要|P1", pri) else \
        10 if re.search(r"P2|今週", pri) else 5 if "P3" in pri else 0
    if base:
        if stale:
            ev.append(_ev(base // 2, f"元表の優先度「{pri}」（更新{age if age is not None else '不明'}日前のため半減。現在の緊急指示ではない）", src, url))
        else:
            ev.append(_ev(base, f"元表の優先度「{pri}」", src, url))
    due = parse_date(task.get("due"))
    if due:
        days = (due - ctx.today).days
        if days < 0:
            ev.append(_ev(5, f"旧期限 {due} を超過（実施済みかを先に確認）", src, url) if stale
                      else _ev(25, f"期限 {due} を{-days}日超過", src, url))
        elif days == 0:
            ev.append(_ev(25, "期限が今日", src, url))
        elif days <= 3:
            ev.append(_ev(15, f"期限まで{days}日", src, url))
        elif days <= 7:
            ev.append(_ev(5, f"期限まで{days}日", src, url))
    elif norm(task.get("due")):
        ev.append(_ev(0, f"期限「{task.get('due')}」は日付として解釈できない", src, url))

    merged: list[str] = []
    for chk in ctx.cash_checks:
        if ctx.assign.get(chk["id"]) != task.get("id"):
            continue
        pts = 40 if chk["severity"] == "high" else 15
        note = "（同一案件かは未確認）" if chk["id"] == "tax_payment" else ""
        ev.append(_ev(pts, f"資金繰り: {chk['title']}{note}", "資金繰り原本",
                      (chk.get("evidence") or [{}])[0].get("url")))
        merged.append(chk["id"])
    for chk in ctx.flycka_checks:
        if ctx.assign.get(chk["id"]) == task.get("id"):
            ev.append(_ev(15, f"FLYCKA実績: {chk['title']}", "Shopify/Meta"))
            merged.append(chk["id"])
    axis = plan_axis(task)
    if axis and ctx.plan.get("available"):
        pol = next((p["value"] for p in ctx.plan.get("policies", []) if p["axis"] == axis), None)
        if pol:
            ev.append(_ev(10, f"今月({ctx.plan['month']})の方針 {axis}: {pol}", "16か月経営計画", ctx.plan.get("url")))
    refs = [s for s in ctx.slack_items if task.get("id") in s.get("todoRefs", []) and s["status"] == "未対応"]
    if refs:
        ev.append(_ev(10, f"Slackで未対応の言及 {len(refs)}件（推定）", "Slack", refs[0]["link"]))
    if "本人のみ" in norm(task.get("ownership")):
        ev.append(_ev(10, "代表本人の判断が必要", src, url))
    if norm(task.get("status")) == "進行中":
        ev.append(_ev(5, "進行中の仕事を閉じる", src, url))
    mins = parse_minutes(task.get("duration"))
    if mins is not None and 0 < mins <= 30 and not stale:
        ev.append(_ev(3, "30分以内で終わる", src, url))

    if stale:
        minutes, assumed, time_type = ctx.settings.stale_check_minutes, True, "確認"
        action = f"現状確認：{task.get('title')}"
        first5 = f"直近の記録を開き、完了済み・継続・不要のどれかを決める。継続なら「{task.get('first5') or '次の1手を書く'}」"
    else:
        minutes = mins if mins is not None else ctx.settings.unknown_duration_minutes
        assumed, time_type = mins is None, "実作業"
        action = task.get("title")
        first5 = task.get("first5") or "完了条件を読み、次の1手を1行で書く"
    return {
        "kind": "todo", "id": task.get("id"), "title": task.get("title"), "action": action,
        "score": sum(e["points"] for e in ev), "evidence": ev, "first5": first5,
        "minutes": minutes, "minutesAssumed": assumed, "timeType": time_type,
        "stale": stale, "age": age, "due": task.get("due"), "status": task.get("status"),
        "priority": pri, "project": task.get("project"), "business": task.get("business"),
        "role": _role(task), "url": url, "source": src, "merged": merged,
        "doneWhen": task.get("done"), "confirmed": True,
    }


def confirm_candidates(results: dict[str, SourceResult], used_ids: set[str], ctx: Ctx) -> list[dict[str, Any]]:
    """データ由来の確認事項（Todoに無いもの）を候補化する。"""
    out: list[dict[str, Any]] = []
    for chk in ctx.cash_checks:
        if chk["id"] in used_ids or chk["severity"] == "low":
            continue
        pts = 50 if chk["severity"] == "high" else 25
        out.append({
            "kind": "confirm", "id": f"CASH:{chk['id']}", "title": chk["title"], "action": chk["title"],
            "score": pts, "evidence": [_ev(pts, chk["detail"], e.get("source", "資金繰り原本"), e.get("url"))
                                       for e in chk.get("evidence", [])[:1]] or [_ev(pts, chk["detail"], "資金繰り原本")],
            "first5": chk["action"], "minutes": 10, "minutesAssumed": True, "timeType": "確認",
            "stale": False, "role": {"me": "本人が確認（数値はJARVISが抽出済み）", "ai": "照合用の明細抽出はAI済み", "person": "経理担当がいれば照合を依頼"},
            "url": (chk.get("evidence") or [{}])[0].get("url"), "source": "資金繰り原本（データ由来）",
            "confirmed": False, "detail": chk["detail"],
        })
    batch = results.get("batch")
    bo = (batch.data or {}).get("backoffice") if batch and batch.usable else None
    if bo:
        bad = [s for s in bo.get("steps", []) if s["outcome"] != "success"]
        if bad:
            names = "、".join(f"{s['name']}（{'成功表示だがエラー' if s['hiddenFailure'] else '失敗'}）" for s in bad)
            pats = sorted({p for s in bad for p in s["patterns"]})
            out.append({
                "kind": "confirm", "id": "BATCH:backoffice", "title": "日次バッチの失敗を止める修正の反映判断",
                "action": "修正PRの内容を確認し、反映（マージ）するか判断",
                "score": 45, "evidence": [_ev(45, f"直近run（{bo['createdAt']}）: {names}。原因: {'; '.join(pats) or 'ログ参照'}", "GitHub Actions", bo["url"])],
                "first5": "PR #19 と追加修正ブランチの差分・テスト結果を開く",
                "minutes": 15, "minutesAssumed": True, "timeType": "確認",
                "stale": False, "role": {"me": "本人がマージ可否を判断", "ai": "原因調査・修正・テストはAI済み", "person": None},
                "url": bo["url"], "source": "GitHub Actions（データ由来）", "confirmed": False,
            })
    for chk in ctx.flycka_checks:
        if chk["id"] in used_ids:
            continue
        out.append({
            "kind": "confirm", "id": f"FLYCKA:{chk['id']}", "title": chk["title"], "action": chk["action"],
            "score": 25, "evidence": [_ev(25, chk["detail"], "Shopify/Meta")], "first5": chk["action"],
            "minutes": 10, "minutesAssumed": True, "timeType": "確認", "stale": False,
            "role": {"me": "本人は判断のみ", "ai": "広告セット別の内訳抽出はAIに任せる", "person": "広告運用担当へ確認依頼も可"},
            "url": None, "source": "FLYCKA実績（データ由来）", "confirmed": False,
        })
    open_slack = [s for s in ctx.slack_items if s["status"] == "未対応" and not s.get("isBot")]
    if open_slack:
        out.append({
            "kind": "confirm", "id": "SLACK:open", "title": f"Slackの未対応候補 {len(open_slack)}件を確認（推定）",
            "action": "Slack未対応候補を見て、Todo化・不要・委任を決める",
            "score": 20, "evidence": [_ev(20, "Slackの依頼・期限・阻害要因を含む未返信スレッド（ルール推定）", "Slack", open_slack[0]["link"])],
            "first5": "未対応候補の上から3件を開く", "minutes": 10, "minutesAssumed": True, "timeType": "確認",
            "stale": False, "role": {"me": "本人がTodo化を判断", "ai": "抽出・重複統合はAI済み", "person": None},
            "url": open_slack[0]["link"], "source": "Slack（推定）", "confirmed": False,
        })
    return out


def select(cands: list[dict[str, Any]], budget: int, count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """時間と件数の範囲で選ぶ。無理に埋めない。"""
    picked, rest = [], []
    used = {"確認": 0, "実作業": 0}
    for c in sorted(cands, key=lambda x: (-x["score"], str(x["id"]))):
        if c["score"] <= 0:
            rest.append({**c, "deferReason": "優先度の根拠が無い"})
            continue
        if len(picked) >= count:
            rest.append({**c, "deferReason": "表示件数の上限"})
        elif sum(used.values()) + c["minutes"] > budget:
            rest.append({**c, "deferReason": f"時間不足（{c['minutes']}分）"})
        else:
            picked.append(c)
            used[c["timeType"]] += c["minutes"]
    return picked, rest, used


# ── 保留・矛盾 ───────────────────────────────────────

def find_conflicts(results: dict[str, SourceResult], plan: dict[str, Any], today: date,
                   claude_md: Path | None) -> list[dict[str, Any]]:
    """出典の食い違いを黙って解消せず列挙する。"""
    out: list[dict[str, Any]] = []
    if claude_md and claude_md.exists() and plan.get("goal"):
        text = claude_md.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"\*\*(.{0,40}株式売却.{0,20})\*\*", text)
        if m and not re.search(r"2026年内", plan["goal"]) and "2026年内" in m[1]:
            mtime = _git_date(claude_md) or datetime.fromtimestamp(claude_md.stat().st_mtime).date().isoformat()
            out.append({"title": "経営目標の期限が出典で異なる",
                        "detail": f"work-manager/CLAUDE.md（更新 {mtime}）「{m[1]}」 / 16か月経営計画「{plan['goal'][:60]}」",
                        "action": "どちらを現行目標とするか決め、古い方を更新する（JARVISは判断しない）"})
    todo = results.get("todo")
    wb = (todo.data or {}).get("weekBoard") if todo and todo.usable else None
    if wb and wb.get("updated"):
        age = (today - date.fromisoformat(wb["updated"])).days
        if age > 7:
            out.append({"title": "「今日・今週」ボードが古い",
                        "detail": f"{wb['updated']}更新（{age}日前）。記載の「今日」「今週」は現在の指示として使っていない",
                        "action": "ボードを更新するか、JARVISのTopを正とするか決める"})
    cash = results.get("cash")
    f13 = (cash.data or {}).get("forecast") if cash and cash.usable else None
    if plan.get("available") and f13 and f13.get("min"):
        if not plan.get("cashMinInput"):
            out.append({"title": "経営計画の「13週最低残高」が未入力",
                        "detail": f"資金繰り原本の保存予測では最低 ¥{f13['min']['close']:,}（{f13['min']['label']}、前提未確認）",
                        "action": "前提を確認してから計画表へ転記（JARVISは書き込まない）"})
    tasks = (todo.data or {}).get("tasks", []) if todo and todo.usable else []
    bad_due = [t["id"] for t in tasks if norm(t.get("due")) and parse_date(t.get("due")) is None and classify(t) != "excluded"]
    if bad_due:
        out.append({"title": "期限が日付でないTodo", "detail": "、".join(bad_due[:8]),
                    "action": "具体的な日付にする（期日ルール）"})
    return out


def _git_date(path: Path) -> str | None:
    """ファイルの最終コミット日（チェックアウト時刻ではなく内容の更新日）。"""
    import subprocess

    try:
        out = subprocess.run(["git", "-C", str(path.parent), "log", "-1", "--format=%cs", "--", path.name],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def holds(results: dict[str, SourceResult]) -> list[dict[str, Any]]:
    """取得失敗・旧データで判断を保留している事項。"""
    out = []
    for r in results.values():
        if r.status == "ok":
            continue
        what = {"unconfigured": "未接続（認証・権限が必要）", "error": "取得失敗", "stale": "旧データ表示",
                "partial": "一部取得"}[r.status]
        out.append({"source": r.label, "status": r.status, "what": what,
                    "detail": "; ".join(r.errors) or "", "notes": r.notes, "lastSuccessAt": r.last_success_at})
    return out


# ── 入口 ────────────────────────────────────────────

def recommend(results: dict[str, SourceResult], settings: Settings, *, today: date,
              budget: int = 180, count: int = 5, claude_md: Path | None = None) -> dict[str, Any]:
    """今日の判断材料をまとめて返す。"""
    if not 0 <= budget <= 1440:
        raise ValueError("使える時間は0〜1440分で指定してください")
    count = max(1, min(int(count), 5))
    todo = results.get("todo")
    cash = results.get("cash")
    fly = results.get("flycka")
    slk = results.get("slack")
    tasks = (todo.data or {}).get("tasks", []) if todo and todo.usable else []
    plan = plan_month((todo.data or {}).get("plan") if todo and todo.usable else None, today)
    groups: dict[str, list[dict[str, Any]]] = {"active": [], "waiting": [], "delegate": [], "excluded": []}
    for t in tasks:
        groups[classify(t)].append(t)
    cash_checks = (cash.data or {}).get("checks", []) if cash and cash.usable else []
    fly_checks = (fly.data or {}).get("checks", []) if fly and fly.usable else []
    ctx = Ctx(today=today, settings=settings, plan=plan, cash_checks=cash_checks, flycka_checks=fly_checks,
              slack_items=(slk.data or {}).get("items", []) if slk and slk.usable else [],
              assign=assign_checks(groups["active"], cash_checks, fly_checks))
    scored = [score_task(t, ctx) for t in groups["active"]]
    used_ids = {i for s in scored for i in s["merged"]}
    cands = scored + confirm_candidates(results, used_ids, ctx)
    top, rest, used = select(cands, budget, count)
    stale_active = sum(1 for s in scored if s["stale"])
    delegation = [{"id": c["id"], "title": c["title"], "role": c["role"], "url": c.get("url")}
                  for c in sorted(scored, key=lambda x: -x["score"]) if c["role"].get("ai") or c["role"].get("person")][:10]
    delegation += [{"id": t["id"], "title": t["title"], "role": {"me": "確認のみ", "ai": None,
                    "person": f"委任済み（{norm(t.get('ownership'))}）"}, "url": t.get("url")} for t in groups["delegate"]]
    waiting = [{"id": t["id"], "title": t["title"], "waitingFor": t.get("waitingFor"), "due": t.get("due"),
                "updated": t.get("updated"), "status": t.get("status"), "url": t.get("url")} for t in groups["waiting"]]
    return {
        "date": today.isoformat(), "budget": budget, "count": count,
        "used": used, "top": top, "deferred": rest[:15],
        "notice": _notice(top, cands, budget, stale_active, len(scored), todo),
        "waiting": waiting, "delegation": delegation,
        "notToday": {
            "excluded": [{"id": t["id"], "title": t["title"], "status": t.get("status")} for t in groups["excluded"]],
            "waiting": len(waiting), "deferred": len(rest),
            "slackUnconfirmed": len([s for s in ctx.slack_items if s["status"] != "対応済み候補"]),
        },
        "plan": plan, "holds": holds(results), "conflicts": find_conflicts(results, plan, today, claude_md),
        "counts": {k: len(v) for k, v in groups.items()} | {"staleActive": stale_active, "total": len(tasks)},
    }


def _notice(top: list[dict[str, Any]], cands: list[dict[str, Any]], budget: int, stale: int,
            active: int, todo: SourceResult | None) -> str:
    """Topの読み方の注意（件数不足・旧データ等）。"""
    parts = []
    if todo is None or not todo.usable:
        parts.append("Todo管理表を読めていないため、Topはデータ由来の確認事項だけです。")
    elif todo.status == "stale":
        parts.append(f"Todoは旧データ（{todo.last_success_at} 時点）です。")
    if len(top) < 3:
        parts.append(f"条件を満たす候補が{len(top)}件のため、Topを無理に埋めていません。" if cands
                     else "今日の候補はありません。")
    if active and stale >= active * 0.8:
        parts.append(f"実行候補{active}件中{stale}件が更新14日超のため、まず「現状確認（各10分）」として扱っています。")
    if budget < 30:
        parts.append("使える時間が30分未満です。確認だけに絞っています。")
    return " ".join(parts)
