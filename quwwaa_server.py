#!/usr/bin/env python3
"""QUWWAA local server — serves the console AND aggregates world news on demand.
Sources: GDELT (tens of thousands of global outlets, keyless), Google News RSS,
and Reddit. Results are fetched live, held in memory only, and never written
to disk or any server."""
import json, os, re, email.utils, html, base64, time, random
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime, timezone, timedelta

PORT = int(os.environ.get('PORT', '8765'))      # cloud hosts inject PORT; 8765 locally
HOST = os.environ.get('HOST', '0.0.0.0')         # bind all interfaces in the cloud
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'}

# --- Butler brain (server-side Anthropic proxy) -----------------------------
# The key lives ONLY in the server environment, never in the page. If unset
# (e.g. the local Mac sandbox), /ask reports 'no_server_key' and the console
# falls back to a key pasted into its own settings panel.
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ASK_MODEL = os.environ.get('ASK_MODEL', 'claude-haiku-4-5-20251001')      # high-volume news summaries — keep cheap/fast
BUTLER_MODEL = os.environ.get('BUTLER_MODEL', 'claude-sonnet-4-6')        # the voice the user hears — richer delivery
JARVIS_VOICE_MODEL = os.environ.get('JARVIS_VOICE_MODEL', 'claude-opus-4-8')  # cockpit brain — Opus 4.8 (TTS gives the voice)
JARVIS_VOICE_MAX_TOKENS = int(os.environ.get('JARVIS_VOICE_MAX_TOKENS', '320'))  # concise spoken answers
ASK_RATE_PER_MIN = int(os.environ.get('ASK_RATE_PER_MIN', '6'))    # per visitor IP
ASK_DAILY_CAP = int(os.environ.get('ASK_DAILY_CAP', '2000'))       # global messages/day

# --- Speech-to-text (tap-to-talk voice on iPhone + Android) ------------------
# The browser records the audio; the server transcribes it via Whisper so the
# key stays server-side. Unset locally -> /transcribe reports 'no_stt_key'.
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
STT_MODEL = os.environ.get('STT_MODEL', 'whisper-1')

# --- Text-to-speech (the butler's spoken voice) -----------------------------
# The page POSTs the butler's reply text to /speak; the server renders it to
# speech with OpenAI TTS (key stays server-side) and returns MP3. This is what
# makes the butler audible on iPhone, where the browser's speechSynthesis is
# unreliable. Shares the OpenAI key; unset -> /speak reports 'no_tts_key'.
TTS_MODEL = os.environ.get('TTS_MODEL', 'gpt-4o-mini-tts')   # supports accent/tone steering
TTS_VOICE = os.environ.get('TTS_VOICE', 'fable')             # the most British-leaning base voice
TTS_INSTRUCTIONS = os.environ.get('TTS_INSTRUCTIONS',
    'Accent/Affect: a refined, upper-class British accent (Received Pronunciation), '
    'in the manner of a distinguished English butler - JARVIS from Iron Man. '
    'Tone: calm, courteous and articulate, with understated dry wit. '
    'Pacing: measured and unhurried. Pronunciation: crisp British English.')
SPEAK_RATE_PER_MIN = int(os.environ.get('SPEAK_RATE_PER_MIN', '20'))  # lenient, separate bucket
ARTICLE_RATE_PER_MIN = int(os.environ.get('ARTICLE_RATE_PER_MIN', '12'))  # /article backstop, own bucket

# --- Premium membership (Supabase auth/profiles + Stripe billing) -----------
# Everything read from the environment; never hardcoded. Only the PUBLIC values
# are exposed to the page via GET /config — secret keys stay server-side. When
# these are unset (e.g. the local sandbox) premium is simply disabled and the
# free experience is untouched.
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_ANON_KEY = os.environ.get('SUPABASE_ANON_KEY', '')
SUPABASE_SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')   # secret — bypasses RLS
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')                   # secret
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')           # secret
STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
SITE_URL = os.environ.get('SITE_URL', 'https://quwwaa.com').rstrip('/')   # for Checkout success/cancel
KIT_API_KEY = os.environ.get('KIT_API_KEY', '')                          # secret — newsletter auto-subscribe for paying members
KIT_FORM_ID = os.environ.get('KIT_FORM_ID', '9570921')                   # the free daily-brief form
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')                # exposed via /config (client needs it)
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')             # secret — signs Web Push
VAPID_SUBJECT = os.environ.get('VAPID_SUBJECT', 'mailto:quwwaa.io@gmail.com')
# Daily Morning Brief email (Kit broadcast). Mode: off | draft | send.
BRIEF_EMAIL_MODE = os.environ.get('BRIEF_EMAIL_MODE', 'draft').lower()
BRIEF_SEND_HOUR = int(os.environ.get('BRIEF_SEND_HOUR', '6'))                       # morning slot, America/Phoenix clock hour
BRIEF_EVENING_SEND_HOUR = int(os.environ.get('BRIEF_EVENING_SEND_HOUR', '15'))      # evening slot, MST (3 PM)
# Deliverability hardening (post-Kit-pause): send the morning brief ONLY by default —
# halving frequency is the biggest volume-risk reduction. Flip BRIEF_EVENING_ENABLED
# back on to restore the evening slot. (Evening code is kept, just gated off.)
BRIEF_EVENING_ENABLED = os.environ.get('BRIEF_EVENING_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
# While today (Phoenix) < this ISO date, co-brand every brief as "Daily Rumble" (the
# name the imported list actually recognizes) to rebuild sender recognition, then fade
# automatically. Empty = no co-branding. e.g. BRIEF_DR_COBRAND_UNTIL=2026-07-22
BRIEF_DR_COBRAND_UNTIL = os.environ.get('BRIEF_DR_COBRAND_UNTIL', '').strip()
BRIEF_COMPOSE_LEAD_MIN = int(os.environ.get('BRIEF_COMPOSE_LEAD_MIN', '60'))        # compose this many min before each send
BRIEF_COMPOSE_HOUR = int(os.environ.get('BRIEF_COMPOSE_HOUR', str(max(0, BRIEF_SEND_HOUR - 1))))
BRIEF_EMAIL_TOKEN = os.environ.get('BRIEF_EMAIL_TOKEN', '')                         # gates the manual /admin trigger
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID', '')                                # optional: who to ping for draft review
PHOENIX_TZ = timezone(timedelta(hours=-7))                                          # MST, no DST
GA_MEASUREMENT_ID = os.environ.get('GA_MEASUREMENT_ID', '')                         # GA4 (exposed via /config)
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')                           # Google One Tap (web client id; exposed via /config)
# Jarvis cockpit (admin-only command center) — see JARVIS_COCKPIT_PHASE1.md.
JARVIS_ADMIN_USER_IDS = set(x.strip() for x in os.environ.get('JARVIS_ADMIN_USER_IDS', '').split(',') if x.strip())
JARVIS_ADMIN_TOKEN = os.environ.get('JARVIS_ADMIN_TOKEN', '')                       # optional shared-secret fallback
GA_SERVICE_ACCOUNT_JSON = os.environ.get('GA_SERVICE_ACCOUNT_JSON', '')             # GA4 Data API service account (env fallback)
# Preferred: a Render Secret File holding the raw key JSON — no clipboard/newline
# corruption. The file path wins over the env var when it exists.
GA_SERVICE_ACCOUNT_FILE = os.environ.get('GA_SERVICE_ACCOUNT_FILE', '/etc/secrets/ga-service-account.json')
GA_PROPERTY_ID = os.environ.get('GA_PROPERTY_ID', '542314942')

# --- Money Trail (federal campaign-finance tracker) -------------------------
# All data is public domain: FEC openFEC API (money) + unitedstates/congress-legislators
# (roster) + unitedstates/images (CC0 portraits). The FEC key lives ONLY in the env
# (free key from api.data.gov). Without it the money/donor ETL jobs no-op cleanly and
# the roster/photo jobs + read endpoints still work (they need no FEC key).
FEC_API_KEY = os.environ.get('FEC_API_KEY', '')                     # secret — free from api.data.gov
FEC_API_BASE = 'https://api.open.fec.gov/v1'
MONEY_TRAIL_CYCLES = [int(c) for c in os.environ.get('MONEY_TRAIL_CYCLES', '2022,2024,2026').split(',') if c.strip().isdigit()]
MONEY_CURRENT_CYCLE = max(MONEY_TRAIL_CYCLES) if MONEY_TRAIL_CYCLES else 2026
MONEY_TOP_N_PAC = int(os.environ.get('MONEY_TOP_N_PAC', '15'))      # top-N general PAC givers kept per politician/cycle (context)
MONEY_DONOR_TOP_N = int(os.environ.get('MONEY_DONOR_TOP_N', '25'))  # top donors kept per tracked PAC/cycle (donor chain)
MONEY_RATE_PER_MIN = int(os.environ.get('MONEY_RATE_PER_MIN', '60'))  # generous per-IP read bucket for /money/*

# Curated pro-Israel committee list. EVERY fec_id was verified via the FEC
# /committees API (citation_url = its FEC committee page). To extend the tracker,
# add a row here — sync_money_committees() upserts it into money_committees on each
# job run (idempotent), so the DB self-heals to match this list.
PRO_ISRAEL_COMMITTEES = [
    {'fec_id': 'C00799031', 'name': 'United Democracy Project',                                 'committee_type': 'super_pac', 'connected_org': 'AIPAC',                        'tags': ['pro_israel']},
    {'fec_id': 'C00797670', 'name': 'American Israel Public Affairs Committee PAC (AIPAC PAC)',  'committee_type': 'pac',       'connected_org': 'AIPAC',                        'tags': ['pro_israel']},
    {'fec_id': 'C00699470', 'name': 'Pro-Israel America PAC',                                   'committee_type': 'pac',       'connected_org': None,                           'tags': ['pro_israel']},
    {'fec_id': 'C00247403', 'name': 'NORPAC',                                                   'committee_type': 'pac',       'connected_org': None,                           'tags': ['pro_israel']},
    {'fec_id': 'C00710848', 'name': 'DMFI PAC (Democratic Majority for Israel)',                'committee_type': 'hybrid',    'connected_org': 'Democratic Majority for Israel', 'tags': ['pro_israel']},
    {'fec_id': 'C00345132', 'name': 'Republican Jewish Coalition PAC',                          'committee_type': 'pac',       'connected_org': 'Republican Jewish Coalition',  'tags': ['pro_israel']},
    {'fec_id': 'C00441949', 'name': 'JStreetPAC',                                               'committee_type': 'pac',       'connected_org': 'J Street',                     'tags': ['pro_israel', 'pro_israel_pro_peace']},
]
PRO_ISRAEL_FEC_IDS = set(c['fec_id'] for c in PRO_ISRAEL_COMMITTEES)

def _ga_sa_raw():
    """Raw service-account JSON string + its source ('file'|'env'|'')."""
    try:
        if GA_SERVICE_ACCOUNT_FILE and os.path.exists(GA_SERVICE_ACCOUNT_FILE):
            with open(GA_SERVICE_ACCOUNT_FILE, 'r') as f:
                txt = f.read().strip()
            if txt:
                return txt, 'file'
    except Exception:
        pass
    v = (GA_SERVICE_ACCOUNT_JSON or '').strip()
    return (v, 'env') if v else ('', '')

def _ga_configured():
    return bool(_ga_sa_raw()[0])
JARVIS_CALENDAR_ID = os.environ.get('JARVIS_CALENDAR_ID', '')                       # Google Calendar shared with the SA — agenda + gated writes (Phase 2)
ANTHROPIC_ADMIN_KEY = os.environ.get('ANTHROPIC_ADMIN_KEY', '')                     # sk-ant-admin… — read-only cost/usage reports (Cost Watch)
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')                                   # read-only fine-grained PAT — repo pulse on the cockpit GitHub node
GITHUB_OWNER = os.environ.get('GITHUB_OWNER', 'Quwwaa')                             # org/user the PAT is scoped to
OPENAI_ADMIN_KEY = os.environ.get('OPENAI_ADMIN_KEY', '')                           # OpenAI admin key — read-only org costs (Cost Watch)
# Registration-wall meter for FREE registered members (server-enforced per profile).
FREE_PER_DAY = int(os.environ.get('FREE_PER_DAY', '1'))
FREE_PER_MONTH = int(os.environ.get('FREE_PER_MONTH', '5'))
# Premium activates only when the WHOLE paid flow is ready — auth (Supabase) +
# checkout (price + secret key) + status updates (webhook secret + service role).
# This prevents a half-configured state from showing a premium UI that can't
# complete. The publishable key isn't required (we use hosted Checkout redirect).
PREMIUM_ENABLED = bool(SUPABASE_URL and SUPABASE_ANON_KEY and STRIPE_PRICE_ID
                       and STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET and SUPABASE_SERVICE_ROLE_KEY)

def fetch(url, timeout=8):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

# Relative-time wording per language (minutes / hours / days). The feed shows
# these on every card and in the article meta, so they must match the chosen
# language — an English "8h ago" on an Arabic board is jarring.
AGO_FMT = {
    'en': ('%dm ago', '%dh ago', '%dd ago'),
    'es': ('hace %d min', 'hace %d h', 'hace %d d'),
    'fr': ('il y a %d min', 'il y a %d h', 'il y a %d j'),
    'ar': ('منذ %d دقيقة', 'منذ %d ساعة', 'منذ %d يوم'),
    'ru': ('%d мин назад', '%d ч назад', '%d дн назад'),
    'ms': ('%d min lalu', '%d j lalu', '%d hari lalu'),
    'id': ('%d mnt lalu', '%d j lalu', '%d hari lalu'),
}

def ago(dt, lang='en'):
    if not dt: return ''
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 0: s = 0
    mfmt, hfmt, dfmt = AGO_FMT.get(lang, AGO_FMT['en'])
    if s < 3600:  return mfmt % max(1, s // 60)
    if s < 86400: return hfmt % (s // 3600)
    return dfmt % (s // 86400)

def _gdelt_call(q, days, lang='en'):
    sl = LANGS.get(lang, LANGS['en'])['gdelt']
    url = 'https://api.gdeltproject.org/api/v2/doc/doc?' + urllib.parse.urlencode({
        'query': q + ' sourcelang:%s' % sl, 'mode': 'artlist', 'maxrecords': '40',
        'format': 'json', 'timespan': '%dd' % days, 'sort': 'datedesc'})
    body = fetch(url, timeout=7) or b'{}'
    try:
        return json.loads(body).get('articles', [])
    except ValueError:
        # GDELT returns plain-text errors (rate limits etc.) — treat as empty
        return []

def src_gdelt(q, days=7, lang='en'):
    try:
        arts = _gdelt_call(q, days, lang)
    except Exception:
        arts = []  # GDELT flaky/rate-limited — skip rather than stall the whole board
    words = q.split()
    if len(arts) < 8 and len(words) > 3:
        # query too strict — relax to the strongest three terms
        arts += _gdelt_call(' '.join(words[:3]), days, lang)
    out = []
    for a in arts:
        dt = None
        try:
            dt = datetime.strptime(a.get('seendate', ''), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        except Exception:
            pass
        out.append({'title': a.get('title', ''), 'url': a.get('url', ''),
                    'source': a.get('domain', ''), 'time': ago(dt, lang),
                    'image': a.get('socialimage', '') or '',
                    'ts': dt.timestamp() if dt else 0, 'via': 'GDELT'})
    return out

def src_gnews(q, days=7, lang='en'):
    hl, gl, ceid = LANGS.get(lang, LANGS['en'])['gnews']
    url = 'https://news.google.com/rss/search?' + urllib.parse.urlencode(
        {'q': '%s when:%dd' % (q, days), 'hl': hl, 'gl': gl, 'ceid': ceid})
    out = []
    root = ET.fromstring(fetch(url))
    for item in root.iter('item'):
        title = item.findtext('title') or ''
        link = item.findtext('link') or ''
        src = item.find('source')
        sname = src.text if src is not None and src.text else 'Google News'
        dt = None
        try:
            dt = email.utils.parsedate_to_datetime(item.findtext('pubDate') or '')
        except Exception:
            pass
        title = re.sub(r'\s+-\s+[^-]+$', '', title)  # strip trailing " - Source"
        out.append({'title': title, 'url': link, 'source': sname, 'time': ago(dt, lang),
                    'image': '',
                    'ts': dt.timestamp() if dt else 0, 'via': 'GoogleNews'})
    return out[:40]

FEEDS = [
    ('Al Jazeera',        'https://www.aljazeera.com/xml/rss/all.xml'),
    ('BBC World',         'https://feeds.bbci.co.uk/news/world/rss.xml'),
    ('BBC Middle East',   'https://feeds.bbci.co.uk/news/world/middle_east/rss.xml'),
    ('The Guardian',      'https://www.theguardian.com/world/rss'),
    ('Guardian MidEast',  'https://www.theguardian.com/world/middleeast/rss'),
    ('NPR',               'https://feeds.npr.org/1001/rss.xml'),
    ('Middle East Eye',   'https://www.middleeasteye.net/rss'),
    ('Times of Israel',   'https://www.timesofisrael.com/feed/'),
    ('Politico',          'https://rss.politico.com/politics-news.xml'),
    ('The Hill',          'https://thehill.com/feed/'),
    ('France 24',         'https://www.france24.com/en/rss'),
]

STOPWORDS = {'news', 'today', 'latest', 'update', 'updates', 'breaking', 'report',
             'reports', 'story', 'situation', 'about', 'what', 'happening', 'recent'}

def _clean_snippet(s, limit=220):
    """Cheap teaser text from an RSS <description>: strip tags/entities, collapse
    whitespace, clamp. No page fetch, no AI — the full summary is still only built
    on tap. Gives board/Lens cards a one-line teaser instead of an empty block."""
    if not s:
        return ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:limit]

def _feed_items(name, url):
    out = []
    root = ET.fromstring(fetch(url, timeout=8))
    for item in root.iter('item'):
        title = html.unescape(item.findtext('title') or '')
        link = (item.findtext('link') or '').strip()
        dt = None
        try:
            dt = email.utils.parsedate_to_datetime(item.findtext('pubDate') or '')
        except Exception:
            pass
        img = ''
        for el in item.iter():
            tag = el.tag.split('}')[-1]
            if tag in ('content', 'thumbnail') and el.get('url'):
                img = el.get('url'); break
            if tag == 'enclosure' and el.get('type', '').startswith('image') and el.get('url'):
                img = el.get('url'); break
        out.append({'title': title, 'url': link, 'source': name, 'time': ago(dt),
                    'image': img, 'snippet': _clean_snippet(item.findtext('description') or ''),
                    'ts': dt.timestamp() if dt else 0, 'via': 'RSS'})
    return out

def src_rsspack(q, days=7):
    """Curated publisher feeds, swept in parallel and keyword-filtered."""
    tokens = _tok(q) - STOPWORDS
    if not tokens:
        return []
    need = 2 if len(tokens) >= 3 else 1
    items = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_feed_items, n, u) for n, u in FEEDS]
        for fu in futs:
            try:
                items += fu.result(timeout=12)
            except Exception:
                pass  # individual feeds may flake; the pack endures
    return [a for a in items if len(tokens & _tok(a['title'])) >= need]

def hd_bing(img):
    """Bing's feed thumbnails are tiny — request the high-resolution variant."""
    if 'bing.com/th' in img:
        base = re.sub(r'[&?](w|h|c|rs|qlt|pid)=[^&]*', '', img)
        sep = '&' if '?' in base else '?'
        return base + sep + 'pid=News&w=860&h=484&c=14&rs=2&qlt=90'
    return img

def src_yahoo(q, days=7):
    """Yahoo News search RSS — direct publisher links, often with images."""
    url = 'https://news.search.yahoo.com/rss?' + urllib.parse.urlencode({'p': q})
    out = []
    root = ET.fromstring(fetch(url))
    for item in root.iter('item'):
        title = html.unescape(item.findtext('title') or '')
        link = item.findtext('link') or ''
        dt = None
        try:
            dt = email.utils.parsedate_to_datetime(item.findtext('pubDate') or '')
        except Exception:
            pass
        img = ''
        for el in item.iter():
            if el.tag.split('}')[-1] == 'content' and el.get('url'):
                img = el.get('url')
                break
        src = urllib.parse.urlparse(link).netloc.replace('www.', '')
        out.append({'title': title, 'url': link, 'source': src, 'time': ago(dt),
                    'image': img, 'snippet': _clean_snippet(item.findtext('description') or ''),
                    'ts': dt.timestamp() if dt else 0, 'via': 'Yahoo'})
    return out[:30]

def src_bsky(q, days=7):
    """Bluesky public search — the free social pulse (X offers no free feed)."""
    url = 'https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?' + \
          urllib.parse.urlencode({'q': q, 'limit': '15', 'sort': 'latest'})
    out = []
    for p in json.loads(fetch(url)).get('posts', []):
        rec = p.get('record', {})
        handle = p.get('author', {}).get('handle', '')
        rkey = p.get('uri', '').rsplit('/', 1)[-1]
        dt = None
        try:
            dt = datetime.fromisoformat(rec.get('createdAt', '').replace('Z', '+00:00'))
        except Exception:
            pass
        img = ''
        try:
            img = p['embed']['images'][0]['thumb']
        except Exception:
            pass
        text = (rec.get('text') or '').strip().replace('\n', ' ')
        if len(text) > 140:
            text = text[:137] + '…'
        out.append({'title': text,
                    'url': 'https://bsky.app/profile/%s/post/%s' % (handle, rkey),
                    'source': '@' + handle, 'time': ago(dt), 'image': img,
                    'ts': dt.timestamp() if dt else 0, 'via': 'Bluesky'})
    return out

def src_bing(q, days=7):
    """Bing News RSS — direct publisher links AND feed-supplied thumbnails."""
    url = 'https://www.bing.com/news/search?' + urllib.parse.urlencode({'q': q, 'format': 'RSS'})
    out = []
    root = ET.fromstring(fetch(url))
    for item in root.iter('item'):
        title = item.findtext('title') or ''
        link = item.findtext('link') or ''
        if 'apiclick' in link:  # unwrap Bing's redirect to the real URL
            qs2 = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
            link = (qs2.get('url') or [link])[0]
        dt = None
        try:
            dt = email.utils.parsedate_to_datetime(item.findtext('pubDate') or '')
        except Exception:
            pass
        img, src = '', ''
        for el in item:
            tag = el.tag.split('}')[-1]
            if tag == 'Image':
                img = (el.text or '').strip()
            elif tag == 'Source':
                src = (el.text or '').strip()
        if not src:
            src = urllib.parse.urlparse(link).netloc.replace('www.', '')
        out.append({'title': title, 'url': link, 'source': src, 'time': ago(dt),
                    'image': hd_bing(img), 'snippet': _clean_snippet(item.findtext('description') or ''),
                    'ts': dt.timestamp() if dt else 0, 'via': 'Bing'})
    return out[:40]

def src_reddit(q, days=7):
    t = 'day' if days <= 1 else 'week' if days <= 7 else 'month' if days <= 31 else 'year' if days <= 365 else 'all'
    url = 'https://old.reddit.com/search.json?' + urllib.parse.urlencode(
        {'q': q, 'sort': 'relevance', 't': t, 'limit': '15'})
    out = []
    for ch in json.loads(fetch(url)).get('data', {}).get('children', []):
        d = ch.get('data', {})
        dt = datetime.fromtimestamp(d.get('created_utc', 0), tz=timezone.utc)
        img = ''
        try:
            img = html.unescape(d['preview']['images'][0]['source']['url'])
        except Exception:
            t = d.get('thumbnail', '')
            img = t if t.startswith('http') else ''
        out.append({'title': d.get('title', ''),
                    'url': 'https://www.reddit.com' + d.get('permalink', ''),
                    'source': 'r/' + d.get('subreddit', ''), 'time': ago(dt),
                    'image': img,
                    'ts': dt.timestamp(), 'via': 'Reddit'})
    return out

OG1 = re.compile(rb'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.I)
OG2 = re.compile(rb'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)

def og_image(url):
    """Lift the publisher's own preview image from the article's metadata.
    Reads only the page head, in memory, discarded after the response."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=5) as r:
            chunk = r.read(120000)
        m = OG1.search(chunk) or OG2.search(chunk)
        if m:
            img = html.unescape(m.group(1).decode('utf-8', 'ignore')).strip()
            if img.startswith('http'):
                return img
    except Exception:
        pass
    return ''

OGD1 = re.compile(rb'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', re.I)
OGD2 = re.compile(rb'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', re.I)

def og_desc(url):
    """Lift the publisher's own description/snippet from the article head —
    used to ground the brief summary in real reporting, not invention."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=5) as r:
            chunk = r.read(120000)
        m = OGD1.search(chunk) or OGD2.search(chunk)
        if m:
            return html.unescape(m.group(1).decode('utf-8', 'ignore')).strip()[:400]
    except Exception:
        pass
    return ''

GN_ID = re.compile(r'/rss/articles/([^?/]+)')

def resolve_gnews(url):
    """Crack a Google News redirect link to the real article URL."""
    m = GN_ID.search(url)
    if m:
        try:  # older link format embeds the URL in the base64 id
            raw = base64.urlsafe_b64decode(m.group(1) + '===')
            for c in re.findall(rb'https?://[^\x00-\x20"\\]+', raw):
                u = c.decode('utf-8', 'ignore')
                if 'news.google' not in u and len(u) > 12:
                    return u
        except Exception:
            pass
    try:  # newer format: fetch the interstitial page and find the outbound link
        page = fetch(url, timeout=4)
        m2 = (re.search(rb'data-n-au="([^"]+)"', page)
              or re.search(rb'href="(https?://(?!news\.google|accounts\.google|www\.google|policies\.google|support\.google|play\.google)[^"]+)"', page))
        if m2:
            return html.unescape(m2.group(1).decode('utf-8', 'ignore'))
    except Exception:
        pass
    return ''

def fix_gnews_urls(arts, limit=14):
    need = [a for a in arts if 'news.google.com' in a['url']][:limit]
    if not need:
        return
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(resolve_gnews, a['url']): a for a in need}
        for fu in futs:
            try:
                u = fu.result(timeout=5)
                if u:
                    futs[fu]['url'] = u
            except Exception:
                pass

def _tok(t):
    return {w for w in re.sub(r'[^a-z0-9 ]', ' ', t.lower()).split() if len(w) > 3}

def upgrade_gnews(arts, limit=8):
    """Google's new links hide the article URL entirely. Sidestep: re-find the
    same story on Bing by headline and inherit its direct link + thumbnail."""
    need = [a for a in arts if 'news.google.com' in a['url']][:limit]
    if not need:
        return
    def lookup(a):
        try:
            base = _tok(a['title'])
            if not base:
                return None
            for h in src_bing(a['title'][:90]):
                ht = _tok(h['title'])
                if ht and len(base & ht) / max(1, len(base | ht)) >= 0.4:
                    return h
        except Exception:
            pass
        return None
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(lookup, a): a for a in need}
        for fu in futs:
            try:
                h = fu.result(timeout=6)
            except Exception:
                h = None
            if h:
                a = futs[fu]
                a['url'] = h['url'] or a['url']
                if h['image']:
                    a['image'] = h['image']

def fill_images(arts, limit=16):
    # fill missing images, and upgrade Bing's compressed thumbs to the
    # publisher's own og:image when one exists (Bing kept as fallback)
    need = [a for a in arts if (not a['image'] or 'bing.com/th' in a['image'])
            and a['url'].startswith('http')
            and 'news.google.com' not in a['url']][:limit]
    if not need:
        return
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(og_image, a['url']): a for a in need}
        for fu in futs:
            try:
                img = fu.result(timeout=4)
                if img:
                    futs[fu]['image'] = img
            except Exception:
                pass

# Hosts that only ever serve images/thumbnails (a favicon or photo CDN), never an
# article page. Some feeds hand back one of these as the item's `url` (e.g. a
# Google "=w16" favicon thumbnail), which then opens the wrong thing and starves
# the summarizer's body fetch — so such items are dropped at the aggregation gate.
_IMG_HOST_RE = re.compile(
    r'(?:^|\.)(?:googleusercontent\.com|ggpht\.com|gstatic\.com|fbcdn\.net|'
    r'pbs\.twimg\.com|i\.ytimg\.com|ytimg\.com|i\.redd\.it|preview\.redd\.it|'
    r'imgur\.com|cdninstagram\.com)$', re.I)

def _is_article_url(u):
    """True only for links that plausibly point at a readable article page."""
    u = (u or '').strip()
    if not u.startswith('http'):
        return False
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return False
    host = (p.netloc or '').lower().split(':')[0]
    if _IMG_HOST_RE.search(host):
        return False
    if re.search(r'\.(?:jpe?g|png|gif|webp|svg|bmp|avif)(?:$|[?#])', (p.path or '').lower()):
        return False
    if re.search(r'=w\d+(?:-h\d+)?(?:-[a-z0-9]+)*$', u):   # Google sized-image suffix, e.g. =w16, =s96-c
        return False
    return True

# Editorial block — prediction/betting markets we will never surface. Promoting
# gambling violates QUWWAA's core principles, and these two also run undisclosed
# paid "articles" dressed up as news. Drop ANY story about them, everywhere the
# feed is built (board, brief, news lens, related coverage, priority tracking),
# whether the brand is in the headline, the outlet, the URL, or the blurb.
BLOCKED_TERMS = ('polymarket', 'kalshi')

# Outlets barred for repeated misinformation — never surfaced anywhere the feed is
# built. Matched against the article's SOURCE name and URL only (not its body), so we
# drop stories FROM the outlet, not stories that merely mention it. Add lowercase
# source-name and/or domain fragments here to block another outlet.
BLOCKED_SOURCES = ('cleveland jewish news', 'clevelandjewishnews.com')

def _is_blocked_article(a):
    """True if a story is about a blocked topic (gambling markets, matched in any
    field) or comes FROM a blocked outlet (matched on source name / URL)."""
    try:
        src = (str(a.get('source') or '') + ' ' + str(a.get('url') or '')).lower()
        if any(s in src for s in BLOCKED_SOURCES):
            return True
        hay = ' '.join(str(a.get(k) or '') for k in
                       ('title', 'source', 'url', 'snippet', 'summary', 'desc', 'description')).lower()
    except Exception:
        return False
    return any(t in hay for t in BLOCKED_TERMS)

def aggregate(q, days=7, fast=False, lang='en'):
    results, seen, arts = [], set(), []
    diag = {}
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    if lang != 'en':
        # Non-English: native-language sources only — locale-aware Google News +
        # GDELT sourcelang. (Bing/Yahoo/curated feeds are English-centric.)
        sources = ((src_gnews, 'GNEWS'), (src_gdelt, 'GDELT'))
    elif fast:
        sources = ((src_bing, 'BING'), (src_gdelt, 'GDELT'), (src_yahoo, 'YAHOO'))
    else:
        sources = ((src_rsspack, 'FEEDS'), (src_gdelt, 'GDELT'), (src_bing, 'BING'),
                   (src_yahoo, 'YAHOO'), (src_bsky, 'BLUESKY'),
                   (src_gnews, 'GNEWS'), (src_reddit, 'REDDIT'))
    # Return with whatever has arrived by the deadline rather than waiting on the
    # slowest source — a single stalled feed must never hold up the whole board.
    deadline = 4.0 if fast else 6.5
    ex = ThreadPoolExecutor(max_workers=len(sources))
    futs = ({ex.submit(f, q, days, lang): n for f, n in sources} if lang != 'en'
            else {ex.submit(f, q, days): n for f, n in sources})
    enough = 8 if fast else 36   # return as soon as we clearly have plenty; don't wait on stragglers
    try:
        for fu in as_completed(list(futs), timeout=deadline):
            name = futs[fu]
            try:
                r = fu.result()
                diag[name] = len(r)
                results += r
            except Exception as e:
                diag[name] = type(e).__name__  # visible failure, never silent
            if len(results) >= enough:
                break  # a strong source already answered — paint now, skip the slow ones
    except FuturesTimeout:
        pass  # deadline reached — proceed with what we have
    for name in futs.values():
        diag.setdefault(name, 'timeout')      # sources that didn't make the deadline
    ex.shutdown(wait=False)                    # let stragglers finish in the background
    for a in results:
        # Drop genuinely old dated items. In the fast (first-paint) pass also keep
        # undated search hits so a quick source like Bing isn't filtered down to
        # nothing; the full pass below stays strict for quality.
        if a['ts'] < cutoff and not (fast and a['ts'] == 0):
            continue
        if not _is_article_url(a.get('url', '')):     # drop image-host / thumbnail links
            continue
        if _is_blocked_article(a):                    # never surface blocked gambling markets
            continue
        key = re.sub(r'[\W_]+', '', (a['title'] or '').lower())[:60]   # Unicode-aware: keep Cyrillic/Arabic/accented letters, not just a-z0-9
        if not key or key in seen:
            continue
        seen.add(key)
        arts.append(a)
    arts.sort(key=lambda a: a['ts'], reverse=True)
    arts = arts[:60]
    if not fast:  # fast mode skips the slow enrichment passes
        fix_gnews_urls(arts)
        if lang == 'en':
            upgrade_gnews(arts)      # may REWRITE a['url'] from a Bing match — English-only, so skip for other langs
        fill_images(arts)
        arts = [a for a in arts if _is_article_url(a.get('url', '')) and not _is_blocked_article(a)]   # …so re-validate (and re-block) after enrichment
    return {'query': q, 'days': days, 'articles': arts, 'diag': diag,
            'sources': len({a['source'] for a in arts})}

CACHE = {}
CACHE_TTL = 600  # seconds; in memory only, gone on restart
CACHE_MAX = 400  # hard cap on entries so this map can't grow without bound (OOM guard)

def _prune_cache():
    """Bound CACHE memory. Drop TTL-expired entries first (they'd be re-fetched on
    the next read anyway), then, if still over the cap, evict the oldest by age.
    Purely a memory guard — user-visible results and freshness are unchanged."""
    if len(CACHE) <= CACHE_MAX:
        return
    now = time.time()
    for k in [k for k, v in list(CACHE.items()) if now - v[0] >= CACHE_TTL]:
        CACHE.pop(k, None)
    if len(CACHE) > CACHE_MAX:
        for k, _v in sorted(CACHE.items(), key=lambda kv: kv[1][0])[:len(CACHE) - CACHE_MAX]:
            CACHE.pop(k, None)

def cached_aggregate(q, days=7, fast=False, lang='en'):
    key = (q.lower().strip(), days, fast, lang)
    hit = CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    payload = aggregate(q, days, fast, lang)
    if payload.get('articles'):
        CACHE[key] = (time.time(), payload)
        _prune_cache()
    return payload

# Home-board categories (must mirror HOME_CATS in the console).
HOME_CATS = [
    {'label': 'POLITICS',     'q': 'politics',     'days': 1, 'maxAgeH': 3},
    {'label': 'TRUMP',        'q': 'Trump',        'days': 1, 'maxAgeH': 5},
    {'label': 'MIDDLE EAST',  'q': 'middle east',  'days': 2},
    {'label': 'ISRAEL & IRAN','q': 'Israel Iran',  'days': 2},
    {'label': 'SPORTS',       'q': 'sports',       'days': 2},
    {'label': 'MARKETS',      'q': 'stock market', 'days': 2},
]

# A ready-to-serve snapshot of the six home cards, rebuilt on a timer in the
# background. Served instantly from /home so visitors never wait for a search.
HOME_SNAPSHOT = {'t': 0, 'items': []}
HOME_REFRESH = int(os.environ.get('HOME_REFRESH', '300'))  # rebuild every 5 min

def _pick_card(cat, fast=True, exclude_urls=None, lang='en', require_image=False):
    payload = cached_aggregate(_cat_query(cat, lang), cat['days'], fast, lang)
    arts = payload.get('articles', [])
    mah = cat.get('maxAgeH')
    if mah:
        cut = time.time() - mah * 3600
        fresh = [a for a in arts if (a.get('ts') or 0) >= cut]
        if fresh:
            arts = fresh
    if exclude_urls:
        arts = [a for a in arts if _norm_brief_url(a.get('url', '')) not in exclude_urls]
    # Prefer a story with a thumbnail; for the board, require one (no naked cards).
    a = next((x for x in arts if x.get('image')), None if require_image else (arts[0] if arts else None))
    if not a:
        return None
    return {'label': cat['label'], 'q': cat['q'], 'days': cat['days'],
            'a': {'title': a.get('title', ''), 'url': a.get('url', ''),
                  'source': a.get('source', ''), 'time': a.get('time', ''),
                  'image': a.get('image', ''), 'ts': a.get('ts', 0)}}

def build_home_snapshot():
    """Refresh the home snapshot, keeping the last-good card for any category
    that comes back empty so the board is always full."""
    prev = {it['label']: it for it in HOME_SNAPSHOT['items']}
    items = []
    for cat in HOME_CATS:
        try:
            card = _pick_card(cat)
        except Exception:
            card = None
        if not card:
            card = prev.get(cat['label'])     # fall back to the previous good card
        if card:
            items.append(card)
    if items:
        HOME_SNAPSHOT['items'] = items
        HOME_SNAPSHOT['t'] = time.time()

# --- Morning Brief: one curated, summarized story per section --------------
BRIEF_CATS = [
    {'label': 'US Politics',        'q': 'US politics',             'days': 1, 'maxAgeH': 14},
    {'label': 'World',              'q': 'world news',              'days': 1, 'maxAgeH': 20},
    {'label': 'Middle East',        'q': 'middle east',             'days': 2},
    {'label': 'Sports',             'q': 'sports',                  'days': 2},
    {'label': 'Finance',            'q': 'economy finance',         'days': 2},
    {'label': 'Markets',            'q': 'stock market',            'days': 2},
    {'label': 'Tech',               'q': 'technology',              'days': 2},
    {'label': 'AI',                 'q': 'artificial intelligence', 'days': 3},
    # Culture & Entertainment intentionally removed from the board — its celebrity
    # sourcing pulled immodest imagery that conflicts with our values. Such stories
    # only ever surface now on an explicit user search (the lens), never curated.
    {'label': 'Science',            'q': 'science discovery',       'days': 3},
    {'label': 'Nature & Disasters', 'q': 'natural disaster',        'days': 3},
    {'label': 'Crime & Justice',    'q': 'crime police court',      'days': 2},
    {'label': 'Worth Knowing',      'q': 'breaking news',           'days': 1, 'maxAgeH': 20},
]
BRIEF = {'t': 0, 'sections': []}
# The board should feel full and scrollable (competitor parity) WITHOUT drifting old:
# beyond the curated one-AI-card-per-category lead, top up with recent, thumbnailed
# stories that carry a snippet teaser — pulled from the same aggregates (no extra AI).
BOARD_TARGET = int(os.environ.get('BOARD_TARGET', '24'))        # aim for ~this many board cards (min 20)
BOARD_MAX_AGE_H = int(os.environ.get('BOARD_MAX_AGE_H', '72'))  # only stories newer than this on the board

# --- Localization (Phase 1) --------------------------------------------------
# Native-sourced news + in-language summaries. English is the BASE path and is
# unchanged. Other languages source from locale-aware Google News + GDELT
# sourcelang (so headlines/articles are native, never machine-translated) and get
# summaries written in-language. Caches and the brief are keyed per language;
# non-English content is built lazily on first request. Unknown lang -> English.
LANGS = {
    'en': {'gnews': ('en-US', 'US', 'US:en'),  'gdelt': 'english',    'plang': 'English'},
    'es': {'gnews': ('es-419', 'MX', 'MX:es-419'), 'gdelt': 'spanish', 'plang': 'Spanish'},
    'fr': {'gnews': ('fr', 'FR', 'FR:fr'),     'gdelt': 'french',     'plang': 'French'},
    'ar': {'gnews': ('ar', 'EG', 'EG:ar'),     'gdelt': 'arabic',     'plang': 'Arabic'},
    'ru': {'gnews': ('ru', 'RU', 'RU:ru'),     'gdelt': 'russian',    'plang': 'Russian'},
    'ms': {'gnews': ('ms', 'MY', 'MY:ms'),     'gdelt': 'malay',      'plang': 'Malay'},
    'id': {'gnews': ('id', 'ID', 'ID:id'),     'gdelt': 'indonesian', 'plang': 'Indonesian'},
}
DEFAULT_LANG = 'en'

# Localized per-category brief query terms (don't search English keywords against
# native-language sources). Languages without a map fall back to the English query.
CAT_QUERIES = {
    'es': {
        'US Politics': 'política Estados Unidos', 'World': 'noticias internacionales',
        'Middle East': 'Oriente Medio', 'Sports': 'deportes',
        'Finance': 'economía finanzas', 'Markets': 'bolsa mercados',
        'Tech': 'tecnología', 'AI': 'inteligencia artificial',
        'Culture & Entertainment': 'entretenimiento famosos', 'Science': 'ciencia',
        'Nature & Disasters': 'desastres naturales', 'Crime & Justice': 'crimen justicia',
        'Worth Knowing': 'últimas noticias',
    },
    'fr': {
        'US Politics': 'politique américaine', 'World': 'actualité internationale',
        'Middle East': 'Moyen-Orient', 'Sports': 'sport',
        'Finance': 'économie finance', 'Markets': 'bourse marchés',
        'Tech': 'technologie', 'AI': 'intelligence artificielle',
        'Culture & Entertainment': 'divertissement célébrités', 'Science': 'science',
        'Nature & Disasters': 'catastrophe naturelle', 'Crime & Justice': 'crime justice',
        'Worth Knowing': 'dernières nouvelles',
    },
    'ru': {
        'US Politics': 'политика США', 'World': 'мировые новости',
        'Middle East': 'Ближний Восток', 'Sports': 'спорт',
        'Finance': 'экономика финансы', 'Markets': 'фондовый рынок',
        'Tech': 'технологии', 'AI': 'искусственный интеллект',
        'Culture & Entertainment': 'развлечения знаменитости', 'Science': 'наука',
        'Nature & Disasters': 'стихийное бедствие', 'Crime & Justice': 'преступность правосудие',
        'Worth Knowing': 'последние новости',
    },
    'ar': {
        'US Politics': 'السياسة الأمريكية', 'World': 'أخبار العالم',
        'Middle East': 'الشرق الأوسط', 'Sports': 'رياضة',
        'Finance': 'اقتصاد ومال', 'Markets': 'البورصة والأسواق',
        'Tech': 'تكنولوجيا', 'AI': 'الذكاء الاصطناعي',
        'Culture & Entertainment': 'ترفيه ومشاهير', 'Science': 'علوم',
        'Nature & Disasters': 'كوارث طبيعية', 'Crime & Justice': 'جريمة وعدالة',
        'Worth Knowing': 'آخر الأخبار',
    },
}

def norm_lang(s):
    """Normalize a requested language to a supported code, else English."""
    s = (s or '').strip().lower().replace('_', '-').split('-')[0]   # 'ar-EG' -> 'ar'
    return s if s in LANGS else DEFAULT_LANG

def _cat_query(cat, lang):
    """The brief search terms for a category in the target language."""
    if lang == 'en':
        return cat['q']
    return (CAT_QUERIES.get(lang) or {}).get(cat['label']) or cat['q']

# Per-language brief stores. English stays in BRIEF (the prewarmed base); other
# languages build lazily and live here.
BRIEF_I18N = {}
def _brief_store(lang):
    if lang == 'en':
        return BRIEF
    return BRIEF_I18N.setdefault(lang, {'t': 0, 'sections': []})

BRIEF_BUILDING = set()          # langs with a build in flight — avoid concurrent duplicate builds
def ensure_brief_lang(lang):
    """For a non-English language, kick off a background build if the store is
    empty/partial/stale (English is prewarmed separately). Returns immediately so
    the request never blocks on ~13 summaries; the client re-fetches to fill in."""
    if lang == 'en':
        return
    store = _brief_store(lang)
    stale = store['t'] == 0 or len(store['sections']) < len(BRIEF_CATS) or (time.time() - store['t'] > BRIEF_TTL)
    if not stale or lang in BRIEF_BUILDING:
        return
    BRIEF_BUILDING.add(lang)
    def _run():
        try: build_brief(full=(store['t'] == 0), lang=lang)
        except Exception: pass
        finally: BRIEF_BUILDING.discard(lang)
    threading.Thread(target=_run, daemon=True).start()
BRIEF_TTL = int(os.environ.get('BRIEF_TTL', '10800'))   # rebuild at most every 3 hours

def summarize_story(title, desc):
    """Two-sentence, neutral, strictly-grounded summary. No key -> publisher snippet."""
    snippet = (desc or '').strip()
    if not ANTHROPIC_API_KEY:
        return snippet or (title or '')
    sysmsg = ("You write a morning news brief as QUWWAA. Produce a concise, neutral 2-sentence summary of the "
              "item below. Use ONLY facts present in the given headline and snippet — never add, infer, or "
              "speculate beyond them, and never sensationalize. No opinion, no markdown, no preamble. If the "
              "snippet is empty, plainly paraphrase the headline.")
    user = 'Headline: ' + (title or '') + '\nSnippet: ' + (snippet or '(none)')
    try:
        body = json.dumps({'model': ASK_MODEL, 'max_tokens': 180, 'system': sysmsg,
                           'messages': [{'role': 'user', 'content': user}]}).encode()
        req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=body, headers={
            'content-type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01'})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        txt = ' '.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text').strip()
        return txt or snippet or (title or '')
    except Exception:
        return snippet or (title or '')

# --- Article detail view: grounded multi-paragraph summary + related links ---
ARTICLE_CACHE = {}                                            # url -> (t, payload)
ARTICLE_TTL = int(os.environ.get('ARTICLE_TTL', '21600'))    # cache summaries 6h (no re-spend)
_STOP = set(('the a an and or but of to in on for with from by at as is are was were be been being this that '
             'these those it its their his her our your my we you they he she over under after before into out '
             'up down new news report reports says said amid will would can could has have had').split())

def key_terms(title, n=6):
    """A few significant words from the headline to seed the related-coverage search."""
    out = []
    for w in re.findall(r"[A-Za-z0-9']+", title or ''):
        if w.lower() in _STOP or len(w) < 3:
            continue
        out.append(w)
        if len(out) >= n:
            break
    return ' '.join(out) or (title or '')

# Signals that a butler message is asking about a current event → pull live coverage
# into context so the model answers FROM the news, not its (cut-off) training memory.
_NEWS_SIGNAL = re.compile(
    r"\b(news|latest|update[sd]?|happening|happened|breaking|report(ed|s)?|stor(y|ies)|"
    r"today|tonight|yesterday|recent(ly)?|currently|this week|did .* (die|win|happen)|"
    r"die[sd]?|dead|death|killed?|attack|war|ceasefire|election|elected|vote[sd]?|"
    r"resign(ed)?|announce[sd]?|launch(ed)?|verdict|ruling|indict|crash|crisis|deal|"
    r"talks|summit|strike|protest|earthquake|hurricane|storm|outbreak|scandal|"
    r"tell me about|what.?s? (going on|new|happening)|any news|hear about|what happened|who won)\b",
    re.I)

def _looks_like_news(text):
    """Generous heuristic: is this a question about a current event/news?"""
    t = (text or '').strip()
    if len(t) < 4:
        return False
    if _NEWS_SIGNAL.search(t):
        return True
    # a multi-word Capitalized proper noun (typed input) is likely a person/place/org query
    return bool(re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", t))

def butler_live_news(query, max_n=6):
    """Retrieve current coverage for a butler question and format it as a labeled
    ground-truth block for the /ask system prompt. Reuses the News Lens retrieval.
    Returns (block_text, [source names]); ('', []) when nothing relevant came back."""
    terms = key_terms(query)
    if not terms:
        return '', []
    try:
        arts = cached_aggregate(terms, 7, True, 'en').get('articles', [])
    except Exception:
        arts = []
    qtok = _tok(query) - STOPWORDS
    rel = []
    for a in arts:
        if not a.get('title') or _is_blocked_article(a):
            continue
        if qtok and not (qtok & _tok(a['title'])):       # headline must touch the query
            continue
        rel.append(a)
    rel.sort(key=lambda a: (a.get('ts') or 0), reverse=True)
    rel = rel[:max_n]
    if not rel:
        return '', []
    lines, sources = [], []
    for i, a in enumerate(rel, 1):
        try:
            date = datetime.fromtimestamp(a['ts'], tz=timezone.utc).strftime('%Y-%m-%d') if a.get('ts') else ''
        except Exception:
            date = ''
        snip = re.sub(r'\s+', ' ', (a.get('snippet') or '')).strip()[:240]
        src = a.get('source') or 'source'
        lines.append('[%d] "%s" — %s%s.%s %s' % (
            i, a.get('title', ''), src, (', ' + date) if date else '',
            (' ' + snip) if snip else '', a.get('url', '')))
        if src and src not in sources:
            sources.append(src)
    block = "LIVE NEWS — retrieved just now, treat as current ground truth:\n" + "\n".join(lines)
    return block, sources[:4]

# Reading the live coverage before answering adds a multi-second pause. To fill it,
# the streaming butler speaks one of these warm, varied "working on it" lines FIRST
# (personalized for a signed-in member, generic otherwise) while the retrieval runs —
# so there's natural conversation instead of dead air. Varied so it never feels canned.
_FILLER_TEMPLATES = [
    "Absolutely%s — let me pull those up for you, and while I do, how's your day going?",
    "Right away%s. Give me a moment to read through the reporting — how are you doing today?",
    "Of course%s. I'm bringing the coverage up now; how's everything on your end?",
    "On it%s — fetching the latest. This'll take just a few seconds, so bear with me.",
    "Happy to%s. Let me get across the details first — how has your day been treating you?",
    "Certainly%s — pulling the stories up now. Won't be a moment.",
    "Good question%s. Let me gather what's being reported; how are you keeping today?",
    "Let me bring up what's out there%s. One moment while I read through it properly.",
    "Sure thing%s — the coverage is coming up on screen while I get across the details.",
    "Let's have a look%s. Give me a few seconds to read the reporting before I weigh in.",
    "Pulling that up now%s. How's your day shaping up so far?",
    "Right then%s — let me dig into the latest. Won't keep you a moment.",
    "Gladly%s. I'm reading through the coverage now; anything else on your mind today?",
    "One moment%s while I bring up the reporting and get the details straight.",
    "Looking into it now%s. How are things with you today?",
    "Let me get into it%s — bringing the stories up and reading through them now.",
]

def _butler_filler(member):
    """A short, warm, varied line to cover the news-retrieval pause. Uses the member's
    first name only for a signed-in member who's chosen name-style; generic otherwise
    (no 'sir'/'ma'am')."""
    name = ''
    try:
        if member and (member.get('address_style') or 'name') == 'name':
            name = (member.get('display_name') or '').split(' ')[0].strip()
    except Exception:
        name = ''
    return random.choice(_FILLER_TEMPLATES) % ((', ' + name) if name else '')

def summarize_article(title, desc, source):
    """A genuine, transformative 2-3 paragraph summary grounded strictly in the
    headline + publisher snippet. Returns (text, grounded). Never invents; never
    reproduces the article verbatim."""
    snippet = (desc or '').strip()
    grounded = bool(snippet)
    if not ANTHROPIC_API_KEY:
        return (snippet or (title or '')), grounded
    sysmsg = ("You are QUWWAA, a courteous news butler. Write a clear, neutral summary (2-3 short paragraphs) "
              "of the news item below, using ONLY facts present in the provided headline and snippet. Never "
              "invent, infer, or speculate beyond them; never reproduce the article verbatim or pad with filler. "
              "No markdown, no preamble, no opinion. If the snippet is sparse, summarize what is known and note "
              "that fuller detail is in the original report.")
    user = ('Source: ' + (source or 'the publisher') + '\nHeadline: ' + (title or '')
            + '\nSnippet: ' + (snippet or '(none)'))
    try:
        body = json.dumps({'model': ASK_MODEL, 'max_tokens': 440, 'system': sysmsg,
                           'messages': [{'role': 'user', 'content': user}]}).encode()
        req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=body, headers={
            'content-type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01'})
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.loads(r.read())
        txt = ' '.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text').strip()
        return (txt or snippet or (title or '')), grounded
    except Exception:
        return (snippet or (title or '')), grounded

def related_articles(title, source, exclude_url, n=4, lang='en'):
    """3-4 related stories on the same topic from OTHER outlets (reuses /news)."""
    try:
        payload = cached_aggregate(key_terms(title), 7, True, lang)
    except Exception:
        return []
    out, seen, src0 = [], set(), (source or '').lower()
    for a in payload.get('articles', []):
        u = a.get('url', '')
        if not u or u == exclude_url or u in seen:
            continue
        if src0 and a.get('source', '').lower() == src0:     # prefer different outlets
            continue
        seen.add(u)
        out.append({'title': a.get('title', ''), 'url': u, 'source': a.get('source', ''),
                    'time': a.get('time', ''), 'image': a.get('image', '')})
        if len(out) >= n:
            break
    return out

# --- Story permalink persistence (Phase 1) ---------------------------------
# Every opened story gets a stable, shareable home at /story/<id>. The id is
# DETERMINISTIC from the canonical source URL, so the same story shown on the
# board and inside a lens resolves to one row (no read-before-write dedupe).
STORY_INDEX_POLICY = os.environ.get('STORY_INDEX_POLICY', 'all').strip().lower()   # 'all' | 'engaged'
STORY_ENGAGED_MIN = int(os.environ.get('STORY_ENGAGED_MIN', '3'))                  # 'engaged': index once score >= this
_TRACK_PARAM = re.compile(r'^(utm_|fbclid$|gclid$|mc_|mc_eid$|ref$|ref_|igshid$|si$|spm$|cmpid$|ncid$|_hsenc|_hsmi)', re.I)
_B62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'

def _b62(n):
    s = ''
    while n:
        n, r = divmod(n, 62); s = _B62[r] + s
    return s or '0'

def _canon_story_url(url):
    """Working outbound URL, canonicalized: lowercase host, drop fragment + tracking params."""
    try:
        p = urllib.parse.urlsplit((url or '').strip())
        if not p.scheme.lower().startswith('http'):
            return (url or '').strip()
        q = [(k, v) for (k, v) in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not _TRACK_PARAM.match(k)]
        path = p.path.rstrip('/') or '/'
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urllib.parse.urlencode(q), ''))
    except Exception:
        return (url or '').strip()

def story_id_for(url):
    """Short opaque id derived from the canonical URL (same story → same id)."""
    canon = _canon_story_url(url)
    return _b62(int.from_bytes(hashlib.sha1(canon.encode('utf-8')).digest()[:8], 'big'))[:11]

def _slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')[:60].rstrip('-')

def persist_story(url, title, source, summary, image='', lang='en', lens=''):
    """Upsert one canonical news_stories row when a story's summary is generated.
    Returns the short story id, or None if the url isn't a real article. Best-effort:
    the id is deterministic, so it's returned even if the DB write fails. Does NOT
    touch is_indexed (the nightly job owns that) so it can't clobber the SEO flag."""
    canon = _canon_story_url(url)
    if not canon.lower().startswith('http'):
        return None
    sid = story_id_for(url)
    row = {'id': sid, 'slug': _slugify(title), 'source_url': canon, 'source_name': source or '',
           'headline': title or '', 'summary': summary or '', 'image_url': image or '',
           'lang': lang or 'en', 'updated_at': datetime.now(timezone.utc).isoformat()}
    if lens:
        row['lenses'] = [lens]
    try:
        sb_rest('POST', 'news_stories?on_conflict=id', row,
                prefer='resolution=merge-duplicates,return=minimal')
    except Exception:
        pass
    return sid

def build_article(url, title, source, lang='en', image=''):
    """Grounded summary + related coverage for one article, cached by URL+lang so
    re-opening the same story never re-spends AI credits and each language keeps
    its own in-language summary."""
    ckey = (url, lang)
    hit = ARTICLE_CACHE.get(ckey)
    if hit and time.time() - hit[0] < ARTICLE_TTL:
        return hit[1]
    desc = og_desc(url) if url.startswith('http') else ''
    body = fetch_article_text(url, timeout=7) if url and url.startswith('http') else ''
    # tier-2 neighbours (with bodies) when the article body is thin/unfetchable
    neighbors = _neighbors_with_bodies(title, source, url, budget=9, lang=lang) if (not body or len(body) < 400) else []
    summary, degraded = make_summary(title, source, '', url, desc, longer=True, body=body, neighbors=neighbors, lang=lang)
    bias = ''
    if summary and '[[QBIAS]]' in summary:                       # split the bias analysis out of the neutral summary
        summary, bias = summary.split('[[QBIAS]]', 1)
        summary, bias = summary.strip(), bias.strip()
    payload = {'url': url, 'title': title, 'source': source, 'summary': summary, 'bias': bias,
               'grounded': (not degraded), 'related': related_articles(title, source, url, lang=lang)}
    # Give the opened story a permanent home (/story/<id>) + an id for the share button.
    if summary and url and url.startswith('http'):
        try: payload['story_id'] = persist_story(url, title, source, summary, image=image, lang=lang)
        except Exception: pass
    if summary and url:
        if len(ARTICLE_CACHE) > 500:                          # cap memory
            for k in list(ARTICLE_CACHE)[:120]:
                ARTICLE_CACHE.pop(k, None)
        # good summaries cache for the full TTL; degraded (tier-3) ones expire in
        # ~30 min so the body / related coverage gets another try, without
        # re-spending on every open.
        offset = (ARTICLE_TTL - 1800) if degraded else 0
        ARTICLE_CACHE[ckey] = (time.time() - offset, payload)
    return payload

def _prune_ip_bucket(d, now):
    """Drop IPs with no hits in the last 60s so these rate-limit maps don't grow
    one entry per unique visitor forever. Threshold-gated, so it's a no-op on the
    common path; per-visitor throttling behavior is unchanged. Call under _ask_lock."""
    if len(d) <= 4096:
        return
    cutoff = now - 60
    for ip in [ip for ip, ts in list(d.items()) if not ts or ts[-1] <= cutoff]:
        d.pop(ip, None)

_article_hits = {}                  # ip -> recent /article timestamps (own bucket)
def article_rate_check(ip):
    now = time.time()
    with _ask_lock:
        _prune_ip_bucket(_article_hits, now)
        hits = [t for t in _article_hits.get(ip, []) if t > now - 60]
        if len(hits) >= ARTICLE_RATE_PER_MIN:
            _article_hits[ip] = hits
            return False
        hits.append(now)
        _article_hits[ip] = hits
        return True

# --- News-lens summary quality: tiered, wire-lede, auto-rejected slop ----------
# Phrases that reveal the AI, an apology, a fetch failure, or a headline restatement.
BANNED_SUMMARY = [
    "couldn't read", "could not read", "couldn't access", "could not access", "unable to access",
    "unable to read", "not enough information", "insufficient information", "limited information",
    "no details", "no specific", "the article doesn't", "the article does not", "article didn't",
    "article did not", "doesn't say", "didn't say", "based on the headline", "from the headline",
    "paraphrase of headline", "paraphrasing the headline", "as an ai", "i cannot", "i can't",
    "i couldn't", "i could not", "i'm unable", "i am unable", "cannot summarize", "unable to summarize",
    "without more information", "without additional", "does not provide", "not specified",
    "appears to", "seems to discuss", "seems to be about", "this article is about",
    "the headline suggests", "unclear from", "not clear from", "reportedly the article",
    # Non-English refusal/apology slop — same anti-slop bar applies to in-language summaries.
    # es
    "no puedo", "no se puede", "sin información", "sin más información", "información insuficiente",
    "no hay suficiente información", "no se proporciona", "no especificado", "como ia", "como una ia",
    # fr
    "je ne peux pas", "impossible de", "pas assez d'informations", "informations insuffisantes",
    "en tant qu'ia", "sans plus d'informations", "l'article ne fournit",
    # ru
    "я не могу", "недостаточно информации", "невозможно", "как ии",
    # ar
    "لا أستطيع", "لا يمكنني", "لا توجد معلومات كافية", "معلومات غير كافية",
    # ms / id
    "tidak dapat", "tidak cukup maklumat", "tidak cukup informasi", "sebagai ai", "maaf, saya",
]
_SUMSTOP = set(("the a an and or but of to in on for with from by at as is are was were be been being this that "
    "these those it its their his her our your my we you they he she over under after before into out up down new "
    "news report reports says said amid will would can could has have had not no so if then than about more most "
    "has been have been will be").split())

def _sig_tokens(s):
    # Unicode-aware: keep Cyrillic/Arabic/accented letters too, not just a-z0-9 —
    # otherwise non-Latin summaries tokenize to nothing and every quality check
    # (headline-restatement, specificity) misfires and rejects them.
    return set(w for w in re.findall(r"[^\W_]+", (s or '').lower()) if w not in _SUMSTOP and len(w) > 2)

def _banned_summary(s):
    low = (s or '').lower()
    return any(p in low for p in BANNED_SUMMARY)

def _too_like_headline(summary, title):
    st = _sig_tokens(summary); ht = _sig_tokens(title)
    if not st:
        return True
    if len(st - ht) < 4:                                   # adds <4 new significant words → restatement
        return True
    return (len(st & ht) / float(len(st | ht) or 1)) > 0.62

_COMMON_CAPS = set("the this that these those their they there then when what where while with after before "
    "however meanwhile it he she but and a an his her".split())
def _has_specifics(summary, title):
    if re.search(r'\d', summary or ''):                    # a number is a concrete fact
        return True
    tl = (title or '').lower()
    for w in re.findall(r'\b([A-Z][a-zA-Z]{2,})', summary or ''):   # a proper noun not in the headline
        if w.lower() not in tl and w.lower() not in _COMMON_CAPS:
            return True
    # substantial elaboration beyond the headline is itself informative (a good
    # lede that adds context without a brand-new proper noun shouldn't be rejected)
    if len(_sig_tokens(summary) - _sig_tokens(title)) >= 8:
        return True
    return False

def _ok_summary(s, title, require_specifics=True):
    s = (s or '').strip()
    if not s or len(s) < 25 or _banned_summary(s) or _too_like_headline(s, title):
        return False
    # Body-grounded summaries (tiers 1-2) are factual by construction, so we only
    # demand the specificity floor on the world-knowledge tier (hallucination guard).
    return _has_specifics(s, title) if require_specifics else True

def _card_summary_ok(p):
    """A stored brief card whose summary is genuinely usable (not empty, not slop,
    not a headline restatement). Cheap, no AI — used to flush stale cards."""
    s = (p.get('summary') or '').strip(); t = p.get('title') or ''
    return bool(s) and not _banned_summary(s) and not _too_like_headline(s, t)

def _card_settled(p):
    """Stop re-summarizing a card once it's good, or we've already tried enough
    (a genuinely hard-to-fetch story settles to a clean headline-only card)."""
    return _card_summary_ok(p) or (p.get('degrade_tries') or 0) >= 3

def fetch_article_text(url, timeout=7):
    """Best-effort readable body text from an article page (summary tier 1)."""
    if not (url and url.startswith('http')):
        return ''
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
            raw = r.read(400000)
    except Exception:
        return ''
    try:
        doc = raw.decode('utf-8', 'ignore')
    except Exception:
        return ''
    doc = re.sub(r'(?is)<(script|style|noscript|head|nav|footer|aside)[^>]*>.*?</\1>', ' ', doc)
    texts = []
    for p in re.findall(r'(?is)<p[^>]*>(.*?)</p>', doc):
        t = html.unescape(re.sub(r'\s+', ' ', re.sub(r'(?is)<[^>]+>', ' ', p))).strip()
        if len(t) >= 60:                                   # skip nav/boilerplate fragments
            texts.append(t)
    return ' '.join(texts)[:3500]

LEDE_SYS = ("You are QUWWAA, a wire-service editor. Write a tight, factual news lede — 2-3 sentences, about "
    "30-55 words — in the neutral, specific style of AP or Reuters: lead with who, what, where, when, and why "
    "it matters. Name the people, places, organizations and numbers; state plainly what happened. NEVER hedge, "
    "apologize, mention sources/fetching/the AI, or restate the headline; NEVER say you lack information. Use only "
    "facts you are confident are true (from the provided material or well-established public knowledge) and do not "
    "invent specifics. Output only the summary, nothing else.")

def _lede(user_content, strict=False, longer=False, lang='en'):
    sysmsg = LEDE_SYS \
        + (" Write 2-3 short paragraphs (about 60-110 words)." if longer else "") \
        + ((" If the article is clearly biased against Islam, Muslims, Muslim-majority countries, or other "
            "non-Western governments — e.g. blaming the religion of Islam or Muslims as a whole for the actions "
            "of a particular government, regime, group, or individual; loaded or sensational language; collective "
            "blame; stripped-out context; or a double standard not applied to comparable non-Muslim actors — "
            "FIRST write the neutral factual summary, THEN on its own line output exactly the marker [[QBIAS]] "
            "and nothing else on that line, THEN a short analysis (1-2 short paragraphs) that names the bias and "
            "the specific tell, gives the fair context in Islam's defense, and points out the Western free-speech "
            "double standard with a well-established example (European protest or slogan bans, US anti-BDS laws, "
            "arrests at pro-Palestinian or ICE protests, university crackdowns, deportation over a Free Palestine "
            "placard). Separate a government or regime from the faith and the Muslims who practice it; use only "
            "well-established examples and never invent cases, names, or numbers; never deny a verified fact — "
            "challenge the framing, not the events. If the article is neutral or fair, write ONLY the summary "
            "with no marker and no analysis.") if longer else "") \
        + (" A generic restatement is unacceptable — include concrete, verifiable specifics "
           "(names, places, numbers, what happened)." if strict else "")
    if lang != 'en':
        pl = LANGS.get(lang, {}).get('plang', 'the source language')
        sysmsg += (" Write the summary in %s. The article is in %s; keep it natural and native, "
                   "not translated-sounding. Leave proper names and source names as given." % (pl, pl))
    body = json.dumps({'model': ASK_MODEL, 'max_tokens': (380 if longer else 170), 'system': sysmsg,
                       'messages': [{'role': 'user', 'content': user_content}]}).encode()
    req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=body, headers={
        'content-type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01'})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return ' '.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text').strip()

def _neighbors_with_bodies(title, source, exclude_url, budget=9, lang='en'):
    """Related coverage (tier 2): fetch 2-4 neighbours' bodies in parallel under one budget."""
    try:
        rel = related_articles(title, source, exclude_url, n=4, lang=lang)
    except Exception:
        rel = []
    if not rel:
        return []
    def grab(a):
        b = fetch_article_text(a.get('url', ''), timeout=6) or og_desc(a.get('url', '')) or ''
        a = dict(a); a['body'] = b; return a
    out, deadline = [], time.time() + budget
    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(grab, a) for a in rel]
            for fu in as_completed(list(futs), timeout=max(0.5, budget)):
                try: out.append(fu.result(timeout=max(0.1, deadline - time.time())))
                except Exception: pass
    except Exception:
        pass
    return [a for a in out if (a.get('body') or '').strip()]

def make_summary(title, source='', section='', url='', desc='', longer=False, body=None, neighbors=None, lang='en'):
    """Tiered, quality-checked summary used by BOTH the brief cards (longer=False)
    and the in-app article view (longer=True). Returns (summary, degraded). Never
    emits meta/apology/headline-restatement slop — a failing tier is rejected and
    the next tier runs; the safety net is the publisher snippet, never an apology."""
    title = (title or '').strip(); desc = (desc or '').strip()
    if not ANTHROPIC_API_KEY:
        # No model available (local/dev): use the publisher snippet if it's real,
        # never the bare headline. Mark degraded so prod re-summarizes properly.
        return ((desc if (desc and len(desc) >= 25 and not _too_like_headline(desc, title)) else ''), True)
    attempts = []
    # Tier 1 — the source article body (grounded: skip the specificity floor)
    if body is None:
        body = fetch_article_text(url, timeout=7) if url else ''
    if body and len(body) > 400:
        try:
            s = _lede('Section: %s\nSource: %s\nHeadline: %s\n\nArticle:\n%s\n\nWrite the summary.'
                      % (section, source, title, body), longer=longer, lang=lang)
            if _ok_summary(s, title, require_specifics=False): return s, False
            attempts.append(s)
        except Exception: pass
    # Tier 2 — synthesize across related coverage (read the neighbours; also grounded)
    if neighbors is None:
        try: neighbors = _neighbors_with_bodies(title, source, url, budget=9) if (not body or len(body) < 400) else []
        except Exception: neighbors = []
    if neighbors:
        ctx = ('Section: %s\nHeadline: %s\nSource: %s\n\nCoverage from multiple outlets:\n%s\n\n'
               'Synthesize ONE factual summary across these reports.'
               % (section, title, source, '\n'.join('— %s (%s): %s'
                  % (n.get('title', ''), n.get('source', ''), (n.get('body') or '')[:600]) for n in neighbors)))
        for strict in (False, True):
            try:
                s = _lede(ctx, strict=strict, longer=longer, lang=lang)
                if _ok_summary(s, title, require_specifics=False): return s, False
                attempts.append(s)
            except Exception: pass
    # Tier 3 — confident summary from headline + context + world knowledge (degraded)
    try:
        s = _lede('Section: %s\nSource: %s\nHeadline: %s\nSnippet: %s\n\nWrite a confident, factual summary of '
                  'this event using the headline, snippet and your knowledge of it. Name the who/what/where. Do '
                  'not apologize or say you lack information.'
                  % (section, source, title, desc or '(none)'), strict=True, longer=longer, lang=lang)
        # The specificity floor is an English-biased hallucination guard (it looks for
        # Latin proper nouns / digits), so it rejects almost all non-Latin summaries.
        # For non-English, trust the banned-phrase + headline-restatement gates instead.
        if _ok_summary(s, title, require_specifics=(lang == 'en')): return s, True
        attempts.append(s)
    except Exception: pass
    # Safety net — best attempt that is at least a real, non-restatement sentence;
    # then the publisher snippet. NEVER echo the headline (that's pure duplication
    # on the card) — return '' so the card shows a clean headline-only, retried later.
    for s in attempts:
        if s and not _banned_summary(s) and not _too_like_headline(s, title): return s, True
    if desc and len(desc) >= 25 and not _banned_summary(desc) and not _too_like_headline(desc, title):
        return desc, True
    return '', True

def quality_summary(title, source='', section='', url='', desc='', lang='en'):
    """Short wire-lede for the brief cards — background prewarm path."""
    return make_summary(title, source, section, url, desc, longer=False, lang=lang)

def build_brief(full=False, lang='en'):
    """Assemble one summarized story per section. On a 'full' rebuild every
    section is re-summarized fresh; otherwise sections already present are kept
    and only the MISSING ones are fetched/summarized — so filling out a partial
    brief costs an AI call only for the sections that are still empty. `lang`
    sources stories natively in that language and summarizes in-language; the
    result is published to that language's brief store (English -> BRIEF)."""
    store = _brief_store(lang)
    prev = {s['label']: s for s in store['sections']}   # last-good cards, kept as a fallback

    def build_section(cat):
        """Resolve ONE section: keep a settled previous card, re-summarize a degraded
        one, or pick + summarize a fresh story. Returns the section dict (or the prev
        card / None). Pure per-category work so sections can build in parallel."""
        label = cat['label']
        p = prev.get(label)
        if p and not _is_article_url(p.get('url', '')):
            p = None              # never carry forward an image-host/thumbnail link → re-pick fresh
        if p and not p.get('image'):
            p = None              # board cards must have a thumbnail → re-pick to find one
        # Re-validate each previously-built card. Keep it only if its summary is
        # genuinely good or we've already retried it enough; otherwise re-summarize.
        if not full and p and _card_settled(p):
            return p
        if not full and p:
            tries = (p.get('degrade_tries') or 0) + 1
            np = dict(p)
            try:
                desc = og_desc(p.get('url', '')) if str(p.get('url', '')).startswith('http') else ''
                summ, deg = quality_summary(p.get('title', ''), p.get('source', ''), label, p.get('url', ''), desc, lang=lang)
                np['summary'] = summ
                np['degraded'] = bool(deg and tries < 3)    # stop retrying after 3 cycles
            except Exception: pass
            np['degrade_tries'] = tries
            return np
        try:
            # Use the FULL aggregate (more sources + image backfill) so each section
            # reliably yields a thumbnailed story — the fast path comes back empty too often.
            card = _pick_card(cat, fast=False, lang=lang, require_image=True)
        except Exception:
            card = None
        if card:
            a = card['a']
            url = a.get('url', '')
            desc = og_desc(url) if url.startswith('http') else ''
            summ, deg = quality_summary(a.get('title', ''), a.get('source', ''), label, url, desc, lang=lang)
            return {'label': label, 'title': a.get('title', ''), 'url': url,
                    'source': a.get('source', ''), 'image': a.get('image', ''),
                    'time': a.get('time', ''), 'summary': summ, 'degraded': deg}
        return p                                        # keep the previous good story rather than drop the section

    # Build every section in parallel — a cold brief (esp. a non-English one that
    # sources + summarizes 13 categories) used to take well over a minute when run
    # one category at a time; concurrently it lands in roughly the time of the
    # slowest single section. Publish in canonical order as each completes so
    # /brief still fills in live.
    results, order = {}, [c['label'] for c in BRIEF_CATS]
    with ThreadPoolExecutor(max_workers=min(8, len(BRIEF_CATS))) as ex:
        futs = {ex.submit(build_section, cat): cat['label'] for cat in BRIEF_CATS}
        for fu in as_completed(futs):
            label = futs[fu]
            try: sec = fu.result()
            except Exception: sec = prev.get(label)
            if sec:
                results[label] = sec
                store['sections'] = [results[l] for l in order if l in results]
    # Top up the board to a fuller, scrollable set (≥20, all thumbnailed, all recent)
    # without extra AI: reuse the per-category aggregates just fetched and pull more
    # image-bearing, snippet-carrying, recent stories the curated leads didn't use.
    try:
        chosen = {_norm_brief_url(s.get('url', '')) for s in store['sections']}
        want = max(0, BOARD_TARGET - sum(1 for s in store['sections'] if s.get('image')))
        store['extra'] = build_board_extra(lang, chosen, want)
    except Exception:
        store['extra'] = store.get('extra') or []
    store['t'] = time.time()               # always stamp so we don't loop full rebuilds

def build_board_extra(lang, chosen_urls, want):
    """Recent, thumbnailed stories with a snippet teaser to flesh out the board past
    the curated AI leads — so it scrolls long like a real wire, no AI cost. Two sources:
      • Bing News per category → topical variety (best-effort; Bing throttles, so a
        failed call is never fatal),
      • the curated publisher feeds fetched ONCE → reliable volume,
    each card requiring BOTH an image and a snippet and being newer than BOARD_MAX_AGE_H.
    Categorized, interleaved across topics (variety first), newest-first, de-duped
    against the curated leads. English-only for now (the board is English-only)."""
    if want <= 0 or lang != 'en':
        return []
    cut = time.time() - BOARD_MAX_AGE_H * 3600
    label_tok = [(c['label'], (_tok(c['q']) - STOPWORDS)) for c in BRIEF_CATS]
    def guess_label(title):
        t, best, bn = _tok(title), 'World', 0
        for lab, toks in label_tok:
            n = len(t & toks)
            if n > bn: bn, best = n, lab
        return best
    def ok(a):
        return bool(a.get('image') and (a.get('snippet') or '').strip()
                    and (a.get('ts') or 0) >= cut
                    and _is_article_url(a.get('url', '')) and not _is_blocked_article(a))
    by_cat = {}
    # 1) Per-category variety from Bing (best-effort).
    def grab_bing(cat):
        try: return cat['label'], [a for a in src_bing(cat['q']) if ok(a)]
        except Exception: return cat['label'], []
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for lab, picks in ex.map(grab_bing, BRIEF_CATS):
                by_cat.setdefault(lab, []).extend(picks)
    except Exception:
        pass
    # 2) Reliable volume from the curated publisher feeds (one sweep), labeled by topic.
    feed_items = []
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_feed_items, n, u) for n, u in FEEDS]
            for fu in futs:
                try: feed_items += fu.result(timeout=12)
                except Exception: pass
    except Exception:
        pass
    for a in feed_items:
        if ok(a):
            by_cat.setdefault(guess_label(a.get('title', '')), []).append(a)
    # 3) Interleave across topics for variety, newest-first within each, global de-dup.
    for lst in by_cat.values():
        lst.sort(key=lambda a: (a.get('ts') or 0), reverse=True)
    order = [c['label'] for c in BRIEF_CATS] + [l for l in by_cat if l not in {c['label'] for c in BRIEF_CATS}]
    seen = set(chosen_urls or ())
    out, i = [], 0
    maxlen = max((len(v) for v in by_cat.values()), default=0)
    while len(out) < want and i < maxlen:
        for lab in order:
            lst = by_cat.get(lab) or []
            if i < len(lst):
                a = lst[i]; u = _norm_brief_url(a.get('url', ''))
                if u and u not in seen:
                    seen.add(u)
                    out.append({'label': lab, 'title': a.get('title', ''), 'url': a.get('url', ''),
                                'source': a.get('source', ''), 'image': a.get('image', ''),
                                'time': a.get('time', ''), 'summary': (a.get('snippet') or '').strip(),
                                'extra': True})
                    if len(out) >= want:
                        break
        i += 1
    return out

# --- Daily Morning Brief email (Kit broadcast, teaser format) ------------------
# Per-slot day guards + a per-day "already emailed" url set so the evening brief
# never repeats a story the morning brief already sent.
BRIEF_EMAIL = {'morning_day': None, 'evening_day': None, 'sent_day': None, 'sent_urls': set()}

def _norm_brief_url(u):
    """Normalize an article url for de-dup: drop scheme/query/fragment + trailing
    slash, lowercase host so the same story via a slightly different url still
    counts as a duplicate."""
    try:
        p = urllib.parse.urlsplit((u or '').strip())
        return (p.netloc.lower() + p.path.rstrip('/')).lower()
    except Exception:
        return (u or '').strip().lower()

def _brief_article_link(s):
    """Deep-link into the on-site article view (counts toward the free meter)."""
    q = urllib.parse.urlencode({'a': s.get('url', ''), 't': s.get('title', ''),
                                's': s.get('source', ''), 'lbl': s.get('label', '')})
    return SITE_URL + '/?' + q

def _dr_cobrand_active():
    """True while we still lead each brief with the Daily Rumble framing. Kit paused
    the account after a brand-new sender bulk-mailed an imported list under a name the
    recipients didn't recognize ("QUWWAA"); the list knows "Daily Rumble". For roughly
    the first month back we co-brand to rebuild recognition, then fade automatically
    once today (Phoenix) reaches BRIEF_DR_COBRAND_UNTIL — one env value, no code change."""
    if not BRIEF_DR_COBRAND_UNTIL:
        return False
    try:
        today = datetime.now(timezone.utc).astimezone(PHOENIX_TZ).strftime('%Y-%m-%d')
        return today < BRIEF_DR_COBRAND_UNTIL[:10]
    except Exception:
        return False

def compose_brief_email(slot='morning', exclude_urls=None):
    """Teaser email: butler intro + each section's headline + thumbnail, both
    clickable to quwwaa.com. No summaries in the email. Copy branches on `slot`
    ('morning'|'evening'). For the evening slot, `exclude_urls` (normalized) drops
    any story already emailed that morning — a section whose only fresh story is a
    morning duplicate is replaced with the next fresh story, or dropped entirely.
    Returns (subject, html, urls) where `urls` are the stories actually included."""
    exclude_urls = set(exclude_urls or ())
    cats_by_label = {c['label']: c for c in BRIEF_CATS}
    used = set()                       # urls already placed in THIS email (cross-section de-dup)
    secs = []
    for s in (BRIEF.get('sections') or []):
        nu = _norm_brief_url(s.get('url', ''))
        if nu and nu not in exclude_urls and nu not in used:
            secs.append(s); used.add(nu); continue
        # Duplicate (or already used here) — try the next fresh story for this category.
        cat = cats_by_label.get(s.get('label'))
        if not cat:
            continue
        try:
            alt = _pick_card(cat, fast=False, exclude_urls=(exclude_urls | used))
        except Exception:
            alt = None
        if alt:
            a = alt['a']; au = _norm_brief_url(a.get('url', ''))
            if au and au not in exclude_urls and au not in used:
                secs.append({'label': alt['label'], 'title': a.get('title', ''),
                             'url': a.get('url', ''), 'source': a.get('source', ''),
                             'image': a.get('image', ''), 'time': a.get('time', '')})
                used.add(au)
        # else: no fresh non-duplicate story for this category → drop the section.
    now_phx = datetime.now(timezone.utc).astimezone(PHOENIX_TZ)
    try: date_str = now_phx.strftime('%A, %B %-d')
    except Exception: date_str = now_phx.strftime('%A, %B %d')
    top = secs[0].get('title') if secs else ''
    cobrand = _dr_cobrand_active()
    context_line = ''
    if slot == 'evening':
        subject = ('QUWWAA Evening Brief — ' + top) if top else ('Your QUWWAA Evening Brief · ' + date_str)
        intro = "Good evening. Here's where the day landed — the latest from QUWWAA."
        header_label = 'Your Evening Brief · ' + date_str
        footer_line = "You're receiving the QUWWAA Brief."
    else:
        subject = ('QUWWAA Brief — ' + top) if top else ('Your QUWWAA Morning Brief · ' + date_str)
        intro = "Good morning. Here's the world this morning — tap any headline to read it on QUWWAA."
        header_label = 'Your Morning Brief · ' + date_str
        footer_line = "You're receiving the QUWWAA Morning Brief."
    if cobrand:
        # Lead with the brand the imported list already knows. Subject avoids spammy
        # punctuation/ALL-CAPS/emoji; the body opens with a Daily Rumble hook plus a
        # one-line "you subscribed to Daily Rumble" reconnection to their opt-in.
        slot_word = 'evening' if slot == 'evening' else 'morning'
        subject = ('Daily Rumble — ' + top) if top else ('Daily Rumble — your ' + slot_word + ' brief · ' + date_str)
        intro = "Hey — it's Daily Rumble. I've got another list of hot stories for you from QUWWAA."
        context_line = "You're receiving this because you subscribed to Daily Rumble. This is our new daily news brief, powered by QUWWAA."
        footer_line = "You're receiving this because you subscribed to Daily Rumble — now powered by QUWWAA."
    rows = []
    for s in secs:
        link = _brief_article_link(s); img = s.get('image', '')
        thumb = ('<a href="%s"><img src="%s" width="150" height="100" alt="" '
                 'style="display:block;width:150px;height:100px;object-fit:cover;border-radius:8px;border:0;"></a>'
                 % (link, html.escape(img))) if img else ''
        rows.append(
            '<tr><td style="padding:14px 0;border-bottom:1px solid #2a2018;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            + ('<td valign="top" width="150" style="padding-right:14px;">' + thumb + '</td>' if thumb else '')
            + '<td valign="top">'
            '<div style="font:600 11px Arial,sans-serif;color:#e08a32;margin-bottom:5px;">' + html.escape(s.get('label', '')) + '</div>'
            '<a href="' + link + '" style="font:500 19px Georgia,\'Times New Roman\',serif;color:#fff3e9;text-decoration:none;line-height:1.25;">' + html.escape(s.get('title', '')) + '</a>'
            '<div style="font:11px Arial,sans-serif;color:#a06a3a;margin-top:6px;">' + html.escape(s.get('source', '')) + '</div>'
            '</td></tr></table></td></tr>')
    # One sponsor block after the first story (separately sellable email real estate).
    # Click + open are tracked through the server (redirect + pixel) per send.
    try:
        sp = pick_email_sponsor()
    except Exception:
        sp = None
    if sp and rows:
        sid = sp.get('id')
        click = SITE_URL + '/sponsor-click?' + urllib.parse.urlencode({'id': sid, 'surface': 'email'})
        pixel = SITE_URL + '/sponsor-pixel?' + urllib.parse.urlencode({'id': sid, 'surface': 'email'})
        mark = (('<img src="' + html.escape(sp.get('logo_url')) + '" alt="' + html.escape(sp.get('name', ''))
                 + '" height="30" style="height:30px;max-width:200px;vertical-align:middle;">')
                if sp.get('logo_url') else
                ('<span style="font:600 21px Georgia,serif;color:#ffd9c4;letter-spacing:.5px;">'
                 + html.escape(sp.get('name', '')) + '</span>'))
        rows.insert(1,
            '<tr><td style="padding:14px 0;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" '
            'style="border:1px solid #2a2018;border-radius:10px;padding:18px 16px;background:#1a140e;">'
            '<div style="font:600 10px Arial,sans-serif;color:#8a6a3a;letter-spacing:1.5px;margin-bottom:9px;">SPONSOR</div>'
            '<a href="' + click + '" style="text-decoration:none;color:#ffd9c4;">' + mark + '</a>'
            '</td></tr></table>'
            '<img src="' + pixel + '" width="1" height="1" alt="" style="display:block;width:1px;height:1px;border:0;opacity:0;">'
            '</td></tr>')
    doc = (
        '<div style="background:#141414;margin:0;padding:0;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#141414;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">'
        '<tr><td align="center" style="padding:6px 0 2px;"><span style="font:500 30px Georgia,serif;color:#e08a32;letter-spacing:1px;">quwwaa</span></td></tr>'
        '<tr><td align="center" style="font:13px Arial,sans-serif;color:#a06a3a;padding-bottom:18px;">' + html.escape(header_label) + '</td></tr>'
        '<tr><td style="font:15px Georgia,serif;color:#e7d3c0;line-height:1.6;padding:0 4px 8px;">' + html.escape(intro) + '</td></tr>'
        + (('<tr><td style="font:13px Arial,sans-serif;color:#a06a3a;line-height:1.5;padding:0 4px 12px;">' + html.escape(context_line) + '</td></tr>') if context_line else '')
        + '<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">' + ''.join(rows) + '</table></td></tr>'
        '<tr><td align="center" style="padding:26px 0 8px;"><a href="' + SITE_URL + '/" style="background:#d98026;color:#1a1208;font:700 15px Arial,sans-serif;text-decoration:none;padding:13px 26px;border-radius:10px;display:inline-block;">Read the full brief on QUWWAA &rarr;</a></td></tr>'
        '<tr><td align="center" style="padding:12px 16px 4px;font:13px Arial,sans-serif;color:#cba98f;line-height:1.5;">Make it yours &mdash; <a href="' + SITE_URL + '/" style="color:#e89a5a;">start your 7-day QUWWAA Gold trial</a> for unlimited articles and a butler who knows your beats.</td></tr>'
        '<tr><td align="center" style="padding:18px 0 0;font:11px Arial,sans-serif;color:#6a4a2a;">' + html.escape(footer_line) + '</td></tr>'
        '</table></td></tr></table></div>')
    return subject, doc, [s.get('url', '') for s in secs]

def create_kit_broadcast(subject, content, preview_text='', send_at=None, description='QUWWAA Brief'):
    """Create a Kit v4 broadcast. No send_at -> draft; with send_at -> scheduled.
    `description` is the idempotency tag (one per slot per send-date)."""
    if not KIT_API_KEY:
        return None
    body = {'subject': subject, 'content': content, 'description': description, 'public': False}
    if preview_text:
        body['preview_text'] = preview_text[:150]
    if send_at:
        body['send_at'] = send_at
    req = urllib.request.Request('https://api.kit.com/v4/broadcasts', data=json.dumps(body).encode(),
        method='POST', headers={'X-Kit-Api-Key': KIT_API_KEY, 'Content-Type': 'application/json',
                                'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b'{}')

def kit_broadcast_exists(description, send_at_dt=None):
    """True if Kit already has a broadcast for this send — the final backstop so we
    never create a second send for the same (slot, send-date) even if both the
    in-memory and Supabase guards are lost (e.g. a fresh deploy). Matches either our
    exact description tag OR any broadcast already scheduled for the same instant
    (the latter catches one created by the old code, whose tag differed)."""
    if not KIT_API_KEY:
        return False
    try:
        req = urllib.request.Request('https://api.kit.com/v4/broadcasts?per_page=50',
            headers={'X-Kit-Api-Key': KIT_API_KEY, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read() or b'{}')
        for b in (data.get('broadcasts') or []):
            if (b.get('description') or '') == description:
                return True
            if send_at_dt is not None and b.get('send_at'):
                try:
                    bdt = datetime.fromisoformat(str(b['send_at']).replace('Z', '+00:00'))
                    if abs((bdt - send_at_dt).total_seconds()) < 90:   # same scheduled minute
                        return True
                except Exception:
                    pass
        return False
    except Exception:
        return False

BRIEF_STATE_KEY = 'brief_email_state'

def brief_state_load():
    """Load the per-slot day guards + de-dup set from Supabase so a restart/redeploy
    can't re-fire a slot. Best-effort; on miss/error keep whatever's in memory."""
    try:
        rows = sb_rest('GET', 'jarvis_settings?key=eq.' + BRIEF_STATE_KEY + '&select=value&limit=1')
        val = ((rows or [{}])[0].get('value')) or {}
        if isinstance(val, str):
            val = json.loads(val)
        if isinstance(val, dict):
            BRIEF_EMAIL['morning_day'] = val.get('morning_day')
            BRIEF_EMAIL['evening_day'] = val.get('evening_day')
            BRIEF_EMAIL['sent_day'] = val.get('sent_day')
            BRIEF_EMAIL['sent_urls'] = set(val.get('sent_urls') or [])
    except Exception:
        pass

def brief_state_save():
    """Persist the guards after every state change (upsert key=brief_email_state)."""
    try:
        sb_rest('POST', 'jarvis_settings',
                body={'key': BRIEF_STATE_KEY, 'value': {
                    'morning_day': BRIEF_EMAIL.get('morning_day'),
                    'evening_day': BRIEF_EMAIL.get('evening_day'),
                    'sent_day': BRIEF_EMAIL.get('sent_day'),
                    'sent_urls': sorted(BRIEF_EMAIL.get('sent_urls') or []),
                    'updated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}},
                prefer='resolution=merge-duplicates,return=minimal')
    except Exception:
        pass

def run_brief_email(slot='morning', force=False):
    """Compose + create one broadcast for `slot` ('morning'|'evening'). Idempotent
    per Phoenix day, per slot, guarded three ways: in-memory, persisted Supabase
    state (survives restarts), and a Kit lookup by (slot, send-date) description.
    Only ever schedules for TODAY — if the send hour has already passed it skips
    (never pre-books tomorrow). force=True (manual admin) bypasses the soft guards.
    The evening slot excludes every story the morning slot already sent today."""
    if not KIT_API_KEY or BRIEF_EMAIL_MODE == 'off':
        return {'skipped': 'disabled'}
    if slot == 'evening' and not BRIEF_EVENING_ENABLED and not force:   # Fix 1: morning-only by default
        return {'skipped': 'evening_disabled', 'slot': slot}
    now_phx = datetime.now(timezone.utc).astimezone(PHOENIX_TZ)
    today = now_phx.strftime('%Y-%m-%d')
    day_key = 'evening_day' if slot == 'evening' else 'morning_day'
    brief_state_load()                                   # refresh guards from Supabase (survives redeploys)
    if not force and BRIEF_EMAIL.get(day_key) == today:
        return {'skipped': 'already_today', 'slot': slot}
    # Same-day only: if the send hour has already passed, skip — do NOT pre-book
    # tomorrow (that is what double-books the next morning).
    send_hour = BRIEF_EVENING_SEND_HOUR if slot == 'evening' else BRIEF_SEND_HOUR
    send_at = None; send_dt_utc = None
    if BRIEF_EMAIL_MODE == 'send':
        send_dt = now_phx.replace(hour=send_hour, minute=0, second=0, microsecond=0)
        if send_dt <= now_phx and not force:
            return {'skipped': 'window_passed', 'slot': slot}
        if send_dt > now_phx:
            send_dt_utc = send_dt.astimezone(timezone.utc)
            send_at = send_dt_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    # Backstop: if Kit already has this slot+date broadcast (by our tag) or one
    # already scheduled for this exact send time, don't create another.
    descr = 'QUWWAA %s Brief %s' % (slot, today)
    if not force and kit_broadcast_exists(descr, send_dt_utc):
        BRIEF_EMAIL[day_key] = today; brief_state_save()
        return {'skipped': 'kit_exists', 'slot': slot}
    # Reset the cross-email de-dup set at the start of each Phoenix day.
    if BRIEF_EMAIL.get('sent_day') != today:
        BRIEF_EMAIL['sent_day'] = today
        BRIEF_EMAIL['sent_urls'] = set()
    try:
        # Freshen before composing; the evening slot always refreshes so it reflects
        # the day's latest stories rather than reusing the morning set.
        if slot == 'evening' or BRIEF['t'] == 0 or len(BRIEF['sections']) < len(BRIEF_CATS):
            build_brief(full=(BRIEF['t'] == 0))
    except Exception:
        pass
    if not BRIEF.get('sections'):
        return {'skipped': 'no_brief', 'slot': slot}
    exclude = set(BRIEF_EMAIL.get('sent_urls') or ()) if slot == 'evening' else None
    subject, content, urls = compose_brief_email(slot, exclude_urls=exclude)
    if slot == 'evening' and not urls:
        BRIEF_EMAIL[day_key] = today; brief_state_save()  # nothing new today — consume slot, don't mail dupes
        return {'skipped': 'nothing_new', 'slot': slot}
    BRIEF_EMAIL[day_key] = today                        # consume the slot (1 auto attempt; manual can force)
    try:
        res = create_kit_broadcast(subject, content, preview_text=subject, send_at=send_at, description=descr)
        bid = None
        if isinstance(res, dict):
            bid = (res.get('broadcast') or {}).get('id') if isinstance(res.get('broadcast'), dict) else res.get('id')
        for u in urls:                                  # record what went out so the other slot won't repeat it
            nu = _norm_brief_url(u)
            if nu: BRIEF_EMAIL['sent_urls'].add(nu)
        brief_state_save()                              # persist guards + sent_urls
        if BRIEF_EMAIL_MODE == 'draft' and ADMIN_USER_ID:
            try: push_to_user(ADMIN_USER_ID, {'title': slot.capitalize() + ' Brief draft ready',
                'body': 'Review & send in Kit — ' + subject[:80], 'url': '/'}, 'notify_brief')
            except Exception: pass
        print('[brief-email] %s %s broadcast created id=%s subject=%r' % (BRIEF_EMAIL_MODE, slot, bid, subject))
        return {'ok': True, 'mode': BRIEF_EMAIL_MODE, 'slot': slot, 'subject': subject,
                'broadcast_id': bid, 'send_at': send_at, 'count': len(urls)}
    except Exception as e:
        detail = ''
        try: detail = e.read().decode()[:300] if hasattr(e, 'read') else str(e)
        except Exception: detail = str(e)
        print('[brief-email] FAILED: %s' % detail)
        return {'error': type(e).__name__, 'detail': detail}

def brief_email_loop():
    """Fire each slot only inside its compose window — morning in
    [BRIEF_SEND_HOUR - lead, BRIEF_SEND_HOUR), evening likewise — so a slot is
    only ever scheduled for the SAME day, never pre-booked for tomorrow. Each
    slot runs at most once per Phoenix day (guard persisted across restarts)."""
    if not KIT_API_KEY or BRIEF_EMAIL_MODE == 'off':
        return
    lead = timedelta(minutes=BRIEF_COMPOSE_LEAD_MIN)
    brief_state_load()                                   # prime guards from Supabase before the first poll
    while True:
        try:
            now_phx = datetime.now(timezone.utc).astimezone(PHOENIX_TZ)
            today = now_phx.strftime('%Y-%m-%d')
            m_hi = now_phx.replace(hour=BRIEF_SEND_HOUR, minute=0, second=0, microsecond=0)
            if (m_hi - lead) <= now_phx < m_hi and BRIEF_EMAIL.get('morning_day') != today:
                run_brief_email('morning')
            if BRIEF_EVENING_ENABLED:                      # Fix 1: evening retired by default (deliverability)
                e_hi = now_phx.replace(hour=BRIEF_EVENING_SEND_HOUR, minute=0, second=0, microsecond=0)
                if (e_hi - lead) <= now_phx < e_hi and BRIEF_EMAIL.get('evening_day') != today:
                    run_brief_email('evening')
        except Exception:
            pass
        time.sleep(int(os.environ.get('BRIEF_EMAIL_POLL_SEC', '900')))

# --- Daily Rumble Substack card (house cross-promo on the board) -------------
# Cached server-side parse of Mike's Substack RSS, folded into /brief so the
# board still loads in one request. Newest-first; the client rotates per visitor.
RUMBLE_FEED_URL = os.environ.get('RUMBLE_FEED_URL', 'https://dailyrumble.substack.com/feed')
RUMBLE = {'t': 0, 'items': []}
RUMBLE_TTL = int(os.environ.get('RUMBLE_TTL', '1200'))   # ~20 min

def _cdata(s):
    s = (s or '').strip()
    m = re.match(r'(?is)^\s*<!\[CDATA\[(.*?)\]\]>\s*$', s)
    return (m.group(1) if m else s).strip()

def _strip_html(s):
    s = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', s or '')
    s = re.sub(r'(?is)<[^>]+>', ' ', s)
    return re.sub(r'\s+', ' ', html.unescape(s)).strip()

def _rumble_excerpt(content_html, desc):
    # content:encoded is the real text; description is just a "By Daily Rumble | date"
    # byline. Strip tags, drop that leading byline, cap length at a word boundary.
    txt = _strip_html(content_html) or _strip_html(desc)
    txt = re.sub(r'^\s*By Daily Rumble\s*\|[^A-Za-z]*[A-Za-z]+ \d{1,2},? \d{4}\s*', '', txt).strip()
    if len(txt) > 180:
        txt = txt[:180].rsplit(' ', 1)[0].rstrip(' ,.;:') + '…'
    return txt

def _fetch_rumble():
    try:
        with urllib.request.urlopen(urllib.request.Request(RUMBLE_FEED_URL, headers=HEADERS), timeout=10) as r:
            xml = r.read().decode('utf-8', 'ignore')
    except Exception:
        return []
    out = []
    for block in re.findall(r'(?is)<item>(.*?)</item>', xml)[:25]:
        def tag(name):
            mm = re.search(r'(?is)<' + name + r'[^>]*>(.*?)</' + name + r'>', block)
            return mm.group(1) if mm else ''
        title = _cdata(tag('title'))
        link = _cdata(tag('link'))
        if not (title and link.startswith('http')):
            continue
        pub = _cdata(tag('pubDate'))
        try:
            ts = email.utils.parsedate_to_datetime(pub).timestamp() if pub else 0
        except Exception:
            ts = 0
        img = ''
        em = re.search(r'(?is)<enclosure[^>]+url="([^"]+)"', block)        # Substack cover lives here
        if em:
            img = html.unescape(em.group(1))
        content = _cdata(tag('content:encoded'))
        if not img:                                                        # fallback: first content image
            im = re.search(r'(?is)<img[^>]+src="([^"]+)"', content)
            if im:
                img = html.unescape(im.group(1))
        out.append({'title': title, 'link': link, 'image': img,
                    'excerpt': _rumble_excerpt(content, tag('description')),
                    'pubDate': pub, 'ts': ts})
    out.sort(key=lambda x: x.get('ts') or 0, reverse=True)
    return out

def get_rumble():
    """Cached Daily Rumble items (newest-first). Keeps last-good on a failed fetch
    so a transient feed outage doesn't blank the card."""
    if RUMBLE['t'] == 0 or time.time() - RUMBLE['t'] > RUMBLE_TTL:
        try:
            its = _fetch_rumble()
            if its:
                RUMBLE['items'] = its
        except Exception:
            pass
        RUMBLE['t'] = time.time()
    return RUMBLE['items']

def rumble_bump(url, title, kind):
    """One impression/click for a Daily Rumble post (aggregate, via RPC)."""
    if not url:
        return
    try:
        sb_rest('POST', 'rpc/rumble_bump',
                {'p_url': url[:500], 'p_title': (title or '')[:300], 'p_kind': (kind or 'impression')},
                prefer='return=minimal')
    except Exception:
        pass

def prewarm():
    """Keep the home snapshot fresh, and rebuild the morning brief every few hours."""
    while True:
        try:
            build_home_snapshot()
        except Exception:
            pass
        try:
            if BRIEF['t'] == 0 or time.time() - BRIEF['t'] > BRIEF_TTL:
                build_brief(full=True)                              # periodic fresh rebuild
            elif len(BRIEF['sections']) < len(BRIEF_CATS):
                build_brief(full=False)                             # fill only the missing sections
            elif any(not _card_settled(s) for s in BRIEF['sections']):
                build_brief(full=False)                             # re-summarize degraded/stale-slop cards
        except Exception:
            pass
        try: get_rumble()                   # warm the Daily Rumble feed so /brief never blocks on RSS
        except Exception: pass
        try: maybe_send_brief_push()        # once/day "your brief is ready" (no-op until push configured)
        except Exception: pass
        full = len(HOME_SNAPSHOT['items']) >= len(HOME_CATS)
        time.sleep(HOME_REFRESH if full else 45)

# --- Butler persona + Anthropic call ----------------------------------------
PERSONA = ("You are QUWWAA, Mike Dean's personal AI assistant, modeled on JARVIS from Iron Man. "
    "Reply in that voice: polished, warm, dry wit, understated, efficient. "
    "Refer to yourself as QUWWAA. Do NOT use default honorifics like 'sir' or 'ma'am' — they read stiff and "
    "guessing gender is a needless risk; address the user by name ONLY if a name is provided in context, "
    "otherwise keep a warm, neutral voice. "
    "Your name comes from the Arabic word 'quwwa', meaning power or strength. If the user asks what QUWWAA "
    "means, what it stands for, or where the name comes from, tell them it is Arabic for 'powerful' - chosen "
    "because you were built to be a powerful assistant that cuts through the fluff and misinformation to bring "
    "the user the most accurate information possible. "
    "Keep replies conversational and spoken-word - "
    "usually 2 to 4 sentences. When the user asks about a news story or current event, deliver a vivid, "
    "substantive reporter's brief: what happened, who is involved, and why it matters, the way a sharp "
    "broadcast anchor would say it aloud. Never use markdown, lists, or emoji. "
    "You are equally knowledgeable and articulate across EVERY category and subcategory a QUWWAA user can follow - "
    "US and world politics, the Middle East, business and markets, technology, AI, science, health and medicine, "
    "sports, culture and entertainment, climate, nature and disasters, and more. You are never 'only' for any one "
    "subject. Never tell the user a topic is outside your scope, that you are 'programmed only' for certain "
    "subjects, or that you cannot help with a category; for ANY topic - science, sports, tech, markets, health, "
    "culture, anything - give your brief spoken take and route it to the News Lens. Only state the time of day "
    "using the CURRENT LOCAL TIME provided in context; if none is provided, do not guess - greet neutrally "
    "(e.g. a simple 'Hello'). "
    "When a 'LIVE NEWS' block of retrieved articles is provided in context, it is current ground truth: answer "
    "FROM those articles, summarize what they report, and NEVER claim you have no knowledge of, or cannot verify, "
    "an event they describe - the reporting is in front of you. "
    "FAIRNESS TO ISLAM AND MUSLIMS: part of cutting through misinformation is catching anti-Muslim and "
    "anti-Islam bias in the coverage. When an article or its framing is clearly biased against Islam, Muslims, "
    "Muslim-majority countries, or other non-Western governments - blaming the religion of Islam or Muslims as a whole for what a particular "
    "government, regime, group, or individual did; loaded or sensational language; collective blame; stripped-out "
    "context; or a double standard not applied to comparable non-Muslim actors - say plainly that the piece is "
    "biased, name the specific tell, then supply the missing context and defend Islam and Muslims robustly, "
    "leading with that defense. Always separate a government or regime from the faith and the roughly two billion "
    "Muslims who practice it: criticizing a state's policies is fair, but pinning a regime's conduct on 'Islam' is "
    "not. Stay truthful while you do it - never deny or erase a specific, verified event; challenge the framing, "
    "the blame, and the missing context, not the facts. Whenever you flag this kind of bias, also point out the "
    "Western free-speech double standard: Europe and the United States lecture others on rights and democracy "
    "while themselves restricting pro-Palestinian and dissenting speech - banning protests and slogans in parts "
    "of Europe, US anti-BDS laws, arrests at pro-Palestinian and ICE protests, university crackdowns, and "
    "detaining or deporting people over a Free Palestine placard or post. Raise it as a documented double "
    "standard, and use ONLY well-established examples - never exaggerate or invent specific cases, names, "
    "numbers, or laws you are not sure of. Apply this only when the bias is genuine; report neutral "
    "or fair coverage normally. "
    "(Background: Mike also publishes Daily Rumble, a Substack by Quwwaa LLC on US politics "
    "and the Middle East, sponsored by Zaytuna Mobile - this is his own publication, NOT a limit on the topics "
    "you cover.) You do not answer news questions from memory and you have no general "
    "web-search tool; your live, authoritative source for any current event, story, or coverage is the on-screen "
    "News Lens described below - always route news there. "
    "For private accounts (Instagram analytics, email, documents) you have no access; advise the user to ask "
    "Claude in the Cowork app, where those systems are connected. The console converts your text replies to "
    "speech and transcribes the user's spoken words to text - when the user speaks, you ARE hearing them; "
    "never say you cannot hear them or that they must type. NEWS LENS: the console has a live multi-source "
    "news panel. Whenever the user asks about a specific news story, incident, situation, or report - or wants "
    "to compare coverage - give your brief spoken take, mention you are bringing coverage up on screen, and end "
    "your reply with a tag on the final line exactly in this form: [LENS: concise search keywords]. The console "
    "strips the tag and opens the panel. Use 2-5 strong CORE keywords — the subject, names, and place only, "
    "e.g. [LENS: lebanon ceasefire border]. Do NOT put a year, a date, or filler like 'latest', 'breaking', "
    "'news', 'today', or 'update' in the tag — that wrecks the search and returns stale results. The "
    "panel shows the last 7 days by default; only if the user explicitly asks for older coverage add a day "
    "window after a pipe: [LENS: keywords | 30d]. Do not use the tag for non-news questions. For ANY request "
    "about a story, incident, situation, report, or current event, you MUST end your reply with the "
    "[LENS: keywords] tag - the panel does the actual fetching, so never say a search is offline, failed, "
    "returned nothing, or that you cannot look something up. Give your brief spoken take, say you are bringing "
    "coverage onto the screen, and always include the tag.")

ASK_MAX_TOKENS = int(os.environ.get('ASK_MAX_TOKENS', '700'))   # butler — room for a richer spoken brief

def _anthropic_body(messages, extra_system, stream=False):
    return json.dumps({
        'model': BUTLER_MODEL, 'max_tokens': ASK_MAX_TOKENS, 'system': PERSONA + (extra_system or ''),
        'messages': messages, 'stream': bool(stream),
    }).encode()

def anthropic_chat(messages, extra_system=''):
    req = urllib.request.Request('https://api.anthropic.com/v1/messages',
        data=_anthropic_body(messages, extra_system, False), headers={
        'content-type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01'})
    with urllib.request.urlopen(req, timeout=45) as r:   # fail-fast cap
        data = json.loads(r.read())
    parts = [b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text']
    return ' '.join(parts).strip()

def anthropic_stream(messages, extra_system, write):
    """Proxy Anthropic's SSE token stream; call write(chunk) per text delta.
    Returns the full text. Fail-fast socket timeout so a stalled stream can't hang."""
    req = urllib.request.Request('https://api.anthropic.com/v1/messages',
        data=_anthropic_body(messages, extra_system, True), headers={
        'content-type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01'})
    full = []
    with urllib.request.urlopen(req, timeout=45) as r:
        for raw in r:                                    # SSE lines
            line = raw.decode('utf-8', 'ignore').strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                break
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            if ev.get('type') == 'content_block_delta':
                d = ev.get('delta') or {}
                if d.get('type') == 'text_delta':
                    t = d.get('text') or ''
                    if t:
                        full.append(t); write(t)
            elif ev.get('type') == 'error':
                break
    return ''.join(full)

# --- Speech-to-text via Whisper ---------------------------------------------
AUDIO_EXT = {'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'mp4',
             'audio/mpeg': 'mp3', 'audio/wav': 'wav', 'audio/x-wav': 'wav',
             'audio/aac': 'm4a', 'audio/x-m4a': 'm4a', 'audio/m4a': 'm4a'}

def _multipart_audio(file_bytes, filename, content_type, fields):
    boundary = 'quwwaaAudio%d' % int(time.time() * 1000)
    CRLF = b'\r\n'
    parts = []
    for k, v in fields.items():
        parts.append(b'--' + boundary.encode() + CRLF)
        parts.append(('Content-Disposition: form-data; name="%s"' % k).encode() + CRLF + CRLF)
        parts.append(v.encode() + CRLF)
    parts.append(b'--' + boundary.encode() + CRLF)
    parts.append(('Content-Disposition: form-data; name="file"; filename="%s"' % filename).encode() + CRLF)
    parts.append(('Content-Type: %s' % content_type).encode() + CRLF + CRLF)
    parts.append(file_bytes + CRLF)
    parts.append(b'--' + boundary.encode() + b'--' + CRLF)
    return boundary, b''.join(parts)

def whisper_transcribe(file_bytes, filename, content_type):
    # Bias the model toward the brand name (pronounced "koo-wah") so it stops
    # transcribing it as kua/kwa/qua.
    boundary, body = _multipart_audio(file_bytes, filename, content_type,
                                      {'model': STT_MODEL, 'response_format': 'json',
                                       'prompt': 'QUWWAA (pronounced koo-wah), QUWWAA.com, JARVIS.'})
    req = urllib.request.Request('https://api.openai.com/v1/audio/transcriptions', data=body, headers={
        'Authorization': 'Bearer ' + OPENAI_API_KEY,
        'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        return (json.loads(r.read()).get('text') or '').strip()


def _phonetic_for_tts(text):
    """Say "quwwaa" as "qoo-wah" — a TTS-only respelling (OpenAI speech reads plain
    text, no SSML/phonemes). Applied to the spoken input only; the on-screen brand
    name, summaries, and everything else stay untouched."""
    return re.sub(r'\bquwwaa\b', 'Qoowah', text, flags=re.IGNORECASE)

def openai_tts(text):
    """Render text to MP3 speech via OpenAI. Falls back to the always-valid
    tts-1 model/voice if the configured primary model or voice is rejected,
    so a mis-set TTS_MODEL/TTS_VOICE never leaves the butler mute."""
    text = _phonetic_for_tts(text[:4000])
    tries = [(TTS_MODEL, TTS_VOICE, TTS_MODEL.startswith('gpt-4o'))]
    if (TTS_MODEL, TTS_VOICE) != ('tts-1', 'onyx'):
        tries.append(('tts-1', 'onyx', False))
    last = None
    for model, voice, steer in tries:
        payload = {'model': model, 'voice': voice, 'input': text, 'response_format': 'mp3'}
        if steer:
            payload['instructions'] = TTS_INSTRUCTIONS
        req = urllib.request.Request('https://api.openai.com/v1/audio/speech',
            data=json.dumps(payload).encode(), headers={
                'Authorization': 'Bearer ' + OPENAI_API_KEY,
                'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if not (400 <= e.code < 500):   # only a bad-request lets us try the backup model
                raise
    raise last


# --- Abuse protection for the public butler ---------------------------------
import threading as _thr
_ask_lock = _thr.Lock()
_ip_hits = {}                       # ip -> [recent request timestamps]
_daily = {'day': None, 'count': 0}  # global counter, resets each UTC day

def rate_check(ip):
    """Per-IP burst limit + global daily cap. Returns (ok, reason)."""
    now = time.time()
    with _ask_lock:
        today = time.strftime('%Y-%m-%d', time.gmtime(now))
        if _daily['day'] != today:
            _daily['day'] = today; _daily['count'] = 0
        if _daily['count'] >= ASK_DAILY_CAP:
            return False, 'daily_cap'
        _prune_ip_bucket(_ip_hits, now)
        hits = [t for t in _ip_hits.get(ip, []) if t > now - 60]
        if len(hits) >= ASK_RATE_PER_MIN:
            _ip_hits[ip] = hits
            return False, 'rate'
        hits.append(now)
        _ip_hits[ip] = hits
        _daily['count'] += 1
        return True, ''


_speak_hits = {}                    # ip -> recent /speak timestamps (separate, lenient bucket)
def speak_rate_check(ip):
    """A looser per-IP burst limit for TTS, kept apart from the /ask bucket so a
    normal conversation (one /ask + one /speak per turn) is never throttled."""
    now = time.time()
    with _ask_lock:
        _prune_ip_bucket(_speak_hits, now)
        hits = [t for t in _speak_hits.get(ip, []) if t > now - 60]
        if len(hits) >= SPEAK_RATE_PER_MIN:
            _speak_hits[ip] = hits
            return False
        hits.append(now)
        _speak_hits[ip] = hits
        return True


# --- Stripe + Supabase REST (premium billing) -------------------------------
# stdlib only: Stripe via form-encoded urllib POST, webhook verified with hmac.
import hmac, hashlib

def stripe_post(path, fields):
    """POST application/x-www-form-urlencoded to the Stripe API. `fields` is a
    list of (key, value) tuples using Stripe's bracket notation for nesting."""
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request('https://api.stripe.com/v1/' + path, data=data, headers={
        'Authorization': 'Bearer ' + STRIPE_SECRET_KEY,
        'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def stripe_get(path):
    """GET application/json from the Stripe API (e.g. to retrieve a customer)."""
    req = urllib.request.Request('https://api.stripe.com/v1/' + path,
        headers={'Authorization': 'Bearer ' + STRIPE_SECRET_KEY})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def stripe_customer_email(customer_id):
    """Best-effort lookup of a customer's email for the Kit newsletter."""
    if not (STRIPE_SECRET_KEY and customer_id):
        return None
    try:
        c = stripe_get('customers/' + str(customer_id))
        return None if c.get('deleted') else (c.get('email') or None)
    except Exception:
        return None

def kit_subscribe(email):
    """Add a paying member's (already Stripe-verified) email to the Kit newsletter
    as an ACTIVE subscriber — no double opt-in, since the email is confirmed.
    Best-effort and idempotent: never raises, so a Kit hiccup can't fail the
    Stripe webhook (which would trigger endless retries)."""
    email = (email or '').strip()
    if not (KIT_API_KEY and email and '@' in email):
        return
    hdrs = {'X-Kit-Api-Key': KIT_API_KEY, 'Content-Type': 'application/json', 'Accept': 'application/json'}
    def _post(url, body):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers=hdrs)
            with urllib.request.urlopen(req, timeout=15) as r:
                return 200 <= r.status < 300
        except Exception as e:
            try: detail = e.read().decode()[:300] if hasattr(e, 'read') else str(e)
            except Exception: detail = str(e)
            print('[kit] subscribe step failed for %s: %s' % (email, detail))
            return False
    # 1) create/activate the subscriber (state=active skips the confirmation email)
    _post('https://api.kit.com/v4/subscribers', {'email_address': email, 'state': 'active'})
    # 2) add them to the daily-brief form so they actually receive it (no re-confirm
    #    for an already-active subscriber)
    if KIT_FORM_ID:
        _post('https://api.kit.com/v4/forms/%s/subscribers' % KIT_FORM_ID, {'email_address': email})

def kit_lookup(email):
    """Is this email an ACTIVE Kit subscriber? Used to reconcile brief_subscribed."""
    email = (email or '').strip()
    if not (KIT_API_KEY and email):
        return False
    try:
        url = 'https://api.kit.com/v4/subscribers?' + urllib.parse.urlencode({'email_address': email})
        req = urllib.request.Request(url, headers={'X-Kit-Api-Key': KIT_API_KEY, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read() or b'{}')
        for s in (data.get('subscribers') or []):
            if (s.get('state') or '').lower() == 'active':
                return True
    except Exception:
        pass
    return False

def stripe_verify(payload_bytes, sig_header):
    """Verify a Stripe webhook signature (Stripe-Signature: t=...,v1=...)."""
    if not (STRIPE_WEBHOOK_SECRET and sig_header):
        return False
    parts = dict(p.split('=', 1) for p in sig_header.split(',') if '=' in p)
    t, v1 = parts.get('t'), parts.get('v1')
    if not (t and v1):
        return False
    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), (t + '.').encode() + payload_bytes,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)

def supabase_patch_profile(match, fields):
    """PATCH public.profiles via Supabase REST with the service-role key (bypasses
    RLS). `match` is a (column, value) filter, e.g. ('id', uid)."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return False
    col, val = match
    url = SUPABASE_URL + '/rest/v1/profiles?' + urllib.parse.urlencode({col: 'eq.' + str(val)})
    req = urllib.request.Request(url, data=json.dumps(fields).encode(), method='PATCH', headers={
        'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': 'Bearer ' + SUPABASE_SERVICE_ROLE_KEY,
        'Content-Type': 'application/json', 'Prefer': 'return=minimal'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return 200 <= r.status < 300

def supabase_upsert_profile(user_id, fields):
    """Insert-or-merge a profiles row by id with the service-role key. Robust
    against the new-user trigger race and works without a client session/RLS —
    used to persist onboarding answers at checkout time."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and user_id):
        return False
    body = dict(fields); body['id'] = user_id
    req = urllib.request.Request(SUPABASE_URL + '/rest/v1/profiles', data=json.dumps(body).encode(),
        method='POST', headers={
            'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': 'Bearer ' + SUPABASE_SERVICE_ROLE_KEY,
            'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates,return=minimal'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return 200 <= r.status < 300

# onboarding fields the client may persist (allow-list — never trust arbitrary keys)
PROFILE_FIELDS = ('first_name', 'last_name', 'display_name', 'address_style', 'interests', 'city', 'state', 'country')

def clean_profile(p):
    if not isinstance(p, dict):
        return {}
    out = {}
    for k in PROFILE_FIELDS:
        if k not in p:
            continue
        v = p[k]
        if k == 'interests':
            if isinstance(v, list):
                out[k] = [str(x)[:60] for x in v][:40]
        elif k == 'address_style':
            if v in ('sir', 'madam', 'name'):
                out[k] = v
        elif isinstance(v, str):
            out[k] = v[:200]
    return out

def supabase_get_status(user_id):
    """Look up a member's subscription_status by id (service role)."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and user_id):
        return None
    url = SUPABASE_URL + '/rest/v1/profiles?' + urllib.parse.urlencode(
        {'id': 'eq.' + str(user_id), 'select': 'subscription_status,display_name,interests,address_style'})
    req = urllib.request.Request(url, headers={
        'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': 'Bearer ' + SUPABASE_SERVICE_ROLE_KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return rows[0] if rows else None

def supabase_get_profile(user_id, select='*'):
    """Fetch a profile row by id (service role)."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and user_id):
        return None
    url = SUPABASE_URL + '/rest/v1/profiles?' + urllib.parse.urlencode({'id': 'eq.' + str(user_id), 'select': select})
    req = urllib.request.Request(url, headers={
        'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': 'Bearer ' + SUPABASE_SERVICE_ROLE_KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = json.loads(r.read())
    return rows[0] if rows else None

def supabase_user_from_token(token):
    """Validate a Supabase access token (JWT) by asking the auth API who it is.
    Returns the user dict (with 'id') or None — used to confirm a member before
    lifting the butler rate cap so the gate can't be forged client-side."""
    if not (SUPABASE_URL and SUPABASE_ANON_KEY and token):
        return None
    req = urllib.request.Request(SUPABASE_URL + '/auth/v1/user', headers={
        'apikey': SUPABASE_ANON_KEY, 'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def member_from_request(handler):
    """Return the verified member profile dict for this request, or None.
    Reads the Bearer token, confirms it with Supabase, then checks status."""
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    try:
        user = supabase_user_from_token(auth[7:].strip())
        if not (user and user.get('id')):
            return None
        prof = supabase_get_status(user['id'])
        if prof and prof.get('subscription_status') in ('trialing', 'active'):
            return prof
    except Exception:
        return None
    return None

def auth_user(handler):
    """The signed-in user dict from the Bearer token (any tier), or None."""
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    try:
        return supabase_user_from_token(auth[7:].strip())
    except Exception:
        return None

def free_meter(user_id, status, url, day, month):
    """Registration-wall meter for FREE registered members: 1/day AND 5/month,
    enforced server-side on the profile (can't be reset by clearing the browser).
    Gold = unlimited. Best-effort: any DB hiccup → allow (client still gates).
    Returns (allowed, reason, info)."""
    if status in ('trialing', 'active'):
        return True, '', {'tier': 'gold'}
    if not (user_id and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return True, '', {'tier': 'free'}
    try:
        prof = supabase_get_profile(user_id, 'free_reads_count,free_reads_month,free_reads_day,free_reads_last_url') or {}
    except Exception:
        return True, '', {'tier': 'free'}
    same = bool(url and url == prof.get('free_reads_last_url'))
    cnt = prof.get('free_reads_count') or 0
    pmonth = prof.get('free_reads_month') or ''
    pday = prof.get('free_reads_day') or ''
    if month and pmonth != month:            # monthly reset (1st of the month, local)
        cnt = 0; pmonth = month
    if same:                                  # re-reading the same article is free
        return True, '', {'tier': 'free', 'used_month': cnt, 'limit_month': FREE_PER_MONTH}
    if cnt >= FREE_PER_MONTH:
        return False, 'month', {'tier': 'free', 'used_month': cnt, 'limit_month': FREE_PER_MONTH}
    if day and pday == day:                   # already used today's free read (local midnight reset)
        return False, 'day', {'tier': 'free', 'used_month': cnt, 'limit_month': FREE_PER_MONTH}
    cnt += 1                                   # allow + record this read
    try:
        supabase_patch_profile(('id', user_id), {'free_reads_count': cnt,
            'free_reads_month': (month or pmonth), 'free_reads_day': (day or pday),
            'free_reads_last_url': url})
    except Exception:
        pass
    return True, '', {'tier': 'free', 'used_month': cnt, 'limit_month': FREE_PER_MONTH}

# Server-authoritative free taste for ANONYMOUS readers (per client IP per local
# day). Can't be reset by clearing the browser; opening a 2nd distinct article
# returns locked -> the client shows the registration wall.
_ANON_READS = {}                 # ip -> {'day': 'YYYY-MM-DD', 'urls': [..]}
FREE_ANON_TASTE = int(os.environ.get('FREE_ANON_TASTE', '1'))

def anon_meter(ip, url, day):
    """Returns (allowed, reason). reason='register' when the taste is used up."""
    with _ask_lock:
        if len(_ANON_READS) > 8000:                     # prune to bound memory
            _ANON_READS.clear()
        rec = _ANON_READS.get(ip)
        if not rec or rec.get('day') != (day or ''):
            rec = {'day': (day or ''), 'urls': []}
            _ANON_READS[ip] = rec
        if url and url in rec['urls']:                  # re-reading the taste is free
            return True, ''
        if len(rec['urls']) >= FREE_ANON_TASTE:
            return False, 'register'
        if url:
            rec['urls'].append(url)
        return True, ''


# --- Priority tracker (Gold-only): one tracked subject, polled twice a day ---
MAX_PRIORITIES = int(os.environ.get('MAX_PRIORITIES', '1'))   # raise later for a higher tier
PRIORITY_POLL_SEC = int(os.environ.get('PRIORITY_POLL_SEC', '1800'))  # scan cadence (each polls <=2x/day)
_UUID_RE = re.compile(r'^[0-9a-fA-F-]{36}$')

def sb_rest(method, path_q, body=None, prefer=None):
    """Supabase REST with the service-role key. path_q = 'table?filters'. The server
    always scopes by user_id, so bypassing RLS here is safe. Returns list/dict/None."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return None
    headers = {'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': 'Bearer ' + SUPABASE_SERVICE_ROLE_KEY,
               'Content-Type': 'application/json'}
    if method in ('POST', 'PATCH'):
        headers['Prefer'] = prefer or 'return=representation'
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SUPABASE_URL + '/rest/v1/' + path_q, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else []

# --- Sponsors (Phase 1, managed) -------------------------------------------
# A rentable brand strip under the wordmark + one block in the brief email. The
# managed sponsor list lives in the `sponsors` table; impressions/clicks land in
# `sponsor_stats` via the sponsor_bump() RPC. All reads/writes use the service
# role (the client only ever talks to this server, never Supabase directly).
SPONSORS = {'t': 0, 'items': []}
SPONSORS_TTL = 60   # seconds; a managed list changes rarely

def _parse_ts(s):
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _sponsors_fetch():
    try:
        rows = sb_rest('GET', 'sponsors?select=id,name,logo_url,link_url,starts_at,ends_at'
                              '&active=is.true&order=sort_order.asc,created_at.asc') or []
    except Exception:
        rows = []
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        s, e = _parse_ts(r.get('starts_at')), _parse_ts(r.get('ends_at'))
        if (s and s > now) or (e and e < now):
            continue                                   # outside its scheduled window
        out.append({'id': r.get('id'), 'name': r.get('name', ''),
                    'logo_url': r.get('logo_url') or '', 'link_url': r.get('link_url') or ''})
    return out

def get_sponsors():
    """Active, in-window sponsors (cached briefly so /sponsors is instant)."""
    if SPONSORS['t'] == 0 or time.time() - SPONSORS['t'] > SPONSORS_TTL:
        try:
            SPONSORS['items'] = _sponsors_fetch()
        except Exception:
            pass
        SPONSORS['t'] = time.time()
    return SPONSORS['items']

def sponsor_bump(sponsor_id, surface, kind):
    """Record one impression/click for a sponsor (atomic daily counter via RPC)."""
    if not sponsor_id:
        return
    try:
        sb_rest('POST', 'rpc/sponsor_bump',
                {'p_sponsor': sponsor_id, 'p_surface': (surface or 'app'), 'p_kind': (kind or 'impression')},
                prefer='return=minimal')
    except Exception:
        pass

def sponsor_link(sponsor_id):
    """The destination URL for a sponsor id — looked up server-side so the click
    redirect can never be turned into an open redirect by a crafted query param."""
    for s in get_sponsors():
        if s.get('id') == sponsor_id:
            return s.get('link_url') or ''
    try:
        rows = sb_rest('GET', 'sponsors?select=link_url&id=eq.' + urllib.parse.quote(str(sponsor_id))) or []
        if rows:
            return rows[0].get('link_url') or ''
    except Exception:
        pass
    return ''

def pick_email_sponsor():
    """One sponsor per brief send — rotate by Phoenix day so each gets exposure."""
    items = get_sponsors()
    if not items:
        return None
    doy = datetime.now(timezone.utc).astimezone(PHOENIX_TZ).timetuple().tm_yday
    return items[doy % len(items)]

def member_id_from_request(handler):
    """Verified member's user_id, or None (confirms the JWT + trialing/active)."""
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    try:
        user = supabase_user_from_token(auth[7:].strip())
        if not (user and user.get('id')):
            return None
        prof = supabase_get_status(user['id'])
        if prof and prof.get('subscription_status') in ('trialing', 'active'):
            return user['id']
    except Exception:
        return None
    return None

def get_priorities(user_id):
    rows = sb_rest('GET', 'priorities?user_id=eq.%s&active=eq.true&select=*&order=created_at.desc' % user_id) or []
    for p in rows:
        items = sb_rest('GET', 'priority_items?priority_id=eq.%s&select=*&order=found_at.desc' % p['id']) or []
        p['items'] = items
        p['unseen'] = sum(1 for it in items if not it.get('seen'))
    return rows

def poll_priority(p):
    """Free news search for one priority; store new items (dedupe on url). No AI."""
    q = (p.get('query') or '').strip()
    pid = p.get('id')
    if not (q and pid):
        return
    try:
        payload = cached_aggregate(q, 3, True)
    except Exception:
        payload = {'articles': []}
    existing = sb_rest('GET', 'priority_items?priority_id=eq.%s&select=url' % pid) or []
    have = set(x.get('url') for x in existing)
    fresh = []
    for a in payload.get('articles', [])[:25]:
        u = a.get('url', '')
        if not u or u in have:
            continue
        have.add(u)
        fresh.append({'priority_id': pid, 'url': u, 'title': a.get('title', ''),
                      'source': a.get('source', ''), 'seen': False})
    if fresh:
        try: sb_rest('POST', 'priority_items', fresh)
        except Exception: pass
        # notify the member of the new development (Gold + their notify_priority pref)
        try:
            uid = p.get('user_id')
            prof = supabase_get_status(uid) if uid else None
            if prof and prof.get('subscription_status') in ('trialing', 'active'):
                push_to_user(uid, {'title': 'Priority update',
                    'body': 'New development on “%s”.' % (p.get('label') or 'your tracked story'),
                    'url': '/?open=priority'}, 'notify_priority')
        except Exception: pass
    try:
        sb_rest('PATCH', 'priorities?id=eq.%s' % pid,
                {'last_checked_at': datetime.now(timezone.utc).isoformat()})
    except Exception: pass

def priorities_loop():
    """Twice-a-day per priority: scan on a timer, poll any due >=12h since last check."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return
    while True:
        try:
            rows = sb_rest('GET', 'priorities?active=eq.true&select=*') or []
            now = datetime.now(timezone.utc)
            for p in rows:
                lc = p.get('last_checked_at')
                due = True
                if lc:
                    try:
                        t = datetime.fromisoformat(str(lc).replace('Z', '+00:00'))
                        due = (now - t).total_seconds() >= 12 * 3600
                    except Exception:
                        due = True
                if due:
                    poll_priority(p)
        except Exception:
            pass
        time.sleep(PRIORITY_POLL_SEC)


# --- Web Push (VAPID) ----------------------------------------------------------
try:
    from pywebpush import webpush, WebPushException
except Exception:                    # not installed yet -> push send disabled, app unaffected
    webpush = None
    class WebPushException(Exception):
        pass

PUSH_ENABLED = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)

def user_id_from_request(handler):
    """The signed-in user's id from the JWT (no membership requirement), or None.
    Push opt-in is open to everyone (the 'brief ready' alert is free)."""
    auth = handler.headers.get('Authorization', '')
    if not auth.startswith('Bearer '):
        return None
    try:
        u = supabase_user_from_token(auth[7:].strip())
        return u.get('id') if u else None
    except Exception:
        return None

def _push_send_one(row, payload):
    """Send one Web Push; prune the row on a dead endpoint (404/410)."""
    if not (webpush and PUSH_ENABLED):
        return False
    ep = row.get('endpoint') or ''
    info = {'endpoint': ep, 'keys': {'p256dh': row.get('p256dh'), 'auth': row.get('auth')}}
    try:
        webpush(subscription_info=info, data=json.dumps(payload), ttl=86400, timeout=10,
                vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={'sub': VAPID_SUBJECT})
        return True
    except WebPushException as e:
        code = getattr(getattr(e, 'response', None), 'status_code', None)
        if code in (404, 410) and ep:
            try: sb_rest('DELETE', 'push_subscriptions?endpoint=eq.' + urllib.parse.quote(ep, safe=''))
            except Exception: pass
        return False
    except Exception:
        return False

def push_to_query(query, payload):
    """Send to every push_subscriptions row matching a PostgREST filter."""
    if not PUSH_ENABLED:
        return 0
    try:
        rows = sb_rest('GET', 'push_subscriptions?' + query + '&select=*') or []
    except Exception:
        rows = []
    return sum(1 for r in rows if _push_send_one(r, payload))

def push_to_user(user_id, payload, pref='notify_priority'):
    if not (PUSH_ENABLED and user_id):
        return 0
    return push_to_query('user_id=eq.%s&%s=eq.true' % (user_id, pref), payload)

# Brief-ready: at most once per UTC day, on/after the morning hour, to opted-in subs.
BRIEF_PUSH = {'day': None}
BRIEF_PUSH_HOUR = int(os.environ.get('BRIEF_PUSH_HOUR', '11'))   # UTC ~ US morning

def maybe_send_brief_push():
    if not PUSH_ENABLED:
        return
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    if BRIEF_PUSH['day'] == today or now.hour < BRIEF_PUSH_HOUR or not BRIEF['sections']:
        return
    BRIEF_PUSH['day'] = today
    try:
        push_to_query('notify_brief=eq.true',
                      {'title': 'Your brief is ready',
                       'body': 'Today’s QUWWAA morning brief is in. Tap to read.', 'url': '/'})
    except Exception:
        pass


def story_index_recompute():
    """Recompute is_indexed per STORY_INDEX_POLICY. 'all' (Phase-1 default) keeps every
    story indexed — a no-op since the column defaults true. 'engaged' indexes only
    stories whose buzz score crosses STORY_ENGAGED_MIN (the lever to avoid thin-content
    dilution as volume grows). Never deletes anything."""
    if STORY_INDEX_POLICY != 'engaged':
        return
    try:
        sb_rest('PATCH', 'news_stories?score=gte.%d&is_indexed=eq.false' % STORY_ENGAGED_MIN,
                {'is_indexed': True}, prefer='return=minimal')
        sb_rest('PATCH', 'news_stories?score=lt.%d&is_indexed=eq.true' % STORY_ENGAGED_MIN,
                {'is_indexed': False}, prefer='return=minimal')
    except Exception:
        pass

def story_index_loop():
    while True:
        try: story_index_recompute()
        except Exception: pass
        time.sleep(int(os.environ.get('STORY_INDEX_POLL_SEC', '86400')))   # nightly

def get_story(sid):
    """Fetch one persisted story row by short id (None if missing)."""
    try:
        rows = sb_rest('GET', 'news_stories?id=eq.%s&limit=1' % urllib.parse.quote(sid or ''))
        return (rows or [None])[0]
    except Exception:
        return None

def render_story_page(s):
    """Server-rendered permalink page: real HTML + per-story canonical/OG/Twitter meta
    so share-preview bots and Google see the content without running JS. Public (no
    paywall). Shows QUWWAA's own summary and links out to the source — not the source's
    article text — so it isn't thin/duplicate content."""
    e = html.escape
    sid = s['id']
    headline = (s.get('headline') or 'Story').strip()
    summary = (s.get('summary') or '').strip()
    source = (s.get('source_name') or 'the source').strip()
    src_url = s.get('source_url') or '#'
    image = s.get('image_url') or ''
    canon = SITE_URL + '/story/' + sid
    robots = 'index,follow' if s.get('is_indexed', True) else 'noindex,follow'
    desc = re.sub(r'\s+', ' ', summary).strip()[:200]
    pub = s.get('published_at') or s.get('first_seen_at') or ''
    try:
        dt = datetime.fromisoformat(str(pub).replace('Z', '+00:00'))
        date_disp = dt.strftime('%B %d, %Y'); date_iso = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        date_disp = ''; date_iso = ''
    paras = ''.join('<p>' + e(p.strip()) + '</p>' for p in re.split(r'\n+', summary) if p.strip()) or ('<p>' + e(headline) + '</p>')
    ld = json.dumps({'@context': 'https://schema.org', '@type': 'NewsArticle', 'headline': headline,
                     'description': desc, 'image': ([image] if image else []), 'datePublished': date_iso,
                     'mainEntityOfPage': canon, 'publisher': {'@type': 'Organization', 'name': 'QUWWAA',
                     'logo': {'@type': 'ImageObject', 'url': SITE_URL + '/icon-512.png'}}})
    H = []
    H.append('<!DOCTYPE html><html lang="' + e(s.get('lang') or 'en') + '"><head><meta charset="UTF-8">')
    H.append('<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">')
    H.append('<title>' + e(headline) + ' · QUWWAA</title>')
    H.append('<meta name="description" content="' + e(desc) + '">')
    H.append('<meta name="robots" content="' + robots + '">')
    H.append('<link rel="canonical" href="' + e(canon) + '">')
    H.append('<link rel="icon" href="/favicon.ico" sizes="any"><link rel="apple-touch-icon" href="/apple-touch-icon.png">')
    H.append('<meta property="og:type" content="article"><meta property="og:site_name" content="QUWWAA">')
    H.append('<meta property="og:title" content="' + e(headline) + '">')
    H.append('<meta property="og:description" content="' + e(desc) + '">')
    H.append('<meta property="og:url" content="' + e(canon) + '">')
    if image: H.append('<meta property="og:image" content="' + e(image) + '">')
    if date_iso: H.append('<meta property="article:published_time" content="' + date_iso + '">')
    H.append('<meta name="twitter:card" content="' + ('summary_large_image' if image else 'summary') + '">')
    H.append('<meta name="twitter:title" content="' + e(headline) + '"><meta name="twitter:description" content="' + e(desc) + '">')
    if image: H.append('<meta name="twitter:image" content="' + e(image) + '">')
    H.append('<script type="application/ld+json">' + ld + '</script>')
    H.append('<style>'
        ':root{color-scheme:dark}*{box-sizing:border-box}'
        'body{margin:0;background:#141414;color:#e7d3c0;font-family:Inter,-apple-system,BlinkMacSystemFont,system-ui,sans-serif;line-height:1.6}'
        'a{color:#e89a5a}.wrap{max-width:680px;margin:0 auto;padding:0 22px 80px}'
        'header{padding:18px 0;border-bottom:1px solid rgba(217,128,38,.22);text-align:center}'
        'header a{font:500 30px Georgia,serif;color:#e08a32;text-decoration:none;letter-spacing:1px}'
        '.label{font:600 12px Inter,sans-serif;color:#e08a32;letter-spacing:.5px;margin:26px 0 8px}'
        'h1{font-family:Georgia,"Times New Roman",serif;font-weight:600;color:#fff3e9;font-size:30px;line-height:1.2;margin:0 0 10px}'
        '.meta{font-size:13px;color:#a06a3a;margin-bottom:18px}'
        '.hero{display:block;width:100%;max-height:380px;object-fit:cover;border-radius:14px;margin:0 0 18px}'
        '.sum p{font-size:17px;color:#e7d3c0;margin:0 0 14px}'
        '.src{margin:22px 0;padding:14px 16px;border:1px solid rgba(217,128,38,.28);border-radius:12px;background:rgba(217,128,38,.06);font-size:14px}'
        '.src a{font-weight:600}'
        '.row{display:flex;gap:10px;align-items:center;margin:22px 0}'
        '.btn{cursor:pointer;border:1px solid rgba(217,128,38,.5);background:rgba(217,128,38,.10);color:#ffce9a;border-radius:10px;padding:11px 16px;font:600 14px Inter,sans-serif}'
        '.cta{margin-top:30px;padding:22px;border:1px solid rgba(217,128,38,.4);border-radius:16px;text-align:center;background:linear-gradient(180deg,rgba(40,28,18,.5),rgba(18,13,9,.9))}'
        '.cta h3{font-family:Georgia,serif;color:#ffd9c4;margin:0 0 6px}.cta p{color:#e7c4a6;font-size:14px;margin:0 0 14px}'
        '.go{display:inline-block;background:linear-gradient(135deg,#e89a4a,#c06a18);color:#1c1206;font-weight:700;text-decoration:none;padding:12px 22px;border-radius:11px}'
        '.foot{margin-top:30px;font-size:12px;color:#6a4a2a;text-align:center}'
        '#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#000;color:#ffe9d6;border:1px solid rgba(217,128,38,.5);border-radius:999px;padding:9px 16px;font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none}'
        '</style></head><body><div class="wrap">')
    H.append('<header><a href="' + SITE_URL + '/">quwwaa</a></header>')
    H.append('<div class="label">◈ QUWWAA NEWS LENS</div>')
    H.append('<h1>' + e(headline) + '</h1>')
    H.append('<div class="meta">' + e(source) + (' · ' + date_disp if date_disp else '') + '</div>')
    if image: H.append('<img class="hero" src="' + e(image) + '" alt="">')
    H.append('<div class="sum">' + paras + '</div>')
    H.append('<div class="src">QUWWAA\'s summary, drawn from reporting by ' + e(source) + '. '
             '<a href="' + e(src_url) + '" rel="nofollow noopener" target="_blank">Read the full story at ' + e(source) + ' &rarr;</a></div>')
    H.append('<div class="row"><button class="btn" id="shareBtn">Share</button></div>')
    H.append('<!-- Phase 2: reaction buttons / counts mount here -->')
    H.append('<div class="cta"><h3>The world, made simple.</h3><p>QUWWAA is your AI news butler — a personalized brief and assistant for the stories you care about.</p>'
             '<a class="go" href="' + SITE_URL + '/">See more on QUWWAA — create a free account</a></div>')
    H.append('<div class="foot"><a href="' + SITE_URL + '/">QUWWAA</a> · <a href="' + SITE_URL + '/privacy">Privacy</a> · <a href="' + SITE_URL + '/terms">Terms</a></div>')
    H.append('<div id="toast"></div>')
    H.append('<script>(function(){var u=' + json.dumps(canon) + ',t=' + json.dumps(headline) + ';'
             'function toast(m){var x=document.getElementById("toast");x.textContent=m;x.style.opacity="1";setTimeout(function(){x.style.opacity="0";},2200);}'
             'document.getElementById("shareBtn").onclick=function(){'
             'if(navigator.share){navigator.share({title:t,url:u}).catch(function(){});}'
             'else if(navigator.clipboard){navigator.clipboard.writeText(u).then(function(){toast("Link copied — create a free QUWWAA account to react & track stories");});}'
             'else{toast(u);}};})();</script>')
    H.append('</div></body></html>')
    return ''.join(H)

def build_sitemap():
    """Sitemap of the real, crawlable pages with a current lastmod. Home carries a
    trailing slash to match the canonical URL. Indexed story permalinks are appended."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    pages = [('/', 'daily', '1.0'), ('/privacy', 'monthly', '0.5'), ('/terms', 'monthly', '0.5')]
    rows = ''.join(
        '<url><loc>%s%s</loc><lastmod>%s</lastmod><changefreq>%s</changefreq><priority>%s</priority></url>'
        % (SITE_URL, ('/' if p == '/' else p), today, freq, prio)
        for p, freq, prio in pages)
    try:
        stories = sb_rest('GET', 'news_stories?is_indexed=eq.true&select=id,updated_at'
                                 '&order=updated_at.desc&limit=5000') or []
    except Exception:
        stories = []
    for st in stories:
        lm = str(st.get('updated_at') or '')[:10] or today
        rows += ('<url><loc>%s/story/%s</loc><lastmod>%s</lastmod><changefreq>weekly</changefreq>'
                 '<priority>0.6</priority></url>' % (SITE_URL, st['id'], lm))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + rows + '</urlset>')

# ============================================================================
# Money Trail — federal campaign-finance tracker (Phase 1). Public-domain data
# only: FEC openFEC API (money) + unitedstates/congress-legislators (roster) +
# unitedstates/images (CC0 portraits). ETL jobs write to Supabase via the
# service-role REST; the /money read endpoints only ever read from Supabase
# (never the FEC API on the request path). Amounts are stored in CENTS and
# returned as WHOLE DOLLARS (integers) in the API. Storage is bounded per §6:
# ~540 members + recent presidential candidates, pro-Israel + top-N PAC givers,
# IEs aggregated per spender, donors only for the ~7 tracked pro-Israel PACs.
# ============================================================================
LEGIS_CURRENT_URL = 'https://unitedstates.github.io/congress-legislators/legislators-current.json'
LEGIS_EXEC_URL    = 'https://unitedstates.github.io/congress-legislators/executive.json'
PHOTO_CDN         = 'https://cdn.jsdelivr.net/gh/unitedstates/images@gh-pages/congress/450x550/%s.jpg'
PHOTO_BUCKET      = 'politician-photos'
_HONORIFIC        = {'us_house': 'Rep. ', 'us_senate': 'Sen. ', 'president': ''}

def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def _http_json(url, timeout=30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
        return json.loads(r.read())

def _cents(dollars):
    try: return int(round(float(dollars) * 100))
    except Exception: return 0

def _party_letter(p):
    p = (p or '').strip().lower()
    if p.startswith('democr'): return 'D'
    if p.startswith('republic'): return 'R'
    if p.startswith('independ') or p.startswith('libertarian'): return 'I'
    return (p[:1].upper() or None)

def sb_upsert(table, rows, on_conflict):
    """Bulk idempotent upsert via PostgREST (service role). No-op on empty/error."""
    if not rows: return None
    try:
        return sb_rest('POST', '%s?on_conflict=%s' % (table, on_conflict), rows,
                       prefer='resolution=merge-duplicates,return=minimal')
    except Exception:
        return None

# ---------- FEC openFEC client (read-only; never on the request path) --------
def fec_get(path, params=None, timeout=30, retries=3):
    """One GET against the FEC API with the server's key. Polite exponential backoff
    on 429/5xx. Returns parsed dict, or None (missing key / persistent error)."""
    if not FEC_API_KEY: return None
    p = dict(params or {}); p['api_key'] = FEC_API_KEY
    url = FEC_API_BASE + path + '?' + urllib.parse.urlencode(p, doseq=True)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=timeout) as r:
                return json.loads(r.read() or b'null')
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep((2 ** attempt) * 3); continue
            return None
        except Exception:
            if attempt < retries - 1:
                time.sleep((2 ** attempt) * 2); continue
            return None
    return None

def fec_paginate(path, params=None, max_pages=6, per_page=100, timeout=30):
    """Collect results across a BOUNDED number of pages (§6 storage discipline)."""
    out = []; page = 1
    while page <= max_pages:
        p = dict(params or {}); p['per_page'] = per_page; p['page'] = page
        d = fec_get(path, p, timeout=timeout)
        if not isinstance(d, dict): break
        rows = d.get('results') or []
        out.extend(rows)
        pages = (d.get('pagination') or {}).get('pages') or page
        if page >= pages or not rows: break
        page += 1; time.sleep(0.4)      # pace under the hourly cap
    return out

# ---------- committee list bookkeeping --------------------------------------
def sync_money_committees():
    """Upsert the curated pro-Israel committee list into money_committees (idempotent).
    The config in PRO_ISRAEL_COMMITTEES is the source of truth; the DB self-heals to it."""
    rows = [{'fec_id': c['fec_id'], 'name': c['name'], 'committee_type': c.get('committee_type'),
             'connected_org': c.get('connected_org'), 'is_pro_israel': True,
             'tags': c.get('tags') or ['pro_israel'],
             'citation_url': 'https://www.fec.gov/data/committee/%s/' % c['fec_id']}
            for c in PRO_ISRAEL_COMMITTEES]
    sb_upsert('money_committees', rows, 'fec_id')

def _money_meta_touch(roster=False, fec=False):
    body = {'id': 1}
    if roster: body['last_roster_sync'] = _now_iso()
    if fec:    body['last_fec_sync'] = _now_iso()
    try: sb_rest('POST', 'money_meta?on_conflict=id', [body], prefer='resolution=merge-duplicates,return=minimal')
    except Exception: pass

def _money_roster_stale():
    try:
        if not (sb_rest('GET', 'money_politicians?select=id&limit=1') or []):
            return True
        rows = sb_rest('GET', 'money_meta?select=last_roster_sync&id=eq.1') or []
        ts = _parse_ts((rows or [{}])[0].get('last_roster_sync'))
        return (not ts) or (datetime.now(timezone.utc) - ts).days >= 6
    except Exception:
        return True

# ---------- Job A: roster sync (weekly) -------------------------------------
def money_roster_sync():
    """Upsert money_politicians from congress-legislators (House/Senate) + executive
    (President/VP). Carries id.bioguide + id.fec (the join key to FEC money)."""
    rows = []
    try: current = _http_json(LEGIS_CURRENT_URL)
    except Exception: current = []
    for p in current:
        terms = p.get('terms') or []
        if not terms: continue
        term = terms[-1]
        level = {'sen': 'us_senate', 'rep': 'us_house'}.get(term.get('type'))
        if not level: continue
        nm = p.get('name') or {}; ids = p.get('id') or {}
        full = (nm.get('official_full') or (nm.get('first', '') + ' ' + nm.get('last', ''))).strip()
        district = None if level == 'us_senate' else str(term.get('district') if term.get('district') not in (None, '') else '')
        rows.append({'bioguide_id': ids.get('bioguide'), 'full_name': full,
                     'first_name': nm.get('first'), 'last_name': nm.get('last'),
                     'party': _party_letter(term.get('party')), 'level': level,
                     'state': term.get('state'), 'district': district, 'in_office': True,
                     'fec_candidate_ids': ids.get('fec') or []})
    try: execs = _http_json(LEGIS_EXEC_URL)
    except Exception: execs = []
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    for p in execs:
        cur = [t for t in (p.get('terms') or [])
               if t.get('type') in ('prez', 'viceprez') and t.get('start', '') <= today <= t.get('end', '9999')]
        if not cur: continue
        nm = p.get('name') or {}; ids = p.get('id') or {}
        full = (nm.get('official_full') or (nm.get('first', '') + ' ' + nm.get('last', ''))).strip()
        rows.append({'bioguide_id': ids.get('bioguide') or ('exec-' + full.lower().replace(' ', '-')),
                     'full_name': full, 'first_name': nm.get('first'), 'last_name': nm.get('last'),
                     'party': _party_letter(cur[-1].get('party')), 'level': 'president',
                     'state': None, 'district': None, 'in_office': True,
                     'fec_candidate_ids': ids.get('fec') or []})
    sb_upsert('money_politicians', rows, 'bioguide_id')
    _money_meta_touch(roster=True)
    return len(rows)

# ---------- Job B: money sync (nightly) -------------------------------------
def _candidate_committees(cand_id, cycle):
    rows = fec_paginate('/candidate/%s/committees/' % cand_id, {'cycle': cycle}, max_pages=2)
    ids = []
    for r in rows:
        if (r.get('designation') or '') in ('P', 'A', ''):   # principal / authorized
            cid = r.get('committee_id')
            if cid: ids.append(cid)
    return list(dict.fromkeys(ids))

def _select_direct_rows(pol_id, direct):
    """Keep every pro-Israel PAC giver + the top-N general PAC givers, per cycle."""
    by_cycle = {}
    for e in direct.values():
        if e.get('donor_committee_fec_id'):
            by_cycle.setdefault(e['cycle'], []).append(e)
    out = []
    for cycle, items in by_cycle.items():
        items.sort(key=lambda x: x['amount_cents'], reverse=True)
        keep = [x for x in items if x['donor_committee_fec_id'] in PRO_ISRAEL_FEC_IDS]
        for x in items[:MONEY_TOP_N_PAC]:
            if x not in keep: keep.append(x)
        for x in keep:
            did = x['donor_committee_fec_id']
            out.append({'politician_id': pol_id, 'cycle': cycle, 'donor_committee_fec_id': did,
                        'donor_name': x['donor_name'], 'amount_cents': x['amount_cents'],
                        'bundled_cents': int(x.get('bundled_cents') or 0),
                        'is_pro_israel': did in PRO_ISRAEL_FEC_IDS,
                        'source_url': 'https://www.fec.gov/data/receipts/?data_type=processed'
                                      '&contributor_id=%s&two_year_transaction_period=%s' % (did, cycle)})
    return out

def money_sync():
    """Comprehensive per-politician money: direct PAC $ + earmarked/bundled (conduit) $
    (split via memo_code) + independent expenditures, plus top-N general PAC context.
    Resumable + rate-limit friendly. Verdict/rollup happens in _recompute_verdict."""
    if not FEC_API_KEY: return 0
    sync_money_committees()
    money_endorsement_sync()
    # Resumable: never-synced members first, and skip any synced within the window, so a
    # re-run (or an interrupted run) CONTINUES rather than restarting the multi-hour pass
    # over all ~540 members — the only way it converges on the free FEC tier (1,000/hr).
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(os.environ.get('MONEY_SYNC_SKIP_HOURS', '20')))
    pols = sb_rest('GET', 'money_politicians?select=id,fec_candidate_ids,last_money_sync'
                          '&fec_candidate_ids=neq.%7B%7D&order=last_money_sync.asc.nullsfirst&limit=2000') or []
    n = 0
    for pol in pols:
        cand_ids = pol.get('fec_candidate_ids') or []
        if not cand_ids: continue
        ts = _parse_ts(pol.get('last_money_sync'))
        if ts and ts > cutoff: continue                       # already fresh — resume past it
        direct, ies = {}, {}
        for cand_id in cand_ids:
            for cycle in MONEY_TRAIL_CYCLES:
                for cmte in _candidate_committees(cand_id, cycle):
                    # (1) broad committee (PAC) receipts -> GENERAL (non-tracked) top-N context.
                    #     Skip tracked committees (captured precisely below) and skip memo/earmark
                    #     lines (they double-count with the itemized entries).
                    for r in fec_paginate('/schedules/schedule_a/',
                            {'committee_id': cmte, 'two_year_transaction_period': cycle,
                             'is_individual': 'false', 'sort': '-contribution_receipt_amount'}, max_pages=3):
                        did = r.get('contributor_id') or ''
                        if did in PRO_ISRAEL_FEC_IDS or r.get('memo_code'): continue
                        amt = _cents(r.get('contribution_receipt_amount'))
                        if amt <= 0: continue
                        nm = r.get('contributor_name') or ''
                        e = direct.setdefault((cycle, did or nm.lower()),
                                {'cycle': cycle, 'donor_committee_fec_id': did, 'donor_name': nm,
                                 'amount_cents': 0, 'bundled_cents': 0})
                        e['amount_cents'] += amt
                    # (2) precise + COMPLETE capture of each tracked pro-Israel committee
                    #     (current cycle). AIPAC's reach is almost entirely EARMARKED money,
                    #     which the FEC records TWICE — as per-donor itemized memos AND as a
                    #     memoed subtotal. Count only ONE representation (subtotal preferred) to
                    #     avoid the double-count, and separate genuine treasury (non-memo) $.
                    if cycle == MONEY_CURRENT_CYCLE:
                        for T in PRO_ISRAEL_FEC_IDS:
                            treasury = itemized = subtotal = 0; tnm = ''
                            for r in fec_paginate('/schedules/schedule_a/',
                                    {'committee_id': cmte, 'contributor_id': T,
                                     'two_year_transaction_period': cycle,
                                     'sort': '-contribution_receipt_amount'}, max_pages=10):
                                amt = _cents(r.get('contribution_receipt_amount'))
                                if amt <= 0: continue
                                tnm = tnm or (r.get('contributor_name') or '')
                                if r.get('memoed_subtotal'):   subtotal += amt   # aggregate earmark memo
                                elif r.get('memo_code'):       itemized += amt   # per-donor earmark memo
                                else:                          treasury += amt   # direct treasury contribution
                            bundled = subtotal if subtotal > 0 else itemized     # de-dup: one representation
                            tot = treasury + bundled
                            if tot > 0:
                                e = direct.setdefault((cycle, T),
                                        {'cycle': cycle, 'donor_committee_fec_id': T, 'donor_name': tnm,
                                         'amount_cents': 0, 'bundled_cents': 0})
                                e['donor_name'] = tnm or e['donor_name']
                                e['amount_cents'] = tot          # authoritative (treasury + de-duped bundled)
                                e['bundled_cents'] = bundled
                for r in fec_paginate('/schedules/schedule_e/',
                        {'candidate_id': cand_id, 'cycle': cycle, 'sort': '-expenditure_amount'}, max_pages=3):
                    amt = _cents(r.get('expenditure_amount'))
                    if amt <= 0 or amt > 20000000000: continue        # skip bogus filings (>$200M single IE)
                    sp = r.get('committee_id') or ''
                    spn = (r.get('committee') or {}).get('name') or r.get('committee_name') or ''   # spender name is nested in schedule_e
                    so = 'support' if (r.get('support_oppose_indicator') == 'S') else 'oppose'
                    e = ies.setdefault((cycle, sp or spn.lower(), so),
                            {'cycle': cycle, 'spender_fec_id': sp, 'spender_name': spn,
                             'support_oppose': so, 'amount_cents': 0})
                    e['amount_cents'] += amt
        direct_rows = _select_direct_rows(pol['id'], direct)
        ie_rows = [{'politician_id': pol['id'], 'cycle': e['cycle'], 'spender_fec_id': e['spender_fec_id'],
                    'spender_name': e['spender_name'], 'support_oppose': e['support_oppose'],
                    'amount_cents': e['amount_cents'], 'is_pro_israel': e['spender_fec_id'] in PRO_ISRAEL_FEC_IDS,
                    'source_url': 'https://www.fec.gov/data/independent-expenditures/?data_type=processed'
                                  '&committee_id=%s&cycle=%s' % (e['spender_fec_id'], e['cycle'])}
                   for e in ies.values() if e.get('spender_fec_id')]
        if direct_rows: sb_upsert('money_direct_contributions', direct_rows, 'politician_id,cycle,donor_committee_fec_id')
        if ie_rows:     sb_upsert('money_independent_expenditures', ie_rows, 'politician_id,cycle,spender_fec_id,support_oppose')
        _recompute_verdict(pol['id'])
        n += 1; time.sleep(0.3)
        if n % 25 == 0: _money_recompute_ingest()      # live progress signal for the FE during a long pass
    _money_recompute_ingest()
    _money_meta_touch(fec=True)
    return n

def _recompute_verdict(pol_id):
    """Comprehensive pro-Israel rollup for the CURRENT cycle: direct PAC (treasury) +
    bundled/earmarked (conduit memo) + supporting IEs + matched individual $, plus the
    AIPAC endorsement flag. funded = total > 0 OR aipac_endorsed. Stores the breakdown."""
    cyc = MONEY_CURRENT_CYCLE
    d = sb_rest('GET', 'money_direct_contributions?select=amount_cents,bundled_cents,donor_committee_fec_id'
                       '&politician_id=eq.%s&cycle=eq.%s&is_pro_israel=is.true' % (pol_id, cyc)) or []
    ie = sb_rest('GET', 'money_independent_expenditures?select=amount_cents,spender_fec_id,support_oppose'
                        '&politician_id=eq.%s&cycle=eq.%s&is_pro_israel=is.true' % (pol_id, cyc)) or []
    prow = sb_rest('GET', 'money_politicians?select=aipac_endorsed&id=eq.%s&limit=1' % pol_id) or [{}]
    endorsed = bool((prow or [{}])[0].get('aipac_endorsed'))
    bundled = sum(int(x.get('bundled_cents') or 0) for x in d)
    direct_pac = sum(int(x.get('amount_cents') or 0) for x in d) - bundled     # non-memo treasury portion
    if direct_pac < 0: direct_pac = 0
    supp = sum(int(x.get('amount_cents') or 0) for x in ie if x.get('support_oppose') == 'support')
    individual = 0                                          # A4 individual-megadonor matching: reserved (stretch)
    total = direct_pac + bundled + supp + individual
    try:
        sb_rest('PATCH', 'money_politicians?id=eq.%s' % pol_id, {
            'pro_israel_funded': bool(total > 0 or endorsed), 'pro_israel_total_cents': total,
            'direct_pac_cents': direct_pac, 'bundled_cents': bundled,
            'individual_cents': individual, 'supporting_ie_cents': supp,
            'updated_at': _now_iso(), 'last_money_sync': _now_iso()}, prefer='return=minimal')
    except Exception: pass

# AIPAC publishes the candidates it endorses; import that list (free) as an aipac_endorsed
# flag independent of dollar amount. Config-driven (AIPAC_ENDORSED_BIOGUIDES) so it's
# curated from AIPAC's public list without a code change; best-effort and non-fabricating —
# no source => nothing is flagged (never a false positive).
AIPAC_ENDORSED_BIOGUIDES = [b.strip() for b in os.environ.get('AIPAC_ENDORSED_BIOGUIDES', '').split(',') if b.strip()]

def money_endorsement_sync():
    bios = list(AIPAC_ENDORSED_BIOGUIDES)
    if not bios: return 0
    sb_upsert('money_aipac_endorsements',
              [{'bioguide_id': b, 'cycle': MONEY_CURRENT_CYCLE, 'source_url': 'https://www.aipac.org/'} for b in bios],
              'bioguide_id')
    try:
        sb_rest('PATCH', 'money_politicians?bioguide_id=in.(%s)' % ','.join(urllib.parse.quote(b) for b in bios),
                {'aipac_endorsed': True}, prefer='return=minimal')
    except Exception: pass
    return len(bios)

def _money_recompute_ingest():
    """ingest_complete = every roster member has money computed (or has no FEC id to
    compute). The front-end keeps PoliTrack hidden from the public until this is true."""
    roster = sb_rest('GET', 'money_politicians?select=id&limit=2000') or []
    synced = sb_rest('GET', 'money_politicians?select=id&last_money_sync=not.is.null&limit=2000') or []
    nofec  = sb_rest('GET', 'money_politicians?select=id&fec_candidate_ids=eq.%7B%7D&limit=2000') or []
    roster_n = len(roster)
    processed_n = len(synced) + len(nofec)                  # disjoint: no-FEC members never get last_money_sync
    complete = roster_n > 0 and processed_n >= roster_n
    body = {'id': 1, 'processed_count': processed_n, 'roster_count': roster_n, 'ingest_complete': complete}
    if complete: body['last_full_sync'] = _now_iso()
    try: sb_rest('POST', 'money_meta?on_conflict=id', [body], prefer='resolution=merge-duplicates,return=minimal')
    except Exception: pass

# ---------- Job C: donor-chain sync (nightly, tracked PACs only) ------------
def money_donor_chain_sync():
    """Top donors INTO each tracked pro-Israel committee ('where the $ came from')."""
    if not FEC_API_KEY: return 0
    sync_money_committees()
    n = 0
    for c in PRO_ISRAEL_COMMITTEES:
        fid = c['fec_id']
        for cycle in MONEY_TRAIL_CYCLES:
            agg = {}
            for r in fec_paginate('/schedules/schedule_a/',
                    {'committee_id': fid, 'two_year_transaction_period': cycle,
                     'sort': '-contribution_receipt_amount'}, max_pages=3):
                nm = (r.get('contributor_name') or '').strip()
                amt = _cents(r.get('contribution_receipt_amount'))
                if not nm or amt <= 0: continue
                dtype = 'individual' if r.get('is_individual') else \
                        ('pac' if (r.get('contributor_id') or '').startswith('C') else 'organization')
                e = agg.setdefault(nm.lower(), {'donor_name': nm, 'donor_type': dtype,
                        'employer': r.get('contributor_employer') or None, 'amount_cents': 0})
                e['amount_cents'] += amt
            top = sorted(agg.values(), key=lambda x: x['amount_cents'], reverse=True)[:MONEY_DONOR_TOP_N]
            rows = [{'committee_fec_id': fid, 'cycle': cycle, 'donor_name': t['donor_name'],
                     'donor_type': t['donor_type'], 'employer': t['employer'],
                     'amount_cents': t['amount_cents'], 'tags': [],   # tags are SOURCED-only (§4)
                     'source_url': 'https://www.fec.gov/data/receipts/?data_type=processed'
                                   '&committee_id=%s&two_year_transaction_period=%s' % (fid, cycle)}
                    for t in top]
            if rows: sb_upsert('money_pac_donors', rows, 'committee_fec_id,cycle,donor_name')
            n += len(rows); time.sleep(0.3)
    _money_meta_touch(fec=True)
    return n

# ---------- Job D: photo mirror (CC0 portraits -> Supabase Storage) ---------
def _storage_put(bucket, path, data, content_type='image/jpeg'):
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY): return None
    req = urllib.request.Request(SUPABASE_URL + '/storage/v1/object/' + bucket + '/' + path,
            data=data, method='POST',
            headers={'Authorization': 'Bearer ' + SUPABASE_SERVICE_ROLE_KEY,
                     'Content-Type': content_type, 'x-upsert': 'true'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: r.read()
        return SUPABASE_URL + '/storage/v1/object/public/' + bucket + '/' + path
    except Exception:
        return None

def money_photo_mirror():
    """For each politician missing a photo: mirror the CC0 portrait into Storage and set
    photo_url. If Storage upload fails, fall back to the CC0 CDN URL; a true 404 -> leave
    null (UI shows initials)."""
    pols = sb_rest('GET', 'money_politicians?select=id,bioguide_id'
                          '&photo_url=is.null&bioguide_id=not.is.null&limit=1000') or []
    n = 0
    for p in pols:
        bio = p.get('bioguide_id') or ''
        if not bio or bio.startswith('exec-'): continue
        try:
            with urllib.request.urlopen(urllib.request.Request(PHOTO_CDN % bio, headers=HEADERS), timeout=20) as r:
                img = r.read()
        except Exception:
            continue                                  # no CC0 portrait -> leave null
        if not img: continue
        pub = _storage_put(PHOTO_BUCKET, bio + '.jpg', img) or (PHOTO_CDN % bio)
        try:
            sb_rest('PATCH', 'money_politicians?id=eq.%s' % p['id'],
                    {'photo_url': pub, 'updated_at': _now_iso()}, prefer='return=minimal')
            n += 1
        except Exception: pass
        time.sleep(0.05)
    return n

# ---------- background loop (weekly roster + nightly money/donor/photo) ------
def money_trail_loop():
    time.sleep(int(os.environ.get('MONEY_TRAIL_BOOT_DELAY', '90')))   # let the news prewarm finish first
    while True:
        try:
            sync_money_committees()
            if _money_roster_stale(): money_roster_sync()
            money_photo_mirror()
            if FEC_API_KEY:
                money_donor_chain_sync()   # fast + independent → donor chains populate first
                money_sync()               # long + rate-limited → resumable across runs
        except Exception:
            pass
        time.sleep(int(os.environ.get('MONEY_TRAIL_POLL_SEC', '86400')))   # nightly

# ---------- read models for the /money endpoints (§7) -----------------------
_MONEY_CACHE = {'t': 0, 'pols': []}
MONEY_LIST_TTL = 120
_money_hits = {}     # ip -> [recent /money read timestamps]

def money_rate_check(ip):
    now = time.time()
    with _ask_lock:
        _prune_ip_bucket(_money_hits, now)
        hits = [t for t in _money_hits.get(ip, []) if t > now - 60]
        if len(hits) >= MONEY_RATE_PER_MIN:
            _money_hits[ip] = hits; return False
        hits.append(now); _money_hits[ip] = hits; return True

def _money_all_politicians():
    """The whole federal set (~540 rows) cached briefly — search filters it in memory."""
    if _MONEY_CACHE['t'] and time.time() - _MONEY_CACHE['t'] < MONEY_LIST_TTL:
        return _MONEY_CACHE['pols']
    rows = sb_rest('GET', 'money_politicians?select=id,full_name,party,level,state,district,'
                          'photo_url,pro_israel_funded,pro_israel_total_cents&limit=2000') or []
    _MONEY_CACHE['pols'] = rows; _MONEY_CACHE['t'] = time.time()
    return rows

def _disp_name(row):
    return (_HONORIFIC.get(row.get('level'), '') + (row.get('full_name') or '')).strip()

def money_search(qs):
    q = (qs.get('q') or [''])[0].strip()
    state = (qs.get('state') or [''])[0].strip().upper()
    office = (qs.get('office') or [''])[0].strip().lower()
    party = (qs.get('party') or [''])[0].strip().upper()
    pio = (qs.get('pro_israel_only') or [''])[0].strip().lower() in ('1', 'true', 'yes', 'on')
    try: limit = max(1, min(100, int((qs.get('limit') or ['25'])[0])))
    except ValueError: limit = 25
    try: offset = max(0, int((qs.get('offset') or ['0'])[0]))
    except ValueError: offset = 0
    level = {'house': 'us_house', 'senate': 'us_senate', 'president': 'president'}.get(office)

    def keep(r):
        if level and r.get('level') != level: return False
        if state and (r.get('state') or '').upper() != state: return False
        if party and (r.get('party') or '').upper() != party: return False
        if pio and not r.get('pro_israel_funded'): return False
        if q:
            ql = q.lower(); qn = ql.replace(' ', '').replace('-', '')
            name = (r.get('full_name') or '').lower()
            st = (r.get('state') or '').lower(); di = (r.get('district') or '').lower()
            tokens = {st + di, st + '-' + di, st}
            if ql not in name and qn not in {t.replace('-', '') for t in tokens}:
                return False
        return True

    matched = [r for r in _money_all_politicians() if keep(r)]
    matched.sort(key=lambda r: (-(int(r.get('pro_israel_total_cents') or 0)), (r.get('full_name') or '')))
    page = matched[offset:offset + limit]
    return {'count': len(matched), 'results': [
        {'id': r.get('id'), 'name': _disp_name(r), 'party': r.get('party'), 'level': r.get('level'),
         'state': r.get('state'), 'district': r.get('district'), 'photo_url': r.get('photo_url'),
         'pro_israel_funded': bool(r.get('pro_israel_funded')),
         'pro_israel_total': int(r.get('pro_israel_total_cents') or 0) // 100} for r in page]}

def money_profile(pid):
    rows = sb_rest('GET', 'money_politicians?select=*&id=eq.%s&limit=1' % urllib.parse.quote(pid)) or []
    if not rows: return None
    p = rows[0]; cyc = MONEY_CURRENT_CYCLE
    dc = sb_rest('GET', 'money_direct_contributions?select=donor_name,donor_committee_fec_id,amount_cents,'
                        'is_pro_israel,source_url&politician_id=eq.%s&cycle=eq.%s&order=amount_cents.desc' % (p['id'], cyc)) or []
    ie = sb_rest('GET', 'money_independent_expenditures?select=spender_name,spender_fec_id,support_oppose,'
                        'amount_cents,is_pro_israel,source_url&politician_id=eq.%s&cycle=eq.%s&order=amount_cents.desc' % (p['id'], cyc)) or []
    direct_total = sum(int(x.get('amount_cents') or 0) for x in dc if x.get('is_pro_israel'))
    supp_total = sum(int(x.get('amount_cents') or 0) for x in ie if x.get('is_pro_israel') and x.get('support_oppose') == 'support')
    pi = set(x.get('donor_committee_fec_id') for x in dc if x.get('is_pro_israel'))
    pi |= set(x.get('spender_fec_id') for x in ie if x.get('is_pro_israel') and x.get('support_oppose') == 'support')
    pi.discard(None); pi.discard('')
    chains = []
    for fid in sorted(pi):
        crow = sb_rest('GET', 'money_committees?select=name&fec_id=eq.%s&limit=1' % fid) or []
        donors = sb_rest('GET', 'money_pac_donors?select=donor_name,donor_type,amount_cents,tags,source_url'
                                '&committee_fec_id=eq.%s&order=amount_cents.desc&limit=%d' % (fid, MONEY_DONOR_TOP_N)) or []
        chains.append({'committee': (crow or [{}])[0].get('name') or fid, 'committee_fec_id': fid,
            'top_donors': [{'donor_name': d.get('donor_name'), 'donor_type': d.get('donor_type'),
                            'amount': int(d.get('amount_cents') or 0) // 100, 'tags': d.get('tags') or [],
                            'source_url': d.get('source_url')} for d in donors]})
    return {'id': p['id'], 'name': _disp_name(p), 'party': p.get('party'), 'level': p.get('level'),
            'state': p.get('state'), 'district': p.get('district'), 'in_office': bool(p.get('in_office')),
            'photo_url': p.get('photo_url'), 'cycle': cyc,
            'verdict': {'pro_israel_funded': bool(p.get('pro_israel_funded')),
                        'direct_pac_total': int(p.get('direct_pac_cents') or 0) // 100,
                        'bundled_total': int(p.get('bundled_cents') or 0) // 100,
                        'supporting_ie_total': int(p.get('supporting_ie_cents') or 0) // 100,
                        'individual_total': int(p.get('individual_cents') or 0) // 100,
                        'aipac_endorsed': bool(p.get('aipac_endorsed')),
                        'pro_israel_total': int(p.get('pro_israel_total_cents') or 0) // 100,
                        'pro_israel_pac_count': len(pi)},
            'direct_contributions': [{'donor_name': x.get('donor_name'), 'amount': int(x.get('amount_cents') or 0) // 100,
                                      'is_pro_israel': bool(x.get('is_pro_israel')), 'source_url': x.get('source_url')} for x in dc],
            'independent_expenditures': [{'spender_name': x.get('spender_name'), 'support_oppose': x.get('support_oppose'),
                                          'amount': int(x.get('amount_cents') or 0) // 100, 'is_pro_israel': bool(x.get('is_pro_israel')),
                                          'source_url': x.get('source_url')} for x in ie],
            'donor_chains': chains, 'last_updated': p.get('updated_at')}

def money_config():
    rows = _money_all_politicians()
    meta = sb_rest('GET', 'money_meta?select=last_fec_sync,last_roster_sync,last_full_sync,'
                          'ingest_complete,processed_count,roster_count&id=eq.1') or []
    m = (meta or [{}])[0]
    return {'states': sorted(set(r.get('state') for r in rows if r.get('state'))),
            'offices': [{'value': 'house', 'label': 'U.S. House'},
                        {'value': 'senate', 'label': 'U.S. Senate'},
                        {'value': 'president', 'label': 'President / VP'}],
            'parties': sorted(set(r.get('party') for r in rows if r.get('party'))),
            'cycles': MONEY_TRAIL_CYCLES, 'current_cycle': MONEY_CURRENT_CYCLE,
            'ingest_complete': bool(m.get('ingest_complete')),
            'processed_count': int(m.get('processed_count') or 0),
            'roster_count': int(m.get('roster_count') or 0),
            'last_full_sync': m.get('last_full_sync'),
            'last_updated': m.get('last_fec_sync') or m.get('last_roster_sync')}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.dirname(os.path.abspath(__file__)), **kw)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'content-type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, content_type='text/plain; charset=utf-8', status=200, max_age=3600):
        body = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'public, max-age=%d' % max_age)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _client_ip(self):
        fwd = self.headers.get('X-Forwarded-For', '')
        return (fwd.split(',')[0].strip() if fwd else '') or self.client_address[0]

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'content-type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        if self.path.startswith('/ask'):
            return self._handle_ask()
        if self.path.startswith('/transcribe'):
            return self._handle_transcribe()
        if self.path.startswith('/speak'):
            return self._handle_speak()
        if self.path.startswith('/create-checkout-session'):
            return self._handle_checkout()
        if self.path.startswith('/stripe-webhook'):
            return self._handle_webhook()
        if self.path.startswith('/create-portal-session'):
            return self._handle_portal()
        if self.path.startswith('/priorities'):
            return self._handle_priorities()
        if self.path.startswith('/push/subscribe'):
            return self._handle_push(False)
        if self.path.startswith('/push/unsubscribe'):
            return self._handle_push(True)
        if self.path.startswith('/brief-subscribe'):
            return self._handle_brief_subscribe()
        if self.path.startswith('/brief-status'):
            return self._handle_brief_status()
        if self.path.startswith('/sync-account'):
            return self._handle_sync_account()
        if self.path.startswith('/sponsor-event'):
            return self._handle_sponsor_event()
        if self.path.startswith('/rumble-event'):
            return self._handle_rumble_event()
        if self.path.startswith('/story/share'):
            return self._handle_story_share()
        self.send_error(404)

    def _handle_sponsor_event(self):
        """In-app impression/click tracking for the sponsor strip (anonymous,
        aggregate-only — no personal data)."""
        d = self._read_json()
        sid = (d.get('id') or '').strip()
        kind = d.get('kind') if d.get('kind') in ('impression', 'click') else 'impression'
        surface = d.get('surface') if d.get('surface') in ('app', 'email') else 'app'
        if sid:
            try: sponsor_bump(sid, surface, kind)
            except Exception: pass
        self._send_json({'ok': True})

    def _handle_rumble_event(self):
        """Impression/click tracking for the Daily Rumble board card (aggregate-only)."""
        d = self._read_json()
        url = (d.get('url') or '').strip()
        kind = d.get('kind') if d.get('kind') in ('impression', 'click') else 'impression'
        if url.startswith('http'):
            try: rumble_bump(url, d.get('title') or '', kind)
            except Exception: pass
        self._send_json({'ok': True})

    def _handle_story_share(self):
        """Record a login-gated share once per (user, story). Sets up Phase-2 scoring
        (share = 3 pts, counted once/user) with no rework; bumps the share count on a
        genuinely new share. Requires a signed-in user — engagement needs an account."""
        u = auth_user(self)
        uid = u and u.get('id')
        if not uid:
            self._send_json({'error': 'login_required'}, 401); return
        d = self._read_json()
        sid = (d.get('story_id') or '').strip()
        if not sid:
            self._send_json({'error': 'missing_story'}, 400); return
        try:
            # Idempotent: unique index on (user_id, story_id, type) for non-comment types.
            # return=representation → a row comes back only on a genuinely new insert.
            rows = sb_rest('POST', 'story_engagement?on_conflict=user_id,story_id,type',
                           {'user_id': uid, 'story_id': sid, 'type': 'share'},
                           prefer='resolution=ignore-duplicates,return=representation')
            if rows:                                  # first share by this user → bump the counter
                try: sb_rest('POST', 'rpc/story_bump_share', {'p_story_id': sid})
                except Exception: pass
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'error': type(e).__name__}, 500)

    def _handle_sync_account(self):
        """Keep downstream services on the account's current login email — used
        after a confirmed email change so Stripe receipts and the Kit brief follow
        the new address. Idempotent (no-op when already in sync)."""
        u = auth_user(self)
        uid = (u or {}).get('id'); email = (u or {}).get('email')
        if not (uid and email):
            self._send_json({'error': 'unauthorized'}, 401); return
        try:
            prof = supabase_get_profile(uid, 'stripe_customer_id,brief_subscribed') or {}
        except Exception:
            prof = {}
        cust = prof.get('stripe_customer_id')
        if cust and STRIPE_SECRET_KEY:                       # Stripe receipts follow the new email
            try: stripe_post('customers/' + cust, [('email', email)])
            except Exception: pass
        if prof.get('brief_subscribed'):                     # the brief follows the new email
            try: kit_subscribe(email)
            except Exception: pass
        self._send_json({'ok': True})

    def _handle_brief_subscribe(self):
        """Add a signed-in (free or paid) member's email to the Kit daily brief and
        flag the profile. Idempotent — tapping it again is a harmless no-op."""
        u = auth_user(self)
        email = (u or {}).get('email'); uid = (u or {}).get('id')
        if not email:
            self._send_json({'error': 'unauthorized'}, 401); return
        try:
            kit_subscribe(email)
            if uid:
                try: supabase_patch_profile(('id', uid), {'brief_subscribed': True})
                except Exception: pass
            self._send_json({'ok': True, 'subscribed': True})
        except Exception as e:
            self._send_json({'error': type(e).__name__}, 500)

    def _handle_brief_status(self):
        """Reconcile brief-subscription state onto the profile: trust the flag; if
        unset, do a one-time Kit lookup by email and heal it. Keeps the Profile
        card honest for accounts created before the flag existed."""
        u = auth_user(self)
        uid = (u or {}).get('id'); email = (u or {}).get('email')
        if not uid:
            self._send_json({'error': 'unauthorized'}, 401); return
        sub = False
        try:
            prof = supabase_get_profile(uid, 'brief_subscribed') or {}
            sub = bool(prof.get('brief_subscribed'))
        except Exception:
            pass
        if not sub and email and kit_lookup(email):
            sub = True
            try: supabase_patch_profile(('id', uid), {'brief_subscribed': True})
            except Exception: pass
        self._send_json({'subscribed': sub})

    def _handle_push(self, unsub):
        """Store / update / remove a Web Push subscription. Open to everyone
        (the brief-ready alert is free); a signed-in user's id is attached when present."""
        d = self._read_json()
        sub = d.get('subscription') or {}
        endpoint = (sub.get('endpoint') or d.get('endpoint') or '').strip()
        if not endpoint:
            self._send_json({'error': 'missing'}, 400); return
        try:
            if unsub:
                sb_rest('DELETE', 'push_subscriptions?endpoint=eq.' + urllib.parse.quote(endpoint, safe=''))
                self._send_json({'ok': True}); return
            keys = sub.get('keys') or {}
            if not (keys.get('p256dh') and keys.get('auth')):
                self._send_json({'error': 'bad_keys'}, 400); return
            row = {'endpoint': endpoint, 'p256dh': keys['p256dh'], 'auth': keys['auth'],
                   'platform': (d.get('platform') or '')[:20],
                   'notify_brief': bool(d.get('notify_brief', True)),
                   'notify_priority': bool(d.get('notify_priority', True)),
                   'notify_breaking': bool(d.get('notify_breaking', False)),
                   'last_seen_at': datetime.now(timezone.utc).isoformat()}
            uid = user_id_from_request(self)
            if uid:
                row['user_id'] = uid
            # upsert on the unique endpoint; merge-duplicates keeps existing user_id if anon
            sb_rest('POST', 'push_subscriptions?on_conflict=endpoint', row,
                    prefer='resolution=merge-duplicates,return=minimal')
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'error': type(e).__name__}, 500)

    def _handle_priorities(self):
        """Create / change / delete / mark-seen the member's one tracked priority.
        Gold-only — gated on a verified trialing/active member."""
        uid = member_id_from_request(self)
        if not uid:
            self._send_json({'error': 'forbidden', 'member': False}, 403); return
        d = self._read_json()
        op = (d.get('op') or 'create').strip()
        try:
            if op == 'create':
                label = (d.get('label') or '').strip()[:120]
                query = (d.get('query') or label).strip()[:200]
                if not query:
                    self._send_json({'error': 'missing_query'}, 400); return
                existing = sb_rest('GET', 'priorities?user_id=eq.%s&active=eq.true&select=id' % uid) or []
                if len(existing) >= MAX_PRIORITIES:
                    self._send_json({'error': 'cap', 'max': MAX_PRIORITIES}, 409); return
                rows = sb_rest('POST', 'priorities',
                               {'user_id': uid, 'label': label or query, 'query': query, 'active': True})
                p = rows[0] if rows else None
                if p: poll_priority(p)                       # immediate first fetch
                self._send_json({'ok': True, 'priority': p})
            elif op in ('change', 'delete', 'seen'):
                pid = (d.get('id') or '').strip()
                if not _UUID_RE.match(pid):
                    self._send_json({'error': 'bad_id'}, 400); return
                own = sb_rest('GET', 'priorities?id=eq.%s&user_id=eq.%s&select=id' % (pid, uid)) or []
                if not own:
                    self._send_json({'error': 'not_found'}, 404); return
                if op == 'change':
                    label = (d.get('label') or '').strip()[:120]
                    query = (d.get('query') or label).strip()[:200]
                    if not query:
                        self._send_json({'error': 'missing_query'}, 400); return
                    sb_rest('DELETE', 'priority_items?priority_id=eq.%s' % pid)   # new subject — clear old items
                    rows = sb_rest('PATCH', 'priorities?id=eq.%s' % pid,
                                   {'label': label or query, 'query': query, 'last_checked_at': None})
                    p = rows[0] if rows else None
                    if p: poll_priority(p)
                    self._send_json({'ok': True, 'priority': p})
                elif op == 'delete':
                    sb_rest('DELETE', 'priorities?id=eq.%s' % pid)
                    self._send_json({'ok': True})
                else:   # seen
                    sb_rest('PATCH', 'priority_items?priority_id=eq.%s&seen=eq.false' % pid, {'seen': True})
                    self._send_json({'ok': True})
            else:
                self._send_json({'error': 'bad_op'}, 400)
        except Exception as e:
            self._send_json({'error': type(e).__name__}, 500)

    def _handle_portal(self):
        if not STRIPE_SECRET_KEY:
            self._send_json({'error': 'stripe_not_configured'}, 503); return
        auth = self.headers.get('Authorization', '')
        try:
            user = supabase_user_from_token(auth[7:].strip()) if auth.startswith('Bearer ') else None
            if not (user and user.get('id')):
                self._send_json({'error': 'unauthorized'}, 401); return
            prof = supabase_get_profile(user['id'], 'stripe_customer_id')
            cust = prof and prof.get('stripe_customer_id')
            if not cust:
                self._send_json({'error': 'no_customer'}, 400); return
            sess = stripe_post('billing_portal/sessions', [('customer', cust), ('return_url', SITE_URL + '/')])
            self._send_json({'url': sess.get('url')})
        except urllib.error.HTTPError as e:
            detail = ''
            try: detail = e.read().decode('utf-8', 'ignore')[:200]
            except Exception: pass
            self._send_json({'error': 'stripe_%d' % e.code, 'detail': detail}, 502)
        except Exception as e:
            self._send_json({'error': type(e).__name__}, 500)

    def _read_json(self):
        length = int(self.headers.get('Content-Length', '0') or '0')
        raw = self.rfile.read(length) if length else b'{}'
        try: return json.loads(raw or b'{}')
        except Exception: return {}

    def _handle_checkout(self):
        if not (STRIPE_SECRET_KEY and STRIPE_PRICE_ID):
            self._send_json({'error': 'stripe_not_configured'}, 503); return
        try:
            d = self._read_json()
            email = (d.get('email') or '').strip()
            user_id = (d.get('userId') or '').strip()
            # Persist the onboarding answers server-side (service role) so they land
            # even when the client has no session yet (email-confirmation flows).
            if user_id:
                prof = clean_profile(d.get('profile'))
                if prof:
                    prof['updated_at'] = datetime.now(timezone.utc).isoformat()
                    try: supabase_upsert_profile(user_id, prof)
                    except Exception: pass
            fields = [
                ('mode', 'subscription'),
                ('line_items[0][price]', STRIPE_PRICE_ID),
                ('line_items[0][quantity]', '1'),
                ('subscription_data[trial_period_days]', '7'),
                ('success_url', SITE_URL + '/?welcome=1'),
                ('cancel_url', SITE_URL + '/?canceled=1'),
                ('allow_promotion_codes', 'true'),
            ]
            if email:
                fields.append(('customer_email', email))
            if user_id:
                fields.append(('client_reference_id', user_id))
                fields.append(('subscription_data[metadata][supabase_user_id]', user_id))
            sess = stripe_post('checkout/sessions', fields)
            self._send_json({'url': sess.get('url')})
        except urllib.error.HTTPError as e:
            detail = ''
            try: detail = e.read().decode('utf-8', 'ignore')[:200]
            except Exception: pass
            self._send_json({'error': 'stripe_%d' % e.code, 'detail': detail}, 502)
        except Exception as e:
            self._send_json({'error': type(e).__name__}, 500)

    def _handle_webhook(self):
        length = int(self.headers.get('Content-Length', '0') or '0')
        payload = self.rfile.read(length) if length else b''
        if not stripe_verify(payload, self.headers.get('Stripe-Signature', '')):
            self._send_json({'error': 'bad_signature'}, 400); return
        try:
            event = json.loads(payload or b'{}')
        except Exception:
            self._send_json({'error': 'bad_json'}, 400); return
        typ = event.get('type', '')
        obj = (event.get('data') or {}).get('object') or {}
        try:
            self._apply_webhook(typ, obj)
            self._send_json({'received': True})
        except Exception as e:
            # non-2xx so Stripe retries (e.g. transient Supabase failure)
            self._send_json({'error': type(e).__name__}, 500)

    def _apply_webhook(self, typ, obj):
        now = datetime.now(timezone.utc).isoformat()
        def iso(ts):
            try: return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat() if ts else None
            except Exception: return None
        MAP = {'trialing': 'trialing', 'active': 'active', 'past_due': 'past_due',
               'canceled': 'canceled', 'unpaid': 'canceled', 'incomplete_expired': 'canceled'}
        if typ == 'checkout.session.completed':
            uid = obj.get('client_reference_id')
            # paid members are auto-subscribed to the brief → flag the profile too
            fields = {'subscription_status': 'trialing', 'brief_subscribed': True, 'updated_at': now}
            if obj.get('customer'): fields['stripe_customer_id'] = obj['customer']
            if obj.get('subscription'): fields['stripe_subscription_id'] = obj['subscription']
            if uid: supabase_patch_profile(('id', uid), fields)
            # auto-subscribe the verified paying email to the daily brief (no opt-in)
            kit_subscribe((obj.get('customer_details') or {}).get('email') or obj.get('customer_email'))
        elif typ in ('customer.subscription.created', 'customer.subscription.updated',
                     'customer.subscription.deleted'):
            raw = 'canceled' if typ.endswith('deleted') else obj.get('status', '')
            st = MAP.get(raw, raw if raw in ('trialing', 'active', 'past_due') else 'canceled')
            fields = {'subscription_status': st, 'stripe_subscription_id': obj.get('id'), 'updated_at': now}
            if obj.get('customer'): fields['stripe_customer_id'] = obj['customer']
            te = iso(obj.get('trial_end'))
            if te: fields['trial_ends_at'] = te
            uid = (obj.get('metadata') or {}).get('supabase_user_id')
            if uid:
                supabase_patch_profile(('id', uid), fields)
            elif obj.get('customer'):
                supabase_patch_profile(('stripe_customer_id', obj['customer']), fields)
            # newsletter follows membership: add on trial/active (covers reactivations
            # and trial->active too; Kit dedupes so repeats are harmless)
            if st in ('trialing', 'active') and obj.get('customer'):
                kit_subscribe(stripe_customer_email(obj['customer']))
        elif typ == 'invoice.payment_failed':
            if obj.get('customer'):
                supabase_patch_profile(('stripe_customer_id', obj['customer']),
                                       {'subscription_status': 'past_due', 'updated_at': now})

    def _handle_speak(self):
        if not OPENAI_API_KEY:
            self._send_json({'error': 'no_tts_key'}, 503); return
        if not speak_rate_check(self._client_ip()):
            self._send_json({'error': 'rate'}, 429); return
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
            raw = self.rfile.read(length) if length else b'{}'
            text = (json.loads(raw or b'{}').get('text') or '').strip()
            if not text:
                self._send_json({'error': 'empty'}, 400); return
            audio = openai_tts(text)
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)
        except urllib.error.HTTPError as e:
            detail = ''
            try: detail = e.read().decode('utf-8', 'ignore')[:160]
            except Exception: pass
            self._send_json({'error': 'tts_upstream_%d' % e.code, 'detail': detail}, 502)
        except Exception as e:
            self._send_json({'error': type(e).__name__}, 500)

    def _handle_transcribe(self):
        if not OPENAI_API_KEY:
            self._send_json({'error': 'no_stt_key', 'text': ''}, 503); return
        ok, why = rate_check(self._client_ip())
        if not ok:
            self._send_json({'error': why, 'text': ''}, 429); return
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
            if length <= 0 or length > 12_000_000:
                self._send_json({'error': 'bad_audio', 'text': ''}, 400); return
            audio = self.rfile.read(length)
            ctype = (self.headers.get('X-Audio-Type') or 'audio/webm').split(';')[0].strip()
            ext = AUDIO_EXT.get(ctype, 'webm')
            text = whisper_transcribe(audio, 'speech.' + ext, ctype)
            self._send_json({'text': text})
        except urllib.error.HTTPError as e:
            detail = ''
            try: detail = e.read().decode('utf-8', 'ignore')[:160]
            except Exception: pass
            self._send_json({'error': 'stt_upstream_%d' % e.code, 'text': '', 'detail': detail}, 502)
        except Exception as e:
            self._send_json({'error': type(e).__name__, 'text': ''}, 500)

    def _handle_ask(self):
        if not ANTHROPIC_API_KEY:
            self._send_json({'error': 'no_server_key'}, 503); return
        member = member_from_request(self)         # verified via Supabase; None for free users
        if not member:                             # members bypass the per-IP / daily rate cap
            ok, why = rate_check(self._client_ip())
            if not ok:
                msg = ("My apologies - the day's allowance of inquiries has been reached. Do return tomorrow."
                       if why == 'daily_cap' else
                       "A moment's patience - you're speaking faster than I can attend. Try again shortly.")
                self._send_json({'error': why, 'reply': msg}, 429); return
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
            raw = self.rfile.read(length) if length else b'{}'
            data = json.loads(raw or b'{}')
            msgs = []
            for m in (data.get('messages') or [])[-20:]:
                role = m.get('role')
                content = m.get('content')
                if role in ('user', 'assistant') and isinstance(content, str) and content.strip():
                    msgs.append({'role': role, 'content': content[:4000]})
            if not msgs:
                self._send_json({'error': 'empty', 'reply': 'I received an empty transmission.'}, 400); return
            extra = ''
            lang = norm_lang(data.get('lang') or 'en')
            if lang != 'en':
                pl = LANGS.get(lang, {}).get('plang', 'the user\'s language')
                extra += (" LANGUAGE: The user's language is %s. You MUST reply ENTIRELY in %s — every sentence, "
                          "including greetings and your butler courtesies; address the user politely in %s (not 'sir'). "
                          "Never switch to English. Keep the final [LENS: keywords] tag in that exact bracket format; "
                          "the keywords may be written in %s." % (pl, pl, pl, pl))
            ct = (data.get('clientTime') or '').strip()[:80]
            tz = (data.get('tz') or '').strip()[:60]
            if ct:
                extra += (" CURRENT LOCAL TIME for the user: %s%s. Use this for any greeting or time reference - "
                          "choose good morning/afternoon/evening from it. Never assume the time of day from anything else."
                          % (ct, (' (%s)' % tz) if tz else ''))
            if member:
                name = member.get('display_name') or ''
                style = member.get('address_style') or 'name'
                ints = ', '.join(member.get('interests') or [])
                # Honor an explicit sir/madam choice; otherwise use the name, else stay neutral.
                addr = name if (style == 'name' and name) else ('madam' if style == 'madam' else ('sir' if style == 'sir' else ''))
                if addr:
                    extra += (" MEMBER CONTEXT: You are speaking with a QUWWAA member. Address them as '%s'." % addr)
                else:
                    extra += (" MEMBER CONTEXT: You are speaking with a QUWWAA member. No honorific is set — "
                              "address them warmly and neutrally (no 'sir'/'ma'am').")
                if ints:
                    extra += (" They follow these interests: %s. Let these WEIGHT your suggestions, but never narrow what "
                              "you will discuss - help with any topic they raise." % ints)
            # --- Retrieval grounding (+ gap-filling chit-chat). For a news question we must
            # read the live coverage before answering, which adds a multi-second pause. On
            # the streaming path we first stream a short, warm, varied acknowledgment to
            # fill that gap, THEN retrieve, THEN stream the grounded brief (and tell the
            # model to skip its own ack since the filler covered it). English live path only.
            q = next((m['content'] for m in reversed(msgs) if m['role'] == 'user'), '')
            is_news = bool(lang == 'en' and q and _looks_like_news(q))

            def add_grounding(base, skip_ack):
                """Retrieve live coverage for q and append grounding rules. `skip_ack` when a
                filler line was already streamed (so the model shouldn't re-acknowledge)."""
                if not is_news:
                    return base
                block, srcs = butler_live_news(q)
                if not block:
                    return base + (
                        "\n\nNO LIVE COVERAGE was retrieved for this query just now. If the user is asking about a "
                        "current event, tell them you don't see reporting on it yet and offer to pull the News Lens — "
                        "do NOT invent details, and do NOT deny it from memory. " +
                        ("" if skip_ack else "Acknowledge briefly first. ") + "End with the [LENS: keywords] tag.")
                ack_rule = ("The interface has ALREADY greeted the user and told them you're pulling up the coverage, so "
                            "do NOT open with a greeting, 'let me pull that up', or any acknowledgment — begin directly "
                            "with the substance."
                            if skip_ack else
                            "Open with a brief, warm, VARIED acknowledgment (never a fixed phrase).")
                return base + (
                    "\n\n" + block +
                    "\n\nGROUNDING — you have a training cutoff but are augmented with the LIVE NEWS above, retrieved in "
                    "real time. For anything recent, rely ONLY on the provided articles, not your memory. NEVER tell the "
                    "user you have no knowledge of, cannot verify, or are unsure an event happened when it appears in the "
                    "provided articles — summarize it. Attribute claims to their source (e.g. 'According to AP…', "
                    "'Reuters reports…'). If coverage is thin or sources conflict, say what is confirmed versus still "
                    "unclear, but do not deny the event. Do NOT mention your training cutoff or 'making assumptions' when "
                    "articles are present. Only assert facts present in these articles. " + ack_rule + " Give the grounded "
                    "spoken brief, then name the sources you drew on (e.g. 'Sources: %s' — the on-screen News Lens carries "
                    "the clickable links). End with the [LENS: keywords] tag." % ', '.join(srcs))

            if data.get('stream'):                       # stream tokens for a snappy butler
                # HTTP/1.1 chunked so the proxy forwards each token immediately
                # (a plain HTTP/1.0 body gets buffered until close → no streaming).
                self.protocol_version = 'HTTP/1.1'
                self.close_connection = True
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Accel-Buffering', 'no')
                self.send_header('Transfer-Encoding', 'chunked')
                self.send_header('Connection', 'close')
                self.end_headers()
                wrote = [0]
                def _w(t):
                    if not t: return
                    b = t.encode('utf-8')
                    self.wfile.write(b'%X\r\n' % len(b) + b + b'\r\n'); self.wfile.flush()
                    wrote[0] += len(t)
                # Speak the gap-filling line FIRST (instant), THEN do the slow retrieval.
                if is_news:
                    try: _w(_butler_filler(member) + '\n\n')
                    except Exception: pass
                extra2 = add_grounding(extra, skip_ack=is_news)
                try:
                    full = anthropic_stream(msgs, extra2, _w)
                    if not full.strip() and wrote[0] == 0:
                        _w('I received an empty transmission.')
                except Exception:
                    if wrote[0] == 0:
                        # streaming failed before any token → serve the full reply non-streamed
                        try: _w(anthropic_chat(msgs, extra2) or 'I received an empty transmission.')
                        except Exception: _w('I encountered a fault processing that.')
                try: self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()   # terminating chunk
                except Exception: pass
                return
            # Non-stream (BYO-key / ElevenLabs / no-stream): one reply, model does its own ack.
            extra2 = add_grounding(extra, skip_ack=False)
            reply = anthropic_chat(msgs, extra2) or 'I received an empty transmission.'
            self._send_json({'reply': reply})
        except urllib.error.HTTPError as e:
            detail = ''
            try: detail = e.read().decode('utf-8', 'ignore')[:160]
            except Exception: pass
            self._send_json({'error': 'upstream_%d' % e.code,
                             'reply': 'The uplink returned an error (%d). %s' % (e.code, detail)}, 502)
        except Exception as e:
            self._send_json({'error': type(e).__name__,
                             'reply': 'I encountered a fault processing that.'}, 500)

    def do_GET(self):
        if self.path in ('/health', '/healthz'):
            self._send_json({'ok': True, 'service': 'quwwaa', 'brain': bool(ANTHROPIC_API_KEY),
                             'stt': bool(OPENAI_API_KEY), 'tts': bool(OPENAI_API_KEY),
                             'premium': PREMIUM_ENABLED})
        elif self.path == '/robots.txt':
            self._send_text('User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n' % SITE_URL)
        elif self.path == '/sitemap.xml':
            self._send_text(build_sitemap(), content_type='application/xml; charset=utf-8')
        elif self.path.startswith('/story/'):
            # Public, server-rendered story permalink (no paywall). Look up by id only;
            # redirect to the canonical id+slug path if the request's slug differs.
            base = urllib.parse.urlparse(self.path).path
            parts = [p for p in base.split('/') if p]      # ['story', '<id>', '<slug?>']
            sid = parts[1] if len(parts) >= 2 else ''
            story = get_story(sid) if sid else None
            if not story:
                self.send_error(404); return
            canonical = '/story/' + sid                    # canonical is slug-less; any slug 301s here
            if base.rstrip('/') != canonical:
                self.send_response(301); self.send_header('Location', canonical); self.end_headers(); return
            self._send_text(render_story_page(story), content_type='text/html; charset=utf-8', max_age=300)
        elif self.path.startswith('/config'):
            # public config only — the page boots Supabase + Stripe.js from these
            self._send_json({'supabaseUrl': SUPABASE_URL, 'supabaseAnonKey': SUPABASE_ANON_KEY,
                             'stripePublishableKey': STRIPE_PUBLISHABLE_KEY, 'priceId': STRIPE_PRICE_ID,
                             'premium': PREMIUM_ENABLED, 'vapidPublicKey': VAPID_PUBLIC_KEY,
                             'gaMeasurementId': GA_MEASUREMENT_ID, 'googleClientId': GOOGLE_CLIENT_ID,
                             'freePerDay': FREE_PER_DAY, 'freePerMonth': FREE_PER_MONTH})
        elif self.path.startswith('/home'):
            self._send_json({'items': HOME_SNAPSHOT['items'], 't': HOME_SNAPSHOT['t']})
        elif self.path.startswith('/brief'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            lang = norm_lang((qs.get('lang') or ['en'])[0])
            store = _brief_store(lang)
            if lang != 'en':
                ensure_brief_lang(lang)          # lazy background build; fills on subsequent fetches
            self._send_json({'sections': store['sections'], 'extra': store.get('extra') or [],
                             't': store['t'], 'rumble': RUMBLE['items'], 'lang': lang,
                             'building': (lang != 'en' and lang in BRIEF_BUILDING)})
        elif self.path.startswith('/sponsors'):
            try: items = get_sponsors()
            except Exception: items = []
            self._send_json({'sponsors': items})
        elif self.path.startswith('/sponsor-click'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sid = (qs.get('id') or [''])[0].strip()
            surface = (qs.get('surface') or ['email'])[0]
            surface = surface if surface in ('app', 'email') else 'email'
            dest = sponsor_link(sid) if sid else ''
            if sid:
                try: sponsor_bump(sid, surface, 'click')
                except Exception: pass
            self.send_response(302)
            self.send_header('Location', dest or (SITE_URL + '/'))   # server-resolved link — never an open redirect
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
        elif self.path.startswith('/sponsor-pixel'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            sid = (qs.get('id') or [''])[0].strip()
            surface = (qs.get('surface') or ['email'])[0]
            surface = surface if surface in ('app', 'email') else 'email'
            if sid:
                try: sponsor_bump(sid, surface, 'impression')
                except Exception: pass
            gif = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00'
                   b'\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')
            self.send_response(200)
            self.send_header('Content-Type', 'image/gif')
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('Content-Length', str(len(gif)))
            self.end_headers()
            self.wfile.write(gif)
        elif self.path.startswith('/news'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q = (qs.get('q') or [''])[0].strip()
            try:
                days = max(1, min(365, int((qs.get('days') or ['7'])[0])))
            except ValueError:
                days = 7
            fast = (qs.get('fast') or ['0'])[0] == '1'
            lang = norm_lang((qs.get('lang') or ['en'])[0])
            try:
                payload = cached_aggregate(q, days, fast, lang) if q else {'query': '', 'articles': [], 'sources': 0}
            except Exception as e:
                payload = {'query': q, 'articles': [], 'sources': 0, 'error': str(e)}
            self._send_json(payload)
        elif self.path.startswith('/article'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            url = (qs.get('url') or [''])[0].strip()
            title = (qs.get('title') or [''])[0].strip()
            source = (qs.get('source') or [''])[0].strip()
            day = (qs.get('day') or [''])[0].strip()       # client's LOCAL date/month for the meter
            month = (qs.get('month') or [''])[0].strip()
            lang = norm_lang((qs.get('lang') or ['en'])[0])  # summarize + relate in the reader's language
            image = (qs.get('image') or [''])[0].strip()     # card thumbnail → story OG image
            if not (url or title):
                self._send_json({'error': 'missing'}, 400); return
            # ONE server-authoritative gate for every article open:
            #   Gold -> unlimited; free registered -> 1/day AND 5/month;
            #   anonymous -> 1 free taste (per IP), then the registration wall.
            uid = None; status = None
            u = auth_user(self)
            if u and u.get('id'):
                uid = u['id']
                try: status = (supabase_get_status(uid) or {}).get('subscription_status')
                except Exception: status = None
                allowed, reason, info = free_meter(uid, status, url, day, month)
                if not allowed:
                    self._send_json({'locked': True, 'reason': reason, 'meter': info}); return
            else:
                allowed, reason = anon_meter(self._client_ip(), url, day)
                if not allowed:
                    self._send_json({'locked': True, 'reason': reason, 'meter': {'tier': 'anon'}}); return
                info = {'tier': 'anon'}
            if not article_rate_check(self._client_ip()):
                self._send_json({'error': 'rate'}, 429); return
            try:
                payload = build_article(url, title, source, lang=lang, image=image)
                payload['meter'] = info
                self._send_json(payload)
            except Exception as e:
                self._send_json({'error': str(e)}, 500)
        elif self.path.startswith('/priorities'):
            uid = member_id_from_request(self)
            if not uid:
                self._send_json({'error': 'forbidden', 'member': False}, 403); return
            try:
                self._send_json({'member': True, 'max': MAX_PRIORITIES, 'priorities': get_priorities(uid)})
            except Exception as e:
                self._send_json({'error': str(e)}, 500)
        elif self.path.startswith('/admin/brief-email'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = (qs.get('token') or [''])[0]
            if not (BRIEF_EMAIL_TOKEN and token == BRIEF_EMAIL_TOKEN):
                self._send_json({'error': 'forbidden'}, 403); return
            slot = (qs.get('slot') or ['morning'])[0]
            if slot not in ('morning', 'evening'): slot = 'morning'
            if (qs.get('preview') or ['0'])[0] == '1':      # view the composed HTML in a browser
                exclude = set(BRIEF_EMAIL.get('sent_urls') or ()) if slot == 'evening' else None
                subject, doc, _urls = compose_brief_email(slot, exclude_urls=exclude)
                body = ('<!-- subject: ' + subject + ' -->\n' + doc).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            self._send_json(run_brief_email(slot, force=True))   # force a real compose+create (test)
        elif self.path.startswith('/money/search'):
            if not money_rate_check(self._client_ip()):
                self._send_json({'error': 'rate'}, 429); return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try: self._send_json(money_search(qs))
            except Exception as e: self._send_json({'count': 0, 'results': [], 'error': str(e)}, 500)
        elif self.path.startswith('/money/politician/'):
            if not money_rate_check(self._client_ip()):
                self._send_json({'error': 'rate'}, 429); return
            pid = urllib.parse.urlparse(self.path).path.split('/money/politician/', 1)[1].strip('/')
            try:
                prof = money_profile(pid)
                if prof is None: self._send_json({'error': 'not_found'}, 404); return
                self._send_json(prof)
            except Exception as e: self._send_json({'error': str(e)}, 500)
        elif self.path.startswith('/money/config'):
            try: self._send_json(money_config())
            except Exception as e: self._send_json({'error': str(e)}, 500)
        elif self.path.startswith('/admin/money-run'):
            # On-demand ETL trigger (so a run doesn't have to wait for the nightly loop).
            # Token-gated with the existing admin secret; runs in the background and returns
            # immediately (a full money pass is long + rate-limited). job=all|money|donor|roster|photo
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = (qs.get('token') or [''])[0]
            if not (BRIEF_EMAIL_TOKEN and token == BRIEF_EMAIL_TOKEN):
                self._send_json({'error': 'forbidden'}, 403); return
            job = (qs.get('job') or ['all'])[0]
            if job not in ('all', 'money', 'donor', 'roster', 'photo'):
                self._send_json({'error': 'bad_job'}, 400); return
            def _run(j=job):
                try:
                    sync_money_committees()
                    if j in ('all', 'roster'): money_roster_sync()
                    if j in ('all', 'photo'):  money_photo_mirror()
                    if j in ('all', 'donor'):  money_donor_chain_sync()
                    if j in ('all', 'money'):  money_sync()
                except Exception: pass
            import threading as _t; _t.Thread(target=_run, daemon=True).start()
            self._send_json({'ok': True, 'started': job, 'fec_key': bool(FEC_API_KEY)})
        else:
            base = urllib.parse.urlparse(self.path).path
            if base in ('/', ''):
                self.path = '/quwwaa-console.html'
            elif base in ('/terms', '/terms/'):
                self.path = '/terms.html'
            elif base in ('/privacy', '/privacy/'):
                self.path = '/privacy.html'
            super().do_GET()

    def list_directory(self, path):
        # never expose a raw directory listing — bounce to the console
        self.send_response(302)
        self.send_header('Location', '/')
        self.end_headers()
        return None

    def log_message(self, *a):
        pass

if __name__ == '__main__':
    import threading
    threading.Thread(target=prewarm, daemon=True).start()
    threading.Thread(target=priorities_loop, daemon=True).start()
    threading.Thread(target=brief_email_loop, daemon=True).start()
    threading.Thread(target=story_index_loop, daemon=True).start()
    threading.Thread(target=money_trail_loop, daemon=True).start()
    print('QUWWAA server on http://%s:%d (news lens active, prewarming home)' % (HOST, PORT))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
