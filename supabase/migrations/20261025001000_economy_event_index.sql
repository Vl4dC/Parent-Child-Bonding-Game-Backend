begin;
-- Quest reads inspect only the selected player's current week of activity.
-- Event records use the existing owner-scoped records RLS and request transaction.
create index records_economy_period on api_private.records (owner_id, (data->>'at'))
    where kind='economy_events';
commit;
