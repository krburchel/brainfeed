alter table public.notes add column archived_at timestamptz;
create index notes_user_archived on public.notes (user_id, archived_at);

-- Tag counts reflect what's in the feed: archived notes are not counted.
create or replace function public.tag_counts()
returns table (tag text, count bigint) language sql stable security invoker set search_path = '' as $$
  select t, count(*) from public.notes n, unnest(n.tags) t
  where n.user_id = (select auth.uid()) and n.archived_at is null
  group by t order by count(*) desc, t;
$$;

create or replace function public.archived_count()
returns bigint language sql stable security invoker set search_path = '' as $$
  select count(*) from public.notes n where n.user_id = (select auth.uid()) and n.archived_at is not null;
$$;
