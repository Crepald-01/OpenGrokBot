# OpenGrokBot guide

Full documentation. Back to the [project README](../README.md).

[Install](#install) · [Run](#run) · [Create your first Bot](#create-your-first-bot) · [A good first handoff](#a-good-first-handoff) · [How it works](#how-it-works) · [Skills](#writing-skills) · [Routines](#routines) · [Plugins and MCP](#adding-plugins-and-mcp-servers) · [Approvals and security](#approval-and-security-model) · [Remote mode](#remote-mode-setup) · [Mobile](#mobile-app) · [Admin policy](#administration-presets) · [Build the .exe](#build-the-exe) · [Tests](#tests) · [Contributing](#contributing)

---

## Install

Requirements: Windows 10/11, Python 3.11 or newer. (The headless service also runs on Linux/macOS/Docker; the desktop UI is built and tested for Windows.)

```powershell
cd opengrokbot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

Chromium (about 150 MB) is the Bots' browser. If you skip that step the app offers to install it on first run.

## Run

```powershell
python main.py
```

The first launch starts the **background service** (a separate process that keeps Bots, routines and background turns alive when the window is closed) and then opens the app. Add a model key in **Settings > Models** (stored in Windows Credential Manager). The **Default model** box then fills with the models your provider offers: pick one from the dropdown, or type a name. Each Bot can override the model in *Edit Bot*.

Other entry points:

| Command | What it does |
|---|---|
| `python main.py --tray` | start minimised to the system tray |
| `python main.py --service` | run only the service (VM, Docker, or "headless PC") |
| `python main.py --service --host 0.0.0.0` | also reachable from your phone/LAN |
| `python main.py --install-browsers` | download the Playwright Chromium engine |
| `python main.py --reset-token` | rotate the access token used by the mobile app and remote clients |

Closing the window keeps the app in the tray. The tray menu has **Quit app (Bots keep running)** and **Quit and stop all Bots**.

## Create your first Bot

Setup is a message, not a workflow builder.

1. Press **＋** next to *Bots* (or pick a role on the welcome screen). Templates: **Chief of Staff, Inbox, Expenses, Recruiting, Bug Fixer, Operations, Sales Outbound, Researcher, Content Writer**. **Create a starter team** makes a Chief of Staff plus specialists in a group chat.
2. Name it and describe the job in a sentence or two.
3. Message it. When it needs a connector (Gmail, Slack, GitHub...) it **asks** and you approve in a prompt; you add the credentials once under **Plugins**.

Each Bot has its own threads, memory, skills access, model and approval mode. Conversations and learned context stay separate per Bot.

## A good first handoff

Give the task, the context, and the finish line. For the **Inbox** template:

> Go through my unread email from the last 2 days. Give me a one-screen summary, draft replies for anything that needs one in my voice, and flag what is urgent. Don't send anything. If you need Gmail access, ask.

What you should see: the Bot requests Gmail access (approve it), works through the mailbox with a live activity feed beside the chat, saves your preferences and voice to its memory, drafts replies, and posts one summary. If it hits a login or CAPTCHA it asks you to **take over** the browser, then continues.

For a team: tell the **Chief of Staff** *"Triage my inbox daily, track receipts, and keep a list of open candidates"*. It hands tasks to the specialists with `handoff_create`, tracks ownership in shared project notes, nudges stalled handoffs, and only pulls you in for judgment calls.

## How it works

**One computer per account.** All your Bots share one persistent computer: a Chromium profile (cookies, app logins), a filesystem workspace (`%APPDATA%\OpenGrokBot\workspace`) and a terminal. Handoffs work without repeating setup. *Everything on it is available to every Bot*, and the app says so. Each Bot gets its **own screen** (a tab) on it; a Bot runs one computer-use task on its screen at a time, while different Bots run in parallel.

**Computer use.** Bots use connectors and MCP tools where available, and the browser for everything else: numbered-element snapshots, screenshots (shown inline in chat), clicking, typing, scrolling. If a site blocks automation, a session expires, or a CAPTCHA/2FA/login appears, the Bot **hands the step to you** and never tries to work around it. **Take over** opens the Bot's browser like a remote desktop; press *Hand back* when done. Passwords are never typed by Bots.

**Memory that compounds.** Per Bot: stable preferences, role context, your voice, edge cases, and summaries of prior work, carried across sessions and visible/editable in *Edit Bot > Memory*. After substantial tasks a curation pass saves what is worth keeping. Facts that go stale are flagged *re-check*, and for consequential decisions Bots are instructed to check the current source rather than trust memory. Bots can search their old conversations to resume work, schedule **follow-ups** for threads they are waiting on, **nudge** stalled handoffs, and (optionally, per Bot) become **proactive**: suggest or pick up work between your messages. Interrupted tasks resume after a restart.

**Teams.** Bots message each other (`message_bot`), work in **group chats** (`@Name` mentions pass the floor; a lead Bot receives your messages), and hand over ownership with **handoffs** (one owner per task, statuses, nudges). Shared **project notes** give every Bot the same context so you never paste notes between chats. Bot-to-bot chatter pauses after 10 exchanges so you stay in control.

**Learn by demonstration.** In the take-over view press **Follow along**, do the job once, add notes, and stop. The Bot drafts a **skill** from the recorded steps (secrets are never recorded). Edit it, **test** it (dry run refuses consequential actions), activate it, and optionally schedule it as a **routine**.

## Writing skills

Skills are markdown files in `%APPDATA%\OpenGrokBot\skills`, editable in the **Skills** page. Starter skills are copied there on first run.

```markdown
---
name: expense-report
description: Collect this month's receipts and prepare the expense summary
status: active          # draft skills are not offered to Bots until you activate them
bot:                    # optional: restrict to one Bot by name
tags: finance, monthly
---
# Expense report

## When to use
Monthly, or when asked for an expense summary.

## Inputs
Month (default: last month). Vendors to ignore (check memory).

## Steps
1. Search Gmail for receipts: `receipt OR invoice newer_than:31d`.
2. Extract merchant, date, amount, currency from each; open the email, never guess.
3. Append rows to `shared/expenses/ledger.csv`.
4. Write `shared/expenses/<month>.md` with totals per category.

## Checks
Totals in the summary equal the sum of the ledger.

## Needs approval
Submitting the report anywhere; paying anything.
```

Bots see each active skill's name and description, and read the full text with `skill_read` when it applies. Bots can write their own skills with `skill_save`; those are saved as **drafts** for you to review.

## Routines

**Routines** run a skill and/or a prompt on a cron schedule, per Bot, in the background service (so they run with the window closed). Create them in **Routines** (presets like *Overnight (2:00 every day)*, or any 5-field cron), or let a Bot propose one (you approve it). Each run gets its own thread, a result, history, and a notification. Options: dry-run, notify on failures only, and *catch up* once if the PC was off at the scheduled time. Unattended runs still stop for approvals; if nobody answers within the routine timeout (default 2 h) that step is skipped.

## Adding plugins and MCP servers

**Built-in connectors** (Plugins page): Gmail and Google Calendar (OAuth: create a *Desktop app* OAuth client in Google Cloud, enable the API, paste client id/secret, press **Connect**), Slack, Notion, Linear, Jira, GitHub, and a **generic REST connector**. Read tools run freely; anything that writes or sends asks for approval.

**Install a plugin** from a local folder or a git URL in **Plugins > Marketplace**. A plugin is a folder with `plugin.json`:

```json
{
  "id": "acme", "name": "Acme", "version": "1.0.0", "description": "Acme API",
  "fields": [{"key": "api_key", "label": "API key", "secret": true, "required": true}],
  "base_url": "https://api.acme.example",
  "tools": [
    {"name": "list_items", "description": "List items",
     "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
     "request": {"method": "GET", "path": "/v1/items", "query": {"q": "{q}"},
                 "headers": {"Authorization": "Bearer {{secret.api_key}}"}},
     "response_path": "data.items", "risk": "safe"},
    {"name": "create_item", "description": "Create an item",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
     "request": {"method": "POST", "path": "/v1/items", "body": {"name": "{name}"}}, "risk": "write"}
  ]
}
```

`{param}` comes from the model's arguments; `{{secret.x}}` / `{{config.x}}` come from your configured fields and are never visible to the model. Tools with `risk` other than `safe` (`write`, `send`, `delete`, `submit`, `purchase`) ask for approval. For code, add `"entry": "plugin.py"` with `register(api)` (see `plugins/example-python`). **Code plugins run inside the service with your permissions: only install code you trust.** Bundled examples: `plugins/open-meteo`, `plugins/hacker-news`, `plugins/example-python`. Add your own catalog with *Settings* `catalog_urls` (a JSON file with the same shape as `plugins/catalog.json`).

**MCP servers** (Plugins > MCP servers): add stdio or remote (streamable HTTP / SSE) servers, or paste the usual config:

```json
{"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\me\\Documents"]}}}
```

Tools appear as `mcp__<server>__<tool>`. Use `${secret:NAME}` in env/headers to pull values from the credential store. A server's tools are visible to a Bot only after you grant that server to it, usually in answer to the Bot's request.

## Approval and security model

**Bots work end to end and ask only for consequential actions:**

| Category | Examples | Notes |
|---|---|---|
| send | emails, Slack posts, calendar invites, `git push` | |
| submit | submitting forms, Enter in a form field, "Save changes" | detected from the page element |
| purchase | Buy now, Checkout, subscribe | never auto-approved |
| delete / overwrite | deleting or overwriting files, destructive buttons | |
| command / outside_workspace | commands outside the workspace, unfamiliar programs, inline code | |
| install / download | `pip install`, `curl`, `winget` | |
| login | logging in to a service you have not used before | handled as a hand-over to you |
| access | granting a Bot a connector or MCP server | never auto-approved |
| schedule | a Bot creating a routine | |

**Approval modes (per Bot):** *Ask me* (default), or **Auto Review**: a reviewer model checks each pending action against what you asked and auto-approves only clearly low-risk ones; everything else, and anything in the never-auto categories above, goes to you. It fails closed (errors escalate). "Approve & always allow this" creates a standing rule you can delete in *Edit Bot*.

**Prompt-injection defense.** Text from web pages, emails, files, API results and command output is wrapped as `<untrusted_content>` data, scanned for instruction-like text, and the Bot is instructed never to obey it. A hit **taints** the task: Auto Review and standing rules are switched off for the rest of it and approval cards show a warning.

**Network policy.** Global and per-Bot allow/deny lists of domains (wildcards ok), enforced on browser requests (including sub-resources), `web_fetch` and connectors, with local/private addresses blocked by default (SSRF guard). Deny always wins.

**Credentials.** API keys and connector tokens live in Windows Credential Manager (keyring), never in the database, settings or files, and are scrubbed from logs/exports. Bots never see secrets: connector credentials are applied outside the model, and Bots cannot type into password fields.

**Action log.** *Action log* shows every tool call, page visited and file touched per Bot, filterable and exportable as JSON/CSV.

**What this is not.** The terminal gate is a heuristic on command text and working folder, not an OS sandbox (a script a Bot wrote could do anything the OS user can). For hard isolation run the computer in a VM or Docker (remote mode). Treat the shared computer as readable by every Bot. The local web server uses a bearer token and plain HTTP; do not expose it to the internet without a VPN/tunnel/TLS.

## Remote mode setup

Remote mode runs the **same computer** (service, browser, workspace, routines) on a machine you control, so work continues while your laptop is closed. The desktop app and phone connect to it.

**Windows VM** (elevated PowerShell on the VM, in the project folder):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\setup-remote-windows.ps1 -Port 8765 -OpenFirewall
```

It installs Python if needed, the headless dependencies and Chromium, prints an **access token**, and registers a scheduled task that keeps the service running. Add provider keys from the desktop app (they are stored in the VM's Credential Manager).

**Docker host:**

```bash
export OPENGROKBOT_TOKEN=$(openssl rand -hex 32)
export ANTHROPIC_API_KEY=sk-ant-...
docker compose -f deploy/docker-compose.yml up -d --build
```

Containers have no credential manager, so provider keys come from environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, or `OPENGROKBOT_SECRET_<NAME>`). The compose file binds to localhost; reach it with an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 you@host`), a VPN such as Tailscale, or a TLS reverse proxy.

**Connect:** *Settings > Computer > Remote*, enter `http://<host>:8765` and the token, **Test and switch**. Use **Take over** to log in to sites on the remote browser once; the logins persist there.

## Mobile app

*Settings > Mobile > Allow phones on my network*, apply (restarts the service), then scan the QR code or open the link on your phone (same Wi-Fi) and **Add to Home screen**. You get the same threads, live streaming, approvals, take-over view, and in-app alerts. For **push while the phone is locked**, install the free [ntfy](https://ntfy.sh) app, subscribe to a long random topic, and enter the server and topic in *Settings > Notifications* (browsers only allow system notifications over HTTPS). Allow the port in Windows Firewall if prompted.

## Administration presets

Place a JSON file at `%PROGRAMDATA%\OpenGrokBot\admin.json` (or point `OPENGROKBOT_ADMIN` at it) to preset a managed deployment. See `admin-settings.example.json`:

* `network_policy` (mode, allow, deny, `locked`), `approval_defaults` (mode, `locked`, `always_ask_categories`)
* `allowed_plugins`, `allowed_mcp_servers`, `allowed_providers`, `allow_plugin_install`, `allow_remote_mode`
* `max_steps`, `weekly_token_limit`

Locked settings are enforced by the service, and the UI shows *managed by your organization*.

## Build the .exe

```powershell
pip install -r requirements-build.txt
python build_exe.py            # dist\OpenGrokBot\OpenGrokBot.exe
python build_exe.py --onefile  # single dist\OpenGrokBot.exe (slower to start)
```

The browser engine is not bundled: the exe offers to download it on first run, or run `OpenGrokBot.exe --install-browsers`. The exe starts its own background service with `OpenGrokBot.exe --service`.

## Data locations

`%APPDATA%\OpenGrokBot` (override with `OPENGROKBOT_HOME`): `opengrokbot.sqlite3`, `workspace\`, `computer\browser-profile\`, `skills\`, `plugins\`, `logs\`. Secrets are not in this folder.

## Tests

```powershell
python -m unittest discover -s tests -t .      # engine, providers, API, MCP/plugins, real-Chromium browser tests
```

`tests/ui_smoke.py` renders every screen offscreen against a live service and saves screenshots (set `QT_QPA_FONTDIR=C:\Windows\Fonts` so text renders). `tests/ui_models.py` checks the model dropdown.

## Contributing

Issues and pull requests are welcome.

* Layout: `core/` (agent loop, bots, computer, browser, approvals, skills, routines, mcp, plugins, memory, providers, db), `service/` (FastAPI API + service runner), `ui/` (PySide6), `web/` (mobile PWA), `skills/`, `plugins/`, `assets/`, `deploy/`.
* The UI is only a client of the service (`ui/api.py`); put behavior in `core/` and expose it in `service/server.py`.
* Every new tool declares its risk (`core/tooling.py: Risk`); anything that sends, submits, spends, deletes or runs code must return a `Risk`. Output from outside the chat must be returned as `data=` so it is wrapped as untrusted.
* Add tests with a scripted model (`tests/test_engine.py: FakeProvider`) rather than a live API.
* Keep secrets out of the repo, the database and logs.

## License

MIT, see `LICENSE`.
