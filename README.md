# b23-llm-proxy

A lightweight Python proxy for OpenAI and Anthropic (Claude) API endpoints with API key management, usage tracking, and metering.

## Features

- **OpenAI Chat Completions API** (`/v1/chat/completions`) - Full passthrough with streaming support
- **Anthropic Messages API** (`/v1/messages`) - Converts Anthropic API to OpenAI format with proper SSE streaming
- **API Key Management** - Simple admin endpoint for managing user API keys
- **Usage Tracking** - Automatic logging of all requests, responses, and token usage to JSONL
- **Streaming Support** - Full SSE (Server-Sent Events) streaming for both OpenAI and Anthropic formats
- **Configurable Logging** - Control log verbosity and size limits via environment variables

## Installation

### Prerequisites

- Python 3.9 or higher
- pip (Python package manager)

### Setup

1. Clone the repository:
```bash
git clone https://github.com/SFWCY/b23-llm-proxy.git
cd b23-llm-proxy
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure environment variables (copy and modify `.env.example`):
```bash
cp .env.example .env
# Edit .env with your settings
```

## Configuration

### Environment Variables

Create a `.env` file or set the following environment variables:

- **UPSTREAM_BASE_URL** - The base URL of your upstream LLM service (e.g., `http://llmapi.bilibili.co/v1`)
- **DATA_DIR** - Directory for data files (default: `./data`)
- **ADMIN_TOKEN** - **Required** - Secure token for admin endpoints (e.g., `your-secure-admin-token-here`)
- **PORT** - Server port (default: `8866`)
- **MAX_LOG_CHUNKS** - Maximum SSE chunks to store per event (default: `50`)
- **MAX_LOG_FIELD_SIZE** - Maximum size of prompt/response in logs (default: `2000` characters)
- **FULL_TRACE_LOGGING** - Enable complete logging of prompts/responses (default: `false`)

Example `.env` file:
```bash
UPSTREAM_BASE_URL=https://api.openai.com/v1
DATA_DIR=./data
ADMIN_TOKEN=my-secret-admin-token-123
PORT=8866
MAX_LOG_CHUNKS=50
MAX_LOG_FIELD_SIZE=2000
FULL_TRACE_LOGGING=false
```

## Running the Server

Start the server with uvicorn:

```bash
uvicorn gateway.main:app --host 0.0.0.0 --port 8866
```

Or with custom port:

```bash
PORT=8866 uvicorn gateway.main:app --host 0.0.0.0 --port $PORT
```

The server will be available at `http://localhost:8866`

## API Usage

### Health Check

```bash
curl http://localhost:8866/healthz
```

### OpenAI Chat Completions

**Non-streaming:**
```python
import openai

client = openai.OpenAI(
    base_url="http://localhost:8866/v1",
    api_key="bsk-xxx"  # Your registered API key
)

response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

**Streaming:**
```python
import openai

client = openai.OpenAI(
    base_url="http://localhost:8866/v1",
    api_key="bsk-xxx"
)

stream = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### Anthropic Messages API (Claude Code)

**Non-streaming:**
```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:8866",
    api_key="bsk-xxx"  # Your registered API key
)

message = client.messages.create(
    model="claude-3-opus-20240229",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}]
)
print(message.content[0].text)
```

**Streaming:**
```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:8866",
    api_key="bsk-xxx"
)

with client.messages.stream(
    model="claude-3-opus-20240229",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}]
) as stream:
    for text in stream.text_stream:
        print(text, end="")
```

## Admin API

### Managing API Keys

Add or update an API key (requires `x-admin-token` header):

```bash
curl -X POST http://localhost:8866/admin/keys/upsert \
  -H "Content-Type: application/json" \
  -H "x-admin-token: your-secure-admin-token-here" \
  -d '{
    "api_key": "bsk-xxx",
    "user_id": "alice",
    "active": true
  }'
```

**Security Note:** The admin endpoint requires the `x-admin-token` header to match the `ADMIN_TOKEN` environment variable. Always keep your admin token secure and never commit it to version control.

## Data Files

### keys.json

Stores API key mappings in JSON format:

```json
{
  "keys": [
    {
      "api_key": "bsk-xxx",
      "user_id": "alice",
      "active": true
    }
  ]
}
```

You can manually edit this file, but it's recommended to use the admin API endpoint.

### events.jsonl

Logs all API interactions in JSON Lines format. Each line contains:

- `request_id` - Unique request identifier
- `protocol` - API protocol (`openai_chat` or `anthropic_messages`)
- `client_app` - Client application identifier
- `user_id` - User identifier from API key
- `upstream_id` - Upstream provider's request ID
- `model` - Model used
- `started_at_ms` - Request start timestamp
- `ended_at_ms` - Request end timestamp
- `latency_ms` - Request latency in milliseconds
- `status_code` - HTTP status code
- `error` - Error message (if any)
- `prompt` - Request payload (size-limited by default)
- `response` - Response payload (size-limited by default)
- `usage` - Token usage statistics (`prompt_tokens`, `completion_tokens`, `total_tokens`)

**Note:** By default, prompts and responses are truncated to `MAX_LOG_FIELD_SIZE` characters. Set `FULL_TRACE_LOGGING=true` to log complete data (not recommended for production due to storage costs).

## Development

### Project Structure

```
b23-llm-proxy/
├── gateway/
│   └── main.py          # Main FastAPI application
├── data/
│   ├── keys.json        # API key storage
│   └── events.jsonl     # Event logs
├── .env.example         # Example environment variables
├── requirements.txt     # Python dependencies
└── README.md           # This file
```

### Testing

The proxy includes a simple health check endpoint. For more comprehensive testing, you can use the included smoke test script or create your own integration tests.

## Security Considerations

1. **Always set ADMIN_TOKEN** - This is required for the admin endpoints
2. **Use HTTPS in production** - Deploy behind a reverse proxy with TLS
3. **Rotate API keys regularly** - Use the admin API to manage keys
4. **Monitor events.jsonl** - Review for suspicious activity
5. **Limit log sizes** - Use `MAX_LOG_FIELD_SIZE` to prevent excessive disk usage
6. **Secure your .env file** - Never commit sensitive credentials

## Troubleshooting

### Server won't start
- Check that port 8866 is not already in use
- Verify all environment variables are set correctly
- Ensure Python 3.9+ is installed

### API key authentication fails
- Verify the API key exists in `data/keys.json` and `active: true`
- Check the `Authorization: Bearer <key>` or `x-api-key: <key>` header is set
- Reload the server if you manually edited `keys.json`

### Streaming not working
- Ensure your client library supports SSE streaming
- Check that `stream: true` is set in the request
- Verify upstream service supports streaming

### Admin endpoint returns 403
- Verify `ADMIN_TOKEN` environment variable is set
- Check `x-admin-token` header matches the token exactly
- Restart the server after changing `ADMIN_TOKEN`

## License

See the repository for license information.