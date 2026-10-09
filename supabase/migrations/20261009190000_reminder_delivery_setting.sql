-- Where reminders go: 'both' (phone notifications + Telegram, default), 'push' (phone
-- only, Telegram as a backup) or 'telegram' (Telegram only).
alter table public.settings add column reminder_delivery text not null default 'both'
  check (reminder_delivery in ('both', 'push', 'telegram'));

-- Hermes's claim. For 'push' users who have a device, only reminders overdue by more
-- than 10 minutes are handed to Hermes: the push sender advances reminders it delivers,
-- so these are the ones push couldn't deliver, and Telegram becomes the backup. With
-- no device registered, 'push' behaves like 'both' so nothing is ever lost.
create or replace function public.bf_claim_reminders(p_user uuid, p_limit int default 20, p_lease int default 300)
returns table (id uuid, body text, due_at timestamptz, repeat text, claim_id uuid, has_more_dates boolean)
language plpgsql security definer set search_path = '' as $$
declare c uuid := gen_random_uuid();
  push_only boolean := coalesce((select s.reminder_delivery = 'push' from public.settings s where s.user_id = p_user), false)
    and exists (select 1 from public.push_subscriptions ps where ps.user_id = p_user);
begin
  return query
  update public.reminders r
     set claim_id = c, claimed_until = now() + make_interval(secs => least(greatest(p_lease, 30), 3600))
   where r.id in (
     select x.id from public.reminders x
      where x.user_id = p_user and not x.done and x.due_at <= now()
        and (not push_only or x.due_at <= now() - interval '10 minutes')
        and (x.claimed_until is null or x.claimed_until < now())
      order by x.due_at
      limit least(greatest(p_limit, 1), 50)
      for update skip locked)
  returning r.id, r.body, r.due_at, r.repeat, r.claim_id,
            (r.repeat = 'dates' and exists (select 1 from unnest(r.dates) d where d > r.due_at));
end $$;
revoke all on function public.bf_claim_reminders(uuid, int, int) from public, anon, authenticated;
grant execute on function public.bf_claim_reminders(uuid, int, int) to service_role;

-- Push queue: skip 'telegram' users.
create or replace function public.bf_push_claim()
returns table (reminder_id uuid, occurrence timestamptz, user_id uuid, body text)
language plpgsql security definer set search_path = '' as $$
#variable_conflict use_column
begin
  delete from private.push_log d where d.occurrence < now() - interval '7 days';
  insert into private.push_log (reminder_id, occurrence)
  select r.id, o.at from public.reminders r
  cross join lateral (values (case when not r.done then r.due_at end), (r.last_due_at)) o(at)
  where o.at is not null and o.at <= now() and o.at > now() - interval '2 hours'
    and exists (select 1 from public.push_subscriptions s where s.user_id = r.user_id)
    and coalesce((select st.reminder_delivery from public.settings st where st.user_id = r.user_id), 'both') <> 'telegram'
  on conflict do nothing;

  return query
  update private.push_log l
     set attempts = l.attempts + 1, locked_until = now() + interval '2 minutes'
   where (l.reminder_id, l.occurrence) in (
     select x.reminder_id, x.occurrence from private.push_log x
      where x.status = 'pending' and x.attempts < 3 and (x.locked_until is null or x.locked_until < now())
      order by x.occurrence limit 50 for update skip locked)
  returning l.reminder_id, l.occurrence,
            (select r.user_id from public.reminders r where r.id = l.reminder_id),
            (select r.body from public.reminders r where r.id = l.reminder_id);
end $$;

-- After a successful push for a 'push' user, the push counts as the delivery: mark
-- one-time reminders done and move repeating ones to their next time, exactly as
-- Hermes's ack does. Only if the reminder is still at that occurrence and not claimed.
create or replace function public.bf_push_finish(p_reminder uuid, p_occurrence timestamptz, p_ok boolean)
returns void language plpgsql security definer set search_path = '' as $$
declare uid uuid; tz text;
begin
  update private.push_log
     set status = case when p_ok then 'sent' when attempts >= 3 then 'failed' else 'pending' end,
         sent_at = case when p_ok then now() end, locked_until = null
   where reminder_id = p_reminder and occurrence = p_occurrence;
  if not p_ok then return; end if;
  select r.user_id into uid from public.reminders r where r.id = p_reminder;
  if uid is null or coalesce((select s.reminder_delivery from public.settings s where s.user_id = uid), 'both') <> 'push' then return; end if;
  select coalesce((select s.timezone from public.settings s where s.user_id = uid), 'UTC') into tz;
  update public.reminders r
     set last_sent_at = now(),
         done = case when r.repeat is null then true
                     when r.repeat = 'dates' then (select min(d) from unnest(r.dates) d where d > now()) is null
                     else false end,
         due_at = case when r.repeat is null then r.due_at
                       when r.repeat = 'dates' then coalesce((select min(d) from unnest(r.dates) d where d > now()), r.due_at)
                       else private.next_occurrence(r.due_at, r.repeat, tz) end
   where r.id = p_reminder and not r.done and r.due_at = p_occurrence
     and (r.claimed_until is null or r.claimed_until < now());
end $$;
revoke all on function public.bf_push_claim() from public, anon, authenticated;
revoke all on function public.bf_push_finish(uuid, timestamptz, boolean) from public, anon, authenticated;
grant execute on function public.bf_push_claim() to service_role;
grant execute on function public.bf_push_finish(uuid, timestamptz, boolean) to service_role;
