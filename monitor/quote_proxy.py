#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""牛股计算器本地行情代理。

为什么需要它：
    bull_calc.html 默认用 JSONP（<script src="https://qt.gtimg.cn/q=...">）取行情，
    但 qt.gtimg.cn 返回的是 GBK 编码（Content-Type: text/html; charset=GBK），
    UTF-8 页面直接注入 <script> 会被按 UTF-8 解，导致执行失败、行情全挂。
    本代理把请求转到本地 127.0.0.1、GBK 解码成 UTF-8 再以 JSON 返回，
    页面（优先）从这里取数，彻底绕开编码问题，且带 CORS 可被 file:// 页面 fetch。

接口：
    GET /quote?codes=sh600000,sz000001
    -> 转发到 https://qt.gtimg.cn/q=...，GBK 解码为 UTF-8，返回 JSON：
       {"ok":true,"data":{"sh600000":"1~浦发银行~600000~...","sz000001":"..."}}

运行：
    python quote_proxy.py            # 默认监听 127.0.0.1:8777
    python quote_proxy.py 8900       # 自定义端口
"""
import sys
import json
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QT_URL = "https://qt.gtimg.cn/q="
DEFAULT_PORT = 8777


def fetch_qt(codes: str) -> str:
    """转发到腾讯行情接口，返回解码后的文本（GBK -> UTF-8）。"""
    url = QT_URL + codes
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://stockapp.finance.qq.com/",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, status: int = 200, ctype: str = "application/javascript; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/quote"):
            # 解析 ?codes=sh600000,sz000001
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            codes = ""
            for kv in qs.split("&"):
                if kv.startswith("codes="):
                    codes = urllib.parse.unquote(kv[len("codes="):])
                    break
            if not codes:
                self._send(b'{"ok":false,"error":"missing codes"}', status=400,
                           ctype="application/json; charset=utf-8")
                return
            try:
                text = fetch_qt(codes)
                # 解析 v_xxx="..."; 结构，组装成 JSON（与腾讯原始字段一致）
                data = {}
                for part in text.split(";"):
                    part = part.strip()
                    if not part.startswith("v_"):
                        continue
                    name, _, val = part.partition("=")
                    code = name[2:]  # 去掉 "v_" 前缀
                    data[code] = val.strip().strip('"')
                body = json.dumps({"ok": True, "data": data}, ensure_ascii=False).encode("utf-8")
                self._send(body, ctype="application/json; charset=utf-8")
            except urllib.error.URLError as e:
                self._send(f'{{"ok":false,"error":"upstream: {e}"}}'.encode("utf-8"), status=502,
                           ctype="application/json; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                self._send(f'{{"ok":false,"error":"{e}"}}'.encode("utf-8"), status=500,
                           ctype="application/json; charset=utf-8")
            return
        if self.path in ("/", "/health"):
            self._send(b'{"ok":true}', ctype="application/json; charset=utf-8")
            return
        self._send(b"not found", status=404)

    def log_message(self, fmt, *args):  # 安静日志
        sys.stdout.write("[quote_proxy] " + (fmt % args) + "\n")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[OK] 牛股计算器行情代理已启动： http://127.0.0.1:{port}/quote?codes=sh600000")
    print("     浏览器打开 bull_calc.html 即可，JSONP 失败会自动回退到这里。Ctrl+C 退出。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[quit] 代理已停止")
        srv.shutdown()


if __name__ == "__main__":
    main()
