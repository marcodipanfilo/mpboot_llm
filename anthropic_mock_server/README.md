# Anthropic Mock Server

Small standalone Anthropic-compatible mock/cache server.

Supported endpoint:

- `POST /v1/messages`

Useful modes:

- `cache-first`
  - return cached response if present, otherwise call the real Anthropic API and cache the result
- `record`
  - always call the real Anthropic API and overwrite the cache entry
- `replay`
  - only return cached responses
- `mock-only`
  - never call the real API; return a synthetic Anthropic-style response
- `passthrough`
  - always call the real Anthropic API and do not write to cache

## Run

```bash
python anthropic_mock_server/server.py
```

Default server:

- host: `127.0.0.1`
- port: `8000`
- mode: `cache-first`

## Environment Variables

- `ANTHROPIC_MOCK_HOST`
- `ANTHROPIC_MOCK_PORT`
- `ANTHROPIC_MOCK_MODE`
- `ANTHROPIC_MOCK_DB`
- `ANTHROPIC_REAL_BASE_URL`
  - default: `https://api.anthropic.com`
- `ANTHROPIC_REAL_API_KEY`
  - required for `cache-first`, `record`, or `passthrough`

## Point a Client to It

If your client lets you override the Anthropic base URL, use:

```text
http://127.0.0.1:8000
```

The client should still call:

```text
/v1/messages
```

## Example Curl

```bash
curl http://127.0.0.1:8000/v1/messages \
  -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 128,
    "messages": [
      {"role": "user", "content": "Say hello"}
    ]
  }'
```

## Files

- `server.py`
  - standalone server
- `cache.sqlite3`
  - created automatically by default
