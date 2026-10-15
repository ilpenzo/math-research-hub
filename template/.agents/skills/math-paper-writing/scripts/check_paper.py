#!/usr/bin/env python3
"""Read-only checks for a LaTeX mathematics paper (math-paper-writing skill).

Errors make the exit status 1. They are:
  undefined or multiply defined references and citations in the build log,
  TeX errors, cite keys without a bibliography entry, references to
  undefined labels, duplicate labels, TODO/[VERIFY]/FIXME/XXX markers, hub
  record IDs, and model meta-comments in the text.
With --release, markers and hub IDs inside TeX comments are errors too, as
are a missing build log and an abstract longer than arXiv's 1920 characters.
Warnings are style and calibration issues to fix or justify in the NOTE.
The script never writes files. Python 3.9+, standard library only.
"""
import argparse
import re
import sys
from pathlib import Path

COMMENT = re.compile(r'(?<!\\)%.*$')
INPUT = re.compile(r'\\(?:input|include)\{([^}]+)\}')
MARKER = re.compile(r'\[VERIFY\b|\bTODO\b|\bFIXME\b|\bXXX\b')
HUB_ID = re.compile(r'(?<![\w-])(?:[A-Z]{2,5}-)?[A-Z]\d{3}(?!\w)')
META = re.compile(r'\b(?:As an AI|as a language model|Here is (?:the|a|an) (?:revised|updated|rewritten)|'
                  r'I hope this helps|Let me know if)\b', re.I)
HUB_WORDS = re.compile(r'\b(?:NOTE\.md|proof swarm|fingerprint|missing implication|'
                       r'proof conductor|integration owner|hub\.py)\b', re.I)
MODEL_NAMES = re.compile(r'\b(?:Claude|Codex|ChatGPT|GPT-\d\w*|LLM)\b')
CITE = re.compile(r'\\(?:cite|citet|citep|parencite|textcite)\*?((?:\[[^\]]*\]){0,2})\{([^}]+)\}')
LABEL = re.compile(r'\\label\{([^}]+)\}')
REF = re.compile(r'\\(?:ref|eqref|cref|Cref|autoref|pageref)\{([^}]+)\}')
BIBITEM = re.compile(r'\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}')
BIBFILES = re.compile(r'\\(?:bibliography|addbibresource)\{([^}]+)\}')
BIBKEY = re.compile(r'@\w+\s*\{\s*([^,\s]+)\s*,')
NEWTHEOREM = re.compile(r'\\newtheorem\{(\w+)\}(\[\w+\])?\{[^}]*\}')
STYLE = [
    (re.compile(r'\b(?:clearly|obviously|trivially|evidently)\b', re.I),
     'give the reason in a few words or drop the adverb [Poonen 26]'),
    (re.compile(r'\bit is (?:easy|straightforward) to (?:see|check|verify)\b', re.I),
     'give the reason instead [Poonen 26]'),
    (re.compile(r'(?<!\\)\b(?:iff|WLOG|w\.l\.o\.g\.|s\.t\.)(?!\w)'), 'blackboard abbreviation [Poonen 30]'),
    (re.compile(r'\b(?:Note that|We remark that|It is worth noting|Notably|Crucially|Importantly|delve|leverage)\b'),
     'filler; omit unless needed to avoid starting with a symbol [Poonen 56]'),
    (re.compile(r"\b(?:do|does|did|is|are|was|were|can|could|would|should|has|have|had|wo)n't\b", re.I),
     'contraction [Poonen 33]'),
    (re.compile(r'\b(?:Theorem|Lemma|Proposition|Corollary|Definition|Example|Remark|Section|Appendix|'
                r'Conjecture|Question|Figure|Table)s? +\\(?:ref|eqref)\{'), 'use a tie: Theorem~\\ref'),
    (re.compile(r'\b(?:theorem|lemma|proposition|corollary|section|appendix)~?\s*\\ref\{'),
     'capitalize a numbered reference [Poonen 49]'),
    (re.compile(r'(?<![\\\w])[A-Za-z]\s*:\s*[^$:=\n]{0,40}?\\(?:to|rightarrow|longrightarrow)\b'),
     'use f\\colon X\\to Y [Poonen 39]'),
    (re.compile(r':='), 'use \\coloneqq [Poonen 42]'),
    (re.compile(r'\\(?:forall|exists|implies|Rightarrow|Longrightarrow)\b'),
     'logical symbol; write it in words unless in a formula about logic [Poonen 31]'),
]
COLON_DISPLAY = re.compile(r':\s*(?:\\\[|\\begin\{(?:equation|align|gather|multline|eqnarray)\*?\})')
SYMBOL_START = re.compile(r'(\S+)[.!?]\s+\$')
ABBREV = {'e.g', 'i.e', 'cf', 'resp', 'etc', 'vs', 'Thm', 'Prop', 'Lem', 'Cor', 'Sec', 'Fig', 'Eq', 'Ch'}
CONCLUSION = re.compile(r'\\section\*?\{\s*(?:Conclusions?|Concluding remarks|Summary)\s*\}', re.I)
WEAK_TITLE = re.compile(r'^\s*(?:A note on|Remarks on|On some)\b', re.I)
LOG_UNDEF = re.compile(r"(?:Reference|Citation) [`']([^']+)' (?:on page \d+ )?undefined")
OVERFULL = re.compile(r'Overfull \\hbox \(([\d.]+)pt too wide\)')


class Report:
    def __init__(self):
        self.errors, self.warnings, self.info = [], [], []

    def add(self, level, where, message):
        getattr(self, level).append(f'{where}: {message}' if where else message)


def load(path, seen=None):
    """Return [(path, lines)] for the main file and its \\input files."""
    seen = seen if seen is not None else set()
    path = path.resolve()
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    raw = path.read_text(encoding='utf-8', errors='replace').splitlines()
    files = [(path, raw)]
    for line in raw:
        for name in INPUT.findall(COMMENT.sub('', line)):
            child = path.parent / name
            if child.suffix != '.tex':
                child = child.with_name(child.name + '.tex')
            files += load(child, seen)
    return files


def braced(text, start):
    """Contents of the brace group opening at or after start."""
    i = text.find('{', start)
    if i < 0:
        return ''
    depth = 0
    for j in range(i, len(text)):
        depth += {'{': 1, '}': -1}.get(text[j], 0)
        if depth == 0:
            return text[i + 1:j]
    return text[i + 1:]


def plain(tex):
    tex = re.sub(r'\\[a-zA-Z]+\*?', ' ', tex)
    return re.sub(r'[{}$\\~]', ' ', tex)


def check(main, log=None, release=False, overfull_pt=1.0, verbose=False):
    rep = Report()
    files = load(Path(main))
    if not files:
        rep.add('errors', None, f'cannot read {main}')
        return rep
    root = files[0][0].parent
    labels, refs, cites, bibkeys = {}, set(), {}, set()
    no_locator, theorem_counters, renumbered = [], [], set()
    texts = {}
    for path, lines in files:
        name = path.name
        stripped = [COMMENT.sub('', line) for line in lines]
        text = '\n'.join(stripped)
        texts[path] = text
        body_start = text.find('\\begin{document}') if path == files[0][0] else 0
        for n, (raw, line) in enumerate(zip(lines, stripped), 1):
            where = f'{name}:{n}'
            comment = raw[len(line):]
            for level, part, tag in (('errors', line, ''),
                                     ('errors' if release else 'warnings', comment, ' (in a comment)')):
                for m in MARKER.finditer(part):
                    rep.add(level, where, f'unresolved marker {m.group(0)!r}{tag}')
                for m in HUB_ID.finditer(part):
                    rep.add(level, where, f'hub record ID {m.group(0)!r}{tag}; keep hub vocabulary out')
            for m in META.finditer(line):
                rep.add('errors', where, f'model meta-comment {m.group(0)!r}')
            for m in HUB_WORDS.finditer(line):
                rep.add('warnings', where, f'hub vocabulary {m.group(0)!r}')
            for m in MODEL_NAMES.finditer(line):
                rep.add('warnings', where, f'model name {m.group(0)!r}: only in a user-approved AI statement')
            for m in LABEL.finditer(line):
                if m.group(1) in labels:
                    rep.add('errors', where, f'duplicate label {m.group(1)!r} (first at {labels[m.group(1)]})')
                labels.setdefault(m.group(1), where)
            for m in REF.finditer(line):
                refs.update(k.strip() for k in m.group(1).split(','))
            for m in CITE.finditer(line):
                for key in (k.strip() for k in m.group(2).split(',')):
                    cites.setdefault(key, where)
                if not m.group(1):
                    no_locator.append(f'{where}: {m.group(0)}')
            bibkeys.update(k.strip() for k in BIBITEM.findall(line))
            for m in NEWTHEOREM.finditer(line):
                if not m.group(2):
                    theorem_counters.append(m.group(1))
            renumbered.update(re.findall(r'\\renewcommand\{?\\the(\w+)\}?', line))
        offset = max(body_start, 0)
        body = text[offset:]
        def at(pos):
            return f'{name}:{text.count(chr(10), 0, offset + pos) + 1}'
        for pattern, advice in STYLE:
            for m in pattern.finditer(body):
                rep.add('warnings', at(m.start()), f'{m.group(0).strip()!r}: {advice}')
        for m in COLON_DISPLAY.finditer(body):
            rep.add('warnings', at(m.start()), 'colon before a display; keep it only if the grammar needs one [Poonen 50]')
        for m in SYMBOL_START.finditer(body):
            if m.group(1).rstrip('.') not in ABBREV:
                rep.add('warnings', at(m.end() - 1), 'sentence starts with a symbol [Poonen 32]')
        for m in CONCLUSION.finditer(body):
            rep.add('warnings', at(m.start()), 'mathematics papers usually have no conclusions section [Poonen 19]')
    for name in BIBFILES.findall('\n'.join(texts.values())):
        for item in name.split(','):
            bib = root / item.strip()
            bib = bib if bib.suffix == '.bib' else bib.with_name(bib.name + '.bib')
            if bib.is_file():
                bibkeys.update(BIBKEY.findall(bib.read_text(encoding='utf-8', errors='replace')))
            else:
                rep.add('warnings', None, f'bibliography file not found: {bib.name}')
    if not bibkeys and cites:
        rep.add('warnings', None, 'no bibliography entries found')
    for key, where in cites.items():
        if bibkeys and key not in bibkeys:
            rep.add('errors', where, f'citation key {key!r} has no bibliography entry')
    for key in sorted(bibkeys - set(cites)):
        rep.add('warnings', None, f'bibliography entry {key!r} is never cited')
    for key in sorted(refs - set(labels)):
        rep.add('errors', None, f'reference to undefined label {key!r}')
    unused = sorted(set(labels) - refs)
    rep.add('info', None, f'labels never referenced: {len(unused)}' + (f' ({", ".join(unused)})' if verbose and unused else ''))
    own = [t for t in theorem_counters if t not in renumbered]
    if len(own) > 1:
        rep.add('warnings', None, f'separate theorem counters {own}; share one counter [Poonen 36]')
    rep.add('info', None, f'citations without a locator: {len(no_locator)}; whole-work citations should be deliberate [Poonen 9]')
    if verbose:
        rep.info.extend('  ' + item for item in no_locator)
    alltext = texts[files[0][0]]
    m = re.search(r'\\begin\{abstract\}(.*?)\\end\{abstract\}', alltext, re.S)
    if not m:
        rep.add('warnings', None, 'no abstract found')
    else:
        abstract = m.group(1)
        chars, words = len(' '.join(abstract.split())), len(plain(abstract).split())
        rep.add('info', None, f'abstract: {words} words, about {chars} characters of TeX')
        if chars > 1920:
            rep.add('errors' if release else 'warnings', None, 'abstract exceeds arXiv limit of 1920 characters')
        if not 50 <= words <= 200:
            rep.add('warnings', None, f'abstract has {words} words; aim for about 80-150')
        if re.search(r'\\(?:cite|ref|eqref)\b', abstract):
            rep.add('warnings', None, 'abstract cites or refers to the body [Poonen 17]')
        if any(ord(c) > 127 for c in abstract):
            rep.add('warnings', None, 'non-ASCII characters in abstract; arXiv metadata is ASCII')
    t = alltext.find('\\title')
    if t >= 0:
        title = ' '.join(braced(alltext, t + len('\\title')).split())
        if WEAK_TITLE.search(title):
            rep.add('warnings', None, f'title {title!r}: omit "A note on" and similar [Poonen 15]')
    check_log(Path(log) if log else files[0][0].with_suffix('.log'), files[0][0], rep, release, overfull_pt)
    return rep


def check_log(log, main, rep, release, overfull_pt):
    if not log.is_file():
        rep.add('errors' if release else 'warnings', None, f'no build log {log.name}; compile with latexmk first')
        return
    if log.stat().st_mtime < main.stat().st_mtime:
        rep.add('warnings', None, f'{log.name} is older than {main.name}; rebuild')
    text = log.read_text(encoding='latin-1')
    for key in sorted(set(LOG_UNDEF.findall(text))):
        rep.add('errors', log.name, f'undefined reference or citation {key!r}')
    if 'multiply defined' in text or 'multiply-defined' in text:
        rep.add('errors', log.name, 'multiply defined labels or citations')
    for line in text.splitlines():
        if line.startswith('! '):
            rep.add('errors', log.name, f'TeX error: {line[2:].strip()}')
    if 'Rerun to get' in text or 'Label(s) may have changed' in text:
        rep.add('warnings', log.name, 'rerun LaTeX: cross-references may have changed')
    widths = [float(w) for w in OVERFULL.findall(text)]
    big = [w for w in widths if w > overfull_pt]
    if big:
        rep.add('warnings', log.name, f'{len(big)} overfull boxes over {overfull_pt}pt (max {max(big):.1f}pt)')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('paper', help='main .tex file')
    parser.add_argument('--log', help='build log (default: PAPER.log)')
    parser.add_argument('--release', action='store_true', help='release gate: stricter comment and log checks')
    parser.add_argument('--overfull-pt', type=float, default=1.0, help='overfull box threshold in pt')
    parser.add_argument('--verbose', action='store_true', help='list citations without locators and unused labels')
    args = parser.parse_args(argv)
    if not Path(args.paper).is_file():
        parser.error(f'not a file: {args.paper}')
    rep = check(args.paper, args.log, args.release, args.overfull_pt, args.verbose)
    print(f'check_paper: {args.paper}' + (' (release mode)' if args.release else ''))
    for title, items in (('ERRORS', rep.errors), ('WARNINGS', rep.warnings), ('INFO', rep.info)):
        if items:
            print(f'\n{title} ({len(items)})')
            print('\n'.join('  ' + item for item in items))
    verdict = f'FAIL: {len(rep.errors)} errors' if rep.errors else 'PASS'
    print(f'\nResult: {verdict}, {len(rep.warnings)} warnings')
    return 1 if rep.errors else 0


if __name__ == '__main__':
    sys.exit(main())
