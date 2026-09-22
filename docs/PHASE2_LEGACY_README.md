# AutoGrab 0.2 — BandwagonHost Phase 2 checkpoint

当前状态：**PHASE 2 PARTIAL / USER ACTION REQUIRED**。默认 `DRY_RUN + DISARMED`。Phase 1 的商品监控、48 商品基线和配置页 DRY RUN 保留；Phase 2 增加订单意图、恢复协调、限时 ARM、停止开关和邮件配置向导。

**真实订单适配器尚未开放。** 2026-09-22 已在真实匿名站点核实配置 → Cart → Checkout；账户登录、真实 SMTP、登录后不会扣款的提交边界、订单/Invoice 读取与真实付款页仍待验证。`prepare_checkout`、`submit_unpaid_order` 主动拒绝；设置 LIVE 或 gateway 不会解除它们。不能把模拟测试通过理解为已经能抢到付款机会。

## 安装与默认启动

要求 Python 3.12、uv、图形桌面；已在 macOS Apple Silicon 使用。继续复用官方 Playwright、macOS keyring 和 Python 标准库，无新增生产依赖、上游 fork 或第三方抢购脚本。

```sh
./setup.sh
./start.sh
```

`setup.sh` 依据 uv.lock 安装依赖和官方 Chromium；`start.sh` 无参数时持续监控，Ctrl-C 停止。配置文件只接受 `mode = "DRY_RUN"`，运行时 LIVE 必须单独显式请求。默认每 30 秒轮询，允许 30–3600 秒。

```sh
./start.sh baseline
./start.sh monitor --once
./start.sh status
./start.sh history
./start.sh probe
./start.sh control-status
./start.sh preflight --offline
```

首次基线只记录商品，`triggered: 0`。已知商品连续 AVAILABLE 不生成新机会；SOLD_OUT → AVAILABLE 生成下一代 RESTOCK。商品消失或未知库存不当作缺货。所有商品保留在基线，资格沿用 Phase 1 促销判断；价格只记录，无价格、预算或硬件门槛。LIVE 协调器只接受启动后新产生的 eligible NEW_PRODUCT / RESTOCK / PROMOTIONAL_EVENT，不能回放历史事件。公开数据源没有独立活动信号时，不虚构 PROMOTIONAL_EVENT。

## Phase 1 DRY RUN

```sh
./start.sh dry-run --product-id 87
./start.sh simulate bandwagon new-product --product-id 87
./start.sh simulate bandwagon restock --product-id 87
./start.sh validate-live --product-id 87
```

这些旧命令均保留原语义：停止于商品配置页的 Add to Cart 提交之前。历史命名 `validate-live` 指真实网站上的 DRY RUN 验证，不会创建订单。`simulate` 模拟事件来源、访问真实配置页，不改变真实 baseline。Phase 1 的 CART_READY 是配置页完成；Phase 2 另有独立状态机，CART_READY 要求真正 Cart 的证据，两个含义不能混用。

已有配置仅重新读取。上次 VERIFIED 且新页面明确为空购物车时才允许一次新配置。DISPATCHED 或损坏 marker 禁止重放；不同商品遇到非空购物车需人工检查。不得删除 marker 来绕过不确定结果。

## Email Setup

```sh
./configure.sh
# 或直接：
./start.sh configure-email
```

配置向导由本人输入 host、账户、收件人和应用专用密码；密码隐藏输入，只写指定 macOS Keychain 条目。非敏感连接设置存入权限 0600 的 `data/email-settings.json`，不入 Git。只支持 SSL 465 / STARTTLS 587，验证服务端证书；不支持明文或跳过证书校验。已有设置需输入 REPLACE 才会替换。

输入 SEND 后实际发送 `[AUTOGRAB TEST] Email notification working`。结果 `SMTP_ACCEPTED` 表示服务器接受，不能证明收件箱收到。不发送或发送失败时不能进入 ARM。向导不会读取或搜索其他邮箱密码。

兼容原 SMTP_* 变量，并支持 AUTOGRAB_SMTP_HOST、AUTOGRAB_SMTP_USER、AUTOGRAB_SMTP_PASSWORD、AUTOGRAB_EMAIL_TO；原变量优先。不要把真实密码放进源码、配置、脚本或命令历史。程序不自动加载 .env。

未配置邮件仍可监控和 DRY RUN。只有真实 PAYMENT_READY 才能发 `[PAY NOW]` 邮件；拒绝模拟 intent、错误来源和身份不匹配的链接。邮件失败保留订单，不重新下单；通知不自动重复发送。

## Session

```sh
./open-session.sh
```

也可在配置菜单选择“打开搬瓦工专用登录窗口”。由本人在 `profiles/bandwagon/` 专用 Chromium 中登录、完成 2FA 或 CAPTCHA，关闭窗口结束并保存会话。程序不复制其他浏览器 Cookie，不处理密码。后续需重新检查官方账号页、成功响应、登录状态和挑战页面，才能记为 SESSION_VALID。

CAPTCHA/Cloudflare 时保留窗口等待本人处理，不 solver、不绕过保护、不自动重试。当前真实登录尚未验证。

人工 `open-session` 遇到 HTTP 403 时也保留窗口供本人查看，仍不自动重试。普通监控的 403 停止行为不变。Edge 的现有登录不会被导入专用 Chromium；关闭登录窗口本身不证明 SESSION_VALID，仍需后续会话检查。

人工登录使用独立 HumanLoginPolicy，允许官方 `/cdn-cgi/challenge-platform/`、固定 `challenges.cloudflare.com` 验证资源，以及登录页已观察到的 `__cf_chl_rt_tk` 单一查询参数；原自动监控和 Checkout 策略不继承这些权限。不生成/复制验证令牌、不改变浏览器身份、不代做验证码。只加载正常验证所需资源不能保证站点放行，Cloudflare 官方不支持自动化框架处理生产挑战：[支持范围](https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/)。如果仍然被拒，停在人工交接，不循环重试。

## LIVE MODE / ARMING / DISARMING

```sh
./start.sh preflight --test-email
./start.sh arm --mode LIVE --hours 1
# 或 --hours 6 / --hours 24，或 --until 带时区的 ISO 时间
./start.sh disarm
./start.sh stop
./start.sh stop-all
```

ARM 必须同时有 LIVE、BANDWAGON、有效基线、健康数据库/浏览器/站点、当前会话、真实 SMTP 接受证据以及已验证的不会扣款的订单边界。ARM 只存在当前前台进程内，最长 24 小时；使用单调时钟及 UTC 检查，重启/崩溃不恢复授权。购买动作前再次读取开关和新 pre-flight。当前真实 adapter 的边界检查固定未通过，因此以上 ARM 命令会拒绝下单。

`disarm` 阻止新订单提交；`stop` 停止继续监控；`stop-all` 两者同时。信号写入不被购买进程锁阻塞，在等待期间最多约一秒检查一次；已经发出的请求无法撤回，但不会因此重发或取消已有订单。

```sh
./start.sh resume-monitoring
./start.sh clear-disarm
```

清除停止标记必须显式操作；上述命令不会启动进程或自动 ARM。`control-status` 是本次检查的本地状态，不是运行中的跨进程 dashboard。当前没有后台服务、开机启动或 Web dashboard。

## Purchase Intent / Payment Ready

一个 provider/product/event 只建立一个 PurchaseIntent，活动商品另有互斥约束；SQLite 中保存 intent_id、event_id、cart_identifier、order_id、invoice_id、payment_url、提交起止时间与证据。进程锁和单一购买 worker 防止并发提交。

新协调链路为：DETECTED → PRODUCT_VERIFIED → CART_READY → CHECKOUT_READY → **先持久化 ORDER_SUBMITTING** → 唯一一次提交 → ORDER_CREATED → INVOICE_CREATED → PAYMENT_URL_READY → PAYMENT_READY → WAITING_FOR_USER。

PAYMENT_READY 要求实际 order_id + invoice_id，以及相同商家/商品/订单/发票/金额存在/官方付款页/UNPAID 的完整验证证据；URL 字符串、Cart、Checkout、简单 true 标记均不够。真实 intent 只接受 REAL_SITE 证据，模拟源有额外隔离。最终 Pay、卡授权、余额扣款、PayPal Confirm 均没有自动化接口。

邮件优先提供官方 Invoice URL，并说明 Login may be required。当前尚无真实 Invoice，官方链接是否跨浏览器/手机登录后可继续付款仍 UNKNOWN。没有本地桥接；手机邮件里的 127.0.0.1 指手机自身，不能访问 Mac。

## Crash Recovery / Order Reconciliation

```sh
./start.sh intents
./start.sh reconcile
```

崩溃后 ORDER_SUBMITTING 标为需要核对。恢复不需要 ARM，只做读取；发现相同订单时补齐原 intent，禁止重新 POST。FOUND / ABSENT / UNKNOWN 分开处理；即使确证 ABSENT，本版仍暂停，不自动重试。邮件失败、部分 receipt 或读页失败保留已知 ID 和活动占用。取消/过期要有匹配身份的服务器终态证据；不自动取消订单。

当前通用恢复流程已用 Mock 验证；真实 Bandwagon 账户/订单匹配还没有实现，会保守返回 UNKNOWN，不能宣称真实恢复已完成。`intents` 会在本人本地终端显示私有订单信息，不复制进报告。

## Safety Boundary / 当前证据

详细矩阵见 [BandwagonCheckoutBoundary](docs/BandwagonCheckoutBoundary.md)。Phase 1 请求策略保持不变；Phase 2 只增加单次、精确的配置 POST 票据及限定的账号/Checkout 只读入口。所有最终订单 POST 和付款请求仍拒绝，未知页面变化关闭流程。

公开数据优先用专用 persistent context 内同源读取 `https://bandwagonhost.com/order/get-data`，普通 HTTP 403 后不持续重试。浏览器可在同一进程内复用预热；登录态及页面证据仍必须重新验证。没有代理轮换、限流规避、几十个并发浏览器。

Phase 2 的 T0..T9 分别记录检测、验证、浏览器、Cart、Checkout、提交、订单、付款入口、PAYMENT_READY、SMTP 接受。未达阶段为 null。当前通用 provider 的 Cart/Checkout 合并方法在返回后才记录 T3/T4，不能分离精确网页耗时；真实 Phase 2 耗时均未测量，Mock 不作为性能结论。

## 隐私、验证与维护

`data/`、`logs/`、`profiles/`、`artifacts/` 均为本地私有运行目录，不入源码包。SQLite、Cookie、会话、真实邮箱配置不打包。源码没有完整卡号/CVV存储字段。公开结构化日志只保留允许字段，不记录原始异常、私密 HTML 或凭据；日志模式标记区分 DRY_RUN / LIVE。

```sh
./test.sh
uv build
```

保留全部 156 项 Phase 1 测试，追加订单、并发、恢复、ARM、边界和邮件向导回归；普通测试使用 Mock/安全 fixture/本地 Chromium 路由，不连接商家或真实 SMTP。真实验证与离线验证分别记录。Phase 1 commit `b6ee17b` 保留，本轮单独 commit，不 push。

后续只在本人完成 SMTP 和专用浏览器登录后继续确认账户边界。真实 smoke 最多一个受控未付款订单，须先满足全部条件；当前创建订单 0、支付 0。DMIT、VMISS、V.PS、Apple 和 Phase 3 均未开始。
