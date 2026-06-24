# Giveaway popup — $1,000 sign-up capture (CHANGES / deploy handoff)

Front-end is already written into the repo working tree. **No server, Supabase, or
Render config changes are required.** The only remaining step is a commit + push to
`main` (Render auto-deploys). The Cowork sandbox cannot reach GitHub (proxy 403), so
the push must happen from Mike's machine or the code agent.

## What this is
A promotional pop-up that appears right after the opening splash (CUA + date → board)
for **non-logged-in** visitors. It offers entry into a $1,000 giveaway; entering =
creating a free QUWWAA account. Built to the live dark-gold butler house style.

## Files changed
1. `quwwaa-console.html` — new self-contained `(#qgiveOv)` module appended just before
   `</body>` (one `<script>` block, ~200 lines). Nothing else in the file was touched.
2. `service-worker.js` — `CACHE` bumped `quwwaa-v73` → `quwwaa-v74` so existing PWA
   clients retire the old shell and pull the new HTML.

## Behavior / decisions
- **Audience:** shown only to anonymous visitors. A known free/paid member never sees
  it; if a member session resolves late, the popup auto-closes (never traps a member).
- **Gating:** anonymous visitors are hard-gated — no X, backdrop click does not dismiss.
  Paths out: (a) enter via email+password, (b) Continue with Google, (c) "Already a
  member? Sign in". (Per Mike's spec: "before they see anything else.")
- **Timing:** waits for the opening splash to finish (`#splash.hide`) before opening;
  triggers off the existing `qp:auth` event, with a 4.5s fallback that only fires when
  `window.QP` exists (so a missing auth layer can never trap a visitor).
- **Entry = existing free-signup path.** On submit it:
  - `QP.sb.auth.signUp({email,password,options:{data:{first_name,last_name}}})`
  - sets `localStorage.qp_pending_register='email'` → existing handler POSTs
    `/brief-subscribe` → `kit_subscribe()` (Kit daily-brief form 9570921) + marks
    `brief_subscribed`. **This is the website-account + Kit-list + free-member step,
    all reused, nothing new.**
  - sets `localStorage.qp_pending_profile={first_name,last_name,display_name,address_style}`
    → existing handler upserts the names to `profiles` once the session exists.
  - Google path calls `QP.signInWithOAuth('google', true)` (same brief-subscribe on return).
- **GA events:** `giveaway_shown`, `giveaway_entry` (method: email|google).
- **Deviations from the approved mockup:** (1) added a password field — Supabase email
  signup requires one; Google stays one-tap. (2) dark-gold theme to match the live app.
  (3) draw date left open-ended ("at the close of the giveaway") — no date supplied yet.

## Deploy steps (code agent / Mike's machine)
```
git add quwwaa-console.html service-worker.js GIVEAWAY_POPUP_CHANGES.md
git commit -m "Add $1,000 giveaway sign-up popup (anon gate, funnels to free signup + Kit)"
git push origin main          # Render auto-deploys on push to main
```

## Post-deploy test checklist
- Incognito / logged-out: splash plays → board appears → giveaway popup opens. No way to
  dismiss except entering or "Sign in".
- Email entry: creates the account, popup closes, header shows the account, and the user
  lands on the Kit daily-brief list (confirm a new active subscriber in Kit / a new
  `profiles` row with `brief_subscribed=true` and the first/last name saved).
- Google entry: completes OAuth, returns signed-in, popup gone, brief-subscribed.
- Logged-in member (free or paid): reload — popup does NOT appear.
- Service worker updated to `quwwaa-v74` (DevTools → Application → Cache Storage).

## Drawing the winner (no schema change needed)
Because the popup gates all anonymous visitors and entry = signup, every new free member
during the promo window is an entrant. Set the promo start to the deploy moment:
```sql
select u.email, p.first_name, p.last_name, p.created_at
from public.profiles p
join auth.users u on u.id = p.id
where p.created_at >= '2026-06-24T00:00:00Z'   -- set to actual go-live timestamp
order by p.created_at;
```

## Easy toggles (ask Mike before changing)
- Add a real draw date → drop it into `#qgiveHero`/`#qgiveFine` copy strings.
- Soften the gate → add an X / "Just browsing" link that calls `close()` for anon too.
- Show members an "You're already entered" confirmation instead of skipping them.
