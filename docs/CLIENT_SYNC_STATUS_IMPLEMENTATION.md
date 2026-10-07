# LineFlow Client 同步狀態與告警實作規格

版本：第一版；核對日期：2026-10-07。
本文件以目前 server 程式實作為準，供 AI 在既有 client 專案中實作。
下文標示「client 建議」的時間、UI 與重試策略不是 server 協定保證。

## 1. 給實作 AI 的任務

請先閱讀既有 client 的 WebSocket 連線、認證、重連、訊息分派與狀態管理程式，再依本文件擴充：

1. 登入成功、重連及回到前景後，查詢每群同步狀態。
2. 處理 `sync_status`、`sync_alert`、`sync_recovered`。
3. 分開呈現 WebSocket 連線狀態、狀態資料新鮮度、各群組同步健康。
4. 對警告、嚴重告警與恢復顯示適當通知，避免重複提醒。
5. 補上解析、重連、通知去重、訂閱更新與異常處理測試。

沿用既有架構與 UI 元件，不另開一條僅供監測的 WebSocket，不改動 server。
保留既有 `new_message`、`sync_batch`、發送訊息、截圖及心跳功能。
同步狀態查詢不能取代既有訊息補同步 `sync`；兩者各自維護游標／request ID。
不得自動發送測試 LINE 訊息、刪除 server 檢查點或修改群組訂閱來解除警告。

## 2. 連線與認證

使用既有 server URL，WebSocket 路徑為 `/ws/lineflow`。正式環境沿用既有 `wss://` 位址。
連線後第一則訊息必須是認證；token 不得放在 URL 或日誌中。

```json
{"action":"auth","token":"<既有安全憑證來源>","instance_id":"instance1"}
```

成功回應：

```json
{"type":"auth_ok","instance_id":"instance1"}
```

失敗為 `auth_fail`（含 `reason`），之後關閉連線。不要對認證失敗無限快速重試。
一條連線綁定一個 instance；跨 instance 的狀態不可混在一起。
必須使用統一的 `type` 分派器，不能假設送出查詢後下一則收到的訊息就是其回應，
因為一般訊息與告警可能插入其間。

## 3. 狀態查詢協定

收到 `auth_ok` 後送出：

```json
{"action":"get_sync_status","request_id":"sync-health-1"}
```

`request_id` 使用每次查詢唯一的字串。以下是示意回應（時間與群組名稱為範例）：

```json
{
  "type":"sync_status",
  "instance_id":"instance1",
  "request_id":"sync-health-1",
  "thresholds":{"warning_seconds":180,"critical_seconds":600},
  "groups":[{
    "instance_id":"instance1",
    "chat_name":"範例群組",
    "created_at":1791378000,
    "last_attempt_at":1791378200,
    "last_success_at":1791378211,
    "last_complete_at":1791378211,
    "last_finished_at":1791378211,
    "in_progress":false,
    "consecutive_failures":0,
    "consecutive_successes":2,
    "outcome":"success",
    "reason":"synced",
    "stage":"commit",
    "duration_sec":16.2,
    "inserted":0,
    "sync_success":true,
    "alert_level":"ok",
    "effective_level":"ok",
    "stale_seconds":9
  }]
}
```

### 欄位與語意

| 欄位 | 型別 | 說明 |
|---|---|---|
| `instance_id`, `chat_name` | string | 群組識別；用兩者的組合做 key，不依陣列位置 |
| `created_at` | integer | 此監測紀錄建立時間，不是群組建立時間 |
| `last_attempt_at` | integer 或 null | 最近開始掃描的時間 |
| `last_success_at` | integer 或 null | 最近資料同步成功的時間；沒有新訊息也會更新 |
| `last_complete_at` | integer 或 null | 最近整輪成功時間，包含退出並確認主頁 |
| `last_finished_at` | integer 或 null | 最近一輪結束時間，可能成功、失敗或取消 |
| `in_progress` | boolean | 有掃描開始但尚未記錄完成；不能單憑此欄判定卡住 |
| `outcome` | string | `pending`、`success`、`failed`、`cancelled`；容許未來新值 |
| `stage` | string 或 null | 最近結果所屬階段，不是即時更新的執行進度 |
| `reason` | string 或 null | 結果原因碼，不一定是錯誤，例如 `synced` |
| `duration_sec` | number 或 null | 最近一輪耗時，單位秒 |
| `inserted` | integer | 最近一輪新增筆數；0 不代表失敗 |
| `sync_success` | boolean，可缺省 | 最近結果的資料同步是否成功；初始紀錄可能沒有此欄 |
| `consecutive_failures` | integer | 連續失敗计數；不要自行用它取代 server 的告警等级 |
| `consecutive_successes` | integer | 連續完整成功輪數；恢復通知使用此判斷 |
| `alert_level` | string | server 已記錄／準備推送的告警等級，不代表 client 已收到 |
| `effective_level` | string | 查詢當下計算出的等級，UI 以此為準 |
| `stale_seconds` | integer | 距最近完整成功的秒數；從未成功時以 `created_at` 起算 |

所有時間戳是 Unix **秒**，不是毫秒。JavaScript 使用 `new Date(value * 1000)`；
null 顯示「尚無紀錄」，不要顯示 1970 年。依使用者時區顯示，不自行再加固定 8 小時。

掃描進行中，`outcome`、`reason`、`stage` 等可能仍是上一輪結果。
`last_success_at` 更新但 `last_complete_at` 未更新，可能是寫入成功、退出失敗。
`baseline` 是首次建立觀察基準，不代表已補齊歷史訊息。

## 4. 告警與恢復推播

server 每約 5 秒獨立評估一次，以「最後完整成功」為基準：

- 未滿 180 秒：`ok`。
- 達 180 秒：`warning`。
- 達 600 秒：`critical`。
- 已在告警中：連續兩輪完整成功才恢復；第一輪成功後可能仍保留告警。

門檻以查詢回應 `thresholds` 為準。不要因第一輪成功或本地計時歸零就自行清除告警。
目前不是「連續失敗三次就告警」的規則。`synced, inserted=0` 算成功。

推播包裝格式：

```typescript
type SyncAlertEvent = { type: "sync_alert"; data: GroupSyncState };
type SyncRecoveredEvent = { type: "sync_recovered"; data: GroupSyncState };
```

其中 `GroupSyncState` 即上節 `groups` 中的完整物件。推播的 `instance_id`、`chat_name`
位於 **`data` 內**，不是外層。事件沒有 `request_id`。

`sync_alert` 的 `data.alert_level` 為 warning 或 critical；
`sync_recovered` 的 `data.alert_level` 為 ok。以 `data.effective_level` 更新狀態。

只在等級變更時推播，不是每輪成功／失敗都推播。事件没有 event ID、序號、版本或
獨立的事件時間，也沒有 ACK／離線補送機制。斷線後必須重新查詢全量狀態。

## 5. Client 狀態管理與重連

### 必要流程

1. socket 開啟後送出 auth；認證完成前不送業務查詢。
2. `auth_ok` 後維持原本的訊息補同步，另外送出一次 `get_sync_status`。
3. 收到符合目前 connection generation、instance 與 request ID 的完整 snapshot 後，
   **替換該 instance 的群組集合**，移除已不在 snapshot 的群組。
4. 收到有效推播時，更新其所屬群組，其他群組保持原狀。
5. `set_group_whitelist` 成功後重新查詢，不要在 client 自行猜測監測紀錄。
6. 斷線後停止相關 timer、取消 pending request；保留舊值作參考，但標記「未連線／資料已過期」。
7. 重連使用新的 connection generation，忽略舊 socket 的任何回呼；重新 auth 與查詢。

### Client 建議的刷新與逾時策略

- 前景且已認證時每 30 秒查詢一次，避免「最後成功時間」只在告警時才更新。
- 每個 instance 同時最多一個狀態查詢；不要多個頁面各自建立輪詢器。
- 查詢 10 秒無回應：標記「狀態查詢逾時」，不是「群組同步失敗」。
  伺服器可能正在處理同連線上的發送等較慢指令，不能只憑這個逾時判定斷線。
- 60 秒未取得 snapshot：標示詳細狀態已過期；以 client monotonic clock 計算資料新鮮度。
- 回到前景即刷新；背景執行與通知依既有平台政策，不假設 WebSocket 永遠存活。
- 沿用既有 ping/pong 與重連退避；不要另建會相互競爭的 reconnect loop。

### 競態處理

server 未提供可全域排序的版本，因此不能用 `last_complete_at` 排序所有事件，
因為警告與升級可以共用相同的最後成功時間。

若查詢等待期間收到推播：立即顯示推播，將該群組標記為「查詢中有變動」。
snapshot 到達時先更新未變動的群組；對變動群組暫保留推播狀態，再合併排程一次刷新。
後續無競態的 snapshot 才作為全量權威結果。不要無限制立即重試。
已知從訂閱移除的群組若收到延遲事件，先刷新確認，不重新顯示已刪群組的通知。

## 6. UI 與通知規則

分開管理三種狀態：

| 維度 | 例子 |
|---|---|
| 連線 | 連線中、已連線、重連中、認證失敗 |
| 資料新鮮度 | 尚未取得、最新、已過期 |
| 每群同步 | 等待首次掃描、正常、警告、嚴重異常 |

初始 `last_complete_at=null` 且等級 ok 應顯示「等待首次掃描」，不是已驗證正常。
warning/critical 優先顯示；單次 failed 且等級仍 ok，可顯示「上輪失敗、等待重試」提示，
不要提前自行升級 server 告警。沒有訂閱群組顯示「尚未設定監測群組」。

建議群組卡片顯示：名稱、健康等級、最近完整成功時間、是否掃描中、最近結果原因。
斷線或詳細資料過期時，不要繼續以綠色暗示「即時正常」。保留舊告警並加上過期標示。
`stale_seconds` 不是「最後一則 LINE 訊息的年齡」，也不是畫面狀態資料的新鮮度。

通知策略：

- 首次 warning 通知一次；升至 critical 再通知一次。
- 同一群組、同一告警等級不重複彈通知；snapshot 刷新不應洗版。
- `sync_recovered` 清除群組告警；只有 client 曾呈現過該次告警才彈一次恢復通知。
- 首次／重連 snapshot 發現異常，顯示常駐警告。可依既有通知策略提示一次，
  但不能每次重連都重複彈同級通知。
- snapshot 已恢復而 client 沒收到恢復事件時，仍應清除舊警告；不要偽造收到過推播。
- 若需要跨 app 重啟去重，可儲存最近「已通知等級」；這不是 server 支援的 exactly-once 保證。
- 原生背景通知權限沿用既有機制；本功能不等同 FCM/APNs 推播服務。

## 7. 原因碼與降級處理

目前可見的結果原因包含以下值，但請接受任意未知字串：

| 原因 | 建議顯示 |
|---|---|
| `synced` | 同步成功 |
| `baseline` | 已建立初始觀察基準 |
| `gap_detected` | 無法接續既有訊息紀錄 |
| `no_readable_messages` | 畫面沒有可讀取的文字訊息 |
| `return_to_main_failed` | 無法確認返回聊天主頁 |
| `verify_latest_failed` / `verify_latest_error` | 無法確認最新訊息位置 |
| `search_failed` / `search_error` | 搜尋未完成 |
| `enter_conversation_failed` / `enter_conversation_error` | 無法確認目標聊天室 |
| `device_failed` / `device_error` | 裝置檢查失敗 |
| `commit_error` | 寫入或同步交易失敗 |
| `interrupted` / `stale` / `stale_subscription` | 掃描取消或訂閱已改變 |

未知原因顯示通用說明與原始代碼，不能導致解析失敗。
reason 是上一輪結果，不一定解釋當下停滯；例如卡在進行中的掃描時，reason 仍可能是 synced。
不要顯示「synced 導致故障」，應搭配 in_progress、最後成功時間與告警等級。

未知 `type` 交由既有處理器或忽略；新增欄位不得讓舊 client 崩潰。
必要身份欄位／等級缺失時，不覆寫正常資料為 ok；標記資料異常並排程重新查詢。
不得記錄完整 auth payload、token 或無關訊息內容來除錯。

## 8. 健康檢查（可選的輔助整合）

`GET /health` 不提供群組細節。HTTP 200 / `status: ok` 表示目前總體判斷健康；
HTTP 503 / `status: degraded` 表示裝置、輪詢、監測或群組同步異常。
503 是合法的健康結果，仍應解析 JSON；不是 token 過期，不能觸發登出。

```json
{
  "status":"degraded",
  "instances":{
    "instance1":{
      "alive":true,
      "serial":"redroid_line1:5555",
      "healthy":false,
      "monitor_alive":true,
      "poll_alive":true,
      "group_count":2,
      "warning_groups":1,
      "critical_groups":0,
      "pending_groups":0
    }
  }
}
```

健康檢查 ok 不應覆寫 WebSocket 中較新的群組告警，也不代表所有群組已有第一次成功掃描；
初始寬限期間可能 `pending_groups>0`。网络逾時與 HTTP 503 要分開呈現。

## 9. 驗收測試清單

- [ ] auth_ok 後送一次狀態查詢，request ID 正確配對；一般訊息插入期間不影響查詢。
- [ ] 正確解析 null 時間、缺省 sync_success、未知欄位與未知 reason。
- [ ] inserted=0 仍顯示成功；pending 顯示等待首次掃描。
- [ ] warning、critical 各通知一次；同級重複事件／週期 snapshot 不重複提醒。
- [ ] 第一輪成功但 server 仍 warning 時保持警告；收到恢復後清除。
- [ ] 重連後查詢、更新錯過的告警／恢復，不依賴離線事件補送。
- [ ] socket 斷線或查詢過期時不維持即時綠燈；舊 socket 回呼不能覆寫新連線。
- [ ] 同一 instance 最多一個 pending 查詢；頁面切換不累積 timer/listener。
- [ ] snapshot 與推播交錯時不讓舊查詢覆蓋新事件，並觸發一次合併刷新。
- [ ] 全量 snapshot 移除未訂閱群組；instance 間相同群組名稱不互相污染。
- [ ] last_success_at 更新、last_complete_at 未更新時，能呈現退出失敗。
- [ ] `/health` 的 503 被視為 degraded 而非登出；HTTP 網路錯誤有獨立提示。
- [ ] 原本新訊息、歷史補同步、發送、截圖及 ping/pong 功能不回歸。

交付時請提供：改動檔案、畫面行為、事件分派實作位置、測試結果與尚未支援的平台限制。

## 10. Server 原始碼對照

- `lineflow/ws/router.py`：auth、get_sync_status 請求／回應。
- `lineflow/db/sync_status.py`：欄位、180／600 秒及恢復規則。
- `lineflow/core/engine.py`：監測週期與通知發送。
- `lineflow/ws/connection.py`：按 instance 廣播。
- `lineflow/main.py`：健康檢查及 HTTP 狀態碼。
- `docs/SYNC_HEALTH.md`：server 功能概要。
