#!/usr/bin/env python3
"""QUWWAA local server — serves the console AND aggregates world news on demand.
Sources: GDELT (tens of thousands of global outlets, keyless), Google News RSS,
and Reddit. Results are fetched live, held in memory only, and never written
to disk or any server."""
import json, os, re, email.utils, html, base64, time
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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
    body = fetch(url, timeout=12) or b'{}'
    try:
        return json.loads(body).get('articles', [])
    except ValueError:
        # GDELT returns plain-text errors (rate limits etc.) — treat as empty
        return []

def src_gdelt(q, days=7):
    try:
        arts = _gdelt_call(q, days)
    except Exception:
        time.sleep(1.5)  # GDELT rate-limits bursts — one polite retry
        arts = _gdelt_call(q, days)
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
        page = fetch(url, timeout=6)
        m2 = (re.search(rb'data-n-au="([^"]+)"', page)
              or re.search(rb'href="(https?://(?!news\.google|accounts\.google|www\.google|policies\.google|support\.google|play\.google)[^"]+)"', page))
        if m2:
            return html.unescape(m2.group(1).decode('utf-8', 'ignore'))
    except Exception:
        pass
    return ''

def fix_gnews_urls(arts, limit=20):
    need = [a for a in arts if 'news.google.com' in a['url']][:limit]
    if not need:
        return
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(resolve_gnews, a['url']): a for a in need}
        for fu in futs:
            try:
                u = fu.result(timeout=9)
                if u:
                    futs[fu]['url'] = u
            except Exception:
                pass

def _tok(t):
    return {w for w in re.sub(r'[^a-z0-9 ]', ' ', t.lower()).split() if len(w) > 3}

def upgrade_gnews(arts, limit=12):
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
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(lookup, a): a for a in need}
        for fu in futs:
            try:
                h = fu.result(timeout=10)
            except Exception:
                h = None
            if h:
                a = futs[fu]
                a['url'] = h['url'] or a['url']
                if h['image']:
                    a['image'] = h['image']

def fill_images(arts, limit=25):
    # fill missing images, and upgrade Bing's compressed thumbs to the
    # publisher's own og:image when one exists (Bing kept as fallback)
    need = [a for a in arts if (not a['image'] or 'bing.com/th' in a['image'])
            and a['url'].startswith('http')
            and 'news.google.com' not in a['url']][:limit]
    if not need:
        return
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(og_image, a['url']): a for a in need}
        for fu in futs:
            try:
                img = fu.result(timeout=8)
                if img:
                    futs[fu]['image'] = img
            except Exception:
                pass

def aggregate(q, days=7, fast=False):
    results, seen, arts = [], set(), []
    diag = {}
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    sources = ((src_bing, 'BING'), (src_gdelt, 'GDELT')) if fast else \
              ((src_rsspack, 'FEEDS'), (src_gdelt, 'GDELT'), (src_bing, 'BING'),
               (src_yahoo, 'YAHOO'), (src_bsky, 'BLUESKY'),
               (src_gnews, 'GNEWS'), (src_reddit, 'REDDIT'))
    with ThreadPoolExecutor(max_workers=7) as ex:
        futs = {ex.submit(f, q, days): n for f, n in sources}
        for fu, name in futs.items():
            try:
                r = fu.result(timeout=20)
                diag[name] = len(r)
                results += r
            except Exception as e:
                diag[name] = type(e).__name__  # visible failure, never silent
    for a in results:
        if a['ts'] < cutoff:
            continue  # hard freshness window — undated or older items are dropped
        key = re.sub(r'[^a-z0-9]+', '', (a['title'] or '').lower())[:60]
        if not key or key in seen:
            continue
        seen.add(key)
        arts.append(a)
    arts.sort(key=lambda a: a['ts'], reverse=True)
    arts = arts[:60]
    if not fast:  # fast mode skips the slow enrichment passes
        fix_gnews_urls(arts)
        upgrade_gnews(arts)
        fill_images(arts)
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

HOME_QUERIES = [('politics', 1), ('sports', 2), ('stock market', 2), ('breaking news', 1)]

def prewarm():
    """Keep the home-screen queries permanently warm in the background."""
    while True:
        for q, d in HOME_QUERIES:
            try:
                cached_aggregate(q, d, True)
            except Exception:
                pass
        time.sleep(480)  # refresh well inside the cache TTL

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

def anthropic_chat(messages):
    body = json.dumps({
        'model': ASK_MODEL, 'max_tokens': 600, 'system': PERSONA, 'messages': messages,
        'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 3}],
    }).encode()
    req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=body, headers={
        'content-type': 'application/json', 'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01'})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    parts = [b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text']
    return ' '.join(parts).strip()

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
        self.send_error(404)

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
            reply = anthropic_chat(msgs) or 'I received an empty transmission, sir.'
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
            self._send_json({'ok': True, 'service': 'quwwaa', 'brain': bool(ANTHROPIC_API_KEY)})
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
        else:
            if self.path in ('/', ''):
                self.path = '/quwwaa-console.html'   # land visitors on the console
            super().do_GET()

    def log_message(self, *a):
        pass

if __name__ == '__main__':
    import threading
    threading.Thread(target=prewarm, daemon=True).start()
    print('QUWWAA server on http://%s:%d (news lens active, prewarming home)' % (HOST, PORT))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
