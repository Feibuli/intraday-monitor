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

# ---- 缓存防丢保护 ----
# 历史教训：nga_posts.json 曾发生「3000+ 条历史悄无声息变成 355 条」。原因是
# 存档只有单份文件、覆盖式写入，且 load_cache 读取失败时只打印一行就当空库启动，
# 于是下一轮 save_cache 用少量数据整体覆盖，连坏掉的那份都救不回来。
# 以下三重保护针对此设计：
#   1) 轮转备份：写新档前定期把旧档存成 .bak1~.bak3，写坏了还能回滚
#   2) 现场保护：读取失败不删除、不静默清空，改名留存 .corrupt-<时间戳> 并从备份恢复
#   3) 覆盖熔断：内存条数远少于磁盘条数时拒绝覆盖，另存 .suspect-<时间戳> 并告警
CACHE_BACKUP_KEEP = 3          # 保留几份轮转备份
CACHE_BACKUP_INTERVAL = 1800   # 两次轮转备份的最小间隔（秒），避免每分钟复制 386KB
CACHE_GUARD_RATIO = 0.5        # 内存条数低于磁盘条数的此比例时触发熔断，拒绝覆盖

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ===== 回溯补抓 =====
# 启动监控后做**一次**回溯：从该作者末页的前一页开始往前翻，
#   1. 累计翻到 BACKFILL_MAX_POSTS 条就停（注意是**条数**不是页数，可配置）；
#   2. 中途碰到「已经抓过的发言」（pid 在缓存里）也立刻停——说明断档到头了；
#   3. 补回来的发言按时间正序推送。
# 不再单独跑回溯服务/线程，也不再定期反复回溯。
BACKFILL_MAX_POSTS = 50          # 往前最多回溯多少**条**（--backfill-posts 可配）

# 翻页之间的间隔（2026-10-24 起每轮只翻 1 页，间隔主要由轮询周期决定，
# 这里只是让它别和本轮那十几个常规请求挤在一起）。
BACKFILL_PAGE_DELAY = 2.0
# 同一个组合连续翻页失败多少次才放弃。回溯对耗时不敏感但不能少，所以失败
# **不减少深度**，页码原样保留、下一轮重试；只有连续失败到这个次数
# （说明该页确实取不到，多半是帖子权限问题）才跳过。
BACKFILL_MAX_FAILS = 10

# 常规轮询里每个 (帖子, 作者) 之间的间隔。站点风控看的是**速率**——
# 一口气 2 次/秒连发十几次必吃 302，摊成 0.5 次/秒则完全没事。因此宁可把一轮的
# 抓取摊到 ~20 秒发完，也不要挤在几秒内打过去。
FETCH_COMBO_DELAY = 1.5

# 推送的「新旧」判据：
# 只看「pid 是否在缓存里」是不够的——存档一旦缺失（被清/损坏/换机器），
# 往前翻出来的全是几个月前的老帖，pid 都不在缓存里 → 会被当成新发言全量推送。
# 因此推送前再用时间判一道：只推 PUSH_MAX_AGE_HOURS 以内的发言，更老的
# 只入库、不推送（页面照样能看）。
PUSH_MAX_AGE_HOURS = 168      # 7 天；超过这个岁数的发言只入库不推送

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
def _page_seg(page):
    """拼接 read.php 的 page 参数：能确定具体页码就用页码，否则用 9999。

    NGA 会把过大的 page 参数夹紧到真实末页（实测请求 page=9999 即返回末页），
    所以「拿不到具体页码」时给 9999 就等于末页，无需用 e。
    NGA 的 authorid 过滤视图按时间正序分页（每页 20 条），新发言只追加到末页，
    旧发言的页码不会前移——抓取时记录下来的页码对这条发言永久有效。
    """
    try:
        n = int(page)
        if n > 0:
            return f"page={n}"
    except (TypeError, ValueError):
        pass
    return "page=9999"


def normalize_post_url(u):
    """统一发言链接格式：去掉 #pid 锚点，page 用具体页码（拿不到则 9999）。

    历史缓存里可能混着三种旧格式（无 page / page=e / 带 #pid），这里一次性归一。
    """
    if not u:
        return u
    base = u.split("#", 1)[0]                       # 去掉 #pid 锚点
    base = re.sub(r"([?&])page=e\b", r"\1page=9999", base)   # page=e -> 9999
    if "page=" not in base:
        base = base + ("&" if "?" in base else "?") + "page=9999"
    return base


def _extract_page_from_lite(raw):
    """从 lite JSON 响应里抽取当前页码（data.__PAGE）。

    请求 page=9999 时 NGA 会夹紧到真实末页，并在 __PAGE 里回报实际页码。
    """
    try:
        n = int(json.loads(raw, strict=False).get("data", {}).get("__PAGE"))
        return n if n > 0 else None
    except Exception:
        return None


def _extract_page_from_html(raw):
    """从标准 HTML 页面抽取当前页码。

    页面内嵌 `var __PAGE = {0:'...',1:总页数,2:当前页,3:每页条数}`，取索引 2；
    取不到时退回用分页条的「上一页(N)」推算（当前页 = N+1）。
    """
    m = re.search(r"var __PAGE\s*=\s*\{([^}]*)\}", raw or "")
    if m:
        parts = [x.strip() for x in m.group(1).split(",")]
        if len(parts) >= 3 and ":" in parts[2]:
            try:
                n = int(parts[2].split(":", 1)[1])
                if n > 0:
                    return n
            except (TypeError, ValueError):
                pass
    m = re.search(r"上一页\((\d+)\)", raw or "")
    if m:
        try:
            return int(m.group(1)) + 1
        except (TypeError, ValueError):
            pass
    return None


def parse_posts_json(raw, tid, authorid, thread_title="", users=None, page=None):
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
            "url": f"https://bbs.nga.cn/read.php?tid={tid}&authorid={authorid}"
                   f"&{_page_seg(page)}",
        })
    return posts


class _PostHTMLParser(html.parser.HTMLParser):
    """NGA read.php HTML 兜底解析：以 name=\"pidxxx\" 锚点切分帖子块。"""
    def __init__(self, page=None):
        super().__init__(convert_charrefs=True)
        self.page = page
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
                                f"&authorid={self._cur['authorid']}"
                                f"&{_page_seg(self.page)}")
            self.posts.append(self._cur)
        self._cur = None
        self._in_post = False


def parse_posts_html(raw, tid, authorid, thread_title="", users=None, page=None):
    """兜底：解析 NGA read.php HTML。"""
    p = _PostHTMLParser(page=page)
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
                       f"&authorid={post['authorid']}&{_page_seg(page)}")
    return p.posts


def parse_posts(raw, tid, authorid, thread_title="", users=None, page=None):
    """先试 lite JSON；若响应以 { 开头却解析失败（多为被截断），直接报错，
    不再回退 HTML——HTML 兜底仅适用于真正的网页响应，对截断的 JSON 回退只会
    静默得到 0 条，掩盖「取不到最新发言」的问题。"""
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            return parse_posts_json(raw, tid, authorid, thread_title, users, page=page)
        except NgaError:
            raise
        except Exception as e:
            raise NgaError(f"NGA 响应解析失败（可能响应被截断）：{e}")
    return parse_posts_html(raw, tid, authorid, thread_title, users, page=page)


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
    return send_wecom_msg(content)




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
        self._name_cache = {}     # (tid, uids元组) -> 解析结果，供 /api/resolve 复用
        self.running = True
        self.startup_pushed = False  # 启动推送（开启/恢复通知）只发一次
        # 回溯补抓的分片状态（2026-10-24：每轮只翻 1 页，慢慢补完，不减深度）
        self._bf_queue = None        # None=未初始化；[] 或 None 之外=待回溯的 (tid,aid) 队列
        self._bf_page = {}           # (tid, aid) -> 下一页要翻的页码
        self._bf_got = {}            # (tid, aid) -> 已补回条数
        self._bf_fail = {}           # (tid, aid) -> 连续失败次数
        self.rounds = 0              # 已执行的轮询轮数（用于定期回溯）
        self.last_pages = {}         # (tid, 作者) -> 该作者末页页码（本轮抓取时更新）
        # 回溯补回的发言是否推送企微。仅在「清理历史存量断档」时用
        # --no-backfill-push 关掉，避免一次性补回几十条把企微刷屏；
        # 补回的发言照样入库，页面上照常可见。
        self.no_backfill_push = bool((config or {}).get("no_backfill_push", False))
        self.load_cache()        # 启动时恢复历史发言，使「开始监控后爬下来的帖子」跨重启保留

    def load_cache(self):
        """启动时从 nga_posts.json 恢复历史发言，使累积数据跨重启保留。

        恢复后把已存在的 pid 标记为已见（seeded=True），避免重启后把历史当新发言刷屏推送；
        但本轮真正新增（不在缓存中的）发言仍会正常推送。
        """
        try:
            if not os.path.exists(POSTS_CACHE_PATH):
                self._cache_count_cached = 0
                return
            try:
                with open(POSTS_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                posts = data.get("posts") or []
            except Exception as e:
                # 关键：绝不静默丢缓存。先把坏文件原样留存，再尝试从轮转备份恢复。
                corrupt = f"{POSTS_CACHE_PATH}.corrupt-{datetime.now():%Y%m%d_%H%M%S}.json"
                try:
                    os.replace(POSTS_CACHE_PATH, corrupt)
                except Exception:
                    pass
                recovered = self._load_cache_from_backup()
                self._alert(
                    f"缓存读取失败：{e}\n"
                    f"损坏文件已留存：{corrupt}\n"
                    + (f"已自动从备份恢复 {recovered} 条。\n"
                       if recovered else
                       "所有备份均不可用，本次将从空库开始（历史发言需重新抓取）。\n")
                    + "旧发言仍可用回溯补抓找回，不会立刻推送。"
                )
                return
            if not posts:
                # 存档存在但一条都没有，同样可疑——先看看备份里有没有更多
                recovered = self._load_cache_from_backup(silent_if_empty=True)
                if recovered:
                    self._alert(
                        f"存档内发言数为 0，已自动从备份恢复 {recovered} 条。"
                    )
                    return
                self._cache_count_cached = 0
                return
            with self.lock:
                self.posts = posts
                self.seen_pids = {str(p.get("pid")) for p in posts if p.get("pid")}
                for p in posts:
                    # 用最新 clean_text 重新生成 content_text（修复历史缓存里被旧规则清空/带标记的脏数据）
                    if "content" in p:
                        p["content_text"] = clean_text(p.get("content", ""))
                    # 历史链接归一：去掉 #pid 锚点，page 统一（无 page 或 page=e 都换成 9999）
                    p["url"] = normalize_post_url(p.get("url") or "")
                    tt = p.get("thread_title")
                    if tt:
                        self.thread_titles[str(p.get("tid"))] = tt
                if self.seen_pids:
                    self.seeded = True
                self._cache_count_cached = len(posts)
            span = ""
            try:
                ts = [p.get("ts", 0) for p in posts if p.get("ts")]
                if ts:
                    span = (f"（{datetime.fromtimestamp(min(ts)):%Y-%m-%d %H:%M} ~ "
                            f"{datetime.fromtimestamp(max(ts)):%Y-%m-%d %H:%M}）")
            except Exception:
                pass
            print(f"[{datetime.now():%H:%M:%S}] 已从缓存恢复 {len(posts)} 条历史发言{span}。")
            # 备份里条数明显更多时提示一下，便于发现「悄悄变少」的情况
            try:
                bak = f"{POSTS_CACHE_PATH}.bak1"
                if os.path.exists(bak):
                    with open(bak, "r", encoding="utf-8") as f:
                        n = len((json.load(f) or {}).get("posts") or [])
                    if n > len(posts) * 2 and n > 200:
                        print(f"[{datetime.now():%H:%M:%S}] [提示] 上一份备份有 {n} 条，"
                              f"当前存档仅 {len(posts)} 条，若不符预期可手工取用 {bak}")
            except Exception:
                pass
        except Exception as e:
            print(f"[缓存] 恢复失败: {e}")

    def _load_cache_from_backup(self, silent_if_empty=False):
        """从 .bak1~.bakN 依次尝试恢复缓存，成功返回条数（0 表示都不可用）。"""
        for i in range(1, CACHE_BACKUP_KEEP + 1):
            path = f"{POSTS_CACHE_PATH}.bak{i}"
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    posts = (json.load(f) or {}).get("posts") or []
                if not posts:
                    continue
                with self.lock:
                    self.posts = posts
                    self.seen_pids = {str(p.get("pid")) for p in posts if p.get("pid")}
                    for p in posts:
                        if "content" in p:
                            p["content_text"] = clean_text(p.get("content", ""))
                        p["url"] = normalize_post_url(p.get("url") or "")
                        tt = p.get("thread_title")
                        if tt:
                            self.thread_titles[str(p.get("tid"))] = tt
                    if self.seen_pids:
                        self.seeded = True
                    self._cache_count_cached = len(posts)
                print(f"[{datetime.now():%H:%M:%S}] 已从备份 {path} 恢复 {len(posts)} 条。")
                return len(posts)
            except Exception:
                continue
        return 0

    # ---------- 抓取 ----------
    def fetch_author_posts(self, tid, authorid, page=9999):
        """抓取 (tid, authorid) 某一页发言，返回 (posts, real_page)。

        page 默认 9999：NGA 会把过大的 page 夹紧到真实末页，即拿到该作者最新的
        那一页；末页条数不固定（不一定是整 20 条），全部返回，不再按条数截断。
        显式传 page=N 则抓第 N 页（回溯补抓时用）。

        real_page 为 NGA 回报的真实页码，用于生成可精确定位的发言链接。

        优先用 lite=js JSON 接口（干净、含作者名）；若该接口对该作者截断/
        报错（某些发言多的作者会触发 NGA 服务端截断），则回退到浏览器同款
        的标准 HTML 页面解析，保证数据不丢。
        """
        raw = self._nga_get(tid, authorid, page, lite=True)
        try:
            title = _extract_thread_title(raw)
            if title:
                self.thread_titles[str(tid)] = title
            users = _extract_users(raw)
            # 记下这些发言所在的页码：NGA 已把 page=9999 夹紧到末页，__PAGE 即真实页码
            real_page = _extract_page_from_lite(raw)
            return parse_posts(raw, tid, authorid, title, users, page=real_page), real_page
        except NgaError as e:
            msg = str(e)
            # 登录/鉴权问题在 HTML 页面同样无解，直接向上抛；风控 302 同理
            # （换 HTML 接口也会被挡，回退只会浪费一次请求额度）
            if "403" in msg or "未授权" in msg or "未登录" in msg or "重定向" in msg:
                raise
            # 其它（多为 lite 接口被截断/解析失败）→ 回退标准 HTML 页面
            return self._fetch_author_posts_html(tid, authorid, page)
        except json.JSONDecodeError:
            return self._fetch_author_posts_html(tid, authorid, page)

    def resolve_names(self, tid, uids):
        """把 tid / 作者 UID 反查成「帖子标题 + 作者名」，供页面配置区显示。

        配置区只填了一串数字，根本看不出谁是谁，所以页面每次渲染目标卡片时
        调一次 /api/resolve 把名字查出来（页面自己再存 localStorage 兜底）。

        节流：先查本地已有的（thread_titles / 已抓到的发言），缺的才发一次
        网络请求；结果按 (tid, uids) 缓存，同一组合只查一次。
        """
        tid = str(tid).strip()
        uids = [str(u).strip() for u in (uids or []) if str(u).strip()]
        key = (tid, tuple(sorted(uids)))
        with self.lock:
            hit = self._name_cache.get(key)
        if hit:
            return hit

        title = self.thread_titles.get(tid, "")
        users, err = {}, ""
        with self.lock:
            for p in self.posts:
                if str(p.get("tid")) != tid:
                    continue
                if not title and p.get("thread_title"):
                    title = p["thread_title"]
                aid, au = p.get("authorid"), p.get("author")
                if aid and au:
                    users[str(aid)] = au
        missing = [u for u in uids if u not in users]
        if uids and (missing or not title):
            aid = missing[0] if missing else uids[0]
            try:
                posts, _ = self.fetch_author_posts(tid, aid, 9999)
                title = title or self.thread_titles.get(tid, "") or ""
                for p in posts:
                    if p.get("authorid") and p.get("author"):
                        users[str(p["authorid"])] = p["author"]
                    if not title and p.get("thread_title"):
                        title = p["thread_title"]
            except Exception as e:
                err = str(e)
        res = {"ok": True, "tid": tid, "title": title or "",
               "users": users, "error": err}
        with self.lock:
            self._name_cache[key] = res
        return res

    def _fetch_author_posts_html(self, tid, authorid, page=9999):
        """兜底：用标准 HTML 页面（即浏览器访问的页面）抓取该作者某一页。"""
        html = self._nga_get(tid, authorid, page, lite=False)
        title = _extract_thread_title_from_html(html) or self.thread_titles.get(str(tid), "")
        if title:
            self.thread_titles[str(tid)] = title
        name = _extract_username_from_html(html, authorid)
        real_page = _extract_page_from_html(html)
        posts = parse_posts_html(html, tid, authorid, title, None, page=real_page)
        for p in posts:
            if not p.get("author") and name:
                p["author"] = name
        return posts, real_page

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
                if 300 <= e.code < 400:
                    # NGA 风控：短时间请求过多时 read.php 会返回 302 并把请求重定向成
                    # 死循环。此时换 HTML 接口也一样被挡，直接向上抛，让调用方
                    # （回溯补抓）识趣地停手、把预算留到下一轮。
                    raise NgaError(f"NGA 返回 {e.code} 重定向循环（触发风控，请求过于频繁）")
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

    def backfill_one_page(self, tid, authorid, page, known_pids,
                          max_posts=BACKFILL_MAX_POSTS, already=0):
        """回溯补抓的**单页**步骤：翻一页，返回这一页里漏抓的发言。

        规则：
          1. 累计翻到 max_posts **条**（不是页数）就停；
          2. 中途碰到「已经抓过的发言」（pid 在 known_pids 里）立刻停——
             说明这一段本来就是连续的、没有断档，再往前翻都是抓过的；
          3. 每页内部按**从新到旧**遍历（NGA 一页内是时间正序），先补离末页近的。

        返回 (这一页补回的发言列表(时间正序), 是否到此为止, 下一页页码)。
        """
        try:
            posts, _ = self.fetch_author_posts(tid, authorid, page)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] 回溯翻页失败 tid={tid} "
                  f"uid={authorid} page={page}: {e}")
            return [], False, page      # 页码原样返回 → 上层下轮重试，不减少深度
        if not posts:
            return [], True, page - 1   # 空页 = 到头
        found, stop = [], False
        for post in reversed(posts):    # 页内从新到旧
            if post.get("pid") in known_pids:
                stop = True             # 碰到抓过的 → 断档到头，收工
                break
            found.append(post)
            if already + len(found) >= max_posts:
                stop = True
                break
        return list(reversed(found)), stop, page - 1

    def _bf_init(self, targets):
        """初始化回溯队列：把「有历史记录且拿到了末页」的组合排队。

        只做一次。没有历史（新目标）或本轮没抓到末页的组合直接跳过——
        没有「上次抓到哪」的基准，回溯没有意义。
        """
        q = []
        for t in targets:
            tid = t["tid"]
            for aid in t.get("authorids", []):
                key = (str(tid), str(aid))
                last_page = self.last_pages.get(key)
                with self.lock:
                    known = {p["pid"] for p in self.posts
                             if str(p.get("tid")) == str(tid)
                             and str(p.get("authorid")) == str(aid)}
                if not last_page or last_page <= 1 or not known:
                    continue
                self._bf_page[key] = last_page - 1
                self._bf_got[key] = 0
                self._bf_fail[key] = 0
                q.append(key)
        self._bf_queue = q
        print(f"[{datetime.now():%H:%M:%S}] 回溯补抓：{len(q)} 个目标排队，"
              f"每轮翻 1 页慢慢补（不减深度，翻不动的下轮重试）。")

    def _backfill(self, targets, all_posts):
        """启动后把漏抓的发言补回来——**分片执行**：每轮只翻 1 页。

        ⚠️ 分片执行的原因：一次跑完要在几分钟内连发数十次翻页请求，容易触发
        站点风控（302），导致整批补抓一条都拿不到。所以：
          - 每轮只发 **1 次**翻页请求（和常规抓取一起，一分钟总共十几个请求）；
          - 翻不动（302）的页码**原样保留、下一轮重试**，不减少回溯深度；
          - 每个组合翻够上限条数 / 碰到已抓过的 / 到第一页 才出队。

        语义上仍然是「启动后做一次」——一次完整的扫描，不是定期反复回溯；
        也不单开回溯服务/线程。全部出队后本函数自动空转。

        返回本轮补回的发言列表（供调用方区分「补回的」与「常规新发言」）。
        """
        if self._bf_queue is None:
            self._bf_init(targets)
        if not self._bf_queue:
            return []
        max_posts = int((self.config or {}).get("backfill_max_posts")
                        or BACKFILL_MAX_POSTS)
        key = self._bf_queue[0]
        tid, aid = key
        page = self._bf_page.get(key)
        if not page or page < 1:
            self._bf_queue.pop(0)
            return []
        with self.lock:
            known = {p["pid"] for p in self.posts
                     if str(p.get("tid")) == str(tid)
                     and str(p.get("authorid")) == str(aid)}
        time.sleep(BACKFILL_PAGE_DELAY)
        got, stop, next_page = self.backfill_one_page(
            tid, aid, page, known, max_posts=max_posts,
            already=self._bf_got.get(key, 0))

        if not got and not stop:
            # 这一页没拿到（网络/风控）：页码不变，下轮重试
            self._bf_fail[key] = self._bf_fail.get(key, 0) + 1
            if self._bf_fail[key] >= BACKFILL_MAX_FAILS:
                print(f"[{datetime.now():%H:%M:%S}] 回溯 tid={tid} uid={aid} "
                      f"page={page} 连续失败 {self._bf_fail[key]} 次，跳过该目标。")
                self._bf_queue.pop(0)
            return []

        self._bf_fail[key] = 0
        self._bf_got[key] = self._bf_got.get(key, 0) + len(got)
        if stop or next_page < 1:
            self._bf_queue.pop(0)
            print(f"[{datetime.now():%H:%M:%S}] 回溯完成 tid={tid} uid={aid}："
                  f"共补回 {self._bf_got[key]} 条（还剩 {len(self._bf_queue)} 个目标）。")
        else:
            self._bf_page[key] = next_page
        if got:
            all_posts.extend(got)
            print(f"[{datetime.now():%H:%M:%S}] 回溯补抓 tid={tid} uid={aid} "
                  f"page={page}：补回 {len(got)} 条漏抓的发言。")
        return got

    def _is_pushable(self, p, abs_floor):
        """判断一条「缓存里没有的」发言到底该不该推企微。

        缓存里没有 ≠ 是新发言。存档一旦缺失（被清/损坏/换机器），回溯往前翻
        出来的全是几个月前的老帖，pid 同样不在缓存里，会被当成新发言逐条推送。
        所以推送前必须用时间判一道：

            **唯一判据 = 时间不早于绝对下限（启动时刻 - PUSH_MAX_AGE_HOURS，
            默认 7 天，页面「推送时间门槛」可配，0 = 不限）。**

        不再区分新/老组合：新加的作者/帖子当轮就该推，否则加完半天没反应。
        限制只有两个，且都在页面可配：
          1. 回溯最多往前翻 50 条（--backfill-posts / 页面「启动时回溯条数」）；
          2. 推送时间门槛 7 天（--push-max-age-hours / 页面「推送时间门槛」）。

        ⚠️ 刻意**不**用「必须比该作者已知最新一条更新」做判据：停机断档补回的
        发言恰恰早于已知最新一条（中间缺一段、更新的已经在库），那样会把真正
        的漏抓也拦掉。7 天绝对下限已经足够挡住陈年旧帖，且不会误伤断档。
        被拦下的发言照样入库，页面可查。
        """
        ts = p.get("ts") or 0
        if ts <= 0:
            print(f"[{datetime.now():%H:%M:%S}] 跳过推送 pid={p.get('pid')}："
                  f"发言时间解析失败，无法判断新旧（已入库）")
            return False
        return ts >= abs_floor    # 超出硬下限（默认 7 天）的老帖只入库不推送

    def check_once(self, do_push_startup=True, do_backfill=True):
        # 推送判新基准：**只看时间下限**，不再区分新/老组合。ts 解析不出的一律不推。
        max_age_h = int((self.config or {}).get("push_max_age_hours")
                        or PUSH_MAX_AGE_HOURS)
        abs_floor = (time.time() - max_age_h * 3600) if max_age_h > 0 else 0

        targets = self._resolve_ids()
        all_posts, errors = [], []
        tried = 0
        for t in targets:
            tid = t["tid"]
            for aid in t["authorids"]:
                tried += 1
                time.sleep(FETCH_COMBO_DELAY)   # 摊平请求速率，别一口气打过去
                try:
                    posts, real_page = self.fetch_author_posts(tid, aid)
                    all_posts.extend(posts)
                    if real_page:
                        self.last_pages[(str(tid), str(aid))] = real_page
                except NgaError as e:
                    errors.append(f"tid={tid},uid={aid}: {e}")
                except Exception as e:
                    errors.append(f"tid={tid},uid={aid}: 抓取失败 {e}")

        # 回溯补抓：启动后做**一次完整扫描**（不单开服务/线程、也不定期反复），
        # 分片执行：每轮只翻 1 页，翻不动的下轮重试，语义不变、但不会因密集
        # 翻页被风控打挂（详见 _backfill）。放在**第 2 轮**开始，避开第 1 轮刚打完十几个
        # 请求的风控窗口。必须已有历史缓存（seeded）才做——首次运行没有基准。
        backfilled_pids = set()
        if do_backfill:
            self.rounds += 1
            if self.seeded and self.rounds >= 2:
                for p in self._backfill(targets, all_posts):
                    backfilled_pids.add(p["pid"])

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
            # 「部分成功」也算成功：个别目标配错（如固定 403 的帖子）不该让状态
            # 一直显示「从未成功」，那样看不出真正的全局故障
            if not errors or len(errors) < tried:
                self.last_success = time.time()

            if not self.seeded:
                # 首次/配置变更后的静默播种：只记录已见，不推送历史
                self.seen_pids.update(p["pid"] for p in posts)
                self.seeded = True
                print(f"[{datetime.now():%H:%M:%S}] 已播种 {len(posts)} 条历史发言（不推送）。")
            else:
                if self.config.get("monitor_enabled") and new_posts:
                    pushed_n = 0
                    for p in sorted(new_posts, key=lambda x: x["ts"]):
                        # --no-backfill-push：补回的历史断档只入库、不推企微
                        if self.no_backfill_push and p["pid"] in backfilled_pids:
                            continue
                        # 只推「比该作者已知最新一条更新」的发言；回溯/存档缺失翻
                        # 出来的老帖在这里被拦下，只入库不推送（页面照样能看）
                        if not self._is_pushable(p, abs_floor):
                            continue
                        print(f"[{datetime.now():%H:%M:%S}] 新发言 uid={p['authorid']} pid={p['pid']} -> 推送")
                        push_post(p)
                        pushed_n += 1
                    self.seen_pids.update(p["pid"] for p in new_posts)
                    self.last_push = time.time()
                    silent = len(new_posts) - pushed_n
                    print(f"[{datetime.now():%H:%M:%S}] 本轮新增 {len(new_posts)} 条，"
                          f"已推送 {pushed_n} 条" + (f"（{silent} 条补回的静默入库）" if silent else "") + "。")

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

    def _backup_cache(self):
        """把当前存档复制为轮转备份 .bak1~.bakN（旧的往后挪，超出保留数丢弃）。

        只在距上次备份超过 CACHE_BACKUP_INTERVAL 时执行——每轮都复制一份 386KB
        文件没必要，半小时一份已足够在「写坏一份」时回滚。
        """
        now = time.time()
        if now - getattr(self, "_last_backup_ts", 0) < CACHE_BACKUP_INTERVAL:
            return
        self._last_backup_ts = now
        try:
            if not os.path.exists(POSTS_CACHE_PATH):
                return
            import shutil
            # 先依次把老的往后挪：bak2 -> bak3, bak1 -> bak2
            for i in range(CACHE_BACKUP_KEEP - 1, 0, -1):
                src = f"{POSTS_CACHE_PATH}.bak{i}"
                dst = f"{POSTS_CACHE_PATH}.bak{i + 1}"
                if os.path.exists(src):
                    shutil.copy2(src, dst)
            shutil.copy2(POSTS_CACHE_PATH, f"{POSTS_CACHE_PATH}.bak1")
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] [缓存] 轮转备份失败（不影响主流程）: {e}")

    def _alert(self, msg):
        """缓存异常告警：打印醒目提示，并尽量往企微推一条（失败不影响主流程）。"""
        print("\n" + "=" * 60)
        print(msg)
        print("=" * 60 + "\n")
        try:
            if WEBHOOK_URL:
                send_wecom_msg("⚠️ NGA 监控缓存异常\n\n" + msg)
        except Exception:
            pass

    def save_cache(self):
        """把累积的全部发言原子写入 nga_posts.json（落盘即页面读取的唯一数据源）。

        采用「写临时文件 + os.replace 原子替换」：页面每次刷新都直接读这份文件，
        若用普通覆盖写，读取方可能在写入中途拿到半截 JSON 而报错；原子替换
        保证页面任何时候读到的都是完整的一整份文件。

        在此之上还加了三重防丢（详见文件顶部 CACHE_BACKUP_* 注释）：
          1) 覆盖写之前先做轮转备份
          2) 内存条数远少于磁盘条数时熔断，宁可不写也不把多的数据覆盖成少的
          3) 熔断时把内存副本另存为 .suspect-<时间戳>，人工可比对抢救
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
                mem_count = len(posts)

            # ---- 熔断：内存数据明显少于磁盘现存数据时，拒绝覆盖 ----
            disk_count = self._disk_count()
            if disk_count and disk_count > 100 and mem_count < disk_count * CACHE_GUARD_RATIO:
                suspect = f"{POSTS_CACHE_PATH}.suspect-{datetime.now():%Y%m%d_%H%M%S}.json"
                try:
                    with open(suspect, "w", encoding="utf-8") as f:
                        f.write(text)
                except Exception:
                    pass
                self._alert(
                    f"缓存覆盖已阻止：磁盘存档 {disk_count} 条，但内存只有 {mem_count} 条。\n"
                    f"这种情况通常是启动时缓存读取失败导致从空库开始，若照常覆盖会丢失大量历史。\n"
                    f"内存副本已另存为：{suspect}\n"
                    f"原存档未改动。\n"
                    f"若确认要重置（放弃旧存档重新开始）：停止本进程 -> 删除 "
                    f"{os.path.basename(POSTS_CACHE_PATH)} -> 重新启动。"
                )
                return False

            self._backup_cache()
            tmp = POSTS_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, POSTS_CACHE_PATH)
            self._cache_count_cached = mem_count
            return True
        except Exception:
            pass
        return False

    def _disk_count(self):
        """磁盘存档里现有多少条发言。

        结果缓存在 self._cache_count_cached（只在首轮真正读一次文件），
        避免每轮都解析几百 KB 的 JSON。
        """
        cached = getattr(self, "_cache_count_cached", None)
        if cached is not None:
            return cached
        try:
            if not os.path.exists(POSTS_CACHE_PATH):
                self._cache_count_cached = 0
                return 0
            with open(POSTS_CACHE_PATH, "r", encoding="utf-8") as f:
                n = len((json.load(f) or {}).get("posts") or [])
            self._cache_count_cached = n
            return n
        except Exception:
            self._cache_count_cached = 0
            return 0

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
            elif path == "/api/resolve":
                # 页面配置区用：把 tid / 作者 UID 反查成标题与作者名（带缓存与节流）
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                tid = (qs.get("tid") or [""])[0].strip()
                uids = [u.strip() for u in
                        (qs.get("uids") or [""])[0].split(",") if u.strip()]
                if not tid:
                    self._send_json({"ok": False, "error": "缺少 tid"}, 400)
                    return
                self._send_json(monitor.resolve_names(tid, uids))
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
    parser.add_argument("--no-backfill-push", action="store_true",
                        help="回溯补回的历史断档发言只入库、不推企微（避免一次性补回几十条刷屏）")
    parser.add_argument("--backfill-posts", type=int, default=BACKFILL_MAX_POSTS,
                        help=f"启动时每个作者最多往前回溯多少**条**发言（默认 {BACKFILL_MAX_POSTS}）")
    parser.add_argument("--push-max-age-hours", type=int, default=PUSH_MAX_AGE_HOURS,
                        help=f"超过这个小时数的发言只入库不推送（默认 {PUSH_MAX_AGE_HOURS}，"
                             f"即 7 天；设 0 表示不限制）")
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
    if args.no_backfill_push:
        cfg["no_backfill_push"] = True
    # 回溯条数上限（条，不是页）；页面/命令行都能改，默认 BACKFILL_MAX_POSTS
    cfg["backfill_max_posts"] = max(0, int(args.backfill_posts or 0)) or BACKFILL_MAX_POSTS
    # 推送时间门槛（小时）：设 0 = 不限，抓到没见过的就推
    cfg["push_max_age_hours"] = max(0, int(args.push_max_age_hours or 0))
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
        # 调试模式：抓一次并打印，不启动服务、不推送、不做回溯补抓
        m = NgaMonitor(cfg)
        m.check_once(do_push_startup=False, do_backfill=False)
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
