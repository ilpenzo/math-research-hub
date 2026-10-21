/* Split Markdown+TeX source into Markdown with placeholders plus a list of
 * formulas, before Markdown can consume backslashes or underscores.
 * Pure function, shared by the page and the Node tests. */
(function (root) {
  'use strict';

  var OPEN = '', CLOSE = '';
  var ENVS = ['equation', 'equation*', 'align', 'align*', 'alignat', 'alignat*',
    'gather', 'gather*', 'multline', 'multline*', 'flalign', 'flalign*',
    'eqnarray', 'eqnarray*', 'CD', 'displaymath', 'tikzcd', 'xy'];
  var MAX_FORMULA = 20000;

  function randomNonce() {
    var chars = 'abcdefghijklmnopqrstuvwxyz0123456789', out = '';
    for (var i = 0; i < 8; i++) out += chars[Math.floor(Math.random() * chars.length)];
    return out;
  }

  function escapedAt(src, i) {
    // True when src[i] is preceded by an odd number of backslashes.
    var n = 0;
    for (var j = i - 1; j >= 0 && src[j] === '\\'; j--) n++;
    return n % 2 === 1;
  }

  function findUnescaped(src, needle, from, stopAtBlankLine) {
    var limit = Math.min(src.length, from + MAX_FORMULA);
    for (var i = from; i < limit; i++) {
      if (src[i] === '`') return -1;   // never cross into a code span
      if (stopAtBlankLine && src[i] === '\n' && /^\n[ \t]*\n/.test(src.slice(i, i + 64))) return -1;
      if (src.startsWith(needle, i) && !escapedAt(src, i)) return i;
    }
    return -1;
  }

  function fenceAt(src, i) {
    if (i > 0 && src[i - 1] !== '\n') return null;
    var m = /^( {0,3})(`{3,}|~{3,})([^\n`]*)(\n|$)/.exec(src.slice(i, i + 400));
    if (!m) return null;
    var marker = m[2], info = m[3].trim().toLowerCase();
    var body = i + m[0].length;
    var close = new RegExp('(^|\\n) {0,3}' + marker[0] + '{' + marker.length + ',}[ \\t]*(\\n|$)', 'g');
    close.lastIndex = body - 1 >= i ? body - 1 : body;
    var c = close.exec(src);
    while (c && c.index + c[1].length < body) c = close.exec(src);
    var end = c ? c.index + c[0].length : src.length;
    var inner = c ? src.slice(body, c.index + c[1].length) : src.slice(body);
    return { end: end, info: info, inner: inner.replace(/\n$/, '') };
  }

  function codeSpanAt(src, i) {
    var m = /^`+/.exec(src.slice(i, i + 32));
    var run = m[0];
    var j = i + run.length;
    while (true) {
      var k = src.indexOf(run, j);
      if (k < 0) return null;
      if (src[k + run.length] !== '`' && src[k - 1] !== '`') return k + run.length;
      j = k + 1;
      while (src[j] === '`') j++;
    }
  }

  function envAt(src, i) {
    var m = /^\\begin\{([A-Za-z]+\*?)\}/.exec(src.slice(i, i + 40));
    if (!m || ENVS.indexOf(m[1]) < 0) return null;
    var name = m[1], open = '\\begin{' + name + '}', close = '\\end{' + name + '}';
    var depth = 1, j = i + m[0].length, limit = Math.min(src.length, i + MAX_FORMULA);
    while (j < limit) {
      var a = src.indexOf(open, j), b = src.indexOf(close, j);
      if (b < 0) return null;
      if (src.slice(j, b).indexOf('`') >= 0) return null;   // never cross into a code span
      if (a >= 0 && a < b) { depth++; j = a + open.length; continue; }
      depth--; j = b + close.length;
      if (depth === 0) return { end: j, env: name };
    }
    return null;
  }

  /* options.singleDollar (default true): Pandoc rules for $..$.
   * Returns { text, math: [{ tex, display, env, raw }], nonce }. Placeholders are
   * OPEN + nonce + ':' + index + CLOSE; the random nonce means text that merely
   * decodes to the sentinel characters (e.g. HTML entities) never matches. */
  function splitMath(source, options) {
    var singleDollar = !options || options.singleDollar !== false;
    var nonce = (options && options.nonce) || randomNonce();
    var src = String(source || '').replace(/\r\n?/g, '\n').replace(/[]/g, '');
    var out = [], math = [], i = 0, n = src.length;

    function push(tex, display, raw, env) {
      // A formula opened on a quoted line ("> $$") carries the quote markers on
      // its continuation lines; they belong to Markdown, not to TeX.
      var lineStart = src.lastIndexOf('\n', i - 1) + 1;
      if (tex.indexOf('\n') >= 0 && /^[ \t]*>/.test(src.slice(lineStart, i))) {
        tex = tex.replace(/\n[ \t]*(?:>[ \t]?)+/g, '\n');
      }
      out.push(OPEN + nonce + ':' + math.length + CLOSE);
      math.push({ tex: tex, display: display, raw: raw, env: env || null });
    }

    while (i < n) {
      var ch = src[i];
      if ((ch === '`' || ch === '~') && (i === 0 || src[i - 1] === '\n' || /^ {1,3}$/.test(src.slice(src.lastIndexOf('\n', i - 1) + 1, i)))) {
        var lineStart = src.lastIndexOf('\n', i - 1) + 1;
        var fence = fenceAt(src, lineStart);
        if (fence) {
          if (fence.info === 'math') {
            push(fence.inner, true, src.slice(i, fence.end), null);
            out.push('\n');
          } else {
            out.push(src.slice(i, fence.end));
          }
          i = fence.end;
          continue;
        }
      }
      if (ch === '`') {
        var spanEnd = codeSpanAt(src, i);
        if (spanEnd) { out.push(src.slice(i, spanEnd)); i = spanEnd; continue; }
        var run = /^`+/.exec(src.slice(i, i + 32))[0];
        out.push(run); i += run.length; continue;
      }
      if (ch === '\\') {
        var next = src[i + 1];
        if (next === '\\' || next === '$') { out.push(src.slice(i, i + 2)); i += 2; continue; }
        if (next === '[' || next === '(') {
          var closer = next === '[' ? '\\]' : '\\)';
          var k = findUnescaped(src, closer, i + 2, true);
          if (k >= 0 && src.slice(i + 2, k).trim()) {
            push(src.slice(i + 2, k), next === '[', src.slice(i, k + 2));
            i = k + 2; continue;
          }
        }
        if (src.startsWith('\\begin{', i)) {
          var env = envAt(src, i);
          if (env) { push(src.slice(i, env.end), true, src.slice(i, env.end), env.env); i = env.end; continue; }
        }
        out.push(ch); i++; continue;
      }
      if (ch === '$') {
        if (src[i + 1] === '$') {
          var d = findUnescaped(src, '$$', i + 2, false);
          if (d >= 0 && src.slice(i + 2, d).trim()) {
            push(src.slice(i + 2, d), true, src.slice(i, d + 2));
            i = d + 2; continue;
          }
          out.push('$$'); i += 2; continue;
        }
        if (singleDollar && src[i + 1] && !/\s/.test(src[i + 1])) {
          var j = i + 1, limit = Math.min(n, i + 2000), found = -1, depth = 0;
          while (j < limit) {
            if (src[j] === '`') break;
            if ((src[j] === '{' || src[j] === '}') && !escapedAt(src, j)) {
              depth += src[j] === '{' ? 1 : -1;
              if (depth < 0) break;
              j++; continue;
            }
            if (depth > 0) { j++; continue; }   // e.g. $\{p : \text{$p$ prime}\}$
            if (src[j] === '\n' && /^\n[ \t]*\n/.test(src.slice(j, j + 64))) break;
            if (src[j] === '$' && !escapedAt(src, j)) {
              if (src[j + 1] === '$') break;
              if (!/\s/.test(src[j - 1]) && !/[0-9]/.test(src[j + 1] || '')) { found = j; break; }
              break;
            }
            j++;
          }
          if (found > i + 1) {
            push(src.slice(i + 1, found), false, src.slice(i, found + 1));
            i = found + 1; continue;
          }
        }
        out.push('$'); i++; continue;
      }
      // Copy plain runs quickly.
      var stop = i + 1;
      while (stop < n && '`~\\$'.indexOf(src[stop]) < 0) stop++;
      out.push(src.slice(i, stop));
      i = stop;
    }
    return { text: out.join(''), math: math, nonce: nonce };
  }

  var api = { splitMath: splitMath, OPEN: OPEN, CLOSE: CLOSE, ENVS: ENVS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.MathSplit = api;
})(this);
