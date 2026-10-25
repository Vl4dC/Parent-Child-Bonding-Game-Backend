create table api_private.duel_matches (
  id text primary key, owner_id uuid not null,
  source_kind text not null check(source_kind in ('quizzes','tournaments')), source_id text not null,
  lesson_kind text not null default 'lessons' check(lesson_kind='lessons'), lesson_id text not null,
  question_kind text not null default 'question_sets' check(question_kind='question_sets'), question_set_id text not null,
  room_id text references api_private.rooms(id) on delete cascade,
  creation_order bigserial not null, data jsonb not null,
  foreign key(owner_id,source_kind,source_id) references api_private.records(owner_id,kind,id) on delete cascade,
  foreign key(owner_id,lesson_kind,lesson_id) references api_private.records(owner_id,kind,id) on delete cascade,
  foreign key(owner_id,question_kind,question_set_id) references api_private.records(owner_id,kind,id) on delete cascade
);
create table api_private.duel_players (
  match_id text references api_private.duel_matches(id) on delete cascade,
  slot_id text not null, user_id uuid, data jsonb not null,
  primary key(match_id,slot_id), unique(match_id,user_id)
);
create table api_private.duel_requests (
  owner_id uuid not null, operation text not null, key_hash text not null,
  match_id text not null references api_private.duel_matches(id) on delete cascade,
  data jsonb not null, primary key(owner_id,operation,key_hash)
);
create index duel_room_latest on api_private.duel_matches(room_id,creation_order desc);
create index duel_owner_history on api_private.duel_matches(owner_id,id);
create index duel_source on api_private.duel_matches(owner_id,source_kind,source_id);
create index duel_lesson on api_private.duel_matches(owner_id,lesson_id);
create index duel_question_set on api_private.duel_matches(owner_id,question_set_id);
create index duel_player_user on api_private.duel_players(user_id,match_id);
create index duel_action_actors on api_private.duel_matches using gin ((data->'action_actors'));
alter table api_private.duel_matches enable row level security;
alter table api_private.duel_players enable row level security;
alter table api_private.duel_requests enable row level security;
revoke all on api_private.duel_matches,api_private.duel_players,api_private.duel_requests from public,anon,authenticated;

create function api_private.duel_invalidate_source() returns trigger
language plpgsql security definer set search_path='' as $$
declare item record; reason text; outcome text; current_ms bigint;
begin
  if new.kind not in ('lessons','quizzes','tournaments') then return new; end if;
  if new.kind='lessons' then
    if new.data->>'status'='approved' and old.data->'revision' is not distinct from new.data->'revision' then return new; end if;
  else
    if new.data->>'approved'='true' and old.data->'question_set_id' is not distinct from new.data->'question_set_id'
      and old.data->'title' is not distinct from new.data->'title'
      and old.data->'lesson_id' is not distinct from new.data->'lesson_id'
      and old.data->'questions' is not distinct from new.data->'questions' then return new; end if;
  end if;
  for item in select m.id,m.data from api_private.duel_matches m
    where m.owner_id=new.owner_id and (
      (new.kind='lessons' and m.lesson_id=new.id) or (m.source_kind=new.kind and m.source_id=new.id)
    ) and m.data->>'status' in ('waiting','running') order by m.id for update of m
  loop
    current_ms := floor(extract(epoch from clock_timestamp())*1000)::bigint;
    reason := case when (new.kind='lessons' and new.data->>'status' is distinct from 'approved')
      or (new.kind<>'lessons' and new.data->>'approved' is distinct from 'true')
      then 'source_unapproved' else 'source_changed' end;
    if (item.data->>'expires_at')::bigint<=current_ms then outcome:='expired'; reason:='deadline_elapsed';
    else outcome:='cancelled'; end if;
    update api_private.duel_matches set data=data || jsonb_build_object(
      'status',outcome,'reason',reason,'version',(data->>'version')::bigint+1) where id=item.id;
  end loop;
  return new;
end $$;
revoke all on function api_private.duel_invalidate_source() from public,anon,authenticated;
create trigger duel_source_changed after update of data on api_private.records
  for each row execute function api_private.duel_invalidate_source();

create function api_private.duel_forget_player() returns void
language plpgsql security definer set search_path='' as $$
declare item record; current_ms bigint;
begin
  for item in select m.id,m.data from api_private.duel_matches m where m.id in (
    select p.match_id from api_private.duel_players p where p.user_id=auth.uid()
    union select a.id from api_private.duel_matches a where a.data->'action_actors' ? auth.uid()::text
  ) order by m.id for update of m
  loop
    delete from api_private.duel_players where match_id=item.id and user_id=auth.uid();
    update api_private.duel_matches set data=jsonb_set(data,'{actions}',
      (select coalesce(jsonb_object_agg(key,value),'{}'::jsonb) from jsonb_each(data->'actions') where value->>'actor'<>auth.uid()::text)
    ) where id=item.id;
    update api_private.duel_matches set data=jsonb_set(data,'{action_actors}',
      (data->'action_actors') - auth.uid()::text) where id=item.id;
    if item.data->>'status' in ('waiting','running') then
      current_ms:=floor(extract(epoch from clock_timestamp())*1000)::bigint;
      update api_private.duel_matches set data=data || jsonb_build_object(
        'status',case when (data->>'expires_at')::bigint<=current_ms then 'expired' else 'cancelled' end,
        'reason','participant_removed','version',(data->>'version')::bigint+1) where id=item.id;
    end if;
  end loop;
end $$;
revoke all on function api_private.duel_forget_player() from public,anon;
grant execute on function api_private.duel_forget_player() to authenticated;
