# Crisis Management RTA — Quick Build

A starter self-hosted AI workspace with:
- Responsive chat UI
- Ollama and OpenAI-compatible API configuration
- Conversation history persisted in a local SQLite database
- Basic login and admin-created users
- Notes
- Document upload and basic text search
- Docker Compose deployment

> This is a quick-build prototype, not a security-audited production system. Do not expose it directly to the public internet or use sensitive operational data until authentication, authorization, file handling, and deployment security have been reviewed.

## Quick start (Mac / Linux)

1. Install Docker Desktop (or Python 3.11+).
2. Copy `.env.example` to `.env` and set `SECRET_KEY` to a long random value.
3. Start:

```bash
docker compose up --build
```

4. Open http://localhost:8080
5. The first account is created using `ADMIN_USERNAME` and `ADMIN_PASSWORD` from `.env`.

## Local Ollama

Run Ollama on the host and pull a model, for example:

```bash
ollama pull qwen3.5:9b
```

For Docker Desktop on macOS, set `OLLAMA_BASE_URL=http://host.docker.internal:11434` in `.env`.

## OpenAI-compatible APIs

In Settings, choose `OpenAI-compatible`, enter the API base URL, model name and API key. Keys are stored in the app database for this prototype; use only in a trusted local environment. Production deployment should encrypt provider secrets at rest and enforce per-user/provider policy.

## Limitations

- Basic text extraction supports `.txt`, `.md`, `.csv`, `.json`, and `.log`; PDF/DOCX parsing is not included in this quick build.
- Search is keyword-based, not vector/semantic retrieval.
- Memories are simple user-managed text entries, not automatic long-term memory extraction.
- Voice, image generation, shared team spaces, and audit-log UI are not implemented yet.
- First-run admin creation is environment-based. Change credentials before sharing the service.
