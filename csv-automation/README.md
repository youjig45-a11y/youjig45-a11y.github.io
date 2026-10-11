# CSV集計自動化デモ

非公開レビュー用の単一HTMLデモ。参考価格は正式合意前の案です。
データはブラウザ内で処理し、外部送信や永続保存はしません。
本体に外部ライブラリ・APIキー・有料APIは不要です。

## ビルドとテスト

```sh
python csv-automation/scripts/build.py
node --test csv-automation/tests/core.test.cjs
python csv-automation/tests/browser_test.py
```

画面検証はPython PlaywrightとChromiumが必要です。
テストは生成HTMLをChromiumに直接読み込む方法です。Safari/iOS・実公開URL・フォーム到達は別途検証してください。

## 範囲

CSV10ファイル・合計5MB・20,000データ行・80列以内。同じ見出し集合のCSVを結合し、分類別の数値合計を出力します。
UTF-8/Shift_JIS、引用符や改行を含むセル、空欄や異常値の件数表示に対応します。
小数6桁まで、1セルの絶対値10億以下。安全整数の上限超過や、小数6桁の合計を正確に表示できない場合はエラーです。
完全に空の物理行を読み飛ばし、区切り文字や引用符で明示された空欄レコードと重複行は保持します。異なる単位・通貨の混在は検出しません。
分類値の正規化は選択可能です。出力文字列は表計算ソフトの数式として解釈される可能性がある先頭文字をエスケープします。
会計・請求・決済の確定処理には使用しないでください。サンプルは架空データで、実績ではありません。

既存ポートフォリオへの導線追加、受付先、正式価格、公開許可は別途確認してください。

## 既存サイトへの組み込み
トップページの既存CTAを維持してデモリンクを追加。デモ内の相談フォームリンクは相対URL `../index.html?service=csv#contact` です。URLは固定の分類コードだけを含み、CSV・ファイル名・編集した相談文を転送しません。フォームの送信先と既存のサービス種別を変更していません。

統合テスト: リポジトリのルートで `python csv-automation/tests/integration_test.py`。Chromium/Playwrightが必要です。ローカルHTTP配信と疑似受付を使用し、本番への送信はしません。Safari実機は未検証です。

## 2026-10-11 続行時の検証範囲
Node 70ケース、既存画面24シナリオ、追加のオフライン導線17シナリオを検証。オフライン導線は各ページを個別に描画し、URLの解決と固定クエリの注入、フォームのsubmitイベントをローカルで確認します。実際のページ遷移・HTTP POST・受信を検証するものではありません。
この実行環境のChromiumはローカルHTTPページへの移動をERR_BLOCKED_BY_ADMINISTRATORで拒否したため、HTTP統合テストは未完了です。`python csv-automation/tests/integration_test.py` は許可された実行環境で別途実行してください。このテストもFormspreeを模擬するため、実受信は別確認です。

## PRの継続検証
`.github/workflows/csv-demo-tests.yml` はNode回帰、生成HTML一致、Chromium画面、ローカルHTTP→疑似受付を検証します。秘密情報を読み込まず、実Formspreeの受信やiPhone Safari実機を保証しません。公開前の確認はIssue #8、問い合わせ同期はIssue #1で管理します。
