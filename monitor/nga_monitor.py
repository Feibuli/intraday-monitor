#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NGA 大佬监控后端（纯标准库，无需 pip 安装任何第三方依赖）
===================================================================
核心能力：
  1. 监控：开启监控后，指定帖子(tid)下、指定作者(authorid)一旦有新发言，
     立即推送企业微信机器人。
  2. 配置：帖子ID（多个用逗号分隔）/ 作者ID（多个用逗号分隔）/ 监控开关
     / 刷新频率(默认60秒) / 展示条数(默认30条) / NGA 登录 UID 与 CID
     （页面只填这两个 id，后端自动拼成 Cookie），全部可在页面内配置。
  3. 展示：内置 HTTP 服务，页面展示大佬最近发言，自动刷新。

为什么需要 NGA Cookie？
  NGA 的 read.php 对未登录(游客)访问会返回「权限不足」(error 15)。
  要监控某个帖子下的大佬发言，必须在配置里填入你浏览器登录后的
  ngaPassportUid / ngaPassportCid Cookie。详见页面内的「如何获取」说明。

数据来源：
  https://bbs.nga.cn/read.php?tid=<tid>&authorid=<authorid>&lite=js&noprefix=1
  （结构化 JSON，便于解析 pid / 作者 / 时间 / 内容）

依赖：仅 Python 标准库（urllib / json / time / http.server）。
"""

import sys
sys.stdout.reconfigure(line_buffering=True)  # 强制行缓冲，确保日志实时输出

import os
import re
import json
import time
import base64
import zlib
import shutil
import html
import html.parser
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ===================== 极简 .env 加载器（零依赖，复用企业微信 webhook） =====================
def _load_dotenv(path=None):
    """若环境变量尚无 WECHAT_WEBHOOK_URL，则从 monitor/.env 读取。.env 已被 .gitignore 忽略。"""
    if os.environ.get("WECHAT_WEBHOOK_URL"):
        return
    env_path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

WEBHOOK_URL = os.environ.get("WECHAT_WEBHOOK_URL", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "nga_config.json")
CONFIG_EXAMPLE = os.path.join(BASE_DIR, "nga_config.example.json")
PAGE_PATH = os.path.join(BASE_DIR, "..", "pages", "nga_monitor.html")
POSTS_CACHE_PATH = os.path.join(BASE_DIR, "nga_posts.json")   # 最近发言缓存（gitignored，可选持久化）
DEBUG_PATH = os.path.join(BASE_DIR, "nga_debug_last.txt")     # 解析失败时的原始响应（gitignored）

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DEFAULT_CONFIG = {
    "tid": 47207407,
    "authorids": [150058],
    "monitor_enabled": True,
    "refresh_interval": 60,   # 秒
    "display_count": 30,      # 条
    "nga_uid": "",            # NGA 登录 UID（ngaPassportUid）
    "nga_cid": "",            # NGA 登录 CID（ngaPassportCid）
    "port": 8765,
}


class NgaError(Exception):
    """NGA 接口返回的业务错误（如权限不足）。"""


# ===================== 配置读写 =====================
def load_config():
    """加载配置；缺失时从示例模板生成。始终补全默认值。"""
    if not os.path.exists(CONFIG_PATH):
        if os.path.exists(CONFIG_EXAMPLE):
            shutil.copy(CONFIG_EXAMPLE, CONFIG_PATH)
            print(f"[配置] 未找到 {os.path.basename(CONFIG_PATH)}，已用示例模板生成。")
        else:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ===================== 工具函数 =====================
def _pick(d, *keys, default=None):
    """从字典里按候选键取第一个非空值。"""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _build_cookie(cfg):
    """由 nga_uid / nga_cid 拼出 NGA 登录 Cookie 字符串（页面只让填这两个 id）。"""
    uid = cfg.get("nga_uid") or ""
    cid = cfg.get("nga_cid") or ""
    if uid or cid:
        return f"ngaPassportUid={uid}; ngaPassportCid={cid}"
    return ""


def _decode_resp(resp):
    """读取响应体并按正确编码(默认 GBK)解码为文本。

    NGA 的 lite/HTML 接口统一以 GBK (Content-Type: ...; charset=GBK) 返回，
    若按 UTF-8 解码中文会全部乱码。这里优先采用响应头声明的 charset，
    失败则回退 GBK/gb18030/UTF-8。
    """
    raw = resp.read()
    enc = "gbk"  # NGA 默认编码
    try:
        c = resp.headers.get_content_charset()
        if c:
            enc = c
    except Exception:
        pass
    for attempt in (enc, "gb18030", "utf-8"):
        try:
            return raw.decode(attempt)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "ignore")


def parse_ts(ts, ds):
    """解析 NGA 时间戳：优先 Unix 秒；否则尝试解析日期字符串。"""
    if ts:
        try:
            return int(ts)
        except (TypeError, ValueError):
            pass
    if ds:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return int(datetime.strptime(str(ds).strip(), fmt).timestamp())
            except ValueError:
                continue
    return 0


def decode_content(s, encode):
    """解码 NGA lite 帖子内容。无压缩直接返回；带压缩则 base64+zlib 解压。"""
    if not s:
        return ""
    if not encode or str(encode).lower() in ("0", "utf8", "utf-8", "none", ""):
        return s
    try:
            raw = base64.b64decode(s)
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8", "ignore")
            except Exception:
                return zlib.decompress(raw).decode("utf-8", "ignore")
    except Exception:
        return s


_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_MAP = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
               "&quot;": '"', "&apos;": "'", "&#39;": "'"}


def clean_text(s):
    """去除 HTML 标签与常见实体，得到纯文本（用于展示与推送）。"""
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    for k, v in _ENTITY_MAP.items():
        s = s.replace(k, v)
    s = html.unescape(s)
    s = re.sub(r"\[[^\]]*\]", " ", s)        # 去掉 [img] [quote] 等 NGA 标签
    # 去掉 NGA 引用前缀 "Reply Post by 用户名 (时间): "（时间冗余，列表已单独显示）
    # 注意原始内容形如 [quote]...[b]Post by [uid]名[/uid] (时间): [/b]正文 ，
    # 经上面的 [..] 标签清理后会变成 "  Reply  Post by 名 (时间): "，故用 \s+ 容忍空格。
    s = re.sub(r"^\s*Reply\s+Post\s+by\s+.+?\(\d{4}-\d{2}-\d{2}[^)]*\):\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ===================== 解析器 =====================
def parse_posts_json(raw, tid, authorid):
    """解析 NGA lite JSON，返回帖子列表（每个含 pid/author/authorid/ts/content/url）。"""
    data = json.loads(raw).get("data", {})
    if "error" in data:
        raise NgaError(str(data["error"].get("0", data["error"])))
    replies = data.get("__R") or data.get("replies") or []
    if isinstance(replies, dict):
        replies = list(replies.values())
    encode = data.get("encode", "")
    posts = []
    for r in replies:
        if not isinstance(r, dict):
            continue
        pid = _pick(r, "pid", "postid", "id")
        if pid is None:
            continue
        pid = str(pid)
        author = _pick(r, "author", "authorname", "username") or ""
        aid = _pick(r, "authorid", "uid") or authorid
        ts = parse_ts(_pick(r, "postdatetimestamp", "timestamp"), _pick(r, "postdate"))
        content = decode_content(_pick(r, "content", "msg", "text", default=""), encode)
        posts.append({
            "pid": pid,
            "author": str(author),
            "authorid": str(aid),
            "ts": ts,
            "time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "",
            "content": content,
            "content_text": clean_text(content),
            "tid": str(tid),
            "url": f"https://bbs.nga.cn/read.php?tid={tid}&authorid={authorid}#pid{pid}",
        })
    return posts


class _PostHTMLParser(html.parser.HTMLParser):
    """NGA read.php HTML 兜底解析：以 name=\"pidxxx\" 锚点切分帖子块。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self._in_post = False
        self._buf = []
        self._cur = None
        self._stack = []          # (tag, is_postcontent)
        self._in_content = False
        self._content_parts = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and d.get("name", "").startswith("pid"):
            # 新的帖子块开始
            if self._cur:
                self._flush()
            self._cur = {"pid": d["name"][3:], "author": "", "authorid": "",
                        "ts": 0, "time": "", "content": "", "content_text": ""}
            self._in_post = True
            self._buf = []
        if self._in_post:
            if tag == "a" and "uid=" in d.get("href", ""):
                m = re.search(r"uid=(\d+)", d.get("href", ""))
                if m and self._cur:
                    self._cur["authorid"] = m.group(1)
            if tag == "span" and "postdate" in d.get("class", ""):
                self._buf.append("__DATE__")
            if tag == "div" and "postcontent" in d.get("class", ""):
                self._in_content = True
                self._content_parts = []
        if self._in_content:
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if self._in_content:
            if self._stack and self._stack.pop() == tag:
                pass
            if tag == "div" and self._stack == []:
                self._in_content = False
                if self._cur:
                    self._cur["content"] = "".join(self._content_parts)
                    self._cur["content_text"] = clean_text(self._cur["content"])

    def handle_data(self, data):
        if self._in_post and self._buf and self._buf[-1] == "__DATE__":
            self._buf.pop()
            if self._cur:
                self._cur["time"] = data.strip()
                self._cur["ts"] = parse_ts(None, data.strip())
        if self._in_content:
            self._content_parts.append(data)

    def _flush(self):
        if self._cur and self._cur.get("pid"):
            self._cur["url"] = (f"https://bbs.nga.cn/read.php?tid={{tid}}"
                                f"&authorid={self._cur['authorid']}#pid{self._cur['pid']}")
            self.posts.append(self._cur)
        self._cur = None
        self._in_post = False


def parse_posts_html(raw, tid, authorid):
    """兜底：解析 NGA read.php HTML。"""
    p = _PostHTMLParser()
    try:
        p.feed(raw)
        p._flush()
    except Exception:
        pass
    for post in p.posts:
        post["tid"] = str(tid)
        post["authorid"] = post.get("authorid") or str(authorid)
        post["url"] = (f"https://bbs.nga.cn/read.php?tid={tid}"
                       f"&authorid={post['authorid']}#pid{post['pid']}")
    return p.posts


def parse_posts(raw, tid, authorid):
    """先试 lite JSON，失败则兜底 HTML。"""
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            return parse_posts_json(raw, tid, authorid)
        except NgaError:
            raise
        except Exception:
            pass
    return parse_posts_html(raw, tid, authorid)


# ===================== 企业微信推送 =====================
def send_wecom_msg(content):
    """发送企业微信 markdown 消息；未配置 webhook 时跳过。"""
    if not WEBHOOK_URL:
        print("[推送] 未配置 WECHAT_WEBHOOK_URL，跳过企业微信推送（仅本地运行）。")
        return False
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8", "ignore"))
        return result.get("errcode") == 0
    except Exception as e:
        print(f"[推送] 发送失败: {e}")
        return False


def push_post(post):
    """把一条新发言推送成企业微信消息。"""
    text = post.get("content_text") or clean_text(post.get("content", ""))
    if len(text) > 220:
        text = text[:220] + "…"
    content = (
        f"👤 **NGA 大佬发言提醒**\n"
        f"> 作者：{post.get('author') or '未知'}（UID {post.get('authorid')}）\n"
        f"> 时间：{post.get('time') or '未知'}\n"
        f"> 帖子：tid {post.get('tid')}\n"
        f"> 内容：{text}\n"
        f"> 🔗 [点此查看]({post.get('url')})"
    )
    send_wecom_msg(content)


# ===================== 监控核心 =====================
class NgaMonitor:
    def __init__(self):
        self.config = load_config()
        self.seen_pids = set()
        self.posts = []
        self.lock = threading.Lock()
        self.last_error = ""
        self.last_check = 0
        self.last_success = 0
        self.last_push = 0
        self.seeded = False
        self.running = True

    # ---------- 抓取 ----------
    def fetch_author_posts(self, tid, authorid):
        """抓取 (tid, authorid) 的帖子，自动翻到最后一页以拿到最新发言。"""
        def _get(page):
            url = (f"https://bbs.nga.cn/read.php?tid={tid}&authorid={authorid}"
                   f"&lite=js&noprefix=1&page={page}")
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": f"https://bbs.nga.cn/read.php?tid={tid}",
                "Cookie": _build_cookie(self.config),
                "Accept": "*/*",
            })
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return _decode_resp(resp)
            except urllib.error.HTTPError as e:
                body = _decode_resp(e)
                if e.code == 403:
                    raise NgaError("NGA 返回 403 未授权（多半是未登录，请在配置中填写 NGA 登录 Cookie）")
                if body.strip().startswith("{"):
                    try:
                        d = json.loads(body).get("data", {})
                        if "error" in d:
                            raise NgaError("NGA 返回错误：" + str(d["error"].get("0", d["error"]))
                                           + "（多半是未登录，请在配置中填写 NGA 登录 Cookie）")
                    except (NgaError, ValueError):
                        pass
                raise

        raw = _get(1)
        posts = parse_posts(raw, tid, authorid)
        # 尝试翻到最后一页（最新发言通常在末页）
        try:
            data = json.loads(raw).get("data", {})
            pinfo = data.get("__P") or {}
            total = int(pinfo.get("totalpages") or pinfo.get("maxpage")
                        or pinfo.get("pages") or pinfo.get("allpages") or 1)
            if total > 1:
                raw2 = _get(total)
                posts2 = parse_posts(raw2, tid, authorid)
                if posts2:
                    posts = posts2
        except Exception:
            pass
        return posts

    def _resolve_ids(self):
        cfg = self.config
        tids = cfg.get("tid", DEFAULT_CONFIG["tid"])
        if isinstance(tids, str):
            tids = [int(x) for x in re.split(r"[,\s]+", tids) if x.strip()]
        elif isinstance(tids, int):
            tids = [tids]
        aids = cfg.get("authorids", DEFAULT_CONFIG["authorids"])
        if isinstance(aids, str):
            aids = [int(x) for x in re.split(r"[,\s]+", aids) if x.strip()]
        elif isinstance(aids, int):
            aids = [aids]
        return tids, aids

    def check_once(self):
        tids, aids = self._resolve_ids()
        all_posts, errors = [], []
        for tid in tids:
            for aid in aids:
                try:
                    all_posts.extend(self.fetch_author_posts(tid, aid))
                except NgaError as e:
                    errors.append(f"tid={tid},uid={aid}: {e}")
                except Exception as e:
                    errors.append(f"tid={tid},uid={aid}: 抓取失败 {e}")

        by_pid = {}
        for p in all_posts:
            by_pid[p["pid"]] = p
        posts = sorted(by_pid.values(), key=lambda x: x["ts"], reverse=True)

        with self.lock:
            self.posts = posts
            self.last_check = time.time()
            self.last_error = "; ".join(errors)
            if not errors:
                self.last_success = time.time()

            new_posts = [p for p in posts if p["pid"] not in self.seen_pids]
            if not self.seeded:
                # 首次/配置变更后的静默播种：只记录已见，不推送历史
                self.seen_pids.update(p["pid"] for p in posts)
                self.seeded = True
                print(f"[{datetime.now():%H:%M:%S}] 已播种 {len(posts)} 条历史发言（不推送）。")
            else:
                if self.config.get("monitor_enabled") and new_posts:
                    for p in sorted(new_posts, key=lambda x: x["ts"]):
                        print(f"[{datetime.now():%H:%M:%S}] 新发言 uid={p['authorid']} pid={p['pid']} -> 推送")
                        push_post(p)
                    self.seen_pids.update(p["pid"] for p in new_posts)
                    self.last_push = time.time()
                    print(f"[{datetime.now():%H:%M:%S}] 本轮新增 {len(new_posts)} 条，已推送。")
        self.save_cache()

    def save_cache(self):
        try:
            with self.lock:
                data = {
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "posts": self.posts[:200],
                }
            with open(POSTS_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def dump_debug(self, raw):
        try:
            with open(DEBUG_PATH, "w", encoding="utf-8") as f:
                f.write(raw[:20000])
        except Exception:
            pass

    def monitor_loop(self):
        while self.running:
            try:
                self.check_once()
            except Exception as e:
                with self.lock:
                    self.last_error = f"循环异常: {e}"
            with self.lock:
                interval = max(5, int(self.config.get("refresh_interval", 60)))
            time.sleep(interval)

    def update_config(self, new_cfg):
        """应用前端提交的新配置，并触发一次静默重新播种（避免改配置时刷屏推送）。

        注意：监控与页面已解耦——此接口保留仅供脚本自身/调试使用，页面不调用。
        """
        with self.lock:
            for k in ("tid", "authorids", "monitor_enabled", "refresh_interval",
                      "display_count", "nga_uid", "nga_cid", "port"):
                if k in new_cfg:
                    self.config[k] = new_cfg[k]
            save_config(self.config)
            self.seeded = False  # 下一轮重新播种，不推送历史
        threading.Thread(target=self.check_once, daemon=True).start()

    # ---------- HTTP 接口数据（只读，供页面展示用） ----------
    def snapshot(self):
        with self.lock:
            cfg = dict(self.config)
            posts = list(self.posts)
            last_error = self.last_error
            last_check = self.last_check
            last_success = self.last_success
            last_push = self.last_push
            monitor_enabled = cfg.get("monitor_enabled")
            tids, aids = self._resolve_ids()
        display = int(cfg.get("display_count", 30))
        visible = posts[:display]
        out = {
            "status": {
                "monitor_enabled": monitor_enabled,
                "running": True,
                "last_check": last_check,
                "last_success": last_success,
                "last_push": last_push,
                "last_error": last_error,
                "cached": len(posts),
                "has_cookie": bool(cfg.get("nga_uid") and cfg.get("nga_cid")),
                "tids": tids,
                "authorids": aids,
                "refresh_interval": int(cfg.get("refresh_interval", 60)),
                "display_count": display,
                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "posts": visible,
        }
        return out


# ===================== HTTP 服务 =====================
def make_handler(monitor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 静默访问日志

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send_json(self, obj, status=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path, ctype):
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except Exception:
                self.send_error(404)
                return
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send_file(PAGE_PATH, "text/html; charset=utf-8")
            elif path == "/api/posts":
                # 页面只读接口：返回 { status, posts }
                self._send_json(monitor.snapshot())
            elif path == "/api/status":
                self._send_json(monitor.snapshot().get("status"))
            else:
                self.send_error(404)

    return Handler


# ===================== 主程序 =====================
def main():
    print("=" * 50)
    print("NGA 大佬监控后端")
    print("=" * 50)

    if "--test" in sys.argv:
        # 调试模式：抓一次并打印，不启动服务、不推送
        m = NgaMonitor()
        m.check_once()
        with m.lock:
            posts = m.posts
            err = m.last_error
        print("\n--- 抓取结果 ---")
        print("错误:", err or "无")
        print("帖子数:", len(posts))
        for p in posts[:5]:
            print(f"  pid={p['pid']} uid={p['authorid']} {p['time']} {p['content_text'][:50]}")
        return

    monitor = NgaMonitor()
    port = int(monitor.config.get("port", 8765))

    # 启动即播种一次（不推送）
    threading.Thread(target=monitor.check_once, daemon=True).start()
    # 监控循环
    threading.Thread(target=monitor.monitor_loop, daemon=True).start()

    handler = make_handler(monitor)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"NGA 监控后端已启动： http://127.0.0.1:{port}/")
    print(f"监控目标： tid={monitor.config.get('tid')}  authorids={monitor.config.get('authorids')}")
    print(f"监控推送： {'开启' if monitor.config.get('monitor_enabled') else '关闭'}"
          f"   刷新频率：{monitor.config.get('refresh_interval')}秒"
          f"   展示：{monitor.config.get('display_count')}条")
    print("企业微信：", "已配置" if WEBHOOK_URL else "未配置（仅本地）")
    print("Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n监控已停止。")
        monitor.running = False


if __name__ == "__main__":
    main()
