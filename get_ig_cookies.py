#!/usr/bin/env python3
"""
python3 get_ig_cookies.py

Читает IG куки из браузера, сохраняет cookies.txt, открывает папку.
Перетаскиваешь cookies.txt боту в чат.
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
subprocess.Popen(["open", DIR])
print("Перетащи cookies.txt боту в чат")
