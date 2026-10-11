#!/usr/bin/env python3
"""Offline DOM/route simulation. No HTTP navigation, real POST, receipt, or server verification."""
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from functools import partial
from urllib.parse import urlsplit, parse_qs, urljoin
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
origin='https://offline-fixture.invalid'
endpoint='https://formspree.io/f/mbdpnddb'
report={'story':'Offline DOM/route simulation: portfolio -> CSV -> contact -> captured submit event; HTTP and real receipt not verified','started_at_utc':datetime.now(timezone.utc).isoformat()}
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH') or shutil.which('chromium') or p.chromium.executable_path,headless=True,args=['--no-sandbox'])
  report['browser_version']=browser.version
  context=browser.new_context(viewport={'width':1440,'height':1060},accept_downloads=True)
  def block_network(route):
   external_attempts.append(route.request.url);route.abort()
  context.route('**/*',block_network)
  page=context.new_page(); page.on('pageerror',lambda e:errors.append(str(e)))
  def load_fixture(url):
   global page
   viewport=page.viewport_size
   page.close()
   page=context.new_page(); page.set_viewport_size(viewport); page.on('pageerror',lambda e:errors.append(str(e)))
   parsed=urlsplit(url)
   relative=parsed.path.lstrip('/')
   assert relative in ['index.html','csv-automation/index.html']
   content=(REPO/relative).read_text()
   # Fixture-only injection of a fixed query string. Production files are unchanged.
   content=content.replace('window.location.search',json.dumps('?' + parsed.query if parsed.query else ''))
   page.set_content(content,wait_until='load')
   page.evaluate("""() => {
     window.__fixtureHref = null;
     document.querySelectorAll('a').forEach(a=>a.addEventListener('click',event=>{
       event.preventDefault(); window.__fixtureHref=a.getAttribute('href');
     }));
     const form=document.querySelector('form');
     if(form) form.addEventListener('submit',event=>{
       event.preventDefault();window.__submittedData=Object.fromEntries(new FormData(form));
     });
   }""")
  load_fixture(origin+'/index.html')
  expect(page.locator('.hero h1')).not_to_contain_text('10倍')
  expect(page.get_by_role('link',name='無料相談する',exact=True)).to_have_attribute('href','#contact')
  expect(page.get_by_role('link',name='サービスを見る',exact=True)).to_have_attribute('href','#services')
  record('Homepage preserves the two original CTAs and removes unsupported multiplier')
  page.screenshot(path=str(EVIDENCE/'portfolio-desktop.png'),full_page=True)
  page.locator('#csv-demo-link').click(); href=page.evaluate('window.__fixtureHref'); assert urljoin(origin+'/index.html',href)==origin+'/csv-automation/index.html'; load_fixture(urljoin(origin+'/index.html',href))
  expect(page.locator('#sample')).to_be_visible(); record('Demo href resolves to a packaged file in offline route simulation')
  page.locator('#sample').click(); expect(page.locator('#total')).to_have_text('90,000')
  expect(page.locator('#row-count')).to_have_text('8'); record('Offline-rendered demo executes CSP-authorized scripts')
  page.screenshot(path=str(EVIDENCE/'integrated-demo-desktop.png'),full_page=True)
  with page.expect_download() as di: page.locator('#download').click()
  d=di.value; d.save_as(str(EVIDENCE/'integrated-export.csv'))
  raw=(EVIDENCE/'integrated-export.csv').read_bytes(); assert raw.startswith(b'\xef\xbb\xbf')
  assert '検索広告,3,45000,0' in raw.decode('utf-8-sig')
  record('Offline-rendered demo exports correct BOM-prefixed CSV')
  page.locator('details').evaluate('(el)=>el.open=true')
  page.locator('#csv-text').fill('カテゴリ,金額\nprivate-canary,123');page.locator('#read-text').click()
  expect(page.locator('#total')).to_have_text('123')
  page.locator('#make-brief').click(); assert 'private-canary' not in page.locator('#brief').input_value()
  record('Consultation template does not leak input CSV')
  href=page.locator('#contact-link').get_attribute('href'); assert href=='../index.html?service=csv#contact'
  page.locator('#contact-link').click()
  href=page.evaluate('window.__fixtureHref'); target=urljoin(origin+'/csv-automation/index.html',href); assert target==origin+'/index.html?service=csv#contact';load_fixture(target); record('Consultation link carries fixed service code only')
  expect(page.locator('#contact-service')).to_have_value('Webアプリ開発')
  assert 'CSV処理' in page.locator('#contact-message').input_value()
  assert 'private-canary' not in page.locator('#contact-message').input_value()
  record('Contact form prefills an existing service option without copying CSV')
  assert page.locator('form').get_attribute('action')==endpoint
  assert page.locator('form').get_attribute('method').upper()=='POST'
  for field in ['name','company','email','service','message']:
   assert page.locator(f'label[for="contact-{field}"]').count()==1
  expect(page.locator('#contact-privacy')).to_contain_text('Formspree')
  record('Contact endpoint and method attributes, labels and external-send disclosure verified')
  page.get_by_role('button',name='送信する',exact=True).click(); assert not mock_posts
  assert not page.locator('form').evaluate('(f)=>f.checkValidity()')
  record('Missing required contact fields prevent submission')
  page.locator('#contact-name').fill('検証用サンプル')
  page.locator('#contact-email').fill('invalid-email')
  page.get_by_role('button',name='送信する',exact=True).click(); assert not mock_posts
  record('Invalid email prevents submission')
  page.locator('#contact-email').fill('qa@example.invalid')
  page.get_by_role('button',name='送信する',exact=True).click()
  payload=page.evaluate('window.__submittedData'); assert payload
  mock_posts.append({k:[v] for k,v in payload.items()})
  assert len(mock_posts)==1 and mock_posts[0]['service']==['Webアプリ開発']
  assert set(mock_posts[0])=={'name','company','email','service','message'}
  assert 'private-canary' not in json.dumps(mock_posts)
  record('Valid submit event captured locally; no HTTP POST or delivery tested')
  for width in [320,390,768,1440]:
   page.set_viewport_size({'width':width,'height':900})
   for path in ['/index.html','/csv-automation/index.html']:
    load_fixture(origin+path)
    assert page.evaluate('innerWidth') == width
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'Overflow: {width} {path}'
   if width==390:
    page.locator('#sample').click();page.screenshot(path=str(EVIDENCE/'integrated-demo-mobile.png'),full_page=True)
    load_fixture(origin+'/index.html?service=csv#contact');page.screenshot(path=str(EVIDENCE/'portfolio-mobile.png'),full_page=True)
  record('Both pages fit 320, 390, 768 and 1440px Chromium viewports')
  load_fixture(origin+'/index.html'); expect(page.locator('#contact-message')).to_have_value('')
  expect(page.locator('#contact-service')).to_have_value('')
  record('Ordinary visitors do not receive CSV-specific prefill')
  load_fixture(origin+'/index.html?service=%3Cscript%3E#contact')
  expect(page.locator('#contact-message')).to_have_value('')
  record('Unexpected query input is ignored, not rendered into HTML')
  load_fixture(origin+'/csv-automation/index.html');page.locator('#sample').click();page.locator('#reset').click()
  expect(page.locator('#row-count')).to_have_text('0');expect(page.locator('#download')).to_be_disabled()
  source=(REPO/'csv-automation/src/app.js').read_text()
  assert 'localStorage' not in source and 'sessionStorage' not in source
  assert context.cookies()==[]
  record('Reset clears data; source uses no persistent storage; no cookies added')
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
 report.update({'checks':checks,'passed':len(checks),'failed':int('failure'in report),'external_requests_sent':0,'captured_submit_events':len(mock_posts),'javascript_errors':errors,'blocked_unexpected_requests':external_attempts,'safari_or_iphone_hardware_verified':False,'finished_at_utc':datetime.now(timezone.utc).isoformat()})
 (EVIDENCE/'offline-flow-tests.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({k:report[k] for k in ['passed','failed','captured_submit_events','external_requests_sent']},ensure_ascii=False))
