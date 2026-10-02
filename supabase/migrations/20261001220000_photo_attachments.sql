-- Atomically append one attachment to a user's note (max 10 per note).
create or replace function public.bf_append_attachment(p_user uuid, p_note uuid, p_att jsonb)
returns table (id uuid, body text, tags text[], source text, pinned boolean, attachments jsonb, created_at timestamptz, updated_at timestamptz)
language sql security definer set search_path = '' as $$
  update public.notes n
     set attachments = n.attachments || jsonb_build_array(p_att)
   where n.id = p_note and n.user_id = p_user and jsonb_array_length(n.attachments) < 10
  returning n.id, n.body, n.tags, n.source, n.pinned, n.attachments, n.created_at, n.updated_at;
$$;
revoke all on function public.bf_append_attachment(uuid, uuid, jsonb) from public, anon, authenticated;
grant execute on function public.bf_append_attachment(uuid, uuid, jsonb) to service_role;
