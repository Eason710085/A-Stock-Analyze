# MyTT 合并方案（stock-bottom-fishing）

> 评估对象：`https://github.com/mpquant/MyTT`（GPL-3.0，纯 numpy+pandas，与通达信/同花顺口径一致）
> 评估基准：本 skill 现有 `scripts/stock_lib.py`（18 维指标 + 四脚本）
> 状态：**方案 B 已采纳并实施完毕 —— P0~P6 全部落地，P5/P6 文档同步收官中**
>
> 实施结果速览（截至 2026-09-18）：`stock_lib.py` 新增 **35 个 `t_*` 通达信原语** + 7 个补充维度 + **完整逃顶能力**（`t_sar` / `swing_highs` / `divergence_top` / `top_signals`）；TD 九转已原语化重写；`divergence()` 已升级为摆动低点口径，并新增与其严格镜像的 `divergence_top()`；MACD 金叉/死叉已产出 `macd_gc/macd_dc/macd_since_gc/macd_since_dc/macd_gc60` 字段；`analyze_stock.py` 已渲染 `[补充维度]` 与 `[顶部信号]` 两个区块；`macd_stats.py` 新增 `--side top`（0 轴上死叉 / TD 红9 两套顶部口径）。回归 **72 项单测全绿**、四脚本连跑全 rc=0。剩余 P5/P6 文档项与可选清理见 §五 状态列。

---

## 一、结论

**值得融入，但不是整体引入，而是「按需移植约 20 个原语 + 5 个新指标」。**

| MyTT 分层 | 处理 | 理由 |
|---|---|---|
| 2 级指标（MACD/KDJ/RSI/BOLL/WR/BIAS/CCI/OBV/ATR/DMI） | **不移植** | 与本 skill 现有实现口径**完全一致**，引入只会重复与冲突 |
| 0/1 级原语（REF/CROSS/BARSLAST/COUNT/…） | **优先移植** | 这正是本 skill **真正缺的一块** —— 所有"信号确认"类判断现在都靠手写 for 循环或肉眼看序列 |
| 2 级里的 BBI/TRIX/MTM/ROC/VR/MFI/ADXR/XSII/BRAR | **择优纳入** | 对"抄底/潜伏"场景有边际价值（详见附录） |
| 其余指标（KTN/TAQ/ASI/DPO/MASS/EMV/DFMA/CR/PSY/EXPMA） | **不纳入** | 见附录：或纯重复、或方向错误、或噪声过大 |

一句话：**MyTT 的可迁移价值主体在"原语"，少量在"指标"。**

---

## 二、能力对照

| 能力 | 现有 stock_lib | MyTT | 处置 |
|---|---|---|---|
| MA / EMA / SMA / WMA / DMA | 内联 pandas | 有 | 抽为原语 |
| REF / DIFF / STD / SUM | 无（散落 shift/diff） | 有 | 移植 |
| HHV / LLV | `rolling.max/min` | 有 | 移植 |
| **CROSS** | **无** | 有 | **移植（高价值）** |
| **BARSLAST / BARSLASTCOUNT** | **无** | 有 | **移植（高价值）** |
| **COUNT / EVERY / EXIST / FILTER** | **无** | 有 | **移植（高价值）** |
| **HHVBARS / LLVBARS** | **无** | 有 | **移植（高价值）** |
| TOPRANGE / LOWRANGE | 无 | 有 | 移植 |
| BETWEEN / VALUEWHEN | 无 | 有 | 移植 |
| SLOPE / FORCAST | 无 | 有 | 可选（趋势斜率 / 线性外推） |
| MACD/KDJ/RSI/BOLL/WR/BIAS/CCI/OBV | 有，口径一致 | 有 | 不移植 |
| DMI / ADX | 有 | 有（含 ADXR） | 保留现有，**补 ADXR**（已实施，列 `adxr`，未单列维度） |
| ATR | 14 期 | 20 期 | 保留 14（止损更敏感），**增列 atr20**（已实施，列 `atr20`） |
| BBI | **无** | 有 | **纳入**（中期多空分水岭） |
| TRIX / MTM / ROC | 无 | 有 | 纳入（趋势 / 动量背离） |
| VR / MFI | 无 | 有 | 纳入（量价强弱 / 资金强度） |

---

## 三、真正要修的四件事（借原语）

### (a) TD 九转实现不严谨 —— 最高优先级

现状（`indicators()` 内 for 循环）：

```python
buy[i] = (buy[i-1] + 1) if (close[i] < close[i-4] and buy[i-1] > 0) else (1 if close[i] < close[i-4] else 0)
```

缺陷：

1. 到 9 之后**不重置**，会一路数到 10、11…，无法区分"绿9 当日"与"绿9 后第 3 日"
2. 无法回答**"这轮是第几次""距上次绿9 多少天"** —— 而这正是判断"绿9 是否已失效 / 是否该等下一次"的关键
3. **用户反复问的"绿9 之后会不会反弹"，缺的正是这个定量基础**

原语化改法：

```python
cond   = CLOSE < REF(CLOSE, 4)
td_buy = BARSLASTCOUNT(cond)                   # 连续成立天数（严格倒推，自动归零）
td_9   = (td_buy == 9) & ~REF(td_buy == 9, 1)  # 只标记"首次成立 9"那一天
since9 = BARSLAST(td_9)                        # 距上次绿9 的天数
cnt60  = COUNT(td_9, 60)                       # 近 60 日绿9 次数
```

新增输出字段：`td_buy / td_sell / td_since9 / td_since9_sell / td_cnt60`

### (b) MACD 金叉靠肉眼数

现状：`macd_path.py` 靠比对 DIF/DEA 数值大小推断，`macd_stats.py` 逐行统计。

改法：

```python
cross_now = CROSS(DIF, DEA)                        # 今日是否金叉
since_x   = BARSLAST(CROSS(DIF, DEA))              # 距上次金叉天数
below_cnt = COUNT(CROSS(DIF, DEA) & (DIF < 0), 250)# 0 轴下金叉历史次数
```

→ 直接支撑"0 轴下金叉"复盘统计，不再手数。

### (c) 底背离判定太粗

现状：取近 30 日最低点与其之前最低点比 DIF，**没做"低点必须显著"过滤**，也没处理"连续新低"的噪声。

改法：`LLVBARS(S,N)` 精确定位最低点距今天数 + `FILTER` 对低点去重 + `TOPRANGE/LOWRANGE` 判断当前在震荡区间的位置。

### (d) 缺"多空分水岭"

`BBI = (MA3+MA6+MA12+MA24)/4`，抄底潜伏最实用的中期多空线，当前完全没有。同时补 `TRIX`（中长期趋势）、`VR`（量价强弱）、`MFI`（资金强度）。

---

**实施结果（四件事均已落地）**：

| 事项 | 落地形态 | 对应字段 / 函数 |
|---|---|---|
| (a) TD 九转原语化 | 改用 `BARSLASTCOUNT` 严格倒推 + `BARSLAST` 标记"首次=9"，到 9 后能区分"当日"与"第 N 日" | `td_buy / td_sell`（连续计数）、`td_buy9 / td_sell9`（=9 当日）、`td_since9 / td_since9_sell`（距上次 9 的天数）、`td_cnt60 / td_cnt60_sell`（近 60 日 9 的次数） |
| (b) MACD 金叉原语化 | 用 `CROSS` / `BARSLAST` / `COUNT` 产出固定列，上层不再手写比较 | `macd_gc / macd_dc / macd_since_gc / macd_since_dc / macd_gc60` |
| (c) 底背离升级 | 先用 `LLVBARS` 识别摆动低点（左右各 5 根），相邻过近合并为一簇，再比较最近两个低点的"价新低 vs DIF/柱抬高" | `swing_lows()` + `divergence()`（含"柱同步/不同步"成色分档） |
| (d) 多空分水岭与补充维度 | 新增 7 个维度，归入第 3 层做加权 | `bbi / trix+trma / roc12+maroc / vr26 / mfi14 / ar26+br26 / xsii_up·dn·up2·dn2`（另 `adxr`、`atr20` 已算但未单列维度） |

---

## 四、落地形态（推荐 B）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A 整体 vendor | 复制整份 `MyTT.py` 进 `scripts/` | 省事 | 287 行里 90% 用不上；`MA/SUM/MAX/MIN/ABS` 裸名易撞 pandas/numpy；GPL-3.0 传染 |
| **B 按需移植（推荐）** | 在 `stock_lib.py` 新增一节 `# ---- 通达信原语（精简移植自 MyTT, GPL-3.0）`，只搬用得上的 ~20 个原语 + 5 个新指标 | 零新依赖、单文件自洽、命名统一、可按需裁剪 | 上游更新需手动同步（但数学定义稳定，几乎不会变） |
| C 直接 import | `import MyTT as t` | 改动最小 | 新增部署依赖、破坏单文件自洽、命名冲突严重 |

**推荐 B** —— 与 `troubleshooting.md` 已定的纪律一致（「单文件自洽 + 出错可回退」「宁可返回失败也不拿降级数据糊弄」）。

**三条硬约束**：

1. **命名防冲突**：移植函数统一加前缀 `t_`（`t_ref / t_ma / t_ema / t_sma / t_hhv / t_llv / t_sum / t_std / t_cross / t_barslast / t_barslastcount / t_count / t_every / t_exist / t_filter / t_hhvbars / t_llvbars / t_toprange / t_lowrange / t_between / t_valuewhen`，P6 追加 **`t_sar`（取 `TDX_SAR` 通达信口径）**），对外 API 不改名
2. **精度**：MyTT 每个指标都套 `RD(…, 3)`，移植版**默认保留全精度**，仅在输出层 round，避免链条精度损失
3. **许可**：MyTT 为 GPL-3.0。本 skill 为本地个人使用工具、不分发，风险可控，但**必须在文件头保留出处与许可声明**

---

## 五、分阶段实施

| 阶段 | 内容 | 改动文件 | 验证方式 | 状态 |
|---|---|---|---|---|
| **P0** | 原语移植（~20 个 `t_*`，纯数学、无网络） | `stock_lib.py` 新增一节 | 单元自测 + 与通达信/手算对拍 3 组样例 | ✅ **已完成**（本阶段实收 34 个原语；P6 追加 `t_sar` 后共 35 个。`test_mytt_merge.py` 72 项全绿） |
| **P1** | 补 ADXR / BBI / TRIX / ROC / VR / MFI / **XSII / BRAR**，ATR 增列 20 期 | `indicators()` 追加列 | 用宝鼎 002552 跑，**PDI/MDI/ADX 必须与今日报告数值完全一致** | ✅ **已完成**（落位为第 19~25 维：BBI / TRIX+TRMA / ROC+MAROC / VR / MFI / AR·BR / XSII） |
| **P2** | TD 九转重写 + 新增 `td_since9 / td_cnt60` | `indicators()` / `td_chain()` | 用宝鼎"绿5~绿9"那段回测，序列须与现有报告 100% 一致 | ✅ **已完成**（原语化重写，序列已对拍一致） |
| **P3** | 背离升级（`LLVBARS` + `FILTER`）+ MACD 金叉原语化 | `divergence()` | 圣阳股份案例复现 | ✅ **已完成**（`swing_lows` 摆动低点 + `divergence` 成色分档 + `macd_gc/dc/since/gc60`） |
| **P4** | 脚本接入（用原语替换手写循环） | `analyze_stock.py` / `macd_stats.py` | 四脚本回归（注意东财限流，留间隔） | ✅ **已完成**（`analyze_stock.py` 已渲染 `[补充维度]` 区块；`macd_stats.py` 仍可选手写→`macd_gc`，见下） |
| **P5** | 文档同步 | `SKILL.md` / `playbook.md` / `troubleshooting.md` / `report-template.md` / `README.md` | 人工复核 | ✅ **已完成**（`SKILL.md` 新增「顶部与逃顶」专节、`playbook.md` §五 顶部镜像矩阵与止盈体系、`troubleshooting.md` §一~§十、`report-template.md` 模版 E + 例 4、`README.md` 全量对齐）|
| **P6** | **逃顶能力（镜像）**：`t_sar` 移植 + 摆动高点 / 顶背离 / 顶部信号 + 顶部口径历史复盘 | `stock_lib.py` / `analyze_stock.py` / `macd_stats.py` | `t_sar` 与 `TDX_SAR` 对拍零偏差；002552 / 603330 顶部口径实跑 | ✅ **已完成**（`t_sar`(取 `TDX_SAR` 口径) + `swing_highs` / `divergence_top` / `top_signals` + `analyze_stock.py` 渲染 `[顶部信号]` 区块 + `macd_stats.py --side top`）|

**P6 说明**：P0~P5 解决的是"抄底侧口径原语化"，P6 把同一批原语**上下颠倒**用于逃顶 —— 底背离→顶背离（`swing_highs`/`divergence_top`）、绿9/0轴下金叉→红9/0轴上死叉（`--side top`）、止损→**移动止盈线**（`t_sar`）。因此 P6 不新增外部依赖，只在既有原语上补"方向反转"的消费方；`top_signals()` 集中输出客观读数、不产出结论，避免上层各写一套超买线。

**P5 之后的可选清理（不影响可用性）**：

- ~~`macd_stats.py` 的 `collect()` 仍手写 `dif[i] > dea[i] and dif[i-1] <= dea[i-1]` 判金叉~~ → **已完成**：改为直接消费 `macd_gc` 列（对齐 §四 的"不再手写"纪律）。
- ~~`macd_path.py` 自带一份局部 `macd_series()`~~ → **已完成**：改为 `macd_series = L.macd_series`，算法只剩一份；顺带把 `indicators()` 的 KDJ/RSI 与 `weekly()` 的 MACD/KDJ 一并统一到 `t_sma`/`t_ema` 原语。
- ~~`fund_flow()` 逐日明细在东财 `EM_KL` 不可用时仍可能为 `None`~~ → **已降级处理**：实测 `push2his`/`push2` 已是**主机级不可达**（DNS 单 IP、40 次 0/40、8 台变体横扫全挂），且确认此前的"加 `Referer` 就能通"是**偶发窗口假阳性**。因此在缓存兜底之外新增**二级兜底**：用 `push2delay` 那 1 行（同为东财口径）补到缓存尾部，标 `attrs['patched']`；无缓存则只回这一根并标 `attrs['only_today']`。**未采纳**接新浪 `ssl_qsfx_zjlrqs` —— 其口径与东财不同（同号率 53%~72%、5 日累计量级差 2~10 倍），掺进来会污染"连续 N 日净流入"判断；若日后确需，必须作为**独立字段 + 独立标注**接入，不得顶替 `df.flow.main`。

- ~~`holdings_check.py` 自带一份超买标签（原 L56~71：TD红9 / J / CCI / BOLL 上轨）~~ → **已收敛**：超买侧阈值现由 `stock_lib.top_signals()` 唯一定义，`stock_lib.analyze()` 算一次挂在 `res['top']`，`analyze_stock.render()` 与 `holdings_check.tags_for()` **只读不算**（缺失才兜底现算）。`tags_for()` 删除 6 个重复分支，仅保留 `top_signals()` 未覆盖的抄底侧/位置/趋势/量能/资金/筹码项。口径归属已写入 `playbook.md` §七。
- ~~`analyze_stock.py` L129 指标详情行的 `J超买>100 / J超买区>80` 分档仍是**渲染层手写**~~ → **已收敛**：`render()` 开头统一取一次 `tp`（复用 `a['top']`），KDJ 行的超买文案改为判 `tp['tags']` 中是否含对应标签，不再重写 `j > 100 / j > 80`；超卖侧（`J超卖<0` / `J超卖区<20`）不在 `top_signals()` 覆盖范围，留在本地判定。
- ~~`analyze_stock.py` 指标详情行仍有 3 处手写超买线与 `top_signals()` 重复（`WR 超买<20` / `CCI 超买>100` / `MFI 超买>80`）~~ → **已收敛**，并顺带清掉同段同性质的 `VR26 过热>250`、`AR26 人气高>150` 两处，合计 5 处。做法：**判定取自 `tp['tags']`、括号内短标签保留原措辞**（阈值不再本地决定，显示零变化）。至此渲染层已无重复的超买阈值；`top_signals()` 覆盖范围外的侧向（超卖/偏冷/地量/偏弱）仍留本地判定。回归：注入标签可逐条翻转对应文案、移除即回落中性，端到端输出与收敛前逐字一致。

  **说明（未收敛项，属正当保留）**：`analyze_stock.py` 的 `BOLL 现价位于上轨上方` 与 `top_signals()` 的 `冲出BOLL上轨` 条件相同（`close > boll_up`），但前者是四态**位置读数**（上轨上方/上轨-中轨/中轨-下轨/下轨下方），删掉会丢信息，故不视作双写。`RSI: 6日偏强(>60)`、`BIAS: 24日=+x%` 与 `top_signals()` 的 `RSI6超买>80`、`BIAS24正乖离` 阈值不同或无标签，同样不是双写。

**回滚点**：P0~P3 全部是 `stock_lib.py` 内部新增列与新增函数，不动既有列名与函数签名 → 任一步出问题可直接回退，不影响四脚本可用性。

---

## 六、不采纳清单

- MyTT 的 2 级指标函数（与现有一致，纯重复）
- **EXPMA**（与现有 MA5/10/20/60 体系完全重复）
- **MASS**（原著是**抓顶**工具，放进抄底框架属方向错误）
- **ASI**（**验伪**工具，左侧潜伏阶段尚未到该问"突破真假"的时候）
- **TAQ / KTN**（突破系统指标，与抄底方向相反；通道功能已被 BOLL+ATR 覆盖）
- **DFMA**（与 MACD 重复度极高，仅换成简单均线）
- **DPO / EMV / CR / KTN / PSY**（低频噪音或与 BRAR 重复）
- `RD()` 全局 3 位小数包装（改为输出层控制）
- 整份 vendor `MyTT.py`

---

## 附录：其余指标的场景适配评估

**判断准则**：指标本身无好坏，**错配交易系统才是假信号的根源**。这些指标分属三类，与"抄底"关系截然不同：

| 系统类型 | 买点逻辑 | 与抄底的关系 |
|---|---|---|
| 突破 / 趋势跟随（TAQ·KTN·TRIX·DFMA·EXPMA·DPO） | 创新高才买，追确认 | **方向相反**，仅"通道下轨"可反用 |
| 摆动 / 超卖（VR·MFI·EMV·MTM·ROC·BRAR·CR） | 极值回归，逆势 | **同源，直接可用** |
| 顶部反转 / 验伪（MASS·ASI） | 抓顶、验证真假突破 | **反向使用**（低位=无事发生） |

| 指标 | 公式直觉 | 原始设计场景 | 抄底语境下的价值 |
|---|---|---|---|
| TAQ 唐安奇通道 | `HHV(HIGH,20)` / `LLV(LOW,20)` | 海龟交易法：突破 20 日新高买入 | 低（方向相反，与 BOLL 重复） |
| KTN 肯特纳通道 | `EMA(典型价,20) ± 2×ATR(10)` | 波动率通道，比 BOLL 抗异常值 | 中低（下轨击穿收回=超跌，但 BOLL+ATR 已覆盖） |
| **XSII 薛斯通道II** | 内轨 `MA(典型价,5)×1.02/0.98` + 外轨 `(1±7%)×DMA` | **A 股本土"大小通道嵌套"** | **中高**：跌破 TD4 外轨后收回=强超跌；TD2 内轨获支撑=短线底 |
| TRIX 三重指数平滑 | 三次 EMA 后取变化率 | 抓中长期趋势拐点，信号极慢极少 | 中（周线级独立确认） |
| MTM 动量 | `CLOSE - REF(CLOSE,12)` | 动量背离 | 中高（背离**比 MACD 更早**；绝对价差跨股不可比） |
| ROC 变动率 | MTM 的百分比版 | 同上，归一化后可跨股比较 | 中（与 MTM 二选一，取 ROC） |
| DFMA 平行线差 | `MA10 - MA50` + 其 MA10 | 中期趋势方向（DMA） | 低（与 MACD 重复） |
| EXPMA | 两条 `EMA(12/50)` | 裸均线系统 | **零**（纯重复） |
| DPO 区间震荡线 | `CLOSE - REF(MA(C,20),10)` | 去趋势后的周期性残差 | 低（A 股个股周期不稳定） |
| **VR 容量比率** | 26 日涨日量 ÷ 跌日量 | 量价强弱 | **高**：`VR<40` 地量=底部特征；低位回升=资金进场 |
| **MFI 资金流量** | 带量的 RSI（典型价×量） | 资金意愿的超卖 | **高**：`MFI<20` 为资金层面超卖，**比纯 RSI 可信** |
| EMV 简易波动 | 价格位移 ÷ 成交量 | 判断"变盘难易" | 低（小众易钝化） |
| MASS 梅斯线 | 波动幅度的双重均线求和 | **识别牛市顶部反转** | **不采纳**：本质是抓顶工具，方向不符 |
| **BRAR** | AR=多空力量(26日)，BR=情绪(26日) | **人气/情绪温度计** | **中高**：`AR<50`=人气冰点；`BR<AR` 双低位=恐慌出尽。**补现有 18 维的情绪空白** |
| CR 价格动量 | 用中间价算的多空力量 | 与 BRAR 同类 | 低（与 BRAR 二选一，取 BRAR） |
| ASI 振动升降 | 三个价位距离构造的累加 | **验证突破真假** | **不采纳**：左侧潜伏阶段不该问这个问题 |

**两处修正说明**（相对初版判断）：

1. **XSII 不该归为噪音**：它是为 A 股本土设计的"大小通道嵌套"，判底用法明确，是 BOLL 做不到的能力。
2. **BRAR 是现有 18 维的空白**：框架有位置/趋势/动能/量能/资金/筹码，唯独缺"人气温度"。资金流看大单，BRAR 看全市场情绪，是互补而非重复。

**新增维度落位**：

| 维度 | 新增指标 | 补的空白 |
|---|---|---|
| 中期多空 | BBI、ADXR | 分水岭 + 趋势强度延续性 |
| 中长期拐点 | TRIX | 周线级独立确认 |
| 动量背离 | MTM 或 ROC | 比 MACD 更早的背离信号 |
| 量价强弱 | VR、MFI | 地量特征 + 带量超卖 |
| 通道结构 | XSII | 大小通道嵌套 |
| 市场情绪 | BRAR | 人气温度（空白维度） |


---

## 七、风险与边界

1. **限流问题与本方案无关，且已在别处根治**：四脚本连跑时 macd_path 偶发「日K所有数据源均失败」属**数据层**问题，合并 MyTT **不会改善**——它已通过数据层加固解决（缓存分层 + 指数冷却 + 等待预算 + 主机熔断 + 分档限速；四脚本冷缓存 ≈12.4s / 热缓存 ≈9.3s，全部 rc=0，详见 `troubleshooting.md` §九/§十）。P4 回归仍按序执行，但**不必再人为留间隔**。
2. **命名前缀必须严格执行**：否则 `MA/SUM/MAX/MIN` 会覆盖 pandas/numpy 语义，酿成静默错误
3. **TD 九转重写会改变数值**：P2 必须做"新旧序列对拍"，确认与已交付报告一致后才替换
4. **不引入 akshare、不引入 MyTT 为依赖**：保持 `requests pandas numpy` 三项依赖不变

---

## 八、待确认项（均已确认并执行）

1. 是否采纳方案 B（按需移植）？ → **已采纳**，并已实施（P0~P6 完成）。
2. 新增指标是否按附录落位全要（BBI/ADXR/TRIX/ROC/VR/MFI/**XSII**/**BRAR**），还是先只做 **BBI + VR + MFI** 三个最直接的？ → **按附录落位全要**，并额外把 XSII / AR·BR 一并纳入（第 19~25 维）。其中 ADXR 与 ATR20 未单独成维（ADX 已在第 8 维内、ATR 沿用 14 期做止损口径）。
3. TD 九转重写（P2）会改动已交付报告的序列口径，是否接受？ → **已接受**，且重写后序列与旧报告对拍一致，未产生口径漂移。
