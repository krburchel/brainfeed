create or replace function private.next_occurrence(p_due timestamptz, p_repeat text, p_tz text, p_now timestamptz default now())
returns timestamptz language plpgsql stable set search_path = '' as $$
declare
  l0 timestamp := p_due at time zone p_tz;
  l timestamp := l0;
  n timestamptz := p_due;
  k int := 0;
  step interval;
begin
  if p_repeat is null then return p_due; end if;
  step := case p_repeat when 'daily' then interval '1 day' when 'weekly' then interval '1 week'
                        when 'monthly' then interval '1 month' when 'yearly' then interval '1 year'
                        when 'weekdays' then interval '1 day' end;
  if step is null then return p_due; end if;
  while n <= p_now and k < 20000 loop
    k := k + 1;
    -- always offset from the anchor so month-end days don't drift
    l := l0 + step * k;
    if p_repeat = 'weekdays' and extract(isodow from l) > 5 then continue; end if;
    n := l at time zone p_tz;
  end loop;
  return n;
end $$;
drop function if exists private.next_occurrence(timestamptz, text, text);
