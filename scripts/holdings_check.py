# -*- coding: utf-8 -*-
"""
holdings_check.py —— 持仓/多标的批量体检与处置提示

把"持仓该怎么处理"这件事标准化：一次跑完全部标的，先给横向对比表，
再按规则引擎标注风险点与参考止损，需要细节时加 --detail 看单只 18 维全文。

用法:
    python3 holdings_check.py 002552 603330                       # 只给横向表
    python3 holdings_check.py 002552:51.20 603330:9.80            # 带持仓成本(算盈亏)
    python3 holdings_check.py 002552:51.2:200 603330:9.8:1000     # 代码:成本:股数
    python3 holdings_check.py --file holdings.txt                 # 每行 代码,成本,股数
    python3 holdings_check.py 002552 603330 --detail              # 附带单只全文
    python3 holdings_check.py 002552 --no-market                  # 跳过大盘快照

依赖: 同目录 stock_lib.py / analyze_stock.py

标签口径见 references/playbook.md 的"风险标签表"，本脚本只做客观标注，
"处置建议"是规则映射结果，不构成投资建议。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_lib as L          # noqa: E402
import analyze_stock as A      # noqa: E402


def parse_item(s):
    """'002552:51.2:200' -> (code, cost, shares)"""
    p = str(s).strip().split(':')
    code = p[0]
    cost = None
    shares = None
    try:
        if len(p) > 1 and p[1] not in ('', '-'):
            cost = float(p[1])
        if len(p) > 2 and p[2] not in ('', '-'):
            shares = float(p[2])
    except Exception:
        pass
    return code, cost, shares


def tags_for(a, cost=None):
    """风险标签规则引擎 -> (标签列表, 处置提示)

    顶部/超买侧（TD红9、J、CCI超买、WR、RSI6超买、MFI、BIAS24、BOLL/XSII 上轨、
    VR26、AR26、MACD红柱收窄）**不在本脚本重写阈值**，统一取自 `stock_lib.top_signals()`
    —— 这是该类标签的唯一口径来源，`analyze_stock.py` 的 `[顶部信号]` 块与之同源。
    本函数只维护 top_signals() 未覆盖的部分：抄底侧预警、位置/趋势/量能/资金/筹码/盈亏。
    标签与触发条件的对应关系见 references/playbook.md §七。
    """
    df = a['ind']
    lr = df.iloc[-1]
    pr = df.iloc[-2] if len(df) > 1 else lr
    rt = a['rt'] or {}
    chip = a['chip']
    lv = a['levels'] or {}
    px = rt.get('price') or float(lr['close'])

    # 顶部侧：唯一口径来源（stock_lib.analyze() 已算好，缺失时才兜底现算）
    t = list((a.get('top') or L.top_signals(df, px))['tags'])

    # —— 以下为 top_signals() 不覆盖的项 ——
    if lr['td_buy'] >= 9:
        t.append('TD绿9(转折预警,需放量阳确认)')
    if lr['cci14'] < -100:
        t.append('CCI超卖')
    if lr['rsi6'] < 20:
        t.append('RSI6超卖')

    w60 = df.tail(60)
    lo60 = w60['low'].min()
    dist_low = (px / lo60 - 1) * 100
    if dist_low > 50:
        t.append('距60日低%+.0f%%(高位)' % dist_low)

    if lr['close'] < lr['ma20']:
        t.append('跌破MA20')

    if lr['vol_ratio'] < 60:
        t.append('极度缩量%.0f%%' % lr['vol_ratio'])
    elif lr['vol_ratio'] >= 150:
        t.append('明显放量%.0f%%' % lr['vol_ratio'])

    mn = rt.get('main_net')
    m5 = rt.get('main_5d')
    if mn is not None and m5 is not None and mn < 0 and m5 > 0:
        t.append('当日主力流出(与5日背离)')
    elif mn is not None and mn < 0:
        t.append('当日主力净流出')

    if len(df) > 6:
        if df['obv'].iloc[-1] < df['obv'].iloc[-6]:
            t.append('OBV近5日下行')

    if chip:
        if chip['benefit_ratio'] > 0.9:
            t.append('获利盘>90%(套牢轻但浮盈重)')
        elif chip['benefit_ratio'] < 0.2:
            t.append('获利盘<20%(深套)')
        if px < chip['avg_cost']:
            t.append('现价低于平均成本%.0f%%' % abs((px / chip['avg_cost'] - 1) * 100))

    if lr['macd'] < 0 and pr['macd'] < lr['macd']:
        t.append('MACD绿柱收窄')

    if cost:
        pnl = (px / cost - 1) * 100
        if pnl <= -12:
            t.append('浮亏%.1f%%(需处理)' % pnl)
        elif pnl <= -8:
            t.append('浮亏%.1f%%' % pnl)
        elif pnl >= 15:
            t.append('浮盈%.1f%%(注意保护)' % pnl)

    stop = lv.get('stop') or {}
    stop_px = stop.get('price')
    if stop_px and px <= stop_px:
        advice = '已跌破参考止损 %.2f，按纪律应先减/离场' % stop_px
    elif stop_px and (px / stop_px - 1) * 100 < 3:
        advice = '逼近参考止损 %.2f，跌破即减仓' % stop_px
    elif stop_px:
        advice = '参考止损 %.2f（距现价 %+.1f%%）' % (stop_px, (stop_px / px - 1) * 100)
    else:
        advice = '无有效近端支撑，建议用 1×ATR 自设止损'
    return t, advice


def main():
    argv = sys.argv[1:]
    opts = set(x for x in argv if x.startswith('-'))
    args = [x for x in argv if not x.startswith('-')]
    items = []
    if '--file' in argv:
        try:
            p = argv[argv.index('--file') + 1]
            with open(p, 'r', encoding='utf-8') as fh:
                for ln in fh:
                    ln = ln.strip()
                    if ln and not ln.startswith('#'):
                        items.append(parse_item(ln.replace(',', ':')))
        except Exception as e:
            print('读取持仓文件失败:', e)
            return 1
    items += [parse_item(x) for x in args]
    if not items:
        print('用法: python3 holdings_check.py <代码[:成本[:股数]]> [...] [--detail] [--no-market]')
        return 2

    if '--no-market' not in opts:
        ms = L.market_snapshot()
        if ms.get('index'):
            print('[大盘] ' + ' | '.join(
                ['%s %s(%s%%)' % (i['name'], i.get('price'), i.get('pct'))
                 for i in ms['index'] if i.get('price') is not None]))
        if ms.get('up') is not None:
            print('       涨跌家数: 涨 %s / 跌 %s / 平 %s' % (ms.get('up'), ms.get('down'), ms.get('flat')))
        print()

    rows, details = [], []
    for code, cost, shares in items:
        c6, err = L.resolve_symbol(code)
        if err or not c6:
            print('解析失败: %s (%s)' % (code, err))
            continue
        a = L.analyze(c6)
        details.append(a)
        if a['ind'] is None:
            print('%s 日K获取失败: %s' % (c6, a['errors']))
            continue
        df, lr = a['ind'], a['ind'].iloc[-1]
        rt = a['rt'] or {}
        px = rt.get('price') or float(lr['close'])
        chip = a['chip']
        tags, adv = tags_for(a, cost)
        pnl = (px / cost - 1) * 100 if cost else None
        tdc = ('绿%d' % int(lr['td_buy'])) if lr['td_buy'] > 0 else \
              (('红%d' % int(lr['td_sell'])) if lr['td_sell'] > 0 else '-')
        rows.append({
            'code': c6, 'name': a['name'] or c6, 'px': px,
            'pct': rt.get('pct'), 'cost': cost, 'pnl': pnl, 'shares': shares,
            'td': tdc, 'macd': ('红' if lr['macd'] > 0 else '绿') + ('扩' if abs(lr['macd']) > abs(
                a['ind'].iloc[-2]['macd']) else '缩'),
            'd0': '上' if lr['dif'] > 0 else '下',
            'j': float(lr['j']), 'vr': float(lr['vol_ratio']),
            'atr_pct': float(lr['atr_pct']),
            'dist_low': (px / df.tail(60)['low'].min() - 1) * 100,
            'm5': rt.get('main_5d'), 'm10': rt.get('main_10d'),
            'benefit': chip['benefit_ratio'] * 100 if chip else None,
            'adv': adv, 'tags': tags,
        })

    if not rows:
        return 1

    print('===== 持仓/关注池 横向体检 =====')
    print('%-8s %-8s %8s %8s %8s %8s %6s %6s %5s %6s %7s %8s %8s' % (
        '代码', '名称', '现价', '涨跌%', '成本', '盈亏%', 'TD', 'MACD', '0轴',
        'J值', '量比%', '距60低%', '主力5日'))
    for r in rows:
        def f(v, nd=2, suf=''):
            return '-' if v is None else ('%.' + str(nd) + 'f' + suf) % v
        print('%-8s %-8s %8s %8s %8s %8s %6s %6s %5s %6s %7s %8s %8s' % (
            r['code'], r['name'][:6], f(r['px']), f(r['pct']), f(r['cost']), f(r['pnl']),
            r['td'], r['macd'], r['d0'], f(r['j'], 1), f(r['vr'], 0),
            f(r['dist_low'], 0),
            '-' if r['m5'] is None else '%.2f亿' % (r['m5'] / 1e8)))

    print('\n----- 逐只风险标签与处置提示 -----')
    for r in rows:
        print('\n%s(%s) 现价 %s | 止损提示: %s' % (
            r['name'], r['code'], ('%.2f' % r['px']), r['adv']))
        print('  标签: ' + ('、'.join(r['tags']) if r['tags'] else '无显著风险标签'))
        print('  客观读数: 获利盘%s | 涨跌%s%% | ATR波幅%.1f%% | 10日主力%s' % (
            '-' if r['benefit'] is None else '%.1f%%' % r['benefit'],
            '-' if r['pct'] is None else '%.2f' % r['pct'], r['atr_pct'],
            '-' if r['m10'] is None else '%.2f亿' % (r['m10'] / 1e8)))

    if '--detail' in opts:
        for a in details:
            print('\n' + '-' * 64)
            for line in A.render(a):
                print(line)

    print('\n[说明] 本清单为规则引擎输出的客观标注，"处置提示"是止损纪律映射，'
          '请结合自己仓位与风险预算决定，不构成投资建议。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
