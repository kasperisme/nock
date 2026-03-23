-- =====================================================
-- Nock Community Edition — Supabase Schema
--
-- Single-file migration for a self-hosted, single-workspace
-- deployment. No auth.users dependency, no RLS.
-- All tables use service_role access only.
--
-- Tables:
--   notion_connections           Notion workspace token + settings path
--   slack_connections            Slack team → Notion workspace mapping
--   agent_runs                   One row per agent/slash-command invocation
--   agent_tool_calls             Individual Notion tool calls within a run
--   page_operations              Direct Notion API calls (non-agent)
--   agent_conversation_sessions  Multi-turn agent state per (team, user)
-- =====================================================

create schema if not exists public;


-- =====================================================
-- Shared trigger: bump updated_at on any row change
-- =====================================================

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;


-- =====================================================
-- 1. notion_connections
-- Stores Notion workspace OAuth tokens.
-- In a single-workspace deployment, insert one row.
-- The agent looks this up via slack_connections.notion_connection_id.
-- =====================================================

create table if not exists public.notion_connections (
  id                    uuid        primary key default gen_random_uuid(),
  notion_workspace_id   text        not null,
  notion_workspace_name text        null,
  notion_workspace_icon text        null,
  notion_bot_id         text        null,
  access_token          text        not null,
  settings_path         text        null,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),

  unique (notion_workspace_id)
);

comment on table  public.notion_connections                       is 'Notion workspace connections. Insert one row per workspace.';
comment on column public.notion_connections.access_token          is 'Notion internal integration token or OAuth access token.';
comment on column public.notion_connections.settings_path         is 'Notion page path (e.g. CRM/Settings/Agent) for agent system prompt and database context.';

create trigger notion_connections_updated_at
  before update on public.notion_connections
  for each row execute procedure public.set_updated_at();

grant select, insert, update, delete on public.notion_connections to service_role;


-- =====================================================
-- 2. slack_connections
-- Maps a Slack team to a Notion workspace.
-- In a single-workspace deployment, insert one row.
-- When a slash command arrives from slack_team_id,
-- the agent resolves the Notion token via notion_connection_id.
-- =====================================================

create table if not exists public.slack_connections (
  id                    uuid        primary key default gen_random_uuid(),
  slack_team_id         text        not null,
  slack_team_name       text        null,
  slack_channel_id      text        null,
  slack_channel_name    text        null,
  webhook_url           text        null,
  access_token          text        not null,
  notion_connection_id  uuid        null
                                    references public.notion_connections(id) on delete set null,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),

  unique (slack_team_id)
);

comment on table  public.slack_connections                         is 'Slack workspace connections. Insert one row per team.';
comment on column public.slack_connections.notion_connection_id    is 'Notion workspace to use for CRM commands from this Slack team.';

create index if not exists slack_connections_notion_connection_id_idx
  on public.slack_connections (notion_connection_id)
  where notion_connection_id is not null;

create trigger slack_connections_updated_at
  before update on public.slack_connections
  for each row execute procedure public.set_updated_at();

grant select, insert, update, delete on public.slack_connections to service_role;


-- =====================================================
-- 3. agent_runs
-- One row per agent or slash-command invocation.
-- =====================================================

create table if not exists public.agent_runs (
  id                  uuid        primary key default gen_random_uuid(),
  prompt              text        not null,
  slack_context       jsonb       null,
  slack_connection_id uuid        null
                                  references public.slack_connections(id) on delete set null,
  model               text        not null,
  response            text        null,
  iterations          int         null,
  tool_call_count     int         not null default 0,
  success             boolean     not null default true,
  error               text        null,
  created_at          timestamptz not null default now(),
  completed_at        timestamptz null
);

comment on table  public.agent_runs                        is 'Each invocation of the Nock CRM agent.';
comment on column public.agent_runs.prompt                 is 'User prompt sent to the agent.';
comment on column public.agent_runs.slack_context          is 'Slack trigger payload forwarded to the agent.';
comment on column public.agent_runs.slack_connection_id    is 'Slack workspace that triggered this run.';
comment on column public.agent_runs.model                  is 'Model used (e.g. claude-haiku-4-5).';
comment on column public.agent_runs.response               is 'Final text response returned by the agent.';
comment on column public.agent_runs.iterations             is 'Number of LLM round-trips the agent performed.';
comment on column public.agent_runs.tool_call_count        is 'Total Notion tool calls made during the run.';
comment on column public.agent_runs.success                is 'False if the run hit an unrecoverable error.';
comment on column public.agent_runs.error                  is 'Error message when success = false.';
comment on column public.agent_runs.completed_at           is 'Wall-clock time the run finished.';

create index if not exists agent_runs_created_at_idx
  on public.agent_runs (created_at desc);

create index if not exists agent_runs_success_idx
  on public.agent_runs (success)
  where success = false;

create index if not exists agent_runs_slack_connection_id_idx
  on public.agent_runs (slack_connection_id)
  where slack_connection_id is not null;

grant select, insert on public.agent_runs to service_role;


-- =====================================================
-- 4. agent_tool_calls
-- One row per Notion tool call within an agent run.
-- =====================================================

create table if not exists public.agent_tool_calls (
  id           uuid        primary key default gen_random_uuid(),
  agent_run_id uuid        not null references public.agent_runs(id) on delete cascade,
  tool_name    text        not null,
  args         jsonb       not null default '{}',
  result       jsonb       null,
  success      boolean     not null default true,
  error        text        null,
  called_at    timestamptz not null default now()
);

comment on table  public.agent_tool_calls           is 'Individual Notion tool calls made by the agent during a run.';
comment on column public.agent_tool_calls.tool_name is 'Name of the tool called (e.g. get_page, create_page, update_page).';
comment on column public.agent_tool_calls.args      is 'Arguments the model passed to the tool.';
comment on column public.agent_tool_calls.result    is 'Result returned by the tool.';
comment on column public.agent_tool_calls.success   is 'False if the tool returned an error.';

create index if not exists agent_tool_calls_agent_run_id_idx
  on public.agent_tool_calls (agent_run_id);

create index if not exists agent_tool_calls_tool_name_idx
  on public.agent_tool_calls (tool_name);

create index if not exists agent_tool_calls_called_at_idx
  on public.agent_tool_calls (called_at desc);

grant select, insert on public.agent_tool_calls to service_role;


-- =====================================================
-- 5. page_operations
-- One row per direct Notion API call (non-agent path).
-- =====================================================

create table if not exists public.page_operations (
  id                  uuid        primary key default gen_random_uuid(),
  slack_connection_id uuid        null
                                  references public.slack_connections(id) on delete set null,
  operation           text        not null
                                  check (operation in (
                                    'get_page',
                                    'create_page',
                                    'update_page',
                                    'update_page_by_key',
                                    'list_databases',
                                    'get_database',
                                    'get_database_pages'
                                  )),
  page_id             text        null,
  database_id         text        null,
  database_name       text        null,
  property_key        text        null,
  properties          jsonb       null,
  success             boolean     not null default true,
  error               text        null,
  created_at          timestamptz not null default now()
);

comment on table  public.page_operations                     is 'Audit log for direct Notion API calls (non-agent).';
comment on column public.page_operations.slack_connection_id is 'Slack workspace this operation was made for.';
comment on column public.page_operations.operation           is 'Which endpoint was called.';
comment on column public.page_operations.page_id             is 'Notion page ID, when applicable.';
comment on column public.page_operations.database_id         is 'Notion database ID, when applicable.';
comment on column public.page_operations.database_name       is 'Logical database name for key-mapped updates.';
comment on column public.page_operations.property_key        is 'Property key used in key-mapped update calls.';
comment on column public.page_operations.properties          is 'Property name→value map supplied in create or update calls.';
comment on column public.page_operations.success             is 'False if the Notion API returned an error.';

create index if not exists page_operations_created_at_idx
  on public.page_operations (created_at desc);

create index if not exists page_operations_page_id_idx
  on public.page_operations (page_id)
  where page_id is not null;

create index if not exists page_operations_operation_idx
  on public.page_operations (operation);

create index if not exists page_operations_slack_connection_id_idx
  on public.page_operations (slack_connection_id)
  where slack_connection_id is not null;

grant select, insert on public.page_operations to service_role;


-- =====================================================
-- 6. agent_conversation_sessions
-- Multi-turn agent state per (slack_team_id, slack_user_id).
-- One row per user per workspace; upserted on each turn.
--
-- Column notes:
--   openai_response_id    — stores Anthropic tool_use_id when agent
--                           asks a question and awaits a reply.
--   openai_conversation_id — unused in community edition (Anthropic
--                           does not have a Conversations API).
--   pending_reply_channel_id — Slack channel where agent is waiting
--                           for a user reply via Events API.
-- =====================================================

create table if not exists public.agent_conversation_sessions (
  id                       uuid        primary key default gen_random_uuid(),
  slack_team_id            text        not null,
  slack_user_id            text        not null,
  message_history          jsonb       not null default '[]',
  openai_response_id       text        null,
  openai_conversation_id   text        null,
  pending_reply_channel_id text        null,
  updated_at               timestamptz not null default now(),

  unique (slack_team_id, slack_user_id)
);

comment on table  public.agent_conversation_sessions                          is 'Agent conversation state per (team, user) pair for multi-turn continuity.';
comment on column public.agent_conversation_sessions.slack_team_id            is 'Slack workspace/team id.';
comment on column public.agent_conversation_sessions.slack_user_id            is 'Slack user id within the team.';
comment on column public.agent_conversation_sessions.message_history          is 'Chat messages (user, assistant, tool) kept for multi-turn continuity (capped at 40).';
comment on column public.agent_conversation_sessions.openai_response_id       is 'Stores Anthropic tool_use_id when agent is waiting for a user reply (pending_reply flow).';
comment on column public.agent_conversation_sessions.openai_conversation_id   is 'Reserved; unused in community edition.';
comment on column public.agent_conversation_sessions.pending_reply_channel_id is 'Slack channel where agent is waiting for a reply; cleared when reply is received.';
comment on column public.agent_conversation_sessions.updated_at               is 'Last activity timestamp.';

create index if not exists agent_conversation_sessions_team_user_idx
  on public.agent_conversation_sessions (slack_team_id, slack_user_id);

create index if not exists agent_conversation_sessions_updated_at_idx
  on public.agent_conversation_sessions (updated_at desc);

grant select, insert, update on public.agent_conversation_sessions to service_role;


-- =====================================================
-- General schema grant
-- =====================================================

grant usage on schema public to service_role;
