# -*- coding: utf-8 -*-
"""
stock_lib.py —— A股多维技术分析【统一数据层】

本文件是 stock-bottom-fishing 技能所有脚本的公共底座，把"抓数 + 算指标"收敛到一处，
上层脚本（analyze_stock.py / macd_path.py / macd_stats.py / holdings_check.py）只负责渲染。

===== 踩坑沉淀（改动前必读）=====
1) 【代理陷阱】本机 requests 会读取 macOS 系统代理设置，直连东财时表现为
   ProxyError / Connection aborted / RemoteDisconnected。因此模块导入时清除代理
   环境变量，且所有请求统一走 trust_env=False 的 Session。
2) 【东财日K参数陷阱】beg=YYYYMMDD&end=YYYYMMDD 的组合极易触发 RemoteDisconnected；
   必须使用 beg=0&end=20500101 再配 &lmt=N（1600 / 800 / 500 轮转）。
3) 【多源兜底】东财失败自动切腾讯前复权日线 web.ifzq.gtimg.cn（无换手率，
   故腾讯源下筹码分布不可用，会明确标注）。
4) 【筹码分布无服务端接口】东财前端是 JS 算法：用最近 210 根日K（不复权）+
   每日换手率做递归衰减，再统计获利比例/平均成本/成本区间。本库用纯 Python
   复刻该算法，免除 akshare 与 py_mini_racer 依赖。
5) 【获利比例口径】返回值为 0~1 小数（0.591 = 59.1%），不要当百分数直接打印。
6) 【akshare 不可靠】本环境下 akshare 内部自建 Session，无法关闭代理读取，
   经常失败。除极端兜底外不要依赖它。
7) 【指标口径】指标的中间量一律走"通达信原语"（t_ 前缀，见下方区块），
   等价于通达信/同花顺公式语义；不要用 pandas 简写重算，否则口径会漂。
   原语移植自 MyTT (https://github.com/mpquant/MyTT, GPL-3.0)，只取原语不取指标。

依赖：requests, pandas, numpy（无需 akshare）
"""

import os
import sys
import time
import json
import datetime

# ------------------------------------------------------------------ 环境预处理
# 必须在 import requests 之前清除代理相关环境变量
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
           'http_proxy', 'https_proxy', 'all_proxy'):
    os.environ.pop(_k, None)

import requests      # noqa: E402
import numpy as np   # noqa: E402
import pandas as pd  # noqa: E402

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36')
HEADERS = {'User-Agent': UA, 'Referer': 'https://quote.eastmoney.com/'}

_S = requests.Session()
_S.trust_env = False          # 关键：忽略系统代理

EM_PUSH = ['push2his.eastmoney.com', 'push2.eastmoney.com', 'push2delay.eastmoney.com']
# 注意：push2delay 只服务实时类接口（ulist/clist 等），对历史数据接口恒返回
# data:null（kline）或仅 1 根（fflow），因此 K 线不用它，免得白跑一轮还污染判定。
EM_KL = ['push2his.eastmoney.com', 'push2.eastmoney.com']
UT = 'b2884a393a59ad64002292a3e90d46a5'

# 实时快照字段
RT_FIELDS = ('f12,f14,f2,f3,f4,f5,f6,f8,f10,f9,f23,f20,f21,'
             'f62,f184,f164,f174,f15,f16,f17,f18,f13,f11,f19,f22,f115,f114')

# 本地缓存：跨脚本进程共享，用于抗东财偶发限流（连续跑多个脚本时第二个起直接命中）
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache')
CACHE_TTL = 600          # 秒：视为"新鲜"的时长
CACHE_TTL_WEAK = 60      # 秒：降级源（腾讯，无换手率）的新鲜期，短以免长期占位挡住东财


def _is_weak_src(src):
    """降级数据源判定：腾讯源没有换手率，筹码等依赖换手率的计算用它不可靠"""
    return '腾讯' in (src or '')


# 跨进程限速 + 冷却：实测东财 K 线接口在短窗口内只放行约 4 次，第 5 次起被拒，
# 而间隔 15 秒左右会自行恢复。因此做两件事：
#   1) 温和限速：把"上次请求时间"落盘，多个脚本、多次运行共享同一节奏；
#   2) 失败冷却：一旦整轮都被拒，就把"解禁时间"落盘，后续请求先等冷却结束，
#      而不是继续加码重试（越打越久、还会连累腾讯源）。
MIN_INTERVAL = 3.0        # 秒：两次东财请求的最小间隔（历史类接口，受配额约束）
LIGHT_INTERVAL = 1.0      # 秒：轻量行情类接口（实时快照/指数/代码检索）的最小间隔
COOLDOWN_SEC = 20.0       # 秒：判定被限流后的冷却时长（起始值，连续失败会升级）
CD_MAX = 120.0            # 秒：冷却升级上限
CD_IDLE_RESET = 180.0     # 秒：距上次登记冷却超过此时长，失败计数归零（视为新一轮探测）
THROTTLE_FILE = os.path.join(CACHE_DIR, '_lastreq')
COOLDOWN_FILE = os.path.join(CACHE_DIR, '_cooldown')
CD_COUNT_FILE = os.path.join(CACHE_DIR, '_cdcnt')


def _cd_state():
    """读"连续失败次数 + 上次登记时刻"。兼容旧格式（文件里只有一行计数）。"""
    try:
        if os.path.exists(CD_COUNT_FILE):
            with open(CD_COUNT_FILE, 'r') as f:
                parts = (f.read() or '').split()
            n = int(float(parts[0])) if parts else 0
            t = float(parts[1]) if len(parts) > 1 else 0.0
            return n, t
    except Exception:
        pass
    return 0, 0.0


def _set_cooldown(sec=COOLDOWN_SEC, escalate=True):
    """
    登记冷却。escalate=True 时按"连续失败次数"指数升级：20→40→80→120s（封顶 CD_MAX）。

    为什么要升级：东财那次"所有 host 连接层被拒"不是几秒就好的小限流，实测会持续
    数十秒到数分钟。固定 20s 会让同一轮里的每个变体（日线/周线/筹码/实时）各自重新
    撞一次墙，一轮脚本白等一两分钟。升级后同一轮里只有第一次付出代价。

    为什么还要"闲置归零"：升级本身有反作用 —— 一个脚本里 realtime / kline / chips
    会各登记一次，几次就顶到 CD_MAX=120s。若不做闲置归零，用户 30 秒后重跑脚本时
    仍在冷却期内、又直接短路掉东财，于是"永远停在降级态"，而一次干净成功本来就能
    自愈。所以距上次登记超过 CD_IDLE_RESET 就当作全新探测，从 20s 重新开始。
    任何一次请求干净成功都会清空冷却（见 _reset_cooldown）。
    """
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        now = time.time()
        n0, t0 = _cd_state()
        n = (n0 + 1) if (escalate and (now - t0) <= CD_IDLE_RESET) else 1
        eff = min(CD_MAX, sec * (2 ** (n - 1)))
        with open(CD_COUNT_FILE, 'w') as f:
            f.write('%d %.3f' % (n, now))
        with open(COOLDOWN_FILE, 'w') as f:
            f.write('%.3f' % (now + eff))
    except Exception:
        pass


def _reset_cooldown():
    """
    请求干净成功 → 源已恢复，清空冷却与失败计数。

    必须连 COOLDOWN_FILE 一起清：只清计数的话，虽然计数归零、但旧的解禁时刻仍在，
    后续调用会一直短路到降级源直到冷却自然到期（升级到 120s 时就是"卡在降级态"）。
    既然刚有一次请求成功，就说明此刻打得通，剩下的节奏交给 MIN_INTERVAL 限速即可。
    """
    for p in (CD_COUNT_FILE, COOLDOWN_FILE):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _cooldown_left():
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE, 'r') as f:
                return max(0.0, float(f.read().strip() or 0) - time.time())
    except Exception:
        pass
    return 0.0


# ---- 主机级熔断：把"这台 host 刚才在连接层被拒"这件事记下来，短时间内不再撞它 ----
# 为什么需要它（实测冷启动 45s 的真实构成）：东财这些 push2* 域名是"一台一台"地死的。
# 常见形态是 push2his 与 push2 双双 RemoteDisconnected，而只有 push2delay 还活着
# （push2delay 对历史类接口会回空数据，所以它救不了日K/资金流，但能接实时类请求）。
# 于是每一次逻辑请求都要按 MIN_INTERVAL=3s 逐个去撞两台死主机，白等 6s；一份报告里
# 有十几次逻辑请求，光撞墙就烧掉约 40s，而这些秒数全部是纯粹的浪费——死掉的主机在
# 下一分钟里仍然是死的。
# 因此记录 {host: 解禁时刻}，熔断窗口内直接跳过该 host（不发请求、也不占 3s 限速）。
# 与全局冷却的区别：全局冷却回答"东财整体是不是病了"，用于让上层整体降级；主机熔断
# 回答"这一台还能不能打"，用于在同一次轮转里把秒数省下来。两者互补，都要有。
HOST_DOWN_SEC = 30.0                            # 秒：单台 host 在连接层被拒后的熔断时长
HOSTDOWN_FILE = os.path.join(CACHE_DIR, '_hostdown.json')


def _hostdown_load():
    """读主机熔断表；顺手丢弃已过期的条目。"""
    try:
        with open(HOSTDOWN_FILE, 'r') as f:
            d = json.load(f)
        now = time.time()
        return {k: float(v) for k, v in (d or {}).items() if float(v) > now}
    except Exception:
        return {}


def _hostdown_save(d):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(HOSTDOWN_FILE, 'w') as f:
            json.dump(d, f)
    except Exception:
        pass


def _clear_host_down(d, h):
    """该 host 这次干净响应了 → 撤销熔断（写回由调用方负责）。"""
    if h in d:
        d.pop(h, None)


def _throttle(min_interval=None, max_wait=None):
    """
    限速闸门。

    max_wait: 冷却期允许等待的上限（秒）。默认 None 表示最多等 COOLDOWN_SEC。
        由 _get 传入"剩余预算"，保证单次调用不会在冷却里空耗几分钟。
    """
    try:
        cd = _cooldown_left()
        if cd > 0:
            cap = COOLDOWN_SEC if max_wait is None else max(0.0, max_wait)
            if cap > 0:
                time.sleep(min(cd, cap))           # 处于冷却期：先等，别再打
        os.makedirs(CACHE_DIR, exist_ok=True)
        last = 0.0
        if os.path.exists(THROTTLE_FILE):
            try:
                with open(THROTTLE_FILE, 'r') as f:
                    last = float(f.read().strip() or 0)
            except Exception:
                last = 0.0
        gap = time.time() - last
        mi = MIN_INTERVAL if min_interval is None else min_interval
        if gap < mi:
            time.sleep(mi - gap)
        with open(THROTTLE_FILE, 'w') as f:
            f.write('%.3f' % time.time())
    except Exception:
        pass


# ------------------------------------------------------------------ 通用工具
def norm_code(code):
    """归一化为 6 位纯数字代码"""
    c = str(code).strip().lower()
    for p in ('sh', 'sz', 'bj'):
        c = c.replace(p, '')
    c = c.replace('.', '').strip()
    return c


def market_prefix(code6):
    """返回 sh / sz / bj"""
    if code6.startswith(('15', '16', '18')):
        return 'sz'
    if code6.startswith(('51', '56', '58', '50')):
        return 'sh'
    if code6.startswith(('6', '9')):
        return 'sh'
    if code6.startswith(('0', '2', '3')):
        return 'sz'
    if code6.startswith(('4', '8')):
        return 'bj'
    return 'sh'


def is_etf_code(code6):
    return code6.startswith(('15', '16', '18', '50', '51', '52', '56', '58'))


def secid(code6):
    """东财 secid：1.=沪市, 0.=深市/北交"""
    return ('1.' if market_prefix(code6) == 'sh' else '0.') + code6


def n2f(v):
    """安全转 float，'-' / '' / None 返回 None"""
    try:
        if v in ('-', '', None):
            return None
        return float(v)
    except Exception:
        return None


def retry(fn, n=3, wait=1.2):
    last = None
    for _ in range(n):
        try:
            r = fn()
            if r is not None:
                return r
        except Exception as e:
            last = e
        time.sleep(wait)
    return None


def _rows_ok(min_rows, key='klines'):
    """生成一个 _get 用的校验函数：必须能解析出 data[key] 且行数 >= min_rows。

    用途：push2delay 这类"延迟镜像"对历史数据接口会返回 HTTP 200 且 body 以 '{'
    开头，但 data 为空（或只有 1 行）。没有校验就会被当成成功，导致上层把
    「明明没拿到数据」误判为「拿到了」，还会挡住后续 host 的正确响应。
    """
    def _f(t):
        try:
            kl = (json.loads(t).get('data') or {}).get(key) or []
            return len(kl) >= min_rows
        except Exception:
            return False
    return _f


def _get(path, params, hosts=None, enc='utf-8', tries=3, ok=None, budget=None,
         wait_cd=True, min_interval=None):
    """
    带 host 轮转 + 全局限速 + 限流冷却 + 主机熔断的东财请求。

    ok: 可选校验函数 body_text -> bool。不通过则视为失败，继续轮转下一个 host。
        历史数据类接口务必传 _rows_ok(...)，否则会用「空响应当结果」。
    budget: 单次调用的总等待预算（秒）。冷却期与重试叠加起来很容易涨到分钟级，
        设了预算后超时就立刻返回 None 交给上层降级（缓存/备用源），不再空耗。
    wait_cd: 冷却期内是否还等冷却结束。处于冷却期又必须硬试一次时（例如筹码需要
        东财独有的换手率）传 False——只发一次请求、不等，试完就走。
    min_interval: 本次请求的最小间隔（秒）。默认 MIN_INTERVAL=3s，那是按"K线接口在
        短窗口内约 4 次配额"定的；实时快照/指数/代码检索这类轻量接口不背这个约束，
        传 LIGHT_INTERVAL 即可，否则每份报告会白白多等十秒。

    返回通过校验的文本；全部失败或预算耗尽返回 None。
    """
    hosts = hosts or EM_PUSH
    deadline = (time.time() + budget) if budget is not None else None
    down = _hostdown_load()
    for i in range(tries):
        tried = 0
        skipped = 0
        conn_err = 0
        for h in hosts:
            if down.get(h, 0.0) > time.time():
                # 这台 host 刚在连接层被拒（熔断中）。再撞它没有任何信息增益，却要付出
                # 一次 MIN_INTERVAL 限速 + 一次连接超时。直接跳过。
                skipped += 1
                continue
            rem = None if deadline is None else deadline - time.time()
            if rem is not None and rem <= 0:
                return None                     # 预算耗尽：等待已无意义
            cd = _cooldown_left()
            if wait_cd and cd > 0 and rem is not None and cd > rem:
                # 本次预算不够熬过冷却：等下去只会把预算耗光、结果仍是失败。
                # 实测冷启动最耗时的一幕就在这里——每次失败登记 20s 冷却，紧接着
                # 下一次重试把剩下十几秒预算全睡在冷却里，醒来依然是 ConnectionError。
                # 直接返回 None，让上层去用缓存/备用源，省下这段纯浪费。
                return None
            # 全局限速/冷却：等待计入本次预算；冷却期内且明确不等冷却时用 0 封顶
            _throttle(min_interval=min_interval, max_wait=(rem if wait_cd else 0.0))
            tried += 1
            try:
                r = _S.get('https://%s%s' % (h, path), params=params,
                           headers=HEADERS, timeout=12)
                t = r.content.decode(enc, 'ignore')
                if t.startswith('{') and (ok is None or ok(t)):
                    _clear_host_down(down, h)
                    if conn_err == 0 and skipped == 0:
                        # 干净的成功（本轮没被拒的 host、也没跳过熔断中的 host）才算源已恢复。
                        # 若本轮跳过了熔断 host，只说明"活着的那台还能用"，不能推断死掉的那台
                        # 也好了，因此不动全局冷却。
                        _reset_cooldown()
                    _hostdown_save(down)
                    return t
            except Exception:
                conn_err += 1
                down[h] = time.time() + HOST_DOWN_SEC
                _hostdown_save(down)
        if tried == 0:
            # 所有候选 host 都在熔断中 —— 本轮无一台可打，直接交给上层降级。
            return None
        # 只有"所有真正试过的 host 都在连接层被拒"才说明大概率被限流，值得等冷却再试；
        # 若只是 body 不合格（接口本身给的就是残数据），等待毫无意义，直接放弃。
        if conn_err >= tried:
            _set_cooldown(COOLDOWN_SEC)
        else:
            break
    return None


# ------------------------------------------------------------------ 本地缓存
def _cache_path(key):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except Exception:
        return None
    return os.path.join(CACHE_DIR, key + '.json')


def _json_default(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return str(o)


def cache_get(key, ttl=None):
    """ttl=None 表示不看新鲜度（取到就用）；返回 dict 或 None"""
    p = _cache_path(key)
    if not p or not os.path.exists(p):
        return None
    try:
        if ttl is not None and (time.time() - os.path.getmtime(p)) > ttl:
            return None
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def cache_put(key, obj):
    p = _cache_path(key)
    if not p:
        return
    try:
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(obj, f, default=_json_default)
    except Exception:
        pass


def _df_dump(df, src):
    """DataFrame -> 可 JSON 化的 {'src','rows'}"""
    d = df.copy()
    if 'date' in d.columns:
        d['date'] = pd.to_datetime(d['date']).dt.strftime('%Y-%m-%d')
    d = d.where(pd.notnull(d), None)
    return {'src': src, 'rows': d.to_dict('records')}


def _df_load(obj):
    df = pd.DataFrame(obj['rows'])
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
    return df, obj['src']


# ------------------------------------------------------------------ 名称解析
_NAME_CACHE = None


def resolve_symbol(query):
    """名称或代码 -> (code6, err)。名称解析走东财搜索接口，不依赖 akshare。"""
    global _NAME_CACHE
    q = str(query).strip()
    c = norm_code(q)
    if c.isdigit() and len(c) == 6:
        return c, None
    txt = _get('/api/suggest/get', {
        'input': q, 'type': '14', 'token': 'D43BF722C8E33BDC906FB84D85E326E8',
        'count': '10'}, hosts=['searchapi.eastmoney.com',
                               'search-codetable.eastmoney.com',
                               'searchadapter.eastmoney.com'],
        tries=2, budget=10, wait_cd=False,
        min_interval=LIGHT_INTERVAL)   # 代码检索是轻量接口，不背 K 线的 3s 配额节奏
    if txt:
        try:
            data = json.loads(txt).get('QuotationCodeTable', {}).get('Data') or []
            hit = [d for d in data if d.get('Code') and d.get('Name')]
            exact = [d for d in hit if d.get('Name') == q]
            pick = exact or hit
            if pick:
                return pick[0]['Code'], None
        except Exception:
            pass
    return None, '名称"%s"解析失败，请直接使用 6 位代码' % q


# ------------------------------------------------------------------ 日K（多源 + 缓存）
def kline(code6, klt=101, fqt=1, lmt_cands=(1600, 800), weak_ok=True):
    """
    日K线。返回 (DataFrame, 数据源字符串)
    列: date, open, close, high, low, volume, amount, pct, turnover

    三级策略：
      1) 新鲜缓存（TTL 内）直接命中 —— 连续跑多个脚本时第二、三个不再打网络
      2) 东财（盘中含当日，带换手率）→ 腾讯（兜底，无换手率）
      3) 全部源失败时回退到「上次成功的缓存」，并在数据源里标注滞后

    weak_ok=False 时不接受降级源（腾讯）——用于必须要有换手率的场景（筹码分布）：
    只认东财结果，宁可返回失败也不拿降级数据糊弄。
    """
    code6 = norm_code(code6)
    ck = 'kline_%s_%d_%d' % (code6, klt, fqt)

    hit = cache_get(ck, ttl=CACHE_TTL)
    if hit is None:
        # 降级源只认很短的新鲜期，避免它长期占位、挡住后续拿到东财的完整数据
        w = cache_get(ck, ttl=CACHE_TTL_WEAK)
        if w and _is_weak_src(w.get('src')):
            hit = w
    if hit and _is_weak_src(hit.get('src')) and not weak_ok:
        hit = None
    if hit:
        df, src = _df_load(hit)
        return df, src + ' [缓存]'

    cd = _cooldown_left()
    if cd > 0:
        # 冷却期意味着"上一轮所有 host 都在连接层被拒"——此刻再打东财纯属白等。
        # 允许降级时直接跳过东财；不允许降级(筹码要换手率)时也把候选窗口砍到 1 个，
        # 免得 210/300/800 三个候选各熬一遍预算。
        if weak_ok:
            lmt_cands = ()
        else:
            lmt_cands = lmt_cands[:1]
    b0, b1 = (6, 3) if cd > 0 else (15, 6)   # 冷却期内把预算压到最低，尽快失败退出

    sec = secid(code6)
    base = ('/api/qt/stock/kline/get?fields1=f1,f2,f3,f4,f5,f6'
            '&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'
            '&ut=7eea3edcaed734bea9cbfc24409ed989'
            '&klt=%d&fqt=%d&secid=%s&beg=0&end=20500101' % (klt, fqt, sec))
    for idx, nbar in enumerate(lmt_cands):
        # 首个候选给足预算（可能要先熬过一次冷却），后续候选预算收紧，尽快落到下一级兜底源
        txt = _get(base + '&lmt=%d' % nbar, None, hosts=EM_KL,
                   tries=2 if (idx == 0 and cd <= 0) else 1, ok=_rows_ok(2),
                   budget=b0 if idx == 0 else b1, wait_cd=(cd <= 0))
        if not txt:
            continue
        try:
            kl = (json.loads(txt).get('data') or {}).get('klines') or []
        except Exception:
            kl = []
        if not kl:
            continue
        rows = []
        for x in kl:
            p = x.split(',')
            rows.append({
                'date': p[0], 'open': float(p[1]), 'close': float(p[2]),
                'high': float(p[3]), 'low': float(p[4]), 'volume': float(p[5]),
                'amount': n2f(p[6]), 'pct': n2f(p[8]), 'turnover': n2f(p[10]),
            })
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date').reset_index(drop=True)
        src = '东财前复权(fqt=%d)' % fqt
        cache_put(ck, _df_dump(df, src))
        return df, src

    # ---------------- 兜底：腾讯前复权日线（仅当允许降级） ----------------
    if weak_ok:
        pfx = market_prefix(code6)
        for mkt in [pfx, 'sh' if pfx == 'sz' else 'sz']:
            for nbar in (1600, 800):
                for _ in range(2):
                    try:
                        u = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
                             '?param=%s%s,day,,,%d,qfq' % (mkt, code6, nbar))
                        r = _S.get(u, headers={'User-Agent': UA}, timeout=20)
                        j = r.json()
                        d = (j.get('data') or {}).get(mkt + code6) or {}
                        kl = d.get('qfqday') or d.get('day') or []
                        if kl:
                            rows, prev = [], None
                            for x in kl:
                                cl = float(x[2])
                                rows.append({
                                    'date': x[0], 'open': float(x[1]), 'close': cl,
                                    'high': float(x[3]), 'low': float(x[4]),
                                    'volume': float(x[5]), 'amount': None,
                                    'pct': 0.0 if prev is None else (cl / prev - 1) * 100,
                                    'turnover': None})
                                prev = cl
                            df = pd.DataFrame(rows)
                            df['date'] = pd.to_datetime(df['date'])
                            df = df.sort_values('date').reset_index(drop=True)
                            src = '腾讯前复权(无换手率→筹码不可算)'
                            cache_put(ck, _df_dump(df, src))
                            return df, src
                    except Exception:
                        pass
                    time.sleep(1.2)

    # ---------------- 最后兜底：上次成功的缓存（可能滞后） ----------------
    stale = cache_get(ck)
    if stale and (weak_ok or not _is_weak_src(stale.get('src'))):
        df, src = _df_load(stale)
        return df, src + ' [过期缓存,可能滞后]'
    raise RuntimeError('日K所有数据源均失败')


# ------------------------------------------------------------------ 实时快照
def realtime(codes):
    """批量实时快照。codes 可为单个代码或列表。返回 {code: dict}"""
    if isinstance(codes, str):
        codes = [codes]
    codes = [norm_code(c) for c in codes]
    txt = _get('/api/qt/ulist.np/get', {
        'fltt': '2', 'invt': '2',
        'secids': ','.join(secid(c) for c in codes),
        'fields': RT_FIELDS, 'ut': UT}, hosts=EM_PUSH, tries=3,
        budget=12, wait_cd=False,      # 实时快照：冷却期也值得硬试一次，但不等冷却
        min_interval=LIGHT_INTERVAL)   # 行情快照类，按 LIGHT_INTERVAL 走（不占历史配额）
    out = {}
    if not txt:
        return out
    for it in ((json.loads(txt).get('data') or {}).get('diff') or []):
        c = it.get('f12')
        out[c] = {
            'code': c, 'name': it.get('f14'),
            'price': n2f(it.get('f2')), 'pct': n2f(it.get('f3')), 'chg': n2f(it.get('f4')),
            'volume': n2f(it.get('f5')), 'amount': n2f(it.get('f6')),
            'turnover': n2f(it.get('f8')), 'vol_ratio': n2f(it.get('f10')),
            'pe': n2f(it.get('f9')), 'pb': n2f(it.get('f23')),
            'mkt_cap': n2f(it.get('f20')), 'float_cap': n2f(it.get('f21')),
            'high': n2f(it.get('f15')), 'low': n2f(it.get('f16')),
            'open': n2f(it.get('f17')), 'pre_close': n2f(it.get('f18')),
            'main_net': n2f(it.get('f62')), 'main_ratio': n2f(it.get('f184')),
            'main_5d': n2f(it.get('f164')), 'main_10d': n2f(it.get('f174')),
        }
    return out


# ------------------------------------------------------------------ 逐日资金流
def fund_flow(code6, days=10):
    """
    逐日主力资金。返回 DataFrame(date, main, small, mid, large, xlarge, main_ratio)，单位：元

    东财这个接口是"逐日"类数据里最不稳的一个，且实测已恶化到主机级不可达：
      - push2his / push2（即 EM_KL 两台历史主机）DNS 只解析出单 IP，连续 40 次 0 成功；
        横扫 1./2./7./82. 前缀变体亦全部 ConnectionError。不是 path 问题，是主机问题。
      - push2delay（延迟镜像）稳定可达，但 kline/get 与 daykline/get 都只回 1 行，
        且只有"最新交易日"那一根。
    因此这里按优先级做四件事：
      1) 落盘缓存（全量存、读取时再截尾）——连续跑脚本不再重复打这个不稳接口；
      2) _rows_ok(2) 校验——拒绝"HTTP 200 但只有 1 根"的残响应，让它继续轮转；
      3) tries=3 + budget=18s——给它自愈机会，但绝不为了它把整份报告拖到分钟级；
      4) push2delay 兜底——历史拿不到时，用延迟镜像把"最新一根"补到缓存序列尾部，
         避免 10 日表永远停在昨天（attrs['patched']）；连缓存都没有时只回这一根
         （attrs['only_today']）。口径仍是东财，不掺第三方源。
    仍拿不到则回退上次成功的缓存（attrs['stale']），最后返回 None。

    注意：兜底合出来的结果刻意不写回缓存——盘中拿到的"最新一根"可能是不完整值，
    固化进缓存会让后续 10 分钟内的所有报告都显示一个偏小的当日主力净额。
    """
    code6 = norm_code(code6)
    ck = 'fflow_%s' % code6
    params = {
        'lmt': '0', 'klt': '101', 'secid': secid(code6),
        'fields1': 'f1,f2,f3,f7',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65',
        'ut': UT,
    }

    def _raw_cache(ttl=None):
        o = cache_get(ck, ttl=ttl)
        if o and o.get('rows'):
            return _df_load(o)[0]
        return None

    def _parse(txt):
        kl = (json.loads(txt).get('data') or {}).get('klines') or []
        rows = []
        for x in kl:
            p = x.split(',')
            # daykline/get 回 15 字段（p[6] 即 main_ratio），kline/get 只回 6 字段
            rows.append({'date': p[0], 'main': n2f(p[1]), 'small': n2f(p[2]),
                         'mid': n2f(p[3]), 'large': n2f(p[4]), 'xlarge': n2f(p[5]),
                         'main_ratio': n2f(p[6]) if len(p) > 6 else float('nan')})
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['date'])
        return df

    hit = _raw_cache(CACHE_TTL)
    if hit is not None:
        return hit.tail(days).reset_index(drop=True)

    base = _raw_cache()             # 过期但可用的全量历史：二级兜底要拿它来"补齐尾部"

    # ---- 一级：东财历史主机（全量逐日）
    if _cooldown_left() <= 0:       # 冷却期内不再打这个本身就不稳的接口，直接走二级兜底
        txt = _get('/api/qt/stock/fflow/daykline/get', params,
                   hosts=EM_KL, tries=3, ok=_rows_ok(2), budget=18)
        # 注意：这里刻意不传 min_interval=LIGHT_INTERVAL。它和 kline 共用 EM_KL 这组
        # 历史类主机（同一份配额），按 3s 走才是对的；把它当"轻量接口"提速，换来的是
        # 更快撞上限流、然后整份报告一起进冷却——省下的一两秒不值这个风险。
        full = _parse(txt) if txt else None
        if full is not None:
            cache_put(ck, _df_dump(full, '东财'))       # 存全量，读取时再截尾
            return full.tail(days).reset_index(drop=True)

    # ---- 二级：延迟镜像补"最新一根"（只有 1 行，只活在内存里，不写回缓存）
    dly = _get('/api/qt/stock/fflow/daykline/get', params,
               hosts=['push2delay.eastmoney.com'], tries=2, ok=_rows_ok(1), budget=10,
               wait_cd=False, min_interval=LIGHT_INTERVAL)
    last = _parse(dly) if dly else None
    if last is not None:
        last = last.tail(1)
        if base is not None and base['date'].max() < last['date'].iloc[0]:
            merged = pd.concat([base, last], ignore_index=True)
            merged = merged.tail(days).reset_index(drop=True)
            merged.attrs['patched'] = last['date'].iloc[0].strftime('%m-%d')
            return merged
        if base is not None:
            # 缓存里已有（或不早于）这一根 → 等于没拿到更新的，按滞后处理
            out = base.tail(days).reset_index(drop=True)
            out.attrs['stale'] = True
            return out
        one = last.reset_index(drop=True)       # 连缓存都没有：只回这一根，上层单独标注
        one.attrs['only_today'] = True
        return one

    if base is not None:
        stale = base.tail(days).reset_index(drop=True)
        stale.attrs['stale'] = True   # 上层据此标注"滞后"，避免把旧数据当当日
        return stale
    return None


# ------------------------------------------------------------------ 筹码分布
def _cyq_once(kdata, factor=150):
    """
    复刻东财前端 CYQCalculator 算法（单点计算，等价于 index=最后一根）。
    返回 (benefit_part, avg_cost, c90_low, c90_high, c70_low, c70_high)
    kdata: [{'open','close','high','low','hsl'}, ...]（不复权）
    """
    if not kdata:
        return None
    maxp = max(k['high'] for k in kdata)
    minp = min(k['low'] for k in kdata)
    span = maxp - minp
    acc = max(0.01, span / (factor - 1))
    xdata = [0.0] * factor

    for k in kdata:
        o, c, h, l = k['open'], k['close'], k['high'], k['low']
        avg = (o + c + h + l) / 4.0
        turn = min(1.0, (k['hsl'] or 0) / 100.0)
        if turn <= 0:
            continue
        hi_i = int(np.floor((h - minp) / acc))
        lo_i = int(np.ceil((l - minp) / acc))
        g0 = (factor - 1) if h == l else 2.0 / (h - l)
        g1 = int(np.floor((avg - minp) / acc))
        decay = 1.0 - turn
        for n in range(factor):
            xdata[n] *= decay
        if h == l:
            j = min(max(g1, 0), factor - 1)
            xdata[j] += g0 * turn / 2.0
        else:
            lo_i = min(max(lo_i, 0), factor - 1)
            hi_i = min(max(hi_i, 0), factor - 1)
            for j in range(lo_i, hi_i + 1):
                cur = minp + acc * j
                if cur <= avg:
                    if abs(avg - l) < 1e-8:
                        xdata[j] += g0 * turn
                    else:
                        xdata[j] += (cur - l) / (avg - l) * g0 * turn
                else:
                    if abs(h - avg) < 1e-8:
                        xdata[j] += g0 * turn
                    else:
                        xdata[j] += (h - cur) / (h - avg) * g0 * turn

    total = float(sum(xdata))
    if total <= 0:
        return None
    cur_price = kdata[-1]['close']

    def cost_by_chip(chip):
        s = 0.0
        for i, x in enumerate(xdata):
            if s + x > chip:
                return minp + i * acc
            s += x
        return 0.0

    below = 0.0
    for i, x in enumerate(xdata):
        if cur_price >= minp + i * acc:
            below += x
    return {
        'benefit_ratio': below / total,          # 0~1 小数
        'avg_cost': round(cost_by_chip(total * 0.5), 2),
        'c90_low': round(cost_by_chip(total * 0.05), 2),
        'c90_high': round(cost_by_chip(total * 0.95), 2),
        'c70_low': round(cost_by_chip(total * 0.15), 2),
        'c70_high': round(cost_by_chip(total * 0.85), 2),
        'cur_price': cur_price,
    }


def chips(code6, bars=210):
    """
    筹码分布（纯 Python 复刻东财前端算法）。
    需不复权日K + 换手率 → 仅东财源可用；腾讯源返回 None。
    """
    try:
        # weak_ok=False：筹码必须有换手率，只认东财源，不接受腾讯降级数据
        df, src = kline(code6, klt=101, fqt=0, lmt_cands=(bars, 300, 800),
                        weak_ok=False)
    except Exception:
        return None
    if df is None or len(df) < 60 or df['turnover'].isna().all():
        return None
    d = df.tail(bars)
    kdata = [{'open': r.open, 'close': r.close, 'high': r.high, 'low': r.low,
              'hsl': r.turnover} for r in d.itertuples()]
    res = _cyq_once(kdata)
    if res:
        res['src'] = src
        res['date'] = d['date'].iloc[-1].strftime('%Y-%m-%d')
        res['bars'] = len(d)
    return res


# ------------------------------------------------------------------ 通达信原语
# 精简移植自 MyTT (https://github.com/mpquant/MyTT)，GPL-3.0。只取"原语"，不含其 2 级指标。
# 约定：
#   1) 全部纯数学实现，不触网、不读写缓存，可离线单测；
#   2) 统一 t_ 前缀，避免与 pandas / numpy / 本文件既有命名冲突；
#   3) 入参为 pd.Series 时输出保持同索引 Series（MyTT 原版一律返回 ndarray，
#      此处改动是为了能直接 df['x'] = t_xxx(...) 对齐索引）；入参非 Series 则返回 ndarray；
#   4) 中间量保持全精度、不做 round，只在渲染层取整。
# 语义对齐通达信公式系统，逐条与 MyTT 原文一致。


def _s(S):
    """入参归一化为 float Series（仅本区块内部使用）"""
    if isinstance(S, pd.Series):
        return S.astype('float64', copy=False)
    return pd.Series(np.asarray(S, dtype='float64'))


def _ix(S):
    """输入是 Series 则返回其索引，否则 None"""
    return S.index if isinstance(S, pd.Series) else None


def _out(v, ix):
    """还原输出形态：有索引 → Series，无索引 → ndarray"""
    v = np.asarray(v)
    return pd.Series(v, index=ix) if ix is not None else v


# --- 0 级：核心工具
def t_ref(S, N=1):
    """REF：整体下移 N 根（前 N 根为 NaN）"""
    return _out(_s(S).shift(N).values, _ix(S))


def t_diff(S, N=1):
    """DIFF：S 与 N 根前之差"""
    return _out(_s(S).diff(N).values, _ix(S))


def t_std(S, N):
    """STD：N 周期总体标准差（ddof=0，与 BOLL 口径一致）"""
    return _out(_s(S).rolling(N).std(ddof=0).values, _ix(S))


def t_sum(S, N):
    """SUM：N>0 为 N 周期累计和；N=0 为自首根起的累计和"""
    s = _s(S)
    v = s.rolling(N).sum().values if N > 0 else s.cumsum().values
    return _out(v, _ix(S))


def t_const(S):
    """CONST：把序列末值铺成常量序列"""
    s = _s(S)
    return _out(np.full(len(s), s.iloc[-1] if len(s) else np.nan), _ix(S))


def t_hhv(S, N):
    """HHV：N 周期内最高值"""
    return _out(_s(S).rolling(N).max().values, _ix(S))


def t_llv(S, N):
    """LLV：N 周期内最低值"""
    return _out(_s(S).rolling(N).min().values, _ix(S))


def t_hhvbars(S, N):
    """HHVBARS：N 周期内最高值距当前的周期数"""
    return _out(_s(S).rolling(N).apply(
        lambda x: np.argmax(x[::-1]), raw=True).values, _ix(S))


def t_llvbars(S, N):
    """LLVBARS：N 周期内最低值距当前的周期数"""
    return _out(_s(S).rolling(N).apply(
        lambda x: np.argmin(x[::-1]), raw=True).values, _ix(S))


def t_ma(S, N):
    """MA：N 日简单移动平均"""
    return _out(_s(S).rolling(N).mean().values, _ix(S))


def t_ema(S, N):
    """EMA：指数移动平均，alpha=2/(N+1)"""
    return _out(_s(S).ewm(span=N, adjust=False).mean().values, _ix(S))


def t_sma(S, N, M=1):
    """SMA：中国式平滑平均，alpha=M/N"""
    return _out(_s(S).ewm(alpha=M / N, adjust=False).mean().values, _ix(S))


def t_wma(S, N):
    """WMA：线性加权平均，Yn=(1*X1+2*X2+...+n*Xn)/(1+2+...+n)"""
    return _out(_s(S).rolling(N).apply(
        lambda x: x[::-1].cumsum().sum() * 2 / N / (N + 1), raw=True).values, _ix(S))


def t_dma(S, A):
    """DMA：动态移动平均。A 为常数走 ewm；A 为序列时逐点递推（NaN 视作 1.0）"""
    ix = _ix(S)
    s = _s(S)
    if isinstance(A, (int, float)):
        return _out(s.ewm(alpha=float(A), adjust=False).mean().values, ix)
    a = np.array(_s(A), dtype='float64')
    a[np.isnan(a)] = 1.0
    sv = np.array(s.values, dtype='float64')
    y = np.zeros(len(sv))
    if len(sv):
        y[0] = sv[0]
    for i in range(1, len(sv)):
        y[i] = a[i] * sv[i] + (1 - a[i]) * y[i - 1]
    return _out(y, ix)


def t_avedev(S, N):
    """AVEDEV：平均绝对偏差（与自身均值的绝对差均值）"""
    return _out(_s(S).rolling(N).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True).values, _ix(S))


def t_slope(S, N):
    """SLOPE：N 周期线性回归斜率"""
    return _out(_s(S).rolling(N).apply(
        lambda x: np.polyfit(range(N), x, deg=1)[0], raw=True).values, _ix(S))


def t_forcast(S, N):
    """FORCAST：N 周期线性回归末点预测值"""
    return _out(_s(S).rolling(N).apply(
        lambda x: np.polyval(np.polyfit(range(N), x, deg=1), N - 1), raw=True).values, _ix(S))


def t_last(S, A, B):
    """LAST：从前 A 根到前 B 根持续满足 S"""
    return np.array(pd.Series(_s(S)).rolling(A + 1).apply(
        lambda x: np.all(x[::-1][B:]), raw=True), dtype=bool)


def t_if(S, A, B):
    """IF：S 为真取 A，否则取 B"""
    return _out(np.where(np.asarray(_s(S), dtype=bool), A, B), _ix(S))


def t_max(S1, S2):
    return _out(np.maximum(S1, S2), _ix(S1))


def t_min(S1, S2):
    return _out(np.minimum(S1, S2), _ix(S1))


# --- 1 级：应用层
def t_count(S, N):
    """COUNT：近 N 周期内 S 成立的天数"""
    return t_sum(S, N)


def t_every(S, N):
    """EVERY：近 N 周期是否每根都成立"""
    return t_sum(S, N) >= N


def t_exist(S, N):
    """EXIST：近 N 周期内是否至少成立一次"""
    return t_sum(S, N) > 0


def t_filter(S, N):
    """FILTER：S 成立后压掉其后 N 周期内的信号（信号去重，返回 bool 数组）"""
    b = np.array(_s(S), dtype=bool).copy()
    for i in range(len(b)):
        if b[i]:
            b[i + 1:i + 1 + N] = False
    return b


def t_barslast(S):
    """BARSLAST：上一次条件成立距当前的周期数（当根成立记 0）"""
    m = np.concatenate(([0], np.where(np.asarray(_s(S), dtype=bool), 1, 0)))
    for i in range(1, len(m)):
        m[i] = 0 if m[i] else m[i - 1] + 1
    return m[1:].astype(int)


def t_barslastcount(S):
    """BARSLASTCOUNT：连续满足 S 的周期数（不满足记 0）"""
    b = np.asarray(_s(S), dtype=bool)
    rt = np.zeros(len(b) + 1, dtype=int)
    for i in range(len(b)):
        rt[i + 1] = rt[i] + 1 if b[i] else 0
    return rt[1:]


def t_barssincen(S, N):
    """BARSSINCEN：N 周期内首次成立距当前的周期数（N 周期内没成立过 → NaN）"""
    return _out(pd.Series(_s(S)).rolling(N).apply(
        lambda x: N - 1 - np.argmax(x) if (np.argmax(x) or x[0]) else np.nan,
        raw=True).values, _ix(S))


def t_cross(S1, S2):
    """CROSS：S1 上穿 S2（金叉）当根为 True"""
    a, b = np.asarray(S1, dtype='float64'), np.asarray(S2, dtype='float64')
    return np.concatenate(([False],
                           np.logical_not((a > b)[:-1]) & (a > b)[1:]))


def t_longcross(S1, S2, N):
    """LONGCROSS：S1 连续 N 周期低于 S2 后，本周期上穿"""
    a, b = np.asarray(S1, dtype='float64'), np.asarray(S2, dtype='float64')
    return np.array(np.logical_and(t_last(a < b, N, 1), (a > b)), dtype=bool)


def t_valuewhen(S, X):
    """VALUEWHEN：S 成立时取 X 当前值，否则沿用上次成立时的值"""
    ix = _ix(S)
    v = np.where(np.asarray(_s(S), dtype=bool), X, np.nan)
    return _out(pd.Series(v).ffill().values, ix)


def t_between(S, A, B):
    """BETWEEN：S 处于 A/B 之间（支持 A<S<B 或 A>S>B 两种序）"""
    s = _s(S)
    return _out(((A < s) & (s < B)) | ((A > s) & (s > B)), _ix(S))


def t_toprange(S):
    """TOPRANGE：当前值已是近多少周期内的最高值（0=当根即最高）"""
    v = np.asarray(_s(S), dtype='float64')
    rt = np.zeros(len(v), dtype=int)
    for i in range(1, len(v)):
        rt[i] = int(np.argmin(np.flipud(v[:i] < v[i])))
    return rt


def t_lowrange(S):
    """LOWRANGE：当前值已是近多少周期内的最低值（0=当根即最低）"""
    v = np.asarray(_s(S), dtype='float64')
    rt = np.zeros(len(v), dtype=int)
    for i in range(1, len(v)):
        rt[i] = int(np.argmin(np.flipud(v[:i] > v[i])))
    return rt


# --- 1 级：形态类（持有/离场状态机）
def t_sar(h, l, step=2, limit=20):
    """
    SAR 抛物线转向（通达信口径，逐根与 TDX 的 SAR 一致）。

    移植自 MyTT_plus.py 的 TDX_SAR（https://github.com/mpquant/MyTT, GPL-3.0），
    注意 MyTT_plus 里另有一个 SAR() 是聚宽版、口径与通达信不同，此处刻意取 TDX 版，
    以符合本库"口径对齐通达信"的既有约定。step / limit 为百分数，
    默认 step=2（AF 步长 2%）、limit=20（AF 极限 20%），即通达信 SAR(10,2,20) 的后两个参数。

    语义（这是本原语在"持仓/逃顶"语境下的全部价值）：
      - SAR 位于价格**下方** → 多头持有期，SAR 即**移动止损/止盈线**，价格不破就继续拿；
      - SAR 位于价格**上方** → 空头/离场期，SAR 即压力线，站上它才谈得上重新做多；
      - 由多转空（SAR 上穿价格）当根 = 教科书意义上的离场信号。
    返回与输入同索引的 Series（首根为收盘基准 NaN 之外的真实起点）。
    """
    ix = _ix(h) if isinstance(h, pd.Series) else _ix(l)
    hh = np.asarray(_s(h), dtype='float64')
    ll = np.asarray(_s(l), dtype='float64')
    n = len(hh)
    out = np.full(n, np.nan)
    if n == 0:
        return _out(out, ix)
    af_step, af_limit = float(step) / 100.0, float(limit) / 100.0
    bull, af, ep = True, af_step, hh[0]
    out[0] = ll[0]
    for i in range(1, n):
        # 1) 顺势则加速：多创新高 / 空创新低时 AF 递进（封顶 af_limit）
        if bull:
            if hh[i] > ep:
                ep, af = hh[i], min(af + af_step, af_limit)
        else:
            if ll[i] < ep:
                ep, af = ll[i], min(af + af_step, af_limit)
        # 2) 递推 SAR
        out[i] = out[i - 1] + af * (ep - out[i - 1])
        # 3) 修正：SAR 不得进入前两根的价格区间（不允许被当日/昨日振幅扫到）
        if bull:
            out[i] = max(out[i - 1], min(out[i], ll[i], ll[i - 1]))
        else:
            out[i] = min(out[i - 1], max(out[i], hh[i], hh[i - 1]))
        # 4) 翻转判定，并按通达信规则重置 AF 与极值点
        if bull:
            if ll[i] < out[i]:
                bull = False
                tmp_sar = ep                 # 上阶段极值：多头的最高点
                ep, af = ll[i], af_step
                out[i] = tmp_sar if hh[i - 1] == tmp_sar else tmp_sar + af * (ep - tmp_sar)
        else:
            if hh[i] > out[i]:
                bull = True
                ep, af = hh[i], af_step
                out[i] = min(ll[i], ll[i - 1])
    return _out(out, ix)


# ------------------------------------------------------------------ 对外小工具
def macd_series(closes, fast=12, slow=26, signal=9):
    """
    对任意长度的收盘价序列算 MACD，返回 (dif, dea, hist) 三个 ndarray。
    与 indicators() 内的日线口径完全同源（都走 t_ema 原语），
    供 macd_path.py 这类"对模拟外推序列重算 MACD"的场景复用，
    避免各脚本各写一份 ewm 实现、口径悄悄漂移。
    """
    c = np.asarray(closes, dtype='float64')
    dif = t_ema(c, fast) - t_ema(c, slow)
    dea = t_ema(dif, signal)
    return dif, dea, 2 * (dif - dea)


def kdj_series(c, h, l, n=9, m1=3, m2=3):
    """
    对任意 OHLC 序列算 KDJ，返回 (k, d, j)。
    K/D 用 t_sma(S, m, 1)（alpha=m1/n 的中国式平滑），与通达信 SMA 一致；
    入参若为 pd.Series 则输出保持同索引 Series（便于直接赋回 df 列），否则返回 RangeIndex Series。
    """
    ix = c.index if isinstance(c, pd.Series) else None
    c, h, l = (pd.Series(np.asarray(x, dtype='float64'), index=ix) for x in (c, h, l))
    ln, hn = l.rolling(n).min(), h.rolling(n).max()
    rsv = (c - ln) / (hn - ln).replace(0, np.nan) * 100
    k = t_sma(rsv, m1, 1)
    d = t_sma(k, m2, 1)
    return k, d, 3 * k - 2 * d


# ------------------------------------------------------------------ 指标计算
def indicators(df):
    """在日K上补齐 18 维指标列，返回同一个 df（新增列）"""
    df = df.copy()
    c, h, l, v = df['close'], df['high'], df['low'], df['volume']
    for n in (5, 10, 20, 30, 60, 120, 250):
        df['ma%d' % n] = c.rolling(n).mean()
    # MACD(12,26,9)：改走 t_ 原语，口径等同通达信 EMA（数值与旧 ewm 写法一致）
    df['dif'] = t_ema(c, 12) - t_ema(c, 26)
    df['dea'] = t_ema(df['dif'], 9)
    df['macd'] = 2 * (df['dif'] - df['dea'])
    # 金叉/死叉用 CROSS 原语判定，避免上层再用肉眼或不等号近似
    df['macd_gc'] = t_cross(df['dif'], df['dea'])            # DIF 上穿 DEA
    df['macd_dc'] = t_cross(df['dea'], df['dif'])            # DIF 下穿 DEA
    df['macd_since_gc'] = t_barslast(df['macd_gc'])          # 距上次金叉的交易日数
    df['macd_since_dc'] = t_barslast(df['macd_dc'])          # 距上次死叉的交易日数
    df['macd_gc60'] = t_count(df['macd_gc'], 60)             # 近 60 日金叉次数
    # KDJ(9,3,3)：走 kdj_series（t_sma 原语），与 weekly() 同源
    df['k'], df['d'], df['j'] = kdj_series(c, h, l)
    # RSI(6/12/24)：up/dn 的 Wilder 平滑等价于 t_sma(S, N, 1)
    diff = c.diff()
    up, dn = diff.clip(lower=0), -diff.clip(upper=0)

    def _rsi(p):
        u = t_sma(up, p, 1)
        d_ = t_sma(dn, p, 1)
        return (100 - 100 / (1 + u / d_.replace(0, np.nan))).fillna(50)

    df['rsi6'], df['rsi12'], df['rsi24'] = _rsi(6), _rsi(12), _rsi(24)
    # WR(14)
    hh, ll = h.rolling(14).max(), l.rolling(14).min()
    df['wr14'] = (hh - c) / (hh - ll).replace(0, np.nan) * 100
    # CCI(14)
    tp = (h + l + c) / 3
    mad = tp.rolling(14).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    df['cci14'] = (tp - tp.rolling(14).mean()) / (0.015 * mad.replace(0, np.nan))
    # BIAS(6/24)
    df['bias6'] = (c / c.rolling(6).mean() - 1) * 100
    df['bias24'] = (c / c.rolling(24).mean() - 1) * 100
    # ATR(14) 与 ATR(20)：MyTT/TDX 默认 20 期，一并给出便于跨口径核对
    tr = np.maximum(h - l, np.maximum((h - c.shift()).abs(), (l - c.shift()).abs()))
    df['tr'] = tr
    df['atr14'] = tr.rolling(14).mean()
    df['atr20'] = tr.rolling(20).mean()
    df['atr_pct'] = df['atr14'] / c * 100
    # DMI(14)/ADX/ADXR
    up_m, dn_m = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up_m > dn_m) & (up_m > 0), up_m, 0.0), index=df.index)
    mdm = pd.Series(np.where((dn_m > up_m) & (dn_m > 0), dn_m, 0.0), index=df.index)
    trs = tr.rolling(14).sum()
    df['pdi'] = pdm.rolling(14).sum() / trs.replace(0, np.nan) * 100
    df['mdi'] = mdm.rolling(14).sum() / trs.replace(0, np.nan) * 100
    dx = (df['pdi'] - df['mdi']).abs() / (df['pdi'] + df['mdi']).replace(0, np.nan) * 100
    df['adx'] = dx.rolling(14).mean()
    # ADXR 公式为 (ADX+REF(ADX,6))/2；此处刻意沿用本库 14 期平滑的 ADX 序列，
    # 以保住 adx 列的历史可比性（若改成 MyTT 的 6 期平滑，既有报告数字会全变）
    df['adxr'] = (df['adx'] + df['adx'].shift(6)) / 2
    # OBV
    df['obv'] = (np.sign(diff) * v).fillna(0).cumsum()
    # BOLL(20,2)
    df['boll_mid'] = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    df['boll_up'] = df['boll_mid'] + 2 * sd
    df['boll_dn'] = df['boll_mid'] - 2 * sd
    df['boll_width'] = (df['boll_up'] - df['boll_dn']) / df['boll_mid'] * 100
    # 量能
    for n in (5, 10, 20):
        df['vol%d' % n] = v.rolling(n).mean()
    df['vol_ratio'] = df['vol5'] / df['vol20'] * 100
    # ---- 以下为本次按 MyTT 合并方案新增的维度（全部走 t_ 原语，口径对齐通达信）----
    lc = t_ref(c, 1)
    # BBI 多空分水岭 (3,6,12,20)：价格站上 BBI 视为多空转换
    df['bbi'] = (t_ma(c, 3) + t_ma(c, 6) + t_ma(c, 12) + t_ma(c, 20)) / 4
    # TRIX 三重指数平滑 (12,20)：趋势级别的确认/背离
    tr3 = t_ema(t_ema(t_ema(c, 12), 12), 12)
    df['trix'] = (tr3 - t_ref(tr3, 1)) / t_ref(tr3, 1) * 100
    df['trma'] = t_ma(df['trix'], 20)
    # ROC 变动率 (12,6)：与 MTM 同源，取 ROC 是因为它做了归一化、可跨股比较
    df['roc12'] = 100 * (c - t_ref(c, 12)) / t_ref(c, 12)
    df['maroc'] = t_ma(df['roc12'], 6)
    # VR 容量比率 (26)：上涨日量与下跌日量之比，<70 为地量区
    df['vr26'] = (t_sum(t_if(c > lc, v, 0), 26) /
                  t_sum(t_if(c <= lc, v, 0), 26).replace(0, np.nan) * 100)
    # MFI 资金流量指标 (14)：带量的 RSI，补 RSI 只看价不看量的缺口
    typ = (h + l + c) / 3
    tp1 = t_ref(typ, 1)
    df['mfi14'] = 100 - 100 / (1 + t_sum(t_if(typ > tp1, typ * v, 0), 14) /
                               t_sum(t_if(typ < tp1, typ * v, 0), 14).replace(0, np.nan))
    # BRAR 情绪指标 (26)：AR 看开盘人气、BR 看昨收承接，补"全市场情绪"维度
    df['ar26'] = (t_sum(h - df['open'], 26) /
                  t_sum(df['open'] - l, 26).replace(0, np.nan) * 100)
    df['br26'] = (t_sum(t_max(h - lc, 0), 26) /
                  t_sum(t_max(lc - l, 0), 26).replace(0, np.nan) * 100)
    # XSII 薛斯通道II (102,7)：AA 走大小两套通道；判底主要看价格贴近大通道下沿 xsii_dn
    aa = t_ma((2 * c + h + l) / 4, 5)
    df['xsii_up'] = aa * 102 / 100
    df['xsii_dn'] = aa * 98 / 100
    cc = ((2 * c + h + l) / 4 - t_ma(c, 20)).abs() / t_ma(c, 20)
    dd = t_dma(c, cc)
    df['xsii_up2'] = 1.07 * dd
    df['xsii_dn2'] = 0.93 * dd
    # TD 神奇九转：原语化重写。td_buy/td_sell 与旧版 for 循环逐根等价
    # （旧逻辑 = 条件连续成立计数，BARSLASTCOUNT 即其定义）
    cond_buy = c < t_ref(c, 4)
    cond_sell = c > t_ref(c, 4)
    df['td_buy'] = t_barslastcount(cond_buy)
    df['td_sell'] = t_barslastcount(cond_sell)
    b9, s9 = (df['td_buy'] == 9), (df['td_sell'] == 9)
    df['td_buy9'] = b9                      # 绿9 当日
    df['td_sell9'] = s9                     # 红9 当日
    df['td_since9'] = t_barslast(b9)        # 距上次绿9 的交易日数（当根成立记 0）
    df['td_since9_sell'] = t_barslast(s9)   # 距上次红9 的交易日数
    df['td_cnt60'] = t_count(b9, 60)        # 近 60 日绿9 次数
    df['td_cnt60_sell'] = t_count(s9, 60)   # 近 60 日红9 次数
    # ---- 以下为"逃顶/持有"侧新增（对称于抄底侧）----
    # SAR 抛物线转向（通达信口径 SAR(10,2,20)）：持仓期的移动止损/止盈线
    df['sar'] = t_sar(h, l, 2, 20)
    df['sar_bull'] = c > df['sar']          # 现价在 SAR 上方=多头持有期
    df['sar_flip'] = t_cross(df['sar'], c)  # SAR 上穿价格当根=由多转空（离场信号）
    # 0 轴上死叉：经典顶部预警（对称于抄底侧的"0 轴下金叉"）
    df['macd_dc_up0'] = df['macd_dc'] & (df['dif'] > 0)
    # 距离区间极值：多空位置感的对称补充
    df['dist_60h'] = (c / t_hhv(h, 60) - 1) * 100
    df['dist_250h'] = (c / t_hhv(h, 250) - 1) * 100
    return df


def weekly(df):
    """由日K合成周K并计算周线 MACD/KDJ/MA5/MA10"""
    w = (df.set_index('date')
           .resample('W-FRI')
           .agg({'open': 'first', 'high': 'max', 'low': 'min',
                 'close': 'last', 'volume': 'sum'})
           .dropna().reset_index())
    c, h, l = w['close'], w['high'], w['low']
    # 周线 MACD/KDJ 与日线同源（t_ema / t_sma 原语），避免日周两套口径
    w['dif'], w['dea'], w['macd'] = macd_series(c)
    w['k'], w['d'], w['j'] = kdj_series(c, h, l)
    for n in (5, 10):
        w['wma%d' % n] = c.rolling(n).mean()
    return w


def td_chain(df, n=12):
    """返回近 n 日 TD 序列字符串列表"""
    seq = []
    for r in df.tail(n).itertuples():
        if r.td_buy > 0:
            mk = '绿%d' % r.td_buy + ('<==9' if r.td_buy == 9 else '')
        elif r.td_sell > 0:
            mk = '红%d' % r.td_sell + ('<==9' if r.td_sell == 9 else '')
        else:
            mk = '—'
        seq.append('%s %.2f %s' % (r.date.strftime('%m-%d'), r.close, mk))
    return seq


def swing_lows(df, lookback=60, piv=5, keep=3):
    """
    找出近 lookback 日的"摆动低点"（左右各 piv 根都不低于它），返回列表（由旧到新）。
    判定用 LLVBARS 原语：以 p+piv 为窗口右端开 2*piv+1 窗口，若最低点恰在正中（距离=piv），
    p 即为摆动低点。相邻过近的低点视为同一簇，只保留簇内最低的那根。
    """
    n = len(df)
    if n < 2 * piv + 6:
        return []
    lb = min(lookback, n - piv - 1)
    w = df.iloc[n - lb - piv:].reset_index(drop=True)
    m = len(w)
    z = w['low'].reset_index(drop=True)
    since_low = t_llvbars(z, 2 * piv + 1)
    cand = [p for p in range(piv, m - piv) if int(since_low[p + piv]) == piv]
    merged = []
    for p in cand:
        if merged and p - merged[-1] <= piv:
            if z.iloc[p] < z.iloc[merged[-1]]:
                merged[-1] = p
        else:
            merged.append(p)
    out = []
    for p in merged[-keep:]:
        r = w.loc[p]
        out.append({'date': r['date'].strftime('%m-%d'),
                    'low': round(float(r['low']), 2),
                    'dif': round(float(r['dif']), 3),
                    'macd': round(float(r['macd']), 3),
                    'close': round(float(r['close']), 2)})
    return out


def divergence(df, lookback=60):
    """底部背离检测：取最近两个摆动低点，比较"价格新低 vs DIF/柱抬高" """
    lows = swing_lows(df, lookback=lookback)
    if not lows:
        return '样本不足'
    if len(lows) < 2:
        return '仅 1 个摆动低点(%s %.2f)，结构不充分' % (lows[-1]['date'], lows[-1]['low'])
    a, b = lows[-1], lows[-2]
    if a['low'] < b['low']:
        if a['dif'] > b['dif']:
            extra = '（柱同步抬高，强度更好）' if a['macd'] > b['macd'] else '（柱未同步，强度一般）'
            return ('底背离：价 %.2f<%.2f 但 DIF %.3f>%.3f，%s→%s %s' %
                    (a['low'], b['low'], a['dif'], b['dif'], b['date'], a['date'], extra))
        return '无背离：价 %.2f<%.2f 且 DIF %.3f<%.3f 同创新低' % (
            a['low'], b['low'], a['dif'], b['dif'])
    return '低点抬高：%.2f>%.2f（DIF %.3f vs %.3f），%s→%s，暂不构成背离' % (
        a['low'], b['low'], a['dif'], b['dif'], b['date'], a['date'])


def swing_highs(df, lookback=60, piv=5, keep=3):
    """
    找出近 lookback 日的"摆动高点"（左右各 piv 根都不高于它），返回列表（由旧到新）。
    与 swing_lows 严格对称：判定用 HHVBARS 原语，以 p+piv 为窗口右端开 2*piv+1 窗口，
    若最高点恰在正中（距离=piv），p 即为摆动高点。相邻过近的高点并入同一簇，
    只保留簇内最高的那根（对称于低点侧的"只留最低"）。
    """
    n = len(df)
    if n < 2 * piv + 6:
        return []
    lb = min(lookback, n - piv - 1)
    w = df.iloc[n - lb - piv:].reset_index(drop=True)
    m = len(w)
    z = w['high'].reset_index(drop=True)
    since_high = t_hhvbars(z, 2 * piv + 1)
    cand = [p for p in range(piv, m - piv) if int(since_high[p + piv]) == piv]
    merged = []
    for p in cand:
        if merged and p - merged[-1] <= piv:
            if z.iloc[p] > z.iloc[merged[-1]]:
                merged[-1] = p
        else:
            merged.append(p)
    out = []
    for p in merged[-keep:]:
        r = w.loc[p]
        out.append({'date': r['date'].strftime('%m-%d'),
                    'high': round(float(r['high']), 2),
                    'dif': round(float(r['dif']), 3),
                    'macd': round(float(r['macd']), 3),
                    'close': round(float(r['close']), 2)})
    return out


def divergence_top(df, lookback=60):
    """顶部背离检测：取最近两个摆动高点，比较"价格新高 vs DIF/柱走低" """
    highs = swing_highs(df, lookback=lookback)
    if not highs:
        return '样本不足'
    if len(highs) < 2:
        return '仅 1 个摆动高点(%s %.2f)，结构不充分' % (highs[-1]['date'], highs[-1]['high'])
    a, b = highs[-1], highs[-2]
    if a['high'] > b['high']:
        if a['dif'] < b['dif']:
            extra = '（柱同步走低，强度更好）' if a['macd'] < b['macd'] else '（柱未同步，强度一般）'
            return ('顶背离：价 %.2f>%.2f 但 DIF %.3f<%.3f，%s→%s %s' %
                    (a['high'], b['high'], a['dif'], b['dif'], b['date'], a['date'], extra))
        return '无背离：价 %.2f>%.2f 且 DIF %.3f>%.3f 同创新高' % (
            a['high'], b['high'], a['dif'], b['dif'])
    return '高点走低：%.2f<%.2f（DIF %.3f vs %.3f），%s→%s，暂不构成顶背离' % (
        a['high'], b['high'], a['dif'], b['dif'], b['date'], a['date'])


def _nn(v):
    """取数值，NaN/None 一律返回 None（渲染层统一显示 '-'）"""
    try:
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


def top_signals(df, px=None):
    """
    顶部预警信号汇总（刻意与抄底侧对称）：只输出客观读数与标签，**不产出任何结论**。
    集中在这里是为了让 analyze_stock.py / holdings_check.py 共用同一套阈值，
    避免"两处各写一套超买线、数字对不上"的老问题。

    返回 dict：
      tags        超买/顶部风险标签列表
      td_sell     当前红9 计数
      dc_up0_days 距上次"0 轴上死叉"的交易日数
      sar / sar_bull / sar_days / sar_dist  SAR 移动止损线状态
      dist_60h / dist_250h  距 60/250 日最高价的回撤幅度
      div_top     顶背离文字结论
      highs       最近摆动高点列表
      n_overbought 标签数（仅计数，供上层自行加权，不在此定档）
    """
    lr = df.iloc[-1]
    px = _nn(px) or _nn(lr['close'])
    tags = []
    if lr['td_sell'] >= 9:
        tags.append('TD红9(变盘预警)')
    elif lr['td_sell'] >= 7:
        tags.append('TD红%d(接近9)' % int(lr['td_sell']))
    if _nn(lr['j']) is not None and lr['j'] > 100:
        tags.append('J超买>100')
    elif _nn(lr['j']) is not None and lr['j'] > 80:
        tags.append('J超买区>80')
    if _nn(lr['cci14']) is not None and lr['cci14'] > 100:
        tags.append('CCI超买>100')
    if _nn(lr['wr14']) is not None and lr['wr14'] < 20:
        tags.append('WR超买<20')
    if _nn(lr['rsi6']) is not None and lr['rsi6'] > 80:
        tags.append('RSI6超买>80')
    if _nn(lr['mfi14']) is not None and lr['mfi14'] > 80:
        tags.append('MFI超买>80(量价双超买)')
    if _nn(lr['bias24']) is not None and lr['bias24'] > 15:
        tags.append('BIAS24正乖离%+.1f%%(偏离过大)' % lr['bias24'])
    if _nn(lr['close']) is not None and _nn(lr['boll_up']) is not None and lr['close'] > lr['boll_up']:
        tags.append('冲出BOLL上轨')
    if _nn(lr['xsii_up2']) is not None and lr['close'] > lr['xsii_up2']:
        tags.append('冲破XSII大通道上轨')
    if _nn(lr['vr26']) is not None and lr['vr26'] > 250:
        tags.append('VR26过热>250')
    if _nn(lr['ar26']) is not None and lr['ar26'] > 150:
        tags.append('AR26人气过高>150')
    if _nn(lr['macd']) is not None and lr['macd'] > 0 and len(df) > 1 and \
            _nn(df.iloc[-2]['macd']) is not None and abs(lr['macd']) < abs(df.iloc[-2]['macd']):
        tags.append('MACD红柱收窄(顶背离前兆)')

    # 0 轴上死叉：与抄底侧"0 轴下金叉"对称的顶部信号
    try:
        dc0_days = int(t_barslast(np.asarray(df['macd_dc_up0'], dtype=bool))[-1])
        dc0_cnt = int(np.asarray(df['macd_dc_up0'], dtype=bool).sum())
    except Exception:
        dc0_days, dc0_cnt = None, None

    sar = _nn(lr['sar'])
    sar_out = None
    if sar is not None and px is not None:
        try:
            sar_days = int(t_barslast(np.asarray(df['sar_flip'], dtype=bool))[-1])
        except Exception:
            sar_days = None
        sar_out = {'price': round(sar, 2),
                   'bull': bool(px > sar),
                   'days': sar_days,
                   'dist': round((sar / px - 1) * 100, 2)}
    d60 = _nn(lr['dist_60h'])
    d250 = _nn(lr['dist_250h'])
    highs = swing_highs(df)
    return {'tags': tags, 'n_overbought': len(tags),
            'td_sell': int(lr['td_sell']),
            'td_cnt60_sell': int(lr['td_cnt60_sell']),
            'td_since9_sell': int(lr['td_since9_sell']),
            'dc_up0_days': dc0_days, 'dc_up0_cnt': dc0_cnt,
            'sar': sar_out,
            'j': _nn(lr['j']), 'cci14': _nn(lr['cci14']), 'wr14': _nn(lr['wr14']),
            'rsi6': _nn(lr['rsi6']), 'mfi14': _nn(lr['mfi14']),
            'bias24': _nn(lr['bias24']),
            'dist_60h': d60, 'dist_250h': d250,
            'div_top': divergence_top(df), 'highs': highs}


# ------------------------------------------------------------------ 关键价位体系
def levels(df, chip=None, rt=None):
    """
    汇总关键价位：支撑（近端→远端）、压力（近端→远端）、ATR 止损。
    """
    lr = df.iloc[-1]
    px = float(rt['price']) if (rt and rt.get('price')) else float(lr['close'])
    atr = float(lr['atr14'])

    def mk(items, side):
        out = []
        for name, p in items:
            if p is None or (isinstance(p, float) and np.isnan(p)):
                continue
            out.append({'name': name, 'price': round(float(p), 2),
                        'dist': round((float(p) / px - 1) * 100, 2), 'side': side})
        return out

    sup = mk([('MA5', lr['ma5']), ('MA10', lr['ma10']), ('MA20', lr['ma20']),
              ('MA60', lr['ma60']), ('MA120', lr['ma120']),
              ('BOLL下轨', lr['boll_dn']),
              ('90%筹码下沿', chip['c90_low'] if chip else None)], 'down')
    res = mk([('MA5', lr['ma5']), ('MA10', lr['ma10']), ('MA20', lr['ma20']),
              ('MA60', lr['ma60']), ('MA120', lr['ma120']),
              ('BOLL上轨', lr['boll_up']),
              ('筹码平均成本', chip['avg_cost'] if chip else None),
              ('90%筹码上沿', chip['c90_high'] if chip else None)], 'up')
    sup = sorted([s for s in sup if s['price'] < px], key=lambda x: -x['price'])
    res = sorted([r_ for r_ in res if r_['price'] > px], key=lambda x: x['price'])
    stop = None
    if sup:
        key = sup[0]['price']
        stop = {'name': '%s - 1×ATR' % sup[0]['name'],
                'price': round(key - atr, 2),
                'note': 'ATR=%.2f(%.1f%%日波幅)' % (atr, lr['atr_pct'])}
    return {'price': round(px, 2), 'atr': round(atr, 2),
            'atr_pct': round(float(lr['atr_pct']), 2),
            'supports': sup, 'resistances': res, 'stop': stop}


# ------------------------------------------------------------------ 大盘环境
def market_snapshot():
    """主要指数 + 全市场涨跌家数（东财）"""
    idx = {'1.000001': '上证指数', '0.399001': '深证成指', '0.399006': '创业板指',
           '1.000688': '科创50', '0.399303': '国证2000'}
    txt = _get('/api/qt/ulist.np/get', {
        'fltt': '2', 'invt': '2', 'secids': ','.join(idx.keys()),
        'fields': 'f12,f14,f2,f3,f6', 'ut': UT},
        hosts=EM_PUSH, tries=3, budget=12, wait_cd=False,
        min_interval=LIGHT_INTERVAL)   # 指数快照：轻量类
    out = {'index': []}
    if txt:
        for it in ((json.loads(txt).get('data') or {}).get('diff') or []):
            out['index'].append({'name': it.get('f14'), 'price': n2f(it.get('f2')),
                                 'pct': n2f(it.get('f3'))})
    txt2 = _get('/api/qt/clist/get', {
        'pn': '1', 'pz': '1', 'po': '1', 'np': '1', 'fltt': '2', 'invt': '2',
        'fid': 'f3', 'fs': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23',
        'fields': 'f104,f105,f106', 'ut': UT},
        hosts=EM_PUSH, tries=3, budget=12, wait_cd=False,
        min_interval=LIGHT_INTERVAL)   # 涨跌家数：轻量类
    if txt2:
        try:
            d = json.loads(txt2).get('data') or {}
            diff = d.get('diff') or []
            if diff:
                out['up'] = n2f(diff[0].get('f104'))
                out['down'] = n2f(diff[0].get('f105'))
                out['flat'] = n2f(diff[0].get('f106'))
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ 一键分析
def analyze(code, want_chips=True, want_flow=True, want_rt=True):
    """
    一站式采集：返回 dict(code, name, df, ind, wk, chip, flow, rt, src, levels, top)
    any 维度失败时对应字段为 None，不抛异常。
    `top` 为 top_signals() 的结果（顶部/超买标签，唯一口径），在此算一次供
    analyze_stock.render() 与 holdings_check.tags_for() 共用，避免重复计算与双写。
    """
    code6 = norm_code(code)
    res = {'code': code6, 'name': None, 'src': None, 'df': None, 'ind': None,
           'wk': None, 'chip': None, 'flow': None, 'rt': None, 'levels': None,
           'top': None, 'errors': []}
    if want_rt:
        try:
            res['rt'] = realtime(code6).get(code6)
            if res['rt']:
                res['name'] = res['rt'].get('name')
        except Exception as e:
            res['errors'].append('实时:%s' % str(e)[:60])
    try:
        df, src = kline(code6)
        res['df'], res['src'] = df, src
        res['ind'] = indicators(df)
        res['wk'] = weekly(df)
    except Exception as e:
        res['errors'].append('日K:%s' % str(e)[:60])
        return res
    if want_chips:
        try:
            res['chip'] = chips(code6)
        except Exception as e:
            res['errors'].append('筹码:%s' % str(e)[:60])
    if want_flow and not is_etf_code(code6):
        try:
            res['flow'] = fund_flow(code6, days=10)
            if res['flow'] is None:
                res['errors'].append(
                    '资金:东财 fflow 逐日明细未取到(历史主机不可达，且延迟源/缓存也失败)，'
                    '下游用实时快照的主力净额兜底')
        except Exception as e:
            res['errors'].append('资金:%s' % str(e)[:60])
    res['levels'] = levels(res['ind'], res['chip'], res['rt'])
    try:
        # 顶部/超买标签在此算一次，下游（render / 持仓标签）只读不算，杜绝双写
        res['top'] = top_signals(res['ind'], (res['rt'] or {}).get('price'))
    except Exception as e:
        res['errors'].append('顶部信号:%s' % str(e)[:60])
    if not res['name'] and res.get('rt'):
        res['name'] = res['rt'].get('name')
    return res


if __name__ == '__main__':
    c = sys.argv[1] if len(sys.argv) > 1 else '002552'
    a = analyze(c)
    if a.get('ind') is None:
        print('代码 %s 名称 %s 数据获取失败' % (a.get('code'), a.get('name')))
        print('错误:', a.get('errors'))
        sys.exit(1)
    lr = a['ind'].iloc[-1]
    print('代码 %s 名称 %s 源 %s 共 %d 根' % (a['code'], a['name'], a['src'], len(a['ind'])))
    print('收盘 %.2f DIF %.3f DEA %.3f 柱 %.3f' % (lr['close'], lr['dif'], lr['dea'], lr['macd']))
    print('TD 绿%d 红%d' % (lr['td_buy'], lr['td_sell']))
    print('筹码:', a['chip'])
    print('实时:', a['rt'])
    print('资金:', None if a['flow'] is None else a['flow'].tail(3).to_dict('records'))
    print('价位:', a['levels'])
    print('错误:', a['errors'])
