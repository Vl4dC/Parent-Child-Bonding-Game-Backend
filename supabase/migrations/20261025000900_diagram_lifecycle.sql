-- Persist source withdrawal atomically, without rewriting source or invoking any model.
create index diagram_races_template on api_private.diagram_races(template_id);
create function api_private.diagram_invalidate_lesson() returns trigger
language plpgsql security definer set search_path='' as $$
declare
  item record;
  old_visual jsonb;
  new_visual jsonb;
  reason text;
  outcome text;
  current_ms bigint := floor(extract(epoch from clock_timestamp())*1000)::bigint;
begin
  if new.kind <> 'lessons' then return new; end if;
  for item in
    select r.id,r.data,t.visual_id
    from api_private.diagram_templates t
    join api_private.diagram_races r on r.template_id=t.id
    where t.owner_id=new.owner_id and t.lesson_id=new.id
      and r.data->>'status' in ('waiting','running')
    order by r.id for update of r
  loop
    reason := null;
    select value into old_visual from jsonb_array_elements(coalesce(old.data->'visuals','[]'::jsonb))
      where value->>'id'=item.visual_id;
    select value into new_visual from jsonb_array_elements(coalesce(new.data->'visuals','[]'::jsonb))
      where value->>'id'=item.visual_id;
    if new.data->>'status' is distinct from 'approved' then
      reason := 'source_unapproved';
    elsif old_visual is distinct from new_visual
       or old.data->'revision' is distinct from new.data->'revision'
       or new_visual is null then
      reason := 'source_changed';
    end if;
    if reason is null then continue; end if;
    if item.data->>'status'='running' and not exists (
      select 1 from api_private.diagram_players p where p.race_id=item.id
       and p.data->>'completed_at' is null and not (p.data->>'forfeited')::boolean
    ) then
      outcome := 'finished'; reason := null;
    elsif item.data->>'status'='running' and (item.data->>'deadline_at')::bigint<=current_ms then
      outcome := 'expired'; reason := 'deadline_elapsed';
    else outcome := 'cancelled';
    end if;
    update api_private.diagram_races set data=data || jsonb_build_object(
      'status',outcome,'terminal_reason',reason,'version',(data->>'version')::bigint+1
    ) where id=item.id;
  end loop;
  return new;
end $$;
revoke all on function api_private.diagram_invalidate_lesson() from public,anon,authenticated;
create trigger diagram_source_changed after update of data on api_private.records
  for each row execute function api_private.diagram_invalidate_lesson();
