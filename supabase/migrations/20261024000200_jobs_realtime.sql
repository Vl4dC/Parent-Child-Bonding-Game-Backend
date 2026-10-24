begin;
create table api_private.generation_usage (
    owner_id uuid not null,
    day date not null,
    used integer not null,
    primary key(owner_id,day)
);
revoke all on api_private.generation_usage from public, anon, authenticated;

create function api_private.reserve_generation(owner uuid, usage_day text, maximum integer)
returns boolean language plpgsql security definer set search_path='' as $$
declare count_used integer;
begin
    insert into api_private.generation_usage values(owner,usage_day::date,1)
    on conflict(owner_id,day) do update set used=api_private.generation_usage.used+1
        where api_private.generation_usage.used < maximum returning used into count_used;
    return count_used is not null;
end $$;
revoke all on function api_private.reserve_generation(uuid,text,integer) from public, anon, authenticated;

create function api_private.claim_job(lease text, attempt_limit integer, concurrent_limit integer)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r api_private.records;
begin
    perform pg_advisory_xact_lock(hashtextextended('api_job_claim',0));
    update api_private.records set data = jsonb_set(data,'{status}','"failed"')
        where kind='jobs' and data->>'status'='running'
        and (data->>'lease_until')::timestamptz < now()
        and (data->>'attempts')::int >= attempt_limit;
    if (select count(*) from api_private.records where kind='jobs' and data->>'status'='running'
        and (data->>'lease_until')::timestamptz > now()) >= concurrent_limit then return null; end if;
    select * into r from api_private.records where kind='jobs'
        and (data->>'attempts')::int < attempt_limit
        and (data->>'status'='queued' or (data->>'status'='running' and (data->>'lease_until')::timestamptz < now()))
        order by created_at, id for update skip locked limit 1;
    if r.id is null then return null; end if;
    r.data := r.data || jsonb_build_object('status','running','lease_token',lease,
        'lease_until', now()+interval '15 minutes','attempts',(r.data->>'attempts')::int+1);
    update api_private.records set data=r.data,version=version+1,updated_at=now()
        where owner_id=r.owner_id and kind=r.kind and id=r.id;
    return jsonb_build_object('owner_id',r.owner_id,'job',r.data);
end $$;
revoke all on function api_private.claim_job(text,integer,integer) from public, anon, authenticated;

-- Supabase-only optional transport. PostgreSQL-only tests still exercise the room authority.
-- Events contain no snapshots or secrets; clients refetch an authorized snapshot after a change.
create function api_private.room_event() returns trigger language plpgsql security definer set search_path='' as $$
declare room_id text := coalesce(new.id,old.id);
begin
    if to_regprocedure('realtime.send(jsonb,text,text,boolean)') is not null then
        execute 'select realtime.send($1,$2,$3,true)'
            using jsonb_build_object('room_id',room_id),'room_changed','room:'||room_id;
    end if;
    return coalesce(new,old);
end $$;
create trigger room_event after insert or update or delete on api_private.rooms
    for each row execute function api_private.room_event();

create function api_private.is_room_member(topic text) returns boolean
language sql stable security definer set search_path='' as $$
    select exists(select 1 from api_private.rooms r, jsonb_array_elements(r.state->'members') m
        where topic='room:'||r.id and m->>'user_id'=auth.uid()::text
        and (r.state->>'expires_at')::timestamptz > now());
$$;
revoke all on function api_private.is_room_member(text) from public, anon;
grant execute on function api_private.is_room_member(text) to authenticated;
do $$ begin
    if to_regclass('realtime.messages') is not null then
        execute 'create policy api_room_receive on realtime.messages for select to authenticated
            using (api_private.is_room_member(realtime.topic()))';
    end if;
end $$;
commit;
