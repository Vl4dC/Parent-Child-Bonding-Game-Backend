-- Application data is deliberately outside PostgREST's default exposed public schema.
-- Never expose api_private through the Data API: records contain server-side answer keys.
begin;
create schema if not exists api_private;
revoke all on schema api_private from public, anon;
grant usage on schema api_private to authenticated;

create table api_private.records (
    owner_id uuid not null,
    kind text not null check (length(kind) between 1 and 40),
    id text not null check (length(id) between 1 and 128),
    data jsonb not null,
    version bigint not null default 1,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (owner_id, kind, id)
);
alter table api_private.records enable row level security;
create policy owned_records on api_private.records to authenticated
    using (owner_id = (select auth.uid())) with check (owner_id = (select auth.uid()));
grant select, insert, update, delete on api_private.records to authenticated;

create table api_private.rooms (
    id text primary key,
    code text not null unique,
    state jsonb not null
);
alter table api_private.rooms enable row level security;
grant select on api_private.rooms to authenticated;
create policy member_snapshot on api_private.rooms for select to authenticated using (
    exists (select 1 from jsonb_array_elements(state->'members') m
            where m->>'user_id' = (select auth.uid())::text)
    and (state->>'expires_at')::timestamptz > now()
);

create function api_private.room_command(action text, room_id text, payload jsonb)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare
    actor text := auth.uid()::text;
    r api_private.rooms;
    member jsonb;
    members jsonb;
    new_id text;
    new_code text;
begin
    if actor is null then return jsonb_build_object('status',401,'error','Sign in to continue.'); end if;
    if action = 'create' then
        if jsonb_typeof(payload->'display_name') is distinct from 'string'
           or length(trim(payload->>'display_name')) not between 1 and 60
           or coalesce(payload->>'capacity','') !~ '^[2-8]$' then
            return jsonb_build_object('status',422,'error','Invalid room settings.');
        end if;
        new_id := gen_random_uuid()::text;
        loop
            -- Eight characters from a UUID are cryptographically random hexadecimal.
            new_code := upper(substr(replace(gen_random_uuid()::text,'-',''),1,8));
            exit when not exists (select 1 from api_private.rooms where code=new_code);
        end loop;
        r.state := jsonb_build_object('id',new_id,'code',new_code,'host_id',actor,
            'capacity',(payload->>'capacity')::int,'version',1,'closed',false,
            'expires_at',now()+interval '24 hours','members',jsonb_build_array(
                jsonb_build_object('user_id',actor,'display_name',payload->>'display_name','ready',false)));
        insert into api_private.rooms values(new_id,new_code,r.state);
        return r.state;
    end if;
    if action = 'join' then
        select * into r from api_private.rooms where code=payload->>'code' for update;
    else
        select * into r from api_private.rooms where id=room_id for update;
    end if;
    if r.id is null or (r.state->>'expires_at')::timestamptz <= now() then
        return jsonb_build_object('status',404,'error','Room not found.');
    end if;
    select value into member from jsonb_array_elements(r.state->'members')
        where value->>'user_id'=actor;
    if action = 'join' then
        if (r.state->>'closed')::boolean then return jsonb_build_object('status',409,'error','Room is closed.'); end if;
        if member is null then
            if jsonb_typeof(payload->'display_name') is distinct from 'string'
               or length(trim(payload->>'display_name')) not between 1 and 60 then
                return jsonb_build_object('status',422,'error','Invalid display name.'); end if;
            if jsonb_array_length(r.state->'members') >= (r.state->>'capacity')::int then
                return jsonb_build_object('status',409,'error','Room is full.'); end if;
            r.state := jsonb_set(r.state,'{members}',(r.state->'members') || jsonb_build_array(
                jsonb_build_object('user_id',actor,'display_name',payload->>'display_name','ready',false)));
        else return r.state;
        end if;
    else
        if member is null then return jsonb_build_object('status',404,'error','Room not found.'); end if;
        if action='snapshot' then return r.state; end if;
        if (payload->>'expected_version')::bigint is distinct from (r.state->>'version')::bigint then
            return jsonb_build_object('status',409,'error','Room changed. Fetch its current snapshot.'); end if;
        if action='close' then
            if r.state->>'host_id' <> actor then return jsonb_build_object('status',403,'error','Only the host can close the room.'); end if;
            r.state := jsonb_set(r.state,'{closed}','true');
        elsif action='leave' then
            select coalesce(jsonb_agg(value),'[]') into members from jsonb_array_elements(r.state->'members')
                where value->>'user_id'<>actor;
            if jsonb_array_length(members)=0 then delete from api_private.rooms where id=r.id;
            else
                r.state := jsonb_set(jsonb_set(r.state,'{members}',members),'{host_id}',members->0->'user_id');
                r.state := jsonb_set(r.state,'{version}',to_jsonb((r.state->>'version')::bigint+1));
                update api_private.rooms set state=r.state where id=r.id;
            end if;
            return '{"left":true}';
        elsif action='ready' then
            if jsonb_typeof(payload->'ready') is distinct from 'boolean' then
                return jsonb_build_object('status',422,'error','Ready must be a boolean.'); end if;
            if (r.state->>'closed')::boolean then return jsonb_build_object('status',409,'error','Room is closed.'); end if;
            select jsonb_agg(case when value->>'user_id'=actor then jsonb_set(value,'{ready}',payload->'ready') else value end)
                into members from jsonb_array_elements(r.state->'members');
            r.state := jsonb_set(r.state,'{members}',members);
        else return jsonb_build_object('status',422,'error','Unknown room command.');
        end if;
    end if;
    r.state := jsonb_set(r.state,'{version}',to_jsonb((r.state->>'version')::bigint+1));
    update api_private.rooms set state=r.state where id=r.id;
    return r.state;
end $$;
revoke all on function api_private.room_command(text,text,jsonb) from public, anon;
grant execute on function api_private.room_command(text,text,jsonb) to authenticated;
commit;
