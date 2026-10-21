// node harness/tests/mathsplit.test.js
const assert = require('assert');
const { splitMath } = require('../static/mathsplit.js');
const tex = (s, o) => splitMath(s, o).math.map(m => [m.tex, m.display]);
const text = (s, o) => splitMath(s, o).text.replace(/[a-z0-9]+:(\d+)/g, '<M$1>');
let passed = 0;
function t(name, fn) { fn(); passed++; }

t('inline dollars', () => assert.deepStrictEqual(tex('Let $H^1(X,\\mathcal O_X)=0$ hold.'), [['H^1(X,\\mathcal O_X)=0', false]]));
t('currency is not math', () => assert.deepStrictEqual(tex('It costs $5 and $10 today.'), []));
t('closing dollar before digit', () => assert.deepStrictEqual(tex('$x$1 and $y$'), [['y', false]]));
t('space after opening', () => assert.deepStrictEqual(tex('a $ b$ c'), []));
t('display dollars multi-line', () => assert.deepStrictEqual(tex('$$\na_n\n= b_n\n$$'), [['\na_n\n= b_n\n', true]]));
t('backslash delimiters', () => assert.deepStrictEqual(tex('\\(x_1\\) and \\[\\sum_i a_i\\]'), [['x_1', false], ['\\sum_i a_i', true]]));
t('set difference backslash is not a delimiter', () => assert.deepStrictEqual(tex('open W=X\\(C34 disjoint-union C37)\n\nnext para \\) end'), []));
t('escaped dollar', () => assert.deepStrictEqual(tex('price \\$5 and \\$6'), []));
t('code span protects', () => { assert.deepStrictEqual(tex('use `$x$` literally, but $y$'), [['y', false]]); assert.ok(text('use `$x$` here').includes('`$x$`')); });
t('double backtick span', () => assert.deepStrictEqual(tex('``a ` $x$ ``'), []));
t('fence protects', () => assert.deepStrictEqual(tex('```tex\n$x$ \\[y\\]\n```\nthen $z$'), [['z', false]]));
t('tilde fence', () => assert.deepStrictEqual(tex('~~~\n$x$\n~~~'), []));
t('math fence', () => assert.deepStrictEqual(tex('```math\n\\int f\n```'), [['\\int f', true]]));
t('align env', () => { const m = splitMath('Then\n\\begin{align*}\na&=b\\\\\nc&=d\n\\end{align*}\nok').math; assert.strictEqual(m.length, 1); assert.strictEqual(m[0].env, 'align*'); assert.ok(m[0].tex.startsWith('\\begin{align*}')); });
t('nested env', () => assert.strictEqual(splitMath('\\begin{CD} A @>>> B \\end{CD}').math[0].env, 'CD'));
t('non-math env untouched', () => assert.deepStrictEqual(tex('\\begin{proof} $x$ \\end{proof}'), [['x', false]]));
t('underscores protected from markdown', () => assert.strictEqual(text('$a_1 * b_2$ and *em*'), '<M0> and *em*'));
t('blank line ends inline', () => assert.deepStrictEqual(tex('$a\n\nb$'), []));
t('single dollar toggle off', () => assert.deepStrictEqual(tex('$x$ and $$y$$', { singleDollar: false }), [['y', true]]));
t('escaped backslash then paren', () => assert.deepStrictEqual(tex('a \\\\(b\\) c'), []));
t('placeholder chars stripped from input', () => assert.strictEqual(splitMath('0').math.length, 0));
t('table with math pipes', () => assert.deepStrictEqual(tex('| a | $|x|$ |\n|---|---|'), [['|x|', false]]));
t('indented fence', () => assert.deepStrictEqual(tex('  ```\n  $x$\n  ```'), []));
t('unclosed display is literal', () => assert.deepStrictEqual(tex('$$ a'), []));
t('dollar after word', () => assert.deepStrictEqual(tex('US$ value'), []));
t('dollar scan does not cross a code span (review finding)', () => {
  const r = splitMath('The price is $5. Use `$x$` for math.');
  assert.deepStrictEqual(r.math, []);
  assert.ok(r.text.includes('`$x$`'));
});
t('placeholders carry a nonce', () => {
  const r = splitMath('$a$ and $b$', { nonce: 'n0nce' });
  assert.strictEqual(r.text, 'n0nce:0 and n0nce:1');
  assert.notStrictEqual(splitMath('$a$').nonce, splitMath('$a$').nonce);
});
t('backslash delimiters stop at code', () => assert.deepStrictEqual(tex('\\( a `x` \\)'), []));
t('blockquote display math drops quote markers', () => {
  const m = splitMath('> $$\n> H^i(\\mathbb P^n,E(k))=0\n> $$').math;
  assert.strictEqual(m.length, 1);
  assert.ok(!m[0].tex.includes('>'), m[0].tex);
});
t('dollar inside \\text does not close inline math', () => assert.deepStrictEqual(
  tex('$S=\\{p : \\text{$p$ prime}\\}$ ok'), [['S=\\{p : \\text{$p$ prime}\\}', false]]));
t('environment does not cross code (review finding)', () => assert.strictEqual(
  splitMath('Use \\begin{align} then `\\end{align}` to close.').math.length, 0));
console.log(`mathsplit: ${passed} passed`);
