# Memory OOM fix — bound the in-memory caches (CHANGES)

## Symptom
Render emailed "Web Service quwwaa exceeded its memory limit" ~daily. The Render
memory graph showed a textbook sawtooth: memory climbs from ~40% to 100% of the
512 MB starter limit, the instance is OOM-killed and auto-restarted (brief outage),
then climbs again. Two crashes in the last 12 h (5:10 AM and 10:17 AM on 2026-06-28).

## Root cause
`CACHE` (the search/board aggregate cache in `quwwaa_server.py`) was **never evicted**.
Entries are added per unique `(query, days, fast, lang)`, each holding a full payload of
articles; the 600 s TTL was only checked on read, so stale entries were never removed.
Across board categories, user/butler searches, and 5 languages it grew all day until OOM.
(`ARTICLE_CACHE` was already capped at 500 — `CACHE` was the gap.)

Secondary: the per-IP rate-limit maps (`_ip_hits`, `_speak_hits`, `_article_hits`) pruned
each IP's timestamp list but never removed the IP keys, so they grew by one entry per
unique visitor until restart. The giveaway's new anonymous traffic accelerated both.

## Fix (server-only — no front-end / SW change, UX is byte-for-byte identical)
`quwwaa_server.py`:
- Added `CACHE_MAX = 400` and `_prune_cache()`: on write, drop TTL-expired entries first,
  then evict oldest-by-age down to the cap. Called from `cached_aggregate()`. Same data
  and same 600 s freshness for users — only bounds what's kept warm in memory.
- Added `_prune_ip_bucket(d, now)`: threshold-gated (only runs when a bucket exceeds 4096
  IPs) sweep that drops IPs with no hit in the last 60 s. Wired into `article_rate_check`,
  `rate_check`, and `speak_rate_check` (all under `_ask_lock`). Throttling behavior
  unchanged.

Verified: `python3 -m py_compile` clean; unit test confirms CACHE bounds to 400 keeping
the newest, expired-first eviction, and IP-bucket pruning that keeps active IPs.

## Deploy
Push `quwwaa_server.py` to `main`; Render auto-deploys and the new process starts with the
guards in place. No env-var, plan, or front-end change. The 512 MB starter plan is now
sufficient; the sawtooth should flatten. Watch the memory graph for ~24 h to confirm.
