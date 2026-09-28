# LineFlow Gateway — Client API 變更說明

> **版本**: v1.0 → v2.0  
> **生效日期**: 2026-09-28  
> **影響範圍**: 所有 WebSocket 客戶端

---

## ⚠️ Breaking Changes — 必須更新

### 1. 連線端點：`ws://` → `wss://`

| 項目 | 舊版 | 新版 |
|------|------|------|
| 協定 | `ws://`（明文） | `wss://`（TLS 加密） |
| Host | `<server-ip>:8000` | `your-domain.duckdns.org` |
| Port | `8000` | `443`（標準 HTTPS port，可省略） |
| Path | `/ws/lineflow` | `/ws/lineflow`（不變） |

```diff
- ws://your-server-ip:8000/ws/lineflow
+ wss://your-domain.duckdns.org/ws/lineflow
```

---

### 2. 認證方式：URL Query → 第一則訊息 (Auth Handshake)

**舊版（已停用）**
```
wss://...?token=YOUR_TOKEN&instance_id=instance1
```
Token 暴露在 URL 中，會被記錄在 server log 裡。

**新版（必須使用）**

連線建立後，**第一則訊息**必須是 JSON 認證訊息：

```json
{
  "action": "auth",
  "token": "YOUR_TOKEN",
  "instance_id": "instance1"
}
```

Server 會立即回應：

```json
// 成功
{ "type": "auth_ok", "instance_id": "instance1" }

// 失敗（連線會隨即關閉）
{ "type": "auth_fail", "reason": "Invalid token" }
```

---

## 完整連線流程

```
Client                          Server
  │                               │
  │── WebSocket Connect ─────────▶│  wss://your-domain.duckdns.org/ws/lineflow
  │◀─ 101 Switching Protocols ────│
  │                               │
  │── {"action":"auth",  ────────▶│  第一則訊息必須是 auth
  │    "token":"...",             │
  │    "instance_id":"instance1"} │
  │                               │
  │◀─ {"type":"auth_ok"} ─────────│  驗證通過
  │                               │
  │  ← 正常訊息交換 →             │
  │                               │
```

---

## 各語言範例

### JavaScript / TypeScript

```javascript
const ws = new WebSocket('wss://your-domain.duckdns.org/ws/lineflow');

ws.onopen = () => {
  // 連線後立刻送 auth，不能做其他事
  ws.send(JSON.stringify({
    action: 'auth',
    token: 'YOUR_TOKEN',
    instance_id: 'instance1'
  }));
};

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  if (data.type === 'auth_ok') {
    console.log('已驗證，開始使用');
    // 現在可以正常發送其他 action
  } else if (data.type === 'auth_fail') {
    console.error('驗證失敗:', data.reason);
    ws.close();
  } else if (data.type === 'new_message') {
    console.log('新訊息:', data.data);
  }
};
```

### Python

```python
import asyncio
import json
import websockets

async def connect():
    async with websockets.connect('wss://your-domain.duckdns.org/ws/lineflow') as ws:
        # Step 1: 送 auth
        await ws.send(json.dumps({
            'action': 'auth',
            'token': 'YOUR_TOKEN',
            'instance_id': 'instance1'
        }))
        
        # Step 2: 等 auth 回應
        resp = json.loads(await ws.recv())
        if resp.get('type') != 'auth_ok':
            raise Exception(f"Auth failed: {resp.get('reason')}")
        
        print('Connected!')
        
        # Step 3: 正常使用
        async for message in ws:
            data = json.loads(message)
            # 處理 new_message, sync_batch, send_result, pong...

asyncio.run(connect())
```

### Dart / Flutter

```dart
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';

final channel = WebSocketChannel.connect(
  Uri.parse('wss://your-domain.duckdns.org/ws/lineflow'),
);

// 連線後立刻送 auth
channel.sink.add(jsonEncode({
  'action': 'auth',
  'token': 'YOUR_TOKEN',
  'instance_id': 'instance1',
}));

// 監聽回應
channel.stream.listen((message) {
  final data = jsonDecode(message);
  if (data['type'] == 'auth_ok') {
    print('Connected!');
  } else if (data['type'] == 'auth_fail') {
    print('Auth failed: ${data['reason']}');
    channel.sink.close();
  } else if (data['type'] == 'new_message') {
    print('New message: ${data['data']}');
  }
});
```

---

## 現有 Action 不變

驗證通過後，以下 action 的格式與之前完全相同：

| Action | 說明 |
|--------|------|
| `ping` | 心跳測試，server 回 `{"type":"pong"}` |
| `sync` | 補償查詢，需帶 `since_seq_id` |
| `send_message` | 發送訊息，需帶 `target` + `message` + `request_id` |

---

## 錯誤處理建議

```javascript
// 建議加上 reconnect 邏輯
ws.onclose = (event) => {
  if (event.code === 1008) {
    // Policy violation = token 錯誤，不要重連
    console.error('Token 無效，請更新 token');
    return;
  }
  // 其他原因斷線，3 秒後重連
  setTimeout(connect, 3000);
};
```

---

## Token 取得方式

Token 由伺服器管理員提供，請勿在程式碼中硬編碼。

建議儲存方式：
- **行動 App**：系統 Keychain / Keystore
- **後端服務**：環境變數 `.env`
- **前端**：`localStorage`（不推薦，僅開發用）
