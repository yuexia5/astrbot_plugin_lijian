# 璃笺（AstrBot 插件）

运行在 AstrBot 内的本地日记与便签书库：AI 可以自动写日记、读日记、整理归档，
你也可以在 AstrBot WebUI 的书架页里像翻书一样阅读和编辑每一篇。


## 功能

- **日记库本体**：SQLite 存储（`notes` / `settings` / `write_events`），
  支持日记 / 便签两种类型、标签、置顶、归档。
- **LLM 工具**：`write_lijian_diary` / `list_lijian_notes` / `read_lijian_note` /
  `update_lijian_note` / `delete_lijian_note`，让 AI 写日记、读日记、归档。
- **指令**：`/lijian_status` `/lijian_write` `/lijian_list` `/lijian_read` `/lijian_delete`。
- **本地写入接口**：默认监听 `127.0.0.1:27176`，提供与 AI 桥相同的
  `POST /api/v1/diary` 等 HTTP 接口，外部脚本 / 自动任务可以直接调。
- **书架页面**：AstrBot WebUI 插件详情页里的「璃笺书架」——日记是书架上的一本本书，
  点击后从书架取出、翻开的动画，阅读 / 编辑 / 归档 / 导出 / 导入一应俱全。

## 安装

1. 将 `astrbot_plugin_lijian` 文件夹放入 AstrBot 的 `data/plugins/` 目录。
2. 在 AstrBot WebUI 重载插件。
3. 打开 WebUI 插件页 → 璃笺 → 页面 → **璃笺书架**。

## 配置项

| 配置 | 说明 |
| --- | --- |
| `bridge_enabled` | 是否启用本地写入接口（默认开） |
| `bridge_host` / `bridge_port` | 接口监听地址与端口（默认 127.0.0.1:27176） |
| `db_path` | 数据库文件路径，留空用插件数据目录 |
| `token` | 写入令牌。**留空则首次启动时自动生成随机令牌**；若手动填写，则每次启动都会以此值覆盖为当前令牌 |
| `default_mode` | 工具未指定 mode 时的写入模式（append_today / create） |

数据库默认存放在 `data/plugin_data/astrbot_plugin_lijian/lijian.sqlite3`
（即 AstrBot 的 `get_astrbot_plugin_data_path()` 下，不是插件目录本身）。

## 本机写入接口

接口需在请求头携带令牌（插件状态页可查看），默认只监听本机：

- `GET /api/v1/health` — 健康检查
- `POST /api/v1/diary` — 写入或追加日记（支持 Markdown、标题、日期、标签和写入模式）
- `GET /api/v1/notes` — 列出便签和日记摘要（`?include_archived=true` 含归档，`?include_body=true` 附带正文）
- `POST /api/v1/notes` — 新建便签（`body_md` 必填，可选 `title` / `kind` / `diary_date` / `tags` / `source`）
- `GET /api/v1/notes/{id}` — 读取某一篇的完整内容
- `PATCH /api/v1/notes/{id}` — 修改已有条目
- `DELETE /api/v1/notes/{id}` — 将条目移入归档
- `GET /api/v1/events` — SSE 事件流，书架有变动时推送 `notes_changed`（`?token=` 只接受本机连接）

`{id}` 支持唯一前缀，指令里显示的 8 位 id 可以直接拿来读。

## 让 AI 每天自动写日记

开启 AstrBot 主动能力后，在聊天中创建每日任务，例如：

```text
每天晚上 23:30，根据今天你和我的聊天总结，写一篇温柔自然的 Markdown 日记，
标题使用今天日期，标签包含 AstrBot 和 日记，然后调用 write_lijian_diary 写入璃笺，
写入模式使用 append_today。
```

## 书架页面

在书架页中：

- **取书**：点击任意一本书 → 书从书架缓缓抽出、放大，封面像真书一样翻开 → 进入阅读。
- **阅读**：Markdown 渲染（标题、代码块、表格、引用、图片等）。
- **编辑**：点击「编辑」切换为 Markdown 编辑器，Ctrl/⌘+Enter 保存。
- **归档 / 恢复**：归档页可恢复或删除（永久）。
- **导出**：单篇导出 `.md`（带 front matter），或一键导出全部为 zip。
- **导入**：上传 Markdown 文件批量导入（front matter 里的 title/date/tags 会被解析）。
- 搜索、按 日记/便签/归档 筛选、置顶书签（红绳）、AI 来源标记（✦）。

## 注意

- 需要 AstrBot ≥ 4.16（书架页面依赖插件 Pages 与 Web API）。**已在 4.29.1 上实测通过**：
  Web API 13 条路由的注册与匹配、handler 签名、Pages 目录约定、i18n key、配置 schema、版本号一致性，共 33 项。
- 升级 AstrBot 之后先跑一次自检，全绿再重载插件：

  ```bash
  <AstrBot路径>/venv/bin/python check_astrbot_compat.py
  ```

  脚本会自己向上找 AstrBot 源码根，不在 AstrBot 目录下也能跑；装在别处时用 `ASTRBOT_ROOT` 指定。
  它和 `test_main.py`（mock 框架，测插件自身逻辑）不同，检查的是真框架上的接入点有没有变。
- 依赖 `aiohttp`（插件目录 requirements.txt 已声明）。
- 数据目录遵循 AstrBot 规范放在 `data/plugin_data/` 下，更新 / 重装插件不会丢数据。
- 只面向本机使用，`bridge_host` 保持 `127.0.0.1`。

## 安全说明

日记是明文存进 SQLite 的，没有额外加密。下面几条是已经做到的，剩下的得靠使用环境。

- **文件权限**：数据库和它的 `-wal` / `-shm` 每次提交后都收紧到 `0600`，
  同机其他用户读不到。导出的 zip 和导入的中转文件同样是 `0600`，用完即删。
- **写入令牌**：留空时自动生成 `lijian-<uuid4>`（122 位随机）。手填时如果短于 16 个字符，
  启动日志会告警。校验用恒时比较，避免按响应时间猜。
- **SSE 的 `?token=`**：只有「本机直连」才认。请求一旦带上 `X-Forwarded-For`、
  `X-Real-IP` 或 `Forwarded`，说明前面有反代，query 令牌一律拒绝——否则令牌会进反代日志。
  走 header 的 `Authorization: Bearer` 不受这个限制。
- **响应头**：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、
  `Referrer-Policy: no-referrer`、`Cache-Control: no-store`，SSE 连接除外。
- **错误信息**：数据层的业务错误照实返回，其他异常一律 `internal error`，
  原始信息只进日志，免得把库路径、表结构回给调用方。
- **令牌可见性**：书架页面的状态接口会返回令牌明文，方便复制到别处用。
  能打开 WebUI 就是管理员级别，这条按这个前提设计；如果 WebUI 暴露在公网，请先给 WebUI 加密码。
- **绑定到 `0.0.0.0`** 会让同网段的人拿着令牌就能读写，启动时会打警告。不需要就别改。

## 更新日志

### v1.0.3

- 安全：数据库与 `-wal` / `-shm` 收紧到 `0600`，每次提交后复原（WAL 不跟随主库权限）。
- 安全：导出 zip、导入中转文件也设 `0600`；单篇导入上限 8 MB。
- 安全：SSE 的 query 令牌在带转发头时一律拒绝，堵住「反代把外网请求变成本机」的绕过。
- 安全：响应补 `nosniff` / `DENY` / `no-referrer` / `no-store`。
- 安全：桥接接口的内部异常不再原样回传，细节只进日志。
- 安全：令牌短于 16 位时启动告警；`bridge_host` 非回环时打警告。
- 仓库：删掉误提交的 `data/` 运行时目录；补 `.gitattributes`，`.gitignore` 扩充。
- 工具：`check_astrbot_compat.py` 改为在临时目录里跑，不再往插件目录里生成文件。

### v1.0.2

- 修：日记默认日期改用本地时区，凌晨写的日记不再落进前一天。
- 修：书架搜索现在能命中正文（列表接口新增 `include_body`）。
- 修：编辑器清空日期后保存，日期会真的清空。
- 修：`/lijian_read` 支持 8 位 id 前缀，和 `/lijian_list` 显示的一致。
- 修：本地接口重启时不再误报启动成功。
- 修：笔记全部归档后重载插件，不会再生成「第一笺」。
- 修：追加日记时合并标签，不再覆盖原有标签。
- 修：标签支持中文逗号、顿号、分号分隔。
- 修：`kind` 只接受 `note` / `diary`，避免写出前端过滤不到的条目。
- 修：导入的临时文件随用随删，导出的旧 zip 自动清理。
- 加固：令牌改为恒时比较；SSE 的 query 令牌只接受本机连接，去掉通配 CORS。
- 杂项：统一版本号，补 `.gitignore`，README 数据目录改正。
- 新增 `check_astrbot_compat.py`：在当前 AstrBot 上实测接入点，升级框架后先跑这个。