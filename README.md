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

## RAW-ONLY capture v1 (opt-in, separate from legacy fetch)

`fubon_raw_capture.py` exports one new acquisition occurrence beneath an explicit,
existing provider-owned `--export-root`. It never reads/copies old raw files or
SQLite, invokes the legacy parsers, or writes normalized rows. Existing fetch,
watchlist, probe and roster paths remain legacy and are **not admissible** as
capture-v1 evidence.

The standalone CLI requires `--kind roster|detail`, `--provider-version` and
`--git-commit` (40 lowercase hex characters). Detail requests require
`--parameters` JSON with exact string keys `a`, `BHID`, `b`, `C`, `D`, `E`, `ver`;
`C` is `"1"`, `ver` is `"V3"`, and `D`/`E` are ISO requested date bounds.
Supply `BHID` explicitly: no identity/parent/market relationship is inferred.
Roster parameters are `{}`. `--max-body-bytes` is positive, default 20 MiB.
No live acquisition, scheduling, source-term clearance or redistribution is
established by this software. Obtain applicable source permission and scoped
operational authorization before invoking network acquisition.

Python API: `capture(Path(root), kind, parameters, provider_version=...,
git_commit=..., max_body_bytes=...) -> PublicationResult`. The result holds the
unchanged `manifest`, `publication_state` and `durability_status`. Offline tests may inject a transport,
UTC clock and UUID factory. A dedicated requests session disables environment
credentials, automatic retries and redirects and streams decoded entity bytes
under the cap. It preserves bytes without text/numeric parsing. HTTP 200, empty
bodies and HTTP errors establish no financial success or completeness.

Each UUID4 directory contains exact `body.bin` bytes and canonical UTF-8
`manifest.json` (sorted keys, compact separators, Unicode retained, finite JSON,
one final LF). Schema is `fubon-raw-capture-v1`, source `fubon_ebrokerdj_raw`.
Manifest fields are capture/provider identity, exact prepared request/parameters,
per-attempt UTC times, status/content-type/body size/SHA-256, nullable sanitized
transport error, NULL official availability and explicit blocked semantic gates.
Transport errors publish a negative envelope without a body or response time;
body-read/decompression/limit and prepublication storage failures publish no
final UUID capture; abandoned private staging remains inadmissible.
Body creation precedes manifest creation in private `.pending-<UUID>` staging,
both exclusive/no-follow. Files are flushed/fsynced/closed, then staging and
root are fsynced. One Linux `renameat2(RENAME_NOREPLACE)` publishes the complete
directory without replacement; root is then fsynced again. No rename fallback
is supported. Collisions fail without changing existing paths. Private staging
and incomplete artifacts remain untouched and must be rejected by readers. Published captures must never be modified,
moved or cleaned up. Repeating a request creates another occurrence, even when
body bytes match. Provider timestamps are observations, never public release
or consumer first-observation timestamps. Financial normalization, units,
identity, coverage, terms, durable consumer receipts and strict historical PIT
remain blocked pending separate decisions.

Offline verification: `python3 -m unittest discover -s tests -v`. No new test
framework dependency is required. The implementation requires POSIX fd-relative
exclusive/no-follow file operations.


RAW filesystem/publication prerequisites (FUBON-RAW-FS-1): only protected local
Linux filesystems with directory fsync and renameat2 no-replace support are
supported. No NFS/FUSE/remote/uncertain filesystem semantics. The operator must
provision a dedicated trusted writer UID, externally enforce one writer process,
provide read-only trusted consumers, and protect root and all relevant ancestors
against rename/replacement/mount changes. No concurrent rotation or cleanup.
The root must be owned by effective UID with no group/other write bits; the
provider rejects violations without changing permissions. ACL/ancestor and
writer exclusivity protection remain operator duties, not claims established by
these checks or tests. New staging directories are private 0700. Deliberately
broader read access must be provisioned before publication without write access.
Hostile same-UID/root/capability actors, injected callbacks, compromised kernels
and storage are excluded. Boundary identity checks detect the tested deterministic
moves before the next write, but cannot close arbitrary hostile check/write races.
No deployment isolation is attested by offline tests.

Operational dispositions do not change capture-v1 manifest/reader admission:
prepublication rejection raises sanitized CaptureError and CLI exits 1; there
is no new final UUID export. Published/synced returns the typed result and CLI
exits 0 with capture_id, outcome, publication_state and durability_status.
Postpublication root-fsync/descriptor-close failure returns published/unknown,
CLI exit 2 with capture_id and states; the intact immutable export can validate.
A publication exception is reconciled read-only against the pinned staging inode.
Unresolvable publication returns unknown/unknown, CLI exit 2, and blocks further
captures in that process. No rollback, cleanup, overwrite or receipt is attempted.
A wrapper error after real rename must not be called rejection. Absence of an
acknowledgement never proves absence of an export. Surviving final files still
require independent read-only schema/body/hash validation; validation does not
establish crash survival, delivery, official time or financial/PIT eligibility.
Synced means the specified syscalls completed, not physical crash certification.
