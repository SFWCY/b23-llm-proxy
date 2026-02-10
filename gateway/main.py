import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional, List, AsyncIterator

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "http://llmapi.bilibili.co/v1").rstrip("/")
DATA_DIR = os.getenv("DATA_DIR", "./data")
KEYS_FILE = os.getenv("KEYS_FILE", os.path.join(DATA_DIR, "keys.json"))
EVENTS_FILE = os.getenv("EVENTS_FILE", os.path.join(DATA_DIR, "events.jsonl"))

# Admin token for /admin/* endpoints
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

# Event logging configuration
MAX_LOG_CHUNKS = int(os.getenv("MAX_LOG_CHUNKS", "50"))
MAX_LOG_FIELD_SIZE = int(os.getenv("MAX_LOG_FIELD_SIZE", "2000"))
FULL_TRACE_LOGGING = os.getenv("FULL_TRACE_LOGGING", "false").lower() == "true"

# 允许略超：只做轻量预检（可选）；MVP 先不拦截，后续接入滚动30天统计再开启
ENABLE_QUOTA_PRECHECK = os.getenv("ENABLE_QUOTA_PRECHECK", "false").lower() == "true"

# ---------- key store (in-memory) ----------

class KeyStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()
        self._keys: Dict[str, Dict[str, Any]] = {}  # api_key -> {user_id, ...}
        self._mtime: float = 0.0

    async def load(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if not os.path.exists(self.path):
            # initialize empty
            async with self._lock:
                self._keys = {}
                self._mtime = time.time()
            await self._persist({})
            return

        st = os.stat(self.path)
        async with self._lock:
            if st.st_mtime <= self._mtime and self._keys:
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # data format: { "keys": [ {"api_key": "...", "user_id": "...", "active": true, ...}, ... ] }
            keys = {}
            for item in data.get("keys", []):
                if not item.get("active", True):
                    continue
                api_key = item.get("api_key")
                user_id = item.get("user_id")
                if api_key and user_id:
                    keys[api_key] = item
            self._keys = keys
            self._mtime = st.st_mtime

    async def _persist(self, raw: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    async def upsert_key(self, api_key: str, user_id: str, active: bool = True, extra: Optional[Dict[str, Any]] = None):
        extra = extra or {}
        # persist as list style for easy human edit
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"keys": []}

        updated = False
        for item in data.get("keys", []):
            if item.get("api_key") == api_key:
                item["user_id"] = user_id
                item["active"] = active
                item.update(extra)
                updated = True
                break
        if not updated:
            data.setdefault("keys", []).append({"api_key": api_key, "user_id": user_id, "active": active, **extra})

        await self._persist(data)
        await self.load()

    async def resolve_user(self, api_key: str) -> Optional[Dict[str, Any]]:
        await self.load()
        async with self._lock:
            return self._keys.get(api_key)

keystore = KeyStore(KEYS_FILE)

# ---------- metering event queue -> jsonl ----------

@dataclass
class MeterEvent:
    request_id: str
    protocol: str  # openai_chat | anthropic_messages
    client_app: str
    user_id: str
    upstream_id: Optional[str]
    model: Optional[str]
    started_at_ms: int
    ended_at_ms: int
    latency_ms: int
    status_code: int
    error: Optional[str]
    prompt: Any  # store raw request json or subset
    response: Any  # store raw response json or subset
    usage: Optional[Dict[str, Any]]  # {prompt_tokens, completion_tokens, total_tokens, ...}

event_queue: "asyncio.Queue[MeterEvent]" = asyncio.Queue(maxsize=10000)

async def event_writer_loop():
    os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
    while True:
        ev = await event_queue.get()
        try:
            line = json.dumps(asdict(ev), ensure_ascii=False)
            with open(EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # don't crash writer
            pass
        finally:
            event_queue.task_done()

# ---------- helpers ----------

def now_ms() -> int:
    return int(time.time() * 1000)

def limit_log_field(value: Any) -> Any:
    """Limit the size of logged fields to prevent unbounded growth."""
    if FULL_TRACE_LOGGING:
        return value
    
    if value is None:
        return value
    
    # For strings, limit size
    if isinstance(value, str):
        if len(value) > MAX_LOG_FIELD_SIZE:
            return value[:MAX_LOG_FIELD_SIZE] + f"... (truncated, {len(value)} total chars)"
        return value
    
    # For dicts/lists, convert to JSON and limit
    if isinstance(value, (dict, list)):
        try:
            json_str = json.dumps(value, ensure_ascii=False)
            if len(json_str) > MAX_LOG_FIELD_SIZE:
                return json_str[:MAX_LOG_FIELD_SIZE] + f"... (truncated, {len(json_str)} total chars)"
            return value
        except Exception:
            return str(value)[:MAX_LOG_FIELD_SIZE]
    
    return value

def extract_bearer_or_key(authorization: Optional[str], x_api_key: Optional[str]) -> Optional[str]:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    return None

def get_client_app(x_client_app: Optional[str], request: Request) -> str:
    if x_client_app:
        return x_client_app.strip()
    # fallback: path-based
    if request.url.path.startswith("/anthropic/") or request.url.path.startswith("/v1/messages"):
        return "claude-code"
    return "unknown"

def openai_usage_from_json(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    usage = obj.get("usage")
    if isinstance(usage, dict) and ("prompt_tokens" in usage or "completion_tokens" in usage):
        return usage
    return None

def anthropic_usage_from_openai_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    # Anthropic style: input_tokens/output_tokens
    return {
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
    }

# ---------- app ----------

app = FastAPI(title="b23-llm-proxy", version="0.1.0")

@app.on_event("startup")
async def startup():
    await keystore.load()
    asyncio.create_task(event_writer_loop())

@app.get("/healthz")
async def healthz():
    return {"ok": True, "upstream": UPSTREAM_BASE_URL}

# --- key management (simple, for ops; future: move to management platform) ---

@app.post("/admin/keys/upsert")
async def admin_keys_upsert(
    payload: Dict[str, Any],
    x_admin_token: Optional[str] = Header(default=None)
):
    # Require admin token for security
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not configured on server")
    
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing x-admin-token header")
    
    api_key = payload.get("api_key")
    user_id = payload.get("user_id")
    active = bool(payload.get("active", True))
    if not api_key or not user_id:
        raise HTTPException(status_code=400, detail="api_key and user_id required")
    extra = {k: v for k, v in payload.items() if k not in ("api_key", "user_id", "active")}
    await keystore.upsert_key(api_key=api_key, user_id=user_id, active=active, extra=extra)
    return {"ok": True}

# ---------- OpenAI Chat Completions passthrough ----------

@app.post("/v1/chat/completions")
async def openai_chat_completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
    x_client_app: Optional[str] = Header(default=None),
):
    started = now_ms()
    client_app = get_client_app(x_client_app, request)
    req_id = str(uuid.uuid4())

    api_key = extract_bearer_or_key(authorization, x_api_key)
    if not api_key:
        raise HTTPException(status_code=401, detail="missing api key")

    user = await keystore.resolve_user(api_key)
    if not user:
        raise HTTPException(status_code=401, detail="invalid api key")
    user_id = user["user_id"]

    body = await request.json()
    stream = bool(body.get("stream", False))

    upstream_url = f"{UPSTREAM_BASE_URL}/chat/completions"

    headers = {
        # upstream expects its own auth; if your upstream uses same key, set it here
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=None) as client:
        if not stream:
            try:
                r = await client.post(upstream_url, headers=headers, json=body)
                ended = now_ms()
                content = r.json()
                usage = openai_usage_from_json(content)
                model = content.get("model") or body.get("model")
                upstream_id = content.get("id")

                await safe_enqueue_event(MeterEvent(
                    request_id=req_id,
                    protocol="openai_chat",
                    client_app=client_app,
                    user_id=user_id,
                    upstream_id=upstream_id,
                    model=model,
                    started_at_ms=started,
                    ended_at_ms=ended,
                    latency_ms=ended - started,
                    status_code=r.status_code,
                    error=None if r.status_code < 400 else limit_log_field(json.dumps(content, ensure_ascii=False)),
                    prompt=limit_log_field(body),
                    response=limit_log_field(content),
                    usage=usage,
                ))

                return JSONResponse(status_code=r.status_code, content=content)
            except httpx.HTTPError as e:
                ended = now_ms()
                await safe_enqueue_event(MeterEvent(
                    request_id=req_id,
                    protocol="openai_chat",
                    client_app=client_app,
                    user_id=user_id,
                    upstream_id=None,
                    model=body.get("model"),
                    started_at_ms=started,
                    ended_at_ms=ended,
                    latency_ms=ended - started,
                    status_code=502,
                    error=limit_log_field(str(e)),
                    prompt=limit_log_field(body),
                    response=None,
                    usage=None,
                ))
                raise HTTPException(status_code=502, detail=str(e))

        # streaming: raw passthrough SSE + capture final usage if present in chunks (best-effort)
        async def stream_generator() -> AsyncIterator[bytes]:
            nonlocal started
            ended: Optional[int] = None
            status_code: int = 200
            model: Optional[str] = body.get("model")
            upstream_id: Optional[str] = None
            usage: Optional[Dict[str, Any]] = None
            chunks: List[str] = []  # store raw SSE lines (optional; can be big)
            buffer = ""  # buffer for incomplete lines

            try:
                async with client.stream("POST", upstream_url, headers=headers, json=body) as resp:
                    status_code = resp.status_code
                    async for b in resp.aiter_bytes():
                        # forward
                        yield b
                        # capture (optional)
                        try:
                            s = b.decode("utf-8", errors="ignore")
                            buffer += s
                            
                            # Process complete lines
                            while "\n" in buffer:
                                line, buffer = buffer.split("\n", 1)
                                line = line.strip()
                                
                                if not line or not line.startswith("data: "):
                                    continue
                                
                                data = line[6:].strip()
                                if not data or data == "[DONE]":
                                    continue
                                
                                # Store limited chunks
                                if len(chunks) < MAX_LOG_CHUNKS:
                                    chunks.append(line)
                                
                                # Try to extract usage, model, and id from chunk
                                try:
                                    obj = json.loads(data)
                                    
                                    # Capture usage if present
                                    u = openai_usage_from_json(obj)
                                    if u:
                                        usage = u
                                    
                                    # Capture model if present
                                    if obj.get("model"):
                                        model = obj["model"]
                                    
                                    # Capture id if present
                                    if obj.get("id"):
                                        upstream_id = obj["id"]
                                except Exception:
                                    pass
                        except Exception:
                            pass

                ended = now_ms()
                await safe_enqueue_event(MeterEvent(
                    request_id=req_id,
                    protocol="openai_chat",
                    client_app=client_app,
                    user_id=user_id,
                    upstream_id=upstream_id,
                    model=model,
                    started_at_ms=started,
                    ended_at_ms=ended,
                    latency_ms=ended - started,
                    status_code=status_code,
                    error=None if status_code < 400 else "upstream streaming error",
                    prompt=limit_log_field(body),
                    response=limit_log_field({"sse_chunks": chunks}) if chunks else None,
                    usage=usage,
                ))
            except httpx.HTTPError as e:
                ended = now_ms()
                await safe_enqueue_event(MeterEvent(
                    request_id=req_id,
                    protocol="openai_chat",
                    client_app=client_app,
                    user_id=user_id,
                    upstream_id=None,
                    model=model,
                    started_at_ms=started,
                    ended_at_ms=ended,
                    latency_ms=ended - started,
                    status_code=502,
                    error=limit_log_field(str(e)),
                    prompt=limit_log_field(body),
                    response=None,
                    usage=None,
                ))
                # streaming path: after partial output, cannot raise cleanly; just stop.
                return

        return StreamingResponse(stream_generator(), media_type="text/event-stream")

async def safe_enqueue_event(ev: MeterEvent) -> None:
    try:
        event_queue.put_nowait(ev)
    except asyncio.QueueFull:
        # drop on overload (do not impact main path)
        pass

# ---------- Anthropic Messages endpoint (Claude Code) -> OpenAI Chat Completions ----------

@app.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
    x_client_app: Optional[str] = Header(default=None),
):
    """
    Minimal Anthropic Messages API compatibility layer for Claude Code:
    - Accepts Anthropic-style payload.
    - Converts to OpenAI /v1/chat/completions request.
    - Converts response back to Anthropic message format.
    Streaming: returns Anthropic SSE format? (MVP: return OpenAI SSE passthrough is often not compatible)
    For Claude Code, you usually need Anthropic SSE. We implement a simple Anthropic SSE wrapper.
    """
    started = now_ms()
    client_app = get_client_app(x_client_app, request)
    req_id = str(uuid.uuid4())

    api_key = extract_bearer_or_key(authorization, x_api_key)
    if not api_key:
        raise HTTPException(status_code=401, detail="missing api key")

    user = await keystore.resolve_user(api_key)
    if not user:
        raise HTTPException(status_code=401, detail="invalid api key")
    user_id = user["user_id"]

    body = await request.json()
    stream = bool(body.get("stream", False))
    model = body.get("model")

    # Convert Anthropic -> OpenAI messages
    openai_body = anthropic_to_openai_chat(body)

    upstream_url = f"{UPSTREAM_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=None) as client:
        if not stream:
            r = await client.post(upstream_url, headers=headers, json=openai_body)
            ended = now_ms()
            content = r.json()
            usage = openai_usage_from_json(content) or {}
            upstream_id = content.get("id")
            upstream_model = content.get("model") or openai_body.get("model")

            anthropic_resp = openai_chat_to_anthropic_message(content, original_model=model)

            await safe_enqueue_event(MeterEvent(
                request_id=req_id,
                protocol="anthropic_messages",
                client_app=client_app,
                user_id=user_id,
                upstream_id=upstream_id,
                model=upstream_model,
                started_at_ms=started,
                ended_at_ms=ended,
                latency_ms=ended - started,
                status_code=r.status_code,
                error=None if r.status_code < 400 else limit_log_field(json.dumps(content, ensure_ascii=False)),
                prompt=limit_log_field(body),
                response=limit_log_field(anthropic_resp),
                usage=usage if usage else None,
            ))

            return JSONResponse(status_code=r.status_code, content=anthropic_resp)

        # Streaming: wrap OpenAI SSE into Anthropic SSE (minimal: only text deltas)
        async def anthropic_sse() -> AsyncIterator[bytes]:
            ended: Optional[int] = None
            status_code: int = 200
            accumulated_text = ""
            usage: Optional[Dict[str, Any]] = None
            upstream_id: Optional[str] = None
            upstream_model: Optional[str] = openai_body.get("model")
            buffer = ""  # buffer for incomplete lines
            content_block_started = False

            message_id = f"msg_{uuid.uuid4().hex[:24]}"
            
            try:
                # message_start
                yield _sse("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    }
                })
                
                async with client.stream("POST", upstream_url, headers=headers, json={**openai_body, "stream": True}) as resp:
                    status_code = resp.status_code
                    async for b in resp.aiter_bytes():
                        # parse OpenAI SSE lines
                        buffer += b.decode("utf-8", errors="ignore")
                        
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            
                            if not line.startswith("data: "):
                                continue
                            
                            data = line[6:].strip()
                            if not data:
                                continue
                            
                            if data == "[DONE]":
                                # Ensure content block is stopped before message ends
                                if content_block_started:
                                    yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                                    content_block_started = False
                                
                                # message_delta with usage if available
                                anthropic_usage = anthropic_usage_from_openai_usage(usage) if usage else {"output_tokens": 0}
                                yield _sse("message_delta", {
                                    "type": "message_delta",
                                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                    "usage": anthropic_usage
                                })
                                
                                yield _sse("message_stop", {"type": "message_stop"})
                                
                                ended = now_ms()
                                await safe_enqueue_event(MeterEvent(
                                    request_id=req_id,
                                    protocol="anthropic_messages",
                                    client_app=client_app,
                                    user_id=user_id,
                                    upstream_id=upstream_id,
                                    model=upstream_model,
                                    started_at_ms=started,
                                    ended_at_ms=ended,
                                    latency_ms=ended - started,
                                    status_code=status_code,
                                    error=None if status_code < 400 else "upstream streaming error",
                                    prompt=limit_log_field(body),
                                    response=limit_log_field({"text": accumulated_text}),
                                    usage=usage,
                                ))
                                return

                            try:
                                obj = json.loads(data)
                            except Exception:
                                continue

                            # capture id/model/usage if present
                            if obj.get("id"):
                                upstream_id = obj["id"]
                            if obj.get("model"):
                                upstream_model = obj["model"]
                            u = openai_usage_from_json(obj)
                            if u:
                                usage = u

                            # OpenAI delta content
                            try:
                                delta = obj["choices"][0].get("delta", {})
                                delta_text = delta.get("content")
                            except Exception:
                                delta_text = None

                            if delta_text:
                                # Start content block on first text delta
                                if not content_block_started:
                                    yield _sse("content_block_start", {
                                        "type": "content_block_start",
                                        "index": 0,
                                        "content_block": {"type": "text", "text": ""},
                                    })
                                    content_block_started = True
                                
                                accumulated_text += delta_text
                                yield _sse("content_block_delta", {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {"type": "text_delta", "text": delta_text},
                                })
            except httpx.HTTPError as e:
                ended = now_ms()
                await safe_enqueue_event(MeterEvent(
                    request_id=req_id,
                    protocol="anthropic_messages",
                    client_app=client_app,
                    user_id=user_id,
                    upstream_id=upstream_id,
                    model=upstream_model,
                    started_at_ms=started,
                    ended_at_ms=ended,
                    latency_ms=ended - started,
                    status_code=502,
                    error=limit_log_field(str(e)),
                    prompt=limit_log_field(body),
                    response=None,
                    usage=usage,
                ))
                return

        return StreamingResponse(anthropic_sse(), media_type="text/event-stream")

def _sse(event: str, data_obj: Dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data_obj, ensure_ascii=False)}\n\n".encode("utf-8")

def anthropic_to_openai_chat(anthropic: Dict[str, Any]) -> Dict[str, Any]:
    messages: List[Dict[str, Any]] = []

    system = anthropic.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        # blocks
        system_text = ""
        for blk in system:
            if isinstance(blk, dict) and blk.get("type") == "text":
                system_text += (blk.get("text") or "") + "\n\n"
        if system_text.strip():
            messages.append({"role": "system", "content": system_text.strip()})

    for msg in anthropic.get("messages", []) or []:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # blocks -> text join
            text = ""
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    text += (blk.get("text") or "") + "\n"
            messages.append({"role": role, "content": text.strip() or "..."})
        else:
            messages.append({"role": role, "content": "..."})

    openai = {
        "model": anthropic.get("model"),
        "messages": messages,
        "stream": bool(anthropic.get("stream", False)),
        "temperature": anthropic.get("temperature", 1.0),
    }

    # anthropic max_tokens -> openai max_tokens
    if anthropic.get("max_tokens") is not None:
        openai["max_tokens"] = anthropic["max_tokens"]

    return openai

def openai_chat_to_anthropic_message(openai_resp: Dict[str, Any], original_model: Optional[str]) -> Dict[str, Any]:
    # Minimal Anthropic Messages response
    text = ""
    try:
        text = openai_resp["choices"][0]["message"]["content"] or ""
    except Exception:
        text = ""

    usage = openai_usage_from_json(openai_resp) or {}
    anthropic_usage = anthropic_usage_from_openai_usage(usage) if usage else {"input_tokens": 0, "output_tokens": 0}

    return {
        "id": openai_resp.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
        "type": "message",
        "role": "assistant",
        "model": original_model or openai_resp.get("model"),
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": anthropic_usage,
    }