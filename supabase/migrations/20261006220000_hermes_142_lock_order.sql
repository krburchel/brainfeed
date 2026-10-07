-- Hermes 1.4 second review: one lock order on every path.
-- A web/app UPDATE of parent_id locks the note row, then its BEFORE trigger takes the
-- per-user nesting lock (row -> advisory). bf_update_note used to take the advisory lock
-- first (advisory -> row), so the two could deadlock. It now locks the row first and
-- leaves the advisory lock to the same trigger, so every path is row -> advisory.
-- Child inserts take the advisory lock (trigger) and then a KEY SHARE lock on the parent
-- (foreign-key check); KEY SHARE never conflicts with FOR NO KEY UPDATE, so that order
-- can't block on a row held here.
create or replace function public.bf_update_note(
  p_user uuid, p_note uuid, p_body text, p_pinned boolean, p_archived boolean,
  p_set_parent boolean, p_parent uuid, p_add_tags text[], p_remove_tags text[])
returns setof uuid language plpgsql security definer set search_path = '' as $$
begin
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
