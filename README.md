# Nock — Slack CRM Assistant for Notion

A serverless AI agent that turns Slack messages into Notion CRM records. Type a note, hit send — Nock extracts the entities, finds or creates the right records, and posts a link back in Slack.

Built on [Claude](https://anthropic.com) and deployed to [DigitalOcean Functions](https://docs.digitalocean.com/products/functions/).

> This is the community edition. For the hosted version with a setup UI, multi-workspace support, and managed infrastructure, see [nockcrm.com](https://www.nockcrm.com).

---

## How it works

**Slash command — quick update:**
```
/nock John from Acme called, interested in enterprise pricing
```
Nock extracts the company, contact, and interaction, deduplicates against existing records, and responds:
> Logged call with John Smith (Acme Corp). [Acme →](https://notion.so/...) · [John →](https://notion.so/...)

**Slash command — agent mode:**
```
/nock Research Acme Corp and log what you find #agent
```
Spins up a Claude function-calling agent that calls Perplexity for research, then writes structured results to Notion — with citations.

**Slack Events (passive logging):**
Nock listens to channels and scores messages for CRM relevance. When a message mentions a company, contact, or deal stage above the relevance threshold, it surfaces a "Log it to CRM" button. One click, done.

---

## Features

- **`/nock <note>`** — Free-form text → structured Notion records in seconds
- **`/nock <note> #agent`** — Multi-turn Claude agent for research and complex updates
- **Relevance scoring** — Passive channel monitoring with a "Log it" button prompt
- **Duplicate prevention** — Always searches before creating; links to existing records
- **Batch actions** — "Add Acme, add John, log their call" processed in one pass
- **Research tool** — Perplexity API integration; citations written to Notion
- **Status aliasing** — "Lead", "Prospect" etc. mapped to your Notion status options
- **Async dispatch** — Background invocation keeps within Slack's 3-second deadline

### Supported Notion databases

| Database | Used for |
|---|---|
| Companies | Company records |
| Customers | Individual contacts |
| Interactions | Calls, meetings, emails |
| Outreach | Outbound sequences |
| Product Feedback | Feature requests, bug reports |
| Referrals | Referral tracking |

---

## Architecture

```
Slack ──► DigitalOcean Function (Python 3.11)
              │
              ├── Slash command  ──► Extraction (Claude) ──► Notion API
              │                                           └── Supabase (logs)
              │
              ├── Events API     ──► Relevance score (Claude) ──► Slack button
              │
              └── Agent mode     ──► Claude agent loop
                                       ├── Notion CRUD tools
                                       └── Perplexity research
```

The entire backend is a single serverless function. `build.sh` copies shared library files into the function directory before deployment; DigitalOcean bundles each function independently.

---

## Prerequisites

- [DigitalOcean account](https://cloud.digitalocean.com/) with a serverless namespace
- [doctl](https://docs.digitalocean.com/reference/doctl/how-to/install/) CLI installed and authenticated
- A Slack app with slash commands, Events API, and interactive components enabled
- A Notion integration with access to your CRM workspace
- A Supabase project for logging (optional but recommended)

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/your-org/nock.git
cd nock

cp .env.demo .env
# Edit .env and fill in all values
```

### 2. Connect DigitalOcean

```bash
doctl auth init
doctl serverless connect
```

### 3. Build and deploy

```bash
./deploy.sh           # build + deploy
./deploy.sh --dry-run # validate without deploying
```

The deploy script prints your function URL on completion. Use that URL for all three Slack app endpoints (slash command, events, interactivity).

### 4. Test

```bash
python test_local.py  # fast local unit tests, no credentials needed
python test.py        # smoke tests
python test.py --agent  # includes a live agent call (uses tokens)
```

---

## Environment variables

Copy `.env.demo` to `.env` and fill in each value.

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (`sk-ant-...`) |
| `PERPLEXITY_API_KEY` | Perplexity API key for the research tool |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key |
| `SLACK_SIGNING_SECRET` | Slack app signing secret |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`) |
| `API_SECRET` | Bearer token for direct API calls |
| `DO_SLACK_ASYNC_URL` | Your function's web URL (for async self-invocation) |
| `DO_SLACK_ASYNC_TOKEN` | Auth token for async self-invocation (`Basic ...`) |

`PERPLEXITY_API_KEY` and `SUPABASE_*` are optional — Nock degrades gracefully if they are absent.

---

## Notion workspace setup

Nock reads your database schema at runtime and adapts to your field names. The minimum required setup:

1. Create a Notion integration at [notion.so/my-integrations](https://www.notion.so/my-integrations) and note the token.
2. Share each CRM database with the integration (open the database → ··· → Connections → add your integration).
3. Databases should be named **Companies**, **Customers**, **Interactions**, **Outreach**, **Product Feedback**, and **Referrals** — or update `config.py` to match your names.

For best results, create a **CRM Settings** page in your Notion workspace describing your sales process, status options, and naming conventions. Nock reads this page to calibrate its behaviour.

---

## Slack app setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App.
2. **Slash Commands** → create `/nock` pointing to `<your-function-url>`.
3. **Event Subscriptions** → enable and subscribe to `message.channels`. Set the request URL to `<your-function-url>`.
4. **Interactivity & Shortcuts** → set the request URL to `<your-function-url>`.
5. Bot token scopes: `chat:write`, `commands`, `channels:history`.
6. Install to your workspace and copy the bot token to `SLACK_BOT_TOKEN`.

---

## Project structure

```
nock/
├── packages/notion-crm/crm/   # Serverless function
│   ├── __main__.py            # Request router
│   ├── agent.py               # Claude agent loop
│   ├── extraction.py          # Free-form text → CRM action extraction
│   ├── slack_slash.py         # /nock slash command handler
│   ├── slack_events.py        # Slack Events API + relevance scoring
│   ├── slack_interactions.py  # Button interaction handler
│   ├── notion_utils.py        # Notion page/database helpers
│   └── config.py              # System prompts, tool definitions, constants
├── lib/                       # Shared code (copied into function by build.sh)
│   ├── notion_client.py       # Notion REST API client
│   └── crm_logger.py          # Supabase logging client
├── skills/                    # Agent skill guides (injected into system prompt)
│   ├── crm-operations/        # Notion conventions, duplicate prevention
│   ├── slash-quick-update/    # Fast /nock behaviour
│   ├── slash-agent/           # #agent mode behaviour
│   ├── slack-event-scoring/   # Relevance scoring logic
│   ├── research-citations/    # Perplexity + citation formatting
│   └── feedback-context/      # /nock #feedback behaviour
├── build.sh                   # Copies lib/ into function directories
├── deploy.sh                  # Builds and deploys to DigitalOcean
├── project.yml                # DigitalOcean Functions manifest
└── .env.demo                  # Environment variable reference
```

---

## Customising agent behaviour

Agent behaviour is controlled by the skill files in `skills/`. Each skill is a markdown file injected into the agent's system prompt at runtime. Edit these files to change how Nock responds, what it prioritises, or how it formats output — no code changes needed.

---

## Documentation

Full documentation is available at [nockcrm.com/docs](https://www.nockcrm.com/docs).

---

## License

MIT
