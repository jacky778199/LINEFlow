# LINEFLOW - LINE Automation Gateway Server

群組白名單、Client get/set 協議、首次同步起點與排程優先權請見 [群組同步說明](docs/GROUP_SYNC.md)。

LINEFLOW 是一個輕量、模組化且可容器化部署的 LINE 自動化網關服務。它與 Android 模擬器 (Redroid) 協同運作，透過 UIAutomator2 進行 LINE 訊息監聽與自動發送，並提供基於 WebSocket 的雙向長連線、嚴格遞增序號 (`seq_id`) 與斷線訊息補償機制，專門供外部 App 或 AI 決策核心對接使用。

---

## 🏛️ 架構特色

1. **Docker Compose 一體化編排**：Redroid 模擬器與 LINEFLOW 容器同處於內部網路，一鍵啟動，方便未來整包遷移到任意 VM。
2. **斷線補償機制 (Disconnection Compensation)**：每則入庫訊息具備全域唯一遞增序號 (`seq_id`)，外部 App 斷線重連時發送 `sync` 即可補發漏收訊息。
3. **指紋防重去重 (Deduplication)**：基於 `Hash(instance_id + chat_name + sender + content + timestamp)` 進行雙重指紋比對，避免重複入庫。
4. **介面操作互斥鎖 (UI Lock)**：發送指令 (Search 直達) 與未讀輪詢監聽互相協調，保證畫面操作不衝突。
5. **安全認證**：WebSocket 連線支援 Token 金鑰驗證與 Request ID 發送回執。

---

## 📁 目錄結構

```text
LineFlow/
├── docker-compose.yml          # 編排 Redroid 與 LineFlow Server
├── Dockerfile                  # LineFlow 容器建置檔
├── requirements.txt            # Python 相依清單
├── config.yaml                 # 服務與實例配置檔 (ADB、Token、Port)
├── data/                       # 持久化資料夾 (掛載至容器)
│   ├── instance1/              # Redroid 1 內部 Android 資料
│   └── lineflow.db             # SQLite 訊息隊列庫
├── lineflow/                   # Server 核心原始碼
│   ├── config.py               # 設定檔讀取
│   ├── main.py                 # FastAPI 入口與生命週期
│   ├── core/                   # 互斥鎖與調度引擎
│   ├── db/                     # SQLite 連線與 Repository
│   ├── adb/                    # ADB 連線、LINE 輪詢監聽與發送器
│   └── ws/                     # WebSocket 連線管理與路由協議
├── scripts/
│   └── test_client.py          # 終端機模擬測試客戶端
└── tests/
    └── test_unit.py            # 單元測試套件
```

---

## 🚀 快速開始

### 1. 本地/VM 原生環境測試

```bash
# 1. 執行單元測試驗證模組
PYTHONPATH=. python3 -u tests/test_unit.py

# 2. 啟動 LINEFLOW 服務
python3 -m lineflow.main
```

### 2. Docker Compose 容器化部署

```bash
# 建置並啟動 Redroid 與 LineFlow 服務
docker compose up -d --build

# 查看服務日誌
docker compose logs -f lineflow_server
```

### 3. 執行模擬客戶端測試 WebSocket

```bash
python3 scripts/test_client.py --host localhost --port 8000 --token YOUR_SECRET_TOKEN

# 客戶端支援的交互指令：
#   sync 0                         - 補償大於 seq_id 的歷史訊息
#   send <目標名稱> <回覆文字>       - 發送文字至指定聊天室/群組
#   screen [檔名]                  - 截取目前 LINE 畫面並存為圖片
#   ping                           - 心跳測試
```

---

## 📡 WebSocket 通訊協議

* **連線 URL**：`ws://<SERVER_HOST>:8000/ws/lineflow` 或 `wss://<DOMAIN>/ws/lineflow`
* **首則認證握手**：連線後第一則訊息需送出認證：
  ```json
  {"action": "auth", "token": "YOUR_SECRET_TOKEN", "instance_id": "instance1"}
  ```
  認證成功回應：`{"type": "auth_ok", "instance_id": "instance1"}`

### 核心操作指令一覽

| 指令 (`action`) | 說明 | 參數範例 |
|---|---|---|
| `auth` | 首則握手驗證 | `{"action": "auth", "token": "...", "instance_id": "instance1"}` |
| `ping` | 心跳檢測 (回傳 `pong`) | `{"action": "ping"}` |
| `sync` | 斷線訊息補償查詢 | `{"action": "sync", "since_seq_id": 100}` |
| `send_message` | 自動導航至目標並發送文字 | `{"action": "send_message", "request_id": "req-1", "target": "業務交流群", "message": "你好"}` |
| `screenshot` | 截取 Android 模擬器當前畫面 | `{"action": "screenshot", "request_id": "req-2", "format": "jpeg", "quality": 80}` |
| `get_group_whitelist` | 取得當前監聽的群組白名單 | `{"action": "get_group_whitelist", "request_id": "req-3"}` |
| `set_group_whitelist` | 設定監聽群組名單 (替換現有名單) | `{"action": "set_group_whitelist", "request_id": "req-4", "groups": ["群組A", "群組B"]}` |
| `get_sync_status` | 查詢群組輪詢同步健康度與警報狀態 | `{"action": "get_sync_status", "request_id": "req-5"}` |
| `reset_group_checkpoint` | 重設群組檢查點 (遇 `gap_detected` 時重新建立基準) | `{"action": "reset_group_checkpoint", "request_id": "req-6", "chat_name": "群組A"}` |

---

### 詳細請求與回應範例

* **即時推播**：
  ```json
  {"type": "new_message", "data": {"seq_id": 1, "chat_name": "業務交流群", "sender_name": "王小明", "content": "您好", "msg_time": "10:30 AM", "timestamp": 1728345678}}
  ```

* **斷線補償回傳**：
  ```json
  {"type": "sync_batch", "since_seq_id": 100, "count": 2, "messages": [...]}
  ```

* **群組白名單設定與回傳**：
  ```json
  // Request
  {"action": "set_group_whitelist", "request_id": "req-4", "groups": ["台灣福祉車隊-資源群", "106-台灣福祉車衛星派遣車隊"]}

  // Response
  {"type": "group_whitelist", "request_id": "req-4", "instance_id": "instance1", "status": "success", "groups": ["台灣福祉車隊-資源群", "106-台灣福祉車衛星派遣車隊"], "revision": 2}
  ```

* **同步健康狀態查詢 (`get_sync_status`)**：
  ```json
  // Response
  {
    "type": "sync_status",
    "instance_id": "instance1",
    "request_id": "req-5",
    "groups": [
      {
        "chat_name": "台灣福祉車隊-資源群",
        "alert_level": "ok",
        "stale_seconds": 12,
        "consecutive_failures": 0,
        "consecutive_successes": 5,
        "outcome": "success",
        "reason": "synced"
      }
    ],
    "thresholds": {"warning_seconds": 180, "critical_seconds": 600}
  }
  ```

* **重設檢查點基準 (`reset_group_checkpoint`)**：
  當群組因訊息斷層超過 40 頁回溯上限而顯示 `gap_detected` 嚴重警報時，可透過此指令重設 Checkpoint，伺服器將在下次掃描時以當前最新畫面作為新基準開始記錄：
  ```json
  // Request
  {"action": "reset_group_checkpoint", "request_id": "req-6", "chat_name": "106-台灣福祉車衛星派遣車隊"}

  // Response
  {"type": "reset_group_checkpoint", "request_id": "req-6", "instance_id": "instance1", "status": "success", "chat_name": "106-台灣福祉車衛星派遣車隊"}
  ```

* **截圖請求與回傳**：
  ```json
  // Request
  {"action": "screenshot", "request_id": "req-img-001", "format": "jpeg", "quality": 80}

  // Response
  {
    "type": "screenshot_result",
    "request_id": "req-img-001",
    "status": "success",
    "format": "jpeg",
    "width": 720,
    "height": 1280,
    "size_bytes": 93081,
    "image_base64": "/9j/4AAQSkZJRg...",
    "data_uri": "data:image/jpeg;base64,/9j/4AAQSkZJRg...",
    "error_message": null
  }
  ```

---

## 🛡️ 安全與自動容錯機制

1. **聊天導航防禦 (Safe Navigation)**：
   - 發送訊息前強制退回主聊天分頁 (`safe_back_to_main`)，絕不允許於現有聊天室內點擊搜尋。
   - 嚴格鎖定主搜尋列 (`main_tab_search_bar`) 與搜尋輸入框 (`input_text`)，若畫面上出現對話框則立即阻擋，杜絕群組名稱誤輸入對話框之異常。
   - 採用 XML 階層精準鎖定 `Chats (聊天)` 區塊結果，避免誤觸個人好友或群組資料卡。
2. **草稿自動清理 (Draft Auto-Cleanup)**：
   - 進入聊天室發送文字前自動清空殘留草稿；若發送中途失敗亦自動清理，防止輸入框殘留草稿訊息。
   - 背景輪詢掃描時，若偵測到對話框留有群組名稱草稿，自動執行 `clear_text()` 清除。
3. **健康度監控與警報機制 (Sync Health Monitor)**：
   - 獨立於 UI 鎖運作之監控迴圈，每 5 秒評估各群組新鮮度。超過 180 秒未完成進入 `warning`，超過 600 秒進入 `critical`，並主動向 WebSocket Client 推播 `sync_alert` 與 `sync_recovered` 事件。

---

## 🌐 HTTP REST API

除了 WebSocket 外，伺服器亦提供 HTTP 端點便於檢測與檢視：

* **健康檢查**：
  `GET /health`
  * 回傳各實例存活狀態、健康指標、監控執行緒狀態與各群組警報概況。
* **截取即時畫面 (HTTP)**：
  `GET /instances/{instance_id}/screenshot?token=YOUR_TOKEN`
  * **參數**：
    * `token`（或 Header `Authorization: Bearer <token>`、`X-Auth-Token: <token>`）: 認證金鑰
    * `format`: `jpeg` (預設) 或 `png`
    * `quality`: `80` (預設 1~100)
    * `raw`: `true` (預設，直接回傳圖片二進位串流，可直接於瀏覽器查看)；`false` 則回傳 JSON 包含 base64 字串

