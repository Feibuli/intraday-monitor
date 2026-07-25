#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股价监控系统 v2
- 导入 watchlist.csv（自选股列表，由牛股计算器页面导出）
- 日内压力/支撑突破时推送（状态变化才告警）
- 持续监控，达到目标时通过企业微信推送
"""

import sys
sys.stdout.reconfigure(line_buffering=True)  # 强制行缓冲，确保nohup后台运行时日志实时写入

import csv
import time
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# 仅使用 Python 标准库，无需 pip 安装任何第三方依赖：
#   - 行情/推送的 HTTP 请求用 urllib.request
#   - 定时检查用 time.sleep 循环（替代原 schedule 库）
import json
import urllib.request
import urllib.error


# ===================== 极简 .env 加载器（零依赖） =====================
def _load_dotenv(path=None):
    """若进程环境变量中尚无 WECHAT_WEBHOOK_URL，则从脚本同目录的 .env 读取并写入 os.environ。
    .env 已被 .gitignore 忽略，仅本地使用，密钥不会入库。"""
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


# ===================== 配置区域 =====================
# 企业微信机器人 webhook 地址：优先读环境变量，其次读 monitor/.env 的 WECHAT_WEBHOOK_URL，切勿硬编码密钥入库。
# 获取方式：企业微信群 → 添加群机器人 → 复制 Webhook 地址，写入 monitor/.env 的 WECHAT_WEBHOOK_URL。
WEBHOOK_URL = os.environ.get("WECHAT_WEBHOOK_URL", "")
CSV_FILE = "watchlist.csv"  # 自选股列表（由牛股计算器页面导出，放本 monitor/ 目录下）
CHECK_INTERVAL = 60  # 检查间隔（秒）

# 股票数据API
QUOTE_API = "https://qt.gtimg.cn/q={}"

# ===================== 企业微信推送 =====================
def send_wecom_msg(content: str) -> bool:
    """发送企业微信消息；未配置 WECHAT_WEBHOOK_URL 时跳过推送。"""
    if not WEBHOOK_URL:
        print("[推送] 未配置 WECHAT_WEBHOOK_URL，跳过企业微信推送（仅本地运行）。")
        return False
    data = {
        "msgtype": "markdown",
        "markdown": {"content": content}
    }
    try:
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        result = json.loads(body)
        return result.get("errcode") == 0
    except Exception:
        return False


def send_alert(stock_info: Dict, alert_type: str, price: float, price2: float = None, extra: Dict = None, change_pct: float = 0):
    """精简告警消息"""
    # 未勾选监控的股票不推送企微
    if not stock_info.get('monitor', True):
        return
    now = datetime.now().strftime("%H:%M:%S")
    cost = float(stock_info.get('cost', 0))

    # 涨跌幅显示格式：上涨为+X.XX%，下跌为-X.XX%
    change_str = f"{change_pct:+.2f}%"

    # 简洁格式：股票 | 告警 | 价格 | 压力/支撑 | 涨跌幅
    if "止损-6%" in alert_type:
        # 基于成本价6%止损
        loss_pct = (1 - price2 / cost) * 100 if cost > 0 else 0
        emoji = "🛑"
        msg = f"{emoji} {stock_info['name']} 止损-6% | 成本{cost:.2f}→现价{price2:.2f} | 亏{loss_pct:.1f}% | {change_str}"
    elif "破底止损" in alert_type:
        # 基于跌破波段底部3%
        low = float(stock_info.get('low', 0))
        loss_pct = (1 - price2 / low) * 100 if low > 0 else 0
        emoji = "🛑"
        msg = f"{emoji} {stock_info['name']} 破底止损-3% | 底部{low:.2f}→现价{price2:.2f} | 跌{loss_pct:.1f}% | {change_str}"
    elif "保本止盈" in alert_type:
        # 最高浮盈10%~20%，回撤50%利润，保底3%
        profit_pct = extra.get('profit_pct', 0) if extra else 0
        max_profit = extra.get('max_profit', 0) if extra else 0
        emoji = "🎯"
        msg = f"{emoji} {stock_info['name']} 保本止盈 | 最高{max_profit:.1f}%→现{profit_pct:.1f}% | 止盈{price:.2f} | {change_str}"
    elif "动态止盈" in alert_type:
        # 最高浮盈>20%，回撤30%利润
        profit_pct = extra.get('profit_pct', 0) if extra else 0
        max_profit = extra.get('max_profit', 0) if extra else 0
        emoji = "🎯"
        msg = f"{emoji} {stock_info['name']} 动态止盈 | 最高{max_profit:.1f}%→现{profit_pct:.1f}% | 止盈{price:.2f} | {change_str}"
    elif "突破压力" in alert_type:
        emoji = "📈"
        msg = f"{emoji} {stock_info['name']} 突破压力 {price:.3f}→{price2:.3f} | {change_str}"
    elif "跌破支撑" in alert_type:
        emoji = "📉"
        msg = f"{emoji} {stock_info['name']} 跌破支撑 {price:.3f}→{price2:.3f} | {change_str}"
    elif "反弹过支撑" in alert_type:
        emoji = "⚠️"
        msg = f"{emoji} {stock_info['name']} 反弹过支撑 {price:.3f}→{price2:.3f} | {change_str}"
    elif "回落破压力" in alert_type:
        emoji = "⚠️"
        msg = f"{emoji} {stock_info['name']} 回落破压力 {price:.3f}→{price2:.3f} | {change_str}"
    elif "突破新高" in alert_type:
        emoji = "🚀"
        msg = f"{emoji} {stock_info['name']} 突破波段新高 {price:.3f} | {change_str}"
    elif "时间证伪" in alert_type:
        emoji = "⏰"
        msg = f"{emoji} {stock_info['name']} 时间证伪13天 | 现价{price2:.2f} | 距新高{price:.3f} | {change_str}"
    elif "封涨停板" in alert_type:
        block_amount = extra.get('block_amount', 0) if extra else 0
        block_vol = extra.get('block_vol', 0) if extra else 0
        emoji = "🚀"
        msg = f"{emoji} {stock_info['name']} 封涨停板 | 涨停价{price:.2f}→封单{block_vol}手 | 封单额{block_amount:.1f}万 | {change_str}"
    elif "封跌停板" in alert_type:
        block_amount = extra.get('block_amount', 0) if extra else 0
        block_vol = extra.get('block_vol', 0) if extra else 0
        emoji = "🛑"
        msg = f"{emoji} {stock_info['name']} 封跌停板 | 跌停价{price:.2f}→封单{block_vol}手 | 封单额{block_amount:.1f}万 | {change_str}"
    elif "打开涨停板" in alert_type:
        emoji = "🔓"
        msg = f"{emoji} {stock_info['name']} 打开涨停板 | 涨停价{price:.2f}→现价{price2:.2f} | {change_str}"
    elif "打开跌停板" in alert_type:
        emoji = "🔓"
        msg = f"{emoji} {stock_info['name']} 打开跌停板 | 跌停价{price:.2f}→现价{price2:.2f} | {change_str}"
    elif "封单额变化" in alert_type:
        direction = extra.get('direction', '') if extra else ''
        block_amount = extra.get('block_amount', 0) if extra else 0
        emoji = "📊"
        msg = f"{emoji} {stock_info['name']} 封单额{direction}>30% | {price:.1f}万→{block_amount:.1f}万 | {change_str}"
    else:
        emoji = "⚠️"
        msg = f"{emoji} {stock_info['name']} {alert_type} {price:.3f} | 涨{change_str}"

    content = f"{msg} | {now}"
    send_wecom_msg(content)


def send_startup_notification(stocks: List[Dict]):
    """发送启动通知"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    monitored = [s for s in stocks if s.get('monitor', True)]
    stock_list = " ".join([f"{s['name']}({s['code']})" for s in monitored])
    total = len(stocks)
    monitored_count = len(monitored)
    content = f"📊 股价监控启动 | {now} | 监控{monitored_count}/{total}只 | {stock_list}"
    send_wecom_msg(content)


# ===================== 股票数据获取 =====================
def format_code(code: str) -> str:
    """格式化股票代码"""
    code = code.strip().lower()
    if code.startswith(('sh', 'sz', 'hk', 'bj')):
        return code
    if code.isdigit() and len(code) == 6:
        if code.startswith(('5', '6')):
            return f"sh{code}"
        elif code.startswith(('0', '1', '3')):
            return f"sz{code}"
    return code


def get_stock_price(code: str) -> Optional[Dict]:
    """获取股票实时价格"""
    try:
        url = QUOTE_API.format(code)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("gbk", errors="ignore")
        var_name = f"v_{code}"
        
        if var_name not in text:
            return None
        
        start = text.find(var_name) + len(var_name) + 2
        end = text.find('"', start)
        parts = text[start:end].split('~')
        
        if len(parts) < 35:
            return None
        
        return {
            'name': parts[1],
            'code': code,
            'now': float(parts[3]) if parts[3] else 0,
            'prev_close': float(parts[4]) if parts[4] else 0,
            'open': float(parts[5]) if parts[5] else 0,
            'change_pct': float(parts[32]) if parts[32] else 0,
            'high': float(parts[33]) if parts[33] else 0,
            'low': float(parts[34]) if parts[34] else 0,
            'avg': float(parts[51]) if len(parts) > 51 and parts[51] else 0,
            'buy1_vol': int(parts[10]) if len(parts) > 10 and parts[10] else 0,
            'sell1_vol': int(parts[20]) if len(parts) > 20 and parts[20] else 0,
            'limit_up': float(parts[47]) if len(parts) > 47 and parts[47] else 0,
            'limit_down': float(parts[48]) if len(parts) > 48 and parts[48] else 0,
        }
    except Exception:
        return None


# ===================== 策略计算 =====================
def calculate_intraday_levels(avg: float) -> Dict:
    """计算日内压力和支撑位"""
    K = 0.98848
    if avg > 0:
        return {
            'top_line': round(avg / K, 3),
            'bottom_line': round(avg * K, 3)
        }
    return {'top_line': 0, 'bottom_line': 0}


def calculate_fibonacci(high: float, low: float) -> Dict:
    """计算斐波那契回调位"""
    diff = high - low
    return {
        'f382': round(high - diff * 0.382, 3),
        'f618': round(high - diff * 0.618, 3),
        'f786': round(high - diff * 0.786, 3),
    }


# ===================== 状态判断 =====================
def get_price_zone(price: float, top: float, bottom: float) -> str:
    """获取价格所在区间：上涨中/盘整中/下跌中"""
    if top > 0 and price > top:
        return "上涨中"  # 价格 > 日内压力
    elif bottom > 0 and price <= bottom:
        return "下跌中"  # 价格 <= 日内支撑
    else:
        return "盘整中"  # 介于压力和支撑之间


def count_trading_days(start_ts: int, end_ts: int) -> int:
    """计算两个时间戳之间的交易日天数（跳过周末）"""
    if start_ts >= end_ts:
        return 0
    
    start = datetime.fromtimestamp(start_ts)
    end = datetime.fromtimestamp(end_ts)
    
    count = 0
    current = start
    while current <= end:
        # 周一到周五是交易日 (weekday: 0-4)
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    
    return max(0, count - 1)  # 减1是因为start当天不计入


# ===================== CSV导入 =====================
def load_strategy_csv(filepath: str) -> List[Dict]:
    """导入策略CSV文件"""
    stocks = []
    if not os.path.exists(filepath):
        return stocks
    
    encodings = ['utf-8-sig', 'utf-8', 'gbk', 'gb2312']
    for encoding in encodings:
        try:
            with open(filepath, 'r', encoding=encoding) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    code = format_code(row.get('代码', ''))
                    if not code:
                        continue
                    # 解析新高日期
                    new_high_date = row.get('新高日期', '')
                    new_high_ts = 0
                    if new_high_date:
                        try:
                            new_high_ts = int(datetime.strptime(new_high_date, "%Y-%m-%d").timestamp())
                        except:
                            pass
                    
                    stocks.append({
                        'code': code,
                        'name': row.get('名称', ''),
                        'high': float(row.get('阶段顶部', 0) or 0),
                        'low': float(row.get('阶段底部', 0) or 0),
                        'cost': float(row.get('成本价', 0) or 0),
                        'new_high_date': new_high_date,
                        'new_high_ts': new_high_ts,
                        'monitor': row.get('监控', '1').strip() != '0',
                    })
            return stocks
        except Exception:
            continue
    return stocks


# ===================== 监控核心 =====================
class StockMonitor:
    def __init__(self, csv_file: str):
        self.csv_file = csv_file
        self.stocks = []
        self.states = {}        # 记录每个股票的状态 {code: "上涨中"/"盘整中"/"下跌中"}
        self.triggered = set()  # 已触发止损/新高的股票（每日重置）
        self.high_dates = {}    # 记录每个股票的新高时间戳 {code: timestamp}
        self.max_profit = {}   # 记录每个股票的最高浮盈 {code: 浮盈比例}
        self.daily_prices = {}  # 记录每只股票的每日价格数据 {code: {'high': x, 'low': y, 'close': z, 'open': o, 'prev_close': p}}
        self.limit_states = {}     # 记录封板状态 {code: "limit_up"/"limit_down"/None}
        self.limit_baselines = {}  # 记录封单额基准（万元）{code: 万元}
    
    def load_stocks(self):
        self.stocks = load_strategy_csv(self.csv_file)
        return len(self.stocks) > 0
    
    def check_stock(self, stock: Dict):
        code = stock['code']
        price_data = get_stock_price(code)
        
        if not price_data or price_data['now'] <= 0:
            return
        
        now = price_data['now']
        avg = price_data.get('avg', 0)
        high = float(stock.get('high', 0))
        low = float(stock.get('low', 0))
        cost = float(stock.get('cost', 0))

        # 记录每日价格数据用于收盘总结（直接用接口返回的真实high/low/open，不用采样追踪）
        self.daily_prices[code] = {
            'name': stock['name'],
            'high': price_data.get('high', now),
            'low': price_data.get('low', now),
            'close': now,
            'open': price_data.get('open', now),
            'prev_close': price_data.get('prev_close', now),
        }

        print(f"[{datetime.now().strftime('%H:%M:%S')}] {stock['name']}: {now:.3f}")

        # ===================== 涨停/跌停状态机（优先于原有状态机） =====================
        # 14:57后进入尾盘竞价/收盘后阶段，API封单数据不可靠，封板判断仅限连续竞价时段
        now_dt = datetime.now()
        time_val = now_dt.hour * 100 + now_dt.minute
        if time_val < 1457:
            limit_up = price_data.get('limit_up', 0)
            limit_down = price_data.get('limit_down', 0)
            buy1_vol = price_data.get('buy1_vol', 0)
            sell1_vol = price_data.get('sell1_vol', 0)

            # 判断当前限价状态：触及涨停且买一有封单，或触及跌停且卖一有封单
            limit_state = None
            current_block_amount = 0
            if limit_up > 0 and now >= limit_up * 0.999 and buy1_vol > 0:
                limit_state = "limit_up"
                current_block_amount = round(buy1_vol * limit_up * 0.01, 2)  # 万元
            elif limit_down > 0 and now <= limit_down * 1.001 and sell1_vol > 0:
                limit_state = "limit_down"
                current_block_amount = round(sell1_vol * limit_down * 0.01, 2)  # 万元

            prev_limit_state = self.limit_states.get(code)

            if prev_limit_state != limit_state:
                # 限价状态发生了变化
                if prev_limit_state is None and limit_state == "limit_up":
                    self.limit_baselines[code] = current_block_amount
                    send_alert(stock, "封涨停板", limit_up, now, extra={
                        'block_amount': current_block_amount, 'block_vol': buy1_vol, 'limit_price': limit_up
                    }, change_pct=price_data.get('change_pct', 0))
                elif prev_limit_state is None and limit_state == "limit_down":
                    self.limit_baselines[code] = current_block_amount
                    send_alert(stock, "封跌停板", limit_down, now, extra={
                        'block_amount': current_block_amount, 'block_vol': sell1_vol, 'limit_price': limit_down
                    }, change_pct=price_data.get('change_pct', 0))
                elif prev_limit_state == "limit_up" and limit_state is None:
                    if code in self.limit_baselines:
                        del self.limit_baselines[code]
                    send_alert(stock, "打开涨停板", limit_up, now, change_pct=price_data.get('change_pct', 0))
                elif prev_limit_state == "limit_down" and limit_state is None:
                    if code in self.limit_baselines:
                        del self.limit_baselines[code]
                    send_alert(stock, "打开跌停板", limit_down, now, change_pct=price_data.get('change_pct', 0))

                self.limit_states[code] = limit_state
                print(f"  限价状态: {prev_limit_state} -> {limit_state}")
                return  # 状态变化后跳过原有状态机

            elif limit_state is not None and limit_state == prev_limit_state:
                # 持续封板状态 → 检查封单额变化>30%
                baseline = self.limit_baselines.get(code, 0)
                if baseline > 0:
                    change_ratio = abs(current_block_amount - baseline) / baseline
                    if change_ratio > 0.30:
                        direction = "增加" if current_block_amount > baseline else "减少"
                        old_baseline = baseline
                        self.limit_baselines[code] = current_block_amount
                        send_alert(stock, "封单额变化>30%", old_baseline, current_block_amount, extra={
                            'direction': direction, 'block_amount': current_block_amount, 'limit_price': limit_up or limit_down
                        }, change_pct=price_data.get('change_pct', 0))
                        print(f"  封单额变化>30%: {old_baseline}万 -> {current_block_amount}万 ({direction})")
                    else:
                        print(f"  封板中 封单额:{current_block_amount}万 变化:{change_ratio*100:.1f}%")
                else:
                    print(f"  封板中 封单额:{current_block_amount}万")
                # 封板状态下跳过原有状态机
                return

        # ===================== 原有状态机（仅在非封板时执行） =====================

        # 实时计算日内压力/支撑（跟随均价变化）
        intraday = calculate_intraday_levels(avg)
        top = intraday['top_line']
        bottom = intraday['bottom_line']

        # 获取当前区间与上次区间
        current_zone = get_price_zone(now, top, bottom)
        last_zone = self.states.get(code)

        # 纯状态机：状态变化即推送，天然不会连续发同一种告警
        if last_zone is not None and last_zone != current_zone:
            if last_zone == "盘整中" and current_zone == "上涨中":
                send_alert(stock, "突破压力", top, now, change_pct=price_data.get('change_pct', 0))
            elif last_zone == "盘整中" and current_zone == "下跌中":
                send_alert(stock, "跌破支撑", bottom, now, change_pct=price_data.get('change_pct', 0))
            elif last_zone == "下跌中" and current_zone == "盘整中":
                send_alert(stock, "反弹过支撑", bottom, now, change_pct=price_data.get('change_pct', 0))
            elif last_zone == "上涨中" and current_zone == "盘整中":
                send_alert(stock, "回落破压力", top, now, change_pct=price_data.get('change_pct', 0))

        # 更新状态
        self.states[code] = current_zone

        # 打印调试信息
        print(f"  日内支撑:{bottom:.3f} 压力:{top:.3f} | 状态:{current_zone}")
        
        # 止损检测：只在状态变化时触发，且每个股票只触发一次
        if cost > 0:
            stop_loss_key = f"{code}_stop_loss"
            # 亏损6%止损 - 只在状态变化或首次检测时触发
            if now <= cost * 0.94 and stop_loss_key not in self.triggered:
                send_alert(stock, "止损-6%", cost * 0.94, now, change_pct=price_data.get('change_pct', 0))
                self.triggered.add(stop_loss_key)
            # 破底止损 - 只触发一次
            elif low > 0 and now < low * 0.97:
                break_low_key = f"{code}_break_low"
                if break_low_key not in self.triggered:
                    send_alert(stock, "破底止损", low * 0.97, now, change_pct=price_data.get('change_pct', 0))
                    self.triggered.add(break_low_key)
        
        # 突破波段新高（仅触发一次）
        if high > 0 and now >= high:
            new_high_key = f"{code}_new_high"
            if new_high_key not in self.triggered:
                send_alert(stock, "突破新高", high, now, change_pct=price_data.get('change_pct', 0))
                self.triggered.add(new_high_key)
            # 更新新高时间戳
            self.high_dates[code] = int(datetime.now().timestamp())
        
        # 时间证伪检测：13个交易日不创新高（仅持仓且有成本价的股票）
        if cost > 0 and high > 0:
            time_falsify_key = f"{code}_time_falsify"
            # 获取上次新高日期或当前日期
            last_high_ts = self.high_dates.get(code, stock.get('new_high_ts', 0))
            if last_high_ts == 0:
                last_high_ts = int(datetime.now().timestamp())

            # 计算交易日天数（跳过周末）
            days = count_trading_days(last_high_ts, int(datetime.now().timestamp()))
            if days >= 13 and time_falsify_key not in self.triggered:
                send_alert(stock, f"时间证伪13天", high, now, change_pct=price_data.get('change_pct', 0))
                self.triggered.add(time_falsify_key)
        
        # ===================== 止盈检测 =====================
        if cost > 0:
            # 计算当前浮盈比例
            profit_pct = (now - cost) / cost * 100
            
            # 更新最高浮盈
            current_max = self.max_profit.get(code, 0)
            if profit_pct > current_max:
                self.max_profit[code] = profit_pct
            
            max_profit_pct = self.max_profit.get(code, 0)
            
            # 止盈检测
            profit_take_key = f"{code}_profit_take"
            
            # 1. 保本止盈：最高浮盈10%~20%时，允许回撤50%利润，极限保底3%
            if max_profit_pct >= 10 and max_profit_pct <= 20:
                # 止盈点 = 成本价 + (最高浮盈 × 50%利润)
                # 但不低于成本价 + 3%
                take_profit_price = cost * (1 + max_profit_pct * 0.5 / 100)
                min_price = cost * 1.03
                target_price = max(take_profit_price, min_price)

                if now <= target_price and profit_take_key not in self.triggered:
                    profit_pct = (now - cost) / cost * 100
                    send_alert(stock, "保本止盈", target_price, now, {'profit_pct': profit_pct, 'max_profit': max_profit_pct}, change_pct=price_data.get('change_pct', 0))
                    self.triggered.add(profit_take_key)

            # 2. 动态止盈：最高浮盈>20%时，允许从最高点回撤30%利润
            elif max_profit_pct > 20:
                # 止盈点 = 成本价 + (最高浮盈 × 70%利润)
                # 即从最高点回撤30%
                take_profit_price = cost * (1 + max_profit_pct * 0.7 / 100)

                if now <= take_profit_price and profit_take_key not in self.triggered:
                    profit_pct = (now - cost) / cost * 100
                    send_alert(stock, "动态止盈", take_profit_price, now, {'profit_pct': profit_pct, 'max_profit': max_profit_pct}, change_pct=price_data.get('change_pct', 0))
                    self.triggered.add(profit_take_key)

    def send_daily_summary(self):
        """发送每日收盘总结（纯文本格式）"""
        if not self.daily_prices:
            return

        # 只推送监控中的股票
        monitored_codes = {s['code'] for s in self.stocks if s.get('monitor', True)}
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f"📊 每日收盘总结 | {today}", ""]

        for code, dp in self.daily_prices.items():
            if code not in monitored_codes:
                continue
            name = dp['name']
            prev_close = dp['prev_close']
            open_price = dp['open']
            high = dp['high']
            low = dp['low']
            close = dp['close']

            # 计算涨跌幅
            open_change = (open_price - prev_close) / prev_close * 100 if prev_close > 0 else 0
            close_change = (close - prev_close) / prev_close * 100 if prev_close > 0 else 0
            high_change = (high - prev_close) / prev_close * 100 if prev_close > 0 else 0
            low_change = (low - prev_close) / prev_close * 100 if prev_close > 0 else 0

            # 判断趋势
            trend = "📈" if close_change >= 0 else "📉"
            amplitude = high_change - low_change  # 振幅

            # 简洁格式：3行 - 名称+收盘、开盘+振幅、最高+最低
            lines.append(f"{name}{trend} {close:.2f} ({close_change:+.2f}%)")
            lines.append(f"  开盘: {open_price:.2f} ({open_change:+.2f}%) | 振幅: {amplitude:.2f}%")
            lines.append(f"  最高: {high:.2f} ({high_change:+.2f}%) | 最低: {low:.2f} ({low_change:+.2f}%)")
            lines.append("")

        content = "\n".join(lines)
        send_wecom_msg(content)
        print(f"[{datetime.now()}] 收盘总结已发送")

        # 清空今日数据
        self.daily_prices.clear()

    def run_check(self):
        today = datetime.now().strftime("%Y-%m-%d")

        # 检查是否需要清除昨日触发记录（每天首次检查时）
        if hasattr(self, 'last_date') and self.last_date != today:
            print(f"[{datetime.now()}] 新交易日，清除触发记录和最高浮盈，重新加载策略表格")
            self.triggered.clear()
            self.max_profit.clear()
            self.states.clear()
            self.high_dates.clear()
            self.max_profit.clear()
            self.limit_states.clear()
            self.limit_baselines.clear()
            self.load_stocks()
        self.last_date = today
        
        monitored_count = sum(1 for s in self.stocks if s.get('monitor', True))
        print(f"\n{'='*40}")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 检查... [监控{monitored_count}/{len(self.stocks)}只]")
        
        for stock in self.stocks:
            self.check_stock(stock)
            time.sleep(0.5)
    
    def is_trading_time(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        time_val = now.hour * 100 + now.minute
        return (930 <= time_val <= 1135) or (1255 <= time_val <= 1505)
    
    def start(self):
        # 检查是否已有监控进程在运行
        pid_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'monitor.pid')
        if os.path.exists(pid_file):
            try:
                with open(pid_file, 'r') as f:
                    old_pid = int(f.read().strip())
                # 如果pid文件记录的就是当前进程自己（shell脚本提前写入的情况），直接跳过检查
                if old_pid == os.getpid():
                    pass
                else:
                    # 检查进程是否存活
                    import signal
                    os.kill(old_pid, 0)
                    print(f"[{datetime.now()}] 监控进程已在运行 (PID: {old_pid})")
                    return
            except (ProcessLookupError, ValueError, PermissionError, OSError):
                # 进程不存在或无权限，删除旧PID文件（删除失败也忽略，不影响启动）
                try:
                    os.remove(pid_file)
                except OSError:
                    pass
        
        # 写入当前PID
        with open(pid_file, 'w') as f:
            f.write(str(os.getpid()))
        
        print(f"[{datetime.now()}] 监控系统启动")
        
        if not self.load_stocks():
            print("未加载到股票策略")
            return
        
        send_startup_notification(self.stocks)
        if self.is_trading_time():
            self.run_check()

        def job():
            if self.is_trading_time():
                self.run_check()

        def summary_job():
            """收盘后发送总结"""
            now = datetime.now()
            today = now.date()
            # 15:01~15:03 发送收盘总结（扩大窗口防止30秒周期错开漏发），日期去重
            if now.hour == 15 and now.minute in (1, 2, 3) and now.weekday() < 5:
                if summary_sent_date[0] == today:
                    return  # 今天已发送过，跳过
                summary_sent_date[0] = today
                self.send_daily_summary()

        summary_sent_date = [None]  # 用列表存储，避免闭包问题
        auction_sent_date = [None]  # 用列表存储，避免闭包问题

        def heartbeat_job():
            """9:25 推送竞价情况"""
            now = datetime.now()
            today = now.date()
            # 9:25~9:27 推送竞价情况（竞价9:25结束，需在9:25之后触发），用日期标记防止重复发送
            if now.hour == 9 and now.minute in (25, 26, 27) and now.weekday() < 5:
                if auction_sent_date[0] == today:
                    return  # 今天已发送过，跳过
                print(f"[{now}] 开始推送竞价情况...")
                lines = ["📊 竞价情况 | " + now.strftime("%Y-%m-%d"), ""]

                for stock in self.stocks:
                    if not stock.get('monitor', True):
                        continue
                    code = format_code(stock['code'])
                    price_data = get_stock_price(code)
                    if not price_data:
                        print(f"[{now}] 获取 {stock['code']} 数据失败")
                        continue

                    name = price_data['name']
                    now_price = price_data.get('now', 0)
                    prev_close = price_data.get('prev_close', 0)

                    if prev_close > 0:
                        open_change = (now_price - prev_close) / prev_close * 100
                        trend = "📈" if open_change >= 0 else "📉"
                        lines.append(f"{name}{trend} 竞价: {now_price:.2f} ({open_change:+.2f}%)")

                content = "\n".join(lines)
                result = send_wecom_msg(content)
                auction_sent_date[0] = today
                print(f"[{now}] 竞价情况已推送，结果: {result}")

        print(f"运行中，每{CHECK_INTERVAL}秒检查... Ctrl+C停止")

        # 信号处理：退出时清理PID文件
        import atexit
        _pid_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'monitor.pid')
        def cleanup():
            if os.path.exists(_pid_file):
                try:
                    os.remove(_pid_file)
                except OSError:
                    pass
        atexit.register(cleanup)

        # 纯标准库定时循环：主检查按 CHECK_INTERVAL，总结/心跳每 30 秒一次
        last_summary = last_heartbeat = time.time()
        while True:
            try:
                job()
            except Exception:
                pass
            now = time.time()
            if now - last_summary >= 30:
                try:
                    summary_job()
                except Exception:
                    pass
                last_summary = now
            if now - last_heartbeat >= 30:
                try:
                    heartbeat_job()
                except Exception:
                    pass
                last_heartbeat = now
            time.sleep(CHECK_INTERVAL)


# ===================== 主程序 =====================
if __name__ == "__main__":
    print("="*50)
    print("股价监控系统 v2")
    print("="*50)
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(script_dir, CSV_FILE)

    # 小白友好：缺少 watchlist.csv 时，自动从示例模板生成一份，确保开箱即可运行
    if not os.path.exists(csv_path):
        example_path = os.path.join(script_dir, "watchlist.example.csv")
        if os.path.exists(example_path):
            import shutil
            shutil.copy(example_path, csv_path)
            print(f"[提示] 未找到 {CSV_FILE}，已自动用示例模板生成 {csv_path}")
            print(f"        如需自定义自选股，可编辑该文件，或在牛股计算器中导出后覆盖。")
        else:
            print(f"[警告] 未找到 {CSV_FILE}（应在 {script_dir} 下），且示例模板缺失。")
            print(f"         请先准备自选股列表（在牛股计算器中导出 watchlist.csv 放到 monitor/ 目录）。")

    monitor = StockMonitor(csv_path)
    
    try:
        monitor.start()
    except KeyboardInterrupt:
        print("\n监控已停止")
        send_wecom_msg("⏹️ 股价监控已停止")
