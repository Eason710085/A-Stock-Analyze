# -*- coding: utf-8 -*-
"""
macd_stats.py —— MACD 交叉 / TD 九转 事件的历史复盘统计

用途:
  当用户说"日线 MACD 金叉了/要金叉了，会不会反弹"（抄底）或
  "MACD 高位死叉了 / TD 红9 了，要不要走"（逃顶）时，不要凭感觉回答。
  本脚本把该股历史上同类信号逐笔复盘，用**这只票自己**的胜率与中位收益做锚。

  两个方向（--side）：
    buy（默认，抄底口径）—— "0 轴下方金叉"（macd_gc & DIF<0）
    top（逃顶口径）      —— "0 轴上方死叉"（macd_dc_up0）与"TD 红9"（td_sell9）各跑一套

  每套给出：
    1) 逐笔明细：日期 / 价格 / DIF / 量比 / 后 3-5-10 日涨跌 / 10 日内最大涨跌 /
       是否（收复|跌破）MA20 / 10 日内是否（再死叉|再金叉）
    2) 分类统计：全部样本 / 缩量 / 放量 / 深水区或高位区 各自的胜率与中位数收益
    3) 当前状态：落在哪一档历史统计中；top 模式额外给出 SAR 移动止损线与超买标签

用法:
    python3 macd_stats.py 002580                        # 抄底口径（默认，行为不变）
    python3 macd_stats.py 002580 --side top             # 逃顶口径
    python3 macd_stats.py 002580 --start 20220101 --horizon 10

依赖: 同目录 stock_lib.py

口径:
  - 事件一律直接消费 stock_lib indicators() 的 CROSS/九转列，不在本地手写不等号近似，
    保证与 analyze_stock.py / holdings_check.py 的报告口径完全同源。
  - 量比定义: vr = MA5(成交量)/MA20(成交量)，>1 为放量（与报告里的 vol_ratio/100 一致）。
  - 收益以后复权/前复权收盘价计算百分比，不含分红与手续费。
  - 未满 N 日的最近样本标注"未满"，不参与统计。
  - **顶部口径的方向是反的**：胜率在 top 模式下读作"跌率"（事件后如期下跌的比例），
    MA20 读作"破 MA20"，再死叉读作"再金叉"（= 顶部信号后多头又夺回主动、信号失效）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np   # noqa: E402
import stock_lib as L  # noqa: E402


def collect(df, start_ts, horizon=10, kind='gc0'):
    """
    收集事件样本。kind 决定"什么算一次信号"：

      gc0 —— **0 轴下方金叉**（抄底口径，默认值，行为与改造前逐笔一致）
      dc0 —— **0 轴上方死叉**（顶部口径，直接消费 indicators() 的 macd_dc_up0 列，
             即 CROSS(DEA,DIF) 且 DIF>0，与报告口径同源）
      td9 —— **TD 神奇九转红9**（顶部口径，消费 td_sell9 列，即 td_sell==9 当日）

    三种 kind 共用同一套前视指标，故统计函数可原样复用；前视列里同时算出
    "收复/跌破 MA20"与"再死叉/再金叉"两组镜像指标，由渲染侧按 side 选取。
    """
    dif = df['dif'].values
    dea = df['dea'].values
    if kind == 'gc0':
        hit = np.asarray(df['macd_gc'], dtype=bool) & (dif < 0)
    elif kind == 'dc0':
        hit = np.asarray(df['macd_dc_up0'], dtype=bool)
    elif kind == 'td9':
        hit = np.asarray(df['td_sell9'], dtype=bool)
    else:
        raise ValueError('未知 kind: %s' % kind)
    close = df['close'].values
    ma20 = df['ma20'].values
    v5 = df['vol5'].values
    v20 = df['vol20'].values
    dates = df['date'].values
    n = len(df)
    ev = []
    for i in range(n):
        if not hit[i]:
            continue
        if dates[i] < start_ts:
            continue
        e = {'i': i, 'date': dates[i], 'close': float(close[i]),
             'dif': float(dif[i]), 'dea': float(dea[i]),
             'vr': float(v5[i] / v20[i]) if (v20[i] and v20[i] == v20[i] and v20[i] > 0) else None,
             'full': (i + horizon) < n}
        for h in (3, 5, 10):
            if (i + h) < n:
                e['r%d' % h] = (close[i + h] / close[i] - 1) * 100
            else:
                e['r%d' % h] = None
        seg = close[i + 1:i + horizon + 1]
        if len(seg):
            e['maxup'] = (seg.max() / close[i] - 1) * 100
            e['maxdn'] = (seg.min() / close[i] - 1) * 100
            snap_m = ma20[i + 1:i + horizon + 1]
            e['touch_ma20'] = bool(np.any(seg > snap_m))   # 抄底侧用：是否收复 MA20
            e['break_ma20'] = bool(np.any(seg < snap_m))   # 逃顶侧用：是否跌破 MA20
            sub_d, sub_e = dif[i + 1:i + horizon + 1], dea[i + 1:i + horizon + 1]
            e['redeath'] = bool(np.any(sub_d < sub_e))     # 抄底侧用：再死叉率
            e['regold'] = bool(np.any(sub_d > sub_e))      # 逃顶侧用：再金叉率
        else:
            e['maxup'] = e['maxdn'] = None
            e['touch_ma20'] = e['break_ma20'] = None
            e['redeath'] = e['regold'] = None
        ev.append(e)
    return ev


def stats(ev, tag, side='buy'):
    """按 side 打印一行统计。side='top' 时方向反转：胜率→跌率、触MA20→破MA20、再死叉→再金叉"""
    s = [e for e in ev if e['full'] and e.get('r10') is not None]
    if not s:
        print('  %-22s 样本不足' % tag)
        return
    r5 = np.array([e['r5'] for e in s if e.get('r5') is not None])
    r10 = np.array([e['r10'] for e in s if e.get('r10') is not None])
    mu = np.array([e['maxup'] for e in s if e.get('maxup') is not None])
    md = np.array([e['maxdn'] for e in s if e.get('maxdn') is not None])
    if side == 'top':
        win5 = (r5 < 0).mean() * 100 if len(r5) else float('nan')
        win10 = (r10 < 0).mean() * 100 if len(r10) else float('nan')
        m2 = np.mean([e['break_ma20'] for e in s if e.get('break_ma20') is not None]) * 100
        x2 = np.mean([e['regold'] for e in s if e.get('regold') is not None]) * 100
    else:
        win5 = (r5 > 0).mean() * 100 if len(r5) else float('nan')
        win10 = (r10 > 0).mean() * 100 if len(r10) else float('nan')
        m2 = np.mean([e['touch_ma20'] for e in s if e.get('touch_ma20') is not None]) * 100
        x2 = np.mean([e['redeath'] for e in s if e.get('redeath') is not None]) * 100
    print('  %-22s %4d  %6.1f%%  %6.1f%%  %+6.2f%%  %+6.2f%%  %+6.1f%%  %+6.1f%%  %6.1f%%  %6.1f%%' % (
        tag, len(s), win5, win10,
        np.median(r5) if len(r5) else float('nan'),
        np.median(r10) if len(r10) else float('nan'),
        np.median(mu) if len(mu) else float('nan'),
        np.median(md) if len(md) else float('nan'),
        m2, x2))


HDR = '  %-22s %4s  %7s  %7s  %7s  %7s  %7s  %7s  %7s  %7s' % (
    '分组', '样本', '5日胜率', '10日胜率', '5日中位', '10日中位',
    '最大涨', '最大跌', '触MA20', '再死叉')

HDR_TOP = '  %-22s %4s  %7s  %7s  %7s  %7s  %7s  %7s  %7s  %7s' % (
    '分组', '样本', '5日跌率', '10日跌率', '5日中位', '10日中位',
    '最大涨', '最大跌', '破MA20', '再金叉')


def _row(e, horizon):
    """打印一行逐笔明细（两个 side 共用同一格式，只是最大涨/跌的列序对调）"""
    def p(v, nd=2):
        return '-' if v is None else ('%+.' + str(nd) + 'f%%') % v
    print('  %s %8.2f %+8.3f %+8.3f %7s %8s %8s %8s %10s %10s%s' % (
        np.datetime_as_string(e['date'], unit='D'), e['close'], e['dif'], e['dea'],
        '-' if e['vr'] is None else '%.2f' % e['vr'],
        p(e.get('r3')), p(e.get('r5')), p(e.get('r10')),
        p(e.get('maxdn')), p(e.get('maxup')),
        '' if e['full'] else '  <未满%d日>' % horizon))


def report_buy(df, name, code6, start, start_ts, horizon):
    """抄底口径：0 轴下方金叉（改造前 main() 的原有逻辑，逐行保留）"""
    print('===== %s(%s) MACD 0轴下方金叉 历史复盘 | 样本区间 %s 起 | 共%d根日K =====' % (
        name, code6, start, len(df)))

    ev = collect(df, start_ts, horizon=horizon, kind='gc0')
    if not ev:
        print('该区间内没有出现 0 轴下方金叉样本。')
    else:
        print('\n[逐笔明细]（后 N 日涨幅以金叉当日收盘为基准）')
        print('  %-11s %8s %8s %8s %7s %8s %8s %8s %10s %10s' % (
            '日期', '收盘', 'DIF', 'DEA', '量比', '后3日', '后5日', '后10日', '10日最大跌', '10日最大涨'))
        for e in ev[-25:]:
            _row(e, horizon)

        print('\n[分类统计]（仅统计已满 %d 日的样本）' % horizon)
        print(HDR)
        stats(ev, '全部 0轴下金叉')
        stats([e for e in ev if e['vr'] is not None and e['vr'] < 0.8], '缩量 vr<0.8')
        stats([e for e in ev if e['vr'] is not None and e['vr'] >= 0.8], '放量 vr>=0.8')
        stats([e for e in ev if e['dif'] <= -0.3], '深水区 DIF<=-0.3')
        stats([e for e in ev if e['dif'] > -0.3], '浅水区 DIF>-0.3')
        stats([e for e in ev if e['vr'] is not None and e['vr'] >= 1.0 and e['dif'] > -0.3],
              '放量+浅水(最优组合)')

    # ---- 当前状态
    lr = df.iloc[-1]
    print('\n[当前状态]')
    cross_txt = '已金叉' if lr['dif'] > lr['dea'] else '尚未金叉'
    print('  DIF=%.3f DEA=%.3f 柱=%+.3f | %s（DIF 差 %+.3f）| DIF 在 0 轴%s' % (
        lr['dif'], lr['dea'], lr['macd'], cross_txt, lr['dif'] - lr['dea'],
        '上' if lr['dif'] > 0 else '下'))
    if lr['dif'] < 0:
        zone = '深水区(DIF<=-0.3)' if lr['dif'] <= -0.3 else '浅水区(DIF>-0.3)'
        vr = lr['vol_ratio'] / 100.0
        print('  当前落在: %s；量能 vr=%.2f（%s）' % (
            zone, vr, '放量' if vr >= 0.8 else '缩量'))
    else:
        print('  当前 DIF 在 0 轴上方，不属于本脚本统计口径（0轴下金叉）。')

    print('\n[使用建议] 把"该股自身历史上同类信号的中位收益/胜率"作为反弹预期锚，'
          '而不是用"金叉=会涨"的直觉。若中位收益接近 0 或再死叉率 >60%，'
          '应判定为"信号噪音"，不作为入场依据。')


def report_top(df, name, code6, start, start_ts, horizon, rt=None):
    """
    逃顶口径：把"0 轴上方死叉"与"TD 红9"两套顶部信号各跑一套历史复盘。

    与抄底侧最大的差别是**方向镜像**：跌率代替胜率、破 MA20 代替收复 MA20、
    再金叉代替再死叉（顶部信号后多头又夺回主动 = 该信号在本次失效）。
    这样做的意义是：把"顶部信号会不会兑现"这个直觉问题，换成"这只票自己
    历史上同类信号兑现了多少次"的客观锚，与本技能"用历史复盘说话"的口径一致。
    """
    print('===== %s(%s) 顶部信号 历史复盘 | 样本区间 %s 起 | 共%d根日K =====' % (
        name, code6, start, len(df)))
    print('  说明: 跌率 = 事件后 N 日收盘低于事件当日收盘的比例；"中位"为带符号涨幅')

    ev_dc = collect(df, start_ts, horizon=horizon, kind='dc0')
    ev_9 = collect(df, start_ts, horizon=horizon, kind='td9')

    for title, ev, groups in (
        ('0 轴上方死叉 (macd_dc_up0)', ev_dc, [
            ('全部 0轴上死叉', None),
            ('缩量 vr<0.8', lambda x: x['vr'] is not None and x['vr'] < 0.8),
            ('放量 vr>=0.8', lambda x: x['vr'] is not None and x['vr'] >= 0.8),
            ('高位区 DIF>=+0.3', lambda x: x['dif'] >= 0.3),
            ('贴 0 轴 0<DIF<+0.3', lambda x: x['dif'] < 0.3),
            ('放量+高位(最凶组合)', lambda x: x['vr'] is not None and x['vr'] >= 0.8 and x['dif'] >= 0.3),
        ]),
        ('TD 神奇九转 红9 (td_sell9)', ev_9, [
            ('全部 红9', None),
            ('缩量红9 vr<1.0', lambda x: x['vr'] is not None and x['vr'] < 1.0),
            ('放量红9 vr>=1.0', lambda x: x['vr'] is not None and x['vr'] >= 1.0),
            ('DIF>0 红9(强势中)', lambda x: x['dif'] > 0),
            ('DIF<=0 红9(下跌中)', lambda x: x['dif'] <= 0),
        ]),
    ):
        print('\n' + '-' * 78)
        print('【%s】历史共 %d 次' % (title, len(ev)))
        if not ev:
            print('  该区间内无样本。')
            continue
        print('  %-11s %8s %8s %8s %7s %8s %8s %8s %10s %10s' % (
            '日期', '收盘', 'DIF', 'DEA', '量比', '后3日', '后5日', '后10日', '10日最大涨', '10日最大跌'))
        for e in ev[-25:]:
            _row(e, horizon)

        print('\n  [分类统计]（仅统计已满 %d 日的样本；跌率越高=该信号越"灵"）' % horizon)
        print(HDR_TOP)
        for tag, f in groups:
            sub = ev if f is None else [e for e in ev if f(e)]
            stats(sub, tag, side='top')

    # ---- 当前状态（SAR / 超买 / 回撤 / 顶背离 一并摆出，全部走 stock_lib 同一套阈值）
    lr = df.iloc[-1]
    print('\n' + '=' * 78)
    print('[当前状态]')
    print('  DIF=%.3f DEA=%.3f 柱=%+.3f | %s（DIF 差 %+.3f）| DIF 在 0 轴%s' % (
        lr['dif'], lr['dea'], lr['macd'],
        '已死叉' if lr['dif'] < lr['dea'] else '尚未死叉', lr['dif'] - lr['dea'],
        '上' if lr['dif'] > 0 else '下'))
    print('  TD 九转: 红%d（近60日红9 %d 次）| 绿%d（近60日绿9 %d 次）' % (
        int(lr['td_sell']), int(lr['td_cnt60_sell']),
        int(lr['td_buy']), int(lr['td_cnt60'])))
    ts = L.top_signals(df, px=(rt or {}).get('price'))
    print('  超买/顶部标签(%d): %s' % (
        ts['n_overbought'], ' | '.join(ts['tags']) if ts['tags'] else '无'))
    if ts['dc_up0_days'] is not None:
        print('  0轴上死叉: %d 个交易日前（样本区间内共 %d 次）' % (
            ts['dc_up0_days'], ts['dc_up0_cnt']))
    print('  距 60 日最高 %s%% / 距 250 日最高 %s%%' % (
        '-' if ts['dist_60h'] is None else '%+.2f' % ts['dist_60h'],
        '-' if ts['dist_250h'] is None else '%+.2f' % ts['dist_250h']))
    s = ts['sar']
    if s:
        print('  SAR抛物线(step2%%,limit20%%)=%.2f | %s | %s | 距现价 %+.2f%%' % (
            s['price'],
            '多头持有期(现价在其上方)' if s['bull'] else '空头/离场期(现价在其下方)',
            '近期无翻转' if s['days'] is None else '%d 个交易日前翻转' % s['days'],
            s['dist']))
        print('    -> %s' % ('SAR 即移动止损线，收盘跌破即减/离场'
                             if s['bull'] else 'SAR 在上方构成压力，站上它才谈得上重新做多'))
    print('  背离: %s' % ts['div_top'])
    if ts['highs']:
        print('        摆动高点: ' + ' | '.join(
            ['%s %.2f(DIF %+.3f)' % (h['date'], h['high'], h['dif']) for h in ts['highs']]))

    print('\n[使用建议] 顶部信号的价值在于"概率不对称"：看 10 日跌率是否显著 >50%、'
          '再金叉率是否偏低。若跌率接近 50% 或再金叉率 >60%，说明该信号在这只票上'
          '噪音很大，应只当提示、不单独作为离场依据；真正的离场纪律仍以 SAR/参考止损位为准。')


def main():
    argv = sys.argv[1:]
    args = [x for x in argv if not x.startswith('-')]
    if not args:
        print('用法: python3 macd_stats.py <6位代码或名称> [--side buy|top] '
              '[--start YYYYMMDD] [--horizon N]')
        return 2
    side = 'buy'
    if '--side' in argv:
        try:
            side = argv[argv.index('--side') + 1].lower()
        except Exception:
            pass
    if '--top' in argv or '--sell' in argv:
        side = 'top'
    if side not in ('buy', 'top'):
        print('--side 只支持 buy（抄底口径）/ top（顶部口径）')
        return 2
    start = '20220101'
    if '--start' in argv:
        try:
            start = argv[argv.index('--start') + 1]
        except Exception:
            pass
    horizon = 10
    if '--horizon' in argv:
        try:
            horizon = int(argv[argv.index('--horizon') + 1])
        except Exception:
            pass
    start_ts = np.datetime64('%s-%s-%s' % (start[:4], start[4:6], start[6:8]))

    code6, err = L.resolve_symbol(args[0])
    if err or not code6:
        print('解析失败:', err)
        return 1
    a = L.analyze(code6, want_chips=False, want_flow=False, want_rt=True)
    if a['ind'] is None:
        print('日K获取失败:', a['errors'])
        return 1
    df = a['ind']
    name = a['name'] or code6
    if side == 'top':
        report_top(df, name, code6, start, start_ts, horizon, rt=a.get('rt'))
    else:
        report_buy(df, name, code6, start, start_ts, horizon)
    return 0


if __name__ == '__main__':
    sys.exit(main())
