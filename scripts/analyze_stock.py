# -*- coding: utf-8 -*-
"""
analyze_stock.py —— 单只标的 18 维体检（文本报告）

用法:
    python3 analyze_stock.py 002552            # 6 位代码
    python3 analyze_stock.py 宝鼎科技           # 名称（走东财搜索接口）
    python3 analyze_stock.py sh600105          # 可带前缀
    python3 analyze_stock.py 512480 --no-flow  # ETF/跳过资金流
    python3 analyze_stock.py 002552 --no-chip  # 跳过筹码（更快）

依赖: 同目录 stock_lib.py（requests + pandas + numpy，无需 akshare）

输出: 结构化中文文本，含实时/日线/TD九转/18维指标/补充维度(MACD叉距·BBI·TRIX·ROC·VR·
      MFI·BRAR·XSII)/顶背离与摆动高点/**顶部信号块(SAR移动止损线·超买标签·回撤·0轴上
      死叉)**/60日位置/周线/主力资金流/筹码分布/关键价位体系。上层按
      references/report-template.md 套用固定模版，按 references/playbook.md 的判定框架
      给出四档结论，本脚本不产出结论。

      [底部] 与 [顶部] 两块刻意对称：抄底侧看"摆动低点+底背离+0轴下金叉"，
      顶部/持仓侧看"摆动高点+顶背离+0轴上死叉+SAR 移动止损线"，两侧阈值统一收敛在
      stock_lib.py 的 divergence()/divergence_top()/top_signals()，避免两处各写一套。

本脚本不再直接抓数：所有网络与指标计算都收敛到 stock_lib.py（见该文件头部
"踩坑沉淀"）。改动数据口径请改 stock_lib.py，不要在本文件里重复实现。
"""
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_lib as L  # noqa: E402


# ---------------------------------------------------------------- 渲染工具
def _f(v, nd=2, na='-'):
    """安全格式化"""
    if v is None:
        return na
    try:
        if v != v:                      # NaN
            return na
    except Exception:
        pass
    try:
        return ('%.' + str(nd) + 'f') % float(v)
    except Exception:
        return na


def _money(v):
    if v is None:
        return '-'
    try:
        a = abs(v)
        if a >= 1e8:
            return '%+.2f亿' % (v / 1e8)
        if a >= 1e4:
            return '%+.1f万' % (v / 1e4)
        return '%+.0f' % v
    except Exception:
        return '-'


# ---------------------------------------------------------------- 报告渲染
def render(a, show_flow=True, show_chip=True):
    """把 stock_lib.analyze() 的结果渲染为文本行列表（供本脚本与 holdings_check 复用）"""
    if a.get('ind') is None:
        return ['[失败] %s 日K获取失败: %s' % (a.get('code'), a.get('errors'))]

    df = a['ind']
    lr = df.iloc[-1]
    pr = df.iloc[-2] if len(df) > 1 else lr
    rt = a['rt'] or {}
    code = a['code']
    name = a['name'] or code
    chip = a['chip']
    lv = a['levels'] or {}
    # 顶部/超买标签：唯一口径来源是 stock_lib.top_signals()，analyze_stock / holdings_check 共用。
    # stock_lib.analyze() 已算过一份并挂在 a['top']，此处优先复用、不重复计算；
    # 提前到函数开头取，是为了让下方 KDJ 行的超买文案也取自同一份标签，不再手写阈值。
    tp = a.get('top') or L.top_signals(df, px=rt.get('price'))
    out = []

    out.append('# 标的: %s(%s) | 分析时间: %s' %
               (name, code, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    out.append('=' * 64)

    # ---- 实时
    if rt.get('price') is not None:
        out.append('[实时] 现价 %.2f (%s%%) 开%.2f 高%.2f 低%.2f 昨收%.2f 换手%s%% 量比%s' % (
            rt['price'], _f(rt.get('pct'), 2), rt.get('open') or 0, rt.get('high') or 0,
            rt.get('low') or 0, rt.get('pre_close') or 0,
            _f(rt.get('turnover'), 2), _f(rt.get('vol_ratio'), 2)))
        if rt.get('main_net') is not None:
            out.append('       主力净额 %s (%s%%) | 5日 %s | 10日 %s | 成交额 %s' % (
                _money(rt['main_net']), _f(rt.get('main_ratio'), 2),
                _money(rt.get('main_5d')), _money(rt.get('main_10d')),
                _money(rt.get('amount'))))
        if rt.get('pe') is not None or rt.get('pb') is not None:
            out.append('       PE %s PB %s 流通市值 %s' % (
                _f(rt.get('pe'), 2), _f(rt.get('pb'), 2), _money(rt.get('float_cap')).lstrip('+')))
    else:
        out.append('[实时] 获取失败（已回落到日K口径）')

    # ---- 日线
    out.append('[日线] 数据源:%s 范围%s~%s 共%d日' % (
        a['src'], df['date'].iloc[0].date(), df['date'].iloc[-1].date(), len(df)))

    # ---- TD 九转
    tb, ts = int(lr['td_buy']), int(lr['td_sell'])
    td_desc = '绿%d' % tb if tb > 0 else ('红%d' % ts if ts > 0 else '无计数')
    out.append('[TD九转] 最新K线计数: %s' % td_desc)
    out.append('  近12日序列:')
    out.append('   ' + ' | '.join(L.td_chain(df, 12)))
    out.append('  距上次绿9 %d日(近60日%d次) | 距上次红9 %d日(近60日%d次)' % (
        int(lr['td_since9']), int(lr['td_cnt60']),
        int(lr['td_since9_sell']), int(lr['td_cnt60_sell'])))

    # ---- 日线指标
    out.append('[日线指标]')
    macd_trend = '扩大' if abs(lr['macd']) > abs(pr['macd']) else '收窄'
    out.append('  MACD: DIF=%.3f DEA=%.3f 柱=%+.3f | %s柱%s | DIF在0轴%s' % (
        lr['dif'], lr['dea'], lr['macd'], '红' if lr['macd'] > 0 else '绿',
        macd_trend, '上' if lr['dif'] > 0 else '下'))
    out.append('        距上次金叉%d日 | 上次死叉%d日 | 近60日金叉%d次' % (
        int(lr['macd_since_gc']), int(lr['macd_since_dc']), int(lr['macd_gc60'])))
    kdj_sig = '金叉' if (lr['k'] > lr['d'] and pr['k'] <= pr['d']) else \
              ('死叉' if (lr['k'] < lr['d'] and pr['k'] >= pr['d']) else
               ('K>D' if lr['k'] > lr['d'] else 'K<D'))
    # 超买侧文案直接取自 top_signals() 的标签（唯一定义，不在此重写 j>100 / j>80）；
    # 超卖侧不在 top_signals() 的覆盖范围（它只管顶部），留在本地判定。
    j_over = ('J超买>100' if 'J超买>100' in tp['tags'] else
              ('J超买区>80' if 'J超买区>80' in tp['tags'] else ''))
    j_sig = j_over or ('J超卖<0' if lr['j'] < 0 else
                       ('J超卖区<20' if lr['j'] < 20 else 'J中性'))
    out.append('  KDJ: K=%.1f D=%.1f J=%.1f | %s | %s' % (
        lr['k'], lr['d'], lr['j'], kdj_sig, j_sig))
    out.append('  RSI: 6日=%.1f 12日=%.1f 24日=%.1f | %s' % (
        lr['rsi6'], lr['rsi12'], lr['rsi24'],
        '6日超卖(<20)' if lr['rsi6'] < 20 else ('6日偏弱(<40)' if lr['rsi6'] < 40 else
        ('6日偏强(>60)' if lr['rsi6'] > 60 else '中性'))))
    # 超买侧的“判定”统一取自 top_signals() 的标签（唯一定义，不在此重写 wr<20 / cci>100）；
    # 括号里的短标签只是渲染措辞，阈值不在本地决定。超卖侧不在其覆盖范围，留在本地。
    wr_sig = ('超买<20' if 'WR超买<20' in tp['tags'] else
              ('超卖>80' if lr['wr14'] > 80 else '中性'))
    cci_sig = ('超买>100' if 'CCI超买>100' in tp['tags'] else
               ('超卖<-100' if lr['cci14'] < -100 else '中性区'))
    out.append('  WR14=%s (%s) | CCI14=%s (%s)' % (
        _f(lr['wr14'], 1), wr_sig, _f(lr['cci14'], 1), cci_sig))
    out.append('  BIAS: 6日=%+.1f%% 24日=%+.1f%% | ATR14=%.2f ATR20=%.2f (日均波幅约%.1f%%)' % (
        lr['bias6'], lr['bias24'], lr['atr14'], lr['atr20'], lr['atr_pct']))
    out.append('  DMI: +DI=%.1f -DI=%.1f ADX=%s | %s%s' % (
        lr['pdi'], lr['mdi'], _f(lr['adx'], 1),
        '多头主导' if lr['pdi'] > lr['mdi'] else '空头主导',
        '(强趋势)' if (lr['adx'] == lr['adx'] and lr['adx'] >= 25) else '(趋势中等)'))
    obv5 = df['obv'].iloc[-1] - df['obv'].iloc[-6] if len(df) > 6 else 0
    out.append('  OBV: 近5日%s(%+.0f)' % ('上行' if obv5 > 0 else '下行', obv5))
    out.append('  均线: ' + '  '.join(
        ['MA%d=%s' % (n, _f(lr['ma%d' % n])) for n in (5, 10, 20, 60)
         if not _isnan(lr['ma%d' % n])]))
    if not _isnan(lr['ma120']):
        out.append('        MA120=%s MA250=%s | 现价在MA120%s' % (
            _f(lr['ma120']), _f(lr['ma250']),
            '上方' if lr['close'] > lr['ma120'] else '下方'))
    boll_pos = ('上轨上方' if lr['close'] > lr['boll_up'] else
                ('上轨-中轨' if lr['close'] > lr['boll_mid'] else
                 ('中轨-下轨' if lr['close'] > lr['boll_dn'] else '下轨下方')))
    out.append('  BOLL: 上%s 中%s 下%s | 现价位于%s (带宽%s%%)' % (
        _f(lr['boll_up']), _f(lr['boll_mid']), _f(lr['boll_dn']), boll_pos,
        _f(lr['boll_width'], 1)))
    out.append('  量能: 5日均量=%.0f万手 20日均量=%.0f万手 | 近5日量/20日量=%.0f%% (%s)' % (
        lr['vol5'] / 1e4, lr['vol20'] / 1e4, lr['vol_ratio'],
        '缩量' if lr['vol_ratio'] < 100 else '放量'))

    # ---- 补充维度（MyTT 合并方案 P1/P2 新增，全部走通达信原语口径）
    out.append('  [补充维度]')
    out.append('    BBI多空分水岭=%s (现价在其%s) | TRIX=%s vs TRMA=%s (%s) | ROC12=%s%% (MA6=%s)' % (
        _f(lr['bbi']), '上' if lr['close'] > lr['bbi'] else '下',
        _f(lr['trix'], 3), _f(lr['trma'], 3),
        '多头' if lr['trix'] > lr['trma'] else '空头',
        _f(lr['roc12'], 1), _f(lr['maroc'], 1)))
    # 同上：过热/超买侧的判定取自 top_signals() 标签（唯一阈值来源），
    # 地量/偏冷/超卖/偏弱侧不在其覆盖范围，留在本地判定。
    vr_txt = ('过热>250' if 'VR26过热>250' in tp['tags'] else
              ('地量区<70' if lr['vr26'] < 70 else
               ('偏冷<100' if lr['vr26'] < 100 else '中性')))
    mf_txt = ('超买>80' if 'MFI超买>80(量价双超买)' in tp['tags'] else
              ('超卖<20' if lr['mfi14'] < 20 else
               ('偏弱<40' if lr['mfi14'] < 40 else '中性')))
    ar_txt = ('人气高>150' if 'AR26人气过高>150' in tp['tags'] else
              ('人气低<70' if lr['ar26'] < 70 else '人气中性'))
    out.append('    VR26=%s (%s) | MFI14=%s (%s，带量版RSI) | AR26=%s BR26=%s (%s)' % (
        _f(lr['vr26'], 1), vr_txt, _f(lr['mfi14'], 1), mf_txt,
        _f(lr['ar26'], 0), _f(lr['br26'], 0), ar_txt))
    xd1, xd2 = lr['xsii_dn'], lr['xsii_dn2']
    try:
        near = min(abs(lr['close'] / xd1 - 1), abs(lr['close'] / xd2 - 1)) * 100
    except Exception:
        near = float('nan')
    out.append('    XSII通道: 小通道 %s~%s | 大通道 %s~%s | 现价距最近下沿 %s%%%s' % (
        _f(lr['xsii_dn']), _f(lr['xsii_up']), _f(lr['xsii_dn2']), _f(lr['xsii_up2']),
        _f(near, 1), ' ← 已贴下沿，低吸参考区' if (near == near and near <= 1.5) else ''))

    out.append('  背离: %s' % L.divergence(df))
    sl = L.swing_lows(df)
    if sl:
        out.append('        摆动低点: ' + ' | '.join(
            ['%s %.2f(DIF %+.3f)' % (s['date'], s['low'], s['dif']) for s in sl]))

    # ---- 顶部信号（逃顶/持仓侧，与上面抄底侧镜像；阈值统一走 stock_lib.top_signals）
    # tp 已在函数开头取好（复用 stock_lib.analyze() 挂在 a['top'] 的那一份）
    if tp['tags']:
        out.append('[顶部信号] 超买/顶部标签 %d 项: %s' % (
            tp['n_overbought'], ' | '.join(tp['tags'])))
    else:
        out.append('[顶部信号] 无超买/顶部标签触发')
    s = tp['sar']
    if s:
        out.append('  SAR抛物线(step2%%,limit20%%)=%s | %s | %s | 距现价 %+.2f%%' % (
            _f(s['price']),
            '多头持有期(现价在其上方)' if s['bull'] else '空头/离场期(现价在其下方)',
            '近期无翻转' if s['days'] is None else '%d个交易日前翻转' % s['days'],
            s['dist']))
        out.append('        -> %s' % (
            'SAR 即移动止盈/止损线，收盘跌破即减仓或离场'
            if s['bull'] else 'SAR 在上方构成压力，未站上不谈重新做多'))
    out.append('  距60日最高 %s%% | 距250日最高 %s%% | 0轴上死叉 %s' % (
        _f(tp['dist_60h'], 1), _f(tp['dist_250h'], 1),
        '无' if tp['dc_up0_days'] is None
        else '%d个交易日前(样本内共%d次)' % (tp['dc_up0_days'], tp['dc_up0_cnt'])))
    out.append('  顶背离: %s' % tp['div_top'])
    if tp['highs']:
        out.append('        摆动高点: ' + ' | '.join(
            ['%s %.2f(DIF %+.3f)' % (h['date'], h['high'], h['dif']) for h in tp['highs']]))

    # ---- 位置
    w60 = df.tail(60)
    hi60, lo60 = w60['high'].max(), w60['low'].min()
    d_hi = df.loc[w60['high'].idxmax(), 'date'].date()
    d_lo = df.loc[w60['low'].idxmin(), 'date'].date()
    out.append('  60日高 %.2f(%s) 低 %.2f(%s)' % (hi60, d_hi, lo60, d_lo))
    out.append('  现价距60日低 %+.1f%% | 距60日高 %+.1f%% | 60日振幅 %.1f%%' % (
        (lr['close'] / lo60 - 1) * 100, (lr['close'] / hi60 - 1) * 100,
        (hi60 / lo60 - 1) * 100))

    # ---- 周线
    try:
        w = a['wk']
        wl, wp = w.iloc[-1], w.iloc[-2]
        out.append('[周线]')
        out.append('  MACD: DIF=%.3f DEA=%.3f 柱=%+.3f | %s柱%s | DIF在0轴%s' % (
            wl['dif'], wl['dea'], wl['macd'], '红' if wl['macd'] > 0 else '绿',
            '扩大' if abs(wl['macd']) > abs(wp['macd']) else '收窄',
            '上' if wl['dif'] > 0 else '下'))
        out.append('  KDJ: K=%.1f D=%.1f J=%.1f | %s' % (
            wl['k'], wl['d'], wl['j'], 'K>D' if wl['k'] > wl['d'] else 'K<D'))
        if not _isnan(wl['wma5']):
            out.append('  周MA5=%s 周MA10=%s | 最新周收%.2f 在周MA5%s' % (
                _f(wl['wma5']), _f(wl['wma10']), wl['close'],
                '上' if wl['close'] > wl['wma5'] else '下'))
    except Exception as e:
        out.append('[周线] 计算失败: %s' % str(e)[:80])

    # ---- 主力资金流
    if show_flow and not L.is_etf_code(code):
        ff = a['flow']
        if ff is not None and len(ff):
            at = getattr(ff, 'attrs', {}) or {}
            if at.get('stale'):
                tag = ' [本次未取到, 以下为上次成功缓存, 可能滞后]'
            elif at.get('patched'):
                tag = ' [东财历史接口不可达, 最新1日(%s)由延迟源补齐]' % at['patched']
            elif at.get('only_today'):
                tag = ' [东财历史接口不可达, 仅取到最近1日]'
            else:
                tag = ''
            out.append('[主力资金流] (单位:亿元, +流入/-流出)%s' % tag)
            t = ff.tail(10)
            for r in t.itertuples():
                out.append('  %s: %s' % (r.date.strftime('%m-%d'), _money(r.main)))
            if at.get('only_today'):
                out.append('  逐日序列不足，近5日/近10日合计暂不输出 | '
                           '参考上方实时主力净额/5日/10日')
            else:
                out.append('  近5日合计: %s | 近10日合计: %s' % (
                    _money(t['main'].tail(5).sum()), _money(t['main'].sum())))
        elif rt.get('main_net') is not None:
            out.append('[主力资金流] 逐日明细未取到(东财历史接口当前不可达) | '
                       '参考上方实时主力净额/5日/10日，可稍后重跑')
        else:
            out.append('[主力资金流] 获取失败(接口/网络)，可稍后重跑')

    # ---- 筹码
    if show_chip and not L.is_etf_code(code):
        if chip:
            out.append('[筹码] 基于%s 最近%d根不复权日K+换手率递推（东财前端算法复刻）' % (
                chip.get('src', '?'), chip.get('bars', 0)))
            out.append('  获利比例 %.1f%% | 平均成本 %.2f | 90%%成本区 %.2f~%.2f | 70%%成本区 %.2f~%.2f' % (
                chip['benefit_ratio'] * 100, chip['avg_cost'],
                chip['c90_low'], chip['c90_high'], chip['c70_low'], chip['c70_high']))
            out.append('  现价 %.2f vs 平均成本 %.2f: %+.1f%% | 现价在90%%成本区%s' % (
                lr['close'], chip['avg_cost'],
                (lr['close'] / chip['avg_cost'] - 1) * 100,
                '内' if chip['c90_low'] <= lr['close'] <= chip['c90_high'] else '外'))
        else:
            out.append('[筹码] 不可算（腾讯源无换手率，或历史K不足210根）')

    # ---- 关键价位体系
    if lv:
        out.append('[关键价位] 现价 %s | ATR=%s(%s%%日波幅)' % (
            _f(lv.get('price')), _f(lv.get('atr')), _f(lv.get('atr_pct'), 1)))
        if lv.get('supports'):
            out.append('  支撑(由近及远): ' + ' | '.join(
                ['%s %s(%s%%)' % (s['name'], _f(s['price']), _f(s['dist'], 1))
                 for s in lv['supports']]))
        else:
            out.append('  支撑: 现价下方无明显近端支撑（已处于近期低位）')
        if lv.get('resistances'):
            out.append('  压力(由近及远): ' + ' | '.join(
                ['%s %s(%s%%)' % (r['name'], _f(r['price']), _f(r['dist'], 1))
                 for r in lv['resistances']]))
        if lv.get('stop'):
            out.append('  参考止损: %s = %s (%s)' % (
                lv['stop']['name'], _f(lv['stop']['price']), lv['stop']['note']))

    if a.get('errors'):
        out.append('[降级提示] ' + '; '.join(a['errors']))
    out.append('')
    out.append('[说明] 本脚本只输出数据与客观信号标注，不产出买卖结论；'
               '请按 SKILL.md / references/playbook.md 判定框架综合定性。')
    return out


def _isnan(v):
    try:
        return v is None or v != v
    except Exception:
        return True


# ---------------------------------------------------------------- 主流程
def main():
    argv = sys.argv[1:]
    opts = set(x for x in argv if x.startswith('-'))
    args = [x for x in argv if not x.startswith('-')]
    if not args:
        print('用法: python3 analyze_stock.py <6位代码或名称> [--no-chip] [--no-flow]')
        return 2
    q = args[0]
    code6, err = L.resolve_symbol(q)
    if err or not code6:
        print('解析失败:', err)
        return 1
    a = L.analyze(code6, want_chips=('--no-chip' not in opts),
                  want_flow=('--no-flow' not in opts), want_rt=True)
    for line in render(a, show_flow=('--no-flow' not in opts),
                       show_chip=('--no-chip' not in opts)):
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
