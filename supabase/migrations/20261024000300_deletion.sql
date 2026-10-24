begin;
create function api_private.leave_all_rooms() returns void
language plpgsql security definer set search_path='' as $$
declare r api_private.rooms;
begin
    for r in select * from api_private.rooms where exists (
        select 1 from jsonb_array_elements(state->'members') m
        where m->>'user_id'=auth.uid()::text) order by id for update loop
        perform api_private.room_command('leave',r.id,jsonb_build_object('expected_version',r.state->'version'));
    end loop;
end $$;
revoke all on function api_private.leave_all_rooms() from public,anon;
grant execute on function api_private.leave_all_rooms() to authenticated;
commit;
