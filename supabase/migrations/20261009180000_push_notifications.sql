-- Push notifications for reminders (Web Push), alongside Hermes's Telegram delivery.
-- A per-minute pg_cron job calls the brainfeed-push edge function, which asks
-- bf_push_claim() what to send. Each (reminder, occurrence) is pushed at most once,
-- whichever side (Hermes ack, web check-off, push) gets there first.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- The occurrence a reminder was just moved past (ack, check-off, snooze), so a push
-- for it can still go out even if Hermes or the app advanced due_at first.
alter table public.reminders add column last_due_at timestamptz;

create or replace function private.reminders_before_update()
returns trigger language plpgsql set search_path = '' as $$
begin
  if new.due_at is distinct from old.due_at then
    new.claim_id := null; new.claimed_until := null;
    if old.done and new.done and new.due_at > now() then new.done := false; end if;
  end if;
  if new.done is distinct from old.done then
    new.claim_id := null; new.claimed_until := null;
  end if;
  -- Remember a past occurrence the reminder is leaving behind.
  if old.due_at <= now() and not old.done and (new.due_at is distinct from old.due_at or new.done) then
    new.last_due_at := old.due_at;
  end if;
  return new;
end $$;

-- One row per device that turned notifications on.
create table public.push_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  endpoint text not null unique check (endpoint ~ '^https://' and length(endpoint) <= 1000),
  p256dh text not null check (length(p256dh) <= 200),
  auth text not null check (length(auth) <= 100),
  device text check (length(device) <= 120),
  created_at timestamptz not null default now(),
  last_ok_at timestamptz,
  fail_count int not null default 0
);
alter table public.push_subscriptions enable row level security;
create policy "own subscriptions" on public.push_subscriptions for all to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
create index push_subscriptions_user on public.push_subscriptions (user_id);

-- What has been pushed. Not exposed to the API.
create table private.push_log (
  reminder_id uuid not null references public.reminders on delete cascade,
  occurrence timestamptz not null,
  status text not null default 'pending' check (status in ('pending', 'sent', 'failed')),
  attempts int not null default 0,
  locked_until timestamptz,
  sent_at timestamptz,
  primary key (reminder_id, occurrence)
);

-- Queue new due occurrences (last 2 hours, users with a device only), then claim up to
-- 50 pending ones with a 2-minute lease. Service role only.
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

create or replace function public.bf_push_finish(p_reminder uuid, p_occurrence timestamptz, p_ok boolean)
returns void language sql security definer set search_path = '' as $$
  update private.push_log
     set status = case when p_ok then 'sent' when attempts >= 3 then 'failed' else 'pending' end,
         sent_at = case when p_ok then now() end, locked_until = null
   where reminder_id = p_reminder and occurrence = p_occurrence;
$$;

revoke all on function public.bf_push_claim() from public, anon, authenticated;
revoke all on function public.bf_push_finish(uuid, timestamptz, boolean) from public, anon, authenticated;
grant execute on function public.bf_push_claim() to service_role;
grant execute on function public.bf_push_finish(uuid, timestamptz, boolean) to service_role;

-- The per-minute job (secret lives in Vault as 'brainfeed_push_cron'; created separately):
-- select cron.schedule('brainfeed-push', '* * * * *', $job$ ... net.http_post ... $job$);
