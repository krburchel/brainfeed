-- ---------------------------------------------------------------
-- Agent API tokens: only a SHA-256 fingerprint is ever stored.
-- Owners manage their own rows from the web app (RLS); the edge
-- function reads them with the service role.
-- ---------------------------------------------------------------
create table public.api_tokens (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  name text not null default 'Hermes' check (length(name) between 1 and 60),
  token_hash text not null unique check (token_hash ~ '^sha256:[0-9a-f]{64}$'),
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at timestamptz
);
create index api_tokens_user on public.api_tokens (user_id);
alter table public.api_tokens enable row level security;
create policy "own tokens read" on public.api_tokens for select to authenticated
  using ((select auth.uid()) = user_id);
create policy "own tokens add" on public.api_tokens for insert to authenticated
  with check ((select auth.uid()) = user_id and revoked_at is null and last_used_at is null);
create policy "own tokens revoke" on public.api_tokens for update to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy "own tokens delete" on public.api_tokens for delete to authenticated
  using ((select auth.uid()) = user_id);
-- Owners may only change name / revoked_at, never the hash or owner
revoke update on public.api_tokens from authenticated;
grant update (name, revoked_at) on public.api_tokens to authenticated;

-- ---------------------------------------------------------------
-- Reminder delivery: claim with a lease, then ack.
-- ---------------------------------------------------------------
alter table public.reminders
  add column claim_id uuid,
  add column claimed_until timestamptz;

-- (private.next_occurrence is replaced in 20261001000200_next_occurrence_anchor.sql)
create or replace function private.next_occurrence(p_due timestamptz, p_repeat text, p_tz text)
returns timestamptz language plpgsql stable set search_path = '' as $$
declare
  l timestamp := p_due at time zone p_tz;
  n timestamptz := p_due;
  guard int := 0;
begin
  if p_repeat is null then return p_due; end if;
  while n <= now() and guard < 5000 loop
    guard := guard + 1;
    if p_repeat = 'daily' then l := l + interval '1 day';
    elsif p_repeat = 'weekly' then l := l + interval '1 week';
    elsif p_repeat = 'monthly' then l := l + interval '1 month';
    elsif p_repeat = 'yearly' then l := l + interval '1 year';
    elsif p_repeat = 'weekdays' then
      l := l + interval '1 day';
      while extract(isodow from l) > 5 loop l := l + interval '1 day'; end loop;
    else return p_due;
    end if;
    n := l at time zone p_tz;
  end loop;
  return n;
end $$;

-- Editing rules: moving a reminder's time clears any claim, and
-- moving a completed reminder into the future reactivates it.
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
  return new;
end $$;
create trigger reminders_before_update before update on public.reminders
  for each row execute function private.reminders_before_update();

-- Atomically claim due, unclaimed reminders for one user.
create or replace function public.bf_claim_reminders(p_user uuid, p_limit int default 20, p_lease int default 300)
returns table (id uuid, body text, due_at timestamptz, repeat text, claim_id uuid)
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
  returning r.id, r.body, r.due_at, r.repeat, r.claim_id;
end $$;

-- Mark a claim delivered: one-time -> done; repeating -> next time.
create or replace function public.bf_ack_reminders(p_user uuid, p_claim uuid)
returns table (id uuid, body text, done boolean, due_at timestamptz, repeat text)
language plpgsql security definer set search_path = '' as $$
declare tz text;
begin
  select coalesce((select s.timezone from public.settings s where s.user_id = p_user), 'UTC') into tz;
  return query
  update public.reminders r
     set last_sent_at = now(),
         done = (r.repeat is null),
         due_at = case when r.repeat is null then r.due_at else private.next_occurrence(r.due_at, r.repeat, tz) end,
         claim_id = null, claimed_until = null
   where r.user_id = p_user and r.claim_id = p_claim
  returning r.id, r.body, r.done, r.due_at, r.repeat;
end $$;

revoke all on function public.bf_claim_reminders(uuid, int, int) from public, anon, authenticated;
revoke all on function public.bf_ack_reminders(uuid, uuid) from public, anon, authenticated;
grant execute on function public.bf_claim_reminders(uuid, int, int) to service_role;
grant execute on function public.bf_ack_reminders(uuid, uuid) to service_role;
