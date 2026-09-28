# LINEFLOW - LINE Automation Gateway Server

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
* **即時推播**：`{"type": "new_message", "data": {"seq_id": 1, ...}}`
* **斷線補償請求**：`{"action": "sync", "since_seq_id": 100}`
* **斷線補償回傳**：`{"type": "sync_batch", "messages": [...]}`
* **發送回覆請求**：`{"action": "send_message", "request_id": "req-001", "target": "業務交流群", "message": "你好"}`
* **發送執行回執**：`{"type": "send_result", "request_id": "req-001", "status": "success"}`
