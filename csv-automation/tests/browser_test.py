#!/usr/bin/env python3
"""Exercise the generated HTML without hosting it or contacting external services."""
import json
import shutil
import os
from pathlib import Path
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'index.html').read_text(encoding='utf-8')
EVIDENCE = ROOT / 'evidence'
EVIDENCE.mkdir(exist_ok=True)
checks = []

def record(name):
    checks.append({'name': name, 'status': 'passed'})

def pasted(page, text):
    page.locator('details').evaluate('(el) => el.open = true')
    page.locator('#csv-text').fill(text)
    page.locator('#read-text').click()

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH') or shutil.which('chromium') or p.chromium.executable_path, headless=True, args=['--no-sandbox'])
    page = browser.new_page(viewport={'width':1440,'height':1060}, accept_downloads=True)
    errors, requests = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('request', lambda request: requests.append(request.url))
    page.set_content(HTML, wait_until='load')
    expect(page.locator('#download')).to_be_disabled()
    record('Initial state blocks empty download')
    page.locator('#sample').click()
    expect(page.locator('#row-count')).to_have_text('8')
    expect(page.locator('#group-count')).to_have_text('3')
    expect(page.locator('#total')).to_have_text('90,000')
    record('Sample: 8 rows, 3 groups, expense total 90000')
    page.screenshot(path=str(EVIDENCE / 'desktop.png'), full_page=True)
    page.locator('#metric').select_option('3')
    expect(page.locator('#total')).to_have_text('237,000')
    record('Metric switch updates sample revenue total to 237000')
    page.locator('#normalize').uncheck()
    expect(page.locator('#group-count')).to_have_text('4')
    page.locator('#normalize').check()
    expect(page.locator('#group-count')).to_have_text('3')
    record('Normalization option preserves or combines categories as selected')
    page.locator('#metric').select_option('1')
    expect(page.locator('#status')).to_contain_text('別の列')
    expect(page.locator('#download')).to_be_disabled()
    record('Selecting the same group and sum column fails safely')
    pasted(page, 'カテゴリ,金額\nA,1\nA,\nB,abc\n,2')
    expect(page.locator('#total')).to_have_text('3')
    expect(page.locator('#warning')).to_contain_text('2件')
    expect(page.locator('#warning')).to_contain_text('空欄')
    record('Invalid numbers and blank categories reported explicitly')
    pasted(page, 'カテゴリ,金額\n"broken,1')
    expect(page.locator('#download')).to_be_disabled()
    expect(page.locator('#row-count')).to_have_text('0')
    record('Malformed input clears prior result rather than exporting stale data')
    pasted(page, 'カテゴリ,金額')
    expect(page.locator('#download')).to_be_disabled()
    expect(page.locator('#warning')).to_contain_text('データ行がありません')
    record('Header-only CSV stays non-downloadable')
    utf8 = {'name':'first.csv','mimeType':'text/csv','buffer':'カテゴリ,金額\nA,2\nB,3'.encode()}
    reordered = {'name':'second.csv','mimeType':'text/csv','buffer':'金額,カテゴリ\n4,A'.encode()}
    page.locator('#files').set_input_files([utf8,reordered])
    expect(page.locator('#total')).to_have_text('9')
    expect(page.locator('#row-count')).to_have_text('3')
    record('Multiple UTF-8 files with reordered columns are remapped and merged')
    page.locator('#files').set_input_files([utf8,{'name':'bad.csv','mimeType':'text/csv','buffer':b'other,field\nx,1'}])
    expect(page.locator('#status')).to_contain_text('見出しが一致しません')
    expect(page.locator('#download')).to_be_disabled()
    record('Cross-file schema mismatch fails without partial output')
    page.locator('#files').set_input_files([])
    page.locator('#encoding').select_option('shift_jis')
    page.locator('#files').set_input_files({'name':'japanese.csv','mimeType':'text/csv','buffer':'媒体,金額\n検索広告,1200'.encode('shift_jis')})
    expect(page.locator('#total')).to_have_text('1,200')
    expect(page.locator('#tbody')).to_contain_text('検索広告')
    record('Shift_JIS Japanese CSV upload is decoded correctly')
    page.locator('#encoding').select_option('utf-8')
    expect(page.locator('#status')).to_contain_text('文字コード')
    expect(page.locator('#download')).to_be_disabled()
    record('Wrong encoding fails visibly and clears result')
    page.locator('#files').set_input_files({'name':'not.csv','mimeType':'text/csv','buffer':b'x'*(5*1024*1024+1)})
    expect(page.locator('#status')).to_contain_text('5MB以下')
    record('Browser byte-size limit enforced before decoding')
    page.locator('#files').set_input_files({'name':'not.txt','mimeType':'text/plain','buffer':b'a,b\nx,1'})
    expect(page.locator('#status')).to_contain_text('拡張子')
    record('Unsupported file extension rejected')
    pasted(page, 'カテゴリ,金額\n<img src=x onerror=alert(1)>,2\n=1+1,3\nCONFIDENTIAL_MARKER,4')
    expect(page.locator('#total')).to_have_text('9')
    assert page.locator('#tbody img').count() == 0
    expect(page.locator('#tbody')).to_contain_text('<img src=x onerror=alert(1)>')
    record('Untrusted HTML displayed as inert text')
    with page.expect_download() as info:
        page.locator('#download').click()
    download = info.value
    target = EVIDENCE / 'test-export.csv'
    download.save_as(str(target))
    data = target.read_bytes()
    assert data.startswith(b'\xef\xbb\xbf') and b'\r\n' in data
    assert "'=1+1" in data.decode('utf-8-sig')
    record('CSV download contains BOM/CRLF and formula-escaped labels')
    page.locator('#make-brief').click()
    assert 'CONFIDENTIAL_MARKER' not in page.locator('#brief').input_value()
    record('Consultation draft excludes imported data')
    with page.expect_download() as info:
        page.locator('#save-brief').click()
    assert info.value.suggested_filename.endswith('.txt')
    record('Consultation draft can be saved without external submission')
    page.locator('#copy-brief').click()
    expect(page.locator('#copy-status')).not_to_be_empty()
    record('Copy button reports success or an explicit manual-copy fallback')
    page.locator('#reset').click()
    expect(page.locator('#row-count')).to_have_text('0')
    assert page.locator('#brief').input_value() == ''
    assert page.locator('#csv-text').input_value() == ''
    record('Reset clears both imported data and consultation text')
    page.evaluate('''() => {const original=File.prototype.arrayBuffer;File.prototype.arrayBuffer=function(){return new Promise(resolve => setTimeout(()=>resolve(original.call(this)),300));};}''')
    page.locator('#files').set_input_files(utf8)
    page.locator('#reset').click()
    page.wait_for_timeout(450)
    expect(page.locator('#row-count')).to_have_text('0')
    record('Reset wins over in-flight async file loading')
    page.locator('#sample').click()
    for width in [320,390,768,1440]:
        page.set_viewport_size({'width':width,'height':900})
        overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
        assert not overflow, f'Viewport overflow at {width}px'
    record('No horizontal page overflow at 320, 390, 768 and 1440 px')
    page.locator('details').evaluate('(el) => el.open = false')
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path=str(EVIDENCE / 'mobile.png'),full_page=True)
    page.locator('#sample').focus()
    page.keyboard.press('Enter')
    expect(page.locator('#total')).to_have_text('90,000')
    record('Keyboard activation works')
    assert not errors, errors
    assert not [url for url in requests if url.startswith(('http:','https:'))], requests
    record('No page JavaScript errors or HTTP(S) requests during tested interactions')
    report={'executed_at_utc':datetime.now(timezone.utc).isoformat(),'engine':'Chromium','browser_version':browser.version,'render_method':'page.set_content(generated HTML); no live hosting or file-URL launch verified','total':len(checks),'passed':len(checks),'failed':0,'checks':checks,'javascript_errors':errors,'network_requests':requests,'not_tested':['Safari/iOS device execution','Live hosting','Formspree delivery','Any customer data','Payment or revenue receipt']}
    (EVIDENCE / 'browser-tests.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':len(checks),'failed':0,'browser_version':browser.version},ensure_ascii=False))
    browser.close()
