-- Owned attempt records use existing RLS; links preserve source/material deletion cascades.
create table api_private.card_attempt_links (
  owner_id uuid not null, source_deck_id text not null, lesson_id text not null,
  attempt_id text not null, deck_kind text not null default 'decks' check(deck_kind='decks'),
  lesson_kind text not null default 'lessons' check(lesson_kind='lessons'),
  attempt_kind text not null default 'card_attempts' check(attempt_kind='card_attempts'),
  primary key(owner_id,attempt_id),
  foreign key(owner_id,deck_kind,source_deck_id) references api_private.records(owner_id,kind,id) on delete cascade,
  foreign key(owner_id,lesson_kind,lesson_id) references api_private.records(owner_id,kind,id) on delete cascade,
  foreign key(owner_id,attempt_kind,attempt_id) references api_private.records(owner_id,kind,id) on delete cascade
);
create index card_attempt_links_deck on api_private.card_attempt_links(owner_id,source_deck_id);
create index card_attempt_links_lesson on api_private.card_attempt_links(owner_id,lesson_id);
alter table api_private.card_attempt_links enable row level security;
revoke all on api_private.card_attempt_links from public,anon,authenticated;
create function api_private.card_attempt_link_insert() returns trigger
language plpgsql security definer set search_path='' as $$
begin
  insert into api_private.card_attempt_links(owner_id,source_deck_id,lesson_id,attempt_id)
    values(new.owner_id,new.data->>'source_deck_id',new.data->>'lesson_id',new.id);
  return new;
end $$;
revoke all on function api_private.card_attempt_link_insert() from public,anon,authenticated;
create trigger card_attempt_link_insert after insert on api_private.records
  for each row when (new.kind='card_attempts') execute function api_private.card_attempt_link_insert();
create index card_attempt_history on api_private.records(owner_id,(data->>'source_deck_id'),id)
  where kind='card_attempts';
create index card_attempt_receipt_target on api_private.records(owner_id,(data->>'attempt_id'))
  where kind='card_attempt_keys';
create function api_private.card_attempt_source_deleted() returns trigger
language plpgsql security definer set search_path='' as $$
begin
  delete from api_private.records where owner_id=old.owner_id
    and kind='card_attempts' and id=old.attempt_id;
  delete from api_private.records where owner_id=old.owner_id
    and kind='card_attempt_keys' and data->>'attempt_id'=old.attempt_id;
  return old;
end $$;
revoke all on function api_private.card_attempt_source_deleted() from public,anon,authenticated;
create trigger card_attempt_source_deleted after delete on api_private.card_attempt_links
  for each row execute function api_private.card_attempt_source_deleted();
