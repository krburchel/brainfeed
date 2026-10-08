-- Review mode: when a note was last looked at in a review session, so review
-- queues skip recently reviewed notes on every device.
alter table public.notes add column reviewed_at timestamptz;
