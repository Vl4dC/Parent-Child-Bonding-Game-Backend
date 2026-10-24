-- Frozen snapshots are private; room membership grants only scoped game API access.
create table api_private.diagram_templates (
  id text primary key, owner_id uuid not null, lesson_kind text not null default 'lessons'
    check(lesson_kind='lessons'), lesson_id text not null, visual_id text not null,
  source_hash text not null, data jsonb not null,
  unique(owner_id,lesson_id,visual_id,source_hash), unique(owner_id,id),
  foreign key(owner_id,lesson_kind,lesson_id)
    references api_private.records(owner_id,kind,id) on delete cascade
);
create table api_private.diagram_races (
  id text primary key, owner_id uuid not null, template_id text not null,
  creation_order bigserial not null,
  room_id text not null, data jsonb not null,
  foreign key(owner_id,template_id) references api_private.diagram_templates(owner_id,id) on delete cascade
);
create table api_private.diagram_players (
  race_id text references api_private.diagram_races(id) on delete cascade,
  user_id uuid not null, data jsonb not null, primary key(race_id,user_id)
);
create table api_private.diagram_requests (
  owner_id uuid not null, operation text not null, key_hash text not null,
  template_id text not null references api_private.diagram_templates(id) on delete cascade,
  data jsonb not null, primary key(owner_id,operation,key_hash)
);
create index diagram_races_room on api_private.diagram_races(room_id,creation_order desc);
create index diagram_players_user on api_private.diagram_players(user_id);
alter table api_private.diagram_templates enable row level security;
alter table api_private.diagram_races enable row level security;
alter table api_private.diagram_players enable row level security;
alter table api_private.diagram_requests enable row level security;
revoke all on api_private.diagram_templates,api_private.diagram_races,
  api_private.diagram_players,api_private.diagram_requests from public,anon,authenticated;

-- App-data deletion removes the verified caller's participation in other owners' matches.
create function api_private.diagram_forget_player() returns void
language sql security definer set search_path='' as $$
  delete from api_private.diagram_players where user_id=auth.uid();
$$;
revoke all on function api_private.diagram_forget_player() from public,anon;
grant execute on function api_private.diagram_forget_player() to authenticated;
