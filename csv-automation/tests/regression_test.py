#!/usr/bin/env python3
"""Focused regressions against generated HTML; synthetic files and no network."""
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'index.html').read_text(encoding='utf-8')
EVIDENCE = ROOT / 'evidence'
checks, errors, requests = [], [], []


def record(name):
    checks.append({'name': name, 'status': 'passed'})


def pasted(page, text):
    page.locator('details').evaluate('(el) => el.open = true')
    page.locator('#csv-text').fill(text)
    page.locator('#read-text').click()


def new_page(browser, controlled=False):
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    page.set_default_timeout(3000)
    page.route('**/*', lambda route: route.abort())
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: requests.append(request.url))
    page.set_content(HTML, wait_until='load')
    if controlled:
        page.evaluate('''() => {
            const original = File.prototype.arrayBuffer;
            window.pendingReads = [];
            File.prototype.arrayBuffer = function () {
                const file = this;
                return new Promise((resolve, reject) => {
                    pendingReads.push({file, resolve, reject, original, settled: false});
                });
            };
        }''')
    return page


def upload(page, *files):
    """Use native File objects while controlling when their buffers are read."""
    payload = [{'name': name, 'bytes': list(data)} for name, data in files]
    page.evaluate('''files => {
        const transfer = new DataTransfer();
        for (const file of files) {
            transfer.items.add(new File([new Uint8Array(file.bytes)], file.name,
                                       {type: 'text/csv'}));
        }
        const input = document.getElementById('files');
        input.files = transfer.files;
        input.dispatchEvent(new Event('change', {bubbles: true}));
    }''', payload)


def pending(page, count):
    page.wait_for_function('count => pendingReads.length === count', arg=count)


def settle(page, index, reject=False):
    page.evaluate('''async ({index, reject}) => {
        const read = pendingReads[index];
        if (!read || read.settled) throw new Error('Unexpected pending read');
        read.settled = true;
        if (reject) read.reject(new Error('Synthetic stale read failure'));
        else read.resolve(await read.original.call(read.file));
        // Flush promise callbacks and the subsequent UI update deterministically.
        await new Promise(requestAnimationFrame);
        await new Promise(requestAnimationFrame);
    }''', {'index': index, 'reject': reject})


def sample_is_preserved(page):
    expect(page.locator('#row-count')).to_have_text('8')
    expect(page.locator('#total')).to_have_text('90,000')
    expect(page.locator('#source-tag')).to_have_text('架空サンプル')
    expect(page.locator('#download')).to_be_enabled()


with sync_playwright() as p:
    executable = (os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH')
                  or shutil.which('chromium') or p.chromium.executable_path)
    browser = p.chromium.launch(executable_path=executable, headless=True,
                                args=['--no-sandbox'])
    page = new_page(browser)

    for blank in [',', '"",""', ' , ']:
        pasted(page, 'カテゴリ,金額\nA,1\n' + blank + '\nB,2')
        expect(page.locator('#row-count')).to_have_text('3')
        expect(page.locator('#total')).to_have_text('3')
        expect(page.locator('#warning')).to_contain_text('数値 1件')
        expect(page.locator('#warning')).to_contain_text('分類が空欄の行 1件')
        expect(page.locator('#tbody')).to_contain_text('（空欄）')
        expect(page.locator('#download')).to_be_enabled()
        record('Explicit blank record preserved and excluded once: ' + repr(blank))

    for header in [',金額', '"",金額', ' ,金額']:
        pasted(page, header + '\nA,1')
        expect(page.locator('#status')).to_contain_text('見出しに空欄')
        expect(page.locator('#row-count')).to_have_text('0')
        expect(page.locator('#download')).to_be_disabled()
        record('Empty header rejected: ' + repr(header))

    for sign in ['', '-']:
        for separate_groups in [False, True]:
            rows = [f'{"G" + str(i) if separate_groups else "A"},{sign}1000000000'
                    for i in range(9)]
            rows.append(f'{"fraction" if separate_groups else "A"},{sign}0.000001')
            pasted(page, 'カテゴリ,金額\n' + '\n'.join(rows))
            expect(page.locator('#status')).to_contain_text('小数6桁の精度')
            expect(page.locator('#total')).to_have_text('0')
            expect(page.locator('#group-count')).to_have_text('0')
            expect(page.locator('#download')).to_be_disabled()
            assert page.locator('#tbody tr').count() == 0
            record('Unrepresentable six-place ' + ('total' if separate_groups else 'group')
                   + ' rejected: ' + ('negative' if sign else 'positive'))

    page.locator('#sample').click()
    expect(page.locator('#result-title')).to_have_text('媒体別の集計')
    expect(page.locator('#total-caption')).to_have_text('VALID VALUES ONLY')
    page.locator('#metric').select_option('1')
    expect(page.locator('#status')).to_contain_text('別の列')
    expect(page.locator('#result-title')).to_have_text('集計条件を確認してください')
    expect(page.locator('#total-label')).to_have_text('合計')
    expect(page.locator('#total-caption')).to_have_text('集計できません')
    expect(page.locator('#download')).to_be_disabled()
    assert page.locator('#tbody tr').count() == 0
    record('Same-column selection clears stale result title and metric caption')
    page.close()

    first = ('first.csv', 'カテゴリ,金額\nA,2'.encode('utf-8'))
    second = ('second.csv', 'カテゴリ,金額\nB,3'.encode('utf-8'))
    page = new_page(browser, controlled=True)
    upload(page, first, second)
    pending(page, 1)
    settle(page, 0)
    pending(page, 2)
    page.locator('#reset').click()
    settle(page, 1)
    expect(page.locator('#row-count')).to_have_text('0')
    expect(page.locator('#total')).to_have_text('0')
    expect(page.locator('#source-tag')).to_have_text('未読み込み')
    expect(page.locator('#status')).to_contain_text('消去しました')
    expect(page.locator('#download')).to_be_disabled()
    record('Reset during second file prevents partial or stale upload result')
    page.close()

    page = new_page(browser, controlled=True)
    upload(page, first)
    pending(page, 1)
    page.locator('#sample').click()
    settle(page, 0, reject=True)
    sample_is_preserved(page)
    expect(page.locator('#status')).to_contain_text('8行を読み込みました')
    record('Rejected stale upload cannot clear a subsequently selected sample')
    page.close()

    page = new_page(browser, controlled=True)
    upload(page, first)
    pending(page, 1)
    upload(page, ('newer.csv', 'カテゴリ,金額\nNEW,7'.encode('utf-8')))
    pending(page, 2)
    settle(page, 1)
    expect(page.locator('#total')).to_have_text('7')
    settle(page, 0)
    expect(page.locator('#row-count')).to_have_text('1')
    expect(page.locator('#total')).to_have_text('7')
    expect(page.locator('#tbody')).to_contain_text('NEW')
    expect(page.locator('#download')).to_be_enabled()
    record('Older upload completing after newer upload cannot replace newer result')
    page.close()

    page = new_page(browser, controlled=True)
    upload(page, ('encoding.csv', 'カテゴリ,金額\n日本語,23'.encode('shift_jis')))
    pending(page, 1)
    page.locator('#encoding').select_option('shift_jis')
    pending(page, 2)
    settle(page, 0)
    expect(page.locator('#status')).to_have_text('CSVを読み込んでいます。')
    expect(page.locator('#row-count')).to_have_text('0')
    expect(page.locator('#download')).to_be_disabled()
    settle(page, 1)
    expect(page.locator('#row-count')).to_have_text('1')
    expect(page.locator('#total')).to_have_text('23')
    expect(page.locator('#tbody')).to_contain_text('日本語')
    expect(page.locator('#download')).to_be_enabled()
    assert page.evaluate('pendingReads.every(read => read.settled)')
    record('Encoding change invalidates pending decode and uses current encoding')
    page.close()

    assert not errors, errors
    assert not [url for url in requests if url.startswith(('http:', 'https:'))], requests
    record('All regressions finish without page errors or HTTP(S) requests')
    report = {
        'executed_at_utc': datetime.now(timezone.utc).isoformat(),
        'engine': 'Chromium', 'browser_version': browser.version,
        'render_method': 'page.set_content(generated HTML); all network routes aborted',
        'total': len(checks), 'passed': len(checks), 'failed': 0,
        'checks': checks, 'javascript_errors': errors, 'network_requests': requests,
        'not_tested': ['Safari/iOS device execution', 'Live hosting',
                       'Formspree delivery', 'Customer data', 'Payments or revenue'],
    }
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / 'regression-tests.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': len(checks), 'failed': 0,
                      'browser_version': browser.version}, ensure_ascii=False))
    browser.close()
