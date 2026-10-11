#!/usr/bin/env python3
"""Verify local portfolio -> CSV -> contact. Formspree is intercepted, never contacted."""
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from functools import partial
from urllib.parse import urlsplit, parse_qs
import threading, shutil, json, hashlib, base64, re
import os
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, expect

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / 'csv-automation/evidence'
EVIDENCE.mkdir(exist_ok=True)
checks, external_attempts, errors, mock_posts = [], [], [], []
def record(name): checks.append({'name': name, 'status': 'passed'})
class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args): pass
server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(REPO)))
thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
origin=f'http://127.0.0.1:{server.server_port}'
endpoint='https://formspree.io/f/mbdpnddb'
report={'story':'Portfolio -> demo -> local CSV output -> contact form -> mocked submission; real receipt unverified','started_at_utc':datetime.now(timezone.utc).isoformat()}
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH') or shutil.which('chromium') or p.chromium.executable_path,headless=True,args=['--no-sandbox'])
  report['browser_version']=browser.version
  context=browser.new_context(viewport={'width':1440,'height':1060},accept_downloads=True)
  def route_handler(route):
   request=route.request
   if request.url.startswith(origin+'/'):
    route.continue_()
   elif request.url == endpoint:
    assert request.method == 'POST'
    mock_posts.append(parse_qs(request.post_data or '', keep_blank_values=True))
    route.fulfill(status=200,content_type='text/html; charset=utf-8',body='<html lang="ja"><body><h1>ローカル疑似受付</h1><p>本番には送信していません。</p></body></html>')
   else:
    external_attempts.append(request.url); route.abort()
  context.route('**/*',route_handler)
  page=context.new_page(); page.on('pageerror',lambda e:errors.append(str(e)))
  response=page.goto(origin+'/index.html'); assert response.status==200
  expect(page.locator('.hero h1')).not_to_contain_text('10倍')
  expect(page.get_by_role('link',name='無料相談する',exact=True)).to_have_attribute('href','#contact')
  expect(page.get_by_role('link',name='サービスを見る',exact=True)).to_have_attribute('href','#services')
  record('Homepage preserves the two original CTAs and removes unsupported multiplier')
  page.screenshot(path=str(EVIDENCE/'portfolio-desktop.png'),full_page=True)
  page.locator('#csv-demo-link').click(); assert page.url == origin+'/csv-automation/index.html'
  expect(page.locator('#sample')).to_be_visible(); record('Homepage demo link resolves over HTTP')
  page.locator('#sample').click(); expect(page.locator('#total')).to_have_text('90,000')
  expect(page.locator('#row-count')).to_have_text('8'); record('Hosted demo executes CSP-authorized scripts')
  page.screenshot(path=str(EVIDENCE/'integrated-demo-desktop.png'),full_page=True)
  with page.expect_download() as di: page.locator('#download').click()
  d=di.value; d.save_as(str(EVIDENCE/'integrated-export.csv'))
  raw=(EVIDENCE/'integrated-export.csv').read_bytes(); assert raw.startswith(b'\xef\xbb\xbf')
  assert '検索広告,3,45000,0' in raw.decode('utf-8-sig')
  record('HTTP demo exports correct BOM-prefixed CSV')
  page.locator('details').evaluate('(el)=>el.open=true')
  page.locator('#csv-text').fill('カテゴリ,金額\nprivate-canary,123');page.locator('#read-text').click()
  expect(page.locator('#total')).to_have_text('123')
  page.locator('#make-brief').click(); assert 'private-canary' not in page.locator('#brief').input_value()
  record('Consultation template does not leak input CSV')
  href=page.locator('#contact-link').get_attribute('href'); assert href=='../index.html?service=csv#contact'
  page.locator('#contact-link').click()
  assert page.url==origin+'/index.html?service=csv#contact'; record('Consultation link carries fixed service code only')
  expect(page.locator('#contact-service')).to_have_value('Webアプリ開発')
  assert 'CSV処理' in page.locator('#contact-message').input_value()
  assert 'private-canary' not in page.locator('#contact-message').input_value()
  record('Contact form prefills an existing service option without copying CSV')
  assert page.locator('form').get_attribute('action')==endpoint
  assert page.locator('form').get_attribute('method').upper()=='POST'
  for field in ['name','company','email','service','message']:
   assert page.locator(f'label[for="contact-{field}"]').count()==1
  expect(page.locator('#contact-privacy')).to_contain_text('Formspree')
  record('Contact endpoint, method, labels and external-send disclosure verified')
  page.get_by_role('button',name='送信する',exact=True).click(); assert not mock_posts
  assert not page.locator('form').evaluate('(f)=>f.checkValidity()')
  record('Missing required contact fields prevent submission')
  page.locator('#contact-name').fill('検証用サンプル')
  page.locator('#contact-email').fill('invalid-email')
  page.get_by_role('button',name='送信する',exact=True).click(); assert not mock_posts
  record('Invalid email prevents submission')
  page.locator('#contact-email').fill('qa@example.invalid')
  with page.expect_navigation(): page.get_by_role('button',name='送信する',exact=True).click()
  expect(page.locator('h1')).to_have_text('ローカル疑似受付')
  assert len(mock_posts)==1 and mock_posts[0]['service']==['Webアプリ開発']
  assert set(mock_posts[0])=={'name','company','email','service','message'}
  assert 'private-canary' not in json.dumps(mock_posts)
  record('Valid POST payload captured by mock; no real Formspree delivery')
  for width in [320,390,768,1440]:
   page.set_viewport_size({'width':width,'height':900})
   for path in ['/index.html','/csv-automation/index.html']:
    page.goto(origin+path)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'Overflow: {width} {path}'
   if width==390:
    page.locator('#sample').click();page.screenshot(path=str(EVIDENCE/'integrated-demo-mobile.png'),full_page=True)
    page.goto(origin+'/index.html?service=csv#contact');page.screenshot(path=str(EVIDENCE/'portfolio-mobile.png'),full_page=True)
  record('Both pages fit 320, 390, 768 and 1440px Chromium viewports')
  page.goto(origin+'/index.html'); expect(page.locator('#contact-message')).to_have_value('')
  expect(page.locator('#contact-service')).to_have_value('')
  record('Ordinary visitors do not receive CSV-specific prefill')
  page.goto(origin+'/index.html?service=%3Cscript%3E#contact')
  expect(page.locator('#contact-message')).to_have_value('')
  record('Unexpected query input is ignored, not rendered into HTML')
  page.goto(origin+'/csv-automation/index.html');page.locator('#sample').click();page.locator('#reset').click()
  expect(page.locator('#row-count')).to_have_text('0');expect(page.locator('#download')).to_be_disabled()
  assert page.evaluate('localStorage.length + sessionStorage.length')==0
  assert context.cookies()==[]
  record('Reset clears data; demo adds no browser storage or cookies')
  html=(REPO/'csv-automation/index.html').read_text()
  for name in ['csv-core','csv-app']:
   content=re.search(fr'<script id="{name}">(.*?)</script>',html,re.S).group(1)
   digest=base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()
   assert "'sha256-"+digest+"'" in html
  assert "connect-src 'none'" in html and "form-action 'none'" in html
  record('Generated inline script hashes match restrictive demo CSP')
  assert not errors, errors; assert not external_attempts,external_attempts
  record('No uncaught JavaScript errors or unmocked external requests')
  browser.close()
except Exception as e:
 report['failure']=str(e);raise
finally:
 server.shutdown(); server.server_close();thread.join(timeout=5)
 report.update({'checks':checks,'passed':len(checks),'failed':int('failure'in report),'external_requests_sent':0,'mock_posts':len(mock_posts),'javascript_errors':errors,'blocked_unexpected_requests':external_attempts,'safari_or_iphone_hardware_verified':False,'finished_at_utc':datetime.now(timezone.utc).isoformat()})
 (EVIDENCE/'integration-tests.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({k:report[k] for k in ['passed','failed','mock_posts','external_requests_sent']},ensure_ascii=False))
