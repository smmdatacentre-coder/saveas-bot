import os
import re
import glob
import time
import json
import uuid
import base64
import zlib
import subprocess
import sys
import logging
import asyncio
import shutil
import requests
import yt_dlp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOWNLOAD_DIR = "/tmp/saveas_uploads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ['PATH'] = BOT_DIR + ':' + os.environ.get('PATH', '')

ADMIN_ID = 256869382
ERROR_SUFFIX = "\n\n📞 Сообщите об ошибке @d8shot_manager_bot и приложите скрин — пофиксим в ближайшее время!"

user_queues = {}
user_locks = {}
pending_yt = {}
pending_vk = {}
queue_workers = {}


def get_ffmpeg_path():
    bundled = os.path.join(BOT_DIR, 'ffmpeg')
    if os.path.exists(bundled) and os.access(bundled, os.X_OK):
        return bundled
    for p in ['/usr/local/bin/ffmpeg', shutil.which('ffmpeg')]:
        if p and os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return shutil.which('ffmpeg') or 'ffmpeg'


def get_ffmpeg_location():
    ffmpeg_path = get_ffmpeg_path()
    return os.path.dirname(ffmpeg_path) if os.path.sep in ffmpeg_path else None


def get_instagram_cookiefile():
    cookiefile = os.path.join(BOT_DIR, 'cookies.txt')
    if os.path.exists(cookiefile) and os.path.getsize(cookiefile) > 50:
        return cookiefile

    b64_file = os.path.join(BOT_DIR, 'cookies_b64.txt')
    if os.path.exists(b64_file):
        try:
            with open(b64_file) as f:
                encoded = f.read().strip()
            if encoded:
                data = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
                with open(cookiefile, 'w', encoding='utf-8') as f:
                    f.write(data.decode('utf-8'))
                return cookiefile
        except Exception as e:
            logger.error(f'IG cookies b64 restore error: {e}')

    encoded = os.environ.get('INSTAGRAM_COOKIES_B64', '').strip()
    if not encoded:
        return None
    try:
        encoded += '=' * (-len(encoded) % 4)
        data = base64.urlsafe_b64decode(encoded)
        if data.startswith(b'x\x9c') or data.startswith(b'x\xda'):
            data = zlib.decompress(data)
        cookiefile = os.path.join('/tmp', 'instagram_cookies.txt')
        with open(cookiefile, 'w', encoding='utf-8') as f:
            f.write(data.decode('utf-8'))
        return cookiefile
    except Exception as e:
        logger.error(f'Instagram cookies decode error: {e}')
        return None


def get_token():
    env_path = os.path.join(BOT_DIR, '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith('BOT_TOKEN='):
                    return line.strip().split('=', 1)[1]
    return os.environ.get('BOT_TOKEN', '')


def detect_url_type(url):
    u = url.lower()
    if 'ok.ru' in u:
        return 'broken'
    if re.search(r'instagram\.com/p/', u) or re.search(r'instagram\.com/(?:reels?|tv)/', u):
        return 'ig_post'
    if re.search(r'instagram\.com/stories/', u):
        return 'ig_post'
    if 'youtube.com' in u or 'youtu.be' in u:
        if '/shorts/' in u:
            return 'yt_short'
        return 'video'
    if 'vk.com' in u or 'vkvideo.ru' in u:
        return 'video'
    if 'instagram.com' in u:
        return 'video'
    if 'tiktok.com' in u:
        if '/photo/' in u:
            return 'tt_carousel'
        return 'video'
    if 'rutube.ru' in u:
        return 'video'
    if 'pinterest.' in u or 'pin.it' in u:
        return 'pinterest'
    if 'threads.net' in u or 'threads.com' in u:
        return 'threads'
    return 'video'


def detect_platform(url):
    u = url.lower()
    if 'youtube.com' in u or 'youtu.be' in u:
        return 'youtube'
    if 'vk.com' in u or 'vkvideo.ru' in u:
        return 'vk'
    if 'instagram.com' in u:
        return 'instagram'
    if 'tiktok.com' in u:
        return 'tiktok'
    if 'ok.ru' in u:
        return 'ok'
    if 'rutube.ru' in u:
        return 'rutube'
    if 'x.com' in u or 'twitter.com' in u:
        return 'x'
    if 'threads.net' in u or 'threads.com' in u:
        return 'threads'
    if 'pinterest.' in u or 'pin.it' in u:
        return 'pinterest'
    return 'unknown'


def url_to_title(url):
    short = url.replace('https://', '').replace('http://', '')
    short = short.split('?')[0].split('/')[-1]
    if len(short) > 30:
        short = short[:30] + '...'
    return short or url[:40]


class DownloadTask:
    def __init__(self, url, quality=None, title=''):
        self.url = url
        self.quality = quality
        self.title = title
        self.status = 'waiting'
        self.progress = ''
        self.filename = None
        self.result = None
        self.start_time = None


class ProgressTracker:
    def __init__(self, task, msg_ref, loop, title='', queue=None):
        self.task = task
        self.msg_ref = msg_ref
        self.loop = loop
        self.last_update = 0
        self.title = title[:40] if title else ''
        self.queue = queue or []
        self.last_speed_check = 0
        self.low_speed_count = 0
        self.aborted = False

    def hook(self, d):
        if self.aborted:
            raise Exception('Скачивание прервано: слишком медленная скорость')
        if d['status'] != 'downloading':
            return
        now = time.time()
        if now - self.last_update < 3:
            return
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        downloaded = d.get('downloaded_bytes', 0)
        if total <= 0:
            return
        pct = downloaded / total * 100
        speed = d.get('speed')
        eta = d.get('eta')

        if speed is not None and speed > 0 and speed < 10240:
            if now - self.last_speed_check > 10:
                self.low_speed_count += 1
                self.last_speed_check = now
                if self.low_speed_count >= 6:
                    self.aborted = True
                    raise Exception('Скачивание прервано: скорость < 10KB/s более 60 секунд')
        else:
            self.low_speed_count = 0

        bar_len = 15
        filled = int(bar_len * pct / 100)
        bar = '█' * filled + '░' * (bar_len - filled)
        progress = f"{bar} {pct:.0f}%"
        if speed and speed > 0:
            if speed > 1024 * 1024:
                progress += f" | {speed / 1024 / 1024:.1f}MB/s"
            else:
                progress += f" | {speed / 1024:.0f}KB/s"
        if eta and eta > 0:
            m, s = divmod(eta, 60)
            progress += f" | {m}:{s:02d}" if m else f" | {s}с"
        line = f"⬇️ <b>{self.title or 'Загрузка'}</b>\n{progress}"
        self.task.progress = line
        self.last_update = now
        try:
            asyncio.run_coroutine_threadsafe(self._update(), self.loop)
        except Exception:
            pass

    async def _update(self):
        if self.msg_ref[0]:
            try:
                await self.msg_ref[0].edit_text(self.task.progress, parse_mode=ParseMode.HTML)
            except Exception:
                pass

    def done(self):
        self.task.progress = ''


def get_youtube_proxy():
    return os.environ.get('YOUTUBE_PROXY', '').strip() or None


def make_ydl_opts(fmt=None, quality=None):
    base = {
        'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
        'noplaylist': True,
        'no_warnings': True,
        'quiet': True,
        'socket_timeout': 120,
        'retries': 10,
        'fragment_retries': 10,
        'http_chunk_size': 2097152,
        'no_check_certificates': True,
    }
    ffmpeg_location = get_ffmpeg_location()
    if ffmpeg_location:
        base['ffmpeg_location'] = ffmpeg_location
    proxy = get_youtube_proxy()
    if proxy:
        base['proxy'] = proxy
    if fmt:
        base['format'] = fmt
    if quality:
        if quality == 'audio':
            base['format'] = 'bestaudio/best'
            base['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp3'}]
        elif quality.endswith('p'):
            h = int(quality[:-1])
            base['format'] = f'best[height<={h}][ext=mp4]/best[height<={h}]/best'
        else:
            base['format'] = quality
    return base


def get_youtube_formats(url):
    opts = make_ydl_opts()
    opts['extractor_args'] = {'youtube': {'player_client': ['android_vr']}}
    opts['skip_download'] = True
    proxy = get_youtube_proxy()
    if proxy:
        opts['proxy'] = proxy
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    available_heights = set()
    for f in info.get('formats', []):
        h = f.get('height')
        if h:
            available_heights.add(h)
    popular = [1080, 720, 480, 360]
    formats = []
    for h in popular:
        if h in available_heights:
            formats.append({
                'height': h,
                'label': f"🎬 {h}p",
                'quality': f"{h}p",
            })
    if not formats:
        max_h = max(available_heights) if available_heights else 360
        formats.append({
            'height': max_h,
            'label': f"🎬 {max_h}p",
            'quality': f"{max_h}p",
        })
    return info, formats


def get_vk_formats(url):
    opts = make_ydl_opts()
    opts['skip_download'] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    available_heights = set()
    for f in info.get('formats', []):
        h = f.get('height')
        if h and h >= 144:
            available_heights.add(h)
    popular = [1080, 720, 480, 360, 240, 144]
    formats = []
    for h in popular:
        if h in available_heights:
            formats.append({
                'height': h,
                'label': f"🎬 {h}p",
                'quality': f"{h}p",
            })
    if not formats:
        max_h = max(available_heights) if available_heights else 360
        formats.append({
            'height': max_h,
            'label': f"🎬 {max_h}p",
            'quality': f"{max_h}p",
        })
    return info, formats


def _load_ig_cookies():
    cookies = {}
    cookies_file = os.path.join(BOT_DIR, 'cookies.txt')
    if os.path.exists(cookies_file):
        with open(cookies_file) as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                parts = line.strip().split('\t')
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
    return cookies


def _extract_ig_shortcode(url):
    m = re.search(r'instagram\.com/p/([^/?]+)', url)
    if m:
        return m.group(1)
    return None


def _ig_get_media_pk(shortcode):
    try:
        cookies = _load_ig_cookies()
        s = requests.Session()
        for k, v in cookies.items():
            s.cookies.set(k, v, domain='.instagram.com')
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'X-IG-App-ID': '936619743392459',
            'X-CSRFToken': cookies.get('csrftoken', ''),
        })
        resp = s.get(f'https://www.instagram.com/p/{shortcode}/', timeout=15)
        if resp.status_code == 200:
            m = re.search(r'"media_id"\s*:\s*"?(\d+)"?', resp.text)
            if m:
                return int(m.group(1))
    except Exception as e:
        logger.error(f"get media pk error: {e}")
    return None


def _ig_api_get(pk):
    cookies = _load_ig_cookies()
    s = requests.Session()
    for k, v in cookies.items():
        s.cookies.set(k, v, domain='.instagram.com')
    s.headers.update({
        'User-Agent': 'Instagram 301.0.0.27.98 (iPhone16,2; iOS 17_5_1)',
        'X-IG-App-ID': '936619743392459',
        'X-CSRFToken': cookies.get('csrftoken', ''),
    })
    r = s.get(f'https://i.instagram.com/api/v1/media/{pk}/info/', timeout=15)
    if r.status_code == 200:
        items = r.json().get('items', [])
        if items:
            return items[0]
    return None


def _ig_gallery_dl(url, tmp_dir, cookies_file=None):
    """Run gallery-dl and return downloaded files."""
    if not cookies_file:
        cookies_file = get_instagram_cookiefile()
    cmd = [sys.executable, '-m', 'gallery_dl']
    if cookies_file:
        cmd += ['--cookies', cookies_file]
    cmd += ['-d', tmp_dir, url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if result.returncode:
        logger.error(f"Instagram gallery-dl error [{result.returncode}]: {result.stderr[-500:]}")
    photos = []
    for root, dirs, files in os.walk(tmp_dir):
        for fn in sorted(files):
            fp = os.path.join(root, fn)
            if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                if fp.lower().endswith(('.mp4', '.mov', '.webm')):
                    w, h = extract_video_dimensions(fp)
                    if w and h and w == h:
                        logger.warning(f"IG gallery-dl: square video ({w}x{h}), removing")
                        os.remove(fp)
                        continue
                photos.append(fp)
    return photos


def download_ig_post(url):
    photos = []
    caption = ''
    tmp_dir = os.path.join(DOWNLOAD_DIR, f"ig_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)
    cookies_file = get_instagram_cookiefile()

    # Stories — IG API first, instaloader fallback
    story_match = re.search(r'instagram\.com/stories/[^/]+/(\d+)', url)
    if story_match:
        pk = story_match.group(1)
        try:
            item = _ig_api_get(pk)
            if item:
                media_type = item.get('media_type', 1)
                if media_type == 2:
                    vids = item.get('video_versions', [])
                    if vids:
                        vids.sort(key=lambda v: v.get('width', 0) * v.get('height', 0), reverse=True)
                        dl_url = vids[0]['url']
                        dr = requests.get(dl_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
                        if dr.status_code == 200:
                            fp = os.path.join(tmp_dir, 'story.mp4')
                            with open(fp, 'wb') as f:
                                f.write(dr.content)
                            if os.path.getsize(fp) > 0:
                                return [fp], '', tmp_dir
                elif item.get('image_versions2'):
                    cands = item['image_versions2'].get('candidates', [])
                    if cands:
                        best = max(cands, key=lambda x: x.get('width', 0) * x.get('height', 0))
                        dl_url = best.get('url')
                        if dl_url:
                            dr = requests.get(dl_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
                            if dr.status_code == 200:
                                fp = os.path.join(tmp_dir, 'story.jpg')
                                with open(fp, 'wb') as f:
                                    f.write(dr.content)
                                if os.path.getsize(fp) > 0:
                                    return [fp], '', tmp_dir
        except Exception as e:
            logger.error(f"Instagram story API error: {e}")

        # instaloader fallback for stories (with timeout)
        try:
            import instaloader as _il
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout

            def _il_story():
                loader = _il.Instaloader(
                    quiet=True, download_pictures=False, download_videos=False,
                    download_video_thumbnails=False, download_geotags=False,
                    download_comments=False, save_metadata=False, compress_json=False,
                )
                user_match = re.search(r'instagram\.com/stories/([^/]+)/', url)
                if user_match:
                    username = user_match.group(1)
                    profile = _il.Profile.from_username(loader.context, username)
                    stories = loader.get_stories(user_ids=[profile.userid])
                    for story in stories:
                        for item in story.get_items():
                            if str(item.media_id) == pk or str(item.shortcode) == pk:
                                if item.is_video and item.video_url:
                                    fp = os.path.join(tmp_dir, 'story.mp4')
                                    dr = requests.get(item.video_url, timeout=30)
                                    if dr.status_code == 200:
                                        with open(fp, 'wb') as f:
                                            f.write(dr.content)
                                        if os.path.getsize(fp) > 0:
                                            return fp
                                elif item.url:
                                    fp = os.path.join(tmp_dir, 'story.jpg')
                                    dr = requests.get(item.url, timeout=30)
                                    if dr.status_code == 200:
                                        with open(fp, 'wb') as f:
                                            f.write(dr.content)
                                        if os.path.getsize(fp) > 0:
                                            return fp
                return None

            with ThreadPoolExecutor(1) as pool:
                result = pool.submit(_il_story).result(timeout=25)
                if result:
                    return [result], '', tmp_dir
        except _FutTimeout:
            logger.error("Instagram story instaloader timeout")
        except Exception as e:
            logger.error(f"Instagram story instaloader fallback error: {e}")

    # instaloader FIRST — works WITHOUT cookies
    if '/p/' not in url.lower():
        try:
            import instaloader as _il
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout2

            def _il_post():
                loader = _il.Instaloader(
                    quiet=True, download_pictures=False, download_videos=False,
                    download_video_thumbnails=False, download_geotags=False,
                    download_comments=False, save_metadata=False, compress_json=False,
                )
                sc_match = re.search(r'instagram\.com/(?:p|reel|tv)/([^/?]+)', url)
                if not sc_match:
                    return [], ''
                shortcode = sc_match.group(1)
                post = _il.Post.from_shortcode(loader.context, shortcode)
                result_photos = []
                cap = post.caption or ''
                if post.is_video and post.video_url:
                    fp = os.path.join(tmp_dir, f"{post.shortcode}.mp4")
                    dr = requests.get(post.video_url, timeout=60)
                    if dr.status_code == 200:
                        with open(fp, 'wb') as f:
                            f.write(dr.content)
                        if os.path.getsize(fp) > 0:
                            result_photos.append(fp)
                elif post.url:
                    fp = os.path.join(tmp_dir, f"{post.shortcode}.jpg")
                    dr = requests.get(post.url, timeout=30)
                    if dr.status_code == 200:
                        with open(fp, 'wb') as f:
                            f.write(dr.content)
                        if os.path.getsize(fp) > 0:
                            result_photos.append(fp)
                if not result_photos and post.typename == 'GraphSidecar':
                    for i, node in enumerate(post.get_sidecar_nodes()):
                        if node.is_video and node.video_url:
                            ext = 'mp4'
                            dl_url = node.video_url
                        elif node.display_url:
                            ext = 'jpg'
                            dl_url = node.display_url
                        else:
                            continue
                        fp = os.path.join(tmp_dir, f"{i}.{ext}")
                        dr = requests.get(dl_url, timeout=30)
                        if dr.status_code == 200:
                            with open(fp, 'wb') as f:
                                f.write(dr.content)
                            if os.path.getsize(fp) > 0:
                                result_photos.append(fp)
                return result_photos, cap

            with ThreadPoolExecutor(1) as pool:
                r_photos, r_caption = pool.submit(_il_post).result(timeout=25)
                photos = r_photos
                if r_caption:
                    caption = r_caption
        except _FutTimeout2:
            logger.error("Instagram instaloader timeout")
        except Exception as e:
            logger.error(f"Instagram instaloader fallback error: {e}")

    # gallery-dl fallback (needs cookies)
    if not photos:
        photos = _ig_gallery_dl(url, tmp_dir, cookies_file)

    # yt-dlp fallback (needs cookies, non-carousel only)
    if not photos and '/p/' not in url.lower():
        try:
            ydl_opts = make_ydl_opts()
            ydl_opts['outtmpl'] = os.path.join(tmp_dir, '%(id)s.%(ext)s')
            ydl_opts['format'] = 'best[ext=mp4]/best'
            if cookies_file:
                ydl_opts['cookiefile'] = cookies_file
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    for fp in glob.glob(os.path.join(tmp_dir, f"{info.get('id', '')}.*")):
                        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                            if fp.lower().endswith(('.mp4', '.mov', '.webm')):
                                w, h = extract_video_dimensions(fp)
                                if w and h and w == h:
                                    os.remove(fp)
                                    continue
                            photos.append(fp)
        except Exception as e:
            logger.error(f"Instagram yt-dlp fallback error: {e}")

    return photos, caption, tmp_dir


def download_tt_carousel(url):
    """Download TikTok carousel/photo post by parsing page HTML."""
    photos = []
    caption = ''
    tmp_dir = os.path.join(DOWNLOAD_DIR, f"tt_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        s = requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        resp = s.get(url, timeout=20, allow_redirects=True)
        if resp.status_code != 200:
            logger.error(f"TT carousel HTTP {resp.status_code}")
            return photos, caption, tmp_dir

        html = resp.text

        # Try __UNIVERSAL_DATA_FOR_REHYDRATION__ first
        m = re.search(r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                item = None
                # Navigate the JSON structure
                default_scope = data.get('__DEFAULT_SCOPE__', {})
                if 'webapp.detail-page' in default_scope:
                    item_data = default_scope['webapp.detail-page'].get('itemInfo', {}).get('itemStruct', {})
                    item = item_data
                elif 'webapp.video-detail' in default_scope:
                    item_data = default_scope['webapp.video-detail'].get('itemInfo', {}).get('itemStruct', {})
                    item = item_data
                elif 'webapp.reflow.video.detail' in default_scope:
                    item_data = default_scope['webapp.reflow.video.detail'].get('itemInfo', {}).get('itemStruct', {})
                    item = item_data

                if item:
                    caption = item.get('desc', '') or ''
                    # Check for images (carousel)
                    images = item.get('imagePost', {}).get('images', [])
                    if images:
                        for i, img in enumerate(images):
                            img_url = None
                            # Prefer highest resolution
                            if isinstance(img, dict):
                                img_list = img.get('imageURL', {}).get('urlList', [])
                                if img_list:
                                    img_url = img_list[-1]  # last is usually highest res
                            elif isinstance(img, str):
                                img_url = img
                            if img_url:
                                try:
                                    dr = s.get(img_url, timeout=60)
                                    if dr.status_code == 200:
                                        ct = dr.headers.get('content-type', '')
                                        ext = 'mp4' if 'video' in ct else 'jpg'
                                        fp = os.path.join(tmp_dir, f"{i}.{ext}")
                                        with open(fp, 'wb') as f:
                                            f.write(dr.content)
                                        photos.append(fp)
                                except Exception as e:
                                    logger.error(f"TT carousel download [{i}]: {e}")
                    # Also check for video in imagePost
                    video = item.get('imagePost', {}).get('video', {})
                    if video:
                        vid_list = video.get('urlList', [])
                        if vid_list:
                            vid_url = vid_list[-1]
                            try:
                                dr = s.get(vid_url, timeout=120)
                                if dr.status_code == 200:
                                    fp = os.path.join(tmp_dir, "video.mp4")
                                    with open(fp, 'wb') as f:
                                        f.write(dr.content)
                                    photos.append(fp)
                            except Exception as e:
                                logger.error(f"TT carousel video download: {e}")
            except json.JSONDecodeError as e:
                logger.error(f"TT carousel JSON parse error: {e}")

        # Fallback: try SIGI_STATE
        if not photos:
            m2 = re.search(r'<script\s+id="SIGI_STATE"[^>]*>(.*?)</script>', html, re.DOTALL)
            if m2:
                try:
                    data = json.loads(m2.group(1))
                    item_module = data.get('ItemModule', {})
                    items = item_module.get('items', [])
                    if items:
                        item = items[0]
                        caption = item.get('desc', '') or ''
                        images = item.get('imagePost', {}).get('images', [])
                        for i, img in enumerate(images):
                            img_url = None
                            if isinstance(img, dict):
                                img_list = img.get('imageURL', {}).get('urlList', [])
                                if img_list:
                                    img_url = img_list[-1]
                            elif isinstance(img, str):
                                img_url = img
                            if img_url:
                                try:
                                    dr = s.get(img_url, timeout=60)
                                    if dr.status_code == 200:
                                        ct = dr.headers.get('content-type', '')
                                        ext = 'mp4' if 'video' in ct else 'jpg'
                                        fp = os.path.join(tmp_dir, f"{i}.{ext}")
                                        with open(fp, 'wb') as f:
                                            f.write(dr.content)
                                        photos.append(fp)
                                except Exception as e:
                                    logger.error(f"TT carousel sigi download [{i}]: {e}")
                except json.JSONDecodeError as e:
                    logger.error(f"TT carousel SIGI JSON error: {e}")

        # gallery-dl supports TikTok photo URLs that yt-dlp rejects.
        if not photos:
            try:
                result = subprocess.run(
                    [sys.executable, '-m', 'gallery_dl', '-d', tmp_dir, url],
                    capture_output=True, text=True, timeout=90
                )
                if result.returncode:
                    logger.error(f"TT gallery-dl error: {result.stderr[-500:]}")
                for root, dirs, files in os.walk(tmp_dir):
                    for fn in sorted(files):
                        fp = os.path.join(root, fn)
                        if (fp.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov', '.webm'))
                                and os.path.getsize(fp) > 0):
                            photos.append(fp)
            except Exception as e:
                logger.error(f"TT gallery-dl fallback: {e}")

        # TikTok may block a server IP. TikWM returns signed image URLs for
        # photo posts without relying on TikTok's page HTML.
        if not photos:
            try:
                api_response = requests.get(
                    'https://www.tikwm.com/api/', params={'url': url}, timeout=30
                )
                api_data = api_response.json()
                item = api_data.get('data', {}) if api_data.get('code') == 0 else {}
                caption = item.get('title', '') or caption
                for i, image_url in enumerate(item.get('images', [])):
                    image_response = requests.get(image_url, timeout=60)
                    if image_response.status_code == 200:
                        fp = os.path.join(tmp_dir, f"tikwm_{i}.jpg")
                        with open(fp, 'wb') as f:
                            f.write(image_response.content)
                        photos.append(fp)
            except Exception as e:
                logger.error(f"TT TikWM fallback: {e}")

        # Fallback: try yt-dlp for single videos.
        if not photos:
            try:
                opts = {
                    'outtmpl': os.path.join(tmp_dir, '%(id)s.%(ext)s'),
                    'noplaylist': True,
                    'quiet': True,
                    'no_warnings': True,
                    'socket_timeout': 30,
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info:
                        vid = info.get('id', '')
                        for f in glob.glob(os.path.join(tmp_dir, f'{vid}.*')):
                            if os.path.getsize(f) > 0:
                                photos.append(f)
            except Exception as e:
                logger.error(f"TT carousel yt-dlp fallback: {e}")

    except Exception as e:
        logger.error(f"TT carousel error: {e}")

    return photos, caption, tmp_dir


def download_tt_media(url):
    """Unified TikTok downloader: uses TikWM API for both carousels and videos."""
    tmp_dir = os.path.join(DOWNLOAD_DIR, f"tt_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        api_resp = requests.get('https://www.tikwm.com/api/', params={'url': url}, timeout=30)
        api_data = api_resp.json()
        if api_data.get('code') != 0:
            return {'type': 'error', 'error': 'TikWM API error'}

        item = api_data.get('data', {})
        title = item.get('title', '')
        images = item.get('images', [])
        play_url = item.get('play', '')

        if images:
            photos = []
            for i, img_url in enumerate(images):
                try:
                    dr = requests.get(img_url, timeout=60)
                    if dr.status_code == 200:
                        fp = os.path.join(tmp_dir, f"tikwm_{i}.jpg")
                        with open(fp, 'wb') as f:
                            f.write(dr.content)
                        photos.append(fp)
                except Exception as e:
                    logger.error(f"TikWM carousel download [{i}]: {e}")
            return {'type': 'carousel', 'photos': photos, 'caption': title}

        if play_url:
            try:
                dr = requests.get(play_url, timeout=180, stream=True)
                if dr.status_code == 200:
                    fp = os.path.join(tmp_dir, f"tikwm_{item.get('id', 'video')}.mp4")
                    with open(fp, 'wb') as f:
                        for chunk in dr.iter_content(chunk_size=8192):
                            f.write(chunk)
                    if os.path.getsize(fp) > 0:
                        return {
                            'type': 'video',
                            'filename': fp,
                            'title': title,
                            'filesize': os.path.getsize(fp),
                        }
            except Exception as e:
                logger.error(f"TikWM video download error: {e}")

    except Exception as e:
        logger.error(f"TikWM API error: {e}", exc_info=True)

    logger.info(f"TikWM failed for {url}, trying yt-dlp...")
    try:
        import glob as g
        vid = url.rstrip('/').split('/')[-1].split('?')[0]
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': os.path.join(tmp_dir, f'{vid}.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
        }
        ffmpeg_location = get_ffmpeg_location()
        if ffmpeg_location:
            ydl_opts['ffmpeg_location'] = ffmpeg_location
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        fn_list = g.glob(os.path.join(tmp_dir, f'{vid}.*'))
        if fn_list:
            fp = fn_list[0]
            return {
                'type': 'video',
                'filename': fp,
                'title': (info.get('title', '') if info else '') or 'TikTok',
                'filesize': os.path.getsize(fp),
            }
    except Exception as e:
        logger.error(f"TikTok yt-dlp fallback error: {e}", exc_info=True)

    return {'type': 'error', 'error': 'Не удалось скачать TikTok'}


def download_x_media(url):
    tmp_dir = os.path.join(DOWNLOAD_DIR, f"x_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        ydl_opts = {
            'outtmpl': os.path.join(tmp_dir, '%(id)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'writeinfojson': True,
        }
        ffmpeg_location = get_ffmpeg_location()
        if ffmpeg_location:
            ydl_opts['ffmpeg_location'] = ffmpeg_location
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if not info:
            return {'type': 'error', 'error': 'Не удалось получить информацию'}
        description = info.get('description', '') or info.get('fulltitle', '') or ''
        entries = info.get('entries') or [info]
        files = []
        for entry in entries:
            vid_id = entry.get('id', info.get('id', ''))
            for ext in ['mp4', 'webm', 'jpg', 'jpeg', 'png']:
                fp = os.path.join(tmp_dir, f'{vid_id}.{ext}')
                if os.path.exists(fp) and os.path.getsize(fp) > 0:
                    files.append(fp)
                    break
        if not files:
            fn_list = glob.glob(os.path.join(tmp_dir, '*.*'))
            files = [f for f in fn_list if os.path.getsize(f) > 0 and not f.endswith('.json')]
        if files:
            has_video = any(f.lower().endswith(('.mp4', '.webm', '.mov')) for f in files)
            return {
                'type': 'video' if has_video else 'photos',
                'files': files,
                'caption': description[:1024],
            }
    except Exception as e:
        logger.error(f"X/Twitter yt-dlp error: {e}")

    logger.info(f"yt-dlp failed for X, trying page scrape...")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(url, headers=headers, timeout=30)
        html = resp.text
        description = ''
        og_desc = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html)
        if og_desc:
            description = og_desc.group(1)
        else:
            tw_desc = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html)
            if tw_desc:
                description = tw_desc.group(1)
        img_urls = re.findall(r'https://pbs\.twimg\.com/media/[^\s"\'<>]+', html)
        img_urls = list(dict.fromkeys(img_urls))
        if not img_urls:
            img_urls = re.findall(r'https://pbs\.twimg\.com/ext_tw_video/[^\s"\'<>]+', html)
        files = []
        for i, img_url in enumerate(img_urls[:10]):
            clean_url = img_url.split('?')[0]
            if not clean_url.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                clean_url += '.jpg'
            try:
                dr = requests.get(clean_url, headers=headers, timeout=30)
                if dr.status_code == 200:
                    fp = os.path.join(tmp_dir, f"x_{i}.jpg")
                    with open(fp, 'wb') as f:
                        f.write(dr.content)
                    if os.path.getsize(fp) > 0:
                        files.append(fp)
            except Exception as e:
                logger.error(f"X photo download [{i}]: {e}")
        if files:
            return {
                'type': 'photos',
                'files': files,
                'caption': description[:1024],
            }
    except Exception as e:
        logger.error(f"X/Twitter scrape error: {e}", exc_info=True)

    return {'type': 'error', 'error': 'Не удалось скачать из X/Twitter'}


GOOGLEBOT_UA = 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'


def _threads_resolve_url(url):
    """Resolve short/share Threads URLs to full permalink."""
    if '/t/' in url or '/share/' in url:
        try:
            resp = requests.head(url, allow_redirects=True, timeout=10,
                                 headers={'User-Agent': GOOGLEBOT_UA})
            if resp.url and ('/post/' in resp.url or '/@' in resp.url):
                return resp.url
        except:
            pass
    return url


def _get_carousel_from_feed(username, shortcode):
    """Fetch user's feed via Googlebot SSR and extract carousel media for a post."""
    try:
        feed_url = f'https://www.threads.com/@{username}/'
        resp = requests.get(feed_url, headers={'User-Agent': GOOGLEBOT_UA}, timeout=20)
        if resp.status_code != 200:
            return []

        html_feed = resp.text
        sc_pattern = f'"code":"{shortcode}"'
        sc_pos = html_feed.find(sc_pattern)
        if sc_pos < 0:
            return []

        cml_pos = -1

        prev_code_end = html_feed.rfind('"code":"', max(0, sc_pos - 200000), sc_pos)
        search_from = prev_code_end + 10 if prev_code_end >= 0 else max(0, sc_pos - 200000)
        car_matches_back = list(re.finditer(r'"carousel_media":\s*\[', html_feed[search_from:sc_pos]))
        if car_matches_back:
            cml_pos = search_from + car_matches_back[-1].start()

        if cml_pos < 0:
            next_code_pos = html_feed.find('"code":"', sc_pos + len(sc_pattern))
            if next_code_pos < 0:
                next_code_pos = sc_pos + 200000
            car_matches = list(re.finditer(r'"carousel_media":\s*\[', html_feed[sc_pos:next_code_pos]))
            if car_matches:
                cml_pos = sc_pos + car_matches[0].start()

        if cml_pos < 0:
            return []

        array_start = html_feed.find('[', cml_pos)
        if array_start < 0 or array_start > cml_pos + 30:
            return []

        bracket_count = 0
        in_string = False
        escape_next = False
        array_end = -1
        for i in range(array_start, min(len(html_feed), array_start + 200000)):
            ch = html_feed[i]
            if escape_next:
                escape_next = False
                continue
            if ch == '\\':
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '[':
                bracket_count += 1
            elif ch == ']':
                bracket_count -= 1
                if bracket_count == 0:
                    array_end = i + 1
                    break

        if array_end <= array_start:
            return []

        carousel_json = html_feed[array_start:array_end]
        carousel_json = carousel_json.replace('\\\\u0026', '&')
        carousel_json = carousel_json.replace('\\\\u002F', '/')
        carousel_json = carousel_json.replace('\\\\/', '/')
        carousel_json = carousel_json.replace('\\\\u003C', '<')
        carousel_json = carousel_json.replace('\\\\u003E', '>')
        carousel = json.loads(carousel_json)

        if not isinstance(carousel, list):
            return []

        carousel_urls = []
        for item in carousel:
            if not isinstance(item, dict):
                continue
            if item.get('video_versions'):
                best = max(item['video_versions'], key=lambda x: x.get('type', 0))
                url = best.get('url')
                if url:
                    carousel_urls.append(('video', url))
            elif item.get('image_versions2'):
                cands = item['image_versions2'].get('candidates', [])
                if cands:
                    best = max(cands, key=lambda x: x.get('width', 0) * x.get('height', 0))
                    url = best.get('url')
                    if url:
                        carousel_urls.append(('image', url))

        return carousel_urls
    except Exception as e:
        logger.warning(f"Threads feed carousel fallback error: {e}")
        return []


def _extract_threads_post_data(html, shortcode, username=None):
    """Extract post media from Googlebot-rendered Threads HTML."""
    code_pos = html.find(f'"code":"{shortcode}"')
    if code_pos < 0:
        return None

    # Find previous code to define backward search boundary
    prev_code_pos = html.rfind('"code":"', max(0, code_pos - 200000), code_pos)
    back_start = prev_code_pos + 10 if prev_code_pos >= 0 else max(0, code_pos - 200000)

    # Search for carousel_media — try backward first (carousel often before code), then forward
    carousel_pos = -1
    carousel_data = None

    car_matches_back = list(re.finditer(r'"carousel_media":\s*\[', html[back_start:code_pos]))
    if car_matches_back:
        carousel_pos = back_start + car_matches_back[-1].start()

    if carousel_pos < 0:
        fwd_car = html.find('"carousel_media":[', code_pos, code_pos + 200000)
        if fwd_car >= 0:
            snippet = html[fwd_car:fwd_car + 50]
            if not snippet.startswith('"carousel_media":null'):
                carousel_pos = fwd_car

    if carousel_pos >= 0:
        cb_start = html.find('[', carousel_pos)
        if cb_start >= 0:
            depth = 0
            in_str = False
            esc = False
            cb_end = -1
            for ci in range(cb_start, min(len(html), cb_start + 300000)):
                ch = html[ci]
                if esc:
                    esc = False
                    continue
                if ch == '\\':
                    esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        cb_end = ci
                        break
            if cb_end >= 0:
                carousel_json = html[cb_start:cb_end + 1].replace('\\/', '/')
                try:
                    carousel_data = json.loads(carousel_json)
                except:
                    carousel_data = None

    # Extract image and video from the post object
    # Try backward first, then forward
    img_pos = -1
    for search_start, search_end in [(back_start, code_pos), (code_pos, code_pos + 200000)]:
        p = html.find('"image_versions2"', search_start, search_end)
        if p >= 0:
            img_pos = p
            break

    img_url = ''
    if img_pos >= 0:
        img_bracket_start = html.find('[', img_pos)
        img_bracket_end = html.find(']', img_bracket_start)
        if img_bracket_start >= 0 and img_bracket_end >= 0:
            img_json = html[img_bracket_start:img_bracket_end + 1].replace('\\/', '/')
            try:
                imgs = json.loads(img_json)
                if imgs:
                    best_img = max(imgs, key=lambda x: x.get('width', 0) * x.get('height', 0))
                    img_url = best_img.get('url', '')
            except:
                pass

    vid_url = None
    vid_search_end = carousel_pos if carousel_pos > img_pos else img_pos + 80000
    for vs, ve in [(back_start, code_pos), (img_pos if img_pos >= 0 else code_pos, vid_search_end)]:
        vid_pos = html.find('"video_versions"', vs, ve)
        if vid_pos >= 0:
            vb_start = html.find('[', vid_pos)
            vb_end = html.find(']', vb_start)
            if vb_start >= 0 and vb_end >= 0:
                vid_json = html[vb_start:vb_end + 1].replace('\\/', '/')
                try:
                    vids = json.loads(vid_json)
                    if vids:
                        best_v = max(vids, key=lambda x: x.get('type', 0))
                        vid_url = best_v.get('url', '')
                except:
                    pass
            break

    carousel_urls = []
    if carousel_data:
        for item in carousel_data:
            if not isinstance(item, dict):
                continue
            if item.get('video_versions'):
                best = max(item['video_versions'], key=lambda x: x.get('type', 0))
                carousel_urls.append(('video', best.get('url', '')))
            elif item.get('image_versions2'):
                cands = item['image_versions2'].get('candidates', [])
                if cands:
                    best = max(cands, key=lambda x: x.get('width', 0) * x.get('height', 0))
                    carousel_urls.append(('image', best.get('url', '')))

    # If no carousel on post page, try to fetch from user's feed
    if not carousel_urls and username:
        carousel_urls = _get_carousel_from_feed(username, shortcode) or []

    next_code_pos = html.find('"code":"', code_pos + len(f'"code":"{shortcode}"'))
    if next_code_pos < 0:
        next_code_pos = code_pos + 100000

    caption = ''
    def _decode_unicode(m):
        code = int(m.group(1), 16)
        try:
            return chr(code)
        except (ValueError, OverflowError):
            return m.group(0)

    for search_start, search_end in [(back_start, code_pos), (code_pos, next_code_pos)]:
        cap_match = re.search(r'"caption"\s*:\s*\{"text"\s*:\s*"([^"]*)"', html[search_start:search_end])
        if cap_match:
            raw = cap_match.group(1)
            caption = re.sub(r'\\u([0-9a-fA-F]{4})', _decode_unicode, raw)
            caption = caption.replace('\\n', '\n').replace('\\/', '/').replace('\\"', '"')
            break

    return {
        'video_url': vid_url,
        'image_url': img_url,
        'caption': caption,
        'carousel': carousel_urls,
    }


def download_threads_post(url):
    """Download media from Threads post via Googlebot SSR (no browser needed)."""
    tmp_dir = os.path.join(DOWNLOAD_DIR, f"threads_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        url = _threads_resolve_url(url)
        resp = requests.get(url, headers={'User-Agent': GOOGLEBOT_UA}, timeout=20)
        html = resp.text

        og = re.search(r'og:url.*?content="https?://[^/]+/(@[^/]+)/post/([^"&]+)', html)
        if og:
            username = og.group(1)
            shortcode = og.group(2)
        else:
            sc_match = re.search(r'/post/([A-Za-z0-9_-]+)', url)
            shortcode = sc_match.group(1) if sc_match else None
            username_match = re.search(r'/@([^/]+)/post/', url)
            username = username_match.group(1) if username_match else None

        if not shortcode:
            return {'type': 'error', 'error': 'Не удалось определить пост'}

        data = _extract_threads_post_data(html, shortcode, username)

        if not data:
            return {'type': 'error', 'error': 'Медиа не найдено в посте (SSR)'}

        media_urls = []
        if data['carousel']:
            for mtype, murl in data['carousel']:
                if murl:
                    media_urls.append((mtype, murl))
        elif data['video_url']:
            media_urls.append(('video', data['video_url']))
        elif data['image_url']:
            media_urls.append(('image', data['image_url']))

        if not media_urls:
            return {'type': 'error', 'error': 'Медиа не найдено в посте'}

        caption_text = data['caption'] or 'Threads post'
        full_caption = f"{caption_text[:500]}\n\n📎 скачано с @saverdshot_bot"

        dl_headers = {
            'User-Agent': GOOGLEBOT_UA,
            'Referer': 'https://www.threads.com/',
        }

        files = []
        for i, (mtype, murl) in enumerate(media_urls[:10]):
            try:
                dr = requests.get(murl, headers=dl_headers, timeout=60, stream=True)
                if dr.status_code == 200:
                    ext = 'mp4' if mtype == 'video' else 'jpg'
                    filepath = os.path.join(tmp_dir, f'media_{i}.{ext}')
                    with open(filepath, 'wb') as f:
                        for chunk in dr.iter_content(chunk_size=65536):
                            f.write(chunk)
                    if os.path.getsize(filepath) > 0:
                        if ext == 'jpg':
                            with open(filepath, 'rb') as f:
                                header = f.read(12)
                            if b'ftyp' in header:
                                new_path = filepath.replace('.jpg', '.mp4')
                                os.rename(filepath, new_path)
                                filepath = new_path
                        files.append(filepath)
            except Exception as e:
                logger.error(f"Threads media download [{i}]: {e}")

        if not files:
            return {'type': 'error', 'error': 'Не удалось скачать медиа'}

        has_video = any(f.lower().endswith('.mp4') for f in files)
        if has_video and len(files) == 1:
            return {'type': 'video', 'files': files, 'caption': full_caption[:1024]}
        elif len(files) == 1:
            return {'type': 'photo', 'files': files, 'caption': full_caption[:1024]}
        else:
            return {'type': 'media_group', 'files': files, 'caption': full_caption[:1024]}

    except Exception as e:
        logger.error(f"Threads download error: {e}")
        return {'type': 'error', 'error': str(e)[:200]}


def download_pinterest(url):
    """Download photo or video from Pinterest."""
    tmp_dir = os.path.join(DOWNLOAD_DIR, f"pin_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        resp = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html',
        }, timeout=20, allow_redirects=True)
        if resp.status_code != 200:
            return {'type': 'error', 'error': f'HTTP {resp.status_code}'}
        html = resp.text

        # Try to extract video URL
        video_urls = re.findall(
            r'https?://v\d*\.pinimg\.com/videos/[^"\'>\s\\]+\.mp4(?:\?[^"\'>\s\\]*)?',
            html
        )
        if video_urls:
            video_url = video_urls[0]
            dr = requests.get(video_url, headers={
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
            }, timeout=60, stream=True)
            if dr.status_code == 200:
                fp = os.path.join(tmp_dir, 'pinterest_video.mp4')
                with open(fp, 'wb') as f:
                    for chunk in dr.iter_content(chunk_size=65536):
                        f.write(chunk)
                if os.path.getsize(fp) > 0:
                    return {'type': 'video', 'files': [fp], 'caption': ''}

        # Try to extract image URL (originals > 1200x > 720x)
        img_urls = sorted(set(re.findall(
            r'https://i\.pinimg\.com/originals/[^"\'>\s]+', html
        )))
        if not img_urls:
            img_urls = sorted(set(re.findall(
                r'https://i\.pinimg\.com/1200x/[^"\'>\s]+', html
            )))
        if not img_urls:
            img_urls = sorted(set(re.findall(
                r'https://i\.pinimg\.com/720x/[^"\'>\s]+', html
            )))

        # Filter out CSS/icon URLs
        img_urls = [u for u in img_urls if not u.endswith(('.svg', '.ico')) and '/140x140_RS/' not in u and '/136x136/' not in u]

        if img_urls:
            img_url = img_urls[0]
            dr = requests.get(img_url, headers={
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
            }, timeout=60, stream=True)
            if dr.status_code == 200:
                ext = 'png' if img_url.endswith('.png') else 'jpg'
                fp = os.path.join(tmp_dir, f'pinterest_image.{ext}')
                with open(fp, 'wb') as f:
                    for chunk in dr.iter_content(chunk_size=65536):
                        f.write(chunk)
                if os.path.getsize(fp) > 0:
                    return {'type': 'photo', 'files': [fp], 'caption': ''}

        return {'type': 'error', 'error': 'Медиа не найдено'}

    except Exception as e:
        logger.error(f"Pinterest download error: {e}")
        return {'type': 'error', 'error': str(e)[:200]}


def download_youtube_cobalt(url, audio_only=False):
    return None


INNERTUBE_CLIENTS = [
    {
        'clientName': 'ANDROID',
        'clientVersion': '19.09.37',
        'androidSdkVersion': 30,
        'hl': 'en',
        'gl': 'US',
        'userAgent': 'com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip',
        'api_key': 'AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w',
    },
    {
        'clientName': 'ANDROID_VR',
        'clientVersion': '1.57.29',
        'androidSdkVersion': 30,
        'hl': 'en',
        'gl': 'US',
        'userAgent': 'com.google.android.apps.youtube.vr.oculus/1.57.29 (Linux; U; Android 12; eureka-user Build/SQ3A.220605.009.A1) gzip',
        'api_key': 'AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w',
    },
    {
        'clientName': 'IOS',
        'clientVersion': '19.09.3',
        'deviceModel': 'iPhone14,3',
        'hl': 'en',
        'gl': 'US',
        'userAgent': 'com.google.ios.youtube/19.09.3 (iPhone14,3; U; CPU iOS 15_6 like Mac OS X)',
        'api_key': 'AIzaSyB-63vPrdThhKuerbB2N_l7Kwwcxj6yUAc',
    },
    {
        'clientName': 'TVHTML5_SIMPLY_EMBEDDED_PLAYER',
        'clientVersion': '2.0',
        'hl': 'en',
        'gl': 'US',
        'userAgent': 'Mozilla/5.0',
        'api_key': 'AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w',
    },
]


def _extract_youtube_id(url):
    m = re.search(r'(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})', url)
    if m:
        return m.group(1)
    m = re.search(r'([a-zA-Z0-9_-]{11})', url)
    if m:
        return m.group(1)
    return None


def download_youtube_innertube(url, audio_only=False, quality=None):
    """Download YouTube via innertube API — bypasses yt-dlp 403."""
    vid_id = _extract_youtube_id(url)
    if not vid_id:
        return None

    for client_cfg in INNERTUBE_CLIENTS:
        try:
            payload = {
                'videoId': vid_id,
                'context': {
                    'client': {
                        'clientName': client_cfg['clientName'],
                        'clientVersion': client_cfg['clientVersion'],
                        'hl': client_cfg.get('hl', 'en'),
                        'gl': client_cfg.get('gl', 'US'),
                    },
                },
                'contentCheckOk': True,
                'racyCheckOk': True,
            }
            if 'androidSdkVersion' in client_cfg:
                payload['context']['client']['androidSdkVersion'] = client_cfg['androidSdkVersion']
            if 'deviceModel' in client_cfg:
                payload['context']['client']['deviceModel'] = client_cfg['deviceModel']

            resp = requests.post(
                f'https://www.youtube.com/youtubei/v1/player?key={client_cfg["api_key"]}',
                json=payload,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': client_cfg['userAgent'],
                    'X-YouTube-Client-Name': '3' if 'ANDROID' in client_cfg['clientName'] else '5',
                    'X-YouTube-Client-Version': client_cfg['clientVersion'],
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"Innertube {client_cfg['clientName']} HTTP {resp.status_code}")
                continue

            data = resp.json()
            status = data.get('playabilityStatus', {})
            if status.get('status') != 'OK':
                logger.warning(f"Innertube {client_cfg['clientName']} status: {status.get('status')} - {status.get('reason', '')[:100]}")
                continue

            title = data.get('videoDetails', {}).get('title', '')
            streaming = data.get('streamingData', {})
            formats = streaming.get('formats', []) + streaming.get('adaptiveFormats', [])

            if not formats:
                logger.warning(f"Innertube {client_cfg['clientName']}: no formats")
                continue

            if audio_only:
                audio_fmts = [f for f in formats if f.get('mimeType', '').startswith('audio/')]
                if not audio_fmts:
                    logger.warning(f"Innertube {client_cfg['clientName']}: no audio formats")
                    continue
                audio_fmts.sort(key=lambda f: f.get('bitrate', 0), reverse=True)
                chosen = audio_fmts[0]
            else:
                video_fmts = [f for f in formats if f.get('mimeType', '').startswith('video/')]
                if quality:
                    h = int(quality.replace('p', ''))
                    video_fmts = [f for f in video_fmts if f.get('height', 0) <= h]
                if not video_fmts:
                    video_fmts = [f for f in formats if f.get('mimeType', '').startswith('video/')]
                if not video_fmts:
                    logger.warning(f"Innertube {client_cfg['clientName']}: no video formats")
                    continue
                video_fmts.sort(key=lambda f: (f.get('height', 0), f.get('bitrate', 0)), reverse=True)
                chosen = video_fmts[0]

            stream_url = chosen.get('url')
            if not stream_url:
                sig = chosen.get('signatureCipher', '')
                if sig:
                    logger.warning(f"Innertube {client_cfg['clientName']}: needs cipher, skipping")
                    continue
                logger.warning(f"Innertube {client_cfg['clientName']}: no url")
                continue

            tmp_dir = os.path.join(DOWNLOAD_DIR, f"yt_{uuid.uuid4().hex[:8]}")
            os.makedirs(tmp_dir, exist_ok=True)
            mime = chosen.get('mimeType', '')
            ext = 'mp3' if audio_only else ('mp4' if 'video' in mime else 'webm')
            if audio_only and 'webm' in mime:
                ext = 'webm'
            filepath = os.path.join(tmp_dir, f"{vid_id}.{ext}")

            ua = client_cfg['userAgent']
            dr = requests.get(stream_url, timeout=300, stream=True, headers={'User-Agent': ua})
            if dr.status_code != 200:
                logger.warning(f"Innertube {client_cfg['clientName']} download HTTP {dr.status_code}")
                continue
            with open(filepath, 'wb') as f:
                for chunk in dr.iter_content(chunk_size=65536):
                    f.write(chunk)
            if os.path.getsize(filepath) > 0:
                logger.info(f"Innertube {client_cfg['clientName']} download success: {filepath}")
                return filepath, title
            os.remove(filepath)
        except Exception as e:
            logger.error(f"Innertube {client_cfg['clientName']} error: {e}")
            continue

    return None


def download_video(task, msg_ref, loop, queue=None):
    url = task.url
    quality = task.quality
    platform = detect_platform(url)
    url_type = detect_url_type(url)
    max_retries = 2
    ffmpeg_path = get_ffmpeg_path()

    for attempt in range(max_retries + 1):
        opts = make_ydl_opts()

        if platform == 'youtube':
            opts['skip_download'] = True
        elif platform == 'vk':
            if quality:
                h = int(quality.replace('p', ''))
                opts['format'] = f'best[format_id~=url][height<={h}]/best[format_id~=url]/best[ext=mp4][height<={h}]/best'
            else:
                opts['format'] = 'best[format_id~=url]/best[ext=mp4]/best'
        elif platform == 'instagram':
            cookies_file = get_instagram_cookiefile()
            if cookies_file:
                opts['cookiefile'] = cookies_file
            opts['format'] = quality or 'b/best'
        elif platform == 'tiktok':
            opts['format'] = quality or 'best'
        else:
            opts['format'] = quality or 'best'

        opts['socket_timeout'] = 30
        opts['extractor_retries'] = 3

        title = ''
        info = None
        tracker = ProgressTracker(task, msg_ref, loop, title='', queue=queue)
        opts['progress_hooks'] = [tracker.hook]

        try:
            if platform == 'youtube':
                vid_id = None
                opts['extractor_args'] = {'youtube': {'player_client': ['android_vr']}}

                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        return {'error': 'Не удалось получить информацию'}
                    title = info.get('title', '')
                    vid_id = info.get('id', '')
                tracker.title = title[:80] if title else ''
                tracker.title = title[:40] if title else ''

                dl_dir = DOWNLOAD_DIR

                ffmpeg_location = get_ffmpeg_location()

                is_short = '/shorts/' in url
                dl_dir = DOWNLOAD_DIR

                if is_short:
                    dl_format = 'best[height<=1080][ext=mp4][vcodec!=none][acodec!=none]/best[height<=1080][ext=mp4]/best[height<=1080]/best'
                elif quality:
                    h = int(quality.replace('p', ''))
                    dl_format = f'best[height<={h}][ext=mp4][vcodec!=none][acodec!=none]/best[height<={h}][ext=mp4]/best[height<={h}]/best'
                else:
                    dl_format = 'best[ext=mp4][vcodec!=none][acodec!=none]/best[ext=mp4]/best'

                ffmpeg_location = get_ffmpeg_location()
                download_clients = [None, ['android_vr'], ['web'], ['mweb'], ['android'], ['ios'], ['tv'], ['tv_embedded'], ['mediaconnect']]
                proxy = get_youtube_proxy()
                last_err = None
                for cli_idx, client in enumerate(download_clients):
                    try:
                        dl_opts = {
                            'format': dl_format,
                            'outtmpl': f'{dl_dir}/{vid_id}.%(ext)s',
                            'noplaylist': True,
                            'quiet': True,
                            'no_warnings': True,
                            'socket_timeout': 30,
                            'progress_hooks': [tracker.hook],
                        }
                        if client:
                            dl_opts['extractor_args'] = {'youtube': {'player_client': client}}
                        if proxy:
                            dl_opts['proxy'] = proxy
                        if ffmpeg_location:
                            dl_opts['ffmpeg_location'] = ffmpeg_location
                        with yt_dlp.YoutubeDL(dl_opts) as ydl:
                            ydl.download([url])
                        break
                    except Exception as dl_err:
                        last_err = dl_err
                        logger.warning(f"YouTube client {client} failed: {dl_err}")
                        if cli_idx == len(download_clients) - 1:
                            logger.warning("All yt-dlp clients failed, trying innertube API...")
                            innertube_result = download_youtube_innertube(url, audio_only=False, quality=quality)
                            if innertube_result:
                                fn, title = innertube_result
                                if fn.endswith('.webm'):
                                    remuxed = fn.rsplit('.', 1)[0] + '.mp4'
                                    if ffmpeg_path and (os.path.exists(ffmpeg_path) or shutil.which('ffmpeg')):
                                        proc = subprocess.run(
                                            [ffmpeg_path, '-y', '-i', fn, '-c:v', 'libx264', '-c:a', 'aac', '-movflags', '+faststart', remuxed],
                                            capture_output=True, text=True, timeout=300
                                        )
                                        if proc.returncode == 0 and os.path.exists(remuxed) and os.path.getsize(remuxed) > 0:
                                            os.remove(fn)
                                            fn = remuxed
                                tracker.done()
                                return {
                                    'filename': fn,
                                    'title': title,
                                    'uploader': (info.get('uploader', '') or info.get('channel', '')) if info else '',
                                    'filesize': os.path.getsize(fn),
                                    'description': ((info.get('description', '') or '')[:1000] if info else ''),
                                    'audio_only': False,
                                    'duration': (info.get('duration', 0) if info else 0),
                                }
                            raise
                        continue
                else:
                    raise last_err
                tracker.done()

                fn_list = glob.glob(os.path.join(dl_dir, f'{vid_id}.*'))
                if not fn_list:
                    return {'error': 'Файл не найден'}
                fn = fn_list[0]
                if fn.endswith('.mp4'):
                    fn = remux_mp4(fn)
            else:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    tracker.done()
                    if not info:
                        return {'error': 'Не удалось получить информацию'}
                    title = info.get('title', '')
                    task.title = title[:80] if title else ''
                    vid_id = info.get('id', '')
                    fn_list = glob.glob(os.path.join(DOWNLOAD_DIR, f'{vid_id}.*'))
                    if not fn_list:
                        return {'error': 'Файл не найден'}
                    fn = fn_list[0]

            sz = os.path.getsize(fn)
            if sz == 0:
                os.remove(fn)
                return {'error': 'Файл пустой'}
            thumb = extract_thumbnail(fn)
            desc = info.get('description', '') if info else ''
            uploader = (info.get('uploader', '') or info.get('channel', '')) if info else ''
            return {
                'filename': fn,
                'title': title or 'Видео',
                'uploader': uploader,
                'filesize': sz,
                'description': (desc or '')[:1000],
                'thumb': thumb,
            }
        except Exception as e:
            tracker.done()
            msg = str(e)
            if 'прервано' in msg.lower() or 'aborted' in msg.lower():
                if attempt < max_retries:
                    import glob as g
                    old_files = g.glob(os.path.join(DOWNLOAD_DIR, '*'))
                    for f in old_files:
                        try:
                            os.remove(f)
                        except Exception:
                            pass
                    continue
                return {'error': 'Скачивание прервано: медленная скорость. Попробуйте позже или другое качество'}
            if 'Private' in msg or "isn't available" in msg:
                return {'error': 'Видео приватное или недоступно'}
            if 'needs to be reloaded' in msg or '403' in msg or 'Forbidden' in msg:
                return {'error': f'YouTube блокирует запросы с этого сервера (HTTP 403). Попробуйте через прокси/VPN или другой сервер.'}
            if 'timed out' in msg.lower():
                return {'error': 'Таймаут соединения'}
            return {'error': f'Ошибка: {msg[:200]}'}

    return {'error': 'Не удалось скачать после повторных попыток'}


def download_video_raw(url, quality=None):
    opts = make_ydl_opts(quality=quality)
    platform = detect_platform(url)
    if platform == 'youtube':
        opts['extractor_args'] = {'youtube': {'player_client': ['android_vr']}}
        opts['format'] = 'best[ext=mp4][vcodec!=none][acodec!=none]/best[ext=mp4]/best'
    elif platform == 'instagram':
        cookies_file = get_instagram_cookiefile()
        if cookies_file:
            opts['cookiefile'] = cookies_file
        opts['format'] = quality or 'best'
    else:
        opts['format'] = quality or 'best'

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None, 'No info'
            vid = info.get('id', '')
            files = glob.glob(os.path.join(DOWNLOAD_DIR, f'{vid}.*'))
            if not files:
                return None, 'File not found'
            fn = files[0]
            if fn.endswith('.mp4'):
                fn = remux_mp4(fn)
            return fn, None
    except Exception as e:
        return None, str(e)[:200]


def remux_mp4(filepath):
    """Re-mux mp4 with faststart and correct aspect ratio for Telegram."""
    if not filepath.endswith('.mp4'):
        return filepath
    if not shutil.which('ffmpeg') and not os.path.exists(os.path.join(BOT_DIR, 'ffmpeg')):
        return filepath
    out = filepath + '.fixed.mp4'
    try:
        ffmpeg_path = get_ffmpeg_path()
        proc = subprocess.run(
            [ffmpeg_path, '-y', '-i', filepath, '-c', 'copy',
             '-movflags', '+faststart', out],
            capture_output=True, timeout=120
        )
        if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            os.replace(out, filepath)
            return filepath
        else:
            logger.error(f"remux failed: {proc.stderr[:200]}")
            if os.path.exists(out):
                os.remove(out)
            return filepath
    except Exception as e:
        logger.error(f"remux error: {e}")
        if os.path.exists(out):
            os.remove(out)
        return filepath


def extract_thumbnail(filepath):
    """Extract thumbnail from video for Telegram preview."""
    if not filepath.lower().endswith(('.mp4', '.mov', '.webm', '.mkv')):
        return None
    if not shutil.which('ffmpeg') and not os.path.exists(os.path.join(BOT_DIR, 'ffmpeg')):
        return None
    thumb_path = filepath + '.thumb.jpg'
    try:
        ffmpeg_path = get_ffmpeg_path()
        proc = subprocess.run(
            [ffmpeg_path, '-y', '-i', filepath, '-ss', '00:00:01', '-vframes', '1',
             '-vf', 'scale=320:-1', thumb_path],
            capture_output=True, timeout=30
        )
        if proc.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        return None
    except Exception as e:
        logger.error(f"extract_thumbnail error: {e}")
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
        return None


def extract_video_dimensions(filepath):
    for tool in ['ffprobe', 'ffmpeg']:
        tool_path = shutil.which(tool)
        if not tool_path:
            bundled = os.path.join(BOT_DIR, tool)
            if os.path.exists(bundled) and os.access(bundled, os.X_OK):
                tool_path = bundled
        if not tool_path:
            continue
        try:
            if tool == 'ffprobe':
                r = subprocess.run(
                    [tool_path, '-v', 'error', '-select_streams', 'v:0',
                     '-show_entries', 'stream=width,height',
                     '-of', 'csv=p=0', filepath],
                    capture_output=True, text=True, timeout=10
                )
                parts = r.stdout.strip().split(',')
                if len(parts) == 2:
                    return int(parts[0]), int(parts[1])
            else:
                r = subprocess.run([tool_path, '-i', filepath], capture_output=True, text=True, timeout=10)
                for line in r.stderr.split('\n'):
                    m = re.search(r'(\d+)x(\d+)', line)
                    if m:
                        return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
    return None, None


def format_size(b):
    if b < 1024 * 1024:
        return f"{b / 1024:.0f}KB"
    return f"{b / 1024 / 1024:.1f}MB"


def upload_to_hosting(filepath, progress_cb=None):
    try:
        sz = os.path.getsize(filepath)
        logger.info(f"Uploading to tmpfiles.org: {filepath} ({sz / 1024 / 1024:.1f}MB)")
        timeout = max(600, sz // (100 * 1024))

        with open(filepath, 'rb') as f:
            resp = requests.post(
                'https://tmpfiles.org/api/v1/upload',
                files={'file': f},
                timeout=timeout
            )
        logger.info(f"tmpfiles response: {resp.status_code} {resp.text[:200]}")
        if resp.status_code == 200:
            data = resp.json()
            if data.get('status') == 'success':
                page_url = data['data']['url']
                dl_url = page_url.replace('tmpfiles.org/', 'tmpfiles.org/dl/')
                logger.info(f"Upload success: {dl_url}")
                return dl_url
        logger.error(f"Upload failed: {resp.status_code} {resp.text[:200]}")
        return None
    except requests.exceptions.Timeout:
        logger.error("Upload timeout to tmpfiles.org")
        return None
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return None


def build_queue_text(queue):
    if not queue:
        return ''
    lines = ['📋 <b>Очередь:</b>']
    for i, task in enumerate(queue, 1):
        if task.status == 'downloading':
            icon = '⬇️'
            elapsed = ''
            if task.start_time:
                elapsed_sec = int(time.time() - task.start_time)
                m, s = divmod(elapsed_sec, 60)
                elapsed = f' ({m}:{s:02d})' if m else f' ({s}с)'
            status = task.progress if task.progress else f'загружается...{elapsed}'
        elif task.status == 'done':
            icon = '✅'
            status = 'готово'
        elif task.status == 'error':
            err = task.result.get('error', 'ошибка')[:30] if task.result else 'ошибка'
            icon = '❌'
            status = err
        else:
            icon = '⏳'
            status = 'ожидание'
        platform = detect_platform(task.url)
        picon = {'youtube': '▶️', 'vk': 'VK', 'instagram': 'IG', 'tiktok': '🎵', 'ok': 'OK', 'rutube': 'RT'}.get(platform, '🔗')
        title = task.title[:50] if task.title else url_to_title(task.url)
        lines.append(f"{icon} {i}. {picon} {title} — {status}")
    return '\n'.join(lines)


async def process_queue(chat_id, bot, loop):
    lock = user_locks.get(chat_id)
    if not lock:
        return
    async with lock:
        queue = user_queues.get(chat_id, [])
        status_msg = None

        async def update_status(text):
            nonlocal status_msg
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                status_msg = None
            if not text:
                return
            try:
                status_msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
            except Exception:
                pass

        while queue:
            task = queue[0]
            if task.status != 'waiting':
                queue.pop(0)
                continue

            await update_status(build_queue_text(queue))

            url_type = detect_url_type(task.url)

            if url_type == 'broken':
                task.status = 'error'
                task.result = {'error': 'OK.ru не поддерживается'}
                queue.pop(0)
                await update_status(build_queue_text(queue))
                continue

            try:
                if url_type == 'tt_carousel' or detect_platform(task.url) == 'tiktok':
                    task.status = 'downloading'
                    task.start_time = time.time()
                    await update_status(build_queue_text(queue))

                    tt_result = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: download_tt_media(task.url)
                    )

                    if tt_result.get('type') == 'video':
                        fn = tt_result['filename']
                        try:
                            is_high = tt_result.get('filesize', 0) > 50 * 1024 * 1024
                            if is_high:
                                link = await asyncio.get_event_loop().run_in_executor(
                                    None, lambda: upload_to_hosting(fn)
                                )
                                if link:
                                    text = (
                                        f"🎬 <b>{tt_result.get('title', '')}</b>\n\n"
                                        f"⬇️ <a href=\"{link}\">Скачать видео</a>\n\n"
                                        f"<i>Открой ссылку → нажми ⋮ → «Загрузить»</i>\n"
                                        f"📎 скачано с @saverdshot_bot"
                                    )
                                    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                                else:
                                    await bot.send_message(chat_id, f"❌ Не удалось загрузить на хостинг.{ERROR_SUFFIX}")
                            else:
                                video_file = FSInputFile(fn)
                                w, h = extract_video_dimensions(fn)
                                send_kwargs = dict(
                                    chat_id=chat_id, video=video_file,
                                    caption=f"🎬 {tt_result.get('title', '')[:80]}"
                                )
                                if w and h:
                                    send_kwargs['width'] = w
                                    send_kwargs['height'] = h
                                await bot.send_video(**send_kwargs)
                        except Exception as e:
                            await bot.send_message(chat_id, f"❌ Ошибка: {str(e)[:100]}{ERROR_SUFFIX}")
                        finally:
                            if os.path.exists(fn):
                                os.remove(fn)

                    elif tt_result.get('type') == 'carousel':
                        photos = tt_result.get('photos', [])
                        caption = tt_result.get('caption', '')
                        if photos:
                            task.status = 'done'
                            task.result = {'photos': photos}
                            caption_text = caption.strip() if caption else ''
                            for i, fpath in enumerate(photos):
                                try:
                                    cap = caption_text[:1024] if (i == 0 and caption_text) else None
                                    is_video = fpath.lower().endswith(('.mp4', '.mov', '.webm'))
                                    fobj = FSInputFile(fpath)
                                    if is_video:
                                        await bot.send_video(chat_id=chat_id, video=fobj, caption=cap)
                                    else:
                                        await bot.send_photo(chat_id=chat_id, photo=fobj, caption=cap)
                                    if i < len(photos) - 1:
                                        await asyncio.sleep(0.5)
                                except Exception as e:
                                    logger.error(f"TT carousel send [{i}] error: {e}")
                                    await asyncio.sleep(1)
                        else:
                            raise RuntimeError('Не удалось извлечь медиа из TikTok')
                    else:
                        raise RuntimeError(tt_result.get('error', 'Не удалось извлечь медиа из TikTok'))

                    queue.pop(0)
                    await update_status(build_queue_text(queue))
                    continue

                if detect_platform(task.url) == 'x':
                    task.status = 'downloading'
                    task.start_time = time.time()
                    await update_status(build_queue_text(queue))
                    x_result = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: download_x_media(task.url)
                    )
                    if x_result.get('type') in ('video', 'photos'):
                        files = x_result.get('files', [])
                        caption = x_result.get('caption', '')
                        photos = [f for f in files if not f.lower().endswith(('.mp4', '.webm', '.mov'))]
                        videos = [f for f in files if f.lower().endswith(('.mp4', '.webm', '.mov'))]
                        if photos:
                            try:
                                media_group = []
                                for i, fpath in enumerate(photos):
                                    fobj = FSInputFile(fpath)
                                    from aiogram.types import InputMediaPhoto
                                    cap = caption[:1024] if (i == 0 and caption) else None
                                    media_group.append(InputMediaPhoto(media=fobj, caption=cap))
                                if media_group:
                                    await bot.send_media_group(chat_id=chat_id, media=media_group)
                            except Exception as e:
                                logger.error(f"X photos album error: {e}")
                                for fpath in photos:
                                    try:
                                        fobj = FSInputFile(fpath)
                                        await bot.send_photo(chat_id=chat_id, photo=fobj, caption=caption[:1024] if caption else None)
                                        await asyncio.sleep(0.5)
                                    except Exception:
                                        pass
                        for fpath in videos:
                            try:
                                fobj = FSInputFile(fpath)
                                w, h = extract_video_dimensions(fpath)
                                send_kwargs = dict(chat_id=chat_id, video=fobj, caption=caption[:1024] if caption else None)
                                if w and h:
                                    send_kwargs['width'] = w
                                    send_kwargs['height'] = h
                                await bot.send_video(**send_kwargs)
                            except Exception as e:
                                logger.error(f"X video send error: {e}")
                        task.status = 'done'
                        task.result = {'photos': files}
                    else:
                        raise RuntimeError(x_result.get('error', 'Не удалось скачать из X/Twitter'))

                    queue.pop(0)
                    await update_status(build_queue_text(queue))
                    continue

                if detect_platform(task.url) == 'threads':
                    task.status = 'downloading'
                    task.start_time = time.time()
                    await update_status(build_queue_text(queue))
                    threads_result = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: download_threads_post(task.url)
                    )
                    if threads_result.get('type') in ('video', 'photo', 'media_group'):
                        files = threads_result.get('files', [])
                        caption = threads_result.get('caption', '')
                        sent_ok = False
                        if threads_result.get('type') == 'media_group' and len(files) > 1:
                            try:
                                from aiogram.types import InputMediaPhoto, InputMediaVideo
                                media_group = []
                                for i, fpath in enumerate(files[:10]):
                                    is_video = fpath.lower().endswith(('.mp4', '.webm', '.mov'))
                                    fobj = FSInputFile(fpath)
                                    if is_video:
                                        media_group.append(InputMediaVideo(media=fobj, caption=caption[:1024] if i == 0 else None))
                                    else:
                                        media_group.append(InputMediaPhoto(media=fobj, caption=caption[:1024] if i == 0 else None))
                                await bot.send_media_group(chat_id=chat_id, media=media_group)
                                sent_ok = True
                            except Exception as e:
                                logger.error(f"Threads media_group send error: {e}")
                        if not sent_ok:
                            for i, fpath in enumerate(files):
                                try:
                                    is_video = fpath.lower().endswith(('.mp4', '.webm', '.mov'))
                                    fobj = FSInputFile(fpath)
                                    cap = caption[:1024] if (i == 0 and caption) else None
                                    if is_video:
                                        w, h = extract_video_dimensions(fpath)
                                        send_kwargs = dict(chat_id=chat_id, video=fobj, caption=cap)
                                        if w and h:
                                            send_kwargs['width'] = w
                                            send_kwargs['height'] = h
                                        await bot.send_video(**send_kwargs)
                                    else:
                                        await bot.send_photo(chat_id=chat_id, photo=fobj, caption=cap)
                                    if i < len(files) - 1:
                                        await asyncio.sleep(0.5)
                                except Exception as e:
                                    logger.error(f"Threads send [{i}] error: {e}")
                        task.status = 'done'
                        task.result = {'photos': files}
                    else:
                        raise RuntimeError(threads_result.get('error', 'Не удалось скачать из Threads'))

                    queue.pop(0)
                    await update_status(build_queue_text(queue))
                    continue

                if url_type == 'pinterest':
                    task.status = 'downloading'
                    task.start_time = time.time()
                    await update_status(build_queue_text(queue))

                    pin_result = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: download_pinterest(task.url)
                    )

                    if pin_result.get('type') in ('video', 'photo'):
                        files = pin_result.get('files', [])
                        cap = '📎 скачано с @saverdshot_bot'
                        for i, fpath in enumerate(files):
                            try:
                                is_video = fpath.lower().endswith(('.mp4', '.mov', '.webm'))
                                fobj = FSInputFile(fpath)
                                if is_video:
                                    w, h = extract_video_dimensions(fpath)
                                    send_kwargs = dict(chat_id=chat_id, video=fobj, caption=cap)
                                    if w and h:
                                        send_kwargs['width'] = w
                                        send_kwargs['height'] = h
                                    await bot.send_video(**send_kwargs)
                                else:
                                    await bot.send_photo(chat_id=chat_id, photo=fobj, caption=cap)
                            except Exception as e:
                                logger.error(f"Pinterest send error: {e}")
                        task.status = 'done'
                        task.result = {'photos': files}
                    else:
                        raise RuntimeError(pin_result.get('error', 'Не удалось скачать из Pinterest'))

                    queue.pop(0)
                    await update_status(build_queue_text(queue))
                    continue

                if url_type == 'ig_post':
                    task.status = 'downloading'
                    task.start_time = time.time()
                    await update_status(build_queue_text(queue))

                    photos, caption, ig_tmp_dir = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: download_ig_post(task.url)
                    )

                    if photos:
                        task.status = 'done'
                        task.result = {'photos': photos}
                        caption_text = caption.strip() if caption else ''
                        cap_parts = []
                        if caption_text:
                            cap_parts.append(caption_text[:800])
                        cap_parts.append('📎 скачано с @saverdshot_bot')
                        cap = '\n\n'.join(cap_parts)[:1024]

                        sent_ok = False
                        if len(photos) > 1:
                            try:
                                from aiogram.types import InputMediaPhoto, InputMediaVideo
                                media_group = []
                                for i, fpath in enumerate(photos[:10]):
                                    is_video = fpath.lower().endswith(('.mp4', '.mov', '.webm'))
                                    fobj = FSInputFile(fpath)
                                    if is_video:
                                        media_group.append(InputMediaVideo(media=fobj, caption=cap if i == 0 else None))
                                    else:
                                        media_group.append(InputMediaPhoto(media=fobj, caption=cap if i == 0 else None))
                                await bot.send_media_group(chat_id=chat_id, media=media_group)
                                sent_ok = True
                            except Exception as e:
                                logger.error(f"IG media_group send error: {e}")
                        if not sent_ok:
                            for i, fpath in enumerate(photos):
                                try:
                                    is_video = fpath.lower().endswith(('.mp4', '.mov', '.webm'))
                                    fobj = FSInputFile(fpath)
                                    if is_video:
                                        w, h = extract_video_dimensions(fpath)
                                        send_kwargs = dict(chat_id=chat_id, video=fobj, caption=cap)
                                        if w and h:
                                            send_kwargs['width'] = w
                                            send_kwargs['height'] = h
                                        await bot.send_video(**send_kwargs)
                                    else:
                                        await bot.send_photo(chat_id=chat_id, photo=fobj, caption=cap)
                                    if i < len(photos) - 1:
                                        await asyncio.sleep(0.5)
                                except Exception as e:
                                    logger.error(f"IG send [{i}] error: {e}")
                                    await asyncio.sleep(1)
                    else:
                        if not get_instagram_cookiefile():
                            raise RuntimeError('Instagram требует cookies. Обновите cookies.txt')
                        raise RuntimeError('Instagram: не удалось загрузить пост. Возможно, cookies устарели или пост приватный. Попробуйте обновить cookies.txt')

                    queue.pop(0)
                    await update_status(build_queue_text(queue))
                    continue

                task.status = 'downloading'
                task.start_time = time.time()
                await update_status(build_queue_text(queue))

                msg_ref = [status_msg]
                try:
                    result = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, lambda: download_video(task, msg_ref, loop, queue=queue)
                        ),
                        timeout=300
                    )
                except asyncio.TimeoutError:
                    result = {'error': 'Таймаут загрузки (5 мин)'}

                task.result = result

                if 'error' in result:
                    task.status = 'error'
                    await bot.send_message(chat_id, f"❌ {result['error']}{ERROR_SUFFIX}")
                else:
                    task.status = 'done'
                    task.filename = result['filename']

                    try:
                        is_youtube = detect_platform(task.url) == 'youtube'
                        is_vk = detect_platform(task.url) == 'vk'
                        is_short = '/shorts/' in task.url if is_youtube else False
                        is_high_quality = result['filesize'] > 50 * 1024 * 1024

                        if is_high_quality:
                            link = await asyncio.get_event_loop().run_in_executor(
                                None, lambda: upload_to_hosting(result['filename'])
                            )
                            if link:
                                text = (
                                    f"🎬 <b>{result.get('title', '')}</b>\n\n"
                                    f"⬇️ <a href=\"{link}\">Скачать видео</a>\n\n"
                                    f"<i>Открой ссылку → нажми ⋮ → «Загрузить»</i>\n"
                                    f"📎 скачано с @saverdshot_bot"
                                )
                                await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                            else:
                                await bot.send_message(chat_id, f"❌ Не удалось загрузить на хостинг. Попробуйте позже.{ERROR_SUFFIX}")
                        elif is_short:
                            video_file = FSInputFile(result['filename'])
                            w, h = extract_video_dimensions(result['filename'])
                            send_kwargs = dict(
                                chat_id=chat_id, video=video_file,
                                caption=f"🎬 {result.get('title', '')[:80]}"
                            )
                            if w and h:
                                send_kwargs['width'] = w
                                send_kwargs['height'] = h
                            await bot.send_video(**send_kwargs)
                        elif is_youtube or is_vk:
                            video_file = FSInputFile(result['filename'])
                            w, h = extract_video_dimensions(result['filename'])
                            cap = f"🎬 {result.get('title', '')[:80]}\n\n📎 скачано с @saverdshot_bot"
                            send_kwargs = dict(
                                chat_id=chat_id, video=video_file,
                                caption=cap
                            )
                            if w and h:
                                send_kwargs['width'] = w
                                send_kwargs['height'] = h
                            await bot.send_video(**send_kwargs)
                        else:
                            desc = result.get('description', '').strip()
                            cap_parts = []
                            if result.get('uploader'):
                                cap_parts.append(f"👤 {result['uploader']}")
                            if desc:
                                cap_parts.append(desc[:800])
                            cap_parts.append(f"📎 скачано с @saverdshot_bot")
                            cap = '\n\n'.join(cap_parts)

                            if is_high_quality:
                                link = await asyncio.get_event_loop().run_in_executor(
                                    None, lambda: upload_to_hosting(result['filename'])
                                )
                                if link:
                                    text = (
                                        f"🎬 <b>{result['title']}</b>\n\n"
                                        f"⬇️ <a href=\"{link}\">Скачать видео</a>\n\n"
                                        f"<i>Открой ссылку → нажми ⋮ → «Загрузить»</i>\n"
                                        f"📎 скачано с @saverdshot_bot"
                                    )
                                    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                                else:
                                    await bot.send_message(chat_id, f"❌ Не удалось загрузить на хостинг. Попробуйте позже.{ERROR_SUFFIX}")
                            else:
                                video_file = FSInputFile(result['filename'])
                                thumb_file = FSInputFile(result['thumb']) if result.get('thumb') else None
                                w, h = extract_video_dimensions(result['filename'])
                                send_kwargs = dict(chat_id=chat_id, video=video_file, caption=cap[:1024], thumbnail=thumb_file)
                                if w and h:
                                    send_kwargs['width'] = w
                                    send_kwargs['height'] = h
                                await bot.send_video(**send_kwargs)
                    except Exception as e:
                        task.status = 'error'
                        task.result = {'error': f'Ошибка отправки: {str(e)[:200]}'}
                        await bot.send_message(chat_id, f"❌ Ошибка: {str(e)[:100]}{ERROR_SUFFIX}")
                    finally:
                        if os.path.exists(result['filename']):
                            os.remove(result['filename'])
                        if result.get('thumb') and os.path.exists(result['thumb']):
                            os.remove(result['thumb'])
                queue.pop(0)
                await update_status(build_queue_text(queue))
                if queue:
                    await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"Queue worker error for chat {chat_id}: {e}")
                task.status = 'error'
                task.result = {'error': f'Внутренняя ошибка: {str(e)[:200]}'}
                try:
                    await bot.send_message(chat_id, f"❌ {task.result['error']}{ERROR_SUFFIX}")
                except Exception:
                    pass
                queue.pop(0)
                await update_status(build_queue_text(queue))

        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        user_queues.pop(chat_id, None)


def ensure_queue_worker(chat_id, bot, loop):
    worker = queue_workers.get(chat_id)
    if worker and not worker.done():
        return worker

    worker = asyncio.create_task(process_queue(chat_id, bot, loop))
    queue_workers[chat_id] = worker

    def _cleanup(_task):
        if queue_workers.get(chat_id) is _task:
            queue_workers.pop(chat_id, None)

    worker.add_done_callback(_cleanup)
    return worker


async def main():
    token = get_token()
    if not token:
        print("❌ Нет токена")
        return

    user_queues.clear()
    user_locks.clear()
    pending_yt.clear()
    pending_vk.clear()

    bot = Bot(token=token)
    dp = Dispatcher()
    loop = asyncio.get_event_loop()

    @dp.message(F.text == '/start')
    async def cmd_start(message: Message):
        welcome_img = os.path.join(BOT_DIR, 'welcome.png')
        text = (
            "🎬 Saver_bot от dshot.ru\n\n"
            "Отправь ссылку на видео — скачаю!\n\n"
            "🎬 Поддерживаемые платформы:\n"
            "• YouTube (видео + шортсы)\n"
            "• VK (клипы + видео)\n"
            "• Ig (рилсы + карусели)\n"
            "• TikTok (видео + карусели)\n"
            "• X/Twitter (видео + фото + текст)\n"
            "• Pinterest (фото + видео)\n"
            "• Rutube\n"
            "• Threads (видео + фото + карусели)\n\n"
            "🎵 Поиск музыки: напиши название трека — найду и скачаю!\n\n"
            "Можно отправить несколько ссылок подряд —\n"
            "они встанут в очередь."
        )
        start_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🟢 Старт")]],
            resize_keyboard=True
        )
        if os.path.exists(welcome_img):
            try:
                photo = FSInputFile(welcome_img)
                await message.answer_photo(photo=photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=start_kb)
            except Exception as e:
                logger.warning(f"Failed to load welcome image: {e}")
                await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=start_kb)
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=start_kb)

    @dp.message(F.text == '🟢 Старт')
    async def handle_start_btn(message: Message):
        await cmd_start(message)

    @dp.message(F.text == '/updatecookies')
    async def cmd_updatecookies(message: Message):
        if message.from_user.id != ADMIN_ID:
            await message.answer("❌ Нет доступа")
            return

        text = (
            "📋 <b>Обновление IG cookies</b>\n\n"
            "<b>Safari (Mac):</b>\n"
            "1. Залогинься в Instagram в Safari\n"
            "2. Выполни в Терминале:\n\n"
            "<code>pip3 install browser-cookie3 2>/dev/null; python3 &lt;&lt; 'EOF'\n"
            "import browser_cookie3\n"
            "cj = browser_cookie3.safari(domain_name='.instagram.com')\n"
            "with open('/tmp/cookies.txt', 'w') as f:\n"
            "    f.write('# Netscape HTTP Cookie File\\n')\n"
            "    for c in cj:\n"
            "        d = c.domain if c.domain.startswith('.') else '.' + c.domain\n"
            "        f.write(d + '\\t' + ('TRUE' if d.startswith('.') else 'FALSE') + '\\t' + c.path + '\\t' + ('TRUE' if c.secure else 'FALSE') + '\\t' + (str(int(c.expires)) if c.expires else '0') + '\\t' + c.name + '\\t' + c.value + '\\n')\n"
            "print('Done!')\n"
            "EOF</code>\n\n"
            "3. Открой файл: <code>open /tmp</code>\n"
            "4. Загрузи cookies.txt сюда\n\n"
            "<b>Chrome:</b> установи <a href=\"https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc\">Get cookies.txt LOCALLY</a> → Export на instagram.com\n\n"
            "⚠️ Файл cookies.txt в формате Netscape"
        )
        await message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    @dp.message(F.document)
    async def handle_document(message: Message):
        if message.from_user.id != ADMIN_ID:
            return

        doc = message.document
        if not doc.file_name or not doc.file_name.endswith('.txt'):
            return

        try:
            file = await bot.get_file(doc.file_id)
            file_path = file.file_path
            downloaded = await bot.download_file(file_path)
            content = downloaded.read().decode('utf-8', errors='replace') if hasattr(downloaded, 'read') else downloaded.decode('utf-8', errors='replace')

            if 'sessionid' not in content:
                await message.answer("⚠️ sessionid не найден в файле. Убедись что Instagram залогинен.")
                return

            cookiefile = os.path.join(BOT_DIR, 'cookies.txt')
            with open(cookiefile, 'w', encoding='utf-8') as f:
                f.write(content)

            b64_file = os.path.join(BOT_DIR, 'cookies_b64.txt')
            b64_data = base64.urlsafe_b64encode(content.encode('utf-8')).decode('ascii')
            with open(b64_file, 'w', encoding='utf-8') as f:
                f.write(b64_data)

            session_match = re.search(r'sessionid\t([^\t\n]+)', content)
            ds_match = re.search(r'ds_user_id\t([^\t\n]+)', content)
            line_count = len([l for l in content.splitlines() if l and not l.startswith('#')])

            msg = (
                f"✅ <b>IG cookies обновлены!</b>\n\n"
                f"sessionid: {'есть' if session_match else 'нет'}\n"
                f"ds_user_id: {ds_match.group(1) if ds_match else '?'}\n"
                f"Всего кук: {line_count}"
            )
            await message.answer(msg, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Cookie upload error: {e}")
            await message.answer(f"❌ Ошибка: {str(e)[:200]}")

    @dp.message(F.text)
    async def handle_link(message: Message):
        text = message.text.strip()
        urls = re.findall(r'https?://[^\s<>"]+', text)

        if not urls:
            if len(text) < 2:
                await message.answer("❌ Отправь ссылку на видео или название трека")
                return
            await do_music_search(message, text)
            return

        chat_id = message.chat.id
        if chat_id not in user_queues:
            user_queues[chat_id] = []
        if chat_id not in user_locks:
            user_locks[chat_id] = asyncio.Lock()

        queue = user_queues[chat_id]
        added = 0

        for url in urls[:10]:
            platform = detect_platform(url)
            url_type = detect_url_type(url)

            if url_type == 'broken':
                await message.answer("❌ OK.ru не поддерживается")
                continue

            if url_type in ('ig_post',):
                task = DownloadTask(url)
                queue.append(task)
                added += 1
                continue

            if url_type == 'tt_carousel':
                task = DownloadTask(url)
                queue.append(task)
                added += 1
                continue

            if platform == 'tiktok':
                task = DownloadTask(url)
                queue.append(task)
                added += 1
                continue

            if platform == 'x':
                task = DownloadTask(url)
                queue.append(task)
                added += 1
                continue

            if url_type == 'yt_short':
                task = DownloadTask(url)
                queue.append(task)
                added += 1
                continue

            if platform == 'youtube':
                try:
                    info, formats = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: get_youtube_formats(url)
                    )
                    buttons = []
                    for fmt in formats:
                        buttons.append([InlineKeyboardButton(
                            text=fmt['label'],
                            callback_data=f"ytq_{fmt['quality']}"
                        )])
                    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
                    title = info.get('title', '')[:80] if info else ''
                    pending_yt[str(chat_id)] = {'url': url, 'title': title}
                    await message.answer(
                        f"🎬 <b>{title}</b>\n\nВыбери качество:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard
                    )
                except Exception as e:
                    await message.answer(f"❌ Ошибка: {str(e)[:100]}")
                continue

            if platform == 'vk':
                try:
                    info, formats = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: get_vk_formats(url)
                    )
                    buttons = []
                    for fmt in formats:
                        buttons.append([InlineKeyboardButton(
                            text=fmt['label'],
                            callback_data=f"vkq_{fmt['quality']}"
                        )])
                    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
                    title = info.get('title', '')[:80] if info else ''
                    pending_vk[str(chat_id)] = {'url': url, 'title': title}
                    await message.answer(
                        f"🎬 <b>{title}</b>\n\nВыбери качество:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard
                    )
                except Exception as e:
                    task = DownloadTask(url)
                    queue.append(task)
                    added += 1
                continue

            else:
                task = DownloadTask(url)
                queue.append(task)
                added += 1

        if added > 0:
            await message.answer(f"✅ Добавлено в очередь ({added} шт.)")

        if added > 0:
            ensure_queue_worker(chat_id, bot, loop)

    @dp.callback_query(F.data.startswith('ytq_'))
    async def handle_yt_quality(callback: CallbackQuery):
        parts = callback.data.split('_', 1)
        if len(parts) < 2:
            await callback.answer("Ошибка", show_alert=True)
            return

        data_parts = parts[1].split('_')
        quality = data_parts[0]
        chat_id = callback.message.chat.id

        pending = pending_yt.get(str(chat_id))
        if not pending:
            await callback.answer("Ссылка устарела", show_alert=True)
            return

        url = pending['url']
        await callback.answer()

        if chat_id not in user_queues:
            user_queues[chat_id] = []
        if chat_id not in user_locks:
            user_locks[chat_id] = asyncio.Lock()

        title = pending.get('title', '')
        task = DownloadTask(url, quality=quality, title=title)
        user_queues[chat_id].append(task)

        await callback.message.edit_text(f"✅ {quality} добавлено в очередь ({len(user_queues[chat_id])} шт.)")

        pending_yt.pop(str(chat_id), None)

        ensure_queue_worker(chat_id, bot, loop)

    @dp.callback_query(F.data.startswith('vkq_'))
    async def handle_vk_quality(callback: CallbackQuery):
        parts = callback.data.split('_', 1)
        if len(parts) < 2:
            await callback.answer("Ошибка", show_alert=True)
            return

        quality = parts[1]
        chat_id = callback.message.chat.id

        pending = pending_vk.get(str(chat_id))
        if not pending:
            await callback.answer("Ссылка устарела", show_alert=True)
            return

        url = pending.get('url', pending.get('Url', ''))
        title = pending.get('title', '')
        await callback.answer()

        if chat_id not in user_queues:
            user_queues[chat_id] = []
        if chat_id not in user_locks:
            user_locks[chat_id] = asyncio.Lock()

        task = DownloadTask(url, quality=quality, title=title)
        user_queues[chat_id].append(task)

        await callback.message.edit_text(f"✅ {quality} добавлено в очередь ({len(user_queues[chat_id])} шт.)")

        pending_vk.pop(str(chat_id), None)

        ensure_queue_worker(chat_id, bot, loop)

    music_search_cache = {}

    async def yt_music_search(query, limit=10):
        try:
            opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'force_generic_extractor': False,
                'socket_timeout': 15,
            }
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: yt_dlp.YoutubeDL(opts).extract_info(f'ytsearch{limit}:{query}', download=False)
                ),
                timeout=25
            )
            if not result or 'entries' not in result:
                return []
            return list(result['entries'])
        except asyncio.TimeoutError:
            logger.warning("YT search timed out, trying innertube...")
            return await _innertube_search(query, limit)
        except Exception as e:
            logger.error(f"YT music search error: {e}")
            return await _innertube_search(query, limit)

    async def _innertube_search(query, limit=10):
        try:
            payload = {
                'context': {
                    'client': {
                        'clientName': 'WEB',
                        'clientVersion': '2.20240101.00.00',
                        'hl': 'en',
                        'gl': 'US',
                    }
                },
                'query': query,
                'params': 'CAEgBggBEAA=',
            }
            resp = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: requests.post(
                        'https://www.youtube.com/youtubei/v1/search?key=AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w',
                        json=payload,
                        headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'},
                        timeout=15
                    )
                ),
                timeout=20
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            items = data.get('contents', {}).get('twoColumnSearchResultsRenderer', {}).get('primaryContents', {}).get('sectionListRenderer', {}).get('contents', [])
            tracks = []
            for section in items:
                for item in section.get('itemSectionRenderer', {}).get('contents', []):
                    vid = item.get('videoRenderer')
                    if vid:
                        tracks.append({
                            'id': vid.get('videoId', ''),
                            'title': vid.get('title', {}).get('runs', [{}])[0].get('text', ''),
                            'duration': vid.get('lengthText', {}).get('simpleText', ''),
                            'url': f"https://www.youtube.com/watch?v={vid.get('videoId', '')}",
                        })
            return tracks[:limit]
        except Exception as e:
            logger.error(f"Innertube search error: {e}")
            return []

    @dp.message(F.text.startswith('/music'))
    async def cmd_music(message: Message):
        query = message.text.replace('/music', '').strip()
        if not query:
            await message.answer(
                "🎵 <b>Поиск музыки</b>\n\n"
                "Просто напиши название трека в чат",
                parse_mode=ParseMode.HTML
            )
            return
        await do_music_search(message, query)

    async def do_music_search(message: Message, query: str):
        await message.answer(f"🔍 Ищу: <b>{query}</b>...", parse_mode=ParseMode.HTML)

        try:
            tracks = await asyncio.wait_for(yt_music_search(query, limit=10), timeout=45)
        except asyncio.TimeoutError:
            await message.answer("❌ Поиск занял слишком много времени. Попробуйте позже.")
            return
        if not tracks:
            await message.answer("❌ Ничего не найдено")
            return

        music_search_cache[str(message.chat.id)] = {
            'query': query,
            'tracks': tracks,
        }

        text = f"🎵 <b>Результаты поиска:</b> {query}\n\n"
        keyboard = []
        for i, track in enumerate(tracks[:10]):
            title = track.get('title', 'Unknown')
            duration = track.get('duration') or 0
            if isinstance(duration, str) and ':' in duration:
                dur_str = duration
            elif isinstance(duration, (int, float)) and duration:
                dur_str = f"{int(duration) // 60}:{int(duration) % 60:02d}"
            else:
                dur_str = ''
            text += f"<b>{i+1}.</b> {title} {dur_str}\n"
            keyboard.append([InlineKeyboardButton(
                text=f"⬇️ {i+1}. {title[:40]}",
                callback_data=f"mus_dl_{i}"
            )])

        kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    @dp.callback_query(F.data.startswith('mus_dl_'))
    async def handle_music_download(callback: CallbackQuery):
        idx = int(callback.data.split('_')[2])
        chat_id = callback.message.chat.id
        cache = music_search_cache.get(str(chat_id))
        if not cache or idx >= len(cache['tracks']):
            await callback.answer("Ссылка устарела", show_alert=True)
            return

        track = cache['tracks'][idx]
        title = track.get('title', 'track')
        url = track.get('url') or track.get('webpage_url') or f"https://www.youtube.com/watch?v={track.get('id', '')}"

        await callback.message.edit_text(f"⬇️ Скачиваю: {title}...")

        try:
            tmp_dir = os.path.join(DOWNLOAD_DIR, f"music_{uuid.uuid4().hex[:8]}")
            os.makedirs(tmp_dir, exist_ok=True)
            outtmpl = os.path.join(tmp_dir, '%(id)s.%(ext)s')

            loop = asyncio.get_event_loop()
            ffmpeg_location = get_ffmpeg_location()
            
            downloaded_file = None
            
            download_clients = [None, ['android_vr'], ['web'], ['mweb'], ['android'], ['ios'], ['tv_embedded'], ['mediaconnect']]
            for client in download_clients:
                if downloaded_file:
                    break
                try:
                    dl_opts = {
                        'format': 'bestaudio/best',
                        'outtmpl': outtmpl,
                        'postprocessors': [{
                            'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3',
                            'preferredquality': '192',
                        }],
                        'quiet': True,
                        'no_warnings': True,
                        'noplaylist': True,
                        'socket_timeout': 30,
                        'retries': 5,
                        'fragment_retries': 5,
                    }
                    if client:
                        dl_opts['extractor_args'] = {'youtube': {'player_client': client}}
                    if ffmpeg_location:
                        dl_opts['ffmpeg_location'] = ffmpeg_location

                    def do_download(c=client):
                        with yt_dlp.YoutubeDL(dl_opts) as ydl:
                            ydl.download([url])

                    await asyncio.wait_for(loop.run_in_executor(None, do_download), timeout=120)
                    
                    mp3_files = [f for f in os.listdir(tmp_dir) if f.endswith('.mp3')]
                    if mp3_files:
                        downloaded_file = os.path.join(tmp_dir, mp3_files[0])
                except Exception as e:
                    logger.warning(f"yt-dlp music client {client} failed: {e}")
                    continue

            if not downloaded_file:
                try:
                    result = download_youtube_innertube(url, audio_only=True)
                    if result:
                        fn, title_raw = result
                        if fn.endswith('.webm'):
                            remuxed = fn.rsplit('.', 1)[0] + '.mp3'
                            ffmpeg_path = get_ffmpeg_path()
                            if ffmpeg_path and os.path.exists(fn):
                                proc = subprocess.run(
                                    [ffmpeg_path, '-y', '-i', fn, '-vn', '-ab', '192k', '-ar', '44100', remuxed],
                                    capture_output=True, text=True, timeout=120
                                )
                                if proc.returncode == 0 and os.path.exists(remuxed):
                                    os.remove(fn)
                                    downloaded_file = remuxed
                                else:
                                    downloaded_file = fn
                            else:
                                downloaded_file = fn
                        else:
                            downloaded_file = fn
                except Exception as e:
                    logger.error(f"Innertube music download failed: {e}")

            if not downloaded_file or not os.path.exists(downloaded_file):
                await callback.message.edit_text("❌ Не удалось скачать трек")
                return

            audio_file = FSInputFile(downloaded_file)
            cap = f"🎵 {title}\n\n📎 скачано с @saverdshot_bot"
            await bot.send_audio(chat_id=chat_id, audio=audio_file, caption=cap)
            await callback.message.delete()

            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            logger.error(f"Music download error: {e}")
            await callback.message.edit_text(f"❌ Ошибка скачивания: {str(e)[:100]}")

    print("🚀 Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
