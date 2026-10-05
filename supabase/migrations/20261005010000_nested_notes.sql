alter table public.notes add column parent_id uuid references public.notes(id) on delete set null;
create index notes_parent on public.notes (parent_id) where parent_id is not null;

-- One level only, same owner, never itself.
create or replace function private.notes_check_parent()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if new.parent_id is null then return new; end if;
  if new.parent_id = new.id then raise exception 'A note cannot contain itself'; end if;
  if not exists (select 1 from public.notes p where p.id = new.parent_id and p.user_id = new.user_id and p.parent_id is null) then
    raise exception 'That note cannot hold other notes';
  end if;
  if exists (select 1 from public.notes c where c.parent_id = new.id) then
    raise exception 'A note that holds other notes cannot be moved inside another';
  end if;
  return new;
end $$;

create trigger notes_check_parent before insert or update of parent_id on public.notes
  for each row execute function private.notes_check_parent();

-- Child counts for a set of parent notes (RLS-scoped).
create or replace function public.child_counts(p_ids uuid[])
returns table (parent_id uuid, count bigint) language sql stable security invoker set search_path = '' as $$
  select n.parent_id, count(*) from public.notes n
  where n.parent_id = any (p_ids) and n.user_id = (select auth.uid())
  group by n.parent_id;
$$;
