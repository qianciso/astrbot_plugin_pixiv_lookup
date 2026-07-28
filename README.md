# Pixiv ID 查询

面向 AstrBot `>=4.26.7` 与 `aiocqhttp`/NapCat 的 Pixiv 作品 ID 查询插件。插件从
Pixiv 官方 App API 获取作品元数据，图片只经 `i.pixiv.re` 与 `i.pixiv.nl` 下载，
不会回退到 `i.pximg.net` 直连。

## 功能

- `/pi <作品ID> [页码]` 查询单图或多图作品，页码从 1 开始。
- 返回标题、作者、日期、ID、类型、分级、标签、分辨率和实际发送尺寸等可用信息。
- 支持 `original`、`large`、`medium`、`square_medium` 四档尺寸；缺失或过大时向下
  降级，单张图片安全上限为 50 MiB。
- 支持普通 QQ 消息与合并转发；群聊和私聊均通过 OneBot 动作发送并取得
  `message_id`。
- R18/R18G 总控默认关闭。开启后，敏感消息在 5–120 秒内自动撤回；正常热重载、
  停用或卸载时会立即撤回尚未到期的消息。
- 独立轮转日志默认保留 7 天，不记录 refresh token、完整图片 URL 或图片内容。

## 环境要求

- AstrBot `>=4.26.7`
- Python `>=3.10`
- `aiocqhttp` 平台适配器及 NapCat/OneBot 11
- NapCat 账号具备发送图片、合并转发及撤回机器人自身消息的权限

本插件不会修改 AstrBot 本体，也不会绕过 QQ 平台的内容审核或风控。

## 安装

### 从 AstrBot 插件市场安装（推荐）

1. 打开 AstrBot WebUI，进入“插件市场”。
2. 搜索“Pixiv ID 查询”，点击安装。
3. 安装完成后打开插件配置，填写 `pixiv_refresh_token` 并保存。

大陆网络无法连接 Pixiv 时，还需要在配置中填写代理软件提供的本地 HTTP 地址。

### 从 GitHub 安装

1. 从 [GitHub 仓库](https://github.com/qianciso/astrbot_plugin_pixiv_lookup) 下载 ZIP。
2. 在 AstrBot WebUI 的插件管理页面选择“安装插件”并上传 ZIP。
3. 安装完成后填写 `pixiv_refresh_token` 并保存配置。

不要把本项目文件直接覆盖到 AstrBot 源码目录，也不要把真实 token 写进仓库。

## 获取 refresh token

`pixiv_refresh_token` 是用于访问 Pixiv API 的账号凭据。可以通过以下工具或说明，在自己的电脑上登录 Pixiv 并取得 token：

- [eggplants/get-pixivpy-token](https://github.com/eggplants/get-pixivpy-token)：基于 Selenium，操作相对简单，推荐不熟悉代码的用户使用。
- [upbit 的 Selenium/ChromeDriver 获取脚本](https://gist.github.com/upbit/6edda27cb1644e94183291109b8a5fde)：通过浏览器自动化完成登录并获取 token。
- [pixivpy-async 上游 OAuth 说明](https://github.com/Mikubill/pixivpy-async#oauth-flow)：适合希望手动完成 OAuth 流程的用户。

取得 token 后，只需将其填写到 AstrBot 的插件配置页中。请注意：

- 不要在群聊、Issue、日志或截图中公开 token。
- 不要将 token 写入插件源码或提交到 GitHub。
- 不要使用他人分享或示例代码中附带的 token。
- token 失效时需要重新获取；怀疑泄露时应立即撤销 Pixiv 登录会话并更换。

如果你还是无法获取token，请询问你身边的ai工具，他们会帮助你。

图片代理与 API 代理相互独立。无论是否配置 API 代理，图片都只使用白名单反代。

## 使用

```text
/pi 12345678
/pi 12345678 2
```

第一条返回第 1 幅；第二条返回第 2 幅。多图作品始终显示“第 N/M 幅”。页码超出
范围时只返回实际总页数，不下载图片。

`command_name` 可填写 `pi`、`/pi` 或其他不含空格与斜杠的名称。保存配置后 AstrBot
热重载插件并通过原生命令管理功能注册新名称。名称无效或与其他命令冲突时，插件
记录错误并尝试恢复 `/pi`。

## 配置

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `pixiv_refresh_token` | 空 | 必填的 Pixiv 凭据，不会写入插件日志 |
| `pixiv_api_proxy` | 空 | 代理软件的本地 HTTP 地址，如 `http://127.0.0.1:7890`；不用于图片 |
| `command_name` | `pi` | 原生查询命令，可带或不带开头 `/` |
| `r18_enabled` | `false` | R18/R18G 总控；关闭时只返回阻止提示 |
| `r18_recall_seconds` | `120` | 敏感消息撤回延迟，范围 5–120 秒 |
| `image_size` | `large` | 四档图片尺寸之一 |
| `send_as_forward` | `true` | 开启为合并转发，关闭为普通单条消息 |
| `primary_image_proxy` | `i.pixiv.re` | 首选反代，另一域名自动作为备选 |
| `request_timeout` | `30` | 登录、API 与图片下载超时，单位秒 |
| `log_retention_days` | `7` | 独立日志保留 1–30 天 |

## R18 与撤回限制

作品分级优先依据 Pixiv 的 `x_restrict`，标签只在字段缺失时作为兼容判断。无法可靠
确认分级的作品不会发送。R18 总控开启后才会发送 R18/R18G 图片，并为取得的
OneBot `message_id` 创建撤回任务。

正常热重载、停用和卸载会先撤回待处理敏感消息。强制结束进程、机器断电、NapCat
离线或 QQ 拒绝撤回时，插件无法保证消息按时消失。请只在理解该限制且群规允许时
开启 R18。

## R18 与合规声明

R18/R18G 功能默认关闭。启用该功能前，请充分了解所在地法律法规、QQ 平台规则、群聊管理规定及相关内容传播风险。

发送成人或敏感内容可能导致机器人账号、群聊或管理员账号受到警告、限制功能、封禁或其他处罚。自动撤回只能缩短消息的可见时间，不能阻止平台审核、消息缓存、截图、转发或其他形式的留存；进程异常退出、网络中断或机器人权限不足时，撤回也可能失败。

插件使用者应对自身的部署方式、配置、发送内容和使用场景负责，并确保：

- 不向未成年人展示或传播不适宜内容。
- 不利用插件传播违法、侵权或违反平台规则的内容。
- 不将“自动撤回”视为规避平台审核或法律责任的手段。
- 不在无法确认成员年龄、授权范围或内容合规性的群聊中开启 R18 功能。

本项目维护者遵守适用的法律法规，不鼓励、支持或协助任何违法违规用途。如发现明确的滥用行为，维护者有权停止提供支持、停止更新，并从维护者能够控制的官方仓库、插件市场或其他发布渠道中撤下相关版本；必要时将配合平台或有关部门处理。

## 常见错误

- **尚未配置完成**：填写有效 refresh token；大陆网络还可能需要 HTTP API 代理。
- **作品不存在或不可见**：ID 错误、作品已删除，或当前 Pixiv 账号没有查看权限。
- **元数据不完整/分级未知**：插件按安全策略停止发送，不会绕过总控。
- **两个反代均不可用**：检查到 `i.pixiv.re`、`i.pixiv.nl` 的网络与 DNS；插件不会
  改用 Pixiv 图片源直连。
- **QQ 消息发送或撤回失败**：检查 NapCat 连接、账号权限、群禁言状态和平台风控。
- **自定义命令未生效**：检查是否包含空格、额外 `/`，以及 AstrBot 命令管理页是否
  已有同名命令。

## 日志与卸载

插件数据目录下的 `logs/plugin.log` 按天轮转，启动时及每 24 小时删除超过保留期的
备份。日志只包含作品 ID、页码、分级、所用反代主机、尺寸和错误类型。

卸载时在 AstrBot 界面同时勾选“删除配置”和“删除数据”。AstrBot 会删除插件配置与
数据目录，插件自身会关闭网络会话、停止清理任务、处理待撤回消息并删除命令重命名
记录。本插件不在插件目录和 AstrBot 数据目录之外创建文件。

## 扩展与开发

代码通过窄接口解耦主要职责：

- `ArtworkProvider`：元数据源
- `ImageProxyStrategy`：图片来源选择、降级与校验
- `ContentPolicy`：分级和发送策略
- `MessageSender`：平台消息发送与撤回

新增元数据源或平台时实现相应接口，并保持图片主机白名单与日志脱敏约束。主要模块
为 `provider.py`、`image_proxy.py`、`policy.py`、`messaging.py`、`recall.py` 和
`storage.py`。

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m compileall -q .
```

默认测试全部使用模拟 API，不访问 Pixiv。可选真实元数据测试需要显式设置
`PIXIV_REFRESH_TOKEN` 与一个确认全年龄的 `PIXIV_SAFE_ILLUST_ID`；CI 不设置这些变量。

## 许可证

[MIT](LICENSE)
