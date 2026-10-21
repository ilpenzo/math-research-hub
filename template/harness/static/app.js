/* MathDesk page: router, views, live updates, notebook and composer. */
(function () {
  'use strict';

  var S = {
    cachedLookups: [],
    seq: 0, sessions: [], unread: {}, pins: [], status: null, docs: null, projects: [],
    filter: 'all', view: null, session: null, lastSessionFetch: 0,
    singleDollar: true, print: location.pathname.indexOf('/print/') === 0, online: true
  };
  var $ = function (sel, el) { return (el || document).querySelector(sel); };

  // The access token arrives once in the URL fragment (#t=...), which is never sent
  // over HTTP, and is kept in localStorage, which is scoped to this origin and port.
  // Print pages receive a short-lived nonce that is held in memory only.
  // A fragment token is only stored after the server accepts it (any web page
  // could open this URL with a bogus #t=...).
  var TOKEN = null, CANDIDATE = null;
  (function () {
    try { TOKEN = localStorage.getItem('mathdesk.token'); } catch (e) { /* storage blocked */ }
    var m = /^#t=([A-Za-z0-9_-]+)&?(.*)$/.exec(location.hash);
    if (!m) return;
    history.replaceState(null, '', location.pathname + location.search + (m[2] ? '#' + m[2] : ''));
    CANDIDATE = m[1];
  })();

  function adoptToken() {
    if (!CANDIDATE) return Promise.resolve();
    if (location.pathname.indexOf('/print/') === 0) { TOKEN = CANDIDATE; return Promise.resolve(); }
    return fetch('/api/status', { headers: { Authorization: 'Bearer ' + CANDIDATE }, credentials: 'omit' })
      .then(function (r) {
        if (!r.ok) return;
        TOKEN = CANDIDATE;
        try { localStorage.setItem('mathdesk.token', TOKEN); } catch (e) { /* keep in memory */ }
      }).catch(function () { /* server down: keep the stored token */ });
  }
  var esc = function (s) { return Render.escapeHtml(s == null ? '' : s); };
  var SERVICE = { 'claude-code': ['Claude', 'C'], 'codex': ['Codex', 'X'], 'desk': ['Desk', 'D'] };

  // ---------------------------------------------------------------- utilities
  function api(path, body) {
    var opts = { credentials: 'omit', headers: { Authorization: 'Bearer ' + (TOKEN || '') } };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers['Content-Type'] = 'application/json';
      opts.headers['X-MathDesk'] = '1';
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.status === 401 && !S.print) locked();
        if (!r.ok) { var e = new Error(data.error || r.statusText); e.status = r.status; throw e; }
        return data;
      });
    });
  }

  function locked() {
    if (S.locked) return;
    S.locked = true;
    $('#main').innerHTML = '<div class="page empty-state"><p>MathDesk is locked.</p><p class="muted">Open it from the terminal so the access token is supplied:</p><pre>python3 harness/mathdesk.py open</pre></div>';
  }

  // Authenticated binary download, shown in a new tab (PDFs) via a blob URL.
  function openBlob(path, name) {
    var win = window.open('', '_blank');
    if (win) win.document.write('<p style="font:15px -apple-system,sans-serif;padding:2rem">Preparing ' + esc(name || 'PDF') + '…</p>');
    fetch(path, { headers: { Authorization: 'Bearer ' + (TOKEN || '') }, credentials: 'omit' }).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (d) { throw new Error(d.error || r.statusText); });
      return r.blob();
    }).then(function (blob) {
      var url = URL.createObjectURL(blob);
      if (win) win.location = url; else location.href = url;
      setTimeout(function () { URL.revokeObjectURL(url); }, 120000);
    }).catch(function (err) {
      if (win) win.close();
      toast(err.message);
    });
  }

  function blobImage(img, path) {
    fetch(path, { headers: { Authorization: 'Bearer ' + (TOKEN || '') }, credentials: 'omit' })
      .then(function (r) { if (!r.ok) throw new Error(r.statusText); return r.blob(); })
      .then(function (b) { img.src = URL.createObjectURL(b); })
      .catch(function () { img.alt = 'image unavailable'; });
  }

  function load(key, fallback) { try { var v = localStorage.getItem('mathdesk.' + key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; } }
  function save(key, value) { try { localStorage.setItem('mathdesk.' + key, JSON.stringify(value)); } catch (e) { /* private mode */ } }

  function toast(text) {
    var t = $('#toast'); if (!t) return;
    t.textContent = text; t.classList.add('show');
    clearTimeout(toast.timer); toast.timer = setTimeout(function () { t.classList.remove('show'); }, 1800);
  }

  function copy(text, label) {
    var done = function () { toast((label || 'Copied') + ' to the clipboard'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else { fallbackCopy(text); done(); }
  }
  function fallbackCopy(text) {
    var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta);
    ta.select(); try { document.execCommand('copy'); } catch (e) { /* ignore */ } ta.remove();
  }

  function ago(iso) {
    if (!iso) return '';
    var s = (Date.now() - Date.parse(iso)) / 1000;
    if (s < 60) return 'now';
    if (s < 3600) return Math.floor(s / 60) + ' min';
    if (s < 86400) return Math.floor(s / 3600) + ' h';
    return Math.floor(s / 86400) + ' d';
  }
  function when(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function md(text, prefix, extra) {
    var opts = Object.assign({ singleDollar: S.singleDollar, idPrefix: prefix }, extra || {});
    var node = Render.render(text || '', opts);
    if (node.dataset.errors !== '0') S.cachedLookups.push(useCachedLatex(node));
    return node;
  }

  // Formulas KaTeX cannot draw, but which the user typeset with LaTeX before, are
  // shown from the cache straight away (no new compile without a click).
  function useCachedLatex(node) {
    var boxes = Array.prototype.slice.call(node.querySelectorAll('.m-error'));
    return Promise.all(boxes.map(function (box) {
      var q = '/api/tex-cached?display=' + (box.dataset.display ? '1' : '0') + '&tex=' + encodeURIComponent(box.dataset.body || '');
      return api(q).then(function (r) {
        if (!r.ok) return;
        return new Promise(function (resolve) {
          var img = el('img', 'm-svg' + (box.dataset.display ? ' m-svg-display' : ''));
          img.alt = box.dataset.body; img.title = 'Typeset by LaTeX (sandboxed); Alt-click copies the source';
          img.onload = img.onerror = resolve;
          var wrap = el('span', box.dataset.display ? 'm m-display' : 'm');
          wrap.dataset.tex = box.querySelector('.m-src').textContent;
          wrap.appendChild(img); box.replaceWith(wrap);
          blobImage(img, r.url);
        });
      }).catch(function () { /* keep the source box */ });
    }));
  }

  // A one-line title with its mathematics typeset (session titles are often prompts).
  function inlineHtml(text) {
    var node = Render.render(String(text || '').split('\n')[0].slice(0, 200), { singleDollar: S.singleDollar });
    var p = node.querySelector('p');
    return '<span class="t-inline">' + (p ? p.innerHTML : esc(text)) + '</span>';
  }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function badge(service) {
    var s = SERVICE[service] || [service, '?'];
    return '<span class="svc svc-' + esc(service) + '" title="' + esc(s[0]) + '">' + esc(s[1]) + '</span>';
  }

  // ---------------------------------------------------------------- boot
  function boot() {
    S.singleDollar = load('singleDollar', true);
    var theme = load('theme', null);
    if (theme) document.documentElement.dataset.theme = theme;
    adoptToken().then(start);
  }

  function start() {
    Promise.all([
      fetch('/static/macros.json').then(function (r) { return r.json(); }).catch(function () { return {}; }),
      api('/api/ids').catch(function () { retryIds(3000); return {}; }),
      api('/api/status').catch(function () { return null; })
    ]).then(function (res) {
      Render.configure({ macros: res[0], idRegex: res[1].regex, ids: res[1].ids });
      S.status = res[2];
      if (!S.status && !S.print) { locked(); wire(); return; }
      if (S.status && S.status.settings && load('singleDollar', null) === null) S.singleDollar = S.status.settings.single_dollar !== false;
      if (S.print) return printView();
      wire();
      // ?nolive disables the long-poll (headless screenshots wait for idle networks).
      refreshSessions().then(function () { route(); if (!/[?&]nolive\b/.test(location.search)) poll(); });
      refreshPins();
      setInterval(tick, 4000);
    });
  }

  // The hub may be mid-publication at start: retry with backoff, then redraw so
  // IDs already on screen become links.
  function retryIds(delay) {
    setTimeout(function () {
      api('/api/ids').then(function (d) {
        Render.configure({ idRegex: d.regex, ids: d.ids });
        if (S.view && S.view.name === 'session') { $('#convo')._keys = null; refreshSession(false); }
        else route();
      }).catch(function () { if (delay < 60000) retryIds(delay * 2); });
    }, delay);
  }

  // ---------------------------------------------------------------- live updates
  function poll() {
    api('/api/wait?since=' + S.seq + '&timeout=25').then(function (data) {
      setOnline(true);
      var fresh = data.events || [];
      S.seq = data.seq;
      if (fresh.length) onEvents(fresh);
      poll();
    }).catch(function () {
      setOnline(false);
      setTimeout(poll, 2000);
    });
  }

  function setOnline(on) {
    if (on === S.online) return;
    S.online = on;
    var live = $('#live');
    live.classList.toggle('off', !on);
    live.title = on ? 'Connected to the local server' : 'Server not reachable; retrying';
  }

  function onEvents(events) {
    var cur = S.view && S.view.name === 'session' ? S.view.args : null;
    events.forEach(function (e) {
      var key = e.service + '/' + e.session_id;
      var viewing = cur && cur[0] === e.service && cur[1] === e.session_id;
      if (!viewing && e.kind === 'final') S.unread[key] = (S.unread[key] || 0) + 1;
    });
    refreshSessions().then(function () {
      if (!S.view) return;
      if (S.view.name === 'home') showHome(true);
      if (S.view.name === 'session') refreshSession(true);
    });
  }

  function tick() {
    // Intermediate messages appear in transcripts before the turn ends.
    if (S.view && S.view.name === 'session' && S.session) {
      var updated = Date.parse(S.session.session.updated || 0);
      if (Date.now() - updated < 60 * 60 * 1000) refreshSession(true);
    }
    renderRail();
  }

  // ---------------------------------------------------------------- rail
  function refreshSessions() {
    return api('/api/sessions').then(function (data) {
      S.sessions = data.sessions || [];
      if (!S.primed) { S.seq = data.seq || 0; S.primed = true; }
      renderRail();
    })
      .catch(function () { /* offline: keep the last list */ });
  }

  function renderRail() {
    var rail = $('#rail'); if (!rail) return;
    if (!S.print) document.title = (Object.keys(S.unread).length ? '● ' : '') + 'MathDesk';
    var cur = S.view && S.view.name === 'session' ? S.view.args.join('/') : '';
    var list = S.sessions.filter(function (s) { return S.filter === 'all' || s.service === S.filter; });
    var html = '<div class="rail-head"><span>Sessions</span><span class="seg" role="tablist">' +
      ['all', 'claude-code', 'codex', 'desk'].map(function (f) {
        return '<button type="button" data-filter="' + f + '" class="' + (S.filter === f ? 'on' : '') + '">' +
          (f === 'all' ? 'All' : SERVICE[f][0]) + '</button>';
      }).join('') + '</span></div><ul class="sessions">';
    list.slice(0, 80).forEach(function (s) {
      var key = s.service + '/' + s.session_id;
      var fresh = Date.now() - Date.parse(s.updated || 0) < 3 * 60 * 1000;
      html += '<li><a href="#/s/' + key + '" class="' + (key === cur ? 'cur' : '') + '">' + badge(s.service) +
        '<span class="s-main"><span class="s-title">' + (s.title ? inlineHtml(s.title) : 'Untitled session') + '</span>' +
        '<span class="s-meta">' + esc(s.project || 'hub') + ' · ' + esc(ago(s.updated)) +
        (s.source === 'history' ? ' · history' : '') + '</span></span>' +
        (S.unread[key] ? '<span class="unread" title="New responses">' + S.unread[key] + '</span>' : '') +
        (fresh ? '<span class="pulse" title="Active in the last few minutes"></span>' : '') + '</a></li>';
    });
    if (!list.length) html += '<li class="empty">No sessions yet. Finish a turn in Claude Code or Codex inside the hub.</li>';
    html += '</ul><div class="rail-head"><span>Hub</span></div><div id="rail-docs" class="docs">' + docsHtml() + '</div>';
    rail.innerHTML = html;
  }

  function docsHtml() {
    if (!S.docs) return '<a href="#/docs" class="doc-link">Browse documents</a>';
    return '<a href="#/docs" class="doc-link">All documents (' + S.docs.length + ')</a>';
  }

  // ---------------------------------------------------------------- router
  function route() {
    if (S.locked) return;
    var hash = decodeURIComponent(location.hash.replace(/^#\/?/, ''));
    var parts = hash.split('/');
    var main = $('#main');
    var head = parts[0];
    closeDrawerIfRoute();
    if (head === 's' && parts.length >= 3) return showSession(parts[1], parts.slice(2).join('/'));
    if (head === 't' && parts[1]) return showTurn(parts[1]);
    if (head === 'r' && parts[1]) return showRecordPage(parts.slice(1).join('/'));
    if (head === 'd' && parts[1]) return showDoc(parts.slice(1).join('/'));
    if (head === 'docs') return showDocs(parts[1] || '');
    if (head === 'search') return showSearch(parts.slice(1).join('/'));
    if (head === 'notebook') return showNotebook();
    main.dataset.view = 'home';
    return showHome(false);
  }

  function setView(name, args) {
    S.view = { name: name, args: args || [] };
    $('#main').dataset.view = name;
    renderRail();
  }

  // ---------------------------------------------------------------- home
  function showHome(live) {
    setView('home');
    var finals = [];
    var recent = S.sessions.filter(function (s) { return s.last_final; }).slice(0, 12);
    var main = $('#main');
    if (!live) main.innerHTML = '<div class="page"><h1 class="page-title">Latest responses</h1><div id="feed" class="feed"></div></div>';
    var feed = $('#feed');
    if (!recent.length) {
      feed.innerHTML = '<div class="empty-state"><p>Nothing rendered yet.</p><p class="muted">When a Claude Code or Codex turn ends anywhere in the hub, its response appears here, typeset. Earlier sessions are listed on the left.</p></div>';
      return;
    }
    Promise.all(recent.map(function (s) { return api('/api/event/' + s.last_final).catch(function () { return null; }); }))
      .then(function (events) {
        var frag = document.createDocumentFragment();
        events.forEach(function (e, i) {
          if (!e) return;
          finals.push(e);
          frag.appendChild(responseCard(e, recent[i]));
        });
        feed.innerHTML = '';
        feed.appendChild(frag);
      });
  }

  function responseCard(e, session) {
    var card = el('article', 'card response');
    card.innerHTML = '<header class="card-head">' + badge(e.service) +
      '<a class="card-title" href="#/s/' + esc(e.service) + '/' + esc(e.session_id) + '">' +
      ((session && session.title) ? inlineHtml(session.title) : esc('Session ' + e.session_id.slice(0, 8))) + '</a>' +
      '<span class="muted">' + esc(e.project || 'hub') + ' · ' + esc(when(e.ts)) + '</span></header>';
    card.appendChild(md(e.text, 'turn-' + e.id));
    card.appendChild(toolbar({ text: e.text, title: (session && session.title) || 'Response', event: e }));
    return card;
  }

  // ---------------------------------------------------------------- session
  function showSession(service, sid) {
    var key = service + '/' + sid;
    delete S.unread[key];
    setView('session', [service, sid]);
    S.session = null;
    $('#main').innerHTML = '<div class="page"><div id="sess-head"></div><div id="convo" class="convo"><p class="muted">Loading…</p></div></div>';
    refreshSession(false);
  }

  function refreshSession(live) {
    if (!S.view || S.view.name !== 'session') return;
    var args = S.view.args;
    if (live && Date.now() - S.lastSessionFetch < 1500) return;
    S.lastSessionFetch = Date.now();
    api('/api/session/' + args[0] + '/' + encodeURIComponent(args[1])).then(function (data) {
      if (!S.view || S.view.name !== 'session' || S.view.args[1] !== args[1]) return;
      S.session = data;
      renderSession(data, live);   // it redraws only what changed
    }).catch(function (err) {
      if (!live) $('#convo').innerHTML = '<p class="error">' + esc(err.message) + '</p>';
    });
  }

  function hash(text) {   // FNV-1a: cheap content identity for change detection
    var h = 2166136261;
    for (var i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0).toString(36);
  }

  function itemKey(item) {
    return [item.kind, item.id || '', item.name || '', hash(item.text || ''),
      item.event ? item.event.id + ':' + (item.event.pdf ? item.event.pdf.state : '') : ''].join('|');
  }

  // Render items[from..] as blocks; consecutive tool calls share one folded block.
  function renderItems(items, from, s) {
    var blocks = [], tools = null;
    for (var i = from; i < items.length; i++) {
      var item = items[i];
      if (item.kind === 'tool') {
        if (!tools) { tools = el('details', 'tools'); tools.appendChild(el('summary', '', '')); tools._n = 0; blocks.push({ start: i, node: tools, tools: true }); }
        tools._n += 1;
        var line = el('div', 'tool-line');
        line.innerHTML = '<span class="tool-name">' + esc(item.name || 'tool') + '</span> <span class="tool-gist">' + esc(item.text || '') + '</span>';
        tools.appendChild(line);
        tools.firstChild.textContent = tools._n + (tools._n === 1 ? ' tool call' : ' tool calls');
        continue;
      }
      tools = null;
      if (item.kind === 'notice') { blocks.push({ start: i, node: el('div', 'notice', esc(item.text)) }); continue; }
      var isUser = item.kind === 'user';
      var box = el('article', isUser ? 'msg user' : 'msg assistant' + (item.name === 'commentary' ? ' commentary' : ''));
      box.id = 'item-' + i;
      box.appendChild(el('div', 'msg-meta', (isUser ? 'You' : (SERVICE[s.service] || [s.service])[0]) +
        (item.ts ? ' · ' + esc(when(item.ts)) : '') + (item.name === 'commentary' ? ' · progress note' : '')));
      box.appendChild(md(item.text, item.event ? 'turn-' + item.event.id : 's' + i));
      if (!isUser) box.appendChild(toolbar({ text: item.text, title: s.title || 'Response', event: item.event, session: s }));
      blocks.push({ start: i, node: box });
    }
    return blocks;
  }

  function renderSession(data, live) {
    var s = data.session, st = data.status || {};
    var nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
    var headHtml = '<header class="sess-head">' + badge(s.service) +
      '<div class="sess-titles"><h1 class="page-title">' + (s.title ? inlineHtml(s.title) : 'Untitled session') + '</h1>' +
      '<div class="muted">' + esc(s.project || 'hub root') + ' · started ' + esc(when(s.started)) +
      ' · ' + esc(s.session_id) + '</div></div><span class="spacer"></span>' +
      '<button type="button" class="btn ghost" data-blob="/pdf/session/' + esc(s.service) + '/' + esc(encodeURIComponent(s.session_id)) + '">PDF of session</button>' +
      '</header>' +
      (st.warning ? '<div class="banner warn">' + esc(st.warning) + '</div>' : '') +
      (st.fallback ? '<div class="banner">Showing hook captures only (prompts and final responses); the full transcript is unavailable.</div>' : '');
    var head = $('#sess-head');
    if (head._html !== headHtml) { head.innerHTML = headHtml; head._html = headHtml; }

    // Re-render only from the first changed item, so open panes and selections survive.
    var convo = $('#convo'), keys = data.items.map(itemKey);
    var prev = convo._keys && convo._session === s.service + '/' + s.session_id ? convo._keys : null;
    var blocks = prev ? convo._blocks : [];
    var first = 0;
    if (prev) {
      while (first < keys.length && first < prev.length && keys[first] === prev[first]) first++;
      if (first === keys.length && keys.length === prev.length) return;
    } else convo.innerHTML = '';
    // Keep the blocks that end at or before `first`; a tool group that `first`
    // would extend is redrawn too, so consecutive tool calls stay in one fold.
    var end = function (k) { return k + 1 < blocks.length ? blocks[k + 1].start : (prev ? prev.length : 0); };
    var cut = 0;
    while (cut < blocks.length && end(cut) <= first) cut++;
    if (cut > 0 && cut === blocks.length && blocks[cut - 1].tools && data.items[first] && data.items[first].kind === 'tool') cut--;
    var from = cut < blocks.length ? blocks[cut].start : first;
    blocks.slice(cut).forEach(function (b) { b.node.remove(); });
    if (!cut) convo.innerHTML = '';
    var fresh = renderItems(data.items, from, s);
    fresh.forEach(function (b) { convo.appendChild(b.node); });
    convo._blocks = blocks.slice(0, cut).concat(fresh);
    convo._keys = keys;
    convo._session = s.service + '/' + s.session_id;
    if (!data.items.length) convo.innerHTML = '<p class="muted">No messages yet.</p>';
    if (!live || nearBottom) window.scrollTo(0, document.body.scrollHeight);
    else if (fresh.length) toast('New messages below');
  }

  // ---------------------------------------------------------------- toolbar on a response
  function toolbar(ctx) {
    var bar = el('div', 'msg-tools');
    var e = ctx.event;
    var pdf = '';
    if (e) {
      var state = (e.pdf && e.pdf.state) || 'none';
      pdf = '<button type="button" class="chip" data-blob="/pdf/turn/' + esc(e.id) + '" title="PDF snapshot of this response">' +
        (state === 'ready' ? 'PDF' : state === 'pending' ? 'PDF…' : state === 'failed' ? 'PDF (retry)' : 'PDF') + '</button>' +
        '<a class="chip" href="#/t/' + esc(e.id) + '" title="Permalink to this response">Link</a>';
    }
    bar.innerHTML = pdf + '<button type="button" class="chip" data-act="pin">Pin</button>' +
      '<button type="button" class="chip" data-act="copy">Copy source</button>' +
      '<button type="button" class="chip" data-act="raw">Source</button>';
    bar._ctx = ctx;
    return bar;
  }

  function toolbarAction(btn) {
    var bar = btn.closest('.msg-tools'), ctx = bar._ctx, act = btn.dataset.act;
    if (act === 'copy') return copy(ctx.text, 'Source copied');
    if (act === 'pin') {
      var e = ctx.event, s = ctx.session;
      return pin({
        title: ctx.title, text: ctx.text,
        source: e ? { kind: 'response', service: e.service, session: e.session_id, event: e.id, ts: e.ts, sha256: e.text_sha256 }
          : { kind: 'message', service: s && s.service, session: s && s.session_id }
      });
    }
    if (act === 'raw') {
      var host = bar.parentNode, existing = host.querySelector('pre.raw');
      if (existing) { existing.remove(); btn.classList.remove('on'); return; }
      var pre = el('pre', 'raw'); pre.textContent = ctx.text;
      host.insertBefore(pre, bar); btn.classList.add('on');
    }
  }

  // ---------------------------------------------------------------- single turn
  function showTurn(id) {
    setView('turn', [id]);
    var main = $('#main');
    main.innerHTML = '<div class="page" id="turn"><p class="muted">Loading…</p></div>';
    api('/api/event/' + id).then(function (e) {
      var page = $('#turn'); page.innerHTML = '';
      var s = S.sessions.find(function (x) { return x.session_id === e.session_id; });
      page.appendChild(responseCard(e, s));
    }).catch(function (err) { $('#turn').innerHTML = '<p class="error">' + esc(err.message) + '</p>'; });
  }

  // ---------------------------------------------------------------- records
  function recordNode(r) {
    var card = el('article', 'card record');
    var corr = (r.corrections || []).map(function (c) {
      return '<li><a class="hubid" data-id="' + esc(c.id) + '" href="#/r/' + esc(c.id) + '">' + esc(c.id) + '</a> ' + esc(c.title || '') +
        (c.status ? ' <span class="muted">(' + esc(c.status) + ')</span>' : '') + '</li>';
    }).join('');
    card.innerHTML = '<header class="card-head"><span class="rid">' + esc(r.id) + '</span>' +
      '<span class="muted">' + esc([r.project, r.kind, r.status].filter(Boolean).join(' · ')) + '</span></header>' +
      (corr ? '<div class="banner warn"><strong>Corrections or superseding records</strong><ul>' + corr + '</ul></div>' : '');
    card.appendChild(md(r.body_markdown || '', 'rec-' + r.id, { rewriteLink: docLink(''), breaks: true }));
    var links = el('div', 'msg-tools');
    var paths = (r.paths || []).filter(function (p) { return typeof p === 'string' ? true : p.path; });
    links.innerHTML = paths.slice(0, 12).map(function (p) {
      var path = typeof p === 'string' ? p : p.path;
      return /\.pdf$/i.test(path) ? '' : '<a class="chip" href="#/d/' + esc(path) + '">' + esc(path.split('/').slice(-2).join('/')) + '</a>';
    }).join('') + (r.pdf ? '<button type="button" class="chip" data-blob="/library/' + esc(r.id) + '.pdf">Library PDF</button>' : '') +
      '<button type="button" class="chip" data-act="pin">Pin</button><button type="button" class="chip" data-act="copy">Copy source</button>';
    links._ctx = { text: r.body_markdown || '', title: r.id + ' ' + (r.title || ''), event: null, session: null };
    links._ctx.source = { kind: 'record', id: r.id };
    card.appendChild(links);
    return card;
  }

  function showRecordPage(id) {
    setView('record', [id]);
    var main = $('#main');
    main.innerHTML = '<div class="page" id="rec"><p class="muted">Loading ' + esc(id) + '…</p></div>';
    api('/api/record/' + encodeURIComponent(id)).then(function (r) {
      var page = $('#rec'); page.innerHTML = ''; page.appendChild(recordNode(r));
    }).catch(function (err) { $('#rec').innerHTML = '<p class="error">' + esc(id) + ': ' + esc(err.message) + '</p>'; });
  }

  function openDrawer(id) {
    var drawer = $('#drawer');
    drawer.hidden = false;
    document.body.classList.add('with-drawer');
    var slot = el('div', 'drawer-item');
    slot.innerHTML = '<div class="drawer-bar"><a href="#/r/' + esc(id) + '">' + esc(id) + '</a><span class="spacer"></span>' +
      '<button type="button" class="btn ghost icon" data-close title="Close">×</button></div><p class="muted">Loading…</p>';
    drawer.insertBefore(slot, drawer.firstChild);
    while (drawer.children.length > 6) drawer.lastChild.remove();
    api('/api/record/' + encodeURIComponent(id)).then(function (r) {
      slot.querySelector('p').replaceWith(recordNode(r));
    }).catch(function (err) { slot.querySelector('p').textContent = err.message; });
  }

  function closeDrawerIfRoute() { /* the drawer persists across views until closed */ }

  // ---------------------------------------------------------------- documents
  function docLink(base) {
    return function (href) {
      if (!href || /^(https?:|mailto:|#)/i.test(href)) return null;
      if (/^(javascript|data|file|vbscript|ftp|ssh|tel|blob):/i.test(href)) return false;
      var clean = decodeURIComponent(href.split('#')[0]);
      var parts = (base ? base.split('/').slice(0, -1) : []).concat(clean.split('/'));
      var out = [];
      parts.forEach(function (p) { if (p === '..') out.pop(); else if (p && p !== '.') out.push(p); });
      var path = out.join('/');
      if (/\.(md|txt|tex)$/i.test(path)) return '#/d/' + path;
      if (/\.pdf$/i.test(path)) return '#/pdf/' + path;
      return false;
    };
  }

  function loadDocs() {
    if (S.docs) return Promise.resolve(S.docs);
    return api('/api/docs').then(function (data) {
      S.docs = data.documents || []; S.projects = data.projects || []; renderRail(); return S.docs;
    });
  }

  function showDocs(project) {
    setView('docs', [project]);
    var main = $('#main');
    main.innerHTML = '<div class="page"><h1 class="page-title">Documents</h1><div id="docs"><p class="muted">Loading…</p></div></div>';
    loadDocs().then(function (docs) {
      var groups = {};
      docs.forEach(function (d) {
        if (project && (d.project || 'hub') !== project) return;
        var g = (d.project || 'hub') + ' · ' + (d.kind || 'document');
        (groups[g] = groups[g] || []).push(d);
      });
      var projects = ['', 'hub'].concat(S.projects.map(function (p) { return p.name; }));
      var html = '<div class="seg wide">' + projects.map(function (p) {
        return '<a href="#/docs/' + esc(p) + '" class="' + (p === project ? 'on' : '') + '">' + esc(p || 'All') + '</a>';
      }).join('') + '</div>';
      Object.keys(groups).sort().forEach(function (g) {
        var list = groups[g].sort(function (a, b) { return (b.mtime || 0) - (a.mtime || 0); });
        html += '<h2 class="group">' + esc(g) + ' <span class="muted">(' + list.length + ')</span></h2><ul class="doclist">' +
          list.slice(0, 200).map(function (d) {
            return '<li><a href="#/d/' + esc(d.path) + '">' + esc(d.title || d.path) + '</a> <span class="muted">' + esc(d.path) + '</span></li>';
          }).join('') + '</ul>';
      });
      $('#docs').innerHTML = html;
    }).catch(function (err) { $('#docs').innerHTML = '<p class="error">' + esc(err.message) + '</p>'; });
  }

  function showDoc(path) {
    setView('doc', [path]);
    var main = $('#main');
    main.innerHTML = '<div class="page" id="doc"><p class="muted">Loading ' + esc(path) + '…</p></div>';
    api('/api/doc?path=' + encodeURIComponent(path)).then(function (d) {
      var page = $('#doc'); page.innerHTML = '';
      var head = el('header', 'sess-head', '<div class="sess-titles"><div class="muted">' + esc(d.project || 'hub') + ' · ' + esc(d.path) +
        (d.mtime ? ' · modified ' + esc(when(new Date(d.mtime * 1000).toISOString())) : '') + '</div></div><span class="spacer"></span>' +
        '<button type="button" class="btn ghost" data-blob="/pdf/doc?path=' + esc(encodeURIComponent(d.path)) + '">PDF</button>');
      page.appendChild(head);
      var body = md(d.text, 'doc', { rewriteLink: docLink(d.path) });
      body.classList.add('document');
      var toc = tocNode(body);
      if (toc) page.appendChild(toc);
      page.appendChild(body);
      var tools = toolbar({ text: d.text, title: d.path, event: null, session: null });
      tools._ctx.source = { kind: 'document', path: d.path };
      page.appendChild(tools);
    }).catch(function (err) { $('#doc').innerHTML = '<p class="error">' + esc(path) + ': ' + esc(err.message) + '</p>'; });
  }

  function tocNode(body) {
    var heads = body.querySelectorAll('h2, h3');
    if (heads.length < 4) return null;
    var toc = el('details', 'toc');
    toc.innerHTML = '<summary>Contents (' + heads.length + ')</summary>';
    var list = el('ol');
    heads.forEach(function (h) {
      var li = el('li', h.tagName === 'H3' ? 'sub' : '');
      var a = el('a'); a.textContent = h.textContent; a.href = 'javascript:void 0'; a.dataset.target = h.id;
      li.appendChild(a); list.appendChild(li);
    });
    toc.appendChild(list);
    return toc;
  }

  // ---------------------------------------------------------------- search
  function showSearch(q) {
    setView('search', [q]);
    $('#search').value = q;
    var main = $('#main');
    main.innerHTML = '<div class="page"><h1 class="page-title">Search: ' + esc(q) + '</h1><div id="results"><p class="muted">Searching…</p></div></div>';
    api('/api/search?q=' + encodeURIComponent(q)).then(function (data) {
      var res = data.results || [];
      if (!res.length) { $('#results').innerHTML = '<p class="muted">No matches. An empty search is not a novelty check.</p>'; return; }
      var list = el('ul', 'results');
      res.forEach(function (r) {
        var li = el('li');
        var href = r.type === 'record' || r.id ? '#/r/' + (r.id || '') : '#/d/' + (r.path || '');
        if (r.type === 'doc' && r.path) href = '#/d/' + r.path;
        li.innerHTML = '<a href="' + esc(href) + '" class="res-title">' + esc(r.id ? r.id + ' ' : '') + esc(r.title || r.path || '') + '</a>' +
          ' <span class="muted">' + esc([r.project, r.type, r.path && r.type !== 'doc' ? r.path : ''].filter(Boolean).join(' · ')) + '</span>';
        if (r.snippet) li.appendChild(md(r.snippet, null));
        list.appendChild(li);
      });
      var box = $('#results'); box.innerHTML = ''; box.appendChild(list);
    }).catch(function (err) { $('#results').innerHTML = '<p class="error">' + esc(err.message) + '</p>'; });
  }

  // ---------------------------------------------------------------- notebook
  function refreshPins() {
    return api('/api/pins').then(function (d) { S.pins = d.pins || []; $('#pin-count').textContent = S.pins.length; });
  }

  function pin(item) {
    return api('/api/pins', { action: 'add', item: item }).then(function (d) {
      S.pins = d.pins; $('#pin-count').textContent = S.pins.length; toast('Pinned to the notebook (frozen copy)');
    }).catch(function (err) { toast('Pin failed: ' + err.message); });
  }

  function showNotebook() {
    setView('notebook');
    refreshPins().then(function () {
      var main = $('#main');
      main.innerHTML = '<div class="page"><header class="sess-head"><div class="sess-titles"><h1 class="page-title">Notebook</h1>' +
        '<div class="muted">Pinned excerpts are frozen copies with provenance. Use them to collect an argument and hand it to a reviewer.</div></div><span class="spacer"></span>' +
        '<button type="button" class="btn" id="nb-review" title="Send the frozen packet for an independent read-only review by Codex">Ask ' + esc(reviewer()) + ' to review</button>' +
        '<button type="button" class="btn ghost" id="nb-packet">Copy review packet</button>' +
        '<button type="button" class="btn ghost" data-blob="/pdf/notebook">PDF</button>' +
        '<button type="button" class="btn ghost" id="nb-md">Download .md</button></header><div id="consult"></div><div id="pins"></div></div>';
      consultStatus();
      var box = $('#pins');
      if (!S.pins.length) box.innerHTML = '<p class="muted">Nothing pinned yet. Use “Pin” under any response, record or document.</p>';
      S.pins.forEach(function (p, i) {
        var card = el('article', 'card pin');
        card.dataset.id = p.id;
        card.innerHTML = '<header class="card-head"><span class="rid">' + (i + 1) + '</span><span class="card-title">' + esc(p.title || 'Excerpt') + '</span>' +
          '<span class="muted">' + esc(provenance(p)) + '</span><span class="spacer"></span>' +
          '<button type="button" class="btn ghost icon" data-pin="up" title="Move up">↑</button>' +
          '<button type="button" class="btn ghost icon" data-pin="down" title="Move down">↓</button>' +
          '<button type="button" class="btn ghost icon" data-pin="remove" title="Remove">×</button></header>';
        card.appendChild(md(p.text, 'pin-' + p.id));
        var note = el('textarea', 'pin-note'); note.placeholder = 'Your note or objection (kept separate from the excerpt)';
        note.value = p.note || ''; note.dataset.id = p.id;
        card.appendChild(note);
        box.appendChild(card);
      });
    });
  }

  function provenance(p) {
    var s = p.source || {};
    var bits = [s.kind, s.service, s.session ? 'session ' + String(s.session).slice(0, 8) : '', s.id, s.path, s.ts ? when(s.ts) : '', 'sha256 ' + String(p.sha256 || '').slice(0, 12)];
    return bits.filter(Boolean).join(' · ');
  }

  function notebookMarkdown(packet) {
    var out = [];
    if (packet) {
      out.push('# Review packet', '', 'Assembled in MathDesk on ' + new Date().toISOString() + '. Each excerpt is a frozen copy;',
        'check the stated hypotheses and every step, and report the earliest gap. Notes are the',
        'conductor\'s and are kept separate from the excerpts.', '');
    } else out.push('# MathDesk notebook', '');
    S.pins.forEach(function (p, i) {
      out.push('## ' + (i + 1) + '. ' + (p.title || 'Excerpt'), '', '_Provenance: ' + provenance(p) + '_', '', p.text.trim(), '');
      if (p.note && p.note.trim()) out.push('> Note: ' + p.note.trim().replace(/\n/g, '\n> '), '');
    });
    return out.join('\n');
  }

  function reviewer() { return (S.status && S.status.settings && S.status.settings.reviewer_name) || 'Codex'; }

  function askReview() {
    if (!S.pins.length) { toast('Pin the excerpts to review first'); return; }
    var packet = notebookMarkdown(true);
    var ok = window.confirm('Send ' + S.pins.length + ' frozen excerpt(s) (' + packet.length + ' characters) to ' + reviewer() + ' for an ' +
      'independent read-only review?\n\nIt runs Codex on your ChatGPT subscription and may take several minutes. ' +
      'The review appears under Desk when it finishes.');
    if (!ok) return;
    api('/api/consult', { packet: packet, title: S.pins.length === 1 ? S.pins[0].title : S.pins.length + ' excerpts' })
      .then(function () { toast('Review started'); consultStatus(); })
      .catch(function (err) { toast(err.message); });
  }

  function consultStatus() {
    api('/api/consult').then(function (job) {
      var box = $('#consult'); if (!box) return;
      if (job.state === 'running') {
        box.innerHTML = '<div class="banner">' + esc(reviewer()) + ' is reviewing “' + esc(job.title) + '” (started ' + esc(when(job.started)) +
          '). This page updates when it finishes. <button type="button" class="chip" id="nb-cancel">Cancel</button></div>';
        clearTimeout(consultStatus.timer); consultStatus.timer = setTimeout(consultStatus, 5000);
      } else if (job.state === 'done') {
        box.innerHTML = '<div class="banner">' + esc(reviewer()) + '’s review of “' + esc(job.title) + '” is ready: <a href="#/t/' + esc(job.event) + '">open it</a>.</div>';
      } else if (job.state === 'failed') {
        box.innerHTML = '<div class="banner warn">The review failed: ' + esc(job.error || 'unknown error') + '</div>';
      } else box.innerHTML = '';
    }).catch(function () { /* reviews unavailable */ });
  }

  // ---------------------------------------------------------------- composer
  function toggleComposer(show) {
    var c = $('#composer');
    var open = show === undefined ? c.hidden : show;
    c.hidden = !open;
    document.body.classList.toggle('with-composer', open);
    if (open) { $('#cmp-text').focus(); preview(); }
  }

  function preview() {
    var text = $('#cmp-text').value;
    save('draft', text);
    var box = $('#cmp-preview');
    box.innerHTML = '';
    box.appendChild(md(text || '*Preview*', 'draft'));
  }

  // ---------------------------------------------------------------- LaTeX fallback
  function typesetWithLatex(btn) {
    var box = btn.closest('.m-error');
    btn.disabled = true; btn.textContent = 'Typesetting…';
    api('/api/tex', { tex: box.dataset.body, display: !!box.dataset.display }).then(function (r) {
      if (r.ok) {
        var img = el('img', 'm-svg' + (box.dataset.display ? ' m-svg-display' : ''));
        blobImage(img, r.url); img.alt = box.dataset.body; img.title = 'Typeset by LaTeX (sandboxed); Alt-click copies the source';
        var wrap = el('span', box.dataset.display ? 'm m-display' : 'm'); wrap.dataset.tex = box.querySelector('.m-src').textContent;
        wrap.appendChild(img); box.replaceWith(wrap);
      } else {
        btn.remove();
        box.querySelector('.m-msg').textContent = 'LaTeX: ' + (r.error || 'failed') + (r.log ? '\n' + r.log : '');
      }
    }).catch(function (err) { btn.disabled = false; btn.textContent = 'Typeset with LaTeX'; toast(err.message); });
  }

  // ---------------------------------------------------------------- events
  function wire() {
    window.addEventListener('hashchange', route);
    $('#opt-dollar').checked = S.singleDollar;
    $('#opt-dollar').addEventListener('change', function (e) { S.singleDollar = e.target.checked; save('singleDollar', S.singleDollar); route(); });
    $('#btn-theme').addEventListener('click', function () {
      var cur = document.documentElement.dataset.theme ||
        (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      var next = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next; save('theme', next);
    });
    $('#btn-composer').addEventListener('click', function () { toggleComposer(); });
    $('#cmp-close').addEventListener('click', function () { toggleComposer(false); });
    $('#cmp-text').value = load('draft', '');
    var timer = null;
    $('#cmp-text').addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(preview, 120); });
    $('#cmp-copy').addEventListener('click', function () { copy($('#cmp-text').value, 'Draft copied'); });
    $('#cmp-clear').addEventListener('click', function () { $('#cmp-text').value = ''; preview(); });
    $('#cmp-pin').addEventListener('click', function () {
      var text = $('#cmp-text').value; if (text.trim()) pin({ title: 'Draft', text: text, source: { kind: 'draft' } });
    });
    $('#search-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var q = $('#search').value.trim(); if (!q) return;
      var id = q.toUpperCase();
      if (/^((CP|KC)-)?[RCOQLK]\d{3}$/.test(id)) location.hash = '#/r/' + id;
      else location.hash = '#/search/' + encodeURIComponent(q);
    });
    document.addEventListener('click', function (e) {
      var t = e.target;
      var pdfLink = t.closest('a[href^="#/pdf/"]');
      if (pdfLink) { e.preventDefault(); openBlob('/file?path=' + encodeURIComponent(pdfLink.getAttribute('href').slice(6)), 'PDF'); return; }
      var blob = t.closest('[data-blob]');
      if (blob) { e.preventDefault(); openBlob(blob.dataset.blob, blob.textContent.trim()); return; }
      var filter = t.closest('[data-filter]');
      if (filter) { S.filter = filter.dataset.filter; renderRail(); return; }
      var hub = t.closest('a.hubid');
      if (hub && !e.metaKey && !e.ctrlKey) { e.preventDefault(); openDrawer(hub.dataset.id); return; }
      if (t.closest('[data-close]')) { var item = t.closest('.drawer-item'); item.remove(); if (!$('#drawer').children.length) { $('#drawer').hidden = true; document.body.classList.remove('with-drawer'); } return; }
      var act = t.closest('.msg-tools [data-act]');
      if (act) { var bar = act.closest('.msg-tools'); if (bar._ctx && bar._ctx.source && act.dataset.act === 'pin') { pin({ title: bar._ctx.title, text: bar._ctx.text, source: bar._ctx.source }); return; } toolbarAction(act); return; }
      var latex = t.closest('.m-latex');
      if (latex) { typesetWithLatex(latex); return; }
      var formula = t.closest('.m');
      if (formula && e.altKey) { e.preventDefault(); copy(formula.dataset.tex || '', 'TeX source copied'); return; }
      var tocLink = t.closest('.toc a[data-target]');
      if (tocLink) { e.preventDefault(); var h = document.getElementById(tocLink.dataset.target); if (h) h.scrollIntoView({ behavior: 'smooth' }); return; }
      var pinBtn = t.closest('[data-pin]');
      if (pinBtn) {
        var card = pinBtn.closest('.pin'), id = card.dataset.id, kind = pinBtn.dataset.pin;
        var req = kind === 'remove' ? { action: 'remove', id: id } : { action: 'move', id: id, step: kind === 'up' ? -1 : 1 };
        api('/api/pins', req).then(function () { showNotebook(); });
        return;
      }
      if (t.id === 'nb-packet') { copy(notebookMarkdown(true), 'Review packet copied'); return; }
      if (t.id === 'nb-review') { askReview(); return; }
      if (t.id === 'nb-cancel') { api('/api/consult', { action: 'cancel' }).then(consultStatus); return; }
      if (t.id === 'nb-md') {
        var blob = new Blob([notebookMarkdown(false)], { type: 'text/markdown' });
        var a = el('a'); a.href = URL.createObjectURL(blob); a.download = 'mathdesk-notebook.md'; a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
      }
    });
    document.addEventListener('focusout', function (e) {
      if (e.target.classList && e.target.classList.contains('pin-note')) {
        api('/api/pins', { action: 'note', id: e.target.dataset.id, note: e.target.value }).then(function () { toast('Note saved'); });
      }
    });
    document.addEventListener('keydown', function (e) {
      var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
      if (e.key === 'Escape') { if (!$('#composer').hidden) toggleComposer(false); else $('#search').blur(); return; }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === '/') { e.preventDefault(); $('#search').focus(); $('#search').select(); }
      else if (e.key === 'c') { e.preventDefault(); toggleComposer(); }
      else if (e.key === 'n') { location.hash = '#/notebook'; }
      else if (e.key === 'g') { location.hash = '#/'; }
      else if (e.key === 'j' || e.key === 'k') { stepMessage(e.key === 'j' ? 1 : -1); }
    });
  }

  function stepMessage(dir) {
    var msgs = Array.prototype.slice.call(document.querySelectorAll('.msg.assistant, .card.response, .card.pin'));
    if (!msgs.length) return;
    var y = window.scrollY + 80, idx = -1;
    msgs.forEach(function (m, i) { if (m.offsetTop <= y) idx = i; });
    var next = msgs[Math.max(0, Math.min(msgs.length - 1, idx + dir))];
    window.scrollTo({ top: next.offsetTop - 64, behavior: 'smooth' });
  }

  // ---------------------------------------------------------------- print mode
  function printView() {
    document.body.classList.add('print');
    var parts = location.pathname.split('/').slice(2);
    var main = $('#main');
    var done = function () {
      Promise.all(S.cachedLookups).then(settle);
    };
    var settle = function () {
      var imgs = Array.prototype.slice.call(document.images);
      Promise.all(imgs.map(function (i) { return i.complete ? 0 : new Promise(function (r) { i.onload = i.onerror = r; }); }))
        .then(function () { return document.fonts ? document.fonts.ready : 0; })
        .then(function () { setTimeout(function () { document.title = 'READY'; }, 50); });
    };
    var header = function (title, meta) {
      return el('header', 'print-head', '<div class="print-title">' + esc(title) + '</div><div class="muted">' + esc(meta) + '</div>');
    };
    // A failed print page must never become a PDF: say so to the worker and the server.
    var fail = function (err) {
      main.textContent = 'Could not render: ' + (err && err.message || err);
      api('/api/print-failed').catch(function () { /* worker still sees the title */ })
        .then(function () { document.title = 'FAILED'; });
    };
    window.addEventListener('error', function (ev) { fail(ev.error || ev.message); });
    if (parts[0] === 'turn') {
      api('/api/event/' + parts[1]).then(function (e) {
        main.appendChild(header('Response', [SERVICE[e.service] ? SERVICE[e.service][0] : e.service, e.project || 'hub', when(e.ts), 'session ' + e.session_id, 'sha256 ' + e.text_sha256.slice(0, 16)].join(' · ')));
        main.appendChild(md(e.text, 'turn-' + e.id));
        done();
      }).catch(fail);
    } else if (parts[0] === 'session') {
      api('/api/session/' + parts[1] + '/' + parts.slice(2).join('/')).then(function (data) {
        var s = data.session;
        main.appendChild(header(s.title || 'Session', [s.service, s.project || 'hub', 'started ' + when(s.started), s.session_id].join(' · ')));
        data.items.forEach(function (item) {
          if (item.kind === 'tool') return;
          if (item.kind === 'notice') { main.appendChild(el('div', 'notice', esc(item.text))); return; }
          var box = el('article', 'msg ' + item.kind);
          box.appendChild(el('div', 'msg-meta', item.kind === 'user' ? 'You' : (SERVICE[s.service] || [s.service])[0]));
          box.appendChild(md(item.text, null));
          main.appendChild(box);
        });
        done();
      }).catch(fail);
    } else if (parts[0] === 'doc') {
      var path = new URLSearchParams(location.search).get('path');
      api('/api/doc?path=' + encodeURIComponent(path)).then(function (d) {
        main.appendChild(header(d.path, (d.project || 'hub') + ' · printed ' + new Date().toISOString().slice(0, 16) + 'Z'));
        main.appendChild(md(d.text, 'doc'));
        done();
      }).catch(fail);
    } else if (parts[0] === 'notebook') {
      api('/api/pins').then(function (d) {
        S.pins = d.pins || [];
        main.appendChild(header('Notebook', S.pins.length + ' excerpts · ' + new Date().toISOString().slice(0, 16) + 'Z'));
        S.pins.forEach(function (p, i) {
          var box = el('article', 'pin-print');
          box.appendChild(el('h2', '', esc((i + 1) + '. ' + (p.title || 'Excerpt'))));
          box.appendChild(el('div', 'muted', esc(provenance(p))));
          box.appendChild(md(p.text, null));
          if (p.note) box.appendChild(el('blockquote', 'note', esc(p.note)));
          main.appendChild(box);
        });
        done();
      }).catch(fail);
    } else fail(new Error('unknown print view'));
  }

  boot();
})();
