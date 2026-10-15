# FubonBrokerProvider

以純 HTTP 讀取富邦 eBrokerDJ 台股券商／分點買賣資料的 Python 研究工具。提供券商名冊解析、單一股票與券商歷史明細、SQLite 儲存及 watchlist 增量更新。

本專案為非官方工具，與富邦證券及 MoneyDJ 無隸屬關係。

## 安裝

需要 Python 3.10 或更新版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 開始使用

先建立本機券商名冊：

```bash
python fubon_broker_provider.py refresh-roster
```

以南亞科（2408）、台灣摩根士丹利（1470）作為公開操作範例，查詢一段已完成的交易日期：

```bash
python fubon_broker_provider.py fetch \
  --stock-id 2408 --broker-id 1470 \
  --start-date 2026-09-01 --end-date 2026-09-15
```

程式會將結果寫入 `provider_data/fubon_broker.sqlite`，並在 `provider_data/raw/` 保存回應 HTML。SQLite 記錄來源 URL、擷取時間及每次請求的狀態與延遲。相同日期、股票與券商會更新既有資料。

如需變更儲存位置，將全域參數放在子命令前：

```bash
python fubon_broker_provider.py \
  --db data/brokers.sqlite --raw-dir data/raw \
  refresh-roster
```

## 追蹤清單與增量更新

從公開範例建立自己的本機清單：

```bash
cp watchlist.example.csv watchlist.csv
```

欄位格式：

```csv
stock_id,enabled,history_start
2408,true,2026-09-01
```

執行更新時請自行指定已完成的交易日期：

```bash
python fubon_broker_provider.py sync-watchlist \
  --broker-id 1470 --end-date 2026-09-15
```

- 新啟用標的：從 `history_start` 開始回補。
- 已有資料的標的：從最後儲存日期往前 10 個日曆天重新抓取，處理來源修正；可用 `--lookback-days` 調整。
- `enabled=false`：停止更新並保留歷史。
- 每次命令指定一個券商；如需追蹤其他券商，分別執行。

`watchlist.example.csv` 中的股票只是文件範例，不代表持倉或投資建議。實際 `watchlist.csv` 已列入忽略清單。

## 資料欄位

標準資料欄位為 `date, stock_id, broker_id, broker_name, buy, sell, net`。數量單位沿用來源的「張」，日期為 `YYYY-MM-DD`。部分券商分點在來源 URL 使用長編碼，請保留其原始字串。

## HTTP 探測工具

```bash
python probe_fubon_broker.py --stock-id 2408 --broker-id 1470 --output-dir output
python research_provider_readiness.py --output-dir research/output
```

探測工具預設使用固定歷史日期，適合重現測試；可透過 `--help` 查看參數。摘要頁顯示買超與賣超各 Top 15，不能當作完整券商名冊。名冊由網站公開提供的 JavaScript 解析。

## 使用限制

HTTP 請求有連線／讀取 timeout、暫時性失敗重試及預設 1.5 秒間隔。此版本仍屬研究用途，網站格式、涵蓋日期和可用性可能變更；HTTP 200 且零列不代表資料已確認完整。尚無交易日曆完整性檢查、背景排程或服務可用性承諾。

請在自己的環境執行並確認來源的使用條款與資料授權。程式碼公開不代表來源金融資料亦取得再散布授權；本儲存庫不附抓取的完整 HTML、資料庫或歷史行情資料。

## 隱私

儲存庫只包含程式、相依套件清單、文件及通用範例。個人追蹤清單、憑證、原始回應、資料庫和執行紀錄均應保留於本機。提交前請檢查 `git diff --cached`；`.gitignore` 無法自動移除已追蹤的檔案。
