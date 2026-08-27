#!/usr/bin/env python3
"""
Instagram Cookie Extractor (SAFE)
Читает куки из уже залогиненного Chrome/Safari — НОВЫЙ ВХОД НЕ НУЖЕН.

Использование:
  1. Убедись что залогинен в Instagram в Chrome
  2. Запусти: python3 get_ig_cookies.py
  3. Cookies сохранятся и запушатся в GitHub

Риск бана: НЕТ — скрипт только ЧИТАЕТ существующую сессию.
"""
import subprocess
import sys
import os
import base64

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_FILE = os.path.join(REPO_DIR, 'cookies.txt')


def get_chrome_cookies():
    try:
        import browser_cookie3
    except ImportError:
        print("Установи browser_cookie3: pip3 install browser_cookie3")
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


def push_to_github():
    try:
        subprocess.run(['git', 'add', 'cookies.txt', 'cookies_b64.txt'], cwd=REPO_DIR, check=False)
        result = subprocess.run(['git', 'diff', '--cached', '--quiet', 'cookies.txt'], cwd=REPO_DIR)
        if result.returncode == 0:
            print("cookies.txt не изменился, пропускаю push.")
            return True
        subprocess.run(['git', 'commit', '-m', 'chore: update IG cookies'], cwd=REPO_DIR, check=True)
        subprocess.run(['git', 'push'], cwd=REPO_DIR, check=True)
        print("Cookies запушены в GitHub!")
        return True
    except Exception as e:
        print(f"Git push failed: {e}")
        return False


def main():
    print("=" * 50)
    print("Instagram Cookie Extractor (SAFE)")
    print("Читает куки из залогиненного Chrome/Safari")
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
    print(f"Сохранено: {COOKIES_FILE}")

    b64_file = os.path.join(REPO_DIR, 'cookies_b64.txt')
    with open(b64_file, 'w') as f:
        f.write(base64.b64encode(netscape.encode('utf-8')).decode('ascii'))
    print(f"Сохранено: {b64_file}")

    push = input("\nЗапушить в GitHub? (y/n): ").strip().lower()
    if push == 'y':
        push_to_github()
    else:
        print("Пропускаю push.")


if __name__ == '__main__':
    main()
