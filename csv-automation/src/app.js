(function () {
  'use strict';
  const $ = id => document.getElementById(id), kit = CsvKit;
  const sample = '日付,媒体,費用,売上,成果件数\n2026-10-01,検索広告,18000,60000,6\n2026-10-02,検索広告,12000,33000,3\n2026-10-03,検索広告,15000,51000,5\n2026-10-01,SNS広告,10000,21000,3\n2026-10-02,SNS広告,12000,30000,4\n2026-10-03,SNS広告 ,8000,18000,2\n2026-10-01,メール,5000,10000,2\n2026-10-02,メール,10000,14000,3\n';
  let table = null, result = null, loadVersion = 0;
  const number = value => new Intl.NumberFormat('ja-JP', { maximumFractionDigits: 6 }).format(value);
  function status(text, error = false) { $('status').textContent = text; $('status').classList.toggle('error', error); }
  function clearData() {
    table = null; result = null;
    for (const id of ['group','metric']) { $(id).replaceChildren(new Option('CSV読み込み後に選択','')); $(id).disabled = true; }
    for (const id of ['row-count','group-count','total']) $(id).textContent = '0';
    $('total-label').textContent = '合計'; $('total-caption').textContent = 'SELECT A METRIC';
    $('tbody').replaceChildren(); $('table-wrap').hidden = true; $('empty').hidden = false;
    $('download').disabled = true; $('warning').hidden = true;
    $('source-tag').textContent = '未読み込み'; $('result-title').textContent = '集計結果がここに表示されます';
  }
  function clearAll() { loadVersion++; clearData(); $('files').value = ''; $('csv-text').value = ''; $('brief').value = ''; $('consult').hidden = true; $('copy-status').textContent = ''; status('読み込んだデータをこの画面から消去しました。'); }
  function render() {
    result = null; $('download').disabled = true;
    if (!table) return;
    try {
      const gi = Number($('group').value), vi = Number($('metric').value);
      result = kit.aggregate(table, gi, vi, $('normalize').checked);
      $('row-count').textContent = number(result.rows); $('group-count').textContent = number(result.groups.length);
      $('total').textContent = number(result.total); $('total-label').textContent = table.headers[vi] + ' 合計';
      $('total-caption').textContent = 'VALID VALUES ONLY';
      $('group-head').textContent = table.headers[gi]; $('sum-head').textContent = table.headers[vi] + ' 合計';
      const body = document.createDocumentFragment();
      result.groups.slice(0, 100).forEach(group => {
        const tr = document.createElement('tr');
        [group.key === '' ? '（空欄）' : group.key, number(group.count), number(group.sum), number(group.invalid)].forEach(value => {
          const td = document.createElement('td'); td.textContent = value; tr.appendChild(td);
        }); body.appendChild(tr);
      });
      $('tbody').replaceChildren(body); $('table-wrap').hidden = !result.groups.length; $('empty').hidden = !!result.groups.length;
      $('result-title').textContent = table.headers[gi] + '別の集計';
      $('table-caption').textContent = `${result.groups.length}分類中 ${Math.min(100,result.groups.length)}分類を表示。CSVには全分類を出力。`;
      const warnings = [];
      if (result.invalid) warnings.push(`合計対象の空欄・不正な数値 ${number(result.invalid)}件を除外しています。除外内容を元CSVで確認してください。`);
      if (result.blankGroups) warnings.push(`分類が空欄の行 ${number(result.blankGroups)}件は「（空欄）」として集計しています。`);
      if (!result.rows) warnings.push('見出しのみでデータ行がありません。');
      $('warning').textContent = warnings.join(' '); $('warning').hidden = !warnings.length;
      $('download').disabled = !result.groups.length;
      status(`${number(result.rows)}行を読み込みました。元CSVは変更していません。`);
    } catch (error) {
      $('tbody').replaceChildren(); $('table-wrap').hidden = true; $('empty').hidden = false;
      $('warning').hidden = true; $('group-count').textContent = '0'; $('total').textContent = '0';
      $('total-label').textContent = '合計'; $('total-caption').textContent = '集計できません';
      $('result-title').textContent = '集計条件を確認してください';
      status(error.message, true);
    }
  }
  function accept(tables, label) {
    table = kit.mergeTables(tables);
    if (table.headers.length < 2) throw new Error('分類と合計のために2列以上が必要です。');
    for (const id of ['group','metric']) {
      $(id).replaceChildren(...table.headers.map((header,i) => new Option(header, String(i))));
      $(id).disabled = false;
    }
    const groupIndex = table.headers.findIndex(h => ['媒体','カテゴリ','分類','商品','部署'].includes(h));
    $('group').value = String(groupIndex >= 0 ? groupIndex : 0);
    let metricIndex = table.headers.findIndex(h => ['費用','売上','金額','数量'].includes(h));
    if (metricIndex < 0 || metricIndex === Number($('group').value)) metricIndex = table.headers.findIndex((_,i) => i !== Number($('group').value));
    $('metric').value = String(metricIndex);
    $('source-tag').textContent = label;
    render();
  }
  function fail(error) { clearData(); status(error instanceof Error ? error.message : '読み込みに失敗しました。', true); }
  async function readFiles() {
    const version = ++loadVersion;
    const files = Array.from($('files').files), encoding = $('encoding').value;
    clearData();
    if (!files.length) { status('CSVを選択してください。'); return; }
    status('CSVを読み込んでいます。');
    try {
      if (files.length > kit.LIMITS.files) throw new Error('ファイルは10個までです。');
      if (files.reduce((sum,f) => sum + f.size,0) > kit.LIMITS.bytes) throw new Error('ファイルは合計5MB以下にしてください。');
      const tables = [];
      for (const file of files) {
        if (!/\.csv$/i.test(file.name)) throw new Error('拡張子が.csvのファイルを選択してください。');
        const bytes = await file.arrayBuffer();
        if (version !== loadVersion) return;
        let text;
        try { text = new TextDecoder(encoding, { fatal: true }).decode(bytes); }
        catch { throw new Error('文字コードを読み取れません。UTF-8 / Shift_JISの選択を確認してください。'); }
        tables.push(kit.parseCSV(text));
      }
      if (version === loadVersion) { $('csv-text').value = ''; accept(tables, `${files.length}ファイル / ローカル処理`); }
    } catch (error) { if (version === loadVersion) fail(error); }
  }
  function saveText(text, filename, mime) {
    const url = URL.createObjectURL(new Blob([text],{ type: mime }));
    const a = document.createElement('a'); a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  $('sample').addEventListener('click', () => {
    loadVersion++; clearData(); $('files').value = ''; $('csv-text').value = '';
    try { accept([kit.parseCSV(sample)], '架空サンプル'); } catch (error) { fail(error); }
  });
  $('files').addEventListener('change', readFiles);
  $('encoding').addEventListener('change', () => { if ($('files').files.length) readFiles(); });
  $('read-text').addEventListener('click', () => {
    loadVersion++; clearData(); $('files').value = '';
    try {
      const text = $('csv-text').value;
      if (new TextEncoder().encode(text).length > kit.LIMITS.bytes) throw new Error('貼り付けは5MB以下にしてください。');
      accept([kit.parseCSV(text)], '貼り付け / ローカル処理');
    } catch (error) { fail(error); }
  });
  for (const id of ['group','metric','normalize']) $(id).addEventListener('change', render);
  $('reset').addEventListener('click', clearAll);
  $('download').addEventListener('click', () => {
    if (!result || !table || !result.groups.length) return;
    const header = [table.headers[Number($('group').value)],'件数',table.headers[Number($('metric').value)] + ' 合計','除外件数'];
    const rows = result.groups.map(g => [g.key,g.count,g.sum,g.invalid]);
    saveText(kit.toCSV([header,...rows]), '集計結果.csv', 'text/csv;charset=utf-8');
  });
  $('make-brief').addEventListener('click', () => {
    $('brief').value = 'CSV処理の自動化について相談したいです。\n\n【現在の作業】\n（例：毎月複数のCSVをまとめ、分類別の合計を作っています）\n\n【希望する入力と出力】\n入力：\n出力：\n\n【データ量・実行頻度】\n\n【利用端末・ブラウザ】\n\n【希望時期・予算】\n\nまずは入力1形式・出力1形式の範囲で、実現方法とお見積もりを相談したいです。入力例と期待する出力例は、機密情報を匿名化して用意します。';
    $('consult').hidden = false; $('copy-status').textContent = '';
    $('consult').scrollIntoView({behavior:'smooth', block:'start'});
  });
  $('copy-brief').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('brief').value); $('copy-status').textContent = '相談文をコピーしました。送信はしていません。'; }
    catch { $('brief').focus(); $('brief').select(); $('copy-status').textContent = '自動コピーできません。選択中の文章をコピーしてください。'; }
  });
  $('save-brief').addEventListener('click', () => saveText($('brief').value, 'CSV自動化_相談文.txt', 'text/plain;charset=utf-8'));
})();
