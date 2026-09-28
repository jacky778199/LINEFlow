# LineFlow Android App 串接手冊

本手冊說明 Android App 如何以 **App 主動發起連線** 的方式，透過 WebSocket 串接 LineFlow Python Server。本文只涵蓋目前 Server 已實作的行為。

## 1. 整合目標與資料流

Android App 維持一條到 LineFlow Server 的 WebSocket 長連線：

```text
Android App ──主動建立 WSS/WS 連線──> LineFlow Server ──ADB/UIAutomator──> LINE
     │                                      │
     ├─ 接收即時 LINE 訊息 <──────────────────┤
     ├─ 斷線後補回漏收訊息 <──────────────────┤
     └─ 請 Server 發送 LINE 訊息 ─────────────>┘
```

一條連線只會訂閱一個 `instance_id`。目前預設值是 `instance1`；它代表 Server 中的一個 LINE／Android 模擬器實例，不是 Android App 的安裝實例。

## 2. 連線資訊

| 項目 | 值 |
| --- | --- |
| WebSocket 路徑 | `/ws/lineflow` |
| WebSocket 協議 | 正式環境使用 `wss`；開發環境可使用 `ws` |
| 驗證參數 | `token`（必填） |
| 實例參數 | `instance_id`（選填，預設 `instance1`） |
| 訊息編碼 | UTF-8 JSON 文字訊框 |
| HTTP 健康檢查 | `GET /health` |

完整 URL 組成如下：

```text
wss://<SERVER_HOST>/ws/lineflow?token=<ACCESS_TOKEN>&instance_id=<INSTANCE_ID>
```

若未透過反向代理且 Server 直接對外暴露 8000 埠，則主機需包含連接埠，例如：

```text
ws://<SERVER_HOST>:8000/ws/lineflow?token=<ACCESS_TOKEN>&instance_id=instance1
```

`<SERVER_HOST>` 必須是 Android 裝置可連到的 IP 或網域名稱。實機測試時不可使用 `localhost` 指向 Server；Android 裝置上的 `localhost` 是裝置自己。

> 安全提醒：目前 Server 從 URL query string 讀取 `token`。請勿把 Token 寫死在 APK、原始碼、截圖或日誌中。正式環境應透過 TLS (`wss`) 與安全設定下發 Token；後續 Server 版本可再改為短效憑證與非 URL 的驗證方式。

## 3. Android 端建議責任分層

| 元件 | 責任 |
| --- | --- |
| `WebSocketConnectionManager` | 唯一持有連線、送出 JSON、解析事件、心跳與重連 |
| 本機資料庫 | 保存收到的訊息、每個 `instance_id` 的最後處理 `seq_id`、待送請求狀態 |
| Repository／Use Case | 執行同步、去重、將事件提供給 UI 與商業邏輯 |
| UI／ViewModel | 顯示訊息、連線狀態與發送結果；不直接管理 Socket |

同一個 `instance_id` 在 App 內應只維持一條連線，避免多個畫面各自連線造成重複事件及多餘資源使用。

## 4. 連線與初始化流程

1. 取得 Server 位址、Token 與欲訂閱的 `instance_id`。
2. 建立 WebSocket。
3. 連線成功後，從本機資料庫取得該 `instance_id` 最後「已成功保存並處理」的 `seq_id`；初次使用時為 `0`。
4. 立即送出 `sync` 請求補齊歷史訊息。
5. 收到 `sync_batch` 後，依 `seq_id` 由小至大寫入本機資料庫；成功後更新本機游標。
6. 持續接收 `new_message` 即時事件，使用相同的去重與寫入規則處理。
7. 連線中定期送出 `ping`；若逾時未收到 `pong`，視為連線失效並重新連線。

App 不應把「WebSocket 已開啟」等同於「同步完成」。在完成首次 `sync_batch` 前，UI 可顯示「正在同步」狀態。

## 5. 訊息協議

### 5.1 App → Server：心跳

```json
{"action":"ping"}
```

Server 回應：

```json
{"type":"pong"}
```

建議 App 每 20 至 30 秒送一次；超過約 60 秒未收到任何訊息或 `pong`，主動關閉並進入重連流程。

### 5.2 App → Server：斷線補償同步

```json
{"action":"sync","since_seq_id":125}
```

欄位說明：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `action` | string | 固定為 `sync` |
| `since_seq_id` | integer | 本機最後完整處理的序號；首次使用為 `0` |

Server 回應：

```json
{
  "type":"sync_batch",
  "since_seq_id":125,
  "count":2,
  "messages":[
    {
      "seq_id":126,
      "msg_hash":"...",
      "instance_id":"instance1",
      "chat_type":"direct",
      "chat_name":"王小明",
      "sender_name":"王小明",
      "msg_time":"10:57 AM",
      "content":"你好",
      "timestamp":1790000000,
      "delivered":0,
      "created_at":"..."
    }
  ]
}
```

目前每次 `sync` 最多回傳 500 筆訊息。若 `count` 為 500，App 應以此次回傳中最大的 `seq_id` 再送一次 `sync`，直到回傳數量小於 500；這樣才能確保大量離線期間的訊息全數補回。

### 5.3 Server → App：即時新訊息

```json
{
  "type":"new_message",
  "data":{
    "seq_id":126,
    "msg_hash":"...",
    "instance_id":"instance1",
    "chat_type":"group",
    "chat_name":"業務交流群",
    "sender_name":"王小明",
    "msg_time":"10:57 AM",
    "content":"你好",
    "timestamp":1790000000,
    "delivered":0
  }
}
```

`new_message.data` 和 `sync_batch.messages[]` 的核心欄位相同。訊息欄位意義：

| 欄位 | 說明 |
| --- | --- |
| `seq_id` | Server 資料庫產生的遞增訊息序號。用於排序、補償與去重。 |
| `msg_hash` | Server 端去重指紋；App 可保存作為輔助去重資訊。 |
| `instance_id` | 訊息來源的 LINE 實例。 |
| `chat_type` | 目前可能為 `direct` 或 `group`。 |
| `chat_name` | 聊天室或群組名稱。 |
| `sender_name` | 發話者名稱；Server 無法辨識時可能使用聊天室名稱或 `Me`。 |
| `msg_time` | LINE 畫面讀取到的顯示時間，可能為空字串。 |
| `content` | 文字訊息內容。 |
| `timestamp` | Server 擷取／入庫時間，Unix 秒級時間戳。 |

### 5.4 App → Server：發送 LINE 訊息

```json
{
  "action":"send_message",
  "request_id":"2a46b813-7bd5-4f7b-9a94-5b06cb30fc17",
  "target":"業務交流群",
  "message":"您好，已收到您的訊息。"
}
```

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `action` | string | 固定為 `send_message` |
| `request_id` | string | App 產生的唯一 ID，用來將回執對回原始操作；建議 UUID。 |
| `target` | string | Server 以 LINE 聊天室／群組名稱搜尋的目標名稱。 |
| `message` | string | 要送出的文字內容。 |

Server 回執：

```json
{
  "type":"send_result",
  "request_id":"2a46b813-7bd5-4f7b-9a94-5b06cb30fc17",
  "status":"success",
  "error_message":null
}
```

失敗時：

```json
{
  "type":"send_result",
  "request_id":"2a46b813-7bd5-4f7b-9a94-5b06cb30fc17",
  "status":"error",
  "error_message":"具體錯誤原因"
}
```

收到 `success` 前，App 應將指令顯示為「傳送中」。由於目前 Server 沒有以 `request_id` 做持久化冪等保護，若送出後連線立刻中斷，App 無法分辨 Server 是否已執行；此情況不應自動無限重送，以免 LINE 出現重複訊息。建議改由使用者確認重送，或待 Server 新增查詢／冪等能力後再自動補送。

## 6. 訊息一致性與去重規則

WebSocket 即時推送和 `sync` 可能在時間上重疊，因此 App 必須採用「至少一次傳遞、客戶端冪等處理」模型：

1. 對每個 `instance_id` 保存最後成功處理的 `seq_id`。
2. 收到訊息後先以本機唯一鍵（建議 `instance_id + seq_id`）檢查是否已存在。
3. 尚未存在才寫入本機資料庫並觸發後續處理。
4. 僅在交易成功提交後，更新最後處理的 `seq_id`。
5. 若收到較舊或相同的 `seq_id`，安全忽略即可。

不要以 `msg_time` 或 `timestamp` 作為同步游標；應只使用 `seq_id`。

## 7. 斷線與重連策略

下列情況都應進入重連：Socket 關閉、連線失敗、讀取錯誤、心跳逾時、網路從 Wi-Fi 切換到行動網路。

建議使用具上限的指數退避加隨機抖動：首次可約 1 秒，再逐步增加到 2、4、8、16、30 秒，之後維持約 30 秒並加入少量隨機時間。每次重新連上都必須再執行 `sync`，即使 App 認為斷線時間很短。

Android App 在前景和背景的連線策略應由產品需求決定：

| 需求 | 建議 |
| --- | --- |
| 只需使用 App 時即時 | 僅在前景保持連線；回到前景後同步。 |
| 需背景即時處理／自動回覆 | 使用符合 Android 規範的前景服務，顯示常駐通知，並處理省電策略。 |
| 不要求秒級即時 | 斷線期間不維持 Socket；下次啟動或喚醒後同步。 |

## 8. 錯誤與連線狀態

| 情境 | Server 現行行為 | App 處理建議 |
| --- | --- | --- |
| Token 無效 | 以 WebSocket close code `1008` 關閉 | 停止盲目重試，重新取得有效憑證。 |
| `instance_id` 不存在或未啟用 | 以 close code `1003` 關閉 | 提示設定錯誤，改用可用實例。 |
| `send_message` 缺少 `target` 或 `message` | 回傳 `send_result`，`status=error` | 修正輸入，不要當作網路錯誤重連。 |
| JSON 格式錯誤／未知 `action` | Server 記錄後忽略，通常不會回覆 | App 在送出前做 JSON 與欄位驗證；對逾時請求顯示失敗。 |
| Server 或網路中斷 | Socket 關閉或讀寫失敗 | 套用退避重連；成功後先 `sync`。 |

建議 App 對外呈現至少五種狀態：`Disconnected`、`Connecting`、`Synchronizing`、`Connected`、`Reconnecting`。認證失敗或設定錯誤可另顯示為不可自動恢復的 `Error`。

## 9. 部署與 Android 網路注意事項

- 正式環境請以網域、TLS 憑證與反向代理提供 `wss://`；反向代理必須允許 WebSocket Upgrade 並設定足夠長的 idle timeout。
- App 的 Android Manifest 必須具備網路存取權限。
- `ws://` 明文連線在 Android 9 以上可能受到 cleartext traffic 限制；它只適合受控的本機開發環境。實機與正式環境請優先使用 `wss://`。
- 若 Server 跑在 Docker，Android 對外連線使用宿主機公開的網域／IP 與對外映射埠，不使用 Docker service 名稱。
- `GET /health` 可用來提供設定畫面或診斷功能；它只表示 Server 與已設定實例的基本狀態，不取代 WebSocket 的連線與同步檢查。

## 10. 上線前驗收清單

- [ ] App 能以正確的 Token、`instance_id` 建立連線。
- [ ] 初次連線以 `since_seq_id: 0` 完成同步。
- [ ] App 重啟後可從保存的 `seq_id` 補回斷線期間訊息。
- [ ] 同一訊息同時經由推送與同步到達時，只顯示／處理一次。
- [ ] 成功與失敗的 `send_result` 都能正確對應原 `request_id`。
- [ ] Token 失效不會造成無限重試。
- [ ] 切換網路或 Server 重啟後，App 能退避重連並自動同步。
- [ ] 正式版只使用 `wss://`，且不在日誌紀錄完整連線 URL 或 Token。

