# 国家电网 Home Assistant 集成

> **本仓库是 [tiejiang29/state_grid](https://github.com/tiejiang29/state_grid) 的 fork**，版本跟随上游（当前 `0.9.5`）。
> 与上游的差异只有一处：新增 **HA 能源面板可直接引用的电网日用电外部统计**（见下方「本 fork 的差异」）。
> 除此之外的代码、文档、已知限制均与上游一致。

[![GitHub release](https://img.shields.io/github/v/release/tiejiang29/state_grid.svg)](https://github.com/tiejiang29/state_grid/releases)

读取 95598（国家电网）账户的余额、日/月/年用电量与分时拆分。**登录和取数都走国网 App 的接口：没有验证码，不需要大模型，也不需要浏览器。**

取数与解析逻辑沿用 [bilezhou/state_grid](https://github.com/bilezhou/state_grid)（MIT），未改语义。

## 它是怎么工作的

```
App 通道（app_api.py） ──按刷新间隔（默认 12 小时）──▶ 推送缓存 ─▶ refresh_data 解析 ─▶ 实体
```

- App 通道用 SM4/SM2/SM3 信封直连国网 App 网关，设备令牌 `deviceTokenTX` 在本地生成（`turing/`，MIT vendored）——**不经过任何第三方打码或加密代理服务**。
- 会话令牌有效期 15 天，存在 HA 的 `.storage` 里，到期自动重新登录。
- 协调器每 5 分钟一轮，但**取数一天两次就够**：App 通道跟着「刷新间隔」走（默认 12 小时），国网一天内也只会多给出新的一天。每轮先让 App 通道灌一次缓存再决定是否强制刷新，所以刚取到的数据同一轮就被消费。
- **集成不发任何网页请求**：站点升级后网页每个响应都用浏览器里的客户端公钥加密，离线客户端既解不开也没法重放，所以网页那半边不再维护，只剩「上个月抄表」这一格受影响（见下方「已知限制」）。

## 安装与升级

### HACS

1. HACS → 集成 → 探索并添加自定义仓库：`https://github.com/tiejiang29/state_grid`，类别 **集成**
2. 下载 → 重启 Home Assistant

**升级**：HACS → 集成 → 国家电网 → 「重新下载」（有新版本时上面会显示当前版本与可用版本）→ **重启 Home Assistant**。



### 手动

从 [Releases](https://github.com/tiejiang29/state_grid/releases) 下载，把 `custom_components/state_grid/` 整目录覆盖到 HA 配置对应位置，删除 `custom_components/state_grid/__pycache__`，重启 Home Assistant。升级就是同样三步（覆盖 → 清缓存 → 重启）。

> 用 HACS 装的请不要直接改 `custom_components/state_grid/` 里的文件：「重新下载」是整份覆盖，本地改动会丢。

## 配置

**设置 → 设备与服务 → 添加集成 → 国家电网**，只问两件事：**手机号**与**密码**。

添加时集成会立刻做一次 App 登录验证；成功才建条目。密码只在生成 md5 摘要时用一次，明文不进配置项、不进 store、不进日志。

**选项**里可以改：

- **刷新间隔**（12-48 小时）：App 通道多久真实取一次数，也是缓存被消费的闸门。默认 12 小时，也就是一天两次
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
- **"新设备安全验证"（服务端 `resultCode=4006`）无法在 HA 里完成**：这条路线的设备身份是本地合成的（`turing/` 画像 + 现造的 `deviceTokenTX`），服务端第一次见到就可能要短信验证。本集成不做短信、不做扫码，碰上时配置页会直接这么告诉你（不再和"密码错了"混成一句）。自己的账号从 10-03 起没被要求过，所以这是**按账号/按风控**的，不是必然。要避开的只有"换设备画像"这一件事：`.storage/state_grid.app_device` 存的是画像种子，删掉它等于换一台新设备。
- **RK001 是按登录标识计的日额度**（错误码 11401），换 IP、换客户端都没用；被限流时当天不会再成功。

## 日志里能看到什么

集成只往 HA 日志写有用的东西：App 通道每轮入库多少份、命中/未命中哪一格（未命中会带上原因和当时缓存里还剩的键）、中断时的完整回溯。凭证、令牌、密码一律只打长度。

## 本 fork 的差异

上游只把日用电暴露成传感器属性（`recent_30_daily_ele_list`），要接进 HA 能源面板得自己写模板或用 `integration`/`utility_meter` 拼，且历史数据容易断。本 fork 直接把它作为**外部统计（external statistics）**写进 recorder，能源面板里可以直接选到。

- **每个户号一条统计**，`statistic_id` 为 `state_grid:energy_<户号>`，名字是「国家电网 <用电地址> 日用电」，单位 kWh。
- **`sum` 是累计值**（游标法链式累加，截至当日结束），不是当日电量——能源面板按 `sum[n] - sum[n-1]` 反推每日用量，这是它要的语义。
- **数据源**是 `recent_30_daily_ele_list`（滚动 30 天窗口）。导入挂在 coordinator 每次刷新之后，**幂等**：没有新日期就是空操作，**不增加任何 API 调用**。
- **每个日期只导入一次**，已导入的历史不回头修正。历史数据写错了用下面的 reset 服务重建。
- 导入前会做一致性校验：图表里最新一天的日期/电量必须与 `daily_lasted_date` / `daily_ele_num` 对得上，对不上就跳过并打 warning，避免数据错位时把脏数据写进长期统计。
- 游标存在 HA store 的 `state_grid.energy_cursor`。若游标丢了但统计已有数据，会从 recorder 最后一行恢复，不会按基准 0 重写导致历史跳变。
- 统计元数据会按 HA 版本自适应：HA 2025.10+ 用 `mean_type` + `unit_class`，更早的版本用 `has_mean`。

### 重置服务

```yaml
# 全部户号：删除已导入的外部统计并清空游标，下次刷新按基准 0 全量重导入
service: state_grid.reset_energy_statistics

# 单个户号
service: state_grid.reset_energy_statistics
data:
  cons_no: "1234567890"
```

仅当统计删除成功后才重置游标（删除失败就保持原样并报错），否则会出现「游标归零但旧统计还在」导致的历史跳变。

### 能源面板怎么接

**设置 → 仪表盘 → 能源 → 电网 → 添加用电数据**，选「国家电网 <地址> 日用电」。统计是外部统计，不在实体列表里，要在统计选择器里按名字找。

## 致谢

- [bilezhou/state_grid](https://github.com/bilezhou/state_grid) — 取数与解析逻辑来源（MIT，署名见 `custom_components/state_grid/LICENSE.hass-state-grid`）
- [renxiaoyaoo/ha-95598](https://github.com/renxiaoyaoo/ha-95598) 与 [ARC-MX/sgcc_electricity_new](https://github.com/ARC-MX/sgcc_electricity_new) — 接口与验证码链路的逆向参考
- [state_grid_docker](https://github.com/tiejiang29/state_grid_docker) — 同作者的另一个仓库，本集成不依赖它

## 免责声明

本集成只访问您自己账号的用电数据，凭证仅保存在本地 Home Assistant 实例中。国网接口非公开、随时可能变更，届时数据可能中断；使用本项目风险自负。
