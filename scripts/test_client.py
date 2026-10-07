#!/usr/bin/env python3
"""
LineFlow 模擬客戶端 (Test Client)
用於測試驗證：
1. WebSocket 連線與 Token 驗證
2. 斷線補償同步 (Sync by since_seq_id)
3. 即時接收 LINE 新訊息推播 (new_message)
4. 測試下發回覆 (send_message)
"""

import argparse
import asyncio
import base64
import json
import sys
import time
import uuid
import websockets

pending_screenshot_paths = {}

def print_banner():
    print("=" * 60)
    print("  🚀 LineFlow Gateway - 模擬測試客戶端 (Test Client)")
    print("=" * 60)

async def listen_messages(ws):
    """背景持續監聽來自 Server 的 WebSocket 訊息"""
    try:
        async for msg_str in ws:
            try:
                data = json.loads(msg_str)
                msg_type = data.get("type")
                if msg_type == "new_message":
                    m = data.get("data", {})
                    time_str = f" [{m.get('msg_time')}]" if m.get('msg_time') else ""
                    print(f"\n📩 [即時推播] seq_id={m.get('seq_id')} | 對象: {m.get('chat_name')} | 發言者: {m.get('sender_name')}{time_str}")
                    print(f"   內容: {m.get('content')}")
                    print("Prompt > ", end="", flush=True)

                elif msg_type == "sync_batch":
                    messages = data.get("messages", [])
                    print(f"\n🔄 [斷線補償回傳] 收到 {len(messages)} 則遺漏訊息 (since_seq_id={data.get('since_seq_id')}):")
                    for m in messages:
                        time_str = f" [{m.get('msg_time')}]" if m.get('msg_time') else ""
                        print(f"   - [seq={m.get('seq_id')}] {m.get('chat_name')} ({m.get('sender_name')}){time_str}: {m.get('content')}")
                    print("Prompt > ", end="", flush=True)

                elif msg_type == "send_result":
                    req_id = data.get("request_id")
                    status = data.get("status")
                    err = data.get("error_message")
                    if status == "success":
                        print(f"\n✅ [發送回執] 請求 {req_id} 發送成功！")
                    else:
                        print(f"\n❌ [發送回執] 請求 {req_id} 發送失敗: {err}")
                    print("Prompt > ", end="", flush=True)

                elif msg_type == "screenshot_result":
                    req_id = data.get("request_id")
                    status = data.get("status")
                    if status == "success":
                        width = data.get("width")
                        height = data.get("height")
                        fmt = data.get("format", "jpeg")
                        b64_data = data.get("image_base64")
                        out_path = pending_screenshot_paths.pop(req_id, None) or f"screenshot_{int(time.time())}.{fmt}"
                        if b64_data:
                            img_raw = base64.b64decode(b64_data)
                            with open(out_path, "wb") as f:
                                f.write(img_raw)
                            print(f"\n📸 [截圖回傳] 請求 {req_id} 成功！尺寸: {width}x{height} | 大小: {len(img_raw)} bytes")
                            print(f"   已儲存至: {out_path}")
                        else:
                            print(f"\n📸 [截圖回傳] 請求 {req_id} 成功，但未帶有影像資料")
                    else:
                        err = data.get("error_message")
                        print(f"\n❌ [截圖失敗] 請求 {req_id} 失敗: {err}")
                    print("Prompt > ", end="", flush=True)

                elif msg_type == "pong":
                    print("\n🏓 [Pong] 收到心跳回應")
                    print("Prompt > ", end="", flush=True)

                else:
                    print(f"\n📦 [收到訊息]: {data}")
                    print("Prompt > ", end="", flush=True)

            except json.JSONDecodeError:
                print(f"\n⚠️ 收到非 JSON 格式: {msg_str}")
    except websockets.ConnectionClosed:
        print("\n🔌 WebSocket 連線已中斷。")

async def interactive_loop(ws):
    """交互模式：可發送 sync、ping 或 send_message"""
    loop = asyncio.get_event_loop()
    print("\n可輸入的指令：")
    print("  1) sync <seq_id>             - 請求補償大於 seq_id 的訊息 (例: sync 0)")
    print("  2) send <target> <message>   - 測試發送文字至目標聊天室 (例: send 王小明 測試訊息)")
    print("  3) screen [filename]         - 截取目前 LINE 畫面並存檔 (例: screen 或 screen shot.jpg)")
    print("  4) ping                      - 發送心跳測試")
    print("  5) exit                      - 退出模擬客戶端\n")

    while True:
        try:
            line = await loop.run_in_executor(None, input, "Prompt > ")
            line = line.strip()
            if not line:
                continue

            parts = line.split(maxsplit=2)
            cmd = parts[0].lower()

            if cmd == "exit":
                break
            elif cmd == "ping":
                await ws.send(json.dumps({"action": "ping"}))
            elif cmd == "sync":
                since_seq_id = int(parts[1]) if len(parts) > 1 else 0
                payload = {
                    "action": "sync",
                    "since_seq_id": since_seq_id
                }
                await ws.send(json.dumps(payload))
            elif cmd == "send":
                if len(parts) < 3:
                    print("⚠️ 用法: send <目標名稱> <回覆訊息>")
                    continue
                target = parts[1]
                message = parts[2]
                req_id = f"req-{uuid.uuid4().hex[:8]}"
                payload = {
                    "action": "send_message",
                    "request_id": req_id,
                    "target": target,
                    "message": message
                }
                print(f"📤 發送指令下發中... [request_id={req_id}]")
                await ws.send(json.dumps(payload))
            elif cmd in ("screen", "screenshot"):
                filename = parts[1] if len(parts) > 1 else None
                req_id = f"req-{uuid.uuid4().hex[:8]}"
                if filename:
                    pending_screenshot_paths[req_id] = filename
                payload = {
                    "action": "screenshot",
                    "request_id": req_id,
                    "format": "jpeg",
                    "quality": 80
                }
                print(f"📸 截圖請求下發中... [request_id={req_id}]")
                await ws.send(json.dumps(payload))
            else:
                print(f"⚠️ 未知指令: {cmd}")
        except (KeyboardInterrupt, EOFError):
            break

async def main():
    parser = argparse.ArgumentParser(description="LineFlow 模擬客戶端")
    parser.add_argument("--host", default="localhost", help="Server 主機位址")
    parser.add_argument("--port", default=8000, type=int, help="Server 埠號")
    parser.add_argument("--token", required=True, help="安全 Token (set via LINEFLOW_AUTH_TOKEN env or pass explicitly)")
    parser.add_argument("--instance", default="instance1", help="實例標識")
    parser.add_argument("--tls", action="store_true", help="使用 wss:// (生產環境 nginx+TLS)")
    args = parser.parse_args()

    print_banner()
    scheme = "wss" if args.tls else "ws"
    # Token is now sent in the first WebSocket message, NOT in the URL
    uri = f"{scheme}://{args.host}:{args.port}/ws/lineflow"
    print(f"連線目標: {uri} ...")

    try:
        async with websockets.connect(uri) as ws:
            # Send auth handshake as first message
            await ws.send(json.dumps({
                "action": "auth",
                "token": args.token,
                "instance_id": args.instance
            }))
            # Wait for auth result
            auth_resp = json.loads(await ws.recv())
            if auth_resp.get("type") != "auth_ok":
                print(f"❌ 驗證失敗: {auth_resp.get('reason')}")
                sys.exit(1)
            print("🎉 連線成功並通過 Token 驗證！\n")

            listener_task = asyncio.create_task(listen_messages(ws))
            await interactive_loop(ws)
            listener_task.cancel()
    except Exception as e:
        print(f"❌ 連線失敗: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
