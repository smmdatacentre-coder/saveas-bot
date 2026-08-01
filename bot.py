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
    if os.path.exists(cookiefile):
        return cookiefile

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
    if re.search(r'instagram\.com/p/', u) or re.search(r'instagram\.com/reels?/', u):
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
    return 'unknown'


class DownloadTask:
    def __init__(self, url, quality=None, audio_only=False):
        self.url = url
        self.quality = quality
        self.audio_only = audio_only
        self.status = 'waiting'
        self.progress = ''
        self.filename = None
        self.result = None


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
    formats.append({
        'height': 0,
        'label': "🎵 MP3",
        'quality': "mp3",
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


def download_ig_post(url):
    photos = []
    caption = ''
    tmp_dir = os.path.join(DOWNLOAD_DIR, f"ig_{uuid.uuid4().hex[:8]}")
    os.makedirs(tmp_dir, exist_ok=True)

    if '/p/' in url.lower():
        try:
            cookies_file = get_instagram_cookiefile()
            cmd = [sys.executable, '-m', 'gallery_dl']
            if cookies_file:
                cmd += ['--cookies', cookies_file]
            cmd += ['-d', tmp_dir, url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode:
                logger.error(f"Instagram gallery-dl error: {result.stderr[-500:]}")
            for root, dirs, files in os.walk(tmp_dir):
                for fn in sorted(files):
                    fp = os.path.join(root, fn)
                    if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                        photos.append(fp)
            if photos:
                return photos, caption, tmp_dir
        except Exception as e:
            logger.error(f"Instagram gallery-dl carousel error: {e}")

    shortcode = _extract_ig_shortcode(url)
    if not shortcode:
        m = re.search(r'instagram\.com/(?:reel|tv)/([^/?]+)', url)
        if m:
            shortcode = m.group(1)

    if shortcode:
        try:
            pk = _ig_get_media_pk(shortcode)
            if pk:
                item = _ig_api_get(pk)
                if item:
                    caption = item.get('caption', {}).get('text', '') if item.get('caption') else ''
                    media_type = item.get('media_type', 1)
                    if media_type == 8:
                        carousel = item.get('carousel_media', [])
                        for i, c in enumerate(carousel):
                            ct = c.get('media_type', 1)
                            if ct == 2:
                                vids = c.get('video_versions', [])
                                if vids:
                                    vids.sort(key=lambda v: v.get('width', 0) * v.get('height', 0), reverse=True)
                                    dl_url = vids[0]['url']
                                    ext = 'mp4'
                                else:
                                    continue
                            else:
                                imgs = c.get('image_versions2', {}).get('candidates', [])
                                if imgs:
                                    dl_url = imgs[0]['url']
                                    ext = 'jpg'
                                else:
                                    continue
                            try:
                                dr = requests.get(dl_url, timeout=60, headers={'User-Agent': 'Instagram 301.0.0.27.98'})
                                if dr.status_code == 200:
                                    fp = os.path.join(tmp_dir, f"{i}.{ext}")
                                    with open(fp, 'wb') as f:
                                        f.write(dr.content)
                                    photos.append(fp)
                            except Exception as e:
                                logger.error(f"carousel download [{i}]: {e}")
                    elif media_type == 2:
                        vids = item.get('video_versions', [])
                        if vids:
                            # Prefer non-square vertical version (height > width), then by area
                            def ig_vkey(v):
                                w2 = v.get('width', 0)
                                h2 = v.get('height', 0)
                                is_square = 1 if (w2 and h2 and w2 == h2) else 0
                                return (is_square, -(w2 * h2))
                            vids.sort(key=ig_vkey)
                            chosen = vids[0]
                            cw = chosen.get('width', 0)
                            ch = chosen.get('height', 0)
                            dr = requests.get(chosen['url'], timeout=120, headers={'User-Agent': 'Instagram 301.0.0.27.98'})
                            if dr.status_code == 200:
                                fp = os.path.join(tmp_dir, "0.mp4")
                                with open(fp, 'wb') as f:
                                    f.write(dr.content)
                                photos.append(fp)
                                if cw and ch and cw == ch:
                                    logger.warning(f"IG reel {shortcode}: only square ({cw}x{ch}) available from API")
                    else:
                        imgs = item.get('image_versions2', {}).get('candidates', [])
                        if imgs:
                            dr = requests.get(imgs[0]['url'], timeout=60, headers={'User-Agent': 'Instagram 301.0.0.27.98'})
                            if dr.status_code == 200:
                                fp = os.path.join(tmp_dir, "0.jpg")
                                with open(fp, 'wb') as f:
                                    f.write(dr.content)
                                photos.append(fp)
        except Exception as e:
            logger.error(f"IG API error: {e}")

    # A /p/ URL can be a carousel. yt-dlp treats it as a video playlist and
    # fails on image-only entries, so let gallery-dl handle posts directly.
    if not photos and '/p/' not in url.lower():
        try:
            # Instagram API can reject the request even when yt-dlp can extract the media.
            ydl_opts = make_ydl_opts()
            ydl_opts['outtmpl'] = os.path.join(tmp_dir, '%(id)s.%(ext)s')
            ydl_opts['format'] = 'best[ext=mp4]/best'
            cookies_file = get_instagram_cookiefile()
            if cookies_file:
                ydl_opts['cookiefile'] = cookies_file
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    title = info.get('title', '') or caption
                    for fp in glob.glob(os.path.join(tmp_dir, f"{info.get('id', '')}.*")):
                        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
                            # Check if downloaded video is square (common issue with IG reels)
                            if fp.lower().endswith(('.mp4', '.mov', '.webm')):
                                w, h = extract_video_dimensions(fp)
                                if w and h and w == h:
                                    logger.warning(f"IG yt-dlp fallback: square video ({w}x{h}), removing")
                                    os.remove(fp)
                                    continue
                            photos.append(fp)
        except Exception as e:
            logger.error(f"Instagram yt-dlp fallback error: {e}")

    if not photos:
        try:
            cookies_file = get_instagram_cookiefile()
            cmd = [sys.executable, '-m', 'gallery_dl']
            if cookies_file:
                cmd += ['--cookies', cookies_file]
            cmd += ['-d', tmp_dir, url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode:
                logger.error(f"Instagram gallery-dl error: {result.stderr[-500:]}")
            for root, dirs, files in os.walk(tmp_dir):
                for fn in sorted(files):
                    fp = os.path.join(root, fn)
                    if os.path.getsize(fp) > 0:
                        if fp.lower().endswith(('.mp4', '.mov', '.webm')):
                            w, h = extract_video_dimensions(fp)
                            if w and h and w == h:
                                logger.warning(f"IG gallery-dl: square video ({w}x{h}), removing")
                                os.remove(fp)
                                continue
                        photos.append(fp)
        except Exception as e:
            logger.error(f"gallery-dl fallback error: {e}")

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


def download_youtube_cobalt(url, audio_only=False):
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

                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        return {'error': 'Не удалось получить информацию'}
                    title = info.get('title', '')
                    vid_id = info.get('id', '')
                tracker.title = title[:40] if title else ''

                dl_dir = DOWNLOAD_DIR

                ffmpeg_location = get_ffmpeg_location()
                download_clients = [None, ['web'], ['mweb'], ['android'], ['ios'], ['tv'], ['tv_embedded'], ['mediaconnect']]

                if getattr(task, 'audio_only', False):
                    mp3_opts = {
                        'format': 'bestaudio/best',
                        'outtmpl': f'{dl_dir}/{vid_id}.%(ext)s',
                        'noplaylist': True,
                        'quiet': True,
                        'no_warnings': True,
                        'socket_timeout': 30,
                        'progress_hooks': [tracker.hook],
                    }
                    for cli_idx, client in enumerate(download_clients):
                        try:
                            mp3_dl = dict(mp3_opts)
                            if client:
                                mp3_dl['extractor_args'] = {'youtube': {'player_client': client}}
                            with yt_dlp.YoutubeDL(mp3_dl) as ydl:
                                ydl.download([url])
                            break
                        except Exception as dl_err:
                            if cli_idx == len(download_clients) - 1:
                                raise
                            continue
                    tracker.done()
                    audio_exts = ('.mp3', '.m4a', '.webm', '.ogg', '.opus', '.wav', '.aac')
                    fn_list = [f for f in glob.glob(os.path.join(dl_dir, f'{vid_id}.*')) if f.lower().endswith(audio_exts)]
                    if not fn_list:
                        fn_list = glob.glob(os.path.join(dl_dir, f'{vid_id}.*'))
                    if not fn_list:
                        return {'error': 'Файл не найден'}
                    fn = fn_list[0]
                    if not ffmpeg_path or not (os.path.exists(ffmpeg_path) or shutil.which('ffmpeg')):
                        if not fn.lower().endswith(('.mp3',)):
                            os.remove(fn)
                            return {'error': 'MP3 требует ffmpeg на сервере'}
                    if not fn.lower().endswith('.mp3'):
                        audio_fn = fn.rsplit('.', 1)[0] + '.mp3'
                        try:
                            proc = subprocess.run(
                                [ffmpeg_path, '-y', '-i', fn, '-vn',
                                 '-acodec', 'libmp3lame', '-ab', '320k',
                                 '-ar', '44100', audio_fn],
                                capture_output=True, text=True, timeout=300
                            )
                            if proc.returncode != 0:
                                logger.error(f"ffmpeg MP3 error: {proc.stderr[:300]}")
                                os.remove(fn)
                                return {'error': f'MP3 конвертация не удалась: {proc.stderr[:100]}'}
                            if os.path.exists(audio_fn) and os.path.getsize(audio_fn) > 0:
                                os.remove(fn)
                                fn = audio_fn
                            else:
                                os.remove(fn)
                                return {'error': 'MP3: ffmpeg создал пустой файл'}
                        except Exception as e:
                            logger.error(f"ffmpeg MP3 exception: {e}")
                            if os.path.exists(fn):
                                os.remove(fn)
                            return {'error': f'MP3 ошибка: {str(e)[:100]}'}
                    return {
                        'filename': fn,
                        'title': title or 'Audio',
                        'uploader': (info.get('uploader', '') or info.get('channel', '')) if info else '',
                        'filesize': os.path.getsize(fn),
                        'description': ((info.get('description', '') or '')[:1000] if info else ''),
                        'audio_only': True,
                    }

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
                download_clients = [None, ['web'], ['mweb'], ['android'], ['ios'], ['tv'], ['tv_embedded'], ['mediaconnect']]
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
                        if ffmpeg_location:
                            dl_opts['ffmpeg_location'] = ffmpeg_location
                        with yt_dlp.YoutubeDL(dl_opts) as ydl:
                            ydl.download([url])
                        break
                    except Exception as dl_err:
                        last_err = dl_err
                        logger.warning(f"YouTube client {client} failed: {dl_err}")
                        if cli_idx == len(download_clients) - 1:
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
                return {'error': 'YouTube блокирует запросы с этого сервера (HTTP 403). Попробуйте через прокси/VPN или другой сервер.'}
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
            status = task.progress if task.progress else 'загружается...'
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
        lines.append(f"{icon} {i}. {picon} {status}")
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
                                        f"🎬 <b>{tt_result.get('title', '')}</b>\n"
                                        f"📦 {format_size(tt_result['filesize'])}\n\n"
                                        f"⬇️ <a href=\"{link}\">Скачать видео</a>\n\n"
                                        f"<i>Открой ссылку → нажми ⋮ → «Загрузить»</i>"
                                    )
                                    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                                else:
                                    await bot.send_message(chat_id, "❌ Не удалось загрузить на хостинг.")
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
                            await bot.send_message(chat_id, f"❌ Ошибка: {str(e)[:100]}")
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
                                await bot.send_document(chat_id=chat_id, document=fobj, caption=caption[:1024] if caption else None)
                            except Exception as e:
                                logger.error(f"X video send error: {e}")
                        task.status = 'done'
                        task.result = {'photos': files}
                    else:
                        raise RuntimeError(x_result.get('error', 'Не удалось скачать из X/Twitter'))

                    queue.pop(0)
                    await update_status(build_queue_text(queue))
                    continue

                if url_type == 'ig_post':
                    task.status = 'downloading'
                    await update_status(build_queue_text(queue))

                    photos, caption, ig_tmp_dir = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: download_ig_post(task.url)
                    )

                    if photos:
                        task.status = 'done'
                        task.result = {'photos': photos}
                        caption_text = caption.strip() if caption else ''
                        for i, fpath in enumerate(photos):
                            try:
                                cap = caption_text[:1024] if (i == 0 and caption_text) else None
                                is_video = fpath.lower().endswith(('.mp4', '.mov', '.webm'))
                                thumb_path = extract_thumbnail(fpath) if is_video else None
                                fobj = FSInputFile(fpath)
                                thumb_obj = FSInputFile(thumb_path) if thumb_path else None
                                if is_video:
                                    await bot.send_document(chat_id=chat_id, document=fobj, caption=cap)
                                else:
                                    await bot.send_photo(chat_id=chat_id, photo=fobj, caption=cap)
                                if thumb_path and os.path.exists(thumb_path):
                                    os.remove(thumb_path)
                                if i < len(photos) - 1:
                                    await asyncio.sleep(0.5)
                            except Exception as e:
                                logger.error(f"IG send [{i}] error: {e}")
                                await asyncio.sleep(1)
                    else:
                        if not get_instagram_cookiefile():
                            raise RuntimeError('Instagram требует cookies авторизованного аккаунта')
                        raise RuntimeError('Instagram не отдал медиа даже с cookies')

                    queue.pop(0)
                    await update_status(build_queue_text(queue))
                    continue

                task.status = 'downloading'
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
                    await bot.send_message(chat_id, f"❌ {result['error']}")
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
                                    f"🎬 <b>{result.get('title', '')}</b>\n"
                                    f"📦 {format_size(result['filesize'])}\n\n"
                                    f"⬇️ <a href=\"{link}\">Скачать видео</a>\n\n"
                                    f"<i>Открой ссылку → нажми ⋮ → «Загрузить»</i>"
                                )
                                await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                            else:
                                await bot.send_message(chat_id, "❌ Не удалось загрузить на хостинг. Попробуйте позже.")
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
                        elif result.get('audio_only'):
                            audio_file = FSInputFile(result['filename'])
                            await bot.send_audio(chat_id=chat_id, audio=audio_file, caption=f"🎵 {result.get('title', '')[:80]}" if result.get('title') else None)
                        elif is_youtube or is_vk:
                            doc_file = FSInputFile(result['filename'])
                            await bot.send_document(
                                chat_id=chat_id, document=doc_file,
                                caption=f"🎬 {result.get('title', '')[:80]}\n📦 {format_size(result['filesize'])}"
                            )
                        else:
                            desc = result.get('description', '').strip()
                            cap_parts = []
                            if result.get('uploader'):
                                cap_parts.append(f"👤 {result['uploader']}")
                            if desc:
                                cap_parts.append(desc[:800])
                            cap_parts.append(f"📦 {format_size(result['filesize'])}")
                            cap = '\n\n'.join(cap_parts)

                            if is_high_quality:
                                link = await asyncio.get_event_loop().run_in_executor(
                                    None, lambda: upload_to_hosting(result['filename'])
                                )
                                if link:
                                    text = (
                                        f"🎬 <b>{result['title']}</b>\n"
                                        f"📦 {format_size(result['filesize'])}\n\n"
                                        f"⬇️ <a href=\"{link}\">Скачать видео</a>\n\n"
                                        f"<i>Открой ссылку → нажми ⋮ → «Загрузить»</i>"
                                    )
                                    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                                else:
                                    await bot.send_message(chat_id, "❌ Не удалось загрузить на хостинг. Попробуйте позже.")
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
                        await bot.send_message(chat_id, f"❌ Ошибка: {str(e)[:100]}")
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
                    await bot.send_message(chat_id, f"❌ {task.result['error']}")
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
            "🎬 <b>Saver_bot от dshot.ru</b>\n\n"
            "Отправь ссылку на видео — скачаю!\n\n"
            "Поддерживаемые платформы:\n"
            "• YouTube (видео + шортсы + MP3)\n"
            "• VK (клипы + видео)\n"
            "• Instagram (рилсы + карусели)\n"
            "• TikTok (видео + карусели)\n"
            "• X/Twitter (видео + фото + текст)\n"
            "• Rutube\n\n"
            "Можно отправить несколько ссылок подряд —\n"
            "они встанут в очередь.\n\n"
            "Для YouTube и VK можно выбрать качество."
        )
        start_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🚀 Старт")]],
            resize_keyboard=True
        )
        if os.path.exists(welcome_img):
            photo = FSInputFile(welcome_img)
            await message.answer_photo(photo=photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=start_kb)
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=start_kb)

    @dp.message(F.text == '🚀 Старт')
    async def handle_start_btn(message: Message):
        await cmd_start(message)

    @dp.message(F.text)
    async def handle_link(message: Message):
        urls = re.findall(r'https?://[^\s<>"]+', message.text.strip())
        if not urls:
            await message.answer("❌ Отправь ссылку на видео")
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
                    pending_yt[str(chat_id)] = {'url': url}
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
                    pending_vk[str(chat_id)] = {'url': url}
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

        is_mp3 = quality == 'mp3'
        task = DownloadTask(url, quality=None if is_mp3 else quality, audio_only=is_mp3)
        user_queues[chat_id].append(task)

        label = "🎵 MP3 аудио" if is_mp3 else quality
        await callback.message.edit_text(f"✅ {label} добавлено в очередь ({len(user_queues[chat_id])} шт.)")

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

        url = pending['url']
        await callback.answer()

        if chat_id not in user_queues:
            user_queues[chat_id] = []
        if chat_id not in user_locks:
            user_locks[chat_id] = asyncio.Lock()

        task = DownloadTask(url, quality=quality)
        user_queues[chat_id].append(task)

        await callback.message.edit_text(f"✅ {quality} добавлено в очередь ({len(user_queues[chat_id])} шт.)")

        pending_vk.pop(str(chat_id), None)

        ensure_queue_worker(chat_id, bot, loop)

    print("🚀 Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
