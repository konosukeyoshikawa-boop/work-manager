/**
 * JARVIS 画面（表示層）。判断はサーバー（engine.py）が行い、ここでは表示とチャット送受信のみ。
 * 取得データ内の文字列はすべてエスケープして表示し、リンクは http(s) のみ許可する。
 */
'use strict';
(() => {
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const safeUrl = (u) => (/^https:\/\//.test(String(u || '')) ? String(u) : null);
  const link = (u, label) => (safeUrl(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : '');
  const yen = (v) => (v === null || v === undefined || Number.isNaN(+v) ? '不明' : (+v < 0 ? '-¥' : '¥') + Math.abs(Math.round(+v)).toLocaleString('ja-JP'));
  const dt = (s) => (s ? String(s).replace('T', ' ').slice(0, 16) : '—');
  const STATUS = { ok: '取得済み', partial: '一部取得', stale: '旧データ', error: '取得失敗', unconfigured: '未接続' };
  let state = null;
  let view = 'today';

  /** API 呼び出し（エラーは画面に出す）。 */
  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    return body;
  }
  const params = () => `budget=${encodeURIComponent($('#budget').value || 180)}&count=${encodeURIComponent($('#count').value)}`;

  async function load(sync = false) {
    const btn = $('#sync');
    $('#message').innerHTML = sync ? '<span class="spinner"></span> 各ソースを読み取り中（書き込みはしません）…' : '';
    btn.disabled = true;
    try {
      state = sync ? await api(`/api/sync?${params()}`, { method: 'POST', body: '{}' }) : await api(`/api/state?${params()}`);
      $('#message').textContent = sync ? `取得完了 ${dt(state.generatedAt)}` : '';
      render();
    } catch (e) {
      $('#message').textContent = `エラー: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  }

  function render() {
    const r = state.rec;
    $('#subtitle').textContent = `${r.date}（日本時間）· 使える時間 ${r.budget}分 · 生成 ${dt(state.generatedAt)}`;
    renderSyncbar();
    renderAlerts();
    renderMetrics();
    renderView();
  }

  function renderSyncbar() {
    $('#syncbar').innerHTML = Object.values(state.sources).map((s) =>
      `<span class="chip st-${esc(s.status)}" title="${esc((s.errors || []).join(' / ') || s.scope)}"><span class="dot"></span><b>${esc(s.label)}</b><span class="st-label">${esc(STATUS[s.status] || s.status)}</span>${s.dataAsOf ? `対象 ${esc(dt(s.dataAsOf))}` : ''}</span>`).join('');
  }

  function renderAlerts() {
    const r = state.rec;
    const parts = [];
    if (r.notice) parts.push(`<section class="alert info"><strong>読み方</strong><p>${esc(r.notice)}</p></section>`);
    if (r.holds.length) {
      parts.push(`<section class="alert"><strong>判断を保留している事項（データ不足・更新失敗）</strong><ul>${r.holds.map((h) => `<li>${esc(h.source)}：${esc(h.what)}${h.detail ? ' — ' + esc(h.detail) : ''}</li>`).join('')}</ul></section>`);
    }
    $('#alerts').innerHTML = parts.join('');
  }

  function metric(label, value, small, cls = '', tag = '') {
    return `<div class="metric"><div class="metric-label">${esc(label)}${tag ? `<span class="tag">${esc(tag)}</span>` : ''}</div><div class="metric-value ${cls}">${esc(value)}</div><small>${esc(small)}</small></div>`;
  }

  function renderMetrics() {
    const m = [];
    const c = state.cash;
    if (c) {
      const a = c.actual;
      m.push(metric('銀行残高（実績）', yen(a.balance), `${a.bankLastDate} の明細まで · GMO2口座`));
      m.push(metric('実質手元資金（実績）', yen(a.effective), `銀行残高 − UPSIDER未払 ${yen(a.upsiderPayable)}`));
      const f = c.forecast;
      if (f && f.min) m.push(metric('保存予測の最低残高', yen(f.min.close), `${f.min.label} · ${dt(f.updated)}作成`, f.min.close < 0 ? 'negative' : '', '前提未確認'));
    } else {
      m.push(metric('資金繰り', '取得失敗', '0円とは扱いません'));
    }
    const fl = state.flycka;
    const l7 = fl && fl.comparison.periods.last7;
    if (l7 && (l7.shopify || l7.meta)) {
      m.push(metric('FLYCKA 直近7日', l7.shopify ? yen(l7.shopify.sales) : '不明',
        `Shopify売上（税込）· Meta ROAS ${l7.meta && l7.meta.roas != null ? l7.meta.roas : '不明'}（帰属値）`));
    } else {
      m.push(metric('FLYCKA', '取得失敗', '0円とは扱いません'));
    }
    $('#metrics').innerHTML = m.join('');
  }

  function evidence(c) {
    return c.evidence.slice().sort((a, b) => b.points - a.points)
      .map((e) => `<p class="ev"><span class="pt">+${esc(e.points)}</span>${esc(e.label)} <span class="muted">（${esc(e.source)}${e.url ? '・' : ''}${link(e.url, '元データ')}）</span></p>`).join('');
  }

  function card(c, i) {
    const badges = [
      c.confirmed ? `<span class="badge">確定Todo ${esc(c.id)}</span>` : `<span class="badge data">データ由来の確認</span>`,
      c.stale ? `<span class="badge stale">更新${esc(c.age ?? '不明')}日前 · 現状確認</span>` : '',
      `<span class="badge">${esc(c.timeType)} ${esc(c.minutes)}分${c.minutesAssumed ? '（仮）' : ''}</span>`,
    ].join(' ');
    const role = c.role || {};
    return `<article class="task"><div class="task-top"><div class="rank">${i + 1}</div><div class="task-main">
      <div class="meta">${badges}${c.due ? `<span>期限 ${esc(c.due)}</span>` : ''}${c.project ? `<span>${esc(c.project)}</span>` : ''}</div>
      <h3>${esc(c.action)}</h3>
      <p class="reason">${esc((c.evidence.slice().sort((a, b) => b.points - a.points)[0] || {}).label || '')}</p>
      <div class="first"><b>最初の5分</b>${esc(c.first5)}</div>
      <div class="task-bottom"><span>本人: ${esc(role.me || '—')}</span>${role.ai ? `<span>AI: ${esc(role.ai)}</span>` : ''}${role.person ? `<span>人: ${esc(role.person)}</span>` : ''}${link(c.url, '元データを開く')}</div>
      <details><summary>根拠（${esc(c.score)}点の内訳）</summary>${evidence(c)}${c.doneWhen ? `<p class="ev">完了条件: ${esc(c.doneWhen)}</p>` : ''}</details>
    </div></div></article>`;
  }

  const views = {
    today() {
      const r = state.rec;
      if (!r.top.length) return `<div class="empty">今日の候補はありません。${esc(r.notice)}</div>`;
      return `<div class="section-intro"><p>確認 ${r.used['確認']}分 ＋ 実作業 ${r.used['実作業']}分 ／ 使える時間 ${r.budget}分。候補が足りない場合は無理に埋めません。</p></div>${r.top.map(card).join('')}
        ${r.deferred.length ? `<details><summary>次点（${r.deferred.length}件）</summary>${r.deferred.map((d) => `<p class="ev"><span class="pt">${esc(d.score)}</span>${esc(d.action)} — ${esc(d.deferReason)} ${link(d.url, '元データ')}</p>`).join('')}</details>` : ''}`;
    },
    checks() {
      const r = state.rec;
      const c = state.cash;
      const checks = (c && c.checks) || [];
      return `<div class="panel"><h3>出典の食い違い（JARVISは黙って解消しません）</h3>${r.conflicts.length ? r.conflicts.map((x) => `<div class="check medium"><h4>${esc(x.title)}</h4><p>${esc(x.detail)}</p><p class="muted">→ ${esc(x.action)}</p></div>`).join('') : '<p>検出なし</p>'}</div>
        <div class="panel"><h3>資金繰りの確認待ち前提</h3>${checks.map((k) => `<div class="check ${esc(k.severity)}"><h4>${esc(k.title)}</h4><p>${esc(k.detail)}</p><p class="muted">→ ${esc(k.action)} ${(k.evidence || []).map((e) => link(e.url, e.source)).join(' ')}</p></div>`).join('') || '<p>資金繰りデータなし</p>'}</div>
        <div class="panel"><h3>判断保留</h3>${r.holds.map((h) => `<div class="check high"><h4>${esc(h.source)}：${esc(h.what)}</h4><p>${esc(h.detail)}</p>${(h.notes || []).map((n) => `<p class="muted">${esc(n)}</p>`).join('')}</div>`).join('') || '<p>なし</p>'}</div>
        ${batchPanel()}`;
    },
    delegate() {
      const r = state.rec;
      return `<div class="panel"><h3>任せられる部分（依頼・送信はしていません）</h3>${r.delegation.map((d) => `<p class="ev"><b>${esc(d.id)}</b> ${esc(d.title)} — ${esc([d.role.ai, d.role.person].filter(Boolean).join(' / '))} ${link(d.url, '元データ')}</p>`).join('') || '<p>候補なし</p>'}</div>
        <div class="panel"><h3>相手待ち（本人の新規作業ではない）</h3>${r.waiting.map((w) => `<p class="ev"><b>${esc(w.id)}</b> ${esc(w.title)} — ${esc(w.waitingFor || '相手不明')} · 期限 ${esc(w.due || '—')} · 更新 ${esc(w.updated || '—')} ${link(w.url, '元データ')}</p>`).join('') || '<p>なし</p>'}</div>
        <div class="panel"><h3>今日やらなくてよいこと</h3><p>完了・中止・不要 ${r.notToday.excluded.length}件 ／ 時間・件数の範囲外 ${r.notToday.deferred}件 ／ Slack推定（未確定）${r.notToday.slackUnconfirmed}件</p>
        <details><summary>完了・中止・不要の一覧</summary>${r.notToday.excluded.map((x) => `<p class="ev">${esc(x.id)} ${esc(x.title)}（${esc(x.status)}）</p>`).join('')}</details></div>`;
    },
    cash() {
      const c = state.cash;
      if (!c) return `<div class="empty">資金繰りデータを取得できていません。</div>`;
      const a = c.actual;
      const f = c.forecast;
      const weeks = f ? f.weeks.map((w) => `<tr><td>${esc(w.label)}</td><td>${esc(w.kind)}</td><td class="num">${esc(yen(w.open))}</td><td class="num ${w.close < 0 ? 'negative' : ''}">${esc(yen(w.close))}</td></tr>`).join('') : '';
      return `<div class="panel"><h3>① 実残高（実績）</h3><dl class="kv"><dt>銀行残高</dt><dd>${esc(yen(a.balance))}（${esc(a.bankLastDate)} の明細まで）</dd><dt>実質手元資金</dt><dd>${esc(yen(a.effective))}（UPSIDER未払 ${esc(yen(a.upsiderPayable))} を控除）</dd><dt>対象範囲</dt><dd>${esc(a.scope)}</dd><dt>日次CF更新</dt><dd>${esc(dt(a.dailyCfUpdated))} ${link(a.link, '03_日次CF')}</dd></dl></div>
        <div class="panel"><h3>② 保存予測（13週・前提つき）</h3>${f ? `<p>作成 ${esc(dt(f.updated))} · 銀行反映 ${esc(f.bankReflectedDate)} · ${esc(f.currentLabel)} ${esc(yen(f.currentBalance))} · 警戒ライン ${esc(yen(f.warningLine))}</p><p class="muted">自動更新されない手動作成の保存値です。最新値ではありません。</p><div class="table-wrap"><table><thead><tr><th>週</th><th>種別</th><th>期首</th><th>週末残高</th></tr></thead><tbody>${weeks}</tbody></table></div>` : '<p>読めませんでした</p>'}</div>
        <div class="panel"><h3>③ 確認待ちの前提</h3>${c.checks.map((k) => `<div class="check ${esc(k.severity)}"><h4>${esc(k.title)}</h4><p>${esc(k.detail)}</p><p class="muted">→ ${esc(k.action)}</p></div>`).join('')}</div>
        <p class="muted">${link(c.sheetUrl, '資金繰り原本を開く')}</p>`;
    },
    flycka() {
      const f = state.flycka;
      const s = state.sources.flycka || {};
      if (!f) return `<div class="empty">FLYCKA実績を取得できていません。${esc((s.errors || []).join(' / '))}</div>`;
      const p = f.comparison.periods;
      const row = (k, label) => {
        const x = p[k] || {};
        const sh = x.shopify;
        const me = x.meta;
        return `<tr><td>${esc(label)}</td><td class="num">${sh ? esc(yen(sh.sales)) : '不明'}</td><td class="num">${sh ? esc(sh.orders) : '—'}</td><td class="num">${sh ? esc(yen(sh.aov)) : '—'}</td><td class="num">${sh ? esc(yen(sh.refunds)) : '—'}</td><td class="num">${me ? esc(yen(me.spend)) : '不明'}</td><td class="num">${me ? esc(Math.round(me.purchases)) : '—'}</td><td class="num">${me ? esc(yen(me.cpa)) : '—'}</td><td class="num">${me && me.roas != null ? esc(me.roas) : '—'}</td></tr>`;
      };
      return `<div class="panel"><h3>Shopify と Meta広告（別指標・合算しない）</h3><div class="table-wrap"><table><thead><tr><th>期間</th><th>Shopify売上</th><th>注文</th><th>客単価</th><th>返金</th><th>Meta広告費</th><th>Meta購入</th><th>CPA</th><th>ROAS（帰属）</th></tr></thead><tbody>
        ${row('today_partial', '当日（未確定）')}${row('yesterday', '前日')}${row('day_before', '前々日')}${row('last7', '直近7日')}${row('prev7', '比較7日')}</tbody></table></div>
        ${(s.notes || []).map((n) => `<p class="muted">${esc(n)}</p>`).join('')}</div>
        <div class="panel"><h3>確認すべきこと</h3>${f.checks.map((k) => `<div class="check ${esc(k.severity)}"><h4>${esc(k.title)}</h4><p>${esc(k.detail)}</p><p class="muted">→ ${esc(k.action)}</p></div>`).join('') || '<p>大きな変化は検知していません（閾値: ROAS −15%・売上 −20%）。</p>'}
        <div class="check low"><h4>貢献利益: 不明</h4><p>${esc(f.profit.reason)}</p></div></div>`;
    },
    slack() {
      const s = state.sources.slack || {};
      const d = state.slack;
      if (!d) return `<div class="panel"><h3>Slack：${esc(STATUS[s.status] || s.status)}</h3><p>${esc((s.errors || []).join(' / '))}</p>${(s.notes || []).map((n) => `<p class="muted">${esc(n)}</p>`).join('')}</div>`;
      const groups = ['未対応', '相手待ち', '情報', '対応済み候補'];
      const cov = d.coverage || [];
      return `<div class="panel"><h3>カバレッジ</h3><p>見えるチャンネル ${esc(d.channelsVisible)} ／ 取得可 ${esc(d.channelsReadable)} ／ トークン種別 ${esc(d.tokenKind)}${(d.missingScopes || []).length ? ` ／ 不足スコープ ${esc(d.missingScopes.join(', '))}` : ''}</p>
        <details><summary>チャンネル別の取得結果</summary><div class="table-wrap"><table><thead><tr><th>チャンネル</th><th>種別</th><th>結果</th><th>取得件数</th></tr></thead><tbody>${cov.map((c) => `<tr><td>#${esc(c.name)}</td><td>${c.private ? '非公開' : '公開'}</td><td>${esc(c.status)}</td><td class="num">${esc(c.messages)}</td></tr>`).join('')}</tbody></table></div></details></div>
        ${groups.map((g) => { const items = (d.items || []).filter((i) => i.status === g); return `<div class="panel"><h3>${esc(g)}（${items.length}）<span class="badge est">推定</span></h3>${items.slice(0, 30).map((i) => `<p class="ev">#${esc(i.channel)} ${esc(dt(i.postedAt))} [${esc(i.types.join('/'))}] ${esc(i.text)} ${link(i.link, '元メッセージ')}${i.duplicates ? ` <span class="muted">（重複${esc(i.duplicates)}件を統合）</span>` : ''}</p>`).join('') || '<p class="muted">なし</p>'}</div>`; }).join('')}`;
    },
    sources() {
      return `<div class="table-wrap"><table><thead><tr><th>ソース</th><th>状態</th><th>取得</th><th>最終成功</th><th>データ対象</th><th>範囲・エラー</th></tr></thead><tbody>${Object.values(state.sources).map((s) => `<tr><td>${esc(s.label)}</td><td>${esc(STATUS[s.status] || s.status)}</td><td>${esc(dt(s.fetchedAt))}</td><td>${esc(dt(s.lastSuccessAt))}</td><td>${esc(dt(s.dataAsOf))}</td><td>${esc(s.scope)}${(s.errors || []).map((e) => `<p class="negative">${esc(e)}</p>`).join('')}${(s.notes || []).map((n) => `<p class="muted">${esc(n)}</p>`).join('')}</td></tr>`).join('')}</tbody></table></div>
        <p class="muted">自由会話AI: ${esc(state.llm.note)}。${link(state.todoUrl, 'Todo管理表を開く')}</p>${batchPanel()}`;
    },
  };

  function batchPanel() {
    const b = state.batch && state.batch.backoffice;
    if (!b) return '';
    const fl = (state.batch.flycka || []).map((w) => `${esc(w.workflow)}: ${esc(w.conclusion)}（${esc(dt(w.createdAt))}）`).join(' ／ ');
    return `<div class="panel"><h3>定期バッチの実結果</h3><p>backoffice 日次バッチ ${esc(dt(b.createdAt))} · ${esc(b.conclusion)} ${link(b.url, '実行ログ')}</p>
      <div class="table-wrap"><table><thead><tr><th>ステップ</th><th>実結果</th><th>検出</th></tr></thead><tbody>${b.steps.map((s) => `<tr><td>${esc(s.name)}</td><td class="${s.outcome !== 'success' ? 'negative' : ''}">${s.outcome === 'warning' ? '成功表示だがエラー' : esc(s.outcome)}</td><td>${esc(Object.entries(s.patterns).map(([k, v]) => `${k}×${v}`).join(' / '))}</td></tr>`).join('')}</tbody></table></div>
      <p class="muted">flycka-d2c: ${fl || '—'}</p></div>`;
  }

  function renderView() {
    document.querySelectorAll('.tabs button').forEach((b) => { const on = b.dataset.view === view; b.classList.toggle('active', on); b.setAttribute('aria-pressed', on); });
    $('#content').innerHTML = views[view]();
  }

  function addBubble(text, who) {
    const div = document.createElement('div');
    div.className = `bubble ${who}`;
    div.textContent = text;
    $('#messages').appendChild(div);
    $('#messages').scrollTop = $('#messages').scrollHeight;
  }

  async function ask(text) {
    if (!text.trim()) return;
    addBubble(text, 'user');
    try {
      const res = await api('/api/chat', { method: 'POST', body: JSON.stringify({ message: text, budget: +$('#budget').value || 180 }) });
      addBubble(res.text, 'assistant');
    } catch (e) {
      addBubble(`エラー: ${e.message}`, 'assistant');
    }
  }

  document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => { view = b.dataset.view; if (state) renderView(); }));
  document.querySelectorAll('[data-ask]').forEach((b) => b.addEventListener('click', () => ask(b.dataset.ask)));
  $('#chat').addEventListener('submit', (e) => { e.preventDefault(); const v = $('#prompt').value; $('#prompt').value = ''; ask(v); });
  $('#recalc').addEventListener('click', () => load(false));
  $('#sync').addEventListener('click', () => load(true));
  load(false);
})();
