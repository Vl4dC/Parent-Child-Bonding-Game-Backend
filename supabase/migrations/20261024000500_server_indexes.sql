begin;
-- Global worker queries should not scan unrelated schoolwork, decks, and attempts.
create index records_job_status_schedule on api_private.records ((data->>'status'),created_at,id)
    where kind='jobs';
-- Owner-specific enqueue limits can use a compact partial index.
create index records_pending_owner on api_private.records (owner_id,id)
    where kind='jobs' and data->>'status' in ('queued','running');
commit;
