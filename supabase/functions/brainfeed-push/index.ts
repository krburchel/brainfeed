// BrainFeed push notifications (Web Push).
//
// POST with X-BrainFeed-Cron: <secret>  → called every minute by pg_cron. Sends each
//   newly due reminder occurrence (from bf_push_claim) to every device of its owner.
// POST with the signed-in user's JWT    → { "test": true, "endpoint"?: string }: sends a
//   test notification to that device (or all of the user's devices).
//
// Payloads are encrypted for each device by the Web Push protocol, so Apple/Google's
// push services only relay ciphertext. VAPID keys and the cron secret live in Supabase
// Vault (read via bf_push_config, service role only); the service-role key is injected
// by Supabase and never leaves this function.
import { createClient } from 'jsr:@supabase/supabase-js@2';
import webpush from 'npm:web-push@3.6.7';

const db = createClient(Deno.env.get('SUPABASE_URL')!, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!, {
  auth: { persistSession: false, autoRefreshToken: false },
});
let CRON_SECRET = '';
let configured: Promise<void> | null = null;
function configure() {
  configured ||= (async () => {
    const { data, error } = await db.rpc('bf_push_config').single();
    const c = data as { vapid_public: string; vapid_private: string; cron_secret: string } | null;
    if (error || !c?.vapid_public || !c.vapid_private || !c.cron_secret) { configured = null; throw new Error('push config missing'); }
    webpush.setVapidDetails('https://krburchel.github.io/brainfeed/', c.vapid_public, c.vapid_private);
    CRON_SECRET = c.cron_secret;
  })();
  return configured;
}

const ORIGINS = ['https://krburchel.github.io', 'http://localhost:8765'];
let CORS: Record<string, string> = {};
const corsFor = (req: Request) => ({
  'Access-Control-Allow-Origin': ORIGINS.includes(req.headers.get('origin') || '') ? req.headers.get('origin')! : ORIGINS[0],
  'Access-Control-Allow-Headers': 'authorization, apikey, content-type, x-client-info',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Vary': 'Origin',
});
const json = (status: number, data: unknown) =>
  new Response(JSON.stringify(data), { status, headers: { ...CORS, 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });

function constantTimeEqual(a: string, b: string) {
  if (!a || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

type Sub = { id: string; endpoint: string; p256dh: string; auth: string; fail_count: number };

// Send one payload to one device. Expired devices (404/410) are removed; a device that
// keeps failing (10 times in a row) is removed too.
async function send(sub: Sub, payload: Record<string, unknown>): Promise<'ok' | 'gone' | 'fail'> {
  try {
    await webpush.sendNotification({ endpoint: sub.endpoint, keys: { p256dh: sub.p256dh, auth: sub.auth } },
      JSON.stringify(payload), { TTL: 3600, urgency: 'high' });
    await db.from('push_subscriptions').update({ last_ok_at: new Date().toISOString(), fail_count: 0 }).eq('id', sub.id);
    return 'ok';
  } catch (e) {
    const code = (e as { statusCode?: number }).statusCode;
    if (code === 404 || code === 410 || sub.fail_count >= 9) {
      await db.from('push_subscriptions').delete().eq('id', sub.id);
      return 'gone';
    }
    await db.from('push_subscriptions').update({ fail_count: sub.fail_count + 1 }).eq('id', sub.id);
    console.error('push send failed:', code ?? 'network');
    return 'fail';
  }
}

async function subsFor(userId: string) {
  const { data, error } = await db.from('push_subscriptions').select('id,endpoint,p256dh,auth,fail_count').eq('user_id', userId);
  if (error) throw new Error('subscription lookup failed');
  return (data || []) as Sub[];
}

async function dispatch() {
  const { data: rows, error } = await db.rpc('bf_push_claim');
  if (error) throw new Error('claim failed');
  const byUser = new Map<string, Sub[]>();
  let sent = 0, failed = 0;
  for (const r of rows || []) {
    if (!byUser.has(r.user_id)) byUser.set(r.user_id, await subsFor(r.user_id));
    const subs = byUser.get(r.user_id)!;
    const results = await Promise.all(subs.map((s) => send(s, {
      title: '⏰ BrainFeed reminder', body: r.body, tag: 'rem-' + r.reminder_id, url: './?view=reminders',
    })));
    // Done when any device got it, or when no devices are left to try.
    const ok = results.includes('ok') || results.every((x) => x === 'gone');
    byUser.set(r.user_id, subs.filter((_, i) => results[i] !== 'gone'));
    await db.rpc('bf_push_finish', { p_reminder: r.reminder_id, p_occurrence: r.occurrence, p_ok: ok });
    ok ? sent++ : failed++;
  }
  return { sent, failed };
}

async function test(userId: string, endpoint: unknown) {
  let subs = await subsFor(userId);
  if (typeof endpoint === 'string') subs = subs.filter((s) => s.endpoint === endpoint);
  if (!subs.length) return { sent: 0, message: 'This device isn\'t registered for notifications yet.' };
  const results = await Promise.all(subs.map((s) => send(s, {
    title: 'BrainFeed', body: 'Notifications are working on this device ✓', tag: 'bf-test', url: './',
  })));
  return { sent: results.filter((x) => x === 'ok').length, devices: subs.length };
}

Deno.serve(async (req) => {
  CORS = corsFor(req);
  if (req.method === 'OPTIONS') return new Response(null, { headers: CORS });
  if (req.method !== 'POST') return json(405, { error: 'method_not_allowed' });
  try {
    await configure();
    const cron = req.headers.get('x-brainfeed-cron') || '';
    if (cron) {
      if (!constantTimeEqual(cron, CRON_SECRET)) return json(401, { error: 'unauthorized' });
      return json(200, await dispatch());
    }
    const token = (req.headers.get('authorization') || '').replace(/^Bearer\s+/i, '');
    const { data: u, error } = token ? await db.auth.getUser(token) : { data: null, error: true };
    if (error || !u?.user) return json(401, { error: 'unauthorized' });
    const body = await req.json().catch(() => ({}));
    if (body?.test !== true) return json(400, { error: 'bad_request', message: 'Send {"test": true}' });
    return json(200, await test(u.user.id, body.endpoint));
  } catch (e) {
    console.error('brainfeed-push error:', e instanceof Error ? e.message : 'unknown');
    return json(500, { error: 'internal' });
  }
});
