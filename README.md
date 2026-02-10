# LLM Proxy Gateway

This project implements an LLM proxy gateway using FastAPI, allowing you to interact with both OpenAI's and Anthropic's messaging APIs.

## Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/SFWCY/b23-llm-proxy.git
   ```

2. Navigate to the project directory:
   ```bash
   cd b23-llm-proxy
   ```

3. Install the requirements:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

To run the FastAPI server:
```bash
uvicorn gateway.main:app --host 0.0.0.0 --port 8000
```

You can then access the API at `http://localhost:8000`.  

## Admin Endpoint

There is an admin endpoint to upsert keys which can be found at `/admin/keys`. This endpoint requires proper authentication to ensure secure handling of keys.

## Events Logging

Events are written asynchronously to `data/events.jsonl` to maintain records of the interactions made through the gateway.
