# AutoGrab

多商家库存监控与结账辅助工具。共用现有 Python Core、SQLite、邮件和正常 Edge 扩展。

**当前为实验性 alpha：默认 DRY_RUN、LIVE OFF、ARM OFF、REAL_ORDER_SMOKE_TEST_ARMED=false。真实订单 0，付款 0。** · [v0.5 Core report](docs/AUTOGRAB_V0_5_PAYMENT_READY_CORE_REPORT.md)

首次扫描建立基线，不把现有商品当新品。只有基线之后的新品、明确缺货后补货、新分类、新购买链接、新年付周期等变化进入机会判断。SPECIAL、PROMO、Annual 只是线索。价格只记录展示，不设价格、预算、性价比过滤。

## 当前真实能力

| 商家 | 已验证内容 | 限制 |
|---|---|---|
| 搬瓦工 L5 | 既有商品→配置→购物车→已登录 Checkout；48个普通商品基线保留 | 本轮未重跑，停在最终下单前 |
| DMIT L5人工演练 | PID 266、$79.90/月；真实购物车刷新后仍保留，已到登录后的Checkout | 停在Complete Order前；扩展仍只读；旧不确定意图保留 |
| VMISS L0部分 | 当前官方商店入口已确认 | 真实页面 Error 1015 临时限流，并非可完成的CAPTCHA；无实际基线 |
| V.PS L4／下单前边界 | 官方66个PID；当前真实订单按钮边界已核实 | Order Now 会创建真实订单，未点击；L4不代表独立Cart或已创建订单 |
| Apple 库存已验证／购买流程实验性 | 动态官方目录、无需SKU的配置向导；CN研究目标自提库存实测通过 | 普通Edge仅识别541、禁用控件及只读页阶段；加入购物袋、精确Bag内容和Checkout均未验证 |

这些状态分别记录，不能将“已接入”理解为“全链路READY”。详见[验收记录](docs/MULTI_PROVIDER_STATUS.md)和[v0.4收口报告](docs/AUTOGRAB_PROVIDER_CLOSURE_REPORT.md)。

v0.5 共用 L6/L7 生命周期及搬瓦工、DMIT 订单适配器：**IMPLEMENTED＝实验性已实现，REAL-SITE VERIFIED＝NO**。不会扣款的提交契约目前仅在离线夹具中成立，尚未获得真实商家页面证据，因此实际提交仍被阻止。此前两家的L5证据冻结保留，没有重复跑真实购物车。

## 安装与运行

需要 Python 3.12–3.14 和 uv；扩展测试另需 Node.js。Native Messaging 安装器目前支持 macOS + Microsoft Edge。

```sh
uv sync --locked --python 3.12
cp config/config.example.toml config/config.toml
./start.sh baseline --provider all
./start.sh monitor --once --provider all
./start.sh status
```

可把 `all` 换为 `bandwagon`、`dmit`、`vmiss`、`vps`、`apple`。正式读取的配置是 `config/config.toml`；根目录 YAML 文件仅作配置说明。Apple 通过当前官方目录选择目标，无需手填SKU：

```sh
./start.sh apple-configure
./start.sh apple-catalog-refresh
./start.sh monitor --once --provider apple
```

向导依次选择地区、商品类别、型号、容量、颜色、适用运营商及自提门店，保存到私有 `config/apple.local.toml`。已有Apple配置保留备份，主配置和SMTP不改写。研究目标不会自动成为你的正式监控偏好。

每个地区/类别的首次完整目录扫描静默建立基线；之后官网新增SKU才产生 `NEW_SKU`。手动添加监控目标不算新品。已确认缺货→新鲜有货，记录 `RESTOCK` 及 `PICKUP_AVAILABLE`；失败、被阻挡、限流、过期数据保持 `UNKNOWN`。预购、开放订购和配送尚无受支持的充分官方证据，保持未验证；Apple Watch组合配置仍属实验性。

普通监控只记录机会并发送已配置邮件。显式加 `--prepare-checkout` 才允许已验证的适配器把新鲜官方机会送到 Edge，仍停在最终订单之前。实验商家的写操作关闭。

## 实验性订单与付款入口

共用现有 PurchaseRunner、PurchaseIntent 和 SQLite，扩展下单前核验、提交标记、结果不确定、订单／账单查询及付款就绪通知。提交前必须确认当前登录、商品、精确价格和周期，并独立证明不会扣款。`Complete Order` 本身不构成这一证明：账户余额或已保存付款方式可能在下单时扣款。当前商家页面尚不满足该契约。

`provider-capabilities`、`order-status` 分别展示实现与真实证据；`order-precheck` 只读取明确指定的现有 Edge 任务页。独立 `order-smoke` 入口还要求显式 LIVE、`REAL_ORDER_SMOKE_TEST_ARMED`、新鲜核验、SMTP证据及最长60秒的单意图授权。普通 ARM 无法授权，显式开启也不能替代商家证据。本轮没有执行真实订单试验。

提交前持久化唯一 nonce。提交后超时、断连或崩溃进入 `ORDER_UNCERTAIN`，恢复时只查找原订单；即使强证据确认不存在，也不自动重提。`PAYMENT_READY` 必须有匹配的订单／账单、未付款状态、精确商品和金额，以及官方 HTTPS 账单页。Cart、Checkout链接不能作为付款就绪证据。通知发送前领取一次性发送权；SMTP结果不确定时不自动重发。

## 请求预算与冷却

`ProviderRateBudget` 将商家、地区、接口、`blocked_until`、连续限流次数及最近结果保存在私有SQLite。支持 `Retry-After` 秒数及HTTP日期，等待不会短于服务器要求；服务器要求更久时优先遵守，也不会缩短本地保守下限。限流／阻断默认冷却15分钟，反复受限时翻倍，默认退避上限24小时。到期只放行一次探测；重启不会清除预算，探测中断也需冷却。

VMISS目录请求最短间隔15分钟，所有商品共用目录请求预算，目录尚未完整时不建立基线；当前仍无可靠真实基线。Apple自提最短间隔60秒，同一门店合并多个SKU并轮换门店组，按地区和接口隔离预算。541、429、网络失败及数据结构变化均保持UNKNOWN。公开监控的到期探测不授权刷新受阻的Edge购买页面，也不授权重试加入购物袋。

## Edge 扩展

```sh
./start.sh edge-install
./start.sh edge-status
./start.sh edge-dry-run --provider bandwagon --product-id 已建立基线的PID
```

在日常 Edge Profile 打开 `edge://extensions`，开启开发人员模式，加载项目 `edge-extension` 文件夹，再打开 AutoGrab 弹窗连接。扩展 ID 应为 `eddoiocaihhammnclkhmmnafjhjilfnc`。使用官方 Native Messaging 标准输入输出，不开本地TCP服务或CDP。

遇到登录、2FA、验证码或Cloudflare，请本人正常处理。通过后恢复原意图，不新建重复尝试。

```sh
./start.sh edge-resume --intent-id 原意图ID
./start.sh edge-cancel --intent-id 原意图ID
./start.sh stop-all
```

Cancel 只停止本地执行，不清空商家购物车。点击结果不确定时不重放。私有数据库保留原基线和意图，不同商家的相同PID不会混用。

## 邮件与论坛信号

`./start.sh configure-email` 设置邮件，密码保存在专用 macOS Keychain 项目；`./start.sh email-test` 检查SMTP接受情况。真实邮箱与密码不进入Git，公开示例统一为 `you@example.com`。

未来真实L7通知标题固定为 **🚨🚨 [PAY NOW] AutoGrab Payment Ready**，包含商家、商品、精确价格／周期、订单／账单ID、发现／订单创建／付款就绪时间及耗时。主入口为可在手机打开的官方商家HTTPS账单链接，并明确可能需要登录。DRY RUN和夹具邮件保留测试标记；真实PAYMENT_READY邮件尚未验收。

NodeSeek 目前为可选的只读 RSS 线索模块。帖子、关键词和购买链接都不能直接触发下单；官方确认必须经过现有商品发现和基线比较。此版未启用论坛到监控的自动调度。

## 边界与开发

不绕过 CAPTCHA、Cloudflare、排队、2FA或其他商家安全措施。不复制Cookie、不导出浏览器Profile、不伪装指纹、不用Playwright自动登录。永不自动最终付款；最终订单创建为独立受控的实验功能，当前真实证据不足仍阻止提交。不得注入夹具标记、开启旧工具或调整付款／余额设置来绕过核验。

```sh
uv run --locked python -m playwright install chromium
./test.sh
node --test edge-extension/tests/*.test.mjs
uv build
```

测试通过仅证明代码约束；不等于当前商家库存、订单或付款已验证。保留旧回归测试，此轮未增加生产依赖。[MIT许可](LICENSE)与[第三方调研记录](THIRD_PARTY_NOTICES.md)。
