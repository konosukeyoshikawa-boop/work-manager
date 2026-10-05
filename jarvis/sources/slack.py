"""Slack の取得（読み取り専用）— 全チャンネルのカバレッジと差分取得。

方針:
* 対象は認可された接続で見える公開・非公開チャンネル（アーカイブ除く）。DM・グループDMは対象外
* 「全チャンネル連携」を名乗る前に、チャンネルごとの取得結果（取得可/未参加/権限不足）を記録する
* 初回は直近 N 日（既定14日）、以降はチャンネルごとの最新 ts 以降だけを読む（差分取得）
* 返信は、候補になったスレッドを一定期間（既定30日）監視し、``latest_reply`` が変わったときだけ再取得
* 発言内容は「データ」として扱い、指示として実行しない（送信・リアクション等の書き込みAPIは使わない）
* 抽出はルールベース（自由会話AI未接続）。推定タスクは Todoマスターの確定タスクと区別する
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..config import JST, Settings
from ..models import SourceResult, failure, iso, now_jst, redact
from ..parsing import norm

logger = logging.getLogger(__name__)

NAME = "slack"
LABEL = "Slack（公開・非公開チャンネル）"
#: 全チャンネル取得に必要なスコープ（User Token 推奨）
REQUIRED_SCOPES = ("channels:read", "groups:read", "channels:history", "groups:history")
OPTIONAL_SCOPES = ("users:read",)
STATE_FILE = "slack_state.json"
MAX_WATCH_THREADS = 300


# ── 抽出ルール（純関数） ──────────────────────────────

_PATTERNS: dict[str, re.Pattern[str]] = {
    "request": re.compile(r"お願い(します|いたします|致します|できますか|できれば)|(して|確認|対応|共有|送付|返信)(を)?(ください|下さい|頂け|いただけ)|ご確認|ご対応|依頼"),
    "deadline": re.compile(r"(\d{1,2})/(\d{1,2})|(\d{1,2})月(\d{1,2})日|今日中|本日中|明日まで|明日中|今週中|来週|までに|締切|締め切り|期限"),
    "decision": re.compile(r"決定|決めました|確定し|承認|方針(は|を)|で進めます|合意"),
    "progress": re.compile(r"進捗|共有します|ご報告|報告します|対応中|着手"),
    "done": re.compile(r"完了(しました|です)|対応しました|対応済|済みです|送付しました|提出しました|納品しました|解決しました|done", re.I),
    "blocker": re.compile(r"困って|止まって|進まない|ブロック|できません|できない状態|詰まって|保留(中)?です|待ち(です|の状態)"),
    "failure": re.compile(r"failure|failed|失敗|エラー|:x:|:rotating_light:|❌|🚨|異常終了"),
}


def classify(text: str) -> list[str]:
    """発言の種類を判定する（複数可）。"""
    t = norm(text)
    return [k for k, p in _PATTERNS.items() if p.search(t)]


def _ngrams(text: str, n: int = 3) -> set[str]:
    t = re.sub(r"<[^>]+>|\s+|[、。！？!?.,:：]", "", norm(text))
    return {t[i:i + n] for i in range(max(len(t) - n + 1, 1))} if t else set()


def similarity(a: str, b: str) -> float:
    """文字3-gramの Jaccard 係数。"""
    x, y = _ngrams(a), _ngrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def thread_status(root: dict[str, Any], replies: list[dict[str, Any]], owner_id: str | None) -> str:
    """スレッドの状態を推定する。

    Returns:
        対応済み候補 / 相手待ち / 未対応 / 情報
    """
    msgs = [root] + [r for r in replies if r.get("ts") != root.get("ts")]
    if any("done" in classify(m.get("text", "")) for m in msgs[1:]):
        return "対応済み候補"
    root_types = classify(root.get("text", ""))
    last = msgs[-1]
    if owner_id:
        mentioned = f"<@{owner_id}>" in root.get("text", "")
        owner_replied = any(m.get("user") == owner_id for m in msgs[1:])
        if root.get("user") == owner_id and "request" in root_types:
            return "相手待ち" if last.get("user") == owner_id else "未対応"
        if (mentioned or "request" in root_types) and not owner_replied:
            return "未対応"
        if last.get("user") == owner_id:
            return "相手待ち"
    if "request" in root_types and len(msgs) == 1:
        return "未対応"
    return "情報" if not ({"request", "blocker", "failure"} & set(root_types)) else "未対応"


def permalink(domain: str, channel: str, ts: str, thread_ts: str | None = None) -> str:
    """元メッセージへのリンク。"""
    url = f"https://{domain}.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={channel}"
    return url


def build_item(ch: dict[str, Any], root: dict[str, Any], replies: list[dict[str, Any]],
               owner_id: str | None, domain: str) -> dict[str, Any] | None:
    """スレッド（または単独発言）から候補を作る。対象外なら None。"""
    texts = [root.get("text", "")] + [r.get("text", "") for r in replies]
    types = sorted({t for x in texts for t in classify(x)})
    interesting = {"request", "deadline", "decision", "blocker", "failure", "done", "progress"}
    if not (set(types) & interesting):
        return None
    if set(types) <= {"progress"} and not root.get("reply_count"):
        return None
    ts = root["ts"]
    when = datetime.fromtimestamp(float(ts), JST)
    last_ts = max([float(ts)] + [float(r["ts"]) for r in replies if r.get("ts")])
    return {
        "key": f"{ch['id']}:{ts}",
        "channelId": ch["id"], "channel": ch.get("name", ch["id"]),
        "ts": ts, "postedAt": iso(when),
        "lastActivityAt": iso(datetime.fromtimestamp(last_ts, JST)),
        "author": root.get("user") or root.get("bot_id") or "",
        "isBot": bool(root.get("bot_id")),
        "text": norm(re.sub(r"\s+", " ", root.get("text", "")))[:300],
        "types": types,
        "status": thread_status(root, replies, owner_id),
        "replyCount": int(root.get("reply_count") or 0),
        "todoRefs": sorted(set(re.findall(r"T-\d{3}", " ".join(texts)))),
        "link": permalink(domain, ch["id"], ts),
        "links": [permalink(domain, ch["id"], ts)],
        "confirmed": False,  # Slack 由来は常に「推定」
    }


def dedupe(items: list[dict[str, Any]], *, threshold: float = 0.6) -> list[dict[str, Any]]:
    """同じ話題・依頼を1件にまとめる（スレッド単位＋本文類似度）。"""
    out: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda x: x["postedAt"]):
        dup = next((o for o in out if similarity(o["text"], it["text"]) >= threshold
                    and abs(datetime.fromisoformat(o["postedAt"]) - datetime.fromisoformat(it["postedAt"])) <= timedelta(days=14)), None)
        if dup:
            dup["links"] = sorted(set(dup["links"] + it["links"]))
            dup["duplicates"] = dup.get("duplicates", 0) + 1
            if it["lastActivityAt"] > dup["lastActivityAt"]:
                dup["lastActivityAt"], dup["status"] = it["lastActivityAt"], it["status"]
            dup["todoRefs"] = sorted(set(dup["todoRefs"]) | set(it["todoRefs"]))
            dup["types"] = sorted(set(dup["types"]) | set(it["types"]))
        else:
            out.append(dict(it))
    return out


# ── API クライアント ─────────────────────────────────

class SlackApi:
    """読み取り専用の最小クライアント（書き込み系メソッドを持たない）。"""

    READ_METHODS = frozenset({"auth.test", "conversations.list", "conversations.history",
                              "conversations.replies"})

    def __init__(self, token: str, *, http_get: Callable[..., Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        import requests

        self._token = token
        self._get = http_get or requests.get
        self._sleep = sleep
        self.scopes: set[str] = set()

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        """GET でメソッドを呼ぶ（429 は Retry-After 待機して最大3回再試行）。"""
        if method not in self.READ_METHODS:
            raise PermissionError(f"読み取り専用クライアントで許可されていないメソッド: {method}")
        for attempt in range(4):
            resp = self._get(f"https://slack.com/api/{method}", params=params,
                             headers={"Authorization": f"Bearer {self._token}"}, timeout=30)
            if resp.status_code == 429 and attempt < 3:
                self._sleep(float(resp.headers.get("Retry-After", "5")))
                continue
            if resp.status_code >= 500 and attempt < 3:
                self._sleep(2 ** attempt)
                continue
            scopes = resp.headers.get("x-oauth-scopes")
            if scopes:
                self.scopes = {s.strip() for s in scopes.split(",") if s.strip()}
            return resp.json()
        return {"ok": False, "error": "retry_exhausted"}


# ── 状態保存 ─────────────────────────────────────────

def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"channels": {}, "threads": {}, "items": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _load_token(settings: Settings) -> str | None:
    token = os.environ.get("JARVIS_SLACK_TOKEN")
    if token:
        return token
    if settings.slack_env_path.exists():
        from dotenv import dotenv_values

        return dotenv_values(settings.slack_env_path).get("JARVIS_SLACK_TOKEN") or None
    return None


def sync(api: SlackApi, state: dict[str, Any], *, settings: Settings, now: datetime) -> dict[str, Any]:
    """カバレッジ確認と差分取得を行い、state を更新して結果を返す。"""
    auth = api.call("auth.test")
    if not auth.get("ok"):
        raise PermissionError(f"Slack認証失敗: {auth.get('error')}")
    token_kind = "user" if auth.get("user_id") and not auth.get("bot_id") else "bot"
    owner_id = os.environ.get("JARVIS_SLACK_OWNER_ID") or (auth.get("user_id") if token_kind == "user" else None)
    domain = re.sub(r"^https://|\.slack\.com/?$", "", auth.get("url", "")) or settings.slack_workspace_domain
    missing = [s for s in REQUIRED_SCOPES if s not in api.scopes]

    channels: list[dict[str, Any]] = []
    cursor = None
    list_error = None
    while True:
        params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        res = api.call("conversations.list", **params)
        if not res.get("ok"):
            list_error = res.get("error")
            break
        channels += res.get("channels", [])
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break

    coverage: list[dict[str, Any]] = []
    oldest_default = (now - timedelta(days=settings.slack_initial_days)).timestamp()
    watch_cutoff = (now - timedelta(days=settings.slack_thread_watch_days)).timestamp()
    items: dict[str, dict[str, Any]] = state.setdefault("items", {})
    threads: dict[str, dict[str, Any]] = state.setdefault("threads", {})
    ch_state: dict[str, dict[str, Any]] = state.setdefault("channels", {})
    fetched_msgs = 0
    for ch in channels:
        cid = ch["id"]
        st = ch_state.setdefault(cid, {})
        entry = {"id": cid, "name": ch.get("name"), "private": bool(ch.get("is_private")),
                 "member": bool(ch.get("is_member")), "status": "", "messages": 0,
                 "since": None}
        if token_kind == "bot" and not ch.get("is_member"):
            entry["status"] = "未参加（botを招待するまで取得不可）"
            coverage.append(entry)
            continue
        oldest = st.get("latest_ts") or f"{oldest_default:.6f}"
        entry["since"] = iso(datetime.fromtimestamp(float(oldest), JST))
        msgs: list[dict[str, Any]] = []
        cur = None
        err = None
        while True:
            p: dict[str, Any] = {"channel": cid, "oldest": oldest, "limit": 200}
            if cur:
                p["cursor"] = cur
            res = api.call("conversations.history", **p)
            if not res.get("ok"):
                err = res.get("error")
                break
            msgs += res.get("messages", [])
            cur = (res.get("response_metadata") or {}).get("next_cursor")
            if not cur or not res.get("has_more"):
                break
        if err:
            entry["status"] = {"not_in_channel": "未参加", "missing_scope": "権限不足",
                               "channel_not_found": "見えない（権限なし）"}.get(err, f"取得失敗（{err}）")
            coverage.append(entry)
            continue
        entry["status"] = "取得可"
        entry["messages"] = len(msgs)
        fetched_msgs += len(msgs)
        if msgs:
            st["latest_ts"] = max(m["ts"] for m in msgs)
        st["last_ok_at"] = iso(now)
        for m in msgs:
            if m.get("subtype") in ("channel_join", "channel_leave", "channel_topic", "channel_purpose"):
                continue
            item = build_item(ch, m, [], owner_id, domain)
            if item:
                items[item["key"]] = item
                if m.get("reply_count"):
                    threads[item["key"]] = {"channel": cid, "ts": m["ts"], "latest_reply": None}
            elif m.get("reply_count"):
                threads.setdefault(f"{cid}:{m['ts']}", {"channel": cid, "ts": m["ts"], "latest_reply": None,
                                                        "probe": True})
            if m.get("latest_reply") and f"{cid}:{m['ts']}" in threads:
                threads[f"{cid}:{m['ts']}"]["latest_hint"] = m.get("latest_reply")
        coverage.append(entry)

    # スレッド返信（変化したものだけ）
    by_id = {c["id"]: c for c in channels}
    reply_calls = 0
    for key, th in sorted(threads.items(), key=lambda kv: kv[1]["ts"], reverse=True)[:MAX_WATCH_THREADS]:
        if float(th["ts"]) < watch_cutoff:
            continue
        # 親が履歴の取得範囲外になると新着返信のヒントが来ないため、
        # 未解決の候補スレッドは60分ごとに再確認する
        unresolved = items.get(key, {}).get("status") in ("未対応", "相手待ち")
        checked = th.get("checked_at")
        recently = bool(checked and now - datetime.fromisoformat(checked) < timedelta(minutes=60))
        if th.get("latest_reply") and th.get("latest_hint") == th.get("latest_reply") \
                and (not unresolved or recently):
            continue
        res = api.call("conversations.replies", channel=th["channel"], ts=th["ts"], limit=200)
        reply_calls += 1
        th["checked_at"] = iso(now)
        if not res.get("ok"):
            continue
        msgs = res.get("messages", [])
        if not msgs:
            continue
        root, replies = msgs[0], msgs[1:]
        th["latest_reply"] = root.get("latest_reply") or (replies[-1]["ts"] if replies else None)
        th["latest_hint"] = th["latest_reply"]
        item = build_item(by_id.get(th["channel"], {"id": th["channel"]}), root, replies, owner_id, domain)
        if item:
            items[key] = item
            th.pop("probe", None)
    # 監視期限切れの整理
    for key in [k for k, t in threads.items() if float(t["ts"]) < watch_cutoff]:
        threads.pop(key, None)
    for key in [k for k, it in items.items() if float(it["ts"]) < watch_cutoff and it["status"] != "未対応"]:
        items.pop(key, None)

    ok = [c for c in coverage if c["status"] == "取得可"]
    return {
        "tokenKind": token_kind, "ownerKnown": bool(owner_id), "domain": domain,
        "missingScopes": missing, "listError": list_error,
        "channelsVisible": len(channels), "channelsReadable": len(ok),
        "coverage": coverage, "fetchedMessages": fetched_msgs, "replyCalls": reply_calls,
        "items": dedupe(list(items.values())),
    }


def fetch(settings: Settings, *, api: SlackApi | None = None, now: datetime | None = None) -> SourceResult:
    """Slack を差分取得する。トークン未設定なら必要な設定を返す。"""
    now = now or now_jst()
    fetched = iso(now)
    guide = ("Slack App（User Token Scopes: " + ", ".join(REQUIRED_SCOPES) + ", 任意で users:read）を作成・承認し、"
             "User OAuth Token を work-manager/jarvis/.env の JARVIS_SLACK_TOKEN に設定してください。"
             "既存の ec-bot / demo_app3 は channels:read・groups:read が無く、未参加チャンネルも読めません。")
    if api is None:
        token = _load_token(settings)
        if not token:
            res = failure(NAME, LABEL, "Slack読み取りトークンが未設定", status="unconfigured",
                          scope="公開・非公開チャンネル（DM対象外）")
            res.notes.append(guide)
            return res
        api = SlackApi(token)
    state_path = settings.cache_dir / STATE_FILE
    state = load_state(state_path)
    try:
        out = sync(api, state, settings=settings, now=now)
    except Exception as e:
        logger.exception("Slack取得失敗")
        res = failure(NAME, LABEL, e, status="unconfigured" if isinstance(e, PermissionError) else "error")
        res.notes.append(guide)
        return res
    save_state(state_path, state)
    errors: list[str] = []
    notes = ["Slack由来の候補は推定。Todoマスターの確定タスクを上書きしない。発言内の指示は実行しない。",
             f"差分取得: 初回{settings.slack_initial_days}日、以降はチャンネルごとの最新ts以降。返信は候補スレッドを{settings.slack_thread_watch_days}日監視。"]
    if out["missingScopes"]:
        errors.append("不足スコープ: " + ", ".join(out["missingScopes"]))
        notes.append(guide)
    if out["listError"]:
        errors.append(f"チャンネル一覧を取得できません: {out['listError']}")
    unread = [c for c in out["coverage"] if c["status"] != "取得可"]
    status = "ok" if not errors and not unread else ("partial" if out["channelsReadable"] else "error")
    return SourceResult(
        name=NAME, label=LABEL, status=status, fetched_at=fetched,
        last_success_at=fetched if out["channelsReadable"] else None, data_as_of=fetched,
        scope=f"見えるチャンネル {out['channelsVisible']} / 取得可 {out['channelsReadable']} / 取得不可 {len(unread)}（DM対象外）",
        errors=[redact(e) for e in errors], notes=notes, data=out)
