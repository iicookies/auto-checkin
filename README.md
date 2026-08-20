# Auto Checkin — WorkBuddy + Trae 每日自动签到

每天自动领取 **WorkBuddy（腾讯 CodeBuddy 国内版）** 和 **Trae（字节 Trae Work）** 的每日积分。

- 零第三方依赖，纯 Python 标准库（`urllib` + `http.server`）
- OAuth 浏览器登录一次拿 token，之后自动刷新、自动签到
- Windows 任务计划程序定时执行，token 不上云

> ⚠️ 这是第三方逆向脚本，与官方无关，可能违反服务条款，接口随时可能失效。请自行评估风险后使用。

## 文件结构

```
auto-checkin/
├── checkin.py        # 主签到脚本：读凭证、刷新 token、调签到接口、查积分
├── login.py          # 首次登录脚本：OAuth 浏览器登录，拿 token 存盘
├── setup_task.ps1    # 注册 / 卸载 Windows 任务计划
├── README.md         # 本文件
├── auths/            # 登录后自动生成，存放各账号凭证（请勿外传）
│   ├── workbuddy-<uid>.json
│   └── trae-<name>.json
└── logs/             # 运行日志，自动生成
    └── checkin.log
```

## 环境要求

- Python 3.10+（`http.server` 回调、`urllib` 都在标准库里，无需 pip install）
- Windows 系统（定时任务用 PowerShell 脚本注册；签到脚本本身跨平台）

## 前置条件

运行脚本前，确认以下条件已满足：

**账号准备**

- 已注册 **腾讯 CodeBuddy（国内版）** 账号，能正常登录 [codebuddy.cn](https://www.codebuddy.cn)。注意：国际版 WorkBuddy（workbuddy.ai）无每日签到功能，本脚本只支持国内版。
- 已注册 **Trae（国内版 trae.cn）** 账号，能正常登录。本脚本只支持国内版，海外版（trae.ai）账号体系不同。

**系统与网络**

- Python 已加入 PATH，命令行执行 `python --version` 能正确输出版本号
- 运行 `login.py` 登录时，电脑能正常打开浏览器，且本机 `127.0.0.1` 回调端口（WorkBuddy 无需，Trae 用 18080）未被占用
- 能访问 `codebuddy.cn`、`copilot.tencent.com`、`trae.cn`、`api.trae.cn`、`api.trae.com.cn` 这些域名（国内网络环境通常没问题）

**首次登录必须先做**

- `checkin.py` 依赖 `auths/` 目录下的凭证文件，首次使用必须先跑 `python login.py workbuddy` 和 `python login.py trae` 完成登录，否则签到会提示"未找到任何凭证"
- 登录时需要你本人在浏览器里扫码 / 验证码完成认证（脚本无法自动登录，这是平台安全要求）

**定时任务额外条件**

- 注册定时任务需以**管理员身份**运行 PowerShell（普通用户运行 `setup_task.ps1 -Install` 会因权限不足失败）
- 定时执行时电脑需处于开机状态（已配置开机后补跑，但当天若一直关机则跳过）
- 注册任务的用户必须与执行 `login.py` 登录的是同一用户，否则读不到 `auths/` 下的凭证

命令行确认：

```bash
python --version
```

## 快速开始

### 1. 首次登录（两个产品各登录一次）

分别执行，会自动打开浏览器，你扫码 / 验证码完成登录，token 自动存到 `auths/`：

```bash
python login.py workbuddy
python login.py trae
```

**WorkBuddy 登录流程**：脚本请求授权 state → 自动打开腾讯 SSO 登录页 → 你在浏览器完成登录 → 脚本轮询自动捕获 token。
**Trae 登录流程**：脚本启动本地回调服务器 → 自动打开 trae.cn 登录页 → 你手机号验证码登录 → 浏览器跳转到 `127.0.0.1` 回调时脚本自动捕获 token（页面可能显示"打不开 127.0.0.1"是正常的，别关，脚本在后台接收回调）。

登录成功后命令行会显示用户名和凭证保存路径。凭证有效期通常 1-2 个月，到期前脚本会自动用 refreshToken 刷新。

### 2. 手动跑一次签到，确认正常

```bash
python checkin.py
```

正常输出类似：

```
=== 自动签到 2026-08-20 09:45:27 ===

[trae]
  状态: 领取成功
  积分: 当前积分: 4700 (用户福利=2000, 用户福利=2000, 每月登录积分=500)

[workbuddy]
  状态: 签到成功 +100积分，连续6天
  积分: 当前积分: 7862

=== 完成 ===
```

带 `--debug` 可看完整 HTTP 请求/响应，排查问题用：

```bash
python checkin.py --debug
```

### 3. 注册 Windows 定时任务（每天自动执行）

**以管理员身份运行 PowerShell**，进入项目目录：

```powershell
cd C:\path\to\auto-checkin
.\setup_task.ps1 -Install              # 默认每天 09:05 执行
.\setup_task.ps1 -Install -Time 08:30  # 指定执行时间
```

常用操作：

```powershell
.\setup_task.ps1 -RunNow     # 立即执行一次（测试用）
.\setup_task.ps1 -Status     # 查看任务状态和上次/下次运行时间
.\setup_task.ps1 -Uninstall  # 卸载任务
```

运行日志在 `logs\checkin.log`。

任务计划设置了 `-StartWhenAvailable`，如果到点电脑没开机，开机后会补跑一次；电池供电时也允许运行；执行超时 10 分钟，失败自动重试 2 次（间隔 15 分钟）。

## 凭证管理

- 凭证存在 `auths/` 目录下，每个账号一个 JSON 文件
- token 过期前 24 小时脚本自动用 refreshToken 刷新，无需人工干预
- 如果 refreshToken 也失效（长时间没跑、或平台强制下线），脚本会提示"会话已失效"，重新运行 `python login.py workbuddy` 或 `python login.py trae` 即可
- Trae 多账号：再跑一次 `python login.py trae` 登录另一个账号，`auths/` 下会有多个 `trae-*.json`，`checkin.py` 会全部签到

## 常见问题

**Q: Trae 提示"服务端限流，领取失败 (当前参与用户太多)"怎么办？**
这是 Trae 服务端对签到接口的临时限流（返回码 9074），不是账号或脚本的问题。脚本已内置自动重试 3 次（间隔 20s/40s 递增），若仍失败会在日志里明确标注"服务端限流"。等限流解除（通常几小时到一天）下次定时执行时就会成功。2026-08-18/19 两个参考开源仓库均有用户反馈同样问题，属于平台侧策略，连 Trae 桌面客户端原版凭证都会被拦。

**Q: WorkBuddy 显示"今日已签到"是正常的吗？**
正常。说明今天已经签到过了，脚本不再重复领取。

**Q: 任务计划没按时执行？**

- 确认电脑当时是开机状态（任务计划在关机时不会补跑，但设置了 `-StartWhenAvailable`，开机后会补一次）
- `.\setup_task.ps1 -Status` 查看上次运行结果
- 查 `logs\checkin.log`
- 确认注册时用的是当前用户身份（交互式登录），凭证文件在该用户目录下可访问

**Q: 想看详细请求/响应？**
`python checkin.py --debug` 会打印每个 HTTP 请求和返回，适合排查接口变更。

## 工作机制说明

### WorkBuddy（CodeBuddy 国内版）

- **登录**：OAuth 设备授权流程，`POST copilot.tencent.com/v2/plugin/auth/state` 拿 state → 浏览器腾讯 SSO 登录 → 轮询 `/v2/plugin/auth/token` 拿 token。返回字段为驼峰命名（`accessToken`/`refreshToken`/`expiresIn`）。
- **签到**：`POST www.codebuddy.cn/v2/billing/meter/daily-checkin`，鉴权头 `Authorization: Bearer <accessToken>`（实测必须带 Bearer 前缀，无前缀返回 401）。
- **签到返回**：`data.credit`（本次积分）、`data.streak_days`（连续天数）。
- **积分查询**：`POST www.codebuddy.cn/v2/billing/meter/get-user-resource`，积分字段 `data.Response.Data.TotalDosage`。
- **刷新**：`POST copilot.tencent.com/v2/plugin/auth/token/refresh`，body `{"refreshToken": ...}`。

### Trae（Trae Work 积分）

- **登录**：OAuth 浏览器回调流程，构造 `trae.cn/authorization?...` URL → 本地 `127.0.0.1:18080` 服务器接收回调 → 用 refreshToken 调 `ExchangeToken` 换 accessToken。
- **签到**：`POST api.trae.cn/trae/api/v2/ug/checkin_credits/claim`，鉴权头 `Authorization: Cloud-IDE-JWT <token>`，强依赖 `x-device-id` 做设备去重（一设备一天一次）。
- **积分查询**：`POST api.trae.cn/trae/api/v2/pay/ide_user_ent_usage`，从 `user_entitlement_pack_list` 权益包汇总 `credits_limit`。
- **刷新**：`POST api.trae.com.cn/cloudide/api/v3/trae/oauth/ExchangeToken`，body `ClientID/RefreshToken/ClientSecret:"-"/UserID`，返回 `Result.Token` 和 `Result.BoundDeviceID`（作为签到用 deviceId）。
- **9074 限流**：claim 接口被服务端限流时返回 `code:9074 当前参与用户太多`，脚本自动重试 3 次后明确报告。

## 参照的开源仓库

本脚本的接口端点、登录流程和请求格式参考了以下开源项目，致谢原作者：

| 产品      | 仓库                                                                        | 说明                                                    |
| --------- | --------------------------------------------------------------------------- | ------------------------------------------------------- |
| WorkBuddy | [Maquer/workbuddy-checkin](https://github.com/Maquer/workbuddy-checkin)     | Python 标准库，端点最全最准，Docker/cron 部署           |
| Trae      | [baokun-l/trae-work-checkin](https://github.com/baokun-l/trae-work-checkin) | Python 标准库，OAuth 登录 + claim + 计划任务            |
| Trae      | [inlayin/trae-check](https://github.com/inlayin/trae-check)                 | Electron 桌面 GUI，多账号管理，参考了其设备 ID 去重策略 |

实测中根据真实接口返回修正了以下差异（与参考仓库不同）：

- WorkBuddy 鉴权头需加 `Bearer` 前缀（参考仓库写的是无前缀，实测 401）
- WorkBuddy 登录返回字段为驼峰命名（`accessToken` 而非 `access_token`）
- Trae 登录回调实际只返回 `refreshToken`/`data`/`refreshExpireAt`，无 `userInfo`/`userJwt`
- Trae 签到用 `ExchangeToken` 返回的 `BoundDeviceID` 作为 deviceId

## 推广

[**维云模型开放平台**](https://vsllm.com/) — 一站式大模型 API 聚合服务，聚合主流大模型，开箱即用，按量计费，适合个人开发者和团队接入。

## 许可与免责

本脚本仅供学习研究。使用本脚本可能违反相关产品的服务条款，由此产生的任何后果由使用者自行承担。接口随时可能变更，如脚本失效请以官方渠道为准。
