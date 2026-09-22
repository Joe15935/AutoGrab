# AutoGrab

多商家库存监控与结账辅助工具。共用现有 Python Core、SQLite、邮件和正常 Edge 扩展。

**当前为实验性 alpha：默认 DRY_RUN、LIVE OFF、ARM OFF。不会提交最终订单或自动付款。**

首次扫描建立基线，不把现有商品当新品。只有基线之后的新品、明确缺货后补货、新分类、新购买链接、新年付周期等变化进入机会判断。SPECIAL、PROMO、Annual 只是线索。价格只记录展示，不设价格、预算、性价比过滤。

## 当前真实能力

| 商家 | 已验证内容 | 限制 |
|---|---|---|
| 搬瓦工 L5 | 既有商品→配置→购物车→已登录 Checkout；48个普通商品基线保留 | 本轮未重跑，停在最终下单前 |
| DMIT L5人工演练 | PID 266、$79.90/月；真实购物车刷新后仍保留，已到登录后的Checkout | 停在Complete Order前；扩展仍只读；旧不确定意图保留 |
| VMISS L0部分 | 当前官方商店入口已确认 | 真实页面 Error 1015 临时限流，并非可完成的CAPTCHA；无实际基线 |
| V.PS L3 | 官方66个PID；当前真实订单按钮边界已核实 | 一步式按钮会创建真实订单，保持禁用；独立Cart/Checkout未验证 |
| Apple L3 | 动态官方目录、无需SKU的配置向导；CN研究目标自提库存实测通过 | 商品页配送查询541，加入购物袋禁用；Bag/Checkout、配送及预购未验证 |

这些状态分别记录，不能将“已接入”理解为“全链路READY”。详见[验收记录](docs/MULTI_PROVIDER_STATUS.md)和[v0.4收口报告](docs/AUTOGRAB_PROVIDER_CLOSURE_REPORT.md)。

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

NodeSeek 目前为可选的只读 RSS 线索模块。帖子、关键词和购买链接都不能直接触发下单；官方确认必须经过现有商品发现和基线比较。此版未启用论坛到监控的自动调度。

## 边界与开发

不绕过 CAPTCHA、Cloudflare、排队、2FA或其他商家安全措施。不复制Cookie、不导出浏览器Profile、不伪装指纹、不用Playwright自动登录。不自动最终付款；本alpha也不创建最终订单。

```sh
uv run --locked python -m playwright install chromium
./test.sh
node --test edge-extension/tests/*.test.mjs
uv build
```

测试通过仅证明代码约束；不等于当前商家库存、订单或付款已验证。保留旧回归测试，此轮未增加生产依赖。[MIT许可](LICENSE)与[第三方调研记录](THIRD_PARTY_NOTICES.md)。
