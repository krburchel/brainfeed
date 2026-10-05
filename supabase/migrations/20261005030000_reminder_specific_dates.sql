-- "Specific dates" reminders: repeat = 'dates' with the full list in dates[];
-- due_at is always the next date that hasn't been sent.
alter table public.reminders add column dates timestamptz[];
alter table public.reminders drop constraint reminders_repeat_check;
alter table public.reminders add constraint reminders_repeat_check
  check (repeat in ('daily', 'weekdays', 'weekly', 'monthly', 'yearly', 'dates'));
alter table public.reminders add constraint reminders_dates_check
  check ((repeat = 'dates') = (dates is not null and cardinality(dates) between 1 and 100));

-- Ack: specific-dates reminders move to the next future date, or finish.
create or replace function public.bf_ack_reminders(p_user uuid, p_claim uuid)
returns table (id uuid, body text, done boolean, due_at timestamptz, repeat text)
language plpgsql security definer set search_path = '' as $$
declare tz text;
begin
  select coalesce((select s.timezone from public.settings s where s.user_id = p_user), 'UTC') into tz;
  return query
  update public.reminders r
     set last_sent_at = now(),
         done = case when r.repeat is null then true
                     when r.repeat = 'dates' then (select min(d) from unnest(r.dates) d where d > now()) is null
                     else false end,
         due_at = case when r.repeat is null then r.due_at
                       when r.repeat = 'dates' then coalesce((select min(d) from unnest(r.dates) d where d > now()), r.due_at)
                       else private.next_occurrence(r.due_at, r.repeat, tz) end,
         claim_id = null, claimed_until = null
   where r.user_id = p_user and r.claim_id = p_claim
  returning r.id, r.body, r.done, r.due_at, r.repeat;
end $$;
revoke all on function public.bf_ack_reminders(uuid, uuid) from public, anon, authenticated;
grant execute on function public.bf_ack_reminders(uuid, uuid) to service_role;
