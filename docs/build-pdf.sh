#!/usr/bin/env bash
# Rebuild docs/research-hub-guide.pdf from the published web page, so the PDF
# always matches what readers see online. Run after the page has been updated
# on GitHub Pages. Needs Google Chrome (macOS) or chromium/google-chrome (Linux).
set -euo pipefail
DOCS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE="https://ilpenzo.github.io/math-research-hub/"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
CHROME=""
for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" google-chrome chromium chromium-browser; do
  if [ -x "$c" ] || command -v "$c" > /dev/null 2>&1; then CHROME="$c"; break; fi
done
[ -n "$CHROME" ] || { echo "Google Chrome or Chromium is needed." >&2; exit 1; }

curl -fsSL "$SITE?nocache=$(date +%s)" -o "$WORK/page.html"
python3 - "$WORK/page.html" "$SITE" <<'PYEOF'
import datetime, re, sys
path, site = sys.argv[1], sys.argv[2]
html = open(path, encoding="utf-8").read()
# Load the theme's stylesheet from the site; keep in-page links (#...) internal.
html = re.sub(r'href="/math-research-hub/', f'href="{site}', html)
print_css = """<style>
@page { size: A4; margin: 16mm 16mm 18mm 16mm; }
body { font-size: 10.5pt; }
.no-print { display: none !important; }
pre, pre code { font-size: 7.6pt !important; white-space: pre !important; overflow: visible !important; }
pre, table, blockquote, li { break-inside: avoid; }
h1, h2, h3 { break-after: avoid; }
a { color: #0645ad; }
.print-note { font-size: 9pt; color: #555; border-left: 3px solid #ccc; padding-left: 8px; }
</style>"""
html = html.replace("</head>", print_css + "</head>", 1)
note = (f'<p class="print-note">Printed {datetime.date.today():%d %B %Y}. The latest version is online at '
        f'<a href="{site}">{site}</a>.</p>')
html = re.sub(r"(</h3>)", r"\1" + note, html, count=1)
open(path, "w", encoding="utf-8").write(html)
PYEOF
"$CHROME" --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf-no-header \
  --print-to-pdf="$DOCS/research-hub-guide.pdf" "file://$WORK/page.html" > /dev/null 2>&1
echo "Wrote $DOCS/research-hub-guide.pdf"
