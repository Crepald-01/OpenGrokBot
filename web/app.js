// OpenGrokBot mobile web app. Plain JS, no build step. Talks to the same local service as the desktop app.
(() => {
  'use strict';
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const S = { bots: [], groups: [], busy: {}, approvals: [], thread: null, items: [], stream: {}, view: 'bots', es: null, lastEvent: 0, screenBot: null, screenTimer: null, titleBase: document.title };

  // ------------------------------------------------------------------ api
  async function api(path, opts = {}) {
    const init = { credentials: 'same-origin', headers: {}, method: opts.method || 'GET' };
    if (opts.body !== undefined) { init.method = opts.method || 'POST'; init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    const r = await fetch(path, init);
    if (r.status === 401) { showLogin(); throw new Error('Not signed in'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || data.detail || ('HTTP ' + r.status));
    return data;
  }

  // ---------------------------------------------------------------- markdown
  function md(src) {
    const blocks = [];
    let t = esc(src).replace(/```(\w*)\n([\s\S]*?)```/g, (_, l, code) => { blocks.push('<pre><code>' + code + '</code></pre>'); return '\u0000' + (blocks.length - 1) + '\u0000'; });
    const inline = (s) => s.replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>').replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<i>$2</i>')
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    const out = []; let list = null;
    const flush = () => { if (list) { out.push('</' + list + '>'); list = null; } };
    for (const line of t.split('\n')) {
      let m;
      if ((m = line.match(/^\s*[-*]\s+(.*)/))) { if (list !== 'ul') { flush(); out.push('<ul>'); list = 'ul'; } out.push('<li>' + inline(m[1]) + '</li>'); }
      else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== 'ol') { flush(); out.push('<ol>'); list = 'ol'; } out.push('<li>' + inline(m[1]) + '</li>'); }
      else if ((m = line.match(/^#{1,4}\s+(.*)/))) { flush(); out.push('<p><b>' + inline(m[1]) + '</b></p>'); }
      else if (line.trim() === '') { flush(); }
      else { flush(); out.push('<p>' + inline(line) + '</p>'); }
    }
    flush();
    return out.join('').replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[+i]);
  }

  // ------------------------------------------------------------------ views
  function showLogin() { $('#app').hidden = true; $('#login').hidden = false; if (S.es) { S.es.close(); S.es = null; } }
  function setView(v) {
    S.view = v;
    for (const id of ['bots', 'chat', 'approvals', 'settings', 'screen']) $('#view-' + id).hidden = id !== v;
    document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.view === v || (v === 'chat' && b.dataset.view === 'bots')));
    $('#tabs').hidden = v === 'screen';
    $('#back').hidden = !(v === 'chat');
    $('#title').textContent = { bots: 'Bots', approvals: 'Approvals', settings: 'Settings', screen: 'Browser' }[v] || $('#title').textContent;
    if (v === 'bots') renderBots(); if (v === 'approvals') renderApprovals(); if (v === 'settings') renderSettings();
  }
  function botById(id) { return S.bots.find((b) => b.id === id); }
  function stateOf(botId) {
    const b = S.busy[botId];
    if (!b) return ['idle', ''];
    if (b.status === 'waiting_approval') return ['wait', 'needs you'];
    return ['work', b.status === 'retrying' ? 'retrying' : 'working'];
  }

  function renderBots() {
    const el = $('#view-bots');
    const rows = S.bots.map((b) => {
      const [cls, label] = stateOf(b.id);
      return `<div class="card bot-row" data-bot="${b.id}"><div class="emoji">${esc(b.emoji || '🤖')}</div><div class="meta"><div class="name">${esc(b.name)}</div>` +
        `<div class="job">${esc(b.job || 'No job yet')}</div></div>${label ? `<span class="pill ${cls}">${label}</span>` : ''}</div>`;
    }).join('');
    const groups = S.groups.map((g) => `<div class="card bot-row" data-group="${g.thread_id}"><div class="emoji">👥</div><div class="meta"><div class="name">${esc(g.name)}</div>` +
      `<div class="job">${esc(g.member_names.join(', '))}</div></div></div>`).join('');
    el.innerHTML = (rows || '<p class="muted">No Bots yet. Create one in the desktop app.</p>') + (groups ? '<h3 class="section">Group chats</h3>' + groups : '');
    el.querySelectorAll('[data-bot]').forEach((n) => n.onclick = () => openBot(n.dataset.bot));
    el.querySelectorAll('[data-group]').forEach((n) => n.onclick = () => openThread(n.dataset.group, S.groups.find((g) => g.thread_id === n.dataset.group).name, null));
  }

  async function openBot(botId) {
    const ths = await api('/api/bots/' + botId + '/threads');
    const bot = botById(botId);
    openThread(ths[0].id, (bot.emoji || '') + ' ' + bot.name, botId);
  }

  async function openThread(tid, title, botId) {
    const d = await api('/api/threads/' + tid);
    S.thread = { id: tid, title, botId, running: d.running.length > 0 };
    S.items = d.items; S.approvals = [...S.approvals.filter((a) => a.thread_id !== tid), ...d.approvals];
    setView('chat'); $('#title').textContent = title;
    renderMessages(true); renderThreadApprovals(); updateChatStatus();
  }

  function itemHTML(it) {
    if (it.type === 'user') return `<div class="msg user">${md(it.text)}${(it.images || []).map((i) => `<img src="/api/files/shots/${encodeURIComponent(i)}">`).join('')}</div>`;
    if (it.type === 'assistant') return `<div class="msg assistant">${S.thread && !S.thread.botId ? `<div class="who">${esc(it.emoji || '')} ${esc(it.name || '')}</div>` : ''}${md(it.text)}</div>`;
    if (it.type === 'tool') {
      const ic = { running: '●', ok: '✓', error: '✕', denied: '⛔', blocked: '⛔' }[it.status] || '•';
      return `<div class="tool ${it.status}"><span class="ic">${ic}</span><span>${esc(it.label || it.tool)}${(it.images || []).map((i) => `<img src="/api/files/shots/${encodeURIComponent(i)}">`).join('')}</span></div>`;
    }
    if (it.type === 'notice') return `<div class="notice ${esc(it.level)}">${esc(it.text)}</div>`;
    if (it.type === 'system_note') return `<div class="notice">${esc(it.text.slice(0, 200))}</div>`;
    return '';
  }

  function renderMessages(scroll) {
    const el = $('#messages');
    const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    let html = S.items.map(itemHTML).join('');
    for (const [sid, st] of Object.entries(S.stream)) if (st.thread === (S.thread && S.thread.id) && st.text) html += `<div class="msg assistant">${md(st.text)}</div>`;
    el.innerHTML = html;
    if (scroll || atEnd) el.scrollTop = el.scrollHeight;
  }

  function mergeItems(items) {
    for (const it of items) {
      if (it.type === 'tool') {
        const cur = S.items.find((x) => x.type === 'tool' && x.call_id === it.call_id);
        if (cur) { Object.assign(cur, it.partial ? { status: it.status, summary: it.summary, images: it.images, duration_ms: it.duration_ms } : it); continue; }
      }
      if (it.type === 'assistant' && it.stream_id) delete S.stream[it.stream_id];
      if (!S.items.find((x) => x.id === it.id && x.type === it.type && it.type !== 'tool')) S.items.push(it);
    }
  }

  function updateChatStatus() {
    const el = $('#chat-status');
    const run = S.thread && Object.values(S.busy).find((r) => r.thread_id === S.thread.id);
    $('#stop').hidden = !run;
    el.hidden = !run;
    if (run) el.textContent = ({ waiting_approval: 'Waiting for your approval…', waiting_screen: 'Waiting for its screen…', retrying: 'Retrying after a hiccup…' }[run.status]) || `Working… step ${run.steps || 1}`;
  }

  function approvalHTML(a) {
    const d = a.details || {};
    if (a.category === 'question') {
      const opts = (d.options || []).map((o) => `<button data-ans="${esc(o)}">${esc(o)}</button>`).join('');
      return `<div class="card approval q" data-id="${a.id}"><h4>${esc(botName(a.bot_id))} asks</h4><div>${esc(a.summary)}</div><div class="row">${opts}</div>` +
        `<div class="row"><input placeholder="Type an answer" class="ans"><button class="primary send-ans">Send</button></div></div>`;
    }
    if (a.category === 'takeover' || a.category === 'login') {
      return `<div class="card approval" data-id="${a.id}"><h4>${esc(botName(a.bot_id))} needs you at the browser</h4><div>${esc(a.summary)}</div>` +
        `<div class="row"><button class="primary open-screen" data-bot="${a.bot_id}">Open browser</button><button class="approve">I'm done</button><button class="deny">Can't do it</button></div></div>`;
    }
    const taint = a.tainted ? '<p class="err">⚠ Earlier content in this task contained instruction-like text. Be extra careful.</p>' : '';
    const extra = d.body || d.command || d.preview || d.text || '';
    return `<div class="card approval" data-id="${a.id}"><h4>${esc(botName(a.bot_id))}: ${esc(a.title || a.category)}</h4><div>${esc(a.summary)}</div>${taint}` +
      (extra ? `<pre>${esc(String(extra).slice(0, 800))}</pre>` : '') + (d.auto_review ? `<p class="muted">${esc(d.auto_review)}</p>` : '') +
      `<div class="row"><button class="primary approve">Approve</button><button class="remember">Approve &amp; always</button><button class="deny">Deny</button></div></div>`;
  }
  function botName(id) { const b = botById(id); return b ? b.name : 'Bot'; }

  function wireApprovals(root) {
    root.querySelectorAll('.approval').forEach((card) => {
      const id = card.dataset.id;
      const decide = (body) => api('/api/approvals/' + id + '/decide', { body }).then(refreshApprovals).catch((e) => toast('Error', e.message));
      const q = (s) => card.querySelector(s);
      if (q('.approve')) q('.approve').onclick = () => decide({ approve: true });
      if (q('.remember')) q('.remember').onclick = () => decide({ approve: true, remember: true });
      if (q('.deny')) q('.deny').onclick = () => decide({ approve: false });
      card.querySelectorAll('[data-ans]').forEach((b) => b.onclick = () => decide({ approve: true, answer: b.dataset.ans }));
      if (q('.send-ans')) q('.send-ans').onclick = () => { const v = q('.ans').value.trim(); if (v) decide({ approve: true, answer: v }); };
      if (q('.open-screen')) q('.open-screen').onclick = () => openScreen(q('.open-screen').dataset.bot);
    });
  }
  function renderThreadApprovals() {
    const el = $('#chat-approvals');
    const mine = S.approvals.filter((a) => S.thread && a.thread_id === S.thread.id && a.status === 'pending');
    el.innerHTML = mine.map(approvalHTML).join('');
    wireApprovals(el);
  }
  function renderApprovals() {
    const el = $('#view-approvals');
    const pend = S.approvals.filter((a) => a.status === 'pending');
    el.innerHTML = pend.length ? pend.map(approvalHTML).join('') : '<p class="muted">Nothing needs your attention. Your Bots will come back here when they need an approval.</p>';
    wireApprovals(el);
  }
  async function refreshApprovals() {
    S.approvals = await api('/api/approvals?status=pending');
    updateBadge(); if (S.view === 'approvals') renderApprovals(); if (S.view === 'chat') renderThreadApprovals();
  }
  function updateBadge() {
    const n = S.approvals.filter((a) => a.status === 'pending').length;
    $('#badge').hidden = !n; $('#badge').textContent = n;
    document.title = (n ? `(${n}) ` : '') + S.titleBase;
  }

  async function renderSettings() {
    const el = $('#view-settings');
    const perm = 'Notification' in window ? Notification.permission : 'unsupported';
    el.innerHTML = `<div class="card"><b>Notifications</b><p class="muted">Alerts appear here while this page is open. ${window.isSecureContext ? '' : 'System notifications need HTTPS; for push while the app is closed, set an ntfy topic in the desktop app (Settings &gt; Mobile).'}</p>` +
      `<p>System notifications: <b>${perm}</b></p>${perm === 'default' ? '<button id="ask-notif" class="primary">Enable</button>' : ''}</div>` +
      `<div class="card"><b>Install</b><p class="muted">Use your browser menu: “Add to Home screen” to get a full-screen app icon.</p></div>` +
      `<div class="card"><button id="logout">Sign out of this phone</button></div>`;
    if ($('#ask-notif')) $('#ask-notif').onclick = async () => { await Notification.requestPermission(); renderSettings(); };
    $('#logout').onclick = async () => { document.cookie = 'gb_token=; Max-Age=0; path=/'; location.reload(); };
  }

  // ----------------------------------------------------------------- toasts
  function toast(title, body, onclick) {
    const el = document.createElement('div'); el.className = 't'; el.innerHTML = `<b>${esc(title)}</b>${esc(body || '')}`;
    if (onclick) el.onclick = onclick;
    $('#toast').appendChild(el); setTimeout(() => el.remove(), 7000);
  }
  function systemNotify(n) {
    if (navigator.vibrate) navigator.vibrate(n.urgent ? [120, 60, 120] : 60);
    if (document.visibilityState === 'visible') return;
    if ('Notification' in window && Notification.permission === 'granted') {
      navigator.serviceWorker?.getRegistration().then((reg) => (reg ? reg.showNotification(n.title, { body: n.body, tag: n.kind, icon: '/icon.svg' }) : new Notification(n.title, { body: n.body })))
        .catch(() => { try { new Notification(n.title, { body: n.body }); } catch (e) { /* ignore */ } });
    }
  }

  // ----------------------------------------------------------------- events
  function connect() {
    if (S.es) S.es.close();
    const es = new EventSource('/api/events?kind=mobile' + (S.lastEvent ? '&since=' + S.lastEvent : ''));
    S.es = es;
    es.onopen = () => $('#conn').classList.remove('off');
    es.onerror = () => $('#conn').classList.add('off');
    es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (e) { console.error(e); } };
  }
  function onEvent(ev) {
    S.lastEvent = Math.max(S.lastEvent, ev.id || 0);
    switch (ev.type) {
      case 'message':
        if (S.thread && ev.thread_id === S.thread.id) { mergeItems(ev.items); renderMessages(); }
        break;
      case 'delta': {
        const st = S.stream[ev.stream_id] || (S.stream[ev.stream_id] = { text: '', thread: ev.thread_id });
        st.text += ev.text;
        if (S.thread && ev.thread_id === S.thread.id) renderMessages();
        break;
      }
      case 'delta_reset': delete S.stream[ev.stream_id]; if (S.thread && ev.thread_id === S.thread.id) renderMessages(); break;
      case 'turn':
        if (['done', 'stopped', 'error', 'limit', 'skipped'].includes(ev.status)) delete S.busy[ev.bot_id];
        else S.busy[ev.bot_id] = { thread_id: ev.thread_id, status: ev.status, steps: ev.steps, turn_id: ev.turn_id };
        if (S.view === 'bots') renderBots(); updateChatStatus();
        break;
      case 'approval': refreshApprovals(); break;
      case 'bots': case 'groups': loadBootstrap(); break;
      case 'notification':
        toast(ev.title, ev.body, () => { if (ev.thread_id) openThread(ev.thread_id, ev.bot_name || 'Thread', null); });
        systemNotify(ev); break;
      default: break;
    }
  }

  async function loadBootstrap() {
    const d = await api('/api/bootstrap');
    S.bots = d.bots; S.groups = d.groups; S.approvals = d.approvals; S.busy = {};
    for (const [bid, r] of Object.entries(d.status.busy || {})) S.busy[bid] = r;
    S.lastEvent = Math.max(S.lastEvent, d.last_event || 0);
    updateBadge(); if (S.view === 'bots') renderBots();
    $('#login').hidden = true; $('#app').hidden = false;
  }

  // ------------------------------------------------------------------ chat
  async function send() {
    const ta = $('#input'); const text = ta.value.trim();
    if (!text || !S.thread) return;
    ta.value = ''; autoGrow();
    try { await api('/api/threads/' + S.thread.id + '/messages', { body: { text } }); } catch (e) { toast('Could not send', e.message); ta.value = text; }
  }
  function autoGrow() { const ta = $('#input'); ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, window.innerHeight * 0.4) + 'px'; }

  // ------------------------------------------------- take over (remote view)
  async function openScreen(botId) {
    S.screenBot = botId; setView('screen');
    try { await api('/api/bots/' + botId + '/takeover', { body: { action: 'start' } }); } catch (e) { toast('Browser', e.message); }
    const img = $('#scr-img');
    const tick = () => { img.src = '/api/bots/' + botId + '/screen.jpg?q=55&t=' + Date.now(); };
    img.onload = () => { S.screenTimer = setTimeout(tick, 700); }; img.onerror = () => { S.screenTimer = setTimeout(tick, 2500); };
    tick();
  }
  async function closeScreen(handBack) {
    clearTimeout(S.screenTimer); const id = S.screenBot; S.screenBot = null;
    if (handBack && id) { try { await api('/api/bots/' + id + '/takeover', { body: { action: 'handback' } }); toast('Handed back', 'The Bot will continue.'); } catch (e) { toast('Error', e.message); } }
    if (S.thread) setView('chat'); else setView('bots');
  }
  const input = (ev) => api('/api/bots/' + S.screenBot + '/screen/input', { body: ev }).catch((e) => toast('Browser', e.message));

  function wire() {
    $('#login-btn').onclick = async () => {
      try { await api('/api/login', { body: { token: $('#token').value.trim() } }); await loadBootstrap(); connect(); setView('bots'); }
      catch (e) { $('#login-err').textContent = e.message; }
    };
    $('#token').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#login-btn').click(); });
    document.querySelectorAll('#tabs button').forEach((b) => b.onclick = () => setView(b.dataset.view));
    $('#back').onclick = () => { S.thread = null; setView('bots'); };
    $('#composer').onsubmit = (e) => { e.preventDefault(); send(); };
    $('#input').addEventListener('input', autoGrow);
    $('#stop').onclick = () => S.thread && api('/api/threads/' + S.thread.id + '/stop', { body: {} });
    $('#scr-done').onclick = () => closeScreen(true);
    $('#scr-back').onclick = () => closeScreen(false);
    $('#scr-reload').onclick = () => input({ type: 'reload' });
    $('#scr-go').onclick = () => { const u = $('#scr-url').value.trim(); if (u) input({ type: 'navigate', url: u }); };
    $('#scr-type').onclick = () => { const t = $('#scr-text').value; if (t) { input({ type: 'text', text: t }); $('#scr-text').value = ''; } };
    $('#scr-enter').onclick = () => input({ type: 'key', key: 'Enter' });
    $('#scr-up').onclick = () => input({ type: 'scroll', dy: -500 });
    $('#scr-down').onclick = () => input({ type: 'scroll', dy: 500 });
    $('#scr-img').addEventListener('click', (e) => {
      const img = e.currentTarget; const r = img.getBoundingClientRect();
      input({ type: 'click', x: Math.round((e.clientX - r.left) * (1280 / r.width)), y: Math.round((e.clientY - r.top) * (800 / r.height)) });
    });
  }

  async function start() {
    wire();
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
    try { await loadBootstrap(); connect(); setView('bots'); } catch (e) { /* login screen already shown */ }
    setInterval(() => { if (S.view === 'chat') updateChatStatus(); }, 1500);
  }
  start();
})();
