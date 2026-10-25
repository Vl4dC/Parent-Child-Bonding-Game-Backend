begin;
create table api_private.game_invites (
    id uuid primary key,
    sender_id uuid not null,
    recipient_id uuid not null,
    room_id text not null references api_private.rooms(id) on delete cascade,
    game text not null check(game in ('mcq','heads_up','diagram')),
    mode text not null check(mode in ('friends','parent_child','duo')),
    status text not null check(status in ('pending','accepted','declined','cancelled','expired')),
    created_at bigint not null,
    expires_at bigint not null,
    updated_at bigint not null,
    request_key text not null,
    fingerprint text not null,
    check(sender_id <> recipient_id),
    check(expires_at > created_at),
    unique(sender_id,request_key)
);
create index game_invites_inbox on api_private.game_invites(recipient_id,created_at desc,id);
create index game_invites_outbox on api_private.game_invites(sender_id,created_at desc,id);
create unique index game_invites_pending_target on api_private.game_invites(sender_id,recipient_id,room_id)
    where status='pending';
alter table api_private.game_invites enable row level security;
grant select,insert,update,delete on api_private.game_invites to authenticated;
create policy game_invite_read on api_private.game_invites for select to authenticated
    using (auth.uid() in (sender_id,recipient_id));
create policy game_invite_create on api_private.game_invites for insert to authenticated
    with check(auth.uid()=sender_id);
create policy game_invite_update on api_private.game_invites for update to authenticated
    using (auth.uid() in (sender_id,recipient_id)) with check(auth.uid() in (sender_id,recipient_id));
create policy game_invite_delete on api_private.game_invites for delete to authenticated
    using (auth.uid() in (sender_id,recipient_id));

-- A recipient cannot read a room through room RLS until joining. This narrowly scoped helper
-- locks only the invited room, after checking the selected player is an invitation participant.
-- It is called inside the same transaction as room_command('join') and the acceptance update.
create function api_private.game_invite_room(invite_id uuid)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare target text; result jsonb;
begin
    select room_id into target from api_private.game_invites
      where id=invite_id and auth.uid() in (sender_id,recipient_id);
    if target is null then return null; end if;
    select state into result from api_private.rooms where id=target for update;
    return result;
end $$;
revoke all on function api_private.game_invite_room(uuid) from public,anon;
grant execute on function api_private.game_invite_room(uuid) to authenticated;
commit;
