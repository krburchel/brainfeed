-- Private schema: not exposed through the API
create schema if not exists private;

create table private.allowed_emails (email text primary key);
-- Applied with the owner's real address; replace before reusing:
insert into private.allowed_emails values ('you@example.com');

create or replace function private.enforce_signup_allowlist()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if not exists (select 1 from private.allowed_emails a where lower(a.email) = lower(new.email)) then
    raise exception 'Signups are closed for this BrainFeed';
  end if;
  return new;
end $$;

create trigger enforce_signup_allowlist
  before insert on auth.users
  for each row execute function private.enforce_signup_allowlist();

-- Hashtag extraction: #word (must start with a letter), lowercased, deduped
create or replace function private.extract_tags(body text)
returns text[] language sql immutable set search_path = '' as $$
  select coalesce(array_agg(distinct lower(m[2])), '{}')
  from regexp_matches(coalesce(body, ''), '(^|[^&\w])#([A-Za-z][\w-]*)', 'g') as m;
$$;

-- Notes
create table public.notes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  body text not null default '',
  tags text[] not null default '{}',
  source text not null default 'web',
  pinned boolean not null default false,
  attachments jsonb not null default '[]',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  fts tsvector generated always as (to_tsvector('english', body)) stored
);
create index notes_user_created on public.notes (user_id, created_at desc);
create index notes_tags on public.notes using gin (tags);
create index notes_fts on public.notes using gin (fts);

create or replace function private.notes_before_write()
returns trigger language plpgsql security definer set search_path = '' as $$
declare
  first_word text;
  known boolean;
begin
  if tg_op = 'INSERT' then
    new.tags := coalesce(new.tags, '{}') || private.extract_tags(new.body);
  elsif new.body is distinct from old.body and new.tags = old.tags then
    -- keep manually-added tags, re-derive hashtags from the new body
    new.tags := private.extract_tags(new.body) ||
      array(select t from unnest(old.tags) t where t <> all (private.extract_tags(old.body)));
  end if;

  -- "A tag's word files it": if the first word matches an existing tag, file it there
  first_word := lower(substring(new.body from '^\s*([A-Za-z][\w-]*)'));
  if first_word is not null and first_word <> all (new.tags) then
    select exists (
      select 1 from public.notes n
      where n.user_id = new.user_id and first_word = any (n.tags)
    ) into known;
    if known then new.tags := new.tags || first_word; end if;
  end if;

  new.tags := array(select distinct lower(t) from unnest(new.tags) t where t <> '' order by 1);
  if tg_op = 'UPDATE' then new.updated_at := now(); end if;
  return new;
end $$;

create trigger notes_before_write
  before insert or update on public.notes
  for each row execute function private.notes_before_write();

-- Reminders
create table public.reminders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  body text not null,
  due_at timestamptz not null,
  repeat text check (repeat in ('daily', 'weekdays', 'weekly', 'monthly', 'yearly')),
  done boolean not null default false,
  last_sent_at timestamptz,
  source text not null default 'web',
  created_at timestamptz not null default now()
);
create index reminders_due on public.reminders (user_id, done, due_at);

-- Settings (one row per user)
create table public.settings (
  user_id uuid primary key default auth.uid() references auth.users on delete cascade,
  board_tags text[] not null default '{}',
  timezone text not null default 'America/Los_Angeles',
  updated_at timestamptz not null default now()
);

-- Row level security
alter table public.notes enable row level security;
alter table public.reminders enable row level security;
alter table public.settings enable row level security;

create policy "own notes" on public.notes for all to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy "own reminders" on public.reminders for all to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy "own settings" on public.settings for all to authenticated
  using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

-- Tag counts for the sidebar
create or replace function public.tag_counts()
returns table (tag text, count bigint) language sql stable security invoker set search_path = '' as $$
  select t, count(*) from public.notes n, unnest(n.tags) t
  where n.user_id = (select auth.uid())
  group by t order by count(*) desc, t;
$$;

-- Private photo/file bucket, one folder per user
insert into storage.buckets (id, name, public, file_size_limit)
values ('attachments', 'attachments', false, 20971520);

create policy "own attachments read" on storage.objects for select to authenticated
  using (bucket_id = 'attachments' and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy "own attachments write" on storage.objects for insert to authenticated
  with check (bucket_id = 'attachments' and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy "own attachments delete" on storage.objects for delete to authenticated
  using (bucket_id = 'attachments' and (storage.foldername(name))[1] = (select auth.uid())::text);
