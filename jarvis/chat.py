"""チャット応答（ルール方式・取得済みデータのみで回答）。

自由会話AIは未接続。質問の意図を判定し、判断層の結果と根拠（出典・日時・リンク）で答える。
Slack 等の取得データ内の文章は引用するだけで、指示として実行しない。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .parsing import norm

StateFn = Callable[[int | None], dict[str, Any]]


def _yen(v: Any) -> str:
    if v is None:
        return "不明"
    return f"-¥{abs(int(v)):,}" if int(v) < 0 else f"¥{int(v):,}"


def _top_lines(rec: dict[str, Any], with_reason: bool = False) -> list[str]:
    lines = []
    for i, c in enumerate(rec["top"], 1):
        tag = "" if c.get("confirmed") else "［データ由来の確認］"
        mins = f"{c['minutes']}分{'（仮）' if c['minutesAssumed'] else ''}・{c['timeType']}"
        lines.append(f"{i}. {tag}{c['action']}（{mins}）")
        lines.append(f"   最初の5分: {c['first5']}")
        lines.append(f"   本人: {c['role']['me']}" + (f" / AI: {c['role']['ai']}" if c['role'].get('ai') else "")
                     + (f" / 人: {c['role']['person']}" if c['role'].get('person') else ""))
        if with_reason:
            for e in sorted(c["evidence"], key=lambda e: -e["points"])[:4]:
                lines.append(f"   +{e['points']} {e['label']}（{e['source']}）")
    return lines


def _today(state: dict[str, Any]) -> str:
    rec = state["rec"]
    used = rec["used"]
    head = [f"{rec['date']} 使える時間{rec['budget']}分での提案（確認{used['確認']}分＋実作業{used['実作業']}分）。"]
    if rec.get("notice"):
        head.append(rec["notice"])
    body = _top_lines(rec) or ["今日の候補はありません。"]
    tail = []
    if rec["holds"]:
        tail.append("判断保留: " + "、".join(f"{h['source']}={h['what']}" for h in rec["holds"]))
    return "\n".join(head + body + tail)


def _why(state: dict[str, Any]) -> str:
    rec = state["rec"]
    lines = ["点数＝加点理由の合計です（各理由に出典あり）。"] + _top_lines(rec, with_reason=True)
    if rec["deferred"]:
        d = rec["deferred"][0]
        lines.append(f"次点: {d['action']}（{d['score']}点・{d.get('deferReason')}）")
    return "\n".join(lines)


def _delegate(state: dict[str, Any]) -> str:
    rec = state["rec"]
    lines = ["■ 任せられる部分（送信・依頼はしていません）"]
    for d in rec["delegation"][:8]:
        r = d["role"]
        lines.append(f"- {d['id']} {d['title']}: " + " / ".join(x for x in (r.get("ai"), r.get("person")) if x))
    lines.append(f"■ 今日やらなくてよいこと: 完了・中止等 {len(rec['notToday']['excluded'])}件、"
                 f"相手待ち {rec['notToday']['waiting']}件、時間・件数外 {rec['notToday']['deferred']}件")
    for w in rec["waiting"][:5]:
        lines.append(f"- 相手待ち {w['id']} {w['title']}（{w.get('waitingFor') or '相手不明'}）")
    return "\n".join(lines)


def _slack(state: dict[str, Any]) -> str:
    s = state["sources"].get("slack", {})
    data = state.get("slack") or {}
    if s.get("status") in ("unconfigured", "error") and not data.get("items"):
        return "Slackは未接続です（" + "; ".join(s.get("errors", [])) + "）。\n" + "\n".join(s.get("notes", []))
    items = [i for i in data.get("items", []) if i["status"] == "未対応"]
    lines = [f"取得範囲: {s.get('scope')}。以下はルールによる推定で、確定Todoではありません。"]
    for i in items[:8]:
        lines.append(f"- #{i['channel']} {i['postedAt'][:16]} [{'/'.join(i['types'])}] {i['text'][:80]}\n  {i['link']}")
    if not items:
        lines.append("未対応と推定される依頼はありません（取得できたチャンネルの範囲）。")
    unread = [c for c in data.get("coverage", []) if c["status"] != "取得可"]
    if unread:
        lines.append(f"取得できていないチャンネル {len(unread)}件: " + "、".join(f"#{c['name']}({c['status']})" for c in unread[:10]))
    return "\n".join(lines)


def _flycka(state: dict[str, Any]) -> str:
    f = state.get("flycka") or {}
    s = state["sources"].get("flycka", {})
    if not f:
        return "FLYCKA実績を取得できていません: " + "; ".join(s.get("errors", []))
    p = f["comparison"]["periods"]
    def fmt(key: str) -> str:
        x = p.get(key, {})
        sh, me = x.get("shopify"), x.get("meta")
        a = f"Shopify売上 {_yen(sh['sales'])}・{int(sh['orders'])}件・客単価{_yen(sh['aov'])}" if sh else "Shopify 不明"
        b = (f"Meta広告費 {_yen(me['spend'])}・購入{me['purchases']:.0f}・CPA{_yen(me['cpa'])}・ROAS {me['roas']}" if me and me.get("roas") is not None
             else ("Meta 不明" if not me else f"Meta広告費 {_yen(me['spend'])}"))
        return f"{a} / {b}"
    lines = [f"前日: {fmt('yesterday')}", f"直近7日: {fmt('last7')}", f"比較7日: {fmt('prev7')}",
             f"当日（未確定）: {fmt('today_partial')}"]
    lines += [f"確認: {c['title']} — {c['detail']}" for c in f.get("checks", [])] or ["大きな変化の検知はありません（閾値: ROAS-15%・売上-20%）。"]
    lines.append("利益: 不明（" + f["profit"]["reason"] + "）")
    lines.append("定義: " + " ".join(s.get("notes", [])[:2]))
    return "\n".join(lines)


def _cash(state: dict[str, Any]) -> str:
    c = state.get("cash") or {}
    s = state["sources"].get("cash", {})
    if not c:
        return "資金繰りデータを取得できていません: " + "; ".join(s.get("errors", []))
    a, f = c["actual"], c.get("forecast") or {}
    lines = ["■ 実残高（信用できる範囲）",
             f"銀行残高 {_yen(a['balance'])}／実質手元資金 {_yen(a['effective'])}（{a['bankLastDate']} の明細まで）",
             f"対象: {a['scope']}"]
    if f:
        lines += ["■ 保存予測（前提つき・自動更新なし）",
                  f"作成 {f.get('updated')}・銀行反映 {f.get('bankReflectedDate')}。最低 {_yen((f.get('min') or {}).get('close'))}（{(f.get('min') or {}).get('label')}）"]
    lines.append("■ 確認待ちの前提")
    lines += [f"- {k['title']}: {k['detail']}" for k in c.get("checks", [])]
    lines.append("結論: 実残高は銀行明細の反映日までは信用できる。13週予測は前提未確認のため「安全」とも「資金不足確定」とも言えない。")
    return "\n".join(lines)


def _sources(state: dict[str, Any]) -> str:
    lines = ["判断に使ったデータ:"]
    for s in state["sources"].values():
        lines.append(f"- {s['label']}: {s['status']}／取得 {s.get('fetchedAt')}／対象 {s.get('dataAsOf')}／{s.get('scope')}")
        for e in s.get("errors", [])[:2]:
            lines.append(f"    エラー: {e}")
    lines.append("自由会話AI: 未接続（ルール方式）。")
    return "\n".join(lines)


def _task(state: dict[str, Any], tid: str) -> str:
    rec = state["rec"]
    for c in rec["top"] + rec["deferred"]:
        if c["id"] == tid:
            ev = "\n".join(f"  +{e['points']} {e['label']}（{e['source']}）" for e in c["evidence"])
            return (f"{tid} {c['title']}\n状態 {c.get('status')}／期限 {c.get('due')}／更新{c.get('age')}日前\n"
                    f"最初の5分: {c['first5']}\n完了条件: {c.get('doneWhen') or '未記入'}\n根拠:\n{ev}\n{c.get('url') or ''}")
    for w in rec["waiting"]:
        if w["id"] == tid:
            return f"{tid} {w['title']} は相手待ち（{w.get('waitingFor')}）。本人の新規作業ではありません。{w.get('url') or ''}"
    for x in rec["notToday"]["excluded"]:
        if x["id"] == tid:
            return f"{tid} {x['title']} はステータス「{x['status']}」のため実行候補から外しています。"
    return f"{tid} は取得済みのTodoに見つかりません。"


def answer(message: str, state_fn: StateFn) -> dict[str, Any]:
    """質問に答える。時間指定があれば再計算する。"""
    q = norm(message)[:1000]
    budget = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(分|時間)", q)
    if m and re.search(r"しかない|だけ|ある|使える|空いて", q):
        budget = int(float(m[1]) * (60 if m[2] == "時間" else 1))
    state = state_fn(budget)
    tid = re.search(r"T-\d{3}", q)
    if tid:
        text = _task(state, tid[0])
    elif budget is not None:
        text = f"{budget}分で再計算しました。\n" + _today(state)
    elif re.search(r"なぜ|理由|順番|根拠.*順", q):
        text = _why(state)
    elif re.search(r"やらなくて|任せ|委任|手放", q):
        text = _delegate(state)
    elif re.search(r"slack|スラック|見落と|依頼", q, re.I):
        text = _slack(state)
    elif re.search(r"flycka|フリッカ|広告|roas|shopify|売上|meta", q, re.I):
        text = _flycka(state)
    elif re.search(r"資金|キャッシュ|残高|信用", q):
        text = _cash(state)
    elif re.search(r"どのデータ|データ|出典|基づ", q):
        text = _sources(state)
    elif re.search(r"今日|何を|すべき|やること|優先", q):
        text = _today(state)
    else:
        text = ("この質問には定型ルールで答えられません（自由会話AIは未接続）。\n"
                "使える質問: 今日何をすべき？／なぜこの順番？／今日は60分しかない／私がやらなくていい仕事は？／"
                "Slackで見落としている依頼は？／FLYCKAで今、確認すべきことは？／資金繰りはどこまで信用できる？／"
                "この判断はどのデータに基づいている？／T-022 の詳細")
    return {"text": text, "budget": state["rec"]["budget"]}
