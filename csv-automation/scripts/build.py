#!/usr/bin/env python3
"""Build the self-contained demo and lock inline scripts with CSP hashes."""
from pathlib import Path
import base64
import hashlib

ROOT = Path(__file__).resolve().parents[1]
core = (ROOT / 'src/core.js').read_text(encoding='utf-8')
app = (ROOT / 'src/app.js').read_text(encoding='utf-8')
style = (ROOT / 'src/style.css').read_text(encoding='utf-8')
hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'" for s in (core, app)]
csp = "default-src 'none'; script-src " + ' '.join(hashes) + "; style-src 'unsafe-inline'; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
html = (ROOT / 'src/template.html').read_text(encoding='utf-8')
for key, value in {'__CORE__':core,'__APP__':app,'__STYLE__':style,'__CSP__':csp}.items():
    html = html.replace(key, value)
target = ROOT / 'index.html'
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(html,encoding='utf-8')
print(f'Built {target.relative_to(ROOT)} ({target.stat().st_size:,} bytes)')
