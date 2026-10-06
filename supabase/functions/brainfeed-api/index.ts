// BrainFeed agent API (v1, revision 6).
//
// A deliberately small, fixed set of operations for an agent like Hermes
// or an iPhone Shortcut: add / search / edit notes (incl. nesting and
// checklist ticks), list tags, attach photos, add / search / edit
// reminders, and claim/ack due reminders for delivery.
// No deletes, no arbitrary queries.
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

const NOTE_COLS = 'id,body,tags,source,pinned,attachments,archived_at,parent_id,created_at,updated_at';
const REM_COLS = 'id,body,due_at,repeat,dates,done,last_sent_at,source,created_at';
const REPEATS = ['daily', 'weekdays', 'weekly', 'monthly', 'yearly'];
const SOURCES = ['hermes', 'telegram', 'discord', 'sms', 'ios'];
const BUCKET = 'attachments';
const MAX_PHOTO_BYTES = 10 * 1024 * 1024;
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
  if (v === 'dates') return 'dates';
  if (typeof v !== 'string' || !REPEATS.includes(v)) throw bad(`repeat must be one of: none, ${REPEATS.join(', ')}, dates`);
  return v;
}
function source(v: unknown) {
  if (v === undefined) return 'hermes';
  if (typeof v !== 'string' || !SOURCES.includes(v)) throw bad(`source must be one of: ${SOURCES.join(', ')}`);
  return v;
}

// Database rule violations worth showing the agent as-is (nesting trigger).
function dbError(error: { message?: string } | null, fallback: string) {
  const m = error?.message || '';
  if (/cannot contain itself|cannot hold other notes|cannot be moved inside another/.test(m)) return bad(m);
  return new ApiError(500, 'db_error', fallback);
}
function optUuid(v: unknown, field: string) {
  if (v === undefined) return undefined;
  if (v === null) return null;
  return uuid(v, field);
}
const firstLine = (body: string) => (body.split('\n').find((l) => l.trim()) || '').replace(/^#{1,3} |^\s*[-*] \[[ xX]\] /, '').slice(0, 80);

// Checklists are plain-text lines: "- [ ] milk" / "- [x] eggs" (same rule as the web app).
const CHECK_RE = /^(\s*[-*] \[)([ xX])(\] ?)(.*)$/;
function checklist(body: string) {
  return body.split('\n').flatMap((l, i) => { const m = l.match(CHECK_RE); return m ? [{ line: i, done: m[2] !== ' ', text: m[4] }] : []; });
}
type Item = ReturnType<typeof checklist>[number];
// An item is picked by its number (1 = first checklist item) or by its text:
// an exact (case-insensitive) match wins, otherwise the text must be in exactly one item.
function pickItem(items: Item[], sel: unknown, field: string): Item {
  if (Number.isInteger(sel)) {
    const it = items[(sel as number) - 1];
    if (!it) throw bad(`${field}: there is no item ${sel} (the note has ${items.length})`);
    return it;
  }
  if (typeof sel !== 'string' || !sel.trim() || sel.length > 500) throw bad(`${field} entries must be item numbers or item text`);
  const q = sel.trim().toLowerCase();
  const exact = items.filter((x) => x.text.trim().toLowerCase() === q);
  if (exact.length === 1) return exact[0];
  const part = exact.length ? exact : items.filter((x) => x.text.toLowerCase().includes(q));
  if (part.length === 1) return part[0];
  if (!part.length) throw new ApiError(400, 'no_match', `${field}: no checklist item matches "${sel}"`);
  throw new ApiError(400, 'ambiguous', `${field}: "${sel}" matches ${part.length} items (${part.map((x) => x.text).join(' | ')}); use the item number`);
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
// Local calendar parts of an instant in tz.
function localParts(at: Date, tz: string) {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }).formatToParts(at).map((x) => [x.type, x.value]));
  return { y: +p.year, m: +p.month, d: +p.day, h: +p.hour, mi: +p.minute };
}
const pad = (n: number) => String(n).padStart(2, '0');
const dateKey = (y: number, m: number, d: number) => `${y}-${pad(m)}-${pad(d)}`;
const localKey = (at: Date, tz: string) => { const p = localParts(at, tz); return dateKey(p.y, p.m, p.d); };
function shortTime(at: Date, tz: string) {
  return new Intl.DateTimeFormat('en-US', { timeZone: tz, hour: 'numeric', minute: '2-digit' }).format(at).replace(/\s/g, ' ');
}

// Occurrences of a reminder in [from, to), using the same rules as the
// database's next_occurrence: wall-clock time in tz, anchored to the
// original date, month-end days clamped.
function occurrences(r: Record<string, unknown>, from: Date, to: Date, tz: string): Date[] {
  const due = new Date(r.due_at as string);
  if (r.repeat === 'dates') return ((r.dates as string[]) || []).map((x) => new Date(x)).filter((x) => x >= from && x < to);
  if (!r.repeat || r.done) return due >= from && due < to ? [due] : [];
  const a = localParts(due, tz);
  const at = (k: number): Date | null => {
    let y = a.y, m = a.m, d = a.d;
    if (r.repeat === 'daily' || r.repeat === 'weekdays' || r.repeat === 'weekly') {
      const t = new Date(Date.UTC(a.y, a.m - 1, a.d + (r.repeat === 'weekly' ? 7 * k : k)));
      if (r.repeat === 'weekdays' && (t.getUTCDay() === 0 || t.getUTCDay() === 6)) return null;
      y = t.getUTCFullYear(); m = t.getUTCMonth() + 1; d = t.getUTCDate();
    } else {
      const months = (a.m - 1) + (r.repeat === 'monthly' ? k : 12 * k);
      y = a.y + Math.floor(months / 12); m = (months % 12) + 1;
      d = Math.min(a.d, new Date(Date.UTC(y, m, 0)).getUTCDate());
    }
    return localToUtc(`${dateKey(y, m, d)}T${pad(a.h)}:${pad(a.mi)}`, tz);
  };
  const days = Math.floor((from.getTime() - due.getTime()) / 864e5);
  let k = Math.max(0, r.repeat === 'weekly' ? Math.floor(days / 7) - 1 : r.repeat === 'monthly' ? Math.floor(days / 31) - 1
    : r.repeat === 'yearly' ? Math.floor(days / 366) - 1 : days - 1);
  const out: Date[] = [];
  for (let n = 0; n < 400; n++, k++) {
    const x = at(k);
    if (!x) continue;
    if (x >= to) break;
    if (x >= from && x >= due) out.push(x);
  }
  return out;
}

function parseDatesLocal(v: unknown, tz: string) {
  if (!Array.isArray(v) || v.length < 1 || v.length > 100) throw bad('dates_local must be a list of 1-100 local times like 2026-10-09T18:30');
  const ds = v.map((x) => { if (typeof x !== 'string') throw bad('dates_local entries must be strings'); return localToUtc(x, tz); })
    .sort((p, q) => p.getTime() - q.getTime());
  const next = ds.find((d) => d.getTime() > Date.now());
  if (!next) throw bad('dates_local needs at least one future date');
  return { dates: ds, next };
}

const withLocal = (r: Record<string, unknown>, tz: string) => ({
  ...r, due_local: fmtLocal(r.due_at as string, tz), last_sent_local: fmtLocal((r.last_sent_at as string) ?? null, tz),
});

// ------------------------------------------------------------- photos
// The type comes from the file's leading bytes, never from its name or headers.
function sniffImage(b: Uint8Array): { type: string; ext: string } | null {
  const at = (i: number, str: string) => [...str].every((ch, k) => b[i + k] === ch.charCodeAt(0));
  if (b.length > 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return { type: 'image/jpeg', ext: 'jpg' };
  if (b.length > 8 && b[0] === 0x89 && at(1, 'PNG\r\n')) return { type: 'image/png', ext: 'png' };
  if (b.length > 6 && (at(0, 'GIF87a') || at(0, 'GIF89a'))) return { type: 'image/gif', ext: 'gif' };
  if (b.length > 12 && at(0, 'RIFF') && at(8, 'WEBP')) return { type: 'image/webp', ext: 'webp' };
  return null;
}

// POST /v1/photos[?note_id=UUID][&source=S][&name=N]  body: raw image bytes.
// With note_id: attach to that note. Without: create a new "📷 Photo" note.
// Captions are never taken from the URL (query strings end up in logs).
async function handlePhoto(c: Caller, req: Request, url: URL) {
  for (const k of url.searchParams.keys()) {
    if (!['note_id', 'source', 'name'].includes(k)) throw bad(`Unknown parameter: ${k}`);
  }
  if (Number(req.headers.get('content-length') || 0) > MAX_PHOTO_BYTES) throw new ApiError(413, 'too_large', 'Photos must be 10 MB or smaller');
  const bytes = new Uint8Array(await req.arrayBuffer());
  if (!bytes.length) throw bad('Empty upload');
  if (bytes.length > MAX_PHOTO_BYTES) throw new ApiError(413, 'too_large', 'Photos must be 10 MB or smaller');
  const kind = sniffImage(bytes);
  if (!kind) throw new ApiError(415, 'unsupported_media', 'Only JPEG, PNG, GIF or WebP images are accepted');
  const src = source(url.searchParams.get('source') ?? undefined);

  let noteId = url.searchParams.get('note_id');
  let created = false;
  if (noteId) {
    uuid(noteId, 'note_id');
    const { data } = await db.from('notes').select('id,attachments').eq('user_id', c.userId).eq('id', noteId).maybeSingle();
    if (!data) throw new ApiError(404, 'not_found', 'Note not found');
    if ((data.attachments as unknown[]).length >= 10) throw bad('A note can have at most 10 attachments');
  } else {
    const { data, error } = await db.from('notes').insert({ user_id: c.userId, body: '📷 Photo', source: src }).select('id').single();
    if (error) throw new ApiError(500, 'db_error', 'Could not create note');
    noteId = data.id as string;
    created = true;
  }

  const base = (url.searchParams.get('name') || 'photo').replace(/\.[^.]*$/, '').replace(/[^\w-]+/g, '_').slice(0, 60) || 'photo';
  const fileName = `${base}.${kind.ext}`;
  const path = `${c.userId}/${noteId}/${Date.now()}-${fileName}`;
  const up = await db.storage.from(BUCKET).upload(path, bytes, { contentType: kind.type, upsert: false });
  if (up.error) {
    if (created) await db.from('notes').delete().eq('user_id', c.userId).eq('id', noteId);
    throw new ApiError(500, 'storage_error', 'Could not store photo');
  }
  const { data: rows, error } = await db.rpc('bf_append_attachment', {
    p_user: c.userId, p_note: noteId, p_att: { path, name: fileName, type: kind.type, size: bytes.length },
  });
  if (error || !rows?.length) {
    await db.storage.from(BUCKET).remove([path]);
    if (created) await db.from('notes').delete().eq('user_id', c.userId).eq('id', noteId);
    throw new ApiError(error ? 500 : 400, error ? 'db_error' : 'bad_request', error ? 'Could not attach photo' : 'A note can have at most 10 attachments');
  }
  return { note: rows[0], message: 'Photo saved to BrainFeed ✓' };
}

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
    only(b, ['body', 'tags', 'source', 'parent_id']);
    const text = str(b.body, 'body', { required: true })!;
    const parent = optUuid(b.parent_id, 'parent_id') ?? null;
    const { data, error } = await db.from('notes')
      .insert({ user_id: c.userId, body: text, tags: tags(b.tags, 'tags'), source: source(b.source), parent_id: parent })
      .select(NOTE_COLS).single();
    if (error) throw dbError(error, 'Could not save note');
    return { note: data, message: 'Saved to BrainFeed ✓' };
  },

  'POST /v1/notes/search': async (c, b) => {
    only(b, ['q', 'tags', 'pinned', 'limit', 'before', 'archived', 'parent_id']);
    const q = str(b.q, 'q', { max: 200 }) || '';
    const tagList = tags(b.tags, 'tags');
    let query = db.from('notes').select(NOTE_COLS).eq('user_id', c.userId)
      .order('created_at', { ascending: false }).limit(int(b.limit, 'limit', 20, 1, 50));
    if (tagList.length) query = query.contains('tags', tagList);
    if (bool(b.pinned, 'pinned') !== undefined) query = query.eq('pinned', b.pinned as boolean);
    // archived: true = only archived, false = leave them out; omitted = both.
    const arch = bool(b.archived, 'archived');
    if (arch === true) query = query.not('archived_at', 'is', null);
    if (arch === false) query = query.is('archived_at', null);
    if (b.parent_id !== undefined) query = query.eq('parent_id', uuid(b.parent_id, 'parent_id'));
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
    const [{ data: kids }, { data: parent }] = await Promise.all([
      db.from('notes').select('id,body,archived_at').eq('user_id', c.userId).eq('parent_id', data.id).order('created_at').limit(100),
      data.parent_id ? db.from('notes').select('id,body').eq('user_id', c.userId).eq('id', data.parent_id).maybeSingle() : Promise.resolve({ data: null }),
    ]);
    return {
      note: data,
      parent: parent ? { id: parent.id, first_line: firstLine(parent.body as string) } : null,
      children: (kids || []).map((k) => ({ id: k.id, first_line: firstLine(k.body as string), archived: !!k.archived_at })),
      checklist: checklist(data.body as string).map((x, i) => ({ n: i + 1, done: x.done, text: x.text })),
    };
  },

  'POST /v1/notes/update': async (c, b) => {
    only(b, ['id', 'body', 'add_tags', 'remove_tags', 'pinned', 'archived', 'parent_id']);
    const id = uuid(b.id);
    const { data: cur } = await db.from('notes').select('id,tags').eq('user_id', c.userId).eq('id', id).maybeSingle();
    if (!cur) throw new ApiError(404, 'not_found', 'Note not found');
    const text = str(b.body, 'body');
    if (b.body !== undefined && !text) throw bad('body must not be empty');
    const add = tags(b.add_tags, 'add_tags'), remove = tags(b.remove_tags, 'remove_tags');
    const pinned = bool(b.pinned, 'pinned');
    const archived = bool(b.archived, 'archived');
    // parent_id: a note id moves this note inside it; null takes it out.
    const parent = optUuid(b.parent_id, 'parent_id');
    if (text === undefined && !add.length && !remove.length && pinned === undefined && archived === undefined && parent === undefined) throw bad('Nothing to update');
    // Body first: the database re-derives #hashtags from the new text.
    if (text !== undefined || pinned !== undefined || archived !== undefined || parent !== undefined) {
      const patch: Record<string, unknown> = {};
      if (text !== undefined) patch.body = text;
      if (pinned !== undefined) patch.pinned = pinned;
      if (archived !== undefined) patch.archived_at = archived ? new Date().toISOString() : null;
      if (parent !== undefined) patch.parent_id = parent;
      const { error } = await db.from('notes').update(patch).eq('user_id', c.userId).eq('id', id);
      if (error) throw dbError(error, 'Could not update note');
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

  'POST /v1/notes/append': async (c, b) => {
    only(b, ['id', 'body']);
    const id = uuid(b.id);
    const text = str(b.body, 'body', { required: true })!;
    const { data: cur } = await db.from('notes').select('id').eq('user_id', c.userId).eq('id', id).maybeSingle();
    if (!cur) throw new ApiError(404, 'not_found', 'Note not found');
    const tz = await userTz(c.userId);
    const stamp = '— ' + new Intl.DateTimeFormat('en-US', { timeZone: tz, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
      .format(new Date()).replace(/\s/g, ' ');
    // One UPDATE in the database: concurrent appends can't overwrite each other.
    const { data: rows, error } = await db.rpc('bf_append_note', { p_user: c.userId, p_note: id, p_stamp: stamp, p_text: text });
    if (error) throw new ApiError(500, 'db_error', 'Could not update note');
    if (!rows?.length) throw bad('Note would be too long');
    const { data } = await db.from('notes').select(NOTE_COLS).eq('user_id', c.userId).eq('id', id).single();
    return { note: data, message: 'Added to the note ✓' };
  },

  'POST /v1/notes/check': async (c, b) => {
    only(b, ['id', 'check', 'uncheck', 'add']);
    const id = uuid(b.id);
    const list = (v: unknown, field: string) => {
      if (v === undefined) return [];
      if (!Array.isArray(v) || !v.length || v.length > 50) throw bad(`${field} must be a list of 1-50 entries`);
      return v;
    };
    const check = list(b.check, 'check'), uncheck = list(b.uncheck, 'uncheck');
    const add = list(b.add, 'add').map((t) => str(t, 'add item', { required: true, max: 500 })!.replace(/\s*\n\s*/g, ' '));
    if (!check.length && !uncheck.length && !add.length) throw bad('Send check, uncheck or add');
    // Optimistic write: only lands if nobody changed the note since we read it.
    for (let attempt = 0; attempt < 4; attempt++) {
      const { data: cur } = await db.from('notes').select('body,updated_at').eq('user_id', c.userId).eq('id', id).maybeSingle();
      if (!cur) throw new ApiError(404, 'not_found', 'Note not found');
      const lines = (cur.body as string).split('\n');
      const items = checklist(cur.body as string);
      const set = (sel: unknown, field: string, done: boolean) => {
        const it = pickItem(items, sel, field);
        lines[it.line] = lines[it.line].replace(CHECK_RE, (_m, a, _x, z, t) => a + (done ? 'x' : ' ') + z + t);
        return it.text;
      };
      const checked = check.map((x) => set(x, 'check', true));
      const unchecked = uncheck.map((x) => set(x, 'uncheck', false));
      if (add.length) {
        const at = items.length ? items[items.length - 1].line + 1 : lines.length;
        const fresh = add.map((t) => '- [ ] ' + t);
        if (!items.length && lines.length && lines[lines.length - 1].trim()) fresh.unshift('');
        lines.splice(at, 0, ...fresh);
      }
      const body = lines.join('\n');
      if (body.length > MAX_BODY) throw bad('Note would be too long');
      const { data: rows, error } = await db.from('notes').update({ body }).eq('user_id', c.userId).eq('id', id)
        .eq('updated_at', cur.updated_at).select(NOTE_COLS);
      if (error) throw new ApiError(500, 'db_error', 'Could not update note');
      if (rows?.length) {
        const now = checklist(rows[0].body as string);
        return {
          note: rows[0], checked, unchecked, added: add,
          checklist: now.map((x, i) => ({ n: i + 1, done: x.done, text: x.text })),
          message: `${now.filter((x) => x.done).length}/${now.length} done`,
        };
      }
    }
    throw new ApiError(409, 'conflict', 'The note kept changing; try again');
  },

  'POST /v1/tags': async (c, b) => {
    only(b, ['limit']);
    const { data, error } = await db.rpc('bf_tag_counts', { p_user: c.userId });
    if (error) throw new ApiError(500, 'db_error', 'Tag lookup failed');
    const limit = int(b.limit, 'limit', 200, 1, 500);
    return { tags: (data || []).slice(0, limit).map((t: { tag: string; count: number }) => ({ tag: t.tag, count: Number(t.count) })) };
  },

  'POST /v1/calendar': async (c, b) => {
    only(b, ['from_local', 'days']);
    const tz = await userTz(c.userId);
    const days = int(b.days, 'days', 7, 1, 31);
    let start: string;
    if (b.from_local === undefined) { const p = localParts(new Date(), tz); start = dateKey(p.y, p.m, p.d); }
    else if (typeof b.from_local === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(b.from_local)) start = b.from_local;
    else throw bad('from_local must look like 2026-10-05');
    const from = localToUtc(`${start}T00:00`, tz);
    const [y, m, d] = start.split('-').map(Number);
    const endDay = new Date(Date.UTC(y, m - 1, d + days));
    const to = localToUtc(`${dateKey(endDay.getUTCFullYear(), endDay.getUTCMonth() + 1, endDay.getUTCDate())}T00:00`, tz);
    const [{ data: rems, error: e1 }, { data: notes, error: e2 }] = await Promise.all([
      db.from('reminders').select(REM_COLS).eq('user_id', c.userId),
      db.from('notes').select('id,body,created_at').eq('user_id', c.userId).is('archived_at', null)
        .gte('created_at', from.toISOString()).lt('created_at', to.toISOString()).order('created_at').limit(500),
    ]);
    if (e1 || e2) throw new ApiError(500, 'db_error', 'Calendar lookup failed');
    const out: Record<string, { date: string; weekday: string; reminders: unknown[]; notes: unknown[] }> = {};
    for (let i = 0; i < days; i++) {
      const t = new Date(Date.UTC(y, m - 1, d + i));
      const key = dateKey(t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate());
      out[key] = { date: key, weekday: new Intl.DateTimeFormat('en-US', { weekday: 'long', timeZone: 'UTC' }).format(t), reminders: [], notes: [] };
    }
    const now = Date.now();
    for (const r of rems || []) {
      for (const at of occurrences(r, from, to, tz)) {
        const day = out[localKey(at, tz)];
        if (!day) continue;
        const real = at.getTime() === new Date(r.due_at as string).getTime();
        day.reminders.push({
          id: r.id, body: r.body, time_local: shortTime(at, tz), repeat: r.repeat,
          status: r.done || (r.repeat === 'dates' && !real && at < new Date(r.due_at as string)) ? 'done'
            : real && at.getTime() <= now ? 'overdue' : real ? 'scheduled' : 'repeat',
          _t: at.getTime(),
        });
      }
    }
    for (const n of notes || []) {
      const day = out[localKey(new Date(n.created_at as string), tz)];
      if (day) day.notes.push({ id: n.id, first_line: ((n.body as string).split('\n').find((l) => l.trim()) || '').slice(0, 80) });
    }
    for (const day of Object.values(out)) {
      (day.reminders as { _t: number }[]).sort((p, q) => p._t - q._t).forEach((x) => delete (x as Record<string, unknown>)._t);
    }
    // Open reminders that were due before this range, so "overdue" is complete.
    const overdue = (rems || [])
      .filter((r) => !r.done && new Date(r.due_at as string) < from)
      .sort((p, q) => new Date(p.due_at as string).getTime() - new Date(q.due_at as string).getTime())
      .map((r) => ({ id: r.id, body: r.body, due_local: fmtLocal(r.due_at as string, tz), repeat: r.repeat }));
    return { timezone: tz, from_local: start, overdue_before: overdue, days: Object.values(out) };
  },

  'POST /v1/reminders': async (c, b) => {
    only(b, ['body', 'due_at', 'due_local', 'dates_local', 'repeat', 'source']);
    const tz = await userTz(c.userId);
    let due: Date, rep = repeat(b.repeat) ?? null, dates: string[] | null = null;
    if (b.dates_local !== undefined) {
      if (b.due_at !== undefined || b.due_local !== undefined) throw bad('Send dates_local or a single due time, not both');
      if (rep !== null && rep !== undefined && b.repeat !== 'dates') throw bad('dates_local cannot be combined with another repeat');
      const p = parseDatesLocal(b.dates_local, tz);
      due = p.next; rep = 'dates'; dates = p.dates.map((x) => x.toISOString());
    } else {
      if (b.repeat === 'dates') throw bad('repeat "dates" needs dates_local');
      due = parseDue(b, tz, true)!;
    }
    const { data, error } = await db.from('reminders').insert({
      user_id: c.userId, body: str(b.body, 'body', { required: true, max: 2000 }),
      due_at: due.toISOString(), repeat: rep, dates, source: source(b.source),
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
    only(b, ['id', 'body', 'due_at', 'due_local', 'dates_local', 'repeat', 'done']);
    const id = uuid(b.id);
    const tz = await userTz(c.userId);
    const { data: cur } = await db.from('reminders').select('repeat').eq('user_id', c.userId).eq('id', id).maybeSingle();
    if (!cur) throw new ApiError(404, 'not_found', 'Reminder not found');
    const patch: Record<string, unknown> = {};
    const text = str(b.body, 'body', { max: 2000 });
    if (b.body !== undefined) { if (!text) throw bad('body must not be empty'); patch.body = text; }
    const due = parseDue(b, tz, false);
    const rep = repeat(b.repeat);
    if (b.dates_local !== undefined) {
      // Replace the whole date list at once; due_at becomes its next future date.
      if (due) throw bad('Send dates_local or a single due time, not both');
      if (rep !== undefined && rep !== 'dates') throw bad('dates_local cannot be combined with another repeat');
      const p = parseDatesLocal(b.dates_local, tz);
      Object.assign(patch, { repeat: 'dates', dates: p.dates.map((x) => x.toISOString()), due_at: p.next.toISOString(), done: false });
    } else {
      if (rep === 'dates') throw bad('repeat "dates" needs dates_local (the full list of dates)');
      if (cur.repeat === 'dates' && rep === undefined && due) {
        throw bad('This reminder is on specific dates: send dates_local with the full new list instead of a single time');
      }
      if (due) patch.due_at = due.toISOString();
      if (rep !== undefined) { patch.repeat = rep; patch.dates = null; }
    }
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
        id: r.id, body: r.body, repeat: r.repeat, has_more_dates: r.has_more_dates === true,
        due_at: r.due_at, due_local: fmtLocal(r.due_at as string, tz),
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
    if (!handler && !(req.method === 'POST' && path === '/v1/photos')) throw new ApiError(404, 'not_found', `No route: ${req.method} ${path}`);
    const caller = await authenticate(req);
    if (req.method === 'POST' && path === '/v1/photos') return json(200, await handlePhoto(caller, req, url));
    let body: Record<string, unknown> = {};
    if (req.method === 'POST') {
      const raw = await req.text();
      if (raw.length > 64000) throw new ApiError(413, 'too_large', 'Request too large');
      if (raw) {
        try { body = JSON.parse(raw); } catch { throw bad('Body must be JSON'); }
        if (!body || typeof body !== 'object' || Array.isArray(body)) throw bad('Body must be a JSON object');
      }
    }
    return json(200, await handler!(caller, body));
  } catch (e) {
    if (e instanceof ApiError) return json(e.status, { error: e.code, message: e.message });
    // Never echo request details (headers may hold the token).
    console.error('brainfeed-api internal error:', e instanceof Error ? e.message : 'unknown');
    return json(500, { error: 'internal', message: 'Internal error' });
  }
});
