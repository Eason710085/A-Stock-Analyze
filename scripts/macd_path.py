# -*- coding: utf-8 -*-
"""
macd_path.py —— MACD 金叉"路径模拟"器

回答三个问题：
  1) 当前 MACD 处于什么状态（是否已金叉 / 绿柱连收几天 / DIF 离 0 轴多远）
  2) 明天要金叉，最少需要涨多少（反推所需涨幅）
  3) 如果接下来按不同速度走（横盘 / 每日 +0.5%~+2% / 每日 -1%），
     第几个交易日会金叉、金叉时股价大约在哪里

用法:
    python3 macd_path.py 002580
    python3 macd_path.py 宝鼎科技 --days 15

依赖: 同目录 stock_lib.py

口径说明:
  - MACD 参数固定 (12, 26, 9)，口径来自 stock_lib 的 t_ema 原语（等价通达信 EMA、
    adjust=False 递推），与前复权日K、行情软件默认口径一致。本脚本不再自带 ewm 实现。
  - 历史明细里的金叉/死叉直接消费 stock_lib 的 CROSS 原语列 macd_gc / macd_dc。
  - 路径模拟是"机械外推"，只用于量化"等待成本"与"金叉价位"，不是预测。
  - 金叉 ≠ 买点：0 轴下方的金叉在弱势趋势中经常很快再死叉，
    历史存活率请用 macd_stats.py 复盘后再下结论。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np   # noqa: E402
import pandas as pd  # noqa: E402
import stock_lib as L  # noqa: E402

# MACD 序列统一复用 stock_lib 的原语实现（对"模拟外推序列"同样适用），
# 保留本名以兼容既有调用点，但算法只有一份。
macd_series = L.macd_series


def simulate(closes, growth, max_day=12, hold_flat=0):
    """
    从 closes 末尾按每日 growth 外推，返回 (首次金叉日, 该日价格, dif, dea)
    没金叉则首位返回 None。
    hold_flat: 先横盘的天数，之后才按 growth 走。
    """
    sim = list(np.asarray(closes, dtype=float))
    hit, hpx = None, None
    for d in range(1, max_day + 1):
        sim.append(sim[-1] * (1 + growth) if d > hold_flat else sim[-1])
        dif, dea, _ = macd_series(sim)
        if hit is None and dif[-1] > dea[-1]:
            hit, hpx = d, sim[-1]
    dif, dea, _ = macd_series(sim)
    return hit, hpx, float(dif[-1]), float(dea[-1])


def needed_gain(closes, hi=0.5, step=0.001):
    """反推：明天至少要涨多少才金叉。返回百分数（%），无解返回 None"""
    sim = list(np.asarray(closes, dtype=float))
    for k in range(0, int(hi / step) + 1):
        g = k * step
        s2 = sim + [sim[-1] * (1 + g)]
        dif, dea, _ = macd_series(s2)
        if dif[-1] > dea[-1]:
            return g * 100, dif[-1], dea[-1]
    return None, None, None


def main():
    argv = sys.argv[1:]
    opts = set(x for x in argv if x.startswith('-'))
    args = [x for x in argv if not x.startswith('-')]
    if not args:
        print('用法: python3 macd_path.py <6位代码或名称> [--days N]')
        return 2
    max_day = 12
    if '--days' in argv:
        try:
            max_day = int(argv[argv.index('--days') + 1])
        except Exception:
            pass

    code6, err = L.resolve_symbol(args[0])
    if err or not code6:
        print('解析失败:', err)
        return 1
    a = L.analyze(code6, want_chips=False, want_flow=True, want_rt=True)
    if a['ind'] is None:
        print('日K获取失败:', a['errors'])
        return 1

    df = a['ind']
    closes = df['close'].values
    dif, dea, hist = macd_series(closes)
    name = a['name'] or code6
    rt = a['rt'] or {}
    px = rt.get('price') or float(closes[-1])

    print('===== %s(%s) MACD 路径模拟 | %s =====' % (
        name, code6, pd.Timestamp(df['date'].iloc[-1]).strftime('%Y-%m-%d')))
    print('现价 %.2f | DIF=%.3f DEA=%.3f 柱=%+.3f | DIF 在 0 轴%s' % (
        px, dif[-1], dea[-1], hist[-1], '上' if dif[-1] > 0 else '下'))

    # ---- 1) 最近 18 日明细
    gc = np.asarray(df['macd_gc'], dtype=bool)   # CROSS 原语：DIF 上穿 DEA
    dc = np.asarray(df['macd_dc'], dtype=bool)   # CROSS 原语：DIF 下穿 DEA
    print('\n[近18日 MACD 明细]')
    print('  日期        收盘     DIF      DEA      柱     状态')
    for i in range(max(0, len(df) - 18), len(df)):
        cross = '★金叉' if gc[i] else ('▼死叉' if dc[i] else '')
        print('  %s  %7.2f  %+.3f  %+.3f  %+.3f  %s%s' % (
            pd.Timestamp(df['date'].iloc[i]).strftime('%Y-%m-%d'), closes[i],
            dif[i], dea[i], hist[i],
            'DIF<DEA(空头)' if dif[i] <= dea[i] else 'DIF>DEA(多头)', cross))

    # ---- 2) 当前状态
    print('\n[当前状态]')
    if dif[-1] > dea[-1]:
        print('  当前处于【金叉（多头）】状态，柱=%+.3f，已持续 %d 日' % (
            hist[-1], _streak(dif, dea, True)))
    else:
        n = _streak(dif, dea, False)
        shrink = _shrink(hist)
        print('  当前处于【死叉（空头）】状态，绿柱已连续 %d 日' % n)
        print('  柱体变化: %s（%s）' % ('连续收窄 %d 日' % shrink if shrink > 0 else '未见收窄',
                                    '空头动能衰减，接近变盘' if shrink >= 3 else '空头动能仍在释放'))
        print('  DIF 距 DEA: %+.3f（需 DIF 上穿 DEA 才金叉）' % (dif[-1] - dea[-1]))

    # ---- 3) 明日金叉所需涨幅
    print('\n[明日金叉所需涨幅（反推）]')
    g, d2, e2 = needed_gain(closes)
    if g is None:
        print('  即使明日涨停也不足以金叉（缺口过大），需更长时间修复')
    else:
        print('  若明日涨幅 >= %.2f%%（约 %.2f 元）→ 当日收盘即金叉（DIF %.3f vs DEA %.3f）'
              % (g, closes[-1] * (1 + g / 100), d2, e2))
        print('  参考: 该股 ATR14=%.2f，日波幅约 %.1f%%，%.2f%% 的单日涨幅属于%s' % (
            df['atr14'].iloc[-1], df['atr_pct'].iloc[-1], g,
            '很难' if g > df['atr_pct'].iloc[-1] else '较容易'))

    # ---- 4) 多路径模拟
    print('\n[多路径模拟]  （从现价起按固定速度外推，机械模型，非预测）')
    paths = [('横盘 0%/日', 0.000, 0), ('每日 +0.5%', 0.005, 0), ('每日 +1%', 0.010, 0),
             ('每日 +1.5%', 0.015, 0), ('每日 +2%', 0.020, 0),
             ('先横盘2日再+2%/日', 0.020, 2), ('每日 -1%（对照）', -0.010, 0)]
    rows = []
    for tag, gr, hf in paths:
        hit, hpx, dd, ee = simulate(closes, gr, max_day=max_day, hold_flat=hf)
        if hit:
            rows.append((tag, '第%2d个交易日' % hit, '%.2f' % hpx,
                         '%+.1f%%' % ((hpx / closes[-1] - 1) * 100)))
        else:
            rows.append((tag, '%d日内不金叉' % max_day, '-', '-'))
    for r in rows:
        print('  %-20s %-14s 金叉价 %-8s 较现价 %s' % r)

    ok = [r for r in rows if r[1].startswith('第')]
    if ok:
        print('  ---> 最快情景: %s 金叉，价 %s' % (ok[0][1], ok[0][2]))

    # ---- 5) 上方压制顺序
    lv = a['levels'] or {}
    print('\n[上方压制顺序]（金叉后仍需逐级突破）')
    if lv.get('resistances'):
        for r in lv['resistances']:
            print('  %-12s %8.2f  （距现价 %+.1f%%）' % (r['name'], r['price'], r['dist']))
    else:
        print('  现价上方暂无明显压力位')
    if lv.get('supports'):
        print('[下方支撑顺序]')
        for s in lv['supports']:
            print('  %-12s %8.2f  （距现价 %+.1f%%）' % (s['name'], s['price'], s['dist']))
    if lv.get('stop'):
        print('  参考止损: %s = %.2f' % (lv['stop']['name'], lv['stop']['price']))

    ff = a.get('flow')
    if ff is not None and len(ff):
        at = getattr(ff, 'attrs', {}) or {}
        # 与 analyze_stock 渲染层同规则：逐日序列不足时不能算 5/10 日合计
        # （单根算出的"近5日=近10日"是假象），此时回落到实时快照口径并明确标注
        if at.get('only_today'):
            print('\n[资金配合度] 东财历史接口不可达，逐日序列不足，近5/10日合计暂不输出')
        elif at.get('stale'):
            print('\n[资金配合度] 本次未取到，以下为上次成功缓存，可能滞后')
            print('  近5日主力 %+.2f亿 | 近10日 %+.2f亿' % (
                ff['main'].tail(5).sum() / 1e8, ff['main'].sum() / 1e8))
        elif at.get('patched'):
            print('\n[资金配合度] 最新1日(%s)由延迟源补齐' % at['patched'])
            print('  近5日主力 %+.2f亿 | 近10日 %+.2f亿' % (
                ff['main'].tail(5).sum() / 1e8, ff['main'].sum() / 1e8))
        else:
            print('\n[资金配合度] 近5日主力 %+.2f亿 | 近10日 %+.2f亿' % (
                ff['main'].tail(5).sum() / 1e8, ff['main'].sum() / 1e8))
    rt = a.get('rt') or {}
    if rt.get('main_net') is not None:
        print('  [实时快照] 当日主力净额 %+.2f亿' % (rt['main_net'] / 1e8), end='')
        if rt.get('main_5d') is not None:
            print(' | 近5日 %+.2f亿' % (rt['main_5d'] / 1e8), end='')
        if rt.get('main_10d') is not None:
            print(' | 近10日 %+.2f亿' % (rt['main_10d'] / 1e8), end='')
        print()

    print('\n[结论提示] 金叉是"统计事件"不是"买入指令"；请配合 macd_stats.py 的历史存活率，'
          '以及 TD / 量能 / 筹码 / 板块环境综合判断。')
    return 0


def _streak(dif, dea, want_up):
    n = 0
    for i in range(len(dif) - 1, 0, -1):
        if (dif[i] > dea[i]) == want_up:
            n += 1
        else:
            break
    return n


def _shrink(hist):
    """绿柱连续收窄天数（柱为负时绝对值变小）"""
    n = 0
    for i in range(len(hist) - 1, 0, -1):
        if hist[i] < 0 and abs(hist[i]) < abs(hist[i - 1]):
            n += 1
        else:
            break
    return n


if __name__ == '__main__':
    sys.exit(main())
