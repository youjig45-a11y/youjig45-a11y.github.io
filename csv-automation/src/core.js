(function (root) {
  'use strict';
  const LIMITS = Object.freeze({ bytes: 5 * 1024 * 1024, rows: 20000, columns: 80, files: 10 });
  const normalize = value => String(value).normalize('NFKC').trim();
  function parseCSV(source) {
    if (typeof source !== 'string') throw new Error('CSVはテキストで指定してください。');
    if (source.length > LIMITS.bytes) throw new Error('CSVが大きすぎます。合計5MB以下でお試しください。');
    const text = source.replace(/^\uFEFF/, '');
    if (!text.trim()) throw new Error('CSVが空です。');
    if (/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/.test(text)) throw new Error('CSVに未対応の制御文字があります。');
    const rows = []; let row = [], field = '', quoted = false, closed = false, touched = false;
    function cell() {
      row.push(field); field = ''; closed = false;
      if (row.length > LIMITS.columns) throw new Error('列数は80列までです。');
    }
    function line() {
      const hasRecord = touched || row.length > 0 || closed;
      cell();
      if (hasRecord) rows.push(row);
      row = []; touched = false;
      if (rows.length > LIMITS.rows + 1) throw new Error('データは20,000行までです。');
    }
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (quoted) {
        if (ch === '"') {
          if (text[i + 1] === '"') { field += '"'; i++; }
          else { quoted = false; closed = true; }
        } else field += ch;
        touched = true; continue;
      }
      if (ch === ',') { cell(); touched = true; continue; }
      if (ch === '\n' || ch === '\r') { if (ch === '\r' && text[i + 1] === '\n') i++; line(); continue; }
      if (closed) throw new Error('閉じ引用符の後に不正な文字があります。');
      if (ch === '"') {
        if (field !== '') throw new Error('セル途中の引用符を確認してください。');
        quoted = true;
      } else field += ch;
      touched = true;
    }
    if (quoted) throw new Error('閉じられていない引用符があります。');
    if (touched || row.length || field || closed) line();
    if (!rows.length) throw new Error('CSVが空です。');
    const headers = rows[0].map(normalize);
    if (headers.some(h => !h)) throw new Error('見出しに空欄があります。');
    if (new Set(headers).size !== headers.length) throw new Error('正規化後に重複する見出しがあります。');
    const data = rows.slice(1);
    data.forEach((r, i) => { if (r.length !== headers.length) throw new Error(`データ${i + 1}行目の列数が見出しと一致しません。`); });
    return { headers, rows: data };
  }
  function mergeTables(tables) {
    if (!Array.isArray(tables) || !tables.length) throw new Error('CSVを読み込んでください。');
    if (tables.length > LIMITS.files) throw new Error('ファイルは10個までです。');
    const headers = tables[0].headers.slice(), rows = [];
    for (const table of tables) {
      if (table.headers.length !== headers.length || headers.some(h => !table.headers.includes(h))) {
        throw new Error('CSVの見出しが一致しません。同じ項目のCSVだけをまとめてください。');
      }
      const order = headers.map(h => table.headers.indexOf(h));
      for (const row of table.rows) {
        rows.push(order.map(i => row[i]));
        if (rows.length > LIMITS.rows) throw new Error('結合後のデータは20,000行までです。');
      }
    }
    return { headers, rows };
  }
  function parseNumber(value) {
    let text = normalize(value);
    if (!text) return null;
    text = text.replace(/^[¥￥$]/, '').replace(/円$/, '').trim();
    if (!/^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,6})?$/.test(text)) return null;
    const number = Number(text.replaceAll(',', ''));
    return Number.isFinite(number) && Math.abs(number) <= 1000000000 ? number : null;
  }
  function aggregate(table, groupIndex, valueIndex, trimGroups = true) {
    if (![groupIndex, valueIndex].every(i => Number.isInteger(i) && i >= 0 && i < table.headers.length)) throw new Error('集計する列を選んでください。');
    if (groupIndex === valueIndex) throw new Error('分類列と合計列は別の列にしてください。');
    const groups = new Map(); let invalid = 0, blankGroups = 0, total = 0;
    for (const row of table.rows) {
      const key = trimGroups ? normalize(row[groupIndex]) : row[groupIndex];
      if (key === '') blankGroups++;
      if (!groups.has(key)) groups.set(key, { key, count: 0, valid: 0, invalid: 0, sum: 0 });
      const group = groups.get(key); group.count++;
      const value = parseNumber(row[valueIndex]);
      if (value === null) { group.invalid++; invalid++; }
      else {
        // Six-place fixed-point accumulation avoids ordinary decimal addition drift.
        const scaled = Math.round(value * 1000000);
        if (!Number.isSafeInteger(group.sum + scaled) || !Number.isSafeInteger(total + scaled)) throw new Error('合計が安全に扱える数値範囲を超えました。');
        group.sum += scaled; group.valid++; total += scaled;
      }
    }
    function unscale(scaled) {
      const value = scaled / 1000000;
      const fixed = value.toFixed(6);
      const canonical = fixed.replace(/\.?0+$/, '');
      const displayed = new Intl.NumberFormat('ja-JP', { maximumFractionDigits: 6, useGrouping: false }).format(value);
      // A safe scaled integer can still lose a decimal place when converted
      // back to Number. Refuse a result that cannot display all six places.
      if (Number(fixed.replace('.', '')) !== scaled || String(value) !== canonical || displayed !== canonical) {
        throw new Error('合計を小数6桁の精度で表示できません。データを分けて集計してください。');
      }
      return value;
    }
    const result = Array.from(groups.values(), g => ({ ...g, sum: unscale(g.sum) }));
    result.sort((a, b) => b.sum - a.sum || a.key.localeCompare(b.key, 'ja'));
    return { groups: result, invalid, blankGroups, total: unscale(total), rows: table.rows.length };
  }
  function safeCell(value) {
    let text = String(value ?? '');
    // Text cells that spreadsheet software could interpret as formulae are escaped.
    if (typeof value !== 'number' && (/^[\s\uFEFF]*[=+@-]/.test(text) || /^[\t\r\n]/.test(text))) text = "'" + text;
    return /[",\r\n]/.test(text) ? '"' + text.replaceAll('"', '""') + '"' : text;
  }
  function toCSV(rows) { return '\uFEFF' + rows.map(r => r.map(safeCell).join(',')).join('\r\n') + '\r\n'; }
  root.CsvKit = Object.freeze({ LIMITS, normalize, parseCSV, mergeTables, parseNumber, aggregate, safeCell, toCSV });
})(globalThis);
