-- Hermes 1.4 review fixes.

-- 1. Nesting rules under concurrency. The checks below used plain reads, so two
--    transactions could both pass (A into B while B into A, or a child added under A
--    while A moves into B). Every write that sets a parent now takes the same per-user
--    transaction lock first, so these checks run one at a time and each sees the other's
--    committed result (plpgsql statements take a fresh snapshot under READ COMMITTED).
create or replace function private.nesting_lock(p_user uuid)
returns void language sql set search_path = '' as $$
  select pg_advisory_xact_lock(hashtextextended('brainfeed-nesting:' || p_user::text, 0));
$$;

create or replace function private.notes_check_parent()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if new.parent_id is null then return new; end if;
  perform private.nesting_lock(new.user_id);
  if new.parent_id = new.id then raise exception 'A note cannot contain itself'; end if;
  if not exists (select 1 from public.notes p where p.id = new.parent_id and p.user_id = new.user_id and p.parent_id is null) then
    raise exception 'That note cannot hold other notes';
  end if;
  if exists (select 1 from public.notes c where c.parent_id = new.id) then
    raise exception 'A note that holds other notes cannot be moved inside another';
  end if;
  return new;
end $$;

-- 2. One atomic note edit for the agent API: body / pin / archive / parent / tags
--    all land together or not at all, and the row lock stops concurrent tag edits
--    from overwriting each other. NO KEY UPDATE (not FOR UPDATE) so it never
--    deadlocks with a child insert's foreign-key check on this row.
create or replace function public.bf_update_note(
  p_user uuid, p_note uuid, p_body text, p_pinned boolean, p_archived boolean,
  p_set_parent boolean, p_parent uuid, p_add_tags text[], p_remove_tags text[])
returns setof uuid language plpgsql security definer set search_path = '' as $$
begin
  if p_set_parent and p_parent is not null then perform private.nesting_lock(p_user); end if;
  perform 1 from public.notes n where n.id = p_note and n.user_id = p_user for no key update;
  if not found then return; end if;
  if p_body is not null or p_pinned is not null or p_archived is not null then
    update public.notes n
       set body = coalesce(p_body, n.body),
           pinned = coalesce(p_pinned, n.pinned),
           archived_at = case when p_archived is null then n.archived_at
                              when p_archived then coalesce(n.archived_at, now()) else null end
     where n.id = p_note;
  end if;
  if p_set_parent then
    update public.notes n set parent_id = p_parent where n.id = p_note;
  end if;
  if coalesce(cardinality(p_add_tags), 0) > 0 or coalesce(cardinality(p_remove_tags), 0) > 0 then
    update public.notes n
       set tags = array(select distinct t from unnest(n.tags || coalesce(p_add_tags, '{}')) t
                        where t <> all (coalesce(p_remove_tags, '{}')))
     where n.id = p_note;
  end if;
  return next p_note;
end $$;
revoke all on function public.bf_update_note(uuid, uuid, text, boolean, boolean, boolean, uuid, text[], text[]) from public, anon, authenticated;
grant execute on function public.bf_update_note(uuid, uuid, text, boolean, boolean, boolean, uuid, text[], text[]) to service_role;
revoke all on function private.nesting_lock(uuid) from public, anon, authenticated;
