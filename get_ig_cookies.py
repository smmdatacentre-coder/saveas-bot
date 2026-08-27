#!/usr/bin/env python3
"""
Instagram Cookie Extractor
Запускай на своём Mac/PC:
  python3 get_ig_cookies.py

1. Откроется Chrome с Instagram
2. Залогинься (если ещё не)
3. Нажми Enter в терминале
4. Cookies сохранятся и запушатся в GitHub
"""
import subprocess
import sys
import os
import json
import time
import shutil

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(REPO_DIR, 'cookies.txt')

def get_chrome_cookies_via_browser():
    try:
        from browser_cookie3 import chrome
        cj = chrome(domain_name='.instagram.com')
        cookies = []
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
        return cookies
    except Exception as e:
        print(f"browser_cookie3 failed: {e}")
        return None


def get_chrome_cookies_via_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed. Run: pip install playwright")
        return None

    cookies = []
    print("\nОткроется Chrome. Залогинься в Instagram, потом нажми Enter в терминале.\n")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=os.path.join(REPO_DIR, '.ig_chrome_profile'),
            headless=False,
            channel='chrome',
            args=['--no-first-run', '--no-default-browser-check'],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto('https://www.instagram.com/')

        input("\n>>> Залогинься в Instagram, затем нажми Enter... ")

        all_cookies = ctx.cookies('https://www.instagram.com')
        for c in all_cookies:
            cookies.append({
                'name': c['name'],
                'value': c['value'],
                'domain': c.get('domain', ''),
                'path': c.get('path', '/'),
                'secure': c.get('secure', False),
                'httpOnly': c.get('httpOnly', False),
                'expires': int(c.get('expires', -1)) if c.get('expires', -1) > 0 else -1,
            })
        ctx.close()

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
        name = c['name']
        value = c['value']
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    return '\n'.join(lines) + '\n'


def push_to_github():
    try:
        subprocess.run(['git', 'add', 'cookies.txt', 'cookies_b64.txt'], cwd=REPO_DIR, check=False)
        result = subprocess.run(['git', 'status', '--porcelain', 'cookies.txt'], cwd=REPO_DIR, capture_output=True, text=True)
        if 'cookies.txt' not in (result.stdout or ''):
            print("cookies.txt не изменился, пуш не нужен.")
            return True

        subprocess.run(['git', 'commit', '-m', 'chore: update IG cookies'], cwd=REPO_DIR, check=True)
        subprocess.run(['git', 'push'], cwd=REPO_DIR, check=True)
        print("Cookies запушены в GitHub!")
        return True
    except Exception as e:
        print(f"Git push failed: {e}")
        return False


def save_base64(cookies_file):
    import base64
    b64_file = os.path.join(REPO_DIR, 'cookies_b64.txt')
    try:
        with open(cookies_file, 'rb') as f:
            data = f.read()
        with open(b64_file, 'w') as f:
            f.write(base64.b64encode(data).decode())
        print(f"cookies_b64.txt обновлён ({len(data)} bytes)")
    except Exception as e:
        print(f"Base64 save failed: {e}")


def main():
    print("=" * 50)
    print("Instagram Cookie Extractor")
    print("=" * 50)

    cookies = None

    # Method 1: browser_cookie3 (reads from Chrome directly)
    try:
        import browser_cookie3
        cookies = get_chrome_cookies_via_browser()
        if cookies:
            print(f"Получено {len(cookies)} cookies из Chrome (browser_cookie3)")
    except ImportError:
        pass

    # Method 2: Playwright (opens browser, user logs in)
    if not cookies:
        cookies = get_chrome_cookies_via_playwright()

    if not cookies:
        print("Не удалось получить cookies!")
        sys.exit(1)

    ig_cookies = [c for c in cookies if 'instagram' in c.get('domain', '')]
    print(f"Instagram cookies: {len(ig_cookies)}")

    if not ig_cookies:
        print("Нет Instagram cookies! Убедись что залогинен.")
        sys.exit(1)

    # Save cookies.txt
    netscape = cookies_to_netscape(ig_cookies)
    with open(COOKIES_FILE, 'w') as f:
        f.write(netscape)
    print(f"Сохранено: {COOKIES_FILE} ({len(ig_cookies)} cookies)")

    # Save cookies_b64.txt
    save_base64(COOKIES_FILE)

    # Push to GitHub
    push = input("\nЗапушить в GitHub? (y/n): ").strip().lower()
    if push == 'y':
        push_to_github()
    else:
        print("Пропускаю push. Не забудь запушить вручную.")


if __name__ == '__main__':
    main()
