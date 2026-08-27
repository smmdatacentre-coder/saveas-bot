#!/usr/bin/env python3
"""
Instagram Cookie Extractor (SAFE)
Читает куки из залогиненного Chrome/Safari и отправляет боту в Telegram.

Использование:
  1. Убедись что залогинен в Instagram в Chrome
  2. Запусти: python3 get_ig_cookies.py
  3. Куки уйдут боту в Telegram автоматически

Риск бана: НЕТ — скрипт только ЧИТАЕТ существующую сессию.
"""
import sys
import os
import base64
import urllib.request
import urllib.parse

BOT_TOKEN = "8868939755:AAFG6wtLjShyVSLmmbaD650kbNd2Zq3zcjg"
ADMIN_ID = 256869382
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(REPO_DIR, 'cookies.txt')


def get_chrome_cookies():
    try:
        import browser_cookie3
    except ImportError:
        print("Установи: pip3 install browser_cookie3")
        sys.exit(1)

    cookies = []
    for browser_fn in [browser_cookie3.chrome, browser_cookie3.safari]:
        try:
            cj = browser_fn(domain_name='.instagram.com')
            for c in cj:
                cookies.append({
                    'name': c.name,
                    'value': c.value,
                    'domain': c.domain,
                    'path': c.path,
                    'secure': c.secure,
                    'httpOnly': getattr(c, 'httponly', False),
                    'expires': int(c.expires) if c.expires else -1,
                })
        except Exception:
            continue
    return cookies


def cookies_to_netscape(cookies):
    lines = ["# Netscape HTTP Cookie File", "# https://curl.se/docs/http-cookies.html", ""]
    for c in cookies:
        domain = c['domain']
        if not domain.startswith('.'):
            domain = '.' + domain
        flag = 'TRUE' if domain.startswith('.') else 'FALSE'
        path = c.get('path', '/')
        secure = 'TRUE' if c.get('secure', False) else 'FALSE'
        expires = str(int(c['expires'])) if c.get('expires', -1) > 0 else '0'
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{c['name']}\t{c['value']}")
    return '\n'.join(lines) + '\n'


def send_to_bot(content):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

    import io
    data = io.BytesIO(content.encode('utf-8'))
    data.name = 'cookies.txt'

    boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
    body = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f'{ADMIN_ID}\r\n'
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="document"; filename="cookies.txt"\r\n'
        f'Content-Type: text/plain\r\n\r\n'
        f'{content}\r\n'
        f'--{boundary}--\r\n'
    ).encode('utf-8')

    req = urllib.request.Request(
        url,
        data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = resp.read().decode()
        if '"ok":true' in result:
            return True
        else:
            print(f"Telegram error: {result}")
            return False


def main():
    print("=" * 50)
    print("Instagram Cookie Extractor (SAFE)")
    print("=" * 50)

    cookies = get_chrome_cookies()

    if not cookies:
        print("\n❌ Instagram cookies не найдены!")
        print("Убедись что залогинен в Instagram в Chrome или Safari.")
        sys.exit(1)

    print(f"\n✅ Найдено {len(cookies)} Instagram cookies")

    netscape = cookies_to_netscape(cookies)

    with open(COOKIES_FILE, 'w') as f:
        f.write(netscape)

    b64_file = os.path.join(REPO_DIR, 'cookies_b64.txt')
    with open(b64_file, 'w') as f:
        f.write(base64.b64encode(netscape.encode('utf-8')).decode('ascii'))

    print("Отправляю боту в Telegram...")
    if send_to_bot(netscape):
        print("✅ Куки отправлены боту! В боте нажми /updatecookies")
    else:
        print("❌ Ошибка отправки. Попробуй /updatecookies в боте.")


if __name__ == '__main__':
    main()
