-- QUWWAA — Supabase setup for the Priority tracker + Web Push.
-- Run once in Supabase → SQL Editor. Safe to re-run (idempotent).
-- The server writes to all of these with the SERVICE-ROLE key (which bypasses
-- RLS); the policies below additionally let a signed-in member read/manage their
-- own rows directly if ever needed. Anonymous push opt-ins (user_id null) are
-- managed only by the server.

-- ========================================================================
-- 1) Priority tracker
-- ========================================================================
create table if not exists public.priorities (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  label text,
  query text,
  active boolean default true,
  created_at timestamptz default now(),
  last_checked_at timestamptz
);

create table if not exists public.priority_items (
  id uuid primary key default gen_random_uuid(),
  priority_id uuid not null references public.priorities(id) on delete cascade,
  url text,
  title text,
  source text,
  published_at timestamptz,
  found_at timestamptz default now(),
  seen boolean default false,
  unique (priority_id, url)
);

create index if not exists priorities_user_id_idx on public.priorities(user_id);
create index if not exists priority_items_priority_id_idx on public.priority_items(priority_id);

alter table public.priorities enable row level security;
alter table public.priority_items enable row level security;

drop policy if exists "priorities_select_own" on public.priorities;
drop policy if exists "priorities_insert_own" on public.priorities;
drop policy if exists "priorities_update_own" on public.priorities;
drop policy if exists "priorities_delete_own" on public.priorities;
create policy "priorities_select_own" on public.priorities for select using (auth.uid() = user_id);
create policy "priorities_insert_own" on public.priorities for insert with check (auth.uid() = user_id);
create policy "priorities_update_own" on public.priorities for update using (auth.uid() = user_id);
create policy "priorities_delete_own" on public.priorities for delete using (auth.uid() = user_id);

drop policy if exists "priority_items_select_own" on public.priority_items;
drop policy if exists "priority_items_update_own" on public.priority_items;
drop policy if exists "priority_items_delete_own" on public.priority_items;
create policy "priority_items_select_own" on public.priority_items for select using (
  exists (select 1 from public.priorities p where p.id = priority_items.priority_id and p.user_id = auth.uid())
);
create policy "priority_items_update_own" on public.priority_items for update using (
  exists (select 1 from public.priorities p where p.id = priority_items.priority_id and p.user_id = auth.uid())
);
create policy "priority_items_delete_own" on public.priority_items for delete using (
  exists (select 1 from public.priorities p where p.id = priority_items.priority_id and p.user_id = auth.uid())
);

-- ========================================================================
-- 2) Web Push subscriptions
--    user_id is nullable: anonymous visitors may opt in to "brief ready".
-- ========================================================================
create table if not exists public.push_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,   -- null = anonymous brief opt-in
  endpoint text unique not null,
  p256dh text not null,
  auth text not null,
  platform text,                              -- ios | android | desktop
  notify_brief boolean default true,
  notify_breaking boolean default false,      -- Gold (phase 2)
  notify_priority boolean default true,       -- Gold
  created_at timestamptz default now(),
  last_seen_at timestamptz default now()
);

create index if not exists push_subscriptions_user_id_idx on public.push_subscriptions(user_id);

alter table public.push_subscriptions enable row level security;

drop policy if exists "push_select_own" on public.push_subscriptions;
drop policy if exists "push_insert_own" on public.push_subscriptions;
drop policy if exists "push_update_own" on public.push_subscriptions;
drop policy if exists "push_delete_own" on public.push_subscriptions;
create policy "push_select_own" on public.push_subscriptions for select using (auth.uid() = user_id);
create policy "push_insert_own" on public.push_subscriptions for insert with check (auth.uid() = user_id);
create policy "push_update_own" on public.push_subscriptions for update using (auth.uid() = user_id);
create policy "push_delete_own" on public.push_subscriptions for delete using (auth.uid() = user_id);

-- ========================================================================
-- 3) Registration-wall meter columns on profiles (server-enforced free cap:
--    1 article/day AND 5/month for FREE registered members). Server writes via
--    the service-role key; these are not client-writable.
-- ========================================================================
alter table public.profiles add column if not exists free_reads_count int default 0;
alter table public.profiles add column if not exists free_reads_month text;      -- 'YYYY-MM' (local)
alter table public.profiles add column if not exists free_reads_day text;        -- 'YYYY-MM-DD' (local)
alter table public.profiles add column if not exists free_reads_last_url text;

-- Morning Brief subscription flag (so the Profile reflects real Kit state;
-- reconciled against Kit by the server for accounts created before this existed).
alter table public.profiles add column if not exists brief_subscribed boolean default false;
alter table public.profiles add column if not exists brief_subscribed_at timestamptz;
