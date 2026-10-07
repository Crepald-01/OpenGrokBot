# OpenGrokBot API

The background service has a local HTTP API. The desktop app, the phone web app and your own scripts all use the same one. It listens on `http://127.0.0.1:8765` by default (the port is in Settings > Mobile). Responses are JSON. Errors come back as `{"error": "..."}` with a 4xx status.

## Authentication

Send a token in the `Authorization` header:

```
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8765/api/bots
```

There are two kinds of token.

**The main access token** (Settings > Mobile > Show access token). It can do everything. Treat it like a password.

**API tokens** (Settings > API access). Make one per script or tool, so you can revoke one without touching the others. A token has a scope:

| Scope | Can |
|---|---|
| `read` | Make `GET` requests: look at Bots, chats, files, usage, insights and so on. |
| `chat` | Everything `read` can, plus send messages to a Bot (`POST /api/threads/{id}/messages`). |
| `full` | Read and change things: create and edit Bots, run workflows, add knowledge, restore files. |

Some areas always need the main access token, whatever the scope: API tokens, backups, notification channels, settings, provider keys, approvals and rules, MCP and plugin setup, the terminal, trigger management, and the support bundle. A token that is revoked or expired gets `401`; a token used outside its scope gets `403`.

A token is shown once when it is created. Only a hash is kept, so a lost token cannot be recovered, only revoked.

Too many wrong tokens from one address in a few minutes are answered with `429`.

## Common calls

| | |
|---|---|
| `GET /api/health` | Version check. Needs no token. |
| `GET /api/bots` | Your Bots. |
| `GET /api/bots/{id}/threads` | A Bot's conversations. |
| `GET /api/threads/{id}` | A conversation, ready to display: messages, tool calls and pending approvals. |
| `POST /api/threads/{id}/messages` | `{"text": "..."}`: send a message (slash commands work too). |
| `POST /api/threads/{id}/fork` | `{"message_id": 12, "text": "optional new wording", "run": true}`: branch a conversation. |
| `GET /api/threads/{id}/export` | The conversation as Markdown. |
| `GET /api/search?q=...` | Search every chat and memory. |
| `GET /api/digest?spec=today` | What the Bots did: `today`, `yesterday`, `24h` or `week`. |
| `GET /api/insights?days=30&bot_id=...` | Tasks, success rate, busiest hours, tools, problems. |
| `GET /api/usage` | Tokens and estimated cost. |

## Knowledge base

| | |
|---|---|
| `GET /api/knowledge` | Documents and counts. |
| `POST /api/knowledge/note` | `{"name": "...", "text": "...", "bot_id": ""}` (empty `bot_id` shares it with every Bot). |
| `POST /api/knowledge/upload` | `{"filename": "faq.docx", "data_b64": "...", "bot_id": ""}` for text, Markdown, HTML, CSV, JSON and Word files. |
| `POST /api/knowledge/workspace` | `{"path": "docs"}` to index a workspace file or folder. |
| `GET /api/knowledge/search?q=...` | Best matching passages. |
| `DELETE /api/knowledge/{id}` | Remove a document. |

## Workflows

| | |
|---|---|
| `GET /api/workflows` | Workflows and recent runs. |
| `POST /api/workflows` | `{"name": "...", "steps": [{"bot_id": "...", "instruction": "Research {{input}}"}, ...], "cron": ""}` |
| `POST /api/workflows/{id}/run` | `{"input": "...", "dry_run": false}`; returns a `run_id`. |
| `GET /api/workflows/{id}/runs` | Run history with every step's result. |
| `POST /api/workflows/{id}/stop` | Stop the run in progress. |

In a step, `{{input}}` is what you passed to the run, `{{previous}}` is the last step's result and `{{step1}}`, `{{step2}}`... are earlier results. Results are handed over as untrusted data, not as instructions.

## Webhooks

A webhook trigger has its own address, which contains a secret. It does not use an access token:

```
curl -X POST http://127.0.0.1:8765/hooks/<id>/<secret> -H "Content-Type: application/json" -d '{"order": 1042}'
```

It answers `202` with a `run_id` and starts the Bot (or workflow) in the background. Wrong or unknown addresses get an identical `404`; repeated wrong guesses are locked out with `429`. The body is limited to 64 KB, a trigger handles one request at a time, and a webhook runs at most 60 times an hour. Whatever you send is given to the Bot as untrusted data.

The address is shown once, when the trigger is created or when you make a new one (Automations > Triggers).

## Files and history

| | |
|---|---|
| `GET /api/ws/recent`, `/api/ws/list?path=`, `/api/ws/search?q=` | Browse the workspace. |
| `GET /api/ws/preview?path=`, `/api/ws/raw?path=` | Read a file. |
| `GET /api/ws/history?path=` | Earlier versions of a file. |
| `POST /api/ws/restore` | `{"path": "...", "version_id": 7}` to put one back. |
| `GET /api/ws/deleted` | Deleted files that can be restored. |

## Diagnostics

`GET /api/diagnostics` runs the health checks. `GET /api/diagnostics/bundle` (main token only) downloads a support zip with the results, versions, settings without secrets and a scrubbed log tail. It contains no chats, memories, files or keys.
