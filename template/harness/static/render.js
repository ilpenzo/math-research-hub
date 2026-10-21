/* Markdown + TeX rendering: split math, marked, DOMPurify, KaTeX on text nodes,
 * then hub-ID links. Depends on MathSplit, marked, DOMPurify and katex. */
(function (root) {
  'use strict';

  var baseMacros = {};
  var idPattern = null, knownIds = null;

  marked.use({
    gfm: true,
    renderer: {
      // Model output is untrusted: raw HTML is shown as text, never parsed.
      html: function (token) { return escapeHtml(token.raw || token.text || ''); }
    }
  });

  DOMPurify.addHook('afterSanitizeAttributes', function (node) {
    if (node.tagName === 'A') {
      var href = node.getAttribute('href') || '';
      if (/^https?:/i.test(href)) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    }
  });

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function configure(options) {
    if (options.macros) baseMacros = options.macros;
    if (options.idRegex) {
      try { idPattern = new RegExp(options.idRegex, 'g'); } catch (e) { idPattern = null; }
    }
    if (options.ids) knownIds = new Set(options.ids);
  }

  function prepareTex(tex, macros) {
    // Environments and commands KaTeX lacks, mapped to their closest supported form.
    tex = tex.replace(/\\(begin|end)\{multline\*?\}/g, '\\$1{gather*}')
      .replace(/\\(begin|end)\{flalign(\*?)\}/g, '\\$1{align$2}')
      .replace(/\\begin\{eqnarray\*?\}/g, '\\begin{array}{rcl}')
      .replace(/\\end\{eqnarray\*?\}/g, '\\end{array}')
      .replace(/\\label\s*\{[^{}]*\}/g, '')
      .replace(/\\eqref\s*\{([^{}]*)\}/g, '\\text{($1)}')
      .replace(/\\ref\s*\{([^{}]*)\}/g, '\\text{$1}')
      .replace(/\\intertext\s*\{([^{}]*)\}/g, '\\text{$1}\\\\');
    // \DeclareMathOperator is preamble-only in LaTeX; honour it inside a response.
    return tex.replace(/\\DeclareMathOperator(\*?)\s*\{\s*(\\[A-Za-z]+)\s*\}\s*\{([^{}]*)\}/g,
      function (_, star, name, body) {
        macros[name] = '\\operatorname' + (star ? '*' : '') + '{' + body + '}';
        return '';
      });
  }

  function formulaNode(m, macros) {
    var tex = prepareTex(m.tex, macros);
    var span = document.createElement('span');
    span.className = m.display ? 'm m-display' : 'm';
    span.dataset.tex = m.raw;
    if (!tex.trim()) { span.className += ' m-defs'; return span; }
    try {
      span.innerHTML = katex.renderToString(tex, {
        displayMode: m.display, throwOnError: true, trust: false, strict: 'ignore',
        maxExpand: 1000, maxSize: 30, macros: macros, globalGroup: true,
        output: 'htmlAndMathml'
      });
    } catch (err) {
      span.className = 'm-error' + (m.display ? ' m-error-display' : '');
      var diagram = /^\\begin\{(tikzcd|xy)\}/.test(tex.trim());
      var msg = diagram ? 'The browser cannot draw this diagram.' :
        String(err && err.message || err).replace(/^KaTeX parse error:\s*/, '');
      span.innerHTML = '<code class="m-src"></code><span class="m-msg"></span>' +
        '<button type="button" class="m-latex" title="Compile this formula with LaTeX in a sandbox">Typeset with LaTeX</button>';
      span.querySelector('.m-src').textContent = m.raw;
      span.querySelector('.m-msg').textContent = msg;
      span.dataset.display = m.display ? '1' : '';
      span.dataset.body = tex;
    }
    return span;
  }

  function spliceMath(container, math, macros, nonce) {
    var PH = new RegExp('' + nonce + ':(\\d+)', 'g');
    var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    var hits = [], node;
    while ((node = walker.nextNode())) if (node.nodeValue.indexOf('' + nonce) >= 0) hits.push(node);
    hits.forEach(function (text) {
      var parts = text.nodeValue.split(PH);
      var frag = document.createDocumentFragment();
      for (var i = 0; i < parts.length; i++) {
        if (i % 2 === 0) { if (parts[i]) frag.appendChild(document.createTextNode(parts[i])); }
        else {
          var inCode = text.parentNode && text.parentNode.closest && text.parentNode.closest('code, pre');
          var m = math[+parts[i]];
          if (!m) continue;
          frag.appendChild(inCode ? document.createTextNode(m.raw) : formulaNode(m, macros));
        }
      }
      text.parentNode.replaceChild(frag, text);
    });
    // A placeholder that landed in an attribute (e.g. a link target) gets its source back.
    container.querySelectorAll('*').forEach(function (el) {
      for (var a = 0; a < el.attributes.length; a++) {
        var attr = el.attributes[a];
        if (attr.value.indexOf('' + nonce) >= 0) {
          el.setAttribute(attr.name, attr.value.replace(PH, function (all, k) { return math[+k] ? math[+k].raw : ''; }));
        }
      }
    });
    // A display formula alone in a paragraph becomes a block of its own.
    container.querySelectorAll('p').forEach(function (p) {
      if (p.childNodes.length === 1 && p.firstChild.classList && p.firstChild.classList.contains('m-display')) {
        p.classList.add('p-display');
      }
    });
  }

  function linkIds(container) {
    if (!idPattern || !knownIds) return;
    var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        return n.parentNode.closest('a, code, pre, .m, .m-error, .katex') ?
          NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      }
    });
    var hits = [], node;
    while ((node = walker.nextNode())) { idPattern.lastIndex = 0; if (idPattern.test(node.nodeValue)) hits.push(node); }
    hits.forEach(function (text) {
      var value = text.nodeValue, frag = document.createDocumentFragment(), last = 0, m;
      var prevPrefix = null, prevEnd = -1;
      idPattern.lastIndex = 0;
      while ((m = idPattern.exec(value))) {
        // Hub notes abbreviate "KC-R008/R009": the bare second ID inherits the prefix.
        var prefix = (/^([A-Z][A-Z0-9]*-)/.exec(m[0]) || [])[1] || null;
        var id = m[0];
        if (!prefix && prevPrefix && value.slice(prevEnd, m.index) === '/' && knownIds.has(prevPrefix + id)) {
          id = prevPrefix + id; prefix = prevPrefix;
        }
        prevPrefix = prefix; prevEnd = m.index + m[0].length;
        if (!knownIds.has(id)) continue;
        frag.appendChild(document.createTextNode(value.slice(last, m.index)));
        var a = document.createElement('a');
        a.className = 'hubid'; a.href = '#/r/' + id; a.dataset.id = id; a.textContent = m[0];
        if (id !== m[0]) a.title = id;
        frag.appendChild(a);
        last = m.index + m[0].length;
      }
      if (last === 0) return;
      frag.appendChild(document.createTextNode(value.slice(last)));
      text.parentNode.replaceChild(frag, text);
    });
  }

  function slug(text) {
    return text.toLowerCase().replace(/[^\w\s-]/g, '').trim().replace(/\s+/g, '-').slice(0, 80);
  }

  /* Render source into a new element. options: singleDollar, breaks, idPrefix (for
   * stable passage anchors), rewriteLink(href) -> href|null. */
  function render(source, options) {
    options = options || {};
    var split = MathSplit.splitMath(source, { singleDollar: options.singleDollar !== false });
    var html = DOMPurify.sanitize(marked.parse(split.text, { breaks: !!options.breaks }), {
      // No images or media from model output: they would make requests on their own.
      FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'iframe', 'object', 'embed',
        'img', 'video', 'audio', 'source', 'picture', 'svg', 'math', 'link', 'meta'],
      FORBID_ATTR: ['style']
    });
    var el = document.createElement('div');
    el.className = 'md';
    el.innerHTML = html;
    var macros = Object.assign({}, baseMacros);
    spliceMath(el, split.math, macros, split.nonce);
    linkIds(el);
    var used = {};
    el.querySelectorAll('h1, h2, h3, h4').forEach(function (h) {
      var s = slug(h.textContent) || 'section';
      while (used[s]) s += '-';
      used[s] = true;
      h.id = (options.idPrefix ? options.idPrefix + '-' : '') + s;
    });
    if (options.rewriteLink) {
      el.querySelectorAll('a[href]').forEach(function (a) {
        if (a.classList.contains('hubid')) return;
        var next = options.rewriteLink(a.getAttribute('href'));
        if (next === false) {   // a local file the desk cannot show: keep the text, drop the link
          a.removeAttribute('href'); a.classList.add('inert'); a.title = 'Not viewable in MathDesk';
        } else if (next) a.setAttribute('href', next);
      });
    }
    if (options.idPrefix) {
      var n = 0;
      Array.prototype.forEach.call(el.children, function (block) {
        n += 1;
        block.dataset.passage = options.idPrefix + '#p' + n;
      });
    }
    el.dataset.formulas = String(split.math.length);
    el.dataset.errors = String(el.querySelectorAll('.m-error').length);
    return el;
  }

  root.Render = { configure: configure, render: render, escapeHtml: escapeHtml };
})(this);
