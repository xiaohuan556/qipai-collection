# -*- coding: utf-8 -*-
"""统一启动三个本地牌类模块，并提供一个统一入口页面。"""
import os
import subprocess
import sys
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
APPS = [
    ("红十端测试版", "red10", "8899"),
    ("扑克方案推演", "poker", "8787"),
    ("干瞪眼商业化演示版", "dryeye", None),
]

children = []
flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
for _title, folder, port in APPS:
    env = os.environ.copy()
    if port:
        env["PORT"] = port
    children.append(subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=os.path.join(BASE, folder),
        env=env,
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ))

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

os.chdir(BASE)
server = ThreadingHTTPServer(("127.0.0.1", 8800), QuietHandler)
print("棋牌合集统一入口：http://localhost:8800")
time.sleep(1.2)
webbrowser.open("http://localhost:8800")
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
    for child in children:
        if child.poll() is None:
            child.terminate()
