#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Anthropic → OpenAI 协议桥（独立版，仅标准库）

agent-hub 里的 Agent 都用 Anthropic SDK（`ANTHROPIC_BASE_URL` + /v1/messages，
含 function-call 工具循环）。当手上的模型服务是 OpenAI 兼容接口（如小米 MiMo
token-plan）时，用这个桥就能原样跑所有 Agent：

    python3 runtime/anthropic_bridge.py --upstream https://token-plan-cn.xiaomimimo.com/v1 \
        --key <你的key> --port 8765

    export ANTHROPIC_BASE_URL=http://127.0.0.1:8765
    export ANTHROPIC_API_KEY=任意值（桥不校验，转发时用 --key）
    python3 agents/code-reviewer/agent.py app.py --model mimo-v2.6-flash

支持：system、多轮消息、tool_use / tool_result 工具循环、stream（服务端合成 SSE）、usage。
在 agent-manager 评测平台内运行时不需要这个文件——平台网关自带同样的协议桥
（ANTHROPIC_BASE_URL 会被注入成 http://127.0.0.1:<port>/gw/anthropic）。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

STATE = {"upstream": "", "key": "", "model": "mimo-v2.6-flash", "timeout": 300}


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_result":
                parts.append(text_of(block.get("content")))
        return "\n".join(p for p in parts if p)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def anthropic_to_openai(body):
    model = body.get("model") or STATE["model"]
    messages = []
    if body.get("system"):
        messages.append({"role": "system", "content": text_of(body["system"])})

    for message in body.get("messages", []):
        role = message.get("role", "user")
        content = message.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        blocks = content if isinstance(content, list) else []
        if role == "assistant":
            text = ""
            tool_calls = []
            for block in blocks:
                if block.get("type") == "text":
                    text += str(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block.get("id") or "call_%d" % len(tool_calls),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": block.get("input") if isinstance(block.get("input"), str) else json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    })
            msg = {"role": "assistant", "content": text or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
        else:
            text = ""
            for block in blocks:
                kind = block.get("type")
                if kind == "text":
                    text += ("\n" if text else "") + str(block.get("text", ""))
                elif kind == "tool_result":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id", "")),
                        "content": text_of(block.get("content")) or "(empty)",
                    })
                elif kind == "image":
                    text += ("\n" if text else "") + "[image]"
            if text:
                messages.append({"role": "user", "content": text})

    out = {"model": model, "messages": messages, "max_tokens": body.get("max_tokens") or 4096}
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]

    tools = body.get("tools") or []
    if tools:
        out["tools"] = [{
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        } for t in tools]
        choice = body.get("tool_choice") or {}
        ctype = choice.get("type")
        if ctype == "any":
            out["tool_choice"] = "required"
        elif ctype == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": choice.get("name", "")}}
        elif ctype == "none":
            out["tool_choice"] = "none"
        else:
            out["tool_choice"] = "auto"
    return model, out


FINISH_TO_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use", "content_filter": "end_turn"}


def openai_to_anthropic(json_body, fallback_model):
    choice = (json_body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        raw_args = (call.get("function") or {}).get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args)
        except ValueError:
            parsed = {"_raw": raw_args}
        content.append({
            "type": "tool_use",
            "id": call.get("id") or "toolu_0",
            "name": (call.get("function") or {}).get("name", ""),
            "input": parsed,
        })
    if not content:
        content.append({"type": "text", "text": ""})
    usage = json_body.get("usage") or {}
    return {
        "id": "msg_%012x" % (abs(hash(json.dumps(content, ensure_ascii=False))) % (1 << 48)),
        "type": "message",
        "role": "assistant",
        "model": json_body.get("model") or fallback_model,
        "content": content,
        "stop_reason": FINISH_TO_STOP.get(str(choice.get("finish_reason", "stop")), "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
    }


def call_upstream(openai_body):
    url = STATE["upstream"].rstrip("/") + "/chat/completions"
    payload = json.dumps({**openai_body, "stream": False}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST", headers={
        "content-type": "application/json",
        "authorization": "Bearer " + STATE["key"],
    })
    try:
        with urllib.request.urlopen(request, timeout=STATE["timeout"]) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return None, "上游 %s：%s" % (exc.code, exc.read().decode("utf-8", "replace")[:400])
    except Exception as exc:  # noqa: BLE001
        return None, "上游请求失败：%s" % exc


def write_sse(handler, message):
    def send(event, data):
        handler.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8"))

    usage = message.get("usage") or {}
    send("message_start", {"type": "message_start", "message": dict(message, content=[], usage={"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0})})
    for index, block in enumerate(message.get("content") or []):
        if block.get("type") == "text":
            send("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            text = block.get("text") or ""
            for i in range(0, len(text), 400):
                send("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text[i:i + 400]}})
            send("content_block_stop", {"type": "content_block_stop", "index": index})
        elif block.get("type") == "tool_use":
            send("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": {}}})
            partial = json.dumps(block.get("input") or {}, ensure_ascii=False)
            for i in range(0, len(partial), 400):
                send("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": partial[i:i + 400]}})
            send("content_block_stop", {"type": "content_block_stop", "index": index})
    send("message_delta", {"type": "message_delta", "delta": {"stop_reason": message.get("stop_reason"), "stop_sequence": None}, "usage": {"output_tokens": usage.get("output_tokens", 0)}})
    send("message_stop", {"type": "message_stop"})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[bridge] %s\n" % (fmt % args))

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._send_json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "请求体不是合法 JSON"}})

        if self.path.startswith("/v1/messages/count_tokens"):
            return self._send_json(200, {"input_tokens": max(1, len(json.dumps(body, ensure_ascii=False)) // 3)})
        if not self.path.startswith("/v1/messages"):
            return self._send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": "只支持 /v1/messages"}})

        model, openai_body = anthropic_to_openai(body)
        upstream_json, error = call_upstream(openai_body)
        if error:
            return self._send_json(502, {"type": "error", "error": {"type": "api_error", "message": error}})
        message = openai_to_anthropic(upstream_json, model)

        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache, no-transform")
            self.send_header("connection", "keep-alive")
            self.end_headers()
            write_sse(self, message)
            return
        return self._send_json(200, message)


def main():
    parser = argparse.ArgumentParser(description="Anthropic → OpenAI 协议桥")
    parser.add_argument("--upstream", default=os.environ.get("AEP_UPSTREAM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1")
    parser.add_argument("--key", default=os.environ.get("AEP_UPSTREAM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "")
    parser.add_argument("--model", default=os.environ.get("AEP_UPSTREAM_MODEL") or "mimo-v2.6-flash")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if not args.key:
        print("提示：没有 --key，上游请求会 401（可用 --key 或 OPENAI_API_KEY 环境变量）", file=sys.stderr)
    STATE["upstream"] = args.upstream
    STATE["key"] = args.key
    STATE["model"] = args.model

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("[bridge] Anthropic 协议桥已启动：http://%s:%d  →  %s（模型 %s）" % (args.host, args.port, args.upstream, args.model))
    print("[bridge] 用法：export ANTHROPIC_BASE_URL=http://%s:%d" % (args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] 已停止")


if __name__ == "__main__":
    main()
