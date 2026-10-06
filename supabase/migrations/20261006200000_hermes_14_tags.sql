-- Tag counts for the agent API (service role only; the API passes the
-- token's user, never a client-supplied one). Mirrors public.tag_counts().
create or replace function public.bf_tag_counts(p_user uuid)
returns table (tag text, count bigint) language sql stable security definer set search_path = '' as $$
  select t, count(*) from public.notes n, unnest(n.tags) t
  where n.user_id = p_user and n.archived_at is null
  group by t order by count(*) desc, t;
$$;
revoke all on function public.bf_tag_counts(uuid) from public, anon, authenticated;
grant execute on function public.bf_tag_counts(uuid) to service_role;
