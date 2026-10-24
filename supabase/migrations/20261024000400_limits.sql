begin;
create table api_private.request_usage (
    owner_id uuid not null,
    minute_id bigint not null,
    used integer not null,
    primary key(owner_id,minute_id)
);
revoke all on api_private.request_usage from public,anon,authenticated;
create function api_private.reserve_request(owner uuid, minute_window bigint, maximum integer)
returns boolean language plpgsql security definer set search_path='' as $$
declare count_used integer;
begin
    insert into api_private.request_usage values(owner,minute_window,1)
    on conflict(owner_id,minute_id) do update set used=api_private.request_usage.used+1
        where api_private.request_usage.used < maximum returning used into count_used;
    return count_used is not null;
end $$;
revoke all on function api_private.reserve_request(uuid,bigint,integer) from public,anon,authenticated;
commit;
