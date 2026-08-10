#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NGA 大佬监控后端（纯标准库，无需 pip 安装任何第三方依赖）
===================================================================
核心能力：
  1. 监控：开启监控后，指定帖子(tid)下、指定作者(authorid)一旦有新发言，
     立即推送企业微信机器人。
    2. 配置：帖子ID（多个用逗号分隔）/ 作者ID（多个用逗号分隔）/ 监控开关
     / 刷新频率(默认60秒) / 展示方式(固定展示每个作者最后一页) / NGA 登录 UID 与 CID
     （启动时由页面生成的命令以 --uid/--cid/--target 传入，后端自动拼成 Cookie，
      不再依赖 nga_config.json 文件）。
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
PAGE_PATH = os.path.join(BASE_DIR, "..", "pages", "nga_monitor.html")
POSTS_CACHE_PATH = os.path.join(BASE_DIR, "nga_posts.json")   # 最近发言缓存（机器格式，页面数据源；gitignored）
DEBUG_PATH = os.path.join(BASE_DIR, "nga_debug_last.txt")     # 解析失败时的原始响应（gitignored）

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DEFAULT_CONFIG = {
    "targets": [{"tid": 47207407, "authorids": [150058]}],
    "tid": 47207407,            # 兼容旧配置（单帖子）
    "authorids": [150058],      # 兼容旧配置（全局作者）
    "monitor_enabled": True,
    "refresh_interval": 60,   # 秒
    "nga_uid": "",            # NGA 登录 UID（ngaPassportUid）
    "nga_cid": "",            # NGA 登录 CID（ngaPassportCid）
    "port": 8765,
}


class NgaError(Exception):
    """NGA 接口返回的业务错误（如权限不足）。"""


# ===================== 配置（仅内存，来自命令行） =====================
def load_config():
    """配置现仅来自命令行参数（--uid/--cid/--target/--interval 等）。

    自 2026-07-25 起不再读写 nga_config.json：UID/CID 与监控目标全部通过
    页面生成的启动命令（含 --uid/--cid/--target）传入，无磁盘持久化需求。
    此函数仅返回一份默认配置副本，供缺省值兜底。
    """
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    """配置无磁盘持久化（全部来自命令行 / 页面启动命令）。
    保留函数以兼容 update_config 与 /api/config 调用点，但不再写文件。
    """
    return


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
    """把 NGA 帖子内容转为可读纯文本：
      - 去掉 HTML 标签
      - 剔除 NGA BBCode 标记（[b]/[color=]/[url=]/[img]/[quote] 等），但保留标记内的真实文字
      - 去掉 NGA 自动生成的引用头部（[b]Reply … Post by … (date)[/b]）
    只删“标记/脚手架”，不删作者发言的正文文字（发言一字不丢）。
    """
    if not s:
        return ""
    # 1) HTML 标签 → 空格
    s = _TAG_RE.sub(" ", s)
    # 2) NGA 链接：保留文字，去掉 [url=...]/[url] 包裹
    s = re.sub(r"\[url=[^\]]*\](.*?)\[/url\]", r"\1", s, flags=re.S)
    s = re.sub(r"\[url\](.*?)\[/url\]", r"\1", s, flags=re.S)
    # 3) 图片标记：替换为「[图片]」占位（图片本身无文字，但保留非空以免推送内容变空）
    s = re.sub(r"\[img[^\]]*\].*?\[/img\]", " [图片] ", s, flags=re.S)
    # 4) NGA 引用头部：[b]Reply to [pid=..]Reply[/pid] Post by [uid=..]NAME[/uid] (date)[/b]
    #    用非贪婪匹配到首个 [/b] 为止——绝不吞掉 [/b] 之后的真实发言（曾因贪婪 [^\n]* 吃掉整条正文）。
    s = re.sub(r"\[b\]Reply\b.*?\[/b\]", " ", s, flags=re.S)
    # 5) 其余 BBCode 标记（[b] [/b] [color=..] [/color] [size=..] [quote] [/quote] [i] [u] [s:xx] [pid=..] [uid=..] 等）
    #    移除标记本身，保留标记内的文字
    s = re.sub(r"\[/?[a-zA-Z][a-zA-Z0-9]*[^\]]*\]", " ", s)
    # 6) HTML 实体
    for k, v in _ENTITY_MAP.items():
        s = s.replace(k, v)
    s = html.unescape(s)
    # 7) 折叠多余空白：横向空白压成单空格，纵向最多保留一个空行（保留段落结构）
    s = re.sub(r"[ \t　]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ===================== 解析器 =====================
def parse_posts_json(raw, tid, authorid, thread_title="", users=None):
    """解析 NGA lite JSON，返回帖子列表（含 pid/author名/authorid/ts/content/url/thread_title）。"""
    # strict=False：NGA lite 响应常在字符串值里夹带原始换行/制表等控制字符，
    # 默认 strict 模式会抛错导致整段解析失败、静默回退 HTML 后拿到 0 条。
    data = json.loads(raw, strict=False).get("data", {})
    if "error" in data:
        raise NgaError(str(data["error"].get("0", data["error"])))
    replies = data.get("__R") or data.get("replies") or []
    if isinstance(replies, dict):
        replies = list(replies.values())
    encode = data.get("encode", "")
    users = users or {}
    posts = []
    for r in replies:
        if not isinstance(r, dict):
            continue
        pid = _pick(r, "pid", "postid", "id")
        if pid is None:
            continue
        pid = str(pid)
        aid = _pick(r, "authorid", "uid") or authorid
        # 作者名在 __U（uid->用户名）映射里，reply 本身无此字段
        author = users.get(str(aid)) or _pick(r, "author", "authorname", "username") or ""
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
            "thread_title": thread_title,
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
        if tag == "a":
            nm = d.get("name", "")
            aid = d.get("id", "")
            pid = None
            if nm.startswith("pid"):
                pid = nm[3:]
            elif aid.startswith("pid") and aid.endswith("Anchor"):
                pid = aid[3:-len("Anchor")]
            if pid:
                if self._cur:
                    self._flush()
                self._cur = {"pid": pid, "author": "", "authorid": "",
                            "ts": 0, "time": "", "content": "", "content_text": ""}
                self._in_post = True
                self._buf = []
            elif "uid=" in d.get("href", ""):
                m = re.search(r"uid=(\d+)", d.get("href", ""))
                if m and self._cur:
                    self._cur["authorid"] = m.group(1)
        if self._in_post:
            cls = d.get("class", "")
            iid = d.get("id", "")
            if tag == "span" and ("postdate" in cls or iid.startswith("postdate")):
                self._buf.append("__DATE__")
            if tag in ("div", "p", "span") and "postcontent" in cls:
                self._in_content = True
                self._content_parts = []
        if self._in_content:
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if self._in_content:
            if self._stack and self._stack[-1] == tag:
                self._stack.pop()
            if not self._stack:
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
            if self._in_content and self._content_parts:
                self._cur["content"] = "".join(self._content_parts)
                self._cur["content_text"] = clean_text(self._cur["content"])
                self._in_content = False
                self._content_parts = []
            self._cur["url"] = (f"https://bbs.nga.cn/read.php?tid={{tid}}"
                                f"&authorid={self._cur['authorid']}#pid{self._cur['pid']}")
            self.posts.append(self._cur)
        self._cur = None
        self._in_post = False


def parse_posts_html(raw, tid, authorid, thread_title="", users=None):
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
        post["thread_title"] = thread_title
        post["url"] = (f"https://bbs.nga.cn/read.php?tid={tid}"
                       f"&authorid={post['authorid']}#pid{post['pid']}")
    return p.posts


def parse_posts(raw, tid, authorid, thread_title="", users=None):
    """先试 lite JSON；若响应以 { 开头却解析失败（多为被截断），直接报错，
    不再回退 HTML——HTML 兜底仅适用于真正的网页响应，对截断的 JSON 回退只会
    静默得到 0 条，掩盖「取不到最新发言」的问题。"""
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            return parse_posts_json(raw, tid, authorid, thread_title, users)
        except NgaError:
            raise
        except Exception as e:
            raise NgaError(f"NGA 响应解析失败（可能响应被截断）：{e}")
    return parse_posts_html(raw, tid, authorid, thread_title, users)


def _extract_thread_title(raw):
    """从 lite JSON 响应里抽取帖子标题（subject）。"""
    try:
        d = json.loads(raw, strict=False).get("data", {})
        t = d.get("__T") or {}
        return t.get("subject") or t.get("title") or d.get("subject") or ""
    except Exception:
        return ""


def _extract_users(raw):
    """从 lite JSON 响应里抽取用户映射：{uid(str): 用户名}。"""
    try:
        d = json.loads(raw, strict=False).get("data", {})
        U = d.get("__U") or {}
        out = {}
        for uid, u in U.items():
            if isinstance(u, dict):
                out[str(uid)] = u.get("username") or u.get("nickname") or ""
        return out
    except Exception:
        return {}


def _extract_thread_title_from_html(raw):
    """从 NGA 标准 HTML 页面的 <title> 中抽取帖子标题。"""
    m = re.search(r"<title>.*?-\s*(.+?)\s*-\s*中的回复", raw, re.S)
    if m:
        return m.group(1).strip()
    return ""


def _extract_username_from_html(raw, authorid):
    """从 NGA 标准 HTML 页面内嵌 userInfo.setAll({...}) 中抽取指定 uid 的用户名。"""
    m = re.search(r'"%s"\s*:\s*\{[^}]*?"username"\s*:\s*"([^"]*)"' % re.escape(str(authorid)), raw)
    return m.group(1) if m else ""


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
    """把一条新发言推送成企业微信消息（紧凑格式，尽量保留完整正文）。

    作者与时间分两行，分别带「作者：/时间：」前缀；
    作者有名称则不显示 UID（无名时回退显示 UID）；
    内容使用企业微信引用格式（每行前缀 "> "）；
    并按企业微信 markdown 正文 4096 字节上限自动截断——优先保住元数据，正文尽量多留。
    """
    author = post.get("author") or "未知"
    authorid = post.get("authorid") or "?"
    tm = post.get("time") or "未知"
    url = post.get("url") or ""
    text = post.get("content_text") or clean_text(post.get("content", ""))
    if not text.strip():
        text = "（该发言无文字内容，可能是纯图片/附件，请点链接查看）"

    # 作者有名称就不带 UID；无名时回退显示 UID 作为唯一标识
    author_disp = author if author != "未知" else f"UID {authorid}"
    # 内容每行加 "> " 前缀（企业微信引用格式）
    quoted = "> " + "\n> ".join(text.split("\n"))

    def build(q):
        return (
            f"🔔 NGA大佬新发言\n"
            f"作者：{author_disp}\n"
            f"时间：{tm}\n"
            f"{q}\n"
            f"🔗 [点此查看]({url})"
        )

    content = build(quoted)
    # 企业微信 markdown 正文上限 4096 字节；超了就从正文尾部截断，绝不丢元数据
    max_bytes = 4096
    if len(content.encode("utf-8")) > max_bytes:
        # 不含正文的模板体积 + 每行 ">" 前缀（首行 "> " 与每行分隔 "\n> " 合计 2×(换行数+1) 字节）
        overhead = len(build("").encode("utf-8")) + 2 * text.count("\n") + 2
        budget = max_bytes - overhead - 3            # 留 3 字节给省略号
        if budget > 0:
            cut = text.encode("utf-8")[:budget].decode("utf-8", "ignore")
            content = build("> " + "\n> ".join(cut.split("\n")) + "…")
    send_wecom_msg(content)




# ===================== 监控核心 =====================
class NgaMonitor:
    def __init__(self, config=None):
        self.config = config if config is not None else load_config()
        self.seen_pids = set()
        self.posts = []
        self.lock = threading.Lock()
        self.last_error = ""
        self.last_check = 0
        self.last_success = 0
        self.last_push = 0
        self.seeded = False
        self.thread_titles = {}   # tid(str) -> 帖子标题
        self.running = True
        self.startup_pushed = False  # 启动推送（开启/恢复通知）只发一次
        self.load_cache()        # 启动时恢复历史发言，使「开始监控后爬下来的帖子」跨重启保留

    def load_cache(self):
        """启动时从 nga_posts.json 恢复历史发言，使累积数据跨重启保留。

        恢复后把已存在的 pid 标记为已见（seeded=True），避免重启后把历史当新发言刷屏推送；
        但本轮真正新增（不在缓存中的）发言仍会正常推送。
        """
        try:
            if not os.path.exists(POSTS_CACHE_PATH):
                return
            with open(POSTS_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            posts = data.get("posts") or []
            if not posts:
                return
            with self.lock:
                self.posts = posts
                self.seen_pids = {str(p.get("pid")) for p in posts if p.get("pid")}
                for p in posts:
                    # 用最新 clean_text 重新生成 content_text（修复历史缓存里被旧规则清空/带标记的脏数据）
                    if "content" in p:
                        p["content_text"] = clean_text(p.get("content", ""))
                    tt = p.get("thread_title")
                    if tt:
                        self.thread_titles[str(p.get("tid"))] = tt
                if self.seen_pids:
                    self.seeded = True
            print(f"[{datetime.now():%H:%M:%S}] 已从缓存恢复 {len(posts)} 条历史发言。")
        except Exception as e:
            print(f"[缓存] 恢复失败: {e}")

    # ---------- 抓取 ----------
    def fetch_author_posts(self, tid, authorid):
        """抓取 (tid, authorid) 的最后一页（即最新发言）。

        NGA 会把过大的 page 参数夹紧到真实末页，所以直接请求 page=9999
        即可稳定拿到该作者最新的那一页；末页条数不固定（不一定是整 20 条），
        全部展示，不再按条数配置截断。

        优先用 lite=js JSON 接口（干净、含作者名）；若该接口对该作者截断/
        报错（某些发言多的作者会触发 NGA 服务端截断），则回退到浏览器同款
        的标准 HTML 页面解析，保证数据不丢。
        """
        raw = self._nga_get(tid, authorid, 9999, lite=True)
        try:
            title = _extract_thread_title(raw)
            if title:
                self.thread_titles[str(tid)] = title
            users = _extract_users(raw)
            return parse_posts(raw, tid, authorid, title, users)
        except NgaError as e:
            msg = str(e)
            # 登录/鉴权问题在 HTML 页面同样无解，直接向上抛
            if "403" in msg or "未授权" in msg or "未登录" in msg:
                raise
            # 其它（多为 lite 接口被截断/解析失败）→ 回退标准 HTML 页面
            return self._fetch_author_posts_html(tid, authorid)
        except json.JSONDecodeError:
            return self._fetch_author_posts_html(tid, authorid)

    def _fetch_author_posts_html(self, tid, authorid):
        """兜底：用标准 HTML 页面（即浏览器访问的页面）抓取该作者最新一页。"""
        html = self._nga_get(tid, authorid, 9999, lite=False)
        title = _extract_thread_title_from_html(html) or self.thread_titles.get(str(tid), "")
        if title:
            self.thread_titles[str(tid)] = title
        name = _extract_username_from_html(html, authorid)
        posts = parse_posts_html(html, tid, authorid, title, None)
        for p in posts:
            if not p.get("author") and name:
                p["author"] = name
        return posts

    def _nga_get(self, tid, authorid, page, lite=True, retries=3):
        """请求 NGA read.php（带登录 Cookie），返回解码后的响应文本。

        仅对网络层异常（连接错误/超时/5xx）做有限重试；NGA 业务错误（403/
        接口 error）与解析错误不重试——解析错误通常是服务端返回的截断响应，
        重试无意义，应直接向上抛出让调用方记录为「抓取失败」。
        """
        last_err = None
        for attempt in range(retries):
            try:
                if lite:
                    url = (f"https://bbs.nga.cn/read.php?tid={tid}&authorid={authorid}"
                           f"&lite=js&noprefix=1&page={page}")
                else:
                    url = (f"https://bbs.nga.cn/read.php?tid={tid}&authorid={authorid}"
                           f"&page={page}")
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA,
                    "Referer": f"https://bbs.nga.cn/read.php?tid={tid}",
                    "Cookie": _build_cookie(self.config),
                    "Accept": "*/*",
                })
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return _decode_resp(resp)
            except urllib.error.HTTPError as e:
                # 业务错误（403 / NGA 接口 error）：按原逻辑处理，不重试
                body = _decode_resp(e)
                if e.code == 403:
                    raise NgaError("NGA 返回 403 未授权（多半是未登录，请在配置中填写 NGA 登录 Cookie）")
                if body.strip().startswith("{"):
                    try:
                        d = json.loads(body, strict=False).get("data", {})
                        if "error" in d:
                            raise NgaError("NGA 返回错误：" + str(d["error"].get("0", d["error"]))
                                           + "（多半是未登录，请在配置中填写 NGA 登录 Cookie）")
                    except (NgaError, ValueError):
                        pass
                raise
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
        raise NgaError(f"网络请求失败（已重试 {retries} 次）：{last_err}")

    def _resolve_ids(self):
        """返回监控目标列表：[{tid:int, authorids:[int]}]。兼容旧的单 tid/authorids 配置。"""
        cfg = self.config
        targets = cfg.get("targets")
        if targets:
            norm = []
            for t in targets:
                try:
                    tid = int(t.get("tid"))
                except (TypeError, ValueError):
                    continue
                aids = t.get("authorids") or []
                aids = [int(x) for x in aids if str(x).strip()]
                norm.append({"tid": tid, "authorids": aids})
            if norm:
                return norm
        # 兼容旧配置
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
        return [{"tid": t, "authorids": aids} for t in tids]

    def check_once(self, do_push_startup=True):
        targets = self._resolve_ids()
        all_posts, errors = [], []
        for t in targets:
            tid = t["tid"]
            for aid in t["authorids"]:
                try:
                    all_posts.extend(self.fetch_author_posts(tid, aid))
                except NgaError as e:
                    errors.append(f"tid={tid},uid={aid}: {e}")
                except Exception as e:
                    errors.append(f"tid={tid},uid={aid}: 抓取失败 {e}")

        by_pid = {}
        for p in all_posts:
            by_pid[p["pid"]] = p

        with self.lock:
            # 累积：保留历史上所有已见发言（含更早轮次抓到、现已滚出末页的旧帖），
            # 仅把本次新出现的 pid 追加进来。这样「开始监控后爬下来的帖子」全部留存。
            existing = {p["pid"]: p for p in self.posts}
            new_posts = []
            for pid, p in by_pid.items():
                if pid not in existing:
                    existing[pid] = p
                    new_posts.append(p)
                else:
                    # 同一 pid 元数据可能更新（如帖子标题），但内容一般不会变，做轻量合并
                    existing[pid].update({k: v for k, v in p.items()
                                         if k in ("thread_title", "time", "ts")})
            posts = sorted(existing.values(), key=lambda x: x["ts"], reverse=True)

            self.posts = posts
            self.last_check = time.time()
            self.last_error = "; ".join(errors)
            if not errors:
                self.last_success = time.time()

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

            # 启动推送（开启/恢复通知）只发一次；未配置 webhook 或 --test 调试模式安全跳过
            if do_push_startup and not self.startup_pushed:
                self.push_startup(targets)
                self.startup_pushed = True
        self.save_cache()

    def push_startup(self, targets):
        """监控开启时推送一条企业微信通知，确认监控已启动并列出目标。"""
        if not WEBHOOK_URL:
            return
        lines = ["**NGA 大佬监控已开启**"]
        for t in targets:
            tid = t["tid"]
            title = self.thread_titles.get(str(tid), "")
            # 用已抓到的帖子反查作者名
            names = {}
            for p in self.posts:
                if str(p.get("tid")) == str(tid):
                    names[str(p.get("authorid"))] = p.get("author") or p.get("authorid")
            aid_label = "、".join(str(names.get(str(a), a)) for a in t.get("authorids", [])) or "全部"
            lines.append(f"> 帖子：{title or ('tid ' + str(tid))}")
            lines.append(f"> 作者：{aid_label}")
        lines.append(f"> 当前已缓存 {len(self.posts)} 条发言，新发言将实时推送。")
        send_wecom_msg("\n".join(lines))

    def save_cache(self):
        """把累积的全部发言原子写入 nga_posts.json（落盘即页面读取的唯一数据源）。

        采用「写临时文件 + os.replace 原子替换」：页面每次刷新都直接读这份文件，
        若用普通覆盖写，读取方可能在写入中途拿到半截 JSON 而报错；原子替换
        保证页面任何时候读到的都是完整的一整份文件。
        """
        try:
            with self.lock:
                posts = self.posts
                # 上限保护：长期运行避免无限膨胀，仅保留最新的若干条（仍远多于「末页」）
                CAP = 5000
                if len(posts) > CAP:
                    posts = posts[:CAP]
                data = {
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "posts": posts,
                }
                text = json.dumps(data, ensure_ascii=False, indent=2)
            tmp = POSTS_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, POSTS_CACHE_PATH)
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
            for k in ("tid", "authorids", "targets", "monitor_enabled", "refresh_interval",
                      "nga_uid", "nga_cid", "port"):
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
            targets = self._resolve_ids()
        tids = [t["tid"] for t in targets]
        aids = sorted({a for t in targets for a in t["authorids"]})
        # 返回累积的全部发言（前端按帖子分组并自行分页），不再截断为末页。
        visible = posts
        # 按帖子维度统计：配置目标优先排序，再补上历史残留的其它 tid
        thread_counts = {}
        thread_titles_map = {}
        for p in posts:
            t = str(p.get("tid"))
            thread_counts[t] = thread_counts.get(t, 0) + 1
            if p.get("thread_title"):
                thread_titles_map[t] = p["thread_title"]
        threads = [
            {"tid": t["tid"], "title": self.thread_titles.get(str(t["tid"]), ""),
             "count": thread_counts.get(str(t["tid"]), 0)}
            for t in targets
        ]
        seen_tids = {str(t["tid"]) for t in targets}
        for t, c in thread_counts.items():
            if t not in seen_tids:
                threads.append({"tid": t, "title": thread_titles_map.get(t, ""), "count": c})
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
                "targets": [
                    {"tid": t["tid"],
                     "title": self.thread_titles.get(str(t["tid"]), ""),
                     "authorids": t["authorids"]}
                    for t in targets
                ],
                "threads": threads,
                "refresh_interval": int(cfg.get("refresh_interval", 60)),
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
                # 页面只读接口：status 来自内存（轻量），posts 直接读落盘文件 nga_posts.json，
                # 因此页面本质就是在读取这份「监控收到新消息就自动保存」的文件（文件即唯一数据源）。
                status = monitor.snapshot().get("status")
                try:
                    with open(POSTS_CACHE_PATH, "r", encoding="utf-8") as f:
                        disk = json.load(f)
                    disk_posts = disk.get("posts") or []
                except FileNotFoundError:
                    disk_posts = []
                except Exception:
                    disk_posts = list(monitor.posts)
                status = dict(status or {})
                status["cached"] = len(disk_posts)
                self._send_json({"status": status, "posts": disk_posts})
            elif path == "/api/status":
                self._send_json(monitor.snapshot().get("status"))
            elif path == "/api/config":
                # 读取当前配置文件（页面用来回填，避免每次重输）
                cfg = monitor.config
                targets = monitor._resolve_ids()
                all_aids = sorted({x for t in targets for x in t["authorids"]})
                self._send_json({
                    "uid": cfg.get("nga_uid", ""),
                    "cid": cfg.get("nga_cid", ""),
                    "targets": [{"tid": t["tid"], "authorids": t["authorids"]} for t in targets],
                    "tid": targets[0]["tid"] if targets else "",
                    "aids": ",".join(str(a) for a in all_aids) if all_aids else "",
                    "interval": int(cfg.get("refresh_interval", 60)),
                    "monitor_enabled": cfg.get("monitor_enabled", True),
                    "port": int(cfg.get("port", 8765)),
                })
            else:
                self.send_error(404)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/config":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    raw = self.rfile.read(length) if length else b""
                    data = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    self._send_json({"ok": False, "error": "bad request"}, 400)
                    return
                cfg = monitor.config
                if "uid" in data: cfg["nga_uid"] = str(data["uid"])
                if "cid" in data: cfg["nga_cid"] = str(data["cid"])
                if "tid" in data and str(data["tid"]).strip() != "":
                    try: cfg["tid"] = int(data["tid"])
                    except Exception: pass
                if "aids" in data:
                    cfg["authorids"] = [int(x) for x in re.split(r"[,\s]+", str(data["aids"])) if x.strip()]
                if "interval" in data:
                    try: cfg["refresh_interval"] = max(5, int(data["interval"]))
                    except Exception: pass
                if "monitor_enabled" in data:
                    cfg["monitor_enabled"] = bool(data["monitor_enabled"])
                try:
                    save_config(cfg)
                    self._send_json({"ok": True})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)}, 500)
            else:
                self.send_error(404)

    return Handler


# ===================== 主程序 =====================
def main():
    # 强制 stdout/stderr 用 UTF-8，避免中文 Windows（GBK 控制台）打印非 ASCII 字符时崩溃
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    import argparse
    parser = argparse.ArgumentParser(description="NGA 大佬监控后端")
    parser.add_argument("--uid", help="NGA 登录 UID (ngaPassportUid)")
    parser.add_argument("--cid", help="NGA 登录 CID (ngaPassportCid)")
    parser.add_argument("--tid", help="帖子 ID（兼容单帖子用法）")
    parser.add_argument("--authorids", help="作者 ID，多个用逗号分隔（兼容单帖子用法）")
    parser.add_argument("--target", action="append", default=[],
                        help="监控目标，格式 tid:作者id1,作者id2，可重复指定多个帖子")
    parser.add_argument("--interval", type=int, help="刷新频率(秒)，默认 60")
    parser.add_argument("--test", action="store_true",
                        help="调试模式：抓一次并打印，不启动服务/不推送")
    args = parser.parse_args()

    # 配置现仅来自命令行参数（--uid/--cid/--target/--interval），以默认配置为基底再覆盖。
    # 不再依赖 nga_config.json 文件（UID/CID 与监控目标随启动命令一并传入，无磁盘持久化）。
    cfg = dict(DEFAULT_CONFIG)
    if args.uid:
        cfg["nga_uid"] = args.uid
    if args.cid:
        cfg["nga_cid"] = args.cid
    if args.tid:
        cfg["tid"] = int(args.tid)
    if args.authorids:
        cfg["authorids"] = [int(x) for x in re.split(r"[,\s]+", args.authorids) if x.strip()]
    if args.interval:
        cfg["refresh_interval"] = max(5, int(args.interval))
    # 多帖子多作者：--target tid:aid1,aid2 可重复
    cli_targets = []
    for t in (args.target or []):
        tid_s, _, aids_s = str(t).partition(":")
        if not tid_s.strip():
            continue
        aids = [int(x) for x in re.split(r"[,\s]+", aids_s) if x.strip()]
        cli_targets.append({"tid": int(tid_s), "authorids": aids})
    if cli_targets:
        cfg["targets"] = cli_targets

    if args.test:
        # 调试模式：抓一次并打印，不启动服务、不推送
        m = NgaMonitor(cfg)
        m.check_once(do_push_startup=False)
        with m.lock:
            posts = m.posts
            err = m.last_error
        print("\n--- 抓取结果 ---")
        print("错误:", err or "无")
        print("帖子数:", len(posts))
        for p in posts[:5]:
            print(f"  pid={p['pid']} uid={p['authorid']} {p['time']} {p['content_text'][:50]}")
        return

    monitor = NgaMonitor(cfg)
    port = int(monitor.config.get("port", 8765))
    targets = monitor._resolve_ids()

    # 启动即播种一次（不推送，仅启动确认推送）
    threading.Thread(target=monitor.check_once, daemon=True).start()
    # 监控循环
    threading.Thread(target=monitor.monitor_loop, daemon=True).start()

    handler = make_handler(monitor)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print("=" * 50)
    print("[OK] NGA 大佬监控已开启")
    print("=" * 50)
    print(f"监控目标（{len(targets)} 个帖子）：")
    for t in targets:
        print(f"  tid={t['tid']}  作者={t['authorids']}")
    print(f"刷新频率： {monitor.config.get('refresh_interval')} 秒    展示方式： 每个作者的最后一页(全量)")
    print(f"企微推送： {'已配置' if WEBHOOK_URL else '未配置（仅本地）'}")
    print(f"本地页面： http://127.0.0.1:{port}/")
    print(">>> 请回到浏览器打开 NGA 监控页面，点击「立即刷新」查看大佬最近发言。")
    print("Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n监控已停止。")
        monitor.running = False


if __name__ == "__main__":
    main()
