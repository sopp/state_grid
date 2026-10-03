# 国家电网 Home Assistant 集成

[![GitHub release](https://img.shields.io/github/v/release/tiejiang29/state_grid.svg)](https://github.com/tiejiang29/state_grid/releases)

读取 95598（国家电网）账户的余额、日/月/年用电量与分时拆分。**登录和取数都走国网 App 的接口：没有验证码，不需要大模型，也不需要浏览器。**

取数与解析逻辑沿用 [bilezhou/state_grid](https://github.com/bilezhou/state_grid)（MIT），未改语义。

## 它是怎么工作的

```
App 通道（app_api.py）  ──每 1 小时──┐
                                     ├─▶ 推送缓存 ─▶ refresh_data 解析 ─▶ 实体
sidecar 浏览器容器 ──webhook 推送──┘   （原版解析逻辑，一行语义没改）
```

- App 通道用 SM4/SM2/SM3 信封直连国网 App 网关，设备令牌 `deviceTokenTX` 在本地生成（`turing/`，MIT vendored）——**不经过任何第三方打码或加密代理服务**。
- 会话令牌有效期 15 天，存在 HA 的 `.storage` 里，到期自动重新登录。
- 协调器每 5 分钟一轮；每轮先让 App 通道灌一次缓存再决定是否强制刷新，所以刚取到的数据同一轮就被消费。
- 网页那份数据（见下方「已知限制」）由另一个仓库 [state_grid_docker](https://github.com/tiejiang29/state_grid_docker) 的浏览器容器抓好后 POST 给本集成的 webhook。**本集成自己不再登网页**：站点升级后每个响应都用浏览器里的客户端公钥加密，离线客户端解不开。

## 安装

### HACS

1. HACS → 集成 → 探索并添加自定义仓库：`https://github.com/tiejiang29/state_grid`，类别 **集成**
2. 下载 → 重启 Home Assistant

### 手动

从 [Releases](https://github.com/tiejiang29/state_grid/releases) 下载，把 `custom_components/state_grid/` 整目录放到 HA 配置对应位置后重启。

## 配置

**设置 → 设备与服务 → 添加集成 → 国家电网**，只问两件事：**手机号**与**密码**。

添加时集成会立刻做一次 App 登录验证；成功才建条目。密码只在生成 md5 摘要时用一次，明文不进配置项、不进 store、不进日志。

**选项**里可以改：

- **刷新间隔**（12-48 小时，控制的是"多久允许走一次真实取数"，App 通道本身每小时灌一次缓存）
- **新密码**：在国网改了密码后填这里，会走 App 通道验证一次才写入；失败保持原值不变

## 传感器

| 传感器 | 说明 | 单位 |
|--------|------|------|
| 账户余额 | 预付费账户为负数表示预存 | 元 |
| 年度累计用电 / 峰 / 谷 / 平 / 尖 | 年度总量与分时 | kWh |
| 年度累计电费 | 年度总电费 | 元 |
| 上个月用电 / 上个月电费 | 上月账单口径 | kWh / 元 |
| 上个月抄表 | 见下方「已知限制」，目前恒为 0 | - |
| 当月累计用电 / 峰 / 谷 / 平 / 尖 | 当月按日累计 | kWh |
| 日总用电 / 峰 / 谷 / 平 / 尖 | 最近一个出数日 | kWh |
| 最近 30 天每日用电、最近 12 个月每月用电 | 属性里的图表数据 | - |
| 最新日用电日期、最近刷新时间 | 数据新鲜度 | - |

## 已知限制（都是实测，不是"预计没问题"）

- **「上个月抄表」恒为 0。** 95598 站点升级后，网页那条抄表接口返回的载荷里已经没有解析器等的 `readList/billRead` 那组键；App 侧也没有对应端点（月度接口只给档位 `levelStdCode/firstRefPq/secondRefPq` 和结算区间，不给示数）。这一格从站点升级起就是 0，与本项目走哪条通道无关。
- **年度分时之和比年度累计电量少约 0.3%**（实测 4.2 kWh / 1647.3 kWh）。日电量的发布口径与月结算电量本身不重合，不是窗口算错。
- **App 会话是单份的**：同一账号在别处重新登录会顶掉 HA 里这份（服务端回 `-201 登录状态已失效`）。生产侧下一轮会自动重新登录，但调试时请记着这点。
- **令牌 15 天到期后的重登是否需要"新设备短信验证"尚未验证**——这是无人值守运行目前唯一没被证实的环节。
- **RK001 是按登录标识计的日额度**（错误码 11401），换 IP、换客户端都没用；被限流时当天不会再成功。

## 日志里能看到什么

集成只往 HA 日志写有用的东西：App 通道每轮入库多少份、命中/未命中哪一格（未命中会带上原因和当时缓存里还剩的键）、中断时的完整回溯。凭证、令牌、密码一律只打长度。

## 致谢

- [bilezhou/state_grid](https://github.com/bilezhou/state_grid) — 取数与解析逻辑来源（MIT，署名见 `custom_components/state_grid/LICENSE.hass-state-grid`）
- [renxiaoyaoo/ha-95598](https://github.com/renxiaoyaoo/ha-95598) 与 [ARC-MX/sgcc_electricity_new](https://github.com/ARC-MX/sgcc_electricity_new) — 接口与验证码链路的逆向参考
- [state_grid_docker](https://github.com/tiejiang29/state_grid_docker) — 同作者的浏览器兜底容器（网页那份数据从这里进）

## 免责声明

本集成只访问您自己账号的用电数据，凭证仅保存在本地 Home Assistant 实例中。国网接口非公开、随时可能变更，届时数据可能中断；使用本项目风险自负。
