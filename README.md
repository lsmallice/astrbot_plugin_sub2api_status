# AstrBot Sub2API 渠道状态插件

通过 `/status` 查询 Sub2API 官方渠道监控接口，并将结果绘制成适合 QQ、Telegram 等即时通讯平台发送的状态图。可选开启 QQ 群活动：用户先把 QQ 与自己的 Smallice AI 账号绑定，再 @ 机器人领取一次余额。

## 功能

- 使用 Sub2API 管理员 API Key 查询全部已启用渠道监控。
- 自动遍历 `/api/v1/admin/channel-monitors` 的全部分页，不固定渠道数量。
- 兼容响应内置 `timeline`；旧接口则为每个渠道查询最近 60 次检测历史。
- 按渠道的完整分组名和平台精确关联官方分组倍率。
- 展示状态、主模型、分组倍率、对话延迟、端点 PING、7 天可用率、额外模型数量和最近检测时间。
- 每张图片最多展示 5 个渠道，更多渠道自动拆分成多张图片。
- 图片渲染不可用时，自动输出经过分页和排版的完整文本状态。
- 30 秒数据缓存和并发历史查询，避免群聊内重复命令压垮 Sub2API。
- 不输出或记录管理员 Key、渠道 Key 与未脱敏凭据。
- QQ 活动由 sidecar 管理员页面统一配置；活动启用后每个 QQ 发送者和每个 Sub2API 用户各只能成功领取一次。
- 使用 `/绑定` 生成短期网页登录链接并私发给发起绑定的 QQ；群聊中不会展示链接。网页登录后还必须由同一个 QQ 发送 `/绑定确认 6位验证码` 才会真正绑定，防止链接被他人复制后抢绑。
- 只有登录主站后由官方 `/api/v1/auth/me` 校验通过的账号才能进入待确认状态，插件不接收密码、API Key 或 JWT。
- 绑定关系由独立 sidecar 的 `qq_binding` schema 保存，QQ ID 和 Sub2API 用户 ID 均为唯一，挑战默认 10 分钟有效且最终只能使用一次。
- 领取状态保存于独立 sidecar 的 PostgreSQL `qq_claim` schema，不修改 Sub2API 数据库；余额增加使用官方 `Idempotency-Key`，重试不会重复入账。
- 用户可私聊机器人发送 `/余额`、`/账户` 或 `/我的余额` 查询自己的余额、账号状态、并发上限、注册时间和领取状态；发送 `/领取状态` 只查看活动领取结果。群内使用这些查询命令时，详细内容会私发，群里只提示发送结果。管理页面为 `https://smallice.xyz/tools/qq-claims/admin`。

## 安装与更新

将本仓库放入 AstrBot 的插件目录：

```text
AstrBot/data/plugins/astrbot_plugin_sub2api_status
```

然后在 AstrBot WebUI 中重载插件。

通过 WebUI 上传 ZIP 时，AstrBot 使用的是“安装插件”接口，同名目录已存在时会拒绝覆盖，
所以重复上传会提示目录已存在。这不是插件版本判断导致的。

更新本插件有两种方式：

1. 推荐：在 AstrBot 中通过 GitHub 仓库地址安装一次，之后在插件管理页使用“更新”按钮。
   AstrBot 会替换代码并保留插件配置和数据。
2. 继续使用 ZIP：卸载旧插件时保持“删除配置”和“删除数据”均关闭，完成卸载后再上传新 ZIP，
   然后重载插件。不要手动删除 AstrBot 的配置文件或插件数据目录，否则会丢失配置、领取记录和绑定数据。

本插件的配置文件由 AstrBot 独立保存，升级时新增配置会使用默认值，不会覆盖已有的管理员 Key、
绑定服务密钥或活动设置。

## 配置

在 AstrBot WebUI 的插件配置中填写：

| 配置项 | 示例 | 说明 |
| --- | --- | --- |
| `base_url` | `https://smallice.xyz` | Sub2API 站点根地址，不要填写接口路径 |
| `SUB2API_ADMIN_KEY` | `admin-...` | Sub2API 管理员设置中生成的管理员 API Key |
| `binding_service_url` | `https://smallice.xyz/tools/api/invite` | QQ 绑定 sidecar 地址；使用公网反代或 AstrBot 可达的内网地址 |
| `QQ_BINDING_SERVICE_KEY` | `...` | 与 sidecar 相同的内部服务密钥，不要公开 |
| `QQ_CLAIM_SERVICE_KEY` | `...` | 与 sidecar 相同的领取服务密钥；只用于机器人窄接口，不要公开 |
| `welcome_enabled` | `false` | 是否启用 QQ 新人入群欢迎 |
| `welcome_group_ids` | `123456789` | 自动欢迎的 QQ 群号，多个用逗号分隔；留空不发送 |
| `welcome_at_member` | `true` | 欢迎消息是否使用 QQ 原生 `@` 新成员 |
| `welcome_message` | `欢迎加入本群！` | 欢迎文案，支持 `{qq}` 和 `{group_id}` 占位符 |

管理员 Key 拥有完整管理权限。请限制 AstrBot 配置文件权限，不要把 Key 放在聊天消息、截图、日志或公开仓库中。赠送额度、允许群号和注册时间限制统一在主站“QQ群领取管理”中配置，机器人不再使用本地重复配置。

图片中的倍率是分组默认倍率，不包含用户专属倍率、倍率档位或高峰时段倍率。
渠道未填写分组名、分组名无法精确匹配或分组接口暂时不可用时，状态仍会
正常展示，倍率显示为 `--`。

## 使用

在支持的聊天平台中发送：

```text
/status
```

先在 QQ 私聊或群聊发送：

```text
/绑定
```

机器人会将一次性链接私发给当前 QQ 用户。打开私聊中的链接，在主站登录自己的账号并点击确认；再回到发起绑定的 QQ 发送页面显示的 `/绑定确认 6位验证码`。最终绑定成功后，在允许的 QQ 群中发送：

```text
@机器人 领取
```

也可以发送 `/绑定状态` 查看当前绑定状态，发送 `/余额`、`/账户` 或 `/我的余额` 查询自己的余额、账号状态、并发上限、注册时间和领取状态，发送 `/领取状态` 只查看领取结果。群内查询会优先私发详细信息，私发失败时不会在群里展示余额。领取不再接受任意邮箱或用户名，避免他人冒用账号；领取失败不会消耗资格，管理员可修复配置或服务后重试。用户侧不会看到管理员 Key、其他用户余额或具体内部错误。

如果私聊发送失败，群里只会提示先私聊机器人发送任意消息后重试 `/绑定`，不会降级在群里公开绑定链接。

领取时由 sidecar 服务端校验活动是否启用、当前群是否允许、QQ 是否绑定、Sub2API 账号注册时间和是否已经领取；这些条件与赠送额度在主站“QQ群领取管理”页面维护，机器人不再保存本地领取数据库。

新人入群欢迎只处理 QQ OneBot 的 `group_increase` 通知，并且必须同时开启 `welcome_enabled` 和填写
`welcome_group_ids`。机器人自己入群不会触发欢迎；同一群同一成员在 10 分钟内只发送一次。

插件调用：

```text
GET /api/v1/admin/channel-monitors
GET /api/v1/admin/channel-monitors/:id/history
GET /api/v1/admin/groups/all
POST /api/v1/admin/users/:id/balance
```

请求统一使用：

```text
x-api-key: <SUB2API_ADMIN_KEY>
Idempotency-Key: astrbot-sub2api-gift-<qq-user-id>-<sub2api-user-id>
```

## 状态说明

| Sub2API 状态 | 显示 |
| --- | --- |
| `operational` | 正常 |
| `degraded` | 降级 |
| `error` | 故障 |
| 其他或缺失 | 未知 |

文本历史使用 `O` 表示正常、`D` 表示降级、`X` 表示故障、`?` 表示未知、`·` 表示暂无记录。

## 图片渲染

插件首先生成不依赖外部图片资源的 SVG，再通过 AstrBot 官方 `html_render()` 转换为 PNG 后发送。这样保留了 SVG 的清晰布局，也兼容通常不直接展示 SVG 的聊天平台。图片渲染服务只接收格式化后的渠道状态，不会收到管理员 Key。

## 兼容性

- AstrBot `>=4.16,<5`
- Python 3.12
- Sub2API 支持管理员渠道监控接口的版本
