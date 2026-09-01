#!/usr/bin/env python3
"""
python3 get_ig_cookies.py

Читает IG куки из браузера, сохраняет cookies.txt, коммитит в git.
Бот подтянет куки автоматически при следующем рестарте.
"""
import sys
import os
import subprocess
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.expanduser("~/Library/Python/3.9/lib/python/site-packages"))

try:
    import browser_cookie3
except ImportError:
    print("pip3 install browser_cookie3")
    sys.exit(1)

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, "cookies.txt")

cookies = []
for fn in [browser_cookie3.chrome, browser_cookie3.safari]:
    try:
        for c in fn(domain_name=".instagram.com"):
            cookies.append(c)
    except Exception:
        pass

if not cookies:
    print("Нет IG кук! Залогинен в Chrome/Safari?")
    sys.exit(1)

with open(OUT, "w") as f:
    f.write("# Netscape HTTP Cookie File\n\n")
    for c in cookies:
        d = c.domain if c.domain.startswith(".") else "." + c.domain
        f.write(f"{d}\t{'TRUE' if d.startswith('.') else 'FALSE'}\t{c.path}\t{'TRUE' if c.secure else 'FALSE'}\t{str(int(c.expires)) if c.expires else '0'}\t{c.name}\t{c.value}\n")

print(f"cookies.txt готов ({len(cookies)} кук)")

# Auto-commit and push to GitHub
try:
    subprocess.run(["git", "add", "cookies.txt"], cwd=DIR, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "update: IG cookies"], cwd=DIR, check=True, capture_output=True)
    subprocess.run(["git", "push"], cwd=DIR, check=True, capture_output=True, timeout=30)
    print("✅ Закоммичено и запушено в GitHub!")
    print("Бот подтянет куки при следующем рестарте.")
except subprocess.CalledProcessError as e:
    if b"nothing to commit" in (e.stderr or b"") or b"nothing to commit" in (e.stdout or b""):
        print("⚠️ Куки уже актуальны, коммитить нечего.")
    else:
        print(f"⚠️ Git ошибка: {e.stderr.decode(errors='replace')[:200]}")
        print("Куки сохранены локально. Отправь cookies.txt боту в чат.")
except Exception as e:
    print(f"⚠️ Ошибка: {e}")
    print("Куки сохранены локально. Отправь cookies.txt боту в чат.")
