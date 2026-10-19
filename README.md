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

- 需要 AstrBot ≥ 4.16（书架页面依赖插件 Pages 与 Web API），当前适配 4.29.1。
- 依赖 `aiohttp`（插件目录 requirements.txt 已声明）。
- 数据目录遵循 AstrBot 规范放在 `data/plugin_data/` 下，更新 / 重装插件不会丢数据。
- 只面向本机使用，`bridge_host` 保持 `127.0.0.1`。

## 更新日志

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