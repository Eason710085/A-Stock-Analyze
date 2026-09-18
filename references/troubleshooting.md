# 避坑清单（troubleshooting）

> 这份文件是踩过的坑的完整记录。**改脚本前先读这里**，否则会重复踩。
> 所有网络与指标口径的"唯一真相"在 `scripts/stock_lib.py`，脚本一律通过它取数。

---

## 一、网络与代理（本环境第一大坑）

| 现象 | 原因 | 解法 |
|---|---|---|
| `ProxyError` / `HTTPSConnectionPool ... Max retries exceeded` | 本机设了 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY=http://proxy.nioint.com:8080`，请求被代理拦 | 见下方「标准处理」 |
| `akshare` 全部接口报 ProxyError | akshare 内部自己建 session 且读系统代理 | **本环境不要用 akshare**，直接裸 `requests` 打东财/腾讯 |
| 偶发成功、偶发失败 | 代理间歇性可用 | 固定去代理，不要依赖运气 |

**标准处理（`stock_lib.py` 已在导入时完成）**

```python
for _k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
    os.environ.pop(_k, None)
_S = requests.Session()
_S.trust_env = False        # 关键：忽略环境里的代理与 .netrc
```

自查命令：

```bash
env | grep -i proxy          # 看代理是否被设置
python3 -c "import os;[os.environ.pop(k,None) for k in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY')];import requests;print(requests.get('https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&secids=0.002552&fields=f12,f14,f2',timeout=8).status_code)"
```

---

## 二、东财 K 线接口的参数陷阱（第二号坑）

**必须遵守的参数组合**：

```
host : push2his.eastmoney.com  (回退 push2.eastmoney.com / push2delay.eastmoney.com)
path : /api/qt/stock/kline/get
必带 : secid=<0|1>.<code>  klt=101(日)  fqt=1(前复权)
       beg=0  end=20500101  lmt=<1600|800|500 轮转>
       ut=b2884a393a59ad64002292a3e90d46a5
       fields1=f1,f2,f3,f4,f5,f6
       fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61
Header: User-Agent(浏览器串) + Referer: https://quote.eastmoney.com/
```

| 错误写法 | 后果 |
|---|---|
| `beg=20220101&end=20260918` | 连接被断：`RemoteDisconnected` |
| `end` 写具体当天日期 | 同上，盘中还可能少一根 |
| `lmt` 过大（如 5000） | 被限流 / 超时 |
| 不带 UA / Referer | 返回空或 403 |
| 用 `requests.get` 默认超时（无 timeout） | 卡死，无法回退到下一源 |

**`fqt` 的含义**：`1` = 前复权（算指标用）、`0` = 不复权（**算筹码必须用这个**，因为要配真实换手率与真实价格区间）。

**`klines` 每行格式**：`日期,开,收,高,低,成交量,成交额,振幅,涨跌幅,涨跌额,换手率`。

### 二·补、三台主机的分工，以及 `push2delay` 的「假成功」陷阱

东财这三台主机**能力不同**，不能当成等价备份：

| 主机 | 能做什么 | 不能做什么 |
|---|---|---|
| `push2his` | **历史类**（日K、逐日资金流）+ 快照 | 会整个域名在连接层被拒（`RemoteDisconnected`） |
| `push2` | **快照类**（`ulist.np`、`clist`）+ 历史类 | 同上，且死得更频繁 |
| `push2delay` | 快照类（**唯一常年存活的一台**） | **历史类只回空/残响应**：K线回 `data:null` 或 1 行、`fflow` 回 1 行 |

**这就是"假成功"的来源**：`push2delay` 对历史接口返回 **HTTP 200 + `{"rc":0,...}` 但没有数据**。如果只看状态码、只看 `t.startswith('{')`，就会把"空壳响应"当成成功，然后 `data.klines` 取不到 → 上层报"日K获取失败"，而实际上**问题在于没有校验响应体**。

**解法（已实现在 `stock_lib._get`）**：所有历史类接口都必须传响应校验器 `ok=_rows_ok(n)`：

```python
def _rows_ok(min_rows, key='klines'):
    """校验响应里 klines（或 diff）至少 min_rows 行；不通过则视为失败，继续轮转下一台主机。"""
```

| 接口 | 校验 | 说明 |
|---|---|---|
| 日K `kline/get` | `_rows_ok(2)` | 拒绝 `push2delay` 的 0~1 行残响应 |
| 逐日资金流 `fflow/daykline/get` | `_rows_ok(2)` | 同上；这也是它比其他接口更常失败的原因 |
| 快照 `ulist.np/get` | 无（`ok=None`） | 快照类 `push2delay` 是**真能干活**的，1 行就是正常结果 |

**经验法则**：新增任何**历史类**接口时，**必须**传 `ok=_rows_ok(...)`；新增**快照类**接口时不要传，否则 `push2delay` 的正常结果会被误判为失败。

---

## 三、多源回退链与各自的坑

**回退顺序**：东财（首选，盘中含当日）→ 腾讯（兜底）→ **过期缓存**（最后兜底，标注 `[过期缓存,可能滞后]`）。

| 项 | 东财 | 腾讯 | 过期缓存 |
|---|---|---|---|
| 接口 | `push2his.../kline/get` | `web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz002580,day,,,1600,qfq` | 本地 `scripts/.cache/kline_<code>_<klt>_<fqt>.json` |
| 数据路径 | `data.klines`（字符串数组） | `data.<mkt><code>.qfqday`（数组的数组） | 同东财 |
| 每行 | `[日期,开,收,高,低,量,...]` 含**换手率** | `[日期,开,收,高,低,量]` **无换手率** | 含换手率（若是东财来源） |
| 盘中当日 | 有 | 收盘后才有 | 取决于写入时刻 |
| 涨跌幅 | 接口直接给 | **无，必须自算** | 有 |
| 筹码 | 可算 | **不可算**（缺换手率） | 可算（需 `fqt=0` 且东财来源） |
| 新鲜度标注 | 无 | 无 | `[过期缓存,可能滞后]` |

**缓存分层**：`CACHE_TTL=600s`（东财/腾讯正常新鲜期）、`CACHE_TTL_WEAK=60s`（腾讯降级源的短新鲜期——因为腾讯盘中数据不全，不该拿它顶太久）。`kline()` 的取值顺序是：**新鲜缓存 → 东财 → 腾讯 → 过期缓存**，任何一级命中即返回并标注来源。

**重要行为**：日 K 走腾讯兜底时，`analyze_stock.py` 会打印
`数据源:腾讯前复权(无换手率→筹码不可算)`，但**筹码区块仍可能正常**——
因为 `chips()` 是**单独**用东财 `fqt=0` 复权日 K + 换手率算的。
即「日 K 降级 ≠ 筹码降级」，两者独立，报告中不要误判。

**腾讯源注意**：
- 只保留最近 1600 根左右，足够（筹码只需 210 根）
- 停牌日不返回，指数与个股结构一致

---

## 四、筹码分布：没有服务端接口，必须本地复刻

**结论：东财 `/api/qt/stock/cyq/get` 在 `push2 / push2his / push2delay` 三个 host 上全部 404。**
筹码是**前端 JS 算的**，只能本地复刻。

**复刻要点（已实现在 `stock_lib._cyq_once`）**

1. 取**最近 210 根不复权日 K**（`fqt=0`），必须带 `hsl`（换手率）。
2. 价格轴离散化为 `factor=150` 档：`accuracy = max(0.01, (maxp-minp)/(factor-1))`。
3. 逐根处理（**时间正序**）：
   - 每根先对**全数组**按换手率衰减：`xdata[n] *= (1 - min(1, hsl/100))`；
   - 再把该根的 `100%` 筹码按**三角分布**叠加到 `[low, high]`（`h == l` 时退化为单点）。
4. 统计输出：
   - `benefit_ratio` = 现价以下的筹码占比，**是 0~1 的小数**（报告里乘 100 显示为百分比）；
   - `avg_cost` = 累计 50% 处的价格；
   - `c90` / `c70` = 累计 5%~95% / 15%~85% 的价格区间。

| 坑 | 说明 |
|---|---|
| `benefit_ratio` 口径 | 是**小数**不是百分数。历史上曾因口径误读出现「获利比例 0.017」被当成 1.7%，实为 1.7% 是对的、但要确认乘 100 后展示 |
| 用前复权 K 线算筹码 | **错**。前复权价格区间被改写，必须用 `fqt=0` |
| 缺换手率（腾讯源） | 筹码直接返回 `None`，报告写「筹码不可算」，不要硬凑 |
| 停牌/新股 | 210 根窗口不足或含大量无换手日，结果参考性弱，需在报告中提示 |
| 耗时 | 三角分布是 O(210×150) 纯 Python 循环，**单只约 0.3~1 秒**，属正常 |

---

## 五、实时快照与资金流

**实时快照**：`push2.eastmoney.com/api/qt/ulist.np/get`
必带 `fltt=2&invt=2&secids=0.002552&ut=...`（`fltt=2` 才会返回已缩放的小数，否则价格是整数放大 100 倍）。

关键字段：

| 字段 | 含义 |
|---|---|
| f12 / f14 | 代码 / 名称 |
| f2 / f3 | 最新价 / 涨跌幅% |
| f8 / f10 | 换手率 / 量比 |
| f9 / f23 | PE / PB |
| f20 / f21 | 总市值 / 流通市值 |
| **f62 / f184** | 当日主力净额 / 主力净占比 |
| **f164 / f174** | 5 日主力净额 / 10 日主力净额 |

**逐日资金流**：`<host>/api/qt/stock/fflow/daykline/get?lmt=0&klt=101&secid=...&fields1=f1,f2,f3,f7&fields2=f51,...,f65&ut=...`
返回 `data.klines`，每行 `日期,主力,小单,中单,大单,超大单,主力占比,...`。

同一 path 打到不同 host，**回的行字段数不一样**（容易把列读错位）：

| host | `daykline/get` | `kline/get` |
|---|---|---|
| `push2his` / `push2` | 15 字段，`p[6]` = 主力占比 | 6 字段（无占比） |
| `push2delay` | 15 字段，但**只回 1 行** | 6 字段，只回 1 行 |

所以解析必须写 `n2f(p[6]) if len(p) > 6 else nan`，不能用固定下标硬取。

| 坑 | 说明 |
|---|---|
| ETF 无资金流 | 对 ETF 请求返回空 → 用 `--no-flow` 跳过，报告写「ETF 不适用」 |
| **接口本身最不稳** | `fflow/daykline` 是"逐日类"里最容易被回残响应的一个。因此它 `tries=3 ok=_rows_ok(2) budget=18s`，并且**落盘缓存**（`fflow_<code>.json`，全量存、读取时截尾）→ 连续跑脚本不会重复打这个不稳接口 |
| **实测已到主机级不可达** | 不只是限流：`push2his` / `push2` 的 DNS 曾只解析出单个 IP，连续 40 次 **0/40** 成功；横扫 `1./2./7./82.` 前缀变体亦全部 `ConnectionError`。**这不是 path 问题、也不是 `Referer` 问题**（曾有一轮"加 `Referer` 15/15 成功"，经多轮长跑复核证实是偶发窗口的假阳性，与 `Referer` 无因果）。诊断这类问题必须**先多轮长跑 + 多主机横扫**，不要单轮定论 |
| 冷却期内不打它 | `fund_flow()` 里 `if _cooldown_left() <= 0:` —— 冷却期直接跳到二级兜底，不再去撞 |
| **二级兜底：延迟源补"最新一根"** | 历史全挂时，用 `push2delay` 的 1 行（口径仍是东财）接到缓存序列尾部。返回的 DataFrame 带 `attrs['patched']='MM-DD'`，渲染层显示 `[东财历史接口不可达, 最新1日(MM-DD)由延迟源补齐]`；这样 10 日表不会永远停在昨天。**该结果刻意不写回缓存**——盘中那一根可能不完整，固化后会让后续 10 分钟的报告都显示偏小的当日净额 |
| **连缓存都没有时** | 只返回那一根，带 `attrs['only_today']=True`，渲染层显示 `[仅取到最近1日]` 并**停出近 5/10 日合计**（1 行算不出合计，硬算会误导），改为指向实时快照的 5/10 日净额 |
| **取到过期缓存时** | 返回的 DataFrame 带 `attrs['stale']=True`。**渲染层必须显示降级提示**（`analyze_stock.py` / `macd_path.py` 已处理），报告的"数据截至"要标注，不要把陈旧资金流当当日数据。另：延迟源那一根的日期若不晚于缓存末日，也归入 `stale`，不重复补 |
| **渲染层三标注** | `analyze_stock.py` 的 `[主力资金流]` 与 `macd_path.py` 的 `[资金配合度]` 都按同一规则分支：`stale` → 「本次未取到, 以下为上次成功缓存, 可能滞后」；`patched` → 「最新1日(MM-DD)由延迟源补齐」；`only_today` → 「逐日序列不足，近5/10日合计暂不输出」。**任何新写/改写的消费方都必须照抄这套分支**，否则单行会被算成"近5日=近10日"的假象（`macd_path.py` 曾有此漏，已修） |
| **`holdings_check.py` 不走逐日** | 它横向表里的「主力5日」直接取实时快照的 `f164/f174`，不受本接口影响。所以"资金流挂了"时它仍能出数——这也意味着它的数字与 `analyze_stock` 的逐日合计**口径不同**（快照 vs 逐日累加），不要互相校验 |
| 单位 | 接口返回**元**，展示时用 `_money()` 折成万/亿 |
| 背离判读 | 当日净额 <0 而 5 日净额 >0 → **必须标注为背离**，而非取平均 |
| 兜底来源 | 快照里的 `f62/f184/f164/f174`（当日/5日/10日主力净额）即使逐日接口全挂也能拿到，是资金流维度的**最后一道防线**；但它给不出"逐日序列"，无法做"连续 N 日净流入"判断，报告里要说明 |

---

## 六、代码解析与板块

- 名称 → 代码：东财搜索 `/api/suggest/get?type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=5&input=<名称>`，取 `QuotationCodeTable.Data` 里 `Classify` 为 A 股/ETF 的记录。
- `secid` 前缀：**沪市（6 开头）/ 科创（68）/ 沪 ETF（5 开头）= `1.`；深市（0/3 开头）/ 深 ETF（15/16/159）= `0.`**。
- 多只同名 → 让用户给代码，不要猜。
- **北交所（4/8 开头）**：指标有效性弱，结论须加提示。
- **新股（上市 <60 日）**：均线/位置/筹码全不可靠，不要给四档结论。
- **ETF**：跳过资金流与筹码，18 维里只保留价格/指标/均线/BOLL/TD 等。

---

## 七、常见报错速查表

| 报错 / 现象 | 根因 | 处理 |
|---|---|---|
| `[失败] xxx 日K获取失败: ['日K:日K所有数据源均失败']` | **四级都拿不到**（东财→腾讯→过期缓存全空）。若是首次运行=真网络故障；若是老票反复出现=检查 `scripts/.cache` 是否被清空 | **先别再重跑**，按 §八 三步排查（网络 → 数据层 → 上层）。注意：单纯的东财限流**不会再报这个错**，会自动降级到腾讯并标注来源 |
| 报告里出现 `[过期缓存,可能滞后]` | 东财与腾讯都不可用，走了最后一级缓存兜底 | **正常降级**，不是报错。在报告"数据截至"里标注即可；如需最新数据，等一两分钟后重跑 |
| 报告里出现 `腾讯前复权(无换手率→筹码不可算)` | 东财 K 线不可用，走了腾讯兜底 | 正常降级。**筹码是否可用取决于 `fqt=0` 那一路**（独立取数）；若筹码也显示 `-`，说明东财历史接口整段不可用，见下一行 |
| `获利盘 -` / 筹码显示 `None` | 筹码必须用东财 `fqt=0` kline（要有换手率），腾讯源算不了 | 检查是否处于**主机熔断窗口**内（`scripts/.cache/_hostdown.json`）。东财恢复后自动可用，**不要用手算或估算的获利比例代替** |
| `ProxyError` | 系统代理未清除 | 确认 `trust_env=False` 且已 `pop` 代理变量 |
| `RemoteDisconnected` | ① `beg`/`end` 用了具体日期 ② 该台主机在连接层被拒（正常现象，会自动轮转+熔断） | ① 改 `beg=0&end=20500101` ② 无需处理，熔断会跳过它 |
| `json.decoder.JSONDecodeError` | 命中拦截页 / 空响应 | 检查 UA 与 Referer；换 host 重试；历史类接口确认传了 `ok=_rows_ok(...)` |
| 价格出现 5432 这种整数 | 少了 `fltt=2` | 加 `fltt=2&invt=2` |
| 筹码返回 `None` | 日 K 走了腾讯源（无换手率） | 换时段重跑东财；或在报告中标注不可算 |
| 停牌股当日无实时价 | 快照返回空 | 用最后一根日 K 收盘价代替并注明 |
| 图表中文变方块 | 字体缺失 | 显式指定 `Noto Sans CJK SC` / `WenQuanYi Micro Hei` |
| `ModuleNotFoundError: stock_lib` | 未从脚本目录运行 | 脚本已内置 `sys.path.insert(0, 脚本目录)`；若自建脚本需照做 |
| 输出里出现 `-` 或空白 | 该字段 NaN | 正常降级显示，非报错 |
| **整个脚本跑得很慢（>30s）** | 大概率是**限速/冷却闸门在等待**，不是卡死 | 看 `scripts/.cache/_cooldown` 与 `_hostdown.json`。若 `_cooldown` 存在且值很新，说明东财整体在限流窗口内；脚本正在按预算等待或降级。**不要 Ctrl-C 反复重跑**，那会把冷却计数继续往上推 |

---

## 八、标准排查流程

```
1. 先验网络：跑 §一 的一行自检命令
   → 若 push2 报 RemoteDisconnected 但 push2delay 通，属当前环境的常态（见 §二·补），不算故障
2. 再验数据层：python3 stock_lib.py 002552
   → 看「错误 []」是否为空、根数是否 600+、`[日线]` 标的是哪个源、筹码是否非 None
3. 若数据层 OK 但上层脚本失败 → 极罕见（上下层已解耦）；查上层脚本参数与 sys.path
4. 若数据层失败 → 按报错定位：
   - 标注「腾讯」/「过期缓存」→ 已自动降级，属正常，报告里如实标注即可
   - 「日K所有数据源均失败」→ 连缓存都没有：网络/代理问题，回第 1 步
   - 只缺筹码 → 东财 fqt=0 那一路不可用，见 §七 对应行
5. 修复后回归：四个脚本连跑一遍（应全部 rc=0）
   python3 scripts/holdings_check.py 002552:51.2 603330:9.8 && \
   python3 scripts/analyze_stock.py 002552 && \
   python3 scripts/macd_stats.py 002552 && \
   python3 scripts/macd_path.py 002552
   → 冷缓存应 ≈12s，热缓存应 ≈9s（实测基准，见 §十）
```

---

## 九、改动纪律（重要）

1. **数据口径只改 `stock_lib.py`**。`analyze_stock.py` / `holdings_check.py` / `macd_path.py` / `macd_stats.py` 只做渲染与判定，禁止在其中重复实现抓数与指标。
2. 新增数据源时，**必须同时实现回退与降级标注**（报告要能看出用的是哪个源）。
3. 新增指标时，**先确认与行情软件默认口径一致**（如 MACD 用 `ewm(span, adjust=False)`，KDJ 用 9,3,3 的 SMA 平滑），否则跨工具对不上数。**移植 MyTT/通达信原语时另有一条硬约束：入参是 `pd.Series` 就必须返回同索引的 `Series`**（MyTT 原版返回 ndarray），否则上层做 `Series` 对齐时会静默错位——这类 bug 不报错、只出错误结论，最难查。
4. 任何"拿不到数据"的情况，**一律降级标注，不臆造**。
5. 改完必须跑 §八 第 5 步的回归。
6. **新增网络接口时，必须按类型配置韧性参数**（最容易被漏掉的一条）。三条规则：
   - **历史类**（日K、逐日资金流）→ 必须传 `ok=_rows_ok(n)` 做响应校验，必须给 `budget=`，限速走默认 `MIN_INTERVAL=3s`。
   - **轻量快照类**（实时行情、指数、涨跌家数、代码检索）→ 传 `min_interval=LIGHT_INTERVAL`（1s）、`wait_cd=False`，**不要**传 `ok`（它们本来就只回一两行）。
   - **任何接口都不要把 `budget` 留成 `None`**。无预算 = 可能把整段睡眠花在冷却上，详见第 7 条。
   `wait_cd=False` 只在"这个数据值得硬试一次、但不值得等冷却"时用；需要等冷却才能连续拉取的（`kline` / `fund_flow`）保持默认 `wait_cd=True`。
7. **不要为了让单次请求"更稳"而调大 `tries` / `budget` / `lmt`**。历史教训：这几个值越大，一旦撞进冷却，白白烧掉的时间越多——实测把 `fund_flow` 的 18s 预算留给"睡冷却"，单只分析从 15s 涨到 66s，而数据一条没多拿到。正确做法是让熔断与冷却尽快生效、快速降级。

---

## 十、数据层韧性机制的现场诊断

### 1. 四个闸门状态文件（都在 `scripts/` 下，`rm` 掉即"放闸"）

| 文件 | 含义 | 看什么 |
|---|---|---|
| `_lastreq` | 上次请求的时间戳 | 唯一时间戳很小/为 0 = 限速未生效（不该发生） |
| `_cooldown` | 限流冷却的到期时间戳 | 明显大于当前时间 = 正在冷却（最长 120s，闲置 180s 后自动归零） |
| `_cdcnt` | 连击计数（决定冷却时长 20→40→80→120s） | ≥2 说明近期反复撞限流，此时慢是"对的行为" |
| `_hostdown.json` | 各主机的熔断到期时间 | `push2his` / `push2` 在里面 = 已被判定连接层不可用，正在跳过 |

读法（一行）：
```bash
python3 -c "import time,os,json;d='scripts';now=time.time();\
print('now',now);\
print('lastreq',open(os.path.join(d,'_lastreq')).read().strip() if os.path.exists(os.path.join(d,'_lastreq')) else '-');\
print('cooldown',open(os.path.join(d,'_cooldown')).read().strip() if os.path.exists(os.path.join(d,'_cooldown')) else '-');\
print('cdcnt',open(os.path.join(d,'_cdcnt')).read().strip() if os.path.exists(os.path.join(d,'_cdcnt')) else '-');\
print('hostdown',json.load(open(os.path.join(d,'_hostdown.json'))) if os.path.exists(os.path.join(d,'_hostdown.json')) else '-')"
```

### 2. 两条"清闸门"命令

```bash
# 只清闸门（保留缓存）：怀疑熔断/冷却误判时用
rm -f scripts/_lastreq scripts/_cooldown scripts/_cdcnt scripts/_hostdown.json

# 全冷（清闸门 + 清缓存）：要复现冷启动基准时用
rm -rf scripts/.cache && rm -f scripts/_lastreq scripts/_cooldown scripts/_cdcnt scripts/_hostdown.json
```

### 3. 实测基准（本机、单只/四脚本）

| 场景 | 命令 | 应耗时 |
|---|---|---|
| 单只全冷 | `rm -rf scripts/.cache && time python3 scripts/holdings_check.py 002552:51.2 603330:9.8` | **≈8.0s** |
| 单只带探针 | `time python3 probe_holdings.py`（见下） | ≈5.3s |
| 四脚本冷缓存 | §八 第 5 步的连跑命令 | **≈12.4s** |
| 四脚本热缓存 | 同上（不删缓存） | **≈9.3s** |

**判定标准：冷启动 > 20s、或热跑 > 15s，即视为异常**，按本节的闸门文件排查（大概率是 `_cdcnt` 偏高或某台主机反复被撞）。

### 4. 附：探针脚本

`probe_holdings.py` 用 `runpy` 跑 `holdings_check.py`，同时 patch 掉 `_S.get`，逐条打印 `(相对时间, 耗时, 主机, OK/ERR)`，末尾输出 `total wall` 与 `cd_left_end`（结束时剩余冷却）。它不改数据层代码、只做观测，适合定位"到底卡在哪一次请求"。同目录另有 `probe_timeline.py`（打印每步时间线）与 `probe_cold.py`（冷启动专用）。

### 5. 备用源备忘（已探明、未接线）

新浪逐日资金流可用，作为东财全挂时的第三级降级候选：
```
https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=60&sort=opendate&asc=0&daima=sh603330
```
当前**尚未接入 `fund_flow()`**；若日后东财 `EM_KL` 长期不可用、需要补逐日资金流，从这里接。
