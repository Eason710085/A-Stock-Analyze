# -*- coding: utf-8 -*-
"""
A股个股"抄底/潜伏"多维技术分析脚本
用法: python3 analyze_stock.py <代码或名称>  [--name]
示例: python3 analyze_stock.py 002747     (深市)
      python3 analyze_stock.py sh600036   (带前缀)
      python3 analyze_stock.py 埃斯顿 --name
      python3 analyze_stock.py 159558     (ETF)

输出: 结构化文本报告(中文), 含日线/周线全套指标+TD九转+资金流, 供上层按
SKILL.md 中的判定规则综合给出"是否适合抄底潜伏"的结论。

依赖: akshare, pandas, numpy  (pip install akshare pandas numpy --break-system-packages)
注意: 数据源(东财/新浪/腾讯)网络可能不稳定, 脚本已内置多源回退与重试;
      盘中实时数据优先东财, 新浪/腾讯日K收盘后才更新当日.
"""
import sys
import time
import datetime
import pandas as pd
import numpy as np

OUT = None  # 不落盘, 直接输出到 stdout

# ---------------------------------------------------------------- 工具
def retry(fn, n=8, wait=2):
    last = None
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(wait)
    raise last


def is_etf_code(code):
    return code.startswith(('15', '51', '56', '58'))


def norm_code(code):
    """归一化为纯6位数字"""
    code = str(code).strip().lower()
    code = code.replace('sh', '').replace('sz', '').replace('bj', '').replace('.', '')
    return code


def market_prefix(code6):
    # ETF: 15x=深市, 51x/56x/58x=沪市; 股票: 6x/9x=沪, 0x/3x/2x=深, 4x/8x=北交
    if code6.startswith('15'):
        return 'sz'
    if code6.startswith(('51', '56', '58')):
        return 'sh'
    if code6.startswith(('6', '9')):
        return 'sh'
    if code6.startswith(('0', '3', '2')):
        return 'sz'
    if code6.startswith(('4', '8')):
        return 'bj'
    return 'sh'


# ---------------------------------------------------------------- 数据获取
def resolve_symbol(query):
    """名称->代码 解析; 输入为代码则原样返回"""
    q = str(query).strip()
    if q.replace('sh', '').replace('sz', '').replace('bj', '').isdigit() and len(norm_code(q)) == 6:
        return norm_code(q), None
    df = retry(lambda: __import__('akshare').stock_info_a_code_name())
    df.columns = ['code', 'name']
    hit = df[df['name'].str.contains(q, na=False)]
    if len(hit) == 0:
        return None, f'未找到名称包含"{q}"的A股, 请检查名称或改用6位代码'
    if len(hit) > 1:
        return None, f'名称"{q}"匹配到多只: {hit.head(5).to_dict("records")}, 请用6位代码指定'
    return hit.iloc[0]['code'], None


def get_daily(code6, start='2024-09-01', end=None):
    """获取qfq日线, 多源回退: 东财(含盘中当日) -> 新浪(盘后更新) -> 腾讯"""
    import akshare as ak
    end = end or datetime.date.today().strftime('%Y%m%d')
    s = start.replace('-', '')
    pfx = market_prefix(code6)
    cols = ['date', 'open', 'close', 'high', 'low', 'volume']
    if not is_etf_code(code6):
        # 1) 东财(股票)
        try:
            df = retry(lambda: ak.stock_zh_a_hist(symbol=code6, period='daily',
                       start_date=s, end_date=end, adjust='qfq'), n=4, wait=2)
            df = df.rename(columns={'日期': 'date', '开盘': 'open', '收盘': 'close',
                                    '最高': 'high', '最低': 'low', '成交量': 'volume'})
            df['date'] = pd.to_datetime(df['date'])
            df = df[cols].sort_values('date').reset_index(drop=True)
            return df, '东财'
        except Exception:
            pass
        # 2) 新浪(股票)
        try:
            df = retry(lambda: ak.stock_zh_a_daily(symbol=f'{pfx}{code6}',
                       start_date=start, end_date=end, adjust='qfq'), n=4, wait=2)
            df['date'] = pd.to_datetime(df['date'])
            df = df[cols].sort_values('date').reset_index(drop=True)
            return df, '新浪'
        except Exception:
            pass
        # 3) 腾讯(股票)
        try:
            df = retry(lambda: ak.stock_zh_a_hist_tx(symbol=f'{pfx}{code6}',
                       start_date=start, end_date=end, adjust='qfq'), n=4, wait=2)
            df['date'] = pd.to_datetime(df['date'])
            df = df[cols].sort_values('date').reset_index(drop=True)
            return df, '腾讯'
        except Exception:
            pass
    else:
        # ETF: 东财 -> 新浪(可能含份额折算跳变, 仅近60日可信)
        try:
            df = retry(lambda: ak.fund_etf_hist_em(symbol=code6, period='daily',
                       start_date=s, end_date=end, adjust='qfq'), n=4, wait=2)
            df = df.rename(columns={'日期': 'date', '开盘': 'open', '收盘': 'close',
                                    '最高': 'high', '最低': 'low', '成交量': 'volume'})
            df['date'] = pd.to_datetime(df['date'])
            df = df[cols].sort_values('date').reset_index(drop=True)
            return df, '东财'
        except Exception:
            try:
                df = retry(lambda: ak.fund_etf_hist_sina(symbol=f'{pfx}{code6}'), n=4, wait=2)
                df['date'] = pd.to_datetime(df['date'])
                for col in ['open', 'close', 'high', 'low']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
                df = df[cols].sort_values('date').reset_index(drop=True)
                return df.tail(120).reset_index(drop=True), '新浪'
            except Exception:
                pass
    raise RuntimeError('所有日K数据源均失败')


def get_realtime(code6):
    """最新实时快照(东财), 失败返回None"""
    import akshare as ak
    if is_etf_code(code6):
        try:
            spot = retry(lambda: ak.fund_etf_spot_em(), n=4, wait=2)
            r = spot[spot['代码'] == code6]
            if len(r):
                r = r.iloc[0]
                return {'price': r['最新价'], 'pct': r['涨跌幅'], 'open': r['开盘价'],
                        'high': r['最高价'], 'low': r['最低价'], 'pre_close': r['昨收'],
                        'turnover': r['换手率'], 'vol_ratio': r['量比'],
                        'main_net_ratio': r.get('主力净流入-净占比', None)}
        except Exception:
            return None
    else:
        try:
            s = retry(lambda: ak.stock_bid_ask_em(symbol=code6), n=4, wait=2)
            d = dict(zip(s['item'], s['value']))
            return {'price': float(d['最新']), 'pct': float(d['涨幅']),
                    'open': float(d['今开']), 'high': float(d['最高']),
                    'low': float(d['最低']), 'pre_close': float(d['昨收']),
                    'turnover': float(d['换手']), 'vol_ratio': float(d['量比'])}
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- 指标
def calc_indicators(df):
    c, h, l, v = df['close'], df['high'], df['low'], df['volume']
    for n in [5, 10, 20, 30, 60, 120, 250]:
        df[f'ma{n}'] = c.rolling(n).mean()
    # MACD
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    df['dif'] = e12 - e26
    df['dea'] = df['dif'].ewm(span=9, adjust=False).mean()
    df['macd'] = 2 * (df['dif'] - df['dea'])
    # KDJ
    l9, h9 = l.rolling(9).min(), h.rolling(9).max()
    rsv = (c - l9) / (h9 - l9).replace(0, np.nan) * 100
    df['k'] = rsv.ewm(com=2, adjust=False).mean()
    df['d'] = df['k'].ewm(com=2, adjust=False).mean()
    df['j'] = 3 * df['k'] - 2 * df['d']
    # RSI
    diff = c.diff()
    up, dn = diff.clip(lower=0), -diff.clip(upper=0)

    def _rsi(p):
        u = up.ewm(alpha=1 / p, adjust=False).mean()
        d_ = dn.ewm(alpha=1 / p, adjust=False).mean()
        return (100 - 100 / (1 + u / d_.replace(0, np.nan))).fillna(50)

    df['rsi6'], df['rsi12'], df['rsi24'] = _rsi(6), _rsi(12), _rsi(24)
    # WR(14)
    df['wr14'] = (h.rolling(14).max() - c) / (h.rolling(14).max() - l.rolling(14).min()).replace(0, np.nan) * 100
    # CCI(14)
    tp = (h + l + c) / 3
    mad = tp.rolling(14).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    df['cci14'] = (tp - tp.rolling(14).mean()) / (0.015 * mad.replace(0, np.nan))
    # BIAS
    df['bias6'] = (c / c.rolling(6).mean() - 1) * 100
    df['bias24'] = (c / c.rolling(24).mean() - 1) * 100
    # ATR(14)
    tr = np.maximum(h - l, np.maximum((h - c.shift()).abs(), (l - c.shift()).abs()))
    df['atr14'] = tr.rolling(14).mean()
    # DMI(14)
    up_m = h.diff()
    dn_m = -l.diff()
    plus_dm = pd.Series(np.where((up_m > dn_m) & (up_m > 0), up_m, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn_m > up_m) & (dn_m > 0), dn_m, 0.0), index=df.index)
    tr_sum = tr.rolling(14).sum()
    df['pdi'] = plus_dm.rolling(14).sum() / tr_sum.replace(0, np.nan) * 100
    df['mdi'] = minus_dm.rolling(14).sum() / tr_sum.replace(0, np.nan) * 100
    dx = (df['pdi'] - df['mdi']).abs() / (df['pdi'] + df['mdi']).replace(0, np.nan) * 100
    df['adx'] = dx.rolling(14).mean()
    # OBV
    df['obv'] = (np.sign(diff) * v).fillna(0).cumsum()
    # BOLL
    df['boll_mid'] = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    df['boll_up'], df['boll_dn'] = df['boll_mid'] + 2 * sd, df['boll_mid'] - 2 * sd
    # 量能
    df['vol5'] = v.rolling(5).mean()
    df['vol10'] = v.rolling(10).mean()
    df['vol20'] = v.rolling(20).mean()
    # TD Sequence (神奇九转)
    close = c.values
    n = len(df)
    buy = np.zeros(n, dtype=int)
    sell = np.zeros(n, dtype=int)
    for i in range(4, n):
        if close[i] < close[i - 4]:
            buy[i] = buy[i - 1] + 1 if buy[i - 1] > 0 else 1
        else:
            buy[i] = 0
        if close[i] > close[i - 4]:
            sell[i] = sell[i - 1] + 1 if sell[i - 1] > 0 else 1
        else:
            sell[i] = 0
    df['td_buy'], df['td_sell'] = buy, sell
    return df


def weekly(df):
    w = df.set_index('date').resample('W-FRI').agg({'open': 'first', 'high': 'max', 'low': 'min',
                                                    'close': 'last', 'volume': 'sum'}).dropna().reset_index()
    c, h, l = w['close'], w['high'], w['low']
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    w['dif'] = e12 - e26
    w['dea'] = w['dif'].ewm(span=9, adjust=False).mean()
    w['macd'] = 2 * (w['dif'] - w['dea'])
    l9, h9 = l.rolling(9).min(), h.rolling(9).max()
    w['k'] = ((c - l9) / (h9 - l9).replace(0, np.nan) * 100).ewm(com=2, adjust=False).mean()
    w['d'] = w['k'].ewm(com=2, adjust=False).mean()
    w['j'] = 3 * w['k'] - 2 * w['d']
    for n in [5, 10]:
        w[f'wma{n}'] = c.rolling(n).mean()
    return w


def detect_divergence(df, lookback=30):
    """简化底背离检测: 近lookback日内最后两个显著低点, 比较DIF"""
    w = df.tail(lookback).copy()
    if len(w) < 10:
        return '样本不足'
    i1 = w['low'].idxmin()
    v1, d1 = w.loc[i1, 'low'], w.loc[i1, 'dif']
    w2 = w.loc[:i1]
    if len(w2) > 5:
        i2 = w2['low'].idxmin()
        v2, d2 = w2.loc[i2, 'low'], w2.loc[i2, 'dif']
        if v1 < v2 and d1 > d2:
            return f'底背离(价低点{v1:.2f}<{v2:.2f}但DIF {d1:.2f}>{d2:.2f})'
        if v1 < v2 and d1 < d2:
            return '无背离(价与DIF同创新低)'
        return f'近期双低近似({v1:.2f}@{d1:.2f} vs {v2:.2f}@{d2:.2f}), 无显著背离'
    return '低点结构不充分'


# ---------------------------------------------------------------- 报告
def main():
    if len(sys.argv) < 2:
        print('用法: python3 analyze_stock.py <6位代码或名称>  (名称需加 --name)')
        sys.exit(1)
    query = sys.argv[1]
    try:
        code6, err = resolve_symbol(query)
        if err:
            print('解析失败:', err)
            sys.exit(1)
    except Exception as e:
        print('名称解析失败(网络):', str(e)[:80], '| 请改用6位代码')
        sys.exit(1)

    print(f'# 标的: {code6} | 分析时间: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 60)

    # 实时
    rt = get_realtime(code6)
    if rt:
        print(f'[实时] 现价 {rt["price"]} ({rt["pct"]:+.2f}%) 开{rt["open"]} 高{rt["high"]} 低{rt["low"]} '
              f'昨收{rt["pre_close"]} 换手{rt["turnover"]}% 量比{rt["vol_ratio"]}')
        if rt.get('main_net_ratio') is not None:
            print(f'       主力净流入占比 {rt["main_net_ratio"]}%')

    # 日线
    try:
        df, src = get_daily(code6)
        df = calc_indicators(df)
    except Exception as e:
        print('日线获取失败:', str(e)[:120])
        sys.exit(1)
    lr = df.iloc[-1]
    pr = df.iloc[-2] if len(df) > 1 else lr
    print(f'[日线] 数据源:{src} 范围{df["date"].iloc[0].date()}~{df["date"].iloc[-1].date()} 共{len(df)}日')
    if rt is None:
        print(f'       最新K线 {lr["date"].date()} 收{lr["close"]:.2f}')
    print()

    # ---- TD 九转
    tb, ts = lr['td_buy'], lr['td_sell']
    td_desc = f'绿{tb}' if tb > 0 else (f'红{ts}' if ts > 0 else '无计数')
    print(f'[TD九转] 最新K线计数: {td_desc}')
    print('  近12日序列:')
    seq = []
    for _, r in df.tail(12).iterrows():
        mark = ''
        if r['td_buy'] >= 1:
            mark = f'绿{r["td_buy"]}' + ('<==9' if r['td_buy'] == 9 else '')
        elif r['td_sell'] >= 1:
            mark = f'红{r["td_sell"]}' + ('<==9' if r['td_sell'] == 9 else '')
        seq.append(f"{r['date'].strftime('%m-%d')} {r['close']:.2f} {mark}")
    print('   ' + ' | '.join(seq))

    # ---- 日线指标
    print('\n[日线指标]')
    macd_trend = '扩大' if abs(lr['macd']) > abs(pr['macd']) else '收窄'
    print(f"  MACD: DIF={lr['dif']:.3f} DEA={lr['dea']:.3f} 柱={lr['macd']:.3f} | "
          f"{'红' if lr['macd']>0 else '绿'}柱{macd_trend} | DIF在0轴{'上' if lr['dif']>0 else '下'}")
    kdj_sig = '金叉' if lr['k'] > lr['d'] and pr['k'] <= pr['d'] else ('K>D' if lr['k'] > lr['d'] else 'K<D')
    print(f"  KDJ: K={lr['k']:.1f} D={lr['d']:.1f} J={lr['j']:.1f} | {kdj_sig} | J值{'超买>100' if lr['j']>100 else ('超买区>80' if lr['j']>80 else ('超卖<0' if lr['j']<0 else ('超卖区<20' if lr['j']<20 else '中性')))}")
    print(f"  RSI: 6日={lr['rsi6']:.1f} 12日={lr['rsi12']:.1f} 24日={lr['rsi24']:.1f} | "
          f"{'6日超卖(<20)' if lr['rsi6']<20 else ('6日偏弱(<40)' if lr['rsi6']<40 else ('6日偏强(>60)' if lr['rsi6']>60 else '中性'))}")
    print(f"  WR14={lr['wr14']:.1f} ({'超卖>80' if lr['wr14']>80 else ('超买<20' if lr['wr14']<20 else '中性')}) | "
          f"CCI14={lr['cci14']:.1f} ({'超卖<-100' if lr['cci14']<-100 else ('超买>100' if lr['cci14']>100 else '中性区')})")
    print(f"  BIAS: 6日={lr['bias6']:+.1f}% 24日={lr['bias24']:+.1f}% | ATR14={lr['atr14']:.2f} "
          f"(日均波幅约{lr['atr14']/lr['close']*100:.1f}%)")
    dmi_txt = f"+DI={lr['pdi']:.1f} -DI={lr['mdi']:.1f} ADX={lr['adx']:.1f} | " \
              f"{'多头主导' if lr['pdi']>lr['mdi'] else '空头主导'}{'(强趋势)' if lr['adx']>=25 else '(趋势中等)'}"
    print(f"  DMI: {dmi_txt}")
    obv5 = df['obv'].iloc[-1] - df['obv'].iloc[-6]
    print(f"  OBV: 近5日{'上行' if obv5>0 else '下行'}({obv5:+.0f})")
    ma_line = '  '.join([f"MA{n}={lr[f'ma{n}']:.2f}" for n in [5, 10, 20, 60] if not np.isnan(lr[f'ma{n}'])])
    print(f"  均线: {ma_line}")
    if not np.isnan(lr['ma120']):
        print(f"         MA120={lr['ma120']:.2f} MA250={lr['ma250']:.2f}(有值则显示) | 现价在MA120{'上方' if lr['close']>lr['ma120'] else '下方'}")

    boll_pos = ('上轨上方' if lr['close'] > lr['boll_up'] else
                ('上轨-中轨' if lr['close'] > lr['boll_mid'] else
                 ('中轨-下轨' if lr['close'] > lr['boll_dn'] else '下轨下方')))
    print(f"  BOLL: 上{lr['boll_up']:.2f} 中{lr['boll_mid']:.2f} 下{lr['boll_dn']:.2f} | 现价位于{boll_pos}")
    print(f"  量能: 5日均量={lr['vol5']/1e4:.0f}万手 20日均量={lr['vol20']/1e4:.0f}万手 | "
          f"近5日量/20日量={lr['vol5']/lr['vol20']*100:.0f}% ({'缩量' if lr['vol5']<lr['vol20'] else '放量'})")
    print(f"  背离: {detect_divergence(df)}")

    # 支撑压力
    w60 = df.tail(60)
    hi60, lo60 = w60['high'].max(), w60['low'].min()
    d_hi = df.loc[w60['high'].idxmax(), 'date'].date()
    d_lo = df.loc[w60['low'].idxmin(), 'date'].date()
    print(f"  60日高 {hi60:.2f}({d_hi}) 低 {lo60:.2f}({d_lo})")
    print(f"  现价距60日低: {(lr['close']/lo60-1)*100:+.1f}%")

    # ---- 周线
    try:
        w = weekly(df)
        wl = w.iloc[-1]
        wp = w.iloc[-2]
        print('\n[周线]')
        wt = '红' if wl['macd'] > 0 else '绿'
        wd = '扩大' if abs(wl['macd']) > abs(wp['macd']) else '收窄'
        print(f"  MACD: DIF={wl['dif']:.3f} DEA={wl['dea']:.3f} 柱={wl['macd']:.3f} | {wt}柱{wd} | DIF在0轴{'上' if wl['dif']>0 else '下'}")
        wk_sig = 'K>D' if wl['k'] > wl['d'] else 'K<D'
        print(f"  KDJ: K={wl['k']:.1f} D={wl['d']:.1f} J={wl['j']:.1f} | {wk_sig} | J{'超卖' if wl['j']<20 else ('超买' if wl['j']>80 else '中性')}")
        if not np.isnan(wl['wma5']):
            print(f"  周MA5={wl['wma5']:.2f} 周MA10={wl['wma10']:.2f} | 最新周收{wl['close']:.2f} 在周MA5{'上' if wl['close']>wl['wma5'] else '下'}")
    except Exception as e:
        print('[周线] 计算失败:', str(e)[:80])

    # ---- 资金流(仅股票)
    if not is_etf_code(code6):
        try:
            import akshare as ak
            ff = retry(lambda: ak.stock_individual_fund_flow(stock=code6, market=market_prefix(code6)), n=4, wait=2)
            ff.columns = ['date', 'close', 'pct', 'main_net', 'small_net', 'mid_net', 'large_net',
                          'super_net', 'main_ratio', 'small_ratio', 'mid_ratio', 'large_ratio', 'super_ratio']
            ff['date'] = pd.to_datetime(ff['date'])
            ff['main_net'] = pd.to_numeric(ff['main_net'], errors='coerce')
            t = ff.tail(10)
            s5 = t['main_net'].tail(5).sum() / 1e8
            s10 = t['main_net'].sum() / 1e8
            print('\n[主力资金流] (单位:亿元, +流入/-流出)')
            for _, r in t.tail(6).iterrows():
                print(f"  {r['date'].strftime('%m-%d')}: {r['main_net']/1e8:+.3f}")
            print(f"  近5日合计: {s5:+.2f}亿 | 近10日合计: {s10:+.2f}亿")
        except Exception:
            print('\n[主力资金流] 获取失败(接口/网络)')

    # ---- 筹码(尽力)
    if not is_etf_code(code6):
        try:
            import akshare as ak
            cyq = retry(lambda: ak.stock_cyq_em(symbol=code6), n=3, wait=2)
            cyq.columns = ['date', 'profit_ratio', 'avg_cost', 'c90l', 'c90h', 'c90c', 'c70l', 'c70h', 'c70c']
            cyq['date'] = pd.to_datetime(cyq['date'])
            lr_c = cyq.sort_values('date').iloc[-1]
            print(f"\n[筹码] 最新{lr_c['date'].date()}: 获利比例{lr_c['profit_ratio']}% | "
                  f"平均成本{lr_c['avg_cost']} | 90%成本区 {lr_c['c90l']}~{lr_c['c90h']}")
            print(f"      现价{lr['close']:.2f} vs 平均成本{lr_c['avg_cost']}: "
                  f"{(lr['close']/float(lr_c['avg_cost'])-1)*100:+.1f}%")
        except Exception:
            print('\n[筹码] 获取失败(接口/网络), 可在行情软件中查看')

    print('\n[信号汇总提示] 请由上层按 SKILL.md 判定规则结合各维度给出综合结论; '
          '本脚本仅提供数据与客观信号标注, 不构成投资建议。')

if __name__ == '__main__':
    main()
