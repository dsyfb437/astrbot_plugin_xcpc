<div align="center">

# XCPC 备赛助手

**astrbot_plugin_xcpc**

把 XCPC 备赛工作区接进聊天软件——地铁上用一段话完成 VP 复盘，随手查状态、题单和今天的任务。

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.26-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

[功能](#功能特性) · [安装](#安装) · [配置](#配置说明) · [命令](#命令表) · [平台支持](#平台支持) · [原理](#工作原理) · [排查](#常见问题)

</div>

---

## 功能特性

| 功能 | 说明 |
| --- | --- |
| 多平台同步 | Codeforces、AtCoder、QOJ、洛谷的做题记录与比赛记录，统一进本地数据库 |
| 训练方案 | 读一页汇总，告诉你今天做什么、为什么是这些题 |
| 打卡闭环 | `/xcpc 打卡`、`/xcpc 做了一半`、`/xcpc 没做`，执行情况会进入下一轮方案 |
| 榜单解析 | 把 QOJ 的榜单或提交记录整段粘进来，自动补全过题、罚时、AC 顺序 |
| 每日推送 | 晚上推一条：比赛倒计时 + 今天没勾完的任务 + 复盘提醒 |
| 题库标注 | 按难度、算法标签筛题，题号真假会校验 |
| 账号绑定页 | AstrBot WebUI 里挂一个页面，扫码/验证码登录、手动导入 Cookie |
| 自检 | `/xcpc 自检` 一次性告诉你哪里没配好，而不是等你踩到 |

它不是一个"再来一个刷题打卡 bot"。它假设你已经在用一个只有自己看得懂的备赛工作区
（`00-plan/` 到 `04-review/` 那套目录），这个插件是那套东西的聊天入口。

## 安装

**要求：AstrBot ≥ 4.26。** 账号绑定页用的是「插件 Pages」能力，4.26 才有；
低于这个版本插件仍能加载，但点不进绑定页。

在 AstrBot WebUI 的插件页搜索 `astrbot_plugin_xcpc` 安装，或者手动：

```bash
cd AstrBot/data/plugins
git clone https://github.com/dsyfb437/astrbot_plugin_xcpc.git
```

装完在 WebUI 里重载插件。

**没有需要手动装的 pip 依赖。** 核心逻辑只用 Python 标准库；`requirements.txt` 里那行
`aiohttp` 只有走 `backend=http` 时才用得上，而 AstrBot 本体已经依赖它了。

还需要配一个对话模型，`/xcpc 方案` 要用它。装完先在 QQ 里发 **`/xcpc 自检`**，
它会逐项告诉你还缺什么。

## 配置说明

配置在 AstrBot WebUI 的插件配置页里改。

### 基础

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `data_root` | 文本 | 空 | 数据库和日志放哪。留空 = 用插件自己的 `data/` 目录 |
| `log_level` | `info` / `debug` | `info` | `debug` 会记录每次 HTTP 请求（URL、状态码、耗时），凭据在那之前已经打码 |
| `max_reply_chars` | 整数 | `900` | 单条回复最大字数，超了按行切成多条发 |
| `list_preview` | 整数 | `5` | `/xcpc 题单` 每份显示几道 |
| `status_as_image` | 开关 | 关 | 把 `/xcpc 状态` 渲染成图片。要 AstrBot 配了文转图服务，没配就保持关闭 |

### 数据来源

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `backend` | `file` / `http` | `file` | `file` = 直接读写工作区文件，要求 AstrBot 和工作区在同一台机器；`http` = 调工作区里 `serve.py` 的接口，适合 AstrBot 在服务器上 |
| `workspace_root` | 文本 | 空 | 工作区根目录的绝对路径，`backend=file` 时必填 |
| `handle` | 文本 | 空 | 旧后端用的 Codeforces 用户名。留空的话那几个旧命令会直接告诉你去填，**不会猜一个** |
| `http_base` | 文本 | `http://127.0.0.1:8787` | `backend=http` 时，复盘台地址 |
| `http_token` | 文本 | 空 | `serve.py` 启动时打印的那个 4 位访问码 |
| `http_timeout` | 整数 | `20` | HTTP 超时（秒）。复盘台跑 `/api/refresh` 时会比较慢 |

### 模型与同步

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `llm_provider_id` | 文本 | 空 | 留空 = 用当前会话的默认模型。**这不是降级开关**：模型调不通时插件会明确报错，不会退化成规则方案 |
| `plan_max_minutes` | 整数 | `200` | 每天训练时长上限，超了会提醒你计划排太满 |
| `sync_interval_min` | 整数 | `60` | 自动同步间隔。`0` = 只手动 `/xcpc 同步`。各站有速率限制，不建议低于 30 |
| `rate_limit_scale` | 小数 | `1.0` | 拉取速度倍率。调小更慢更安全；调大有硬下限保护 |

### 权限与推送

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `admin_only` | 开关 | 开 | 这个插件会往真实文件里写字，默认只让 AstrBot 管理员用 |
| `allow_senders` | 列表 | 空 | 允许使用的 QQ 号白名单。填了之后只有名单里的号能用，优先级高于 `admin_only` |
| `daily_push` | 开关 | 开 | 每晚推一条。先在会话里发 `/xcpc 订阅`，再发 `/xcpc 推送测试` 当场验证 |
| `push_time` | 文本 | `22:30` | 24 小时制 `HH:MM`，按北京时间算，服务器时区是 UTC 也不影响 |
| `push_umo` | 文本 | 空 | 固定推送目标，形如 `aiocqhttp:FriendMessage:1234567`。一般用 `/xcpc 订阅` 就够了 |

### 解析

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `penalty_per_fail` | 整数 | `20` | 每次失败罚几分钟。ICPC/QOJ/大多数区域赛 = 20，Codeforces = 10。嫌改配置麻烦的话，在粘贴内容第一行单独写个 `cf` 也能按 10 算 |

## 使用

所有指令都挂在 `/xcpc` 这个**指令组**下面，单独发一条 `/xcpc` 会把子指令列出来。
这样做是为了不和别的插件撞名 —— AstrBot 里同名指令是所有插件一起接的，
装个别的插件也叫 `/状态`，两边都会回一条，你分不清哪条是谁发的。

第一次要先关联身份，因为网页不知道你是谁：

```
QQ 里发  /xcpc 绑定          → 拿到一个 6 位码，10 分钟内有效，只能用一次
WebUI 打开「账号绑定」页     → 输入这个码
```

码里的字母数字去掉了 `0/O`、`1/I/l` 这些容易看错的字符，因为它是要在手机上照着敲的。
同一个人重复发 `/xcpc 绑定`，旧码会立刻作废。

然后在同一个页面上填 CF handle。AtCoder 同理，这两个不用登录。
QOJ 需要登录（可能要两步验证），洛谷要从浏览器复制 Cookie 过去。
页面上有登录流程和日志面板，卡在哪一步能直接看见。

之后日常就是三条：

```
/xcpc 同步     拉数据。第一次会顺便拉题库（CF 两万多题，要十几秒）
/xcpc 方案     告诉你下一步练什么
/xcpc 打卡     做完了
```

隔一段时间发一次 `/xcpc 总结`，能看到模型实际收到的是哪一页汇总。
它不花 token，是排查"方案怎么这么离谱"的第一站。

## 命令表

**先做这个**

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/xcpc 自检` | `/xcpc selfcheck` `/xcpc 诊断` | 检查配置和依赖，告诉你哪里没配好 |
| `/xcpc 绑定` | `/xcpc bind` `/xcpc 账号` | 拿绑定码，去 WebUI 页面上输 |

**日常**

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/xcpc 同步` | `/xcpc sync` | 同步做题记录、比赛记录。可跟平台名 |
| `/xcpc 方案` | `/xcpc plan` `/xcpc 下一步` `/xcpc 今天做什么` | 给下一步方案。可以带要求，比如 `/xcpc 方案 这周别排 VP` |
| `/xcpc 我的状态` | `/xcpc mystatus` `/xcpc 数据` | 已同步数据的概况 |
| `/xcpc 比赛` | `/xcpc contests` | 比赛记录 |
| `/xcpc 总结` | — | 看模型实际收到的那份汇总（不花 token） |
| `/xcpc 日志` | `/xcpc log` | 看最近日志（默认 20 条，最多 100）。凭据已打码，可以直接贴出来 |

**打卡闭环**

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/xcpc 打卡` | `/xcpc done` `/xcpc 做完了` | 今天做完了 |
| `/xcpc 做了一半` | `/xcpc partial` `/xcpc 半` | 只做了一部分 |
| `/xcpc 没做` | `/xcpc skip` `/xcpc skip今天` | 今天没做，可以跟原因 |
| `/xcpc 反馈` | `/xcpc feedback` `/xcpc 说一句` | 记一句感受，下一轮方案会带上 |

**复盘**

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/xcpc 复盘` | — | 记一次赛后复盘。也可以直接发一段带「比赛:」的话，不用打指令 |
| `/xcpc 解析` | `/xcpc 粘贴解析` `/xcpc parse` | 把 QOJ 榜单或提交记录粘进来，自动整理。**不写任何文件**，只回报给你核对 |
| `/xcpc 随手记` | `/xcpc 记` `/xcpc inbox` | 往 inbox 里丢一行，不用管格式 |
| `/xcpc 格式` | `/xcpc 模板` `/xcpc format` | 复盘该怎么写 |

**赛程与题单**

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/xcpc 今天` | `/xcpc today` `/xcpc 待办` | 今天那组任务（读 `00-plan/sprint.md`） |
| `/xcpc 状态` | `/xcpc status` `/xcpc st` | 核心指标：rating / 已 AC / 连续天数 / KPI |
| `/xcpc 题单` | `/xcpc list` `/xcpc lists` | 当前题单的前几道 |
| `/xcpc 刷新` | `/xcpc refresh` | 重新抓 CF 数据并重跑诊断（只有 `file` 后端支持） |
| `/xcpc 题库` | `/xcpc bank` `/xcpc problems` | 拉题库标注（难度 + 标签）。可跟平台名，默认 CF。全局数据，拉一次就够 |

**推送**

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/xcpc 订阅` | `/xcpc subscribe` | 打开每日推送（私聊就订阅私聊，群里就订阅那个群） |
| `/xcpc 退订` | `/xcpc unsubscribe` | 关掉每日推送 |
| `/xcpc 推送测试` | `/xcpc pushtest` `/xcpc 测试推送` | 当场推一条，验证推送通不通 |

**其他**

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/xcpc 帮助` | `/xcpc help` | 分主题的帮助：`/xcpc 帮助 同步` |

## 平台支持

| | 要登录吗 | 提交记录 | 比赛记录 | 题库标注 |
| --- | --- | --- | --- | --- |
| Codeforces | 不用 | 官方 API | 有排名和 Δrating | 全量两万多题，带官方 rating 和算法 tag |
| AtCoder | 不用 | 社区 API | 有排名和 Δrating | 全量题库，但 API 里没有算法 tag |
| QOJ | 要 | 登录后 | 还没做 | `/problems` 不用登录 |
| 洛谷 | 要 | 导入 Cookie 后 | 暂不支持 | 难度和标签都有 |

几点说明：

- **QOJ 的提交和榜单必须登录。** 没登录时它返回的是登录页，插件认得出来，
  会直接说「需要登录」，不会显示成「你没提交过」。
- **AtCoder 走的是社区服务**（AtCoder Problems），不是官方。它挂了只影响 AtCoder 一个平台。
- **难度不跨平台换算。** CF rating、AtCoder 的 IRT 值、洛谷的 1–7 档是三套尺子，
  硬换会污染统计。每条记录都带来源标记，跨平台时按区间比，不按数值比。
- **洛谷的自动登录没打通**（被第二层挑战页挡住），走手动导入 Cookie，数据一样完整。

## 工作原理

分两层：

**规则层**负责确定性的部分——同步数据、算统计、筛候选池、校验题号真假。
**模型层**只做判断——从候选池里挑今天做什么、理解你的口语反馈。

模型不直接读原始提交记录，读的是规则压缩过的一页汇总。所以它的每个说法都能追到具体数字，
`/xcpc 总结` 就是那一页。难度区间也是从你自己的数据推的（做过题目的中位难度上下浮动），
不是模型拍脑袋。

目录结构：

```
main.py            AstrBot 插件入口、命令、WebUI 路由
xcpc_core.py       与 AstrBot 无关的核心逻辑（可直接单独用）
core/              数据库、日志、同步、汇总、自检、账号会话
platforms/         四个平台各一个模块，统一成同一套 Fetched 返回
pages/accounts/    账号绑定页（静态页 + 通过 bridge 调后端）
tests/             离线测试，用本地 stub 模拟真实响应
selftest.py        不依赖 AstrBot 的自测
```

平台的测试夹具按真实响应写，包括那些难看的部分：CF 的「HTTP 200 但 body 里 FAILED」、
洛谷的 C3VK 挑战、AtCoder 的 302。

## 数据与隐私

- 每个 QQ 号的数据是分开的，看不到别人的。
- **凭据不进日志，也不进 API 响应。** 日志层会对 `Cookie`、`Set-Cookie`、
  `password`、`_token` 打码；绑定页的接口返回由 `Session.public()` 兜底，
  不含 cookie / token / 密码。
- 凭据只存在本地 SQLite 和 `auth/` 目录，不会进 git。

## 常见问题

**先看 `/xcpc 日志 50`，或者绑定页下面的日志面板。**

失败信息带固定分类：`凭据失效` / `限流` / `挑战未过` / `页面结构变化` /
`网络不可达` / `解析失败` / `数据库错误` / `内部错误`。这样能一眼看出是
「该重新登录了」还是「对面改版了」。

出错时它会明确报错并记日志，不会给你一个悄悄降级的结果。

<details>
<summary><b>推送收不到</b></summary>

QQ 官方机器人（`qq_official`）没有开放主动消息 API，`daily_push` 开了也推不出去。
QQ 个人号（NapCat / Lagrange 这类 aiocqhttp）可以。
先在会话里发 `/xcpc 订阅`，再发 `/xcpc 推送测试`，当场就知道通不通。

</details>

<details>
<summary><b>绑定页点不进去</b></summary>

AstrBot 版本低于 4.26。插件能加载，但没有「插件 Pages」能力。

</details>

<details>
<summary><b>同步很慢</b></summary>

第一次会顺便拉题库，十几秒起步。之后增量同步很快。
嫌慢可以调 `rate_limit_scale`，但别调太大——站点有速率限制。

</details>

## 开发

测试全部离线，不需要 AstrBot，不需要网络：

```bash
python tests/test_db.py
python tests/test_log.py
python tests/test_pages.py
python tests/test_http.py
# ... tests/test_*.py 都是

python selftest.py --root <工作区>          # 210 项
python selftest.py                          # 209 项（少的那一项要工作区）
```

真实站点冒烟（连外网，手动跑）：

```bash
python tests/live_smoke.py
```

## 许可

[MIT](LICENSE)
