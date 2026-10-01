// BrainFeed agent API (v1).
//
// A deliberately small, fixed set of operations for an agent like Hermes:
// add / search / edit notes, add / search / edit reminders, and claim/ack
// due reminders for delivery. No deletes, no arbitrary queries.
//
// Auth: "X-BrainFeed-Token: <64 hex chars>" (a bearer token in a custom
// header: Supabase's gateway logs a prefix of any Authorization header, so
// that header is refused outright). Only sha256:<hex> of the token is
// stored (public.api_tokens). The token row decides the user; clients
// can never supply a user_id. The service-role key used below is
// injected by Supabase and never leaves this function.
import { createClient } from 'jsr:@supabase/supabase-js@2';

const db = createClient(Deno.env.get('SUPABASE_URL')!, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!, {
  auth: { persistSession: false, autoRefreshToken: false },
});

const NOTE_COLS = 'id,body,tags,source,pinned,created_at,updated_at';
const REM_COLS = 'id,body,due_at,repeat,done,last_sent_at,source,created_at';
const REPEATS = ['daily', 'weekdays', 'weekly', 'monthly', 'yearly'];
const SOURCES = ['hermes', 'telegram', 'discord', 'sms'];
const MAX_BODY = 20000;
const TAG_RE = /^[a-z][\w-]{0,49}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
const bad = (msg: string) => new ApiError(400, 'bad_request', msg);

function json(status: number, data: unknown) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });
}

// ---------------------------------------------------------------- auth
async function sha256Hex(s: string) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}
function constantTimeEqual(a: string, b: string) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

type Caller = { tokenId: string; tokenName: string; userId: string };

async function authenticate(req: Request): Promise<Caller> {
  if (req.headers.has('authorization')) {
    throw new ApiError(400, 'bad_request', 'Send the token in X-BrainFeed-Token, not Authorization');
  }
  const raw = req.headers.get('x-brainfeed-token') || '';
  if (!/^[0-9a-f]{64}$/.test(raw)) throw new ApiError(401, 'unauthorized', 'Missing or malformed token');
  const presented = 'sha256:' + (await sha256Hex(raw));
  const { data, error } = await db.from('api_tokens').select('id,name,user_id,token_hash').is('revoked_at', null);
  if (error) throw new ApiError(503, 'unavailable', 'Token lookup failed');
  // Compare against every active token without early exit.
  let match: { id: string; name: string; user_id: string } | null = null;
  for (const row of data || []) if (constantTimeEqual(row.token_hash, presented)) match = row;
  if (!match) throw new ApiError(401, 'unauthorized', 'Invalid or revoked token');
  await db.from('api_tokens').update({ last_used_at: new Date().toISOString() }).eq('id', match.id);
  return { tokenId: match.id, tokenName: match.name, userId: match.user_id };
}

// ---------------------------------------------------------- validation
function only(body: Record<string, unknown>, allowed: string[]) {
  for (const k of Object.keys(body)) if (!allowed.includes(k)) throw bad(`Unknown field: ${k}`);
}
function str(v: unknown, field: string, { required = false, max = MAX_BODY } = {}) {
  if (v === undefined || v === null) { if (required) throw bad(`${field} is required`); return undefined; }
  if (typeof v !== 'string') throw bad(`${field} must be a string`);
  const s = v.trim();
  if (required && !s) throw bad(`${field} must not be empty`);
  if (s.length > max) throw bad(`${field} is longer than ${max} characters`);
  return s;
}
function uuid(v: unknown, field = 'id') {
  if (typeof v !== 'string' || !UUID_RE.test(v)) throw bad(`${field} must be a UUID`);
  return v;
}
function tags(v: unknown, field: string) {
  if (v === undefined) return [];
  if (!Array.isArray(v) || v.length > 20) throw bad(`${field} must be an array of up to 20 tags`);
  return v.map((t) => {
    const s = typeof t === 'string' ? t.replace(/^#/, '').toLowerCase() : '';
    if (!TAG_RE.test(s)) throw bad(`Invalid tag: ${String(t)}`);
    return s;
  });
}
function int(v: unknown, field: string, def: number, min: number, max: number) {
  if (v === undefined) return def;
  if (!Number.isInteger(v) || (v as number) < min || (v as number) > max) throw bad(`${field} must be an integer ${min}-${max}`);
  return v as number;
}
function bool(v: unknown, field: string) {
  if (v === undefined) return undefined;
  if (typeof v !== 'boolean') throw bad(`${field} must be true or false`);
  return v;
}
function repeat(v: unknown) {
  if (v === undefined) return undefined;
  if (v === null || v === 'none') return null;
  if (typeof v !== 'string' || !REPEATS.includes(v)) throw bad(`repeat must be one of: none, ${REPEATS.join(', ')}`);
  return v;
}
function source(v: unknown) {
  if (v === undefined) return 'hermes';
  if (typeof v !== 'string' || !SOURCES.includes(v)) throw bad(`source must be one of: ${SOURCES.join(', ')}`);
  return v;
}

// --------------------------------------------------------- time zones
async function userTz(userId: string) {
  const { data } = await db.from('settings').select('timezone').eq('user_id', userId).maybeSingle();
  return data?.timezone || 'UTC';
}
// Offset (ms) of a zone at a given instant
function tzOffset(tz: string, at: Date) {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).formatToParts(at).map((x) => [x.type, x.value]));
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
  return asUtc - Math.floor(at.getTime() / 1000) * 1000;
}
// "2026-10-01T09:00" in tz -> Date
function localToUtc(local: string, tz: string) {
  const m = local.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$/);
  if (!m) throw bad('due_local must look like 2026-10-01T09:00 (no offset)');
  const guess = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  let t = guess - tzOffset(tz, new Date(guess));
  t = guess - tzOffset(tz, new Date(t)); // settle across DST edges
  return new Date(t);
}
function parseDue(body: Record<string, unknown>, tz: string, required: boolean) {
  const { due_at, due_local } = body;
  if (due_at !== undefined && due_local !== undefined) throw bad('Send due_at or due_local, not both');
  if (due_at !== undefined) {
    if (typeof due_at !== 'string' || !/(Z|[+-]\d{2}:?\d{2})$/.test(due_at)) throw bad('due_at must be ISO 8601 with Z or an offset');
    const d = new Date(due_at);
    if (isNaN(d.getTime())) throw bad('due_at is not a valid date');
    return d;
  }
  if (due_local !== undefined) {
    if (typeof due_local !== 'string') throw bad('due_local must be a string');
    return localToUtc(due_local, tz);
  }
  if (required) throw bad('due_at or due_local is required');
  return undefined;
}
function fmtLocal(iso: string | null, tz: string) {
  if (!iso) return null;
  return new Intl.DateTimeFormat('en-US', { timeZone: tz, weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(iso));
}
const withLocal = (r: Record<string, unknown>, tz: string) => ({
  ...r, due_local: fmtLocal(r.due_at as string, tz), last_sent_local: fmtLocal((r.last_sent_at as string) ?? null, tz),
});

// ------------------------------------------------------------- routes
type Handler = (c: Caller, body: Record<string, unknown>) => Promise<unknown>;

const routes: Record<string, Handler> = {
  'GET /v1/status': async (c) => {
    const [{ count: notes }, { count: open }, tz, { data: u }] = await Promise.all([
      db.from('notes').select('id', { count: 'exact', head: true }).eq('user_id', c.userId),
      db.from('reminders').select('id', { count: 'exact', head: true }).eq('user_id', c.userId).eq('done', false),
      userTz(c.userId),
      db.auth.admin.getUserById(c.userId),
    ]);
    const email = u?.user?.email || '';
    return {
      ok: true, api: 'brainfeed/v1', token: c.tokenName,
      account: email.replace(/^(.).*(@.*)$/, '$1***$2'),
      timezone: tz, now_local: fmtLocal(new Date().toISOString(), tz),
      notes, open_reminders: open,
    };
  },

  'POST /v1/notes': async (c, b) => {
    only(b, ['body', 'tags', 'source']);
    const text = str(b.body, 'body', { required: true })!;
    const { data, error } = await db.from('notes')
      .insert({ user_id: c.userId, body: text, tags: tags(b.tags, 'tags'), source: source(b.source) })
      .select(NOTE_COLS).single();
    if (error) throw new ApiError(500, 'db_error', 'Could not save note');
    return { note: data };
  },

  'POST /v1/notes/search': async (c, b) => {
    only(b, ['q', 'tags', 'pinned', 'limit', 'before']);
    const q = str(b.q, 'q', { max: 200 }) || '';
    const tagList = tags(b.tags, 'tags');
    let query = db.from('notes').select(NOTE_COLS).eq('user_id', c.userId)
      .order('created_at', { ascending: false }).limit(int(b.limit, 'limit', 20, 1, 50));
    if (tagList.length) query = query.contains('tags', tagList);
    if (bool(b.pinned, 'pinned') !== undefined) query = query.eq('pinned', b.pinned as boolean);
    if (b.before !== undefined) {
      const d = new Date(String(b.before));
      if (isNaN(d.getTime())) throw bad('before must be an ISO date');
      query = query.lt('created_at', d.toISOString());
    }
    for (const w of q.split(/\s+/).filter(Boolean).slice(0, 8)) {
      if (/^#[A-Za-z][\w-]*$/.test(w)) query = query.contains('tags', [w.slice(1).toLowerCase()]);
      else query = query.ilike('body', `%${w.replace(/[%_\\]/g, '\\$&')}%`);
    }
    const { data, error } = await query;
    if (error) throw new ApiError(500, 'db_error', 'Search failed');
    return { notes: data };
  },

  'POST /v1/notes/get': async (c, b) => {
    only(b, ['id']);
    const { data } = await db.from('notes').select(NOTE_COLS).eq('user_id', c.userId).eq('id', uuid(b.id)).maybeSingle();
    if (!data) throw new ApiError(404, 'not_found', 'Note not found');
    return { note: data };
  },

  'POST /v1/notes/update': async (c, b) => {
    only(b, ['id', 'body', 'add_tags', 'remove_tags', 'pinned']);
    const id = uuid(b.id);
    const { data: cur } = await db.from('notes').select('id,tags').eq('user_id', c.userId).eq('id', id).maybeSingle();
    if (!cur) throw new ApiError(404, 'not_found', 'Note not found');
    const text = str(b.body, 'body');
    if (b.body !== undefined && !text) throw bad('body must not be empty');
    const add = tags(b.add_tags, 'add_tags'), remove = tags(b.remove_tags, 'remove_tags');
    const pinned = bool(b.pinned, 'pinned');
    if (text === undefined && !add.length && !remove.length && pinned === undefined) throw bad('Nothing to update');
    // Body first: the database re-derives #hashtags from the new text.
    if (text !== undefined || pinned !== undefined) {
      const patch: Record<string, unknown> = {};
      if (text !== undefined) patch.body = text;
      if (pinned !== undefined) patch.pinned = pinned;
      const { error } = await db.from('notes').update(patch).eq('user_id', c.userId).eq('id', id);
      if (error) throw new ApiError(500, 'db_error', 'Could not update note');
    }
    if (add.length || remove.length) {
      const { data: now } = await db.from('notes').select('tags').eq('user_id', c.userId).eq('id', id).single();
      const next = [...new Set([...(now!.tags as string[]), ...add])].filter((t) => !remove.includes(t));
      const { error } = await db.from('notes').update({ tags: next }).eq('user_id', c.userId).eq('id', id);
      if (error) throw new ApiError(500, 'db_error', 'Could not update tags');
    }
    const { data } = await db.from('notes').select(NOTE_COLS).eq('user_id', c.userId).eq('id', id).single();
    return { note: data };
  },

  'POST /v1/reminders': async (c, b) => {
    only(b, ['body', 'due_at', 'due_local', 'repeat', 'source']);
    const tz = await userTz(c.userId);
    const due = parseDue(b, tz, true)!;
    const { data, error } = await db.from('reminders').insert({
      user_id: c.userId, body: str(b.body, 'body', { required: true, max: 2000 }),
      due_at: due.toISOString(), repeat: repeat(b.repeat) ?? null, source: source(b.source),
    }).select(REM_COLS).single();
    if (error) throw new ApiError(500, 'db_error', 'Could not save reminder');
    return { reminder: withLocal(data, tz), timezone: tz };
  },

  'POST /v1/reminders/search': async (c, b) => {
    only(b, ['q', 'include_done', 'limit']);
    const tz = await userTz(c.userId);
    let query = db.from('reminders').select(REM_COLS).eq('user_id', c.userId)
      .order('due_at', { ascending: true }).limit(int(b.limit, 'limit', 25, 1, 100));
    if (!bool(b.include_done, 'include_done')) query = query.eq('done', false);
    const q = str(b.q, 'q', { max: 200 });
    for (const w of (q || '').split(/\s+/).filter(Boolean).slice(0, 8)) query = query.ilike('body', `%${w.replace(/[%_\\]/g, '\\$&')}%`);
    const { data, error } = await query;
    if (error) throw new ApiError(500, 'db_error', 'Search failed');
    return { reminders: data.map((r) => withLocal(r, tz)), timezone: tz };
  },

  'POST /v1/reminders/update': async (c, b) => {
    only(b, ['id', 'body', 'due_at', 'due_local', 'repeat', 'done']);
    const id = uuid(b.id);
    const tz = await userTz(c.userId);
    const patch: Record<string, unknown> = {};
    const text = str(b.body, 'body', { max: 2000 });
    if (b.body !== undefined) { if (!text) throw bad('body must not be empty'); patch.body = text; }
    const due = parseDue(b, tz, false); if (due) patch.due_at = due.toISOString();
    const rep = repeat(b.repeat); if (rep !== undefined) patch.repeat = rep;
    const done = bool(b.done, 'done'); if (done !== undefined) patch.done = done;
    if (!Object.keys(patch).length) throw bad('Nothing to update');
    const { data, error } = await db.from('reminders').update(patch).eq('user_id', c.userId).eq('id', id).select(REM_COLS).maybeSingle();
    if (error) throw new ApiError(500, 'db_error', 'Could not update reminder');
    if (!data) throw new ApiError(404, 'not_found', 'Reminder not found');
    return { reminder: withLocal(data, tz), timezone: tz };
  },

  'POST /v1/reminders/claim': async (c, b) => {
    only(b, ['limit', 'lease_seconds']);
    const tz = await userTz(c.userId);
    const { data, error } = await db.rpc('bf_claim_reminders', {
      p_user: c.userId, p_limit: int(b.limit, 'limit', 20, 1, 50), p_lease: int(b.lease_seconds, 'lease_seconds', 300, 30, 3600),
    });
    if (error) throw new ApiError(500, 'db_error', 'Claim failed');
    const now = Date.now();
    return {
      claim_id: data[0]?.claim_id ?? null, timezone: tz,
      reminders: data.map((r: Record<string, unknown>) => ({
        id: r.id, body: r.body, repeat: r.repeat, due_at: r.due_at, due_local: fmtLocal(r.due_at as string, tz),
        late_minutes: Math.max(0, Math.round((now - new Date(r.due_at as string).getTime()) / 60000)),
      })),
    };
  },

  'POST /v1/reminders/ack': async (c, b) => {
    only(b, ['claim_id']);
    const tz = await userTz(c.userId);
    const { data, error } = await db.rpc('bf_ack_reminders', { p_user: c.userId, p_claim: uuid(b.claim_id, 'claim_id') });
    if (error) throw new ApiError(500, 'db_error', 'Ack failed');
    return { acked: data.length, reminders: data.map((r: Record<string, unknown>) => withLocal(r, tz)) };
  },

  'POST /v1/token/revoke': async (c, b) => {
    only(b, []);
    await db.from('api_tokens').update({ revoked_at: new Date().toISOString() }).eq('id', c.tokenId);
    return { revoked: true };
  },
};

Deno.serve(async (req) => {
  try {
    const url = new URL(req.url);
    // Path arrives as /brainfeed-api/v1/... ; strip the function name.
    const path = url.pathname.replace(/^\/(functions\/v1\/)?brainfeed-api/, '') || '/';
    const handler = routes[`${req.method} ${path}`];
    if (!handler) throw new ApiError(404, 'not_found', `No route: ${req.method} ${path}`);
    const caller = await authenticate(req);
    let body: Record<string, unknown> = {};
    if (req.method === 'POST') {
      const raw = await req.text();
      if (raw.length > 64000) throw new ApiError(413, 'too_large', 'Request too large');
      if (raw) {
        try { body = JSON.parse(raw); } catch { throw bad('Body must be JSON'); }
        if (!body || typeof body !== 'object' || Array.isArray(body)) throw bad('Body must be a JSON object');
      }
    }
    return json(200, await handler(caller, body));
  } catch (e) {
    if (e instanceof ApiError) return json(e.status, { error: e.code, message: e.message });
    // Never echo request details (headers may hold the token).
    console.error('brainfeed-api internal error:', e instanceof Error ? e.message : 'unknown');
    return json(500, { error: 'internal', message: 'Internal error' });
  }
});
