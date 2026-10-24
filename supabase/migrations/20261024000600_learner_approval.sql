-- Server-only approval control plane. No Auth users or family permissions are created here.
create table api_private.approval_records (
    namespace text not null check (namespace in
      ('requests','idempotency','limits','authority','outbox','decisions')),
    id text not null,
    data jsonb not null check (jsonb_typeof(data) = 'object'),
    primary key(namespace,id)
);
alter table api_private.approval_records enable row level security;
revoke all on api_private.approval_records from public, anon, authenticated;
create index approval_guardian_lookup on api_private.approval_records
  ((data->>'guardian_email'),id) where namespace='requests';
create index approval_due_mail on api_private.approval_records
  (((data->>'next_attempt_at')::bigint),id)
  where namespace='outbox' and data->>'status' in ('queued','running');
-- Deliberately no client policies or security-definer entry points. Only the trusted
-- backend control connection can inspect receipts, mail state, or authority grants.
