# Aster / Binance 云主机延迟测试

Ubuntu 24，一条命令运行。无需 API Key，不发单、不撤单；用于筛选策略部署区域。

## 一键运行

首次使用：

```bash
sudo apt-get update && sudo apt-get install -y git python3-venv
```

```bash
git clone https://github.com/wurong98/asterdex-test.git && cd asterdex-test && ./run.sh --label aws-tokyo
```

也可以直接 `./run.sh`，默认用主机名标记结果。

新加坡主机使用 `--label aws-singapore`。已克隆时直接：

```bash
./run.sh --label aws-tokyo --duration 1800
```

首先对 `fapi.binance.com`、`fstream.binance.com`、`fapi.asterdex.com`、`fstream.asterdex.com` 四个域名单独执行 DNS 和最多三次 HTTPS 探测（遭拒绝或限流提前停止），记录连接 IP、TCP（含本次 DNS）和 TLS 耗时、HTTP 状态、CDN 响应头；根路径状态仅用于传输诊断，403/404 不代表有效接口可用。详情也写入 summary.json 的 hosts 字段。

默认两个交易所并行进行 WS 测试，接收 300 秒 BTCUSDT 聚合成交，每 5 秒发送带唯一载荷的协议 Ping，匹配 Pong 测量 WS RTT，并自动回复服务端 Ping。默认约 5 分钟，前置诊断和网络超时会延长。REST 仅在加 `--rest` 时运行（默认 60 次，排除首个请求）。虚拟环境仅创建在项目内；关闭环境代理，测直接公网路径。参数 `--symbol ETHUSDT` 可换币对。

## 结果

`results/UTC时间-区域标签/` 中有：

- `summary.json`：成功样本 P50/P95/P99、错误次数、WS 消息数等。
- `raw.jsonl`：DNS IP、CDN 响应头、首个 REST 请求、每次请求和行情事件记录。

`results/` 已加入 gitignore，不会自动上传机器信息。将各机器的 summary.json 收集到同一目录后：

```bash
python3 scripts/compare.py collected/*/summary.json
```

失败请求不混进成功延迟分位数。403/418/429/451 会停止对应探测，不把拒绝响应当作成功样本。样本数量不足时 P99 参考价值很低，必须同时看样本数和错误。

## 指标能证明什么

- **WS ping RTT**：同一条真实 WS 长连接的 Ping/Pong 往返耗时，输出 P50/P95/P99；测的是 WS 服务端返回控制帧的路径，不能证明到达撮合系统。优先比较这个指标及断线数、实际消息数。未返回匹配 Pong 时样本数为零，不能当成 0ms。
- **raw_event_age**：本地接收时间减行情 `E`，未校正，依赖本机及交易所时钟同步。
- **REST RTT**：有效 `/time` 请求的完整往返时间。排除第一个请求，尝试复用连接；服务器主动断连后的重建也会计入。不能证明请求到达撮合引擎。
- **WS handshake**：DNS、TCP、TLS 和 HTTP Upgrade 的总时间，不是订单延迟。
- **estimated_event_age**：本地接收时间加估计时钟偏移再减行情 `E`。仅启用 `--rest` 后提供；偏移来自 RTT 最小的 `/time` 请求中点，只是近似，受路径不对称、时钟漂移和服务端时钟差异影响；负值保留。NTP 状态也会记录。不能当作精确单向网络延迟。
- **WS interarrival**：相邻行情消息到达间隔，包含市场成交疏密和交易所聚合行为，不能直接称作网络抖动或丢包率。WS 错误次数也不等于行情丢失数量。
- **DNS / CDN POP**：说明公开接入点，不能定位撮合系统。脚本不寻找或绕过 CDN 源站。

## 高频选址怎么做

1. 先按小时短租东京、新加坡的同规格机器，同一云商、相同币对和参数、尽量同步采样。东京是候选区域，不是 Aster 撮合位置结论。
2. 各跑至少 30 分钟，并在不同时段重复，比较有效样本、错误率和尾部延迟。候选区再比较云商和可用区；不要只看最低一次延迟。
3. 公开探测只能初筛。最终接入自己的策略，记录单调时钟：发单前、收到订单确认、收到用户流订单状态、成交回报；按客户端订单 ID 关联。下单确认与成交等待分开统计。
4. 若做币安行情驱动 Aster 交易，比较完整的“币安消息到达 → 策略计算 → Aster 发单 → Aster 确认/用户流回报”。不能仅凭币安 CDN 或行情 RTT 决定位置。
5. 测试下单接口不一定进入真实撮合链路；真实订单测量会产生资金风险，本仓库不会自动执行。向交易所申请低延迟接入信息，确认适用产品和资格后再增加专用端点。

## 已确认与未知

当前环境的一次探测中，Aster REST/WS HTTP 响应显示 CloudFront `NRT20-P4/P6`，币安 REST 显示 `SIN3-P1`。这是东京/新加坡的 CDN 接入点，不是源站或撮合机房证明。Aster 撮合系统具体地理位置尚未获得官方确认；不能将社区对币安的东京说法直接套用到 Aster。

## 官方接口资料

- [Binance WebSocket 连接说明](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect)：本工具使用 `/market/ws/btcusdt@aggTrade`。
- [Aster WebSocket 文档](https://asterdex.github.io/aster-api-website/futures/websocket-market-streams/)：`/ws/btcusdt@aggTrade`。
- [Aster 官方公共行情接口说明](https://github.com/asterdex/aster-skills-hub/blob/main/skills/aster-api-market-data-v3/SKILL.md)：REST `/fapi/v3/time`。
- [Binance 官方低延迟服务](https://www.binance.com/en/support/faq/detail/7df7f3838c3b49e692d175374c3a3283)。

Python 3.12（Ubuntu 24 默认）适用。依赖见 requirements.txt。无需 root 运行测试，仅首次系统依赖安装需要 sudo。

## 实际 WS 入口

默认使用官方文档所述合约聚合成交流：

```text
Binance: wss://fstream.binance.com/market/ws/btcusdt@aggTrade
Aster:   wss://fstream.asterdex.com/ws/btcusdt@aggTrade
```

可用 `--binance-ws-url`、`--aster-ws-url` 覆盖为实际策略使用的公开 aggTrade 流地址（可用 `{symbol}` 占位符）。当前解析器仅统计 aggTrade，其他类型消息不计为有效行情。请勿输入带 listenKey、签名等敏感参数的账户流地址，URL 会写入本地结果。WS 发单接口与行情接口也可能位于不同网关；本工具不测 WS 发单。

## 5～10 秒快速筛选

```bash
./run.sh --fast                              # 默认 8 秒
./run.sh --fast --duration 5 --label aws-tokyo
./run.sh --fast --duration 10 --label aws-singapore
```

两个交易所同时测，跳过四域名前置 HTTPS 诊断及 REST，仅连接实际 WS（仍会正常解析其域名）。每秒一个协议 Ping，显示握手、Ping 样本数/P50/最大值、行情数和错误数。握手计入时限，关闭连接最多额外约 1 秒；首次安装依赖另计。无法连接也会输出错误及空样本。短测用于排除明显慢的机器，小样本 P95/P99 不代表长期稳定性；入围机器再跑长测。
