-- 1. A specific-dates reminder must always be due on one of its dates,
--    whatever path changes it (API, web app, delivery).
create or replace function private.reminders_dates_guard()
returns trigger language plpgsql set search_path = '' as $$
begin
  if new.repeat = 'dates' and not (new.due_at = any (new.dates)) then
    raise exception 'A specific-dates reminder must be due on one of its dates (send the full date list to change it)';
  end if;
  return new;
end $$;
create trigger reminders_dates_guard before insert or update on public.reminders
  for each row execute function private.reminders_dates_guard();

-- 2. Claims report whether a specific-dates reminder has more dates after this one.
drop function public.bf_claim_reminders(uuid, int, int);
create function public.bf_claim_reminders(p_user uuid, p_limit int default 20, p_lease int default 300)
returns table (id uuid, body text, due_at timestamptz, repeat text, claim_id uuid, has_more_dates boolean)
language plpgsql security definer set search_path = '' as $$
declare c uuid := gen_random_uuid();
begin
  return query
  update public.reminders r
     set claim_id = c, claimed_until = now() + make_interval(secs => least(greatest(p_lease, 30), 3600))
   where r.id in (
     select x.id from public.reminders x
      where x.user_id = p_user and not x.done and x.due_at <= now()
        and (x.claimed_until is null or x.claimed_until < now())
      order by x.due_at
      limit least(greatest(p_limit, 1), 50)
      for update skip locked)
  returning r.id, r.body, r.due_at, r.repeat, r.claim_id,
            (r.repeat = 'dates' and exists (select 1 from unnest(r.dates) d where d > r.due_at));
end $$;
revoke all on function public.bf_claim_reminders(uuid, int, int) from public, anon, authenticated;
grant execute on function public.bf_claim_reminders(uuid, int, int) to service_role;

-- 3. Atomic append: one UPDATE, so concurrent appends can't overwrite each other.
create or replace function public.bf_append_note(p_user uuid, p_note uuid, p_stamp text, p_text text)
returns setof uuid language plpgsql security definer set search_path = '' as $$
begin
  return query
  update public.notes n
     set body = rtrim(n.body, E' \t\n') || E'\n\n' || p_stamp || E'\n' || p_text
   where n.id = p_note and n.user_id = p_user
     and length(n.body) + length(p_stamp) + length(p_text) + 3 <= 20000
  returning n.id;
end $$;
revoke all on function public.bf_append_note(uuid, uuid, text, text) from public, anon, authenticated;
grant execute on function public.bf_append_note(uuid, uuid, text, text) to service_role;

-- Same for the web app, as the signed-in user (RLS applies).
create or replace function public.append_to_note(p_note uuid, p_stamp text, p_text text)
returns setof public.notes language sql security invoker set search_path = '' as $$
  update public.notes n
     set body = rtrim(n.body, E' \t\n') || E'\n\n' || p_stamp || E'\n' || p_text
   where n.id = p_note and n.user_id = (select auth.uid())
  returning n.*;
$$;
revoke all on function public.append_to_note(uuid, text, text) from public, anon;
grant execute on function public.append_to_note(uuid, text, text) to authenticated;
