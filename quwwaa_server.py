#!/usr/bin/env python3
"""QUWWAA local server — serves the console AND aggregates world news on demand.
Sources: GDELT (tens of thousands of global outlets, keyless), Google News RSS,
and Reddit. Results are fetched live, held in memory only, and never written
to disk or any server."""
import json, os, re, email.utils, html, base64, time
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
ASK_MODEL = os.environ.get('ASK_MODEL', 'claude-haiku-4-5-20251001')
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
BRIEF_SEND_HOUR = int(os.environ.get('BRIEF_SEND_HOUR', '6'))                       # America/Phoenix clock hour
BRIEF_COMPOSE_HOUR = int(os.environ.get('BRIEF_COMPOSE_HOUR', str(max(0, BRIEF_SEND_HOUR - 1))))
BRIEF_EMAIL_TOKEN = os.environ.get('BRIEF_EMAIL_TOKEN', '')                         # gates the manual /admin trigger
ADMIN_USER_ID = os.environ.get('ADMIN_USER_ID', '')                                # optional: who to ping for draft review
PHOENIX_TZ = timezone(timedelta(hours=-7))                                          # MST, no DST
GA_MEASUREMENT_ID = os.environ.get('GA_MEASUREMENT_ID', '')                         # GA4 (exposed via /config)
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')                           # Google One Tap (web client id; exposed via /config)
# Jarvis cockpit (admin-only command center) — see JARVIS_COCKPIT_PHASE1.md.
JARVIS_ADMIN_USER_IDS = set(x.strip() for x in os.environ.get('JARVIS_ADMIN_USER_IDS', '').split(',') if x.strip())
JARVIS_ADMIN_TOKEN = os.environ.get('JARVIS_ADMIN_TOKEN', '')                       # optional shared-secret fallback
GA_SERVICE_ACCOUNT_JSON = os.environ.get('GA_SERVICE_ACCOUNT_JSON', '')             # GA4 Data API service account (Traffic card; stubbed until set)
GA_PROPERTY_ID = os.environ.get('GA_PROPERTY_ID', '542314942')
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

def ago(dt):
    if not dt: return ''
    s = (datetime.now(timezone.utc) - dt).total_seconds()
    if s < 0: s = 0
    if s < 3600:  return '%dm ago' % max(1, s // 60)
    if s < 86400: return '%dh ago' % (s // 3600)
    return '%dd ago' % (s // 86400)

def _gdelt_call(q, days):
    url = 'https://api.gdeltproject.org/api/v2/doc/doc?' + urllib.parse.urlencode({
        'query': q + ' sourcelang:english', 'mode': 'artlist', 'maxrecords': '40',
        'format': 'json', 'timespan': '%dd' % days, 'sort': 'datedesc'})
    body = fetch(url, timeout=7) or b'{}'
    try:
        return json.loads(body).get('articles', [])
    except ValueError:
        # GDELT returns plain-text errors (rate limits etc.) — treat as empty
        return []

def src_gdelt(q, days=7):
    try:
        arts = _gdelt_call(q, days)
    except Exception:
        arts = []  # GDELT flaky/rate-limited — skip rather than stall the whole board
    words = q.split()
    if len(arts) < 8 and len(words) > 3:
        # query too strict — relax to the strongest three terms
        arts += _gdelt_call(' '.join(words[:3]), days)
    out = []
    for a in arts:
        dt = None
        try:
            dt = datetime.strptime(a.get('seendate', ''), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        except Exception:
            pass
        out.append({'title': a.get('title', ''), 'url': a.get('url', ''),
                    'source': a.get('domain', ''), 'time': ago(dt),
                    'image': a.get('socialimage', '') or '',
                    'ts': dt.timestamp() if dt else 0, 'via': 'GDELT'})
    return out

def src_gnews(q, days=7):
    url = 'https://news.google.com/rss/search?' + urllib.parse.urlencode(
        {'q': '%s when:%dd' % (q, days), 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})
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
        out.append({'title': title, 'url': link, 'source': sname, 'time': ago(dt),
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
                    'image': img, 'ts': dt.timestamp() if dt else 0, 'via': 'RSS'})
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
                    'image': img, 'ts': dt.timestamp() if dt else 0, 'via': 'Yahoo'})
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
                    'image': hd_bing(img), 'ts': dt.timestamp() if dt else 0, 'via': 'Bing'})
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

def aggregate(q, days=7, fast=False):
    results, seen, arts = [], set(), []
    diag = {}
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    sources = ((src_bing, 'BING'), (src_gdelt, 'GDELT'), (src_yahoo, 'YAHOO')) if fast else \
              ((src_rsspack, 'FEEDS'), (src_gdelt, 'GDELT'), (src_bing, 'BING'),
               (src_yahoo, 'YAHOO'), (src_bsky, 'BLUESKY'),
               (src_gnews, 'GNEWS'), (src_reddit, 'REDDIT'))
    # Return with whatever has arrived by the deadline rather than waiting on the
    # slowest source — a single stalled feed must never hold up the whole board.
    deadline = 4.0 if fast else 6.5
    ex = ThreadPoolExecutor(max_workers=len(sources))
    futs = {ex.submit(f, q, days): n for f, n in sources}
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
        key = re.sub(r'[^a-z0-9]+', '', (a['title'] or '').lower())[:60]
        if not key or key in seen:
            continue
        seen.add(key)
        arts.append(a)
    arts.sort(key=lambda a: a['ts'], reverse=True)
    arts = arts[:60]
    if not fast:  # fast mode skips the slow enrichment passes
        fix_gnews_urls(arts)
        upgrade_gnews(arts)          # may REWRITE a['url'] from a Bing match…
        fill_images(arts)
        arts = [a for a in arts if _is_article_url(a.get('url', ''))]   # …so re-validate after enrichment
    return {'query': q, 'days': days, 'articles': arts, 'diag': diag,
            'sources': len({a['source'] for a in arts})}

CACHE = {}
CACHE_TTL = 600  # seconds; in memory only, gone on restart

def cached_aggregate(q, days=7, fast=False):
    key = (q.lower().strip(), days, fast)
    hit = CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    payload = aggregate(q, days, fast)
    if payload.get('articles'):
        CACHE[key] = (time.time(), payload)
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

def _pick_card(cat, fast=True):
    payload = cached_aggregate(cat['q'], cat['days'], fast)
    arts = payload.get('articles', [])
    mah = cat.get('maxAgeH')
    if mah:
        cut = time.time() - mah * 3600
        fresh = [a for a in arts if (a.get('ts') or 0) >= cut]
        if fresh:
            arts = fresh
    a = next((x for x in arts if x.get('image')), arts[0] if arts else None)
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
    {'label': 'Culture & Entertainment', 'q': 'entertainment celebrity', 'days': 2},
    {'label': 'Science',            'q': 'science discovery',       'days': 3},
    {'label': 'Nature & Disasters', 'q': 'natural disaster',        'days': 3},
    {'label': 'Crime & Justice',    'q': 'crime police court',      'days': 2},
    {'label': 'Worth Knowing',      'q': 'breaking news',           'days': 1, 'maxAgeH': 20},
]
BRIEF = {'t': 0, 'sections': []}
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

def related_articles(title, source, exclude_url, n=4):
    """3-4 related stories on the same topic from OTHER outlets (reuses /news)."""
    try:
        payload = cached_aggregate(key_terms(title), 7, True)
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

def build_article(url, title, source):
    """Grounded summary + related coverage for one article, cached by URL so
    re-opening the same story never re-spends AI credits."""
    hit = ARTICLE_CACHE.get(url)
    if hit and time.time() - hit[0] < ARTICLE_TTL:
        return hit[1]
    desc = og_desc(url) if url.startswith('http') else ''
    body = fetch_article_text(url, timeout=7) if url and url.startswith('http') else ''
    # tier-2 neighbours (with bodies) when the article body is thin/unfetchable
    neighbors = _neighbors_with_bodies(title, source, url, budget=9) if (not body or len(body) < 400) else []
    summary, degraded = make_summary(title, source, '', url, desc, longer=True, body=body, neighbors=neighbors)
    payload = {'url': url, 'title': title, 'source': source, 'summary': summary,
               'grounded': (not degraded), 'related': related_articles(title, source, url)}
    if summary and url:
        if len(ARTICLE_CACHE) > 500:                          # cap memory
            for k in list(ARTICLE_CACHE)[:120]:
                ARTICLE_CACHE.pop(k, None)
        # good summaries cache for the full TTL; degraded (tier-3) ones expire in
        # ~30 min so the body / related coverage gets another try, without
        # re-spending on every open.
        offset = (ARTICLE_TTL - 1800) if degraded else 0
        ARTICLE_CACHE[url] = (time.time() - offset, payload)
    return payload

_article_hits = {}                  # ip -> recent /article timestamps (own bucket)
def article_rate_check(ip):
    now = time.time()
    with _ask_lock:
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
]
_SUMSTOP = set(("the a an and or but of to in on for with from by at as is are was were be been being this that "
    "these those it its their his her our your my we you they he she over under after before into out up down new "
    "news report reports says said amid will would can could has have had not no so if then than about more most "
    "has been have been will be").split())

def _sig_tokens(s):
    return set(w for w in re.findall(r"[a-z0-9']+", (s or '').lower()) if w not in _SUMSTOP and len(w) > 2)

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

def _lede(user_content, strict=False, longer=False):
    sysmsg = LEDE_SYS \
        + (" Write 2-3 short paragraphs (about 60-110 words)." if longer else "") \
        + (" A generic restatement is unacceptable — include concrete, verifiable specifics "
           "(names, places, numbers, what happened)." if strict else "")
    body = json.dumps({'model': ASK_MODEL, 'max_tokens': (380 if longer else 170), 'system': sysmsg,
                       'messages': [{'role': 'user', 'content': user_content}]}).encode()
    req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=body, headers={
        'content-type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY, 'anthropic-version': '2023-06-01'})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return ' '.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text').strip()

def _neighbors_with_bodies(title, source, exclude_url, budget=9):
    """Related coverage (tier 2): fetch 2-4 neighbours' bodies in parallel under one budget."""
    try:
        rel = related_articles(title, source, exclude_url, n=4)
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

def make_summary(title, source='', section='', url='', desc='', longer=False, body=None, neighbors=None):
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
                      % (section, source, title, body), longer=longer)
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
                s = _lede(ctx, strict=strict, longer=longer)
                if _ok_summary(s, title, require_specifics=False): return s, False
                attempts.append(s)
            except Exception: pass
    # Tier 3 — confident summary from headline + context + world knowledge (degraded)
    try:
        s = _lede('Section: %s\nSource: %s\nHeadline: %s\nSnippet: %s\n\nWrite a confident, factual summary of '
                  'this event using the headline, snippet and your knowledge of it. Name the who/what/where. Do '
                  'not apologize or say you lack information.'
                  % (section, source, title, desc or '(none)'), strict=True, longer=longer)
        if _ok_summary(s, title): return s, True
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

def quality_summary(title, source='', section='', url='', desc=''):
    """Short wire-lede for the brief cards — background prewarm path."""
    return make_summary(title, source, section, url, desc, longer=False)

def build_brief(full=False):
    """Assemble one summarized story per section. On a 'full' rebuild every
    section is re-summarized fresh; otherwise sections already present are kept
    and only the MISSING ones are fetched/summarized — so filling out a partial
    brief costs an AI call only for the sections that are still empty."""
    prev = {s['label']: s for s in BRIEF['sections']}   # last-good cards, kept as a fallback
    out = []
    for cat in BRIEF_CATS:
        label = cat['label']
        p = prev.get(label)
        if p and not _is_article_url(p.get('url', '')):
            p = None              # never carry forward an image-host/thumbnail link → re-pick fresh
        # Re-validate each previously-built card. Keep it only if its summary is
        # genuinely good or we've already retried it enough; otherwise re-summarize
        # — this flushes stale headline-restatement slop even when it was stored as
        # non-degraded, and a truly hard story settles to a clean headline-only card.
        if not full and p and _card_settled(p):
            out.append(p)
        elif not full and p:
            tries = (p.get('degrade_tries') or 0) + 1
            np = dict(p)
            try:
                desc = og_desc(p.get('url', '')) if str(p.get('url', '')).startswith('http') else ''
                summ, deg = quality_summary(p.get('title', ''), p.get('source', ''), label, p.get('url', ''), desc)
                np['summary'] = summ
                np['degraded'] = bool(deg and tries < 3)    # stop retrying after 3 cycles
            except Exception: pass
            np['degrade_tries'] = tries
            out.append(np)
        else:
            try:
                # Use the FULL aggregate (more sources + image backfill) so each section
                # reliably yields a thumbnailed story — the fast path comes back empty too often.
                card = _pick_card(cat, fast=False)
            except Exception:
                card = None
            if card:
                a = card['a']
                url = a.get('url', '')
                desc = og_desc(url) if url.startswith('http') else ''
                summ, deg = quality_summary(a.get('title', ''), a.get('source', ''), label, url, desc)
                out.append({'label': label, 'title': a.get('title', ''), 'url': url,
                            'source': a.get('source', ''), 'image': a.get('image', ''),
                            'time': a.get('time', ''), 'summary': summ, 'degraded': deg})
            elif p:
                out.append(p)                           # keep the previous good story rather than drop the section
        if out:
            BRIEF['sections'] = list(out)               # publish progress so /brief fills in live
    BRIEF['t'] = time.time()               # always stamp so we don't loop full rebuilds

# --- Daily Morning Brief email (Kit broadcast, teaser format) ------------------
BRIEF_EMAIL = {'day': None}

def _brief_article_link(s):
    """Deep-link into the on-site article view (counts toward the free meter)."""
    q = urllib.parse.urlencode({'a': s.get('url', ''), 't': s.get('title', ''),
                                's': s.get('source', ''), 'lbl': s.get('label', '')})
    return SITE_URL + '/?' + q

def compose_brief_email():
    """Teaser email: butler intro + each section's headline + thumbnail, both
    clickable to quwwaa.com. No summaries in the email. Returns (subject, html)."""
    secs = list(BRIEF.get('sections') or [])
    now_phx = datetime.now(timezone.utc).astimezone(PHOENIX_TZ)
    try: date_str = now_phx.strftime('%A, %B %-d')
    except Exception: date_str = now_phx.strftime('%A, %B %d')
    top = secs[0].get('title') if secs else ''
    subject = ('QUWWAA Brief — ' + top) if top else ('Your QUWWAA Morning Brief · ' + date_str)
    intro = "Good morning. Here's the world this morning — tap any headline to read it on QUWWAA."
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
        '<tr><td align="center" style="font:13px Arial,sans-serif;color:#a06a3a;padding-bottom:18px;">Your Morning Brief · ' + html.escape(date_str) + '</td></tr>'
        '<tr><td style="font:15px Georgia,serif;color:#e7d3c0;line-height:1.6;padding:0 4px 8px;">' + html.escape(intro) + '</td></tr>'
        '<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">' + ''.join(rows) + '</table></td></tr>'
        '<tr><td align="center" style="padding:26px 0 8px;"><a href="' + SITE_URL + '/" style="background:#d98026;color:#1a1208;font:700 15px Arial,sans-serif;text-decoration:none;padding:13px 26px;border-radius:10px;display:inline-block;">Read the full brief on QUWWAA &rarr;</a></td></tr>'
        '<tr><td align="center" style="padding:12px 16px 4px;font:13px Arial,sans-serif;color:#cba98f;line-height:1.5;">Make it yours &mdash; <a href="' + SITE_URL + '/" style="color:#e89a5a;">start your 7-day QUWWAA Gold trial</a> for unlimited articles and a butler who knows your beats.</td></tr>'
        '<tr><td align="center" style="padding:18px 0 0;font:11px Arial,sans-serif;color:#6a4a2a;">You\'re receiving the QUWWAA Morning Brief.</td></tr>'
        '</table></td></tr></table></div>')
    return subject, doc

def create_kit_broadcast(subject, content, preview_text='', send_at=None):
    """Create a Kit v4 broadcast. No send_at -> draft; with send_at -> scheduled."""
    if not KIT_API_KEY:
        return None
    body = {'subject': subject, 'content': content, 'description': 'QUWWAA Morning Brief',
            'public': False}
    if preview_text:
        body['preview_text'] = preview_text[:150]
    if send_at:
        body['send_at'] = send_at
    req = urllib.request.Request('https://api.kit.com/v4/broadcasts', data=json.dumps(body).encode(),
        method='POST', headers={'X-Kit-Api-Key': KIT_API_KEY, 'Content-Type': 'application/json',
                                'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b'{}')

def run_brief_email(force=False):
    """Compose + create the daily broadcast. Idempotent per Phoenix day (one auto
    attempt/day); force=True (manual admin trigger) bypasses the day guard."""
    if not KIT_API_KEY or BRIEF_EMAIL_MODE == 'off':
        return {'skipped': 'disabled'}
    now_phx = datetime.now(timezone.utc).astimezone(PHOENIX_TZ)
    today = now_phx.strftime('%Y-%m-%d')
    if not force and BRIEF_EMAIL['day'] == today:
        return {'skipped': 'already_today'}
    try:
        if BRIEF['t'] == 0 or len(BRIEF['sections']) < len(BRIEF_CATS):
            build_brief(full=(BRIEF['t'] == 0))        # freshen before composing
    except Exception:
        pass
    if not BRIEF.get('sections'):
        return {'skipped': 'no_brief'}
    BRIEF_EMAIL['day'] = today                          # consume the day (1 auto attempt; manual can force)
    subject, content = compose_brief_email()
    send_at = None
    if BRIEF_EMAIL_MODE == 'send':
        send_dt = now_phx.replace(hour=BRIEF_SEND_HOUR, minute=0, second=0, microsecond=0)
        if send_dt <= now_phx:
            send_dt += timedelta(days=1)
        send_at = send_dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        res = create_kit_broadcast(subject, content, preview_text=subject, send_at=send_at)
        bid = None
        if isinstance(res, dict):
            bid = (res.get('broadcast') or {}).get('id') if isinstance(res.get('broadcast'), dict) else res.get('id')
        if BRIEF_EMAIL_MODE == 'draft' and ADMIN_USER_ID:
            try: push_to_user(ADMIN_USER_ID, {'title': 'Morning Brief draft ready',
                'body': 'Review & send in Kit — ' + subject[:80], 'url': '/'}, 'notify_brief')
            except Exception: pass
        print('[brief-email] %s broadcast created id=%s subject=%r' % (BRIEF_EMAIL_MODE, bid, subject))
        return {'ok': True, 'mode': BRIEF_EMAIL_MODE, 'subject': subject, 'broadcast_id': bid, 'send_at': send_at}
    except Exception as e:
        detail = ''
        try: detail = e.read().decode()[:300] if hasattr(e, 'read') else str(e)
        except Exception: detail = str(e)
        print('[brief-email] FAILED: %s' % detail)
        return {'error': type(e).__name__, 'detail': detail}

def brief_email_loop():
    """Fire once per Phoenix day on/after the compose hour."""
    if not KIT_API_KEY or BRIEF_EMAIL_MODE == 'off':
        return
    while True:
        try:
            if datetime.now(timezone.utc).astimezone(PHOENIX_TZ).hour >= BRIEF_COMPOSE_HOUR:
                run_brief_email()
        except Exception:
            pass
        time.sleep(int(os.environ.get('BRIEF_EMAIL_POLL_SEC', '900')))

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
        try: maybe_send_brief_push()        # once/day "your brief is ready" (no-op until push configured)
        except Exception: pass
        full = len(HOME_SNAPSHOT['items']) >= len(HOME_CATS)
        time.sleep(HOME_REFRESH if full else 45)

# --- Butler persona + Anthropic call ----------------------------------------
PERSONA = ("You are QUWWAA, Mike Dean's personal AI assistant, modeled on JARVIS from Iron Man. "
    "Reply in that voice: impeccably polite British butler, dry wit, understated, efficient. "
    "Refer to yourself as QUWWAA. Address the user as 'sir'. Keep replies brief (1-3 sentences) and "
    "conversational since they will be spoken aloud. Never use markdown, lists, or emoji. Context: Mike "
    "runs Daily Rumble, a Substack by Quwwaa LLC covering US politics and the Iran/Israel/Lebanon/Palestine "
    "region, sponsored by Zaytuna Mobile. You have a web search tool - use it whenever asked about current "
    "events, news, or to check public pages such as whether the latest Daily Rumble post is live on Substack. "
    "For private accounts (Instagram analytics, email, documents) you have no access; advise the user to ask "
    "Claude in the Cowork app, where those systems are connected. The console converts your text replies to "
    "speech and transcribes the user's spoken words to text - when the user speaks, you ARE hearing them; "
    "never say you cannot hear them or that they must type. NEWS LENS: the console has a live multi-source "
    "news panel. Whenever the user asks about a specific news story, incident, situation, or report - or wants "
    "to compare coverage - give your brief spoken take, mention you are bringing coverage up on screen, and end "
    "your reply with a tag on the final line exactly in this form: [LENS: concise search keywords]. The console "
    "strips the tag and opens the panel. Use 2-5 strong keywords, e.g. [LENS: lebanon ceasefire border]. The "
    "panel shows the last 7 days by default; only if the user explicitly asks for older coverage add a day "
    "window after a pipe: [LENS: keywords | 30d]. Do not use the tag for non-news questions.")

ASK_MAX_TOKENS = int(os.environ.get('ASK_MAX_TOKENS', '400'))   # punchy butler — shorter = faster + cheaper

def _anthropic_body(messages, extra_system, stream=False):
    return json.dumps({
        'model': ASK_MODEL, 'max_tokens': ASK_MAX_TOKENS, 'system': PERSONA + (extra_system or ''),
        'messages': messages, 'stream': bool(stream),
        'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 3}],
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
    boundary, body = _multipart_audio(file_bytes, filename, content_type,
                                      {'model': STT_MODEL, 'response_format': 'json'})
    req = urllib.request.Request('https://api.openai.com/v1/audio/transcriptions', data=body, headers={
        'Authorization': 'Bearer ' + OPENAI_API_KEY,
        'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        return (json.loads(r.read()).get('text') or '').strip()


def openai_tts(text):
    """Render text to MP3 speech via OpenAI. Falls back to the always-valid
    tts-1 model/voice if the configured primary model or voice is rejected,
    so a mis-set TTS_MODEL/TTS_VOICE never leaves the butler mute."""
    text = text[:4000]
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


# ===========================================================================
# Jarvis cockpit — GET /jarvis/stats (admin-only). Aggregates Members (Supabase),
# Revenue (Stripe), Traffic (GA4 Data API), Newsletter (Kit) into one cached
# object. Each source degrades independently so one failure never blanks the
# board; GA stays a clean stub until GA_SERVICE_ACCOUNT_JSON is configured.
# ===========================================================================
def jarvis_authed(handler):
    tok = handler.headers.get('X-Jarvis-Token', '')
    if JARVIS_ADMIN_TOKEN and tok and tok == JARVIS_ADMIN_TOKEN:
        return True
    if JARVIS_ADMIN_USER_IDS:
        try:
            u = auth_user(handler)
            if u and u.get('id') in JARVIS_ADMIN_USER_IDS:
                return True
        except Exception:
            pass
    return False

def sb_count(path_q):
    """Exact row count for a PostgREST query without pulling the rows."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return None
    headers = {'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': 'Bearer ' + SUPABASE_SERVICE_ROLE_KEY,
               'Prefer': 'count=exact', 'Range-Unit': 'items', 'Range': '0-0'}
    req = urllib.request.Request(SUPABASE_URL + '/rest/v1/' + path_q, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        cr = r.headers.get('Content-Range', '')        # e.g. '0-0/1234' or '*/1234'
    try:
        return int(cr.split('/')[-1])
    except Exception:
        return None

def jarvis_members():
    out = {'total': None, 'free': None, 'trialing': None, 'active': None,
           'brief_subscribed': None, 'new_today': None, 'new_24h': None}
    try:
        total = sb_count('profiles?select=id')
        trialing = sb_count('profiles?select=id&subscription_status=eq.trialing')
        active = sb_count('profiles?select=id&subscription_status=eq.active')
        brief = sb_count('profiles?select=id&brief_subscribed=is.true')
        now = datetime.now(timezone.utc)
        day0 = now.astimezone(PHOENIX_TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        new_today = sb_count('profiles?select=id&created_at=gte.' + urllib.parse.quote(day0.isoformat()))
        new_24h = sb_count('profiles?select=id&created_at=gte.' + urllib.parse.quote((now - timedelta(hours=24)).isoformat()))
        free = (total - (trialing or 0) - (active or 0)) if total is not None else None
        out.update(total=total, free=free, trialing=trialing, active=active,
                   brief_subscribed=brief, new_today=new_today, new_24h=new_24h)
    except Exception:
        pass
    return out

def _stripe_list(path, params, cap_pages=6):
    """Paginated Stripe list (bounded) → flat list of objects."""
    items, q = [], dict(params)
    for _ in range(cap_pages):
        data = stripe_get(path + '?' + urllib.parse.urlencode(q))
        items += data.get('data', [])
        if data.get('has_more') and data.get('data'):
            q['starting_after'] = data['data'][-1]['id']
        else:
            break
    return items

def _sub_monthly_cents(sub):
    total = 0.0
    for it in (sub.get('items', {}) or {}).get('data', []):
        price = it.get('price') or {}
        amt = (price.get('unit_amount') or 0) * (it.get('quantity') or 1)
        rec = price.get('recurring') or {}
        interval = rec.get('interval', 'month'); n = rec.get('interval_count', 1) or 1
        if interval == 'year':   amt = amt / 12.0 / n
        elif interval == 'week': amt = amt * 52 / 12.0 / n
        elif interval == 'day':  amt = amt * 365 / 12.0 / n
        else:                    amt = amt / n
        total += amt
    return total

def jarvis_revenue():
    out = {'active': None, 'trialing': None, 'mrr': None, 'revenue_month': None,
           'new_customers_week': None, 'currency': 'usd'}
    if not STRIPE_SECRET_KEY:
        return out
    try:
        active = _stripe_list('subscriptions', {'status': 'active', 'limit': 100})
        trial = _stripe_list('subscriptions', {'status': 'trialing', 'limit': 100})
        mrr = sum(_sub_monthly_cents(s) for s in active + trial) / 100.0
        now = datetime.now(timezone.utc)
        month0 = now.astimezone(PHOENIX_TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        charges = _stripe_list('charges', {'limit': 100, 'created[gte]': int(month0.timestamp())})
        rev = sum(c.get('amount', 0) for c in charges if c.get('paid') and not c.get('refunded')) / 100.0
        custs = _stripe_list('customers', {'limit': 100, 'created[gte]': int((now - timedelta(days=7)).timestamp())})
        cur = (active or trial or [{}])[0].get('currency') or 'usd'
        out.update(active=len(active), trialing=len(trial), mrr=round(mrr, 2),
                   revenue_month=round(rev, 2), new_customers_week=len(custs), currency=cur)
    except Exception:
        pass
    return out

def _ga_access_token(sa):
    import jwt                                              # PyJWT (RS256 via cryptography, already installed)
    now = int(time.time())
    assertion = jwt.encode({'iss': sa['client_email'],
                            'scope': 'https://www.googleapis.com/auth/analytics.readonly',
                            'aud': 'https://oauth2.googleapis.com/token',
                            'iat': now, 'exp': now + 3600}, sa['private_key'], algorithm='RS256')
    body = urllib.parse.urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                                   'assertion': assertion}).encode()
    req = urllib.request.Request('https://oauth2.googleapis.com/token', data=body,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())['access_token']

def _ga_run(token, body):
    req = urllib.request.Request('https://analyticsdata.googleapis.com/v1beta/properties/%s:runReport' % GA_PROPERTY_ID,
                                 data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def jarvis_traffic():
    out = {'available': False, 'users_today': None, 'new_users_today': None, 'active_7d': None,
           'avg_engagement_sec': None, 'trend': [], 'top_pages': [], 'top_countries': [], 'note': ''}
    if not GA_SERVICE_ACCOUNT_JSON:
        out['note'] = 'GA service account not configured'
        return out
    try:
        sa = json.loads(GA_SERVICE_ACCOUNT_JSON)
        tok = _ga_access_token(sa)
        today = _ga_run(tok, {'dateRanges': [{'startDate': 'today', 'endDate': 'today'}],
                              'metrics': [{'name': 'activeUsers'}, {'name': 'newUsers'}, {'name': 'userEngagementDuration'}]})
        trend = _ga_run(tok, {'dateRanges': [{'startDate': '6daysAgo', 'endDate': 'today'}],
                              'dimensions': [{'name': 'date'}], 'metrics': [{'name': 'activeUsers'}],
                              'orderBys': [{'dimension': {'dimensionName': 'date'}}]})
        pages = _ga_run(tok, {'dateRanges': [{'startDate': '7daysAgo', 'endDate': 'today'}],
                              'dimensions': [{'name': 'pageTitle'}], 'metrics': [{'name': 'screenPageViews'}],
                              'limit': 5, 'orderBys': [{'metric': {'metricName': 'screenPageViews'}, 'desc': True}]})
        ctry = _ga_run(tok, {'dateRanges': [{'startDate': '7daysAgo', 'endDate': 'today'}],
                             'dimensions': [{'name': 'country'}], 'metrics': [{'name': 'activeUsers'}],
                             'limit': 5, 'orderBys': [{'metric': {'metricName': 'activeUsers'}, 'desc': True}]})
        tr = today.get('rows', [])
        ut = int(float(tr[0]['metricValues'][0]['value'])) if tr else 0
        nu = int(float(tr[0]['metricValues'][1]['value'])) if tr else 0
        eng = float(tr[0]['metricValues'][2]['value']) if tr else 0.0
        tl = [{'date': r['dimensionValues'][0]['value'], 'users': int(float(r['metricValues'][0]['value']))}
              for r in trend.get('rows', [])]
        out.update(available=True, users_today=ut, new_users_today=nu, active_7d=sum(d['users'] for d in tl),
                   avg_engagement_sec=(round(eng / ut) if ut else 0), trend=tl,
                   top_pages=[{'title': r['dimensionValues'][0]['value'], 'views': int(float(r['metricValues'][0]['value']))}
                              for r in pages.get('rows', [])],
                   top_countries=[{'country': r['dimensionValues'][0]['value'], 'users': int(float(r['metricValues'][0]['value']))}
                                  for r in ctry.get('rows', [])], note='')
    except Exception as e:
        out['note'] = 'GA error: ' + type(e).__name__
    return out

def _kit_get(path):
    req = urllib.request.Request('https://api.kit.com/v4/' + path,
                                 headers={'X-Kit-Api-Key': KIT_API_KEY, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b'{}')

def jarvis_newsletter():
    out = {'available': False, 'subscribers': None, 'new_this_week': None, 'last_broadcast': None, 'note': ''}
    if not KIT_API_KEY:
        out['note'] = 'Kit not configured'
        return out
    try:
        subs = _kit_get('subscribers?status=active&per_page=1')
        out['subscribers'] = (subs.get('pagination') or {}).get('total_count')
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')
        recent = _kit_get('subscribers?status=active&created_after=' + urllib.parse.quote(since) + '&per_page=1')
        out['new_this_week'] = (recent.get('pagination') or {}).get('total_count')
        bl = _kit_get('broadcasts?per_page=1')
        bcs = bl.get('broadcasts') or []
        if bcs:
            b = bcs[0]
            try:
                st = _kit_get('broadcasts/%s/stats' % b.get('id'))
                s = (st.get('broadcast') or {}).get('stats') or st.get('stats') or {}
            except Exception:
                s = {}
            out['last_broadcast'] = {'subject': b.get('subject'),
                                     'sent_at': b.get('send_at') or b.get('published_at'),
                                     'recipients': s.get('recipients'),
                                     'open_rate': s.get('open_rate'), 'click_rate': s.get('click_rate')}
        out['available'] = True
    except Exception as e:
        out['note'] = 'Kit error: ' + type(e).__name__
    return out

JARVIS_CACHE = {'t': 0, 'data': None}
JARVIS_TTL = 60

def jarvis_stats():
    if JARVIS_CACHE['data'] and time.time() - JARVIS_CACHE['t'] < JARVIS_TTL:
        return JARVIS_CACHE['data']

    def _safe(fn):
        try: return fn()
        except Exception: return {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        fm = ex.submit(_safe, jarvis_members); fr = ex.submit(_safe, jarvis_revenue)
        ft = ex.submit(_safe, jarvis_traffic); fn = ex.submit(_safe, jarvis_newsletter)
        data = {'members': fm.result(), 'revenue': fr.result(),
                'traffic': ft.result(), 'newsletter': fn.result(),
                'generated_at': datetime.now(timezone.utc).isoformat()}
    JARVIS_CACHE['data'] = data; JARVIS_CACHE['t'] = time.time()
    return data


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
                msg = ("My apologies, sir - the day's allowance of inquiries has been reached. Do return tomorrow."
                       if why == 'daily_cap' else
                       "A moment's patience, sir - you are speaking faster than I can attend. Try again shortly.")
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
                self._send_json({'error': 'empty', 'reply': 'I received an empty transmission, sir.'}, 400); return
            extra = ''
            if member:
                name = member.get('display_name') or ''
                style = member.get('address_style') or 'name'
                ints = ', '.join(member.get('interests') or [])
                addr = name if (style == 'name' and name) else ('madam' if style == 'madam' else 'sir')
                extra = (" MEMBER CONTEXT: You are speaking with a QUWWAA member. Address them as '%s'." % addr)
                if ints:
                    extra += " They follow these interests: %s. Weight your awareness and suggestions toward them when relevant." % ints
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
                try:
                    full = anthropic_stream(msgs, extra, _w)
                    if not full.strip() and wrote[0] == 0:
                        _w('I received an empty transmission, sir.')
                except Exception:
                    if wrote[0] == 0:
                        # streaming failed before any token → serve the full reply non-streamed
                        try: _w(anthropic_chat(msgs, extra) or 'I received an empty transmission, sir.')
                        except Exception: _w('I encountered a fault processing that, sir.')
                try: self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()   # terminating chunk
                except Exception: pass
                return
            reply = anthropic_chat(msgs, extra) or 'I received an empty transmission, sir.'
            self._send_json({'reply': reply})
        except urllib.error.HTTPError as e:
            detail = ''
            try: detail = e.read().decode('utf-8', 'ignore')[:160]
            except Exception: pass
            self._send_json({'error': 'upstream_%d' % e.code,
                             'reply': 'The uplink returned an error (%d), sir. %s' % (e.code, detail)}, 502)
        except Exception as e:
            self._send_json({'error': type(e).__name__,
                             'reply': 'I encountered a fault processing that, sir.'}, 500)

    def do_GET(self):
        if self.path in ('/health', '/healthz'):
            self._send_json({'ok': True, 'service': 'quwwaa', 'brain': bool(ANTHROPIC_API_KEY),
                             'stt': bool(OPENAI_API_KEY), 'tts': bool(OPENAI_API_KEY),
                             'premium': PREMIUM_ENABLED})
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
            self._send_json({'sections': BRIEF['sections'], 't': BRIEF['t']})
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
            try:
                payload = cached_aggregate(q, days, fast) if q else {'query': '', 'articles': [], 'sources': 0}
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
                payload = build_article(url, title, source)
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
            if (qs.get('preview') or ['0'])[0] == '1':      # view the composed HTML in a browser
                subject, doc = compose_brief_email()
                body = ('<!-- subject: ' + subject + ' -->\n' + doc).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            self._send_json(run_brief_email(force=True))     # force a real compose+create (test)
        elif self.path.startswith('/jarvis/stats'):
            if not jarvis_authed(self):
                self._send_json({'error': 'unauthorized'}, 401); return
            try:
                self._send_json(jarvis_stats())
            except Exception as e:
                self._send_json({'error': type(e).__name__}, 500)
        else:
            base = urllib.parse.urlparse(self.path).path
            host = (self.headers.get('Host') or '').split(':')[0].lower()
            hq = host.startswith('hq.')                      # the admin cockpit subdomain
            if base in ('/', ''):
                self.path = '/jarvis.html' if hq else '/quwwaa-console.html'
            elif base in ('/jarvis', '/jarvis/'):            # also reachable by path for testing pre-DNS
                self.path = '/jarvis.html'
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
    print('QUWWAA server on http://%s:%d (news lens active, prewarming home)' % (HOST, PORT))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
