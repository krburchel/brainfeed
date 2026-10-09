-- Push config for the brainfeed-push function (VAPID keys + cron secret live in Vault,
-- created out of band, never in the repo). Service role only.
create or replace function public.bf_push_config()
returns table (vapid_public text, vapid_private text, cron_secret text)
language sql stable security definer set search_path = '' as $$
  select (select decrypted_secret from vault.decrypted_secrets where name = 'brainfeed_vapid_public'),
         (select decrypted_secret from vault.decrypted_secrets where name = 'brainfeed_vapid_private'),
         (select decrypted_secret from vault.decrypted_secrets where name = 'brainfeed_push_cron');
$$;
revoke all on function public.bf_push_config() from public, anon, authenticated;
grant execute on function public.bf_push_config() to service_role;

-- Every minute: ask brainfeed-push to send newly due reminders.
select cron.schedule('brainfeed-push', '* * * * *', $job$
  select net.http_post(
    url := 'https://bzvibdjrknqvmurwjroq.supabase.co/functions/v1/brainfeed-push',
    headers := jsonb_build_object('Content-Type', 'application/json',
      'x-brainfeed-cron', (select decrypted_secret from vault.decrypted_secrets where name = 'brainfeed_push_cron')),
    body := '{}'::jsonb, timeout_milliseconds := 20000);
$job$);
