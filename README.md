# 🧾 AstrBot 游戏折扣提醒插件

本插件用于监控 NS 商店游戏的折扣状态，支持自动获取任天堂商店认证、全量/增量爬取游戏基础信息与折扣周期。提供游戏模糊查询、愿望单管理、折扣图片推送、未发售游戏列表查看等功能。

## ✨ 当前进度
- ✅ 任天堂商店基础数据爬取（全量/增量，支持断点续爬、随机间隔、平台过滤）
- ✅ 游戏折扣详情获取（增量更新）
- ✅ 自动获取任天堂商店认证（Playwright 模拟浏览器）
- ✅ 定时任务（基础信息每日增量更新、折扣信息每日增量更新）
- ✅ 手动指令（`getdata`、`getsaledata`、`remindhelp`）
- ✅ 时区配置（支持偏移量或标准时区名）
- ✅ **模糊查询** (`remindme` 指令)：
  - 第一层：多关键词 LIKE 匹配（汉字拆2-4子串，英文单词保留）
  - 第二层：拼音/罗马字片段匹配 + Levenshtein 编辑距离（支持翻页、确认）
- ✅ **愿望单管理**：`remindlist` 查看，`confirm` 添加，`remindnow` 推送折扣
- ✅ **未发售游戏列表**：`releaselist` 指令，按月查询，去重合并平台，生成图片
- ⏳ 折扣变化检测与主动推送
- ⏳ Steam、PS、Xbox 平台框架

## ⚙️ 配置说明

插件配置项通过 `_conf_schema.json` 定义，在 WebUI 中可直接编辑。主要配置如下：

### 时区设置
| 配置项     | 类型   | 默认值 | 说明                                                         |
| ---------- | ------ | ------ | ------------------------------------------------------------ |
| `timezone` | string | `"+8"` | 时区设置，支持偏移量（如 `+8`、`-5`）或标准时区名（如 `Asia/Shanghai`）。用于定时任务和折扣时间展示。 |

### NS 平台爬取配置
| 配置项                          | 类型   | 默认值         | 说明                                                         |
| ------------------------------- | ------ | -------------- | ------------------------------------------------------------ |
| `enable_ns`                     | bool   | `true`         | 是否启用任天堂平台追踪                                       |
| `ns_sessions`                   | list   | `[]`           | 允许追踪任天堂平台的会话 ID 列表（如 `"NapCat_Kagura:GroupMessage:12345678"`） |
| `ns_crawler_interval`           | int    | `60`           | 基础信息爬取每页间隔（秒）                                   |
| `ns_crawler_random_range`       | int    | `10`           | 基础信息爬取间隔随机浮动范围（秒），0 表示不浮动             |
| `ns_sales_crawler_interval`     | int    | `6`            | 折扣信息爬取每页间隔（秒）                                   |
| `ns_sales_crawler_random_range` | int    | `2`            | 折扣信息爬取间隔随机浮动范围（秒），0 表示不浮动             |
| `ns_basic_info_cron`            | string | `"0 2 * * *"`  | 基础信息更新周期（Cron 表达式，基于配置时区），默认每天 02:00 |
| `ns_sales_info_cron`            | string | `"1 23 * * *"` | 折扣信息更新周期（Cron 表达式，基于配置时区），默认每天 23:01 |

### 通用推送配置
| 配置项          | 类型 | 默认值 | 说明                                       |
| --------------- | ---- | ------ | ------------------------------------------ |
| `push_sessions` | list | `[]`   | 推送目标会话 ID 列表（未来推送功能使用）   |
| `help_text`     | text | `""`   | 自定义帮助信息（由 `remindhelp` 指令显示） |

### 权限认证（可选）
| 配置项                 | 类型 | 默认值  | 说明                             |
| ---------------------- | ---- | ------- | -------------------------------- |
| `enable_personal_auth` | bool | `false` | 开启个人权限认证（基于会话 ID）  |
| `personal_auth_list`   | list | `[]`    | 允许使用插件的会话 ID 列表       |
| `enable_manager_auth`  | bool | `false` | 开启群管理员身份认证（仅限群聊） |
| `manager_auth_list`    | list | `[]`    | 需要管理员认证的群会话 ID 列表   |
| `enable_simple_auth`   | bool | `false` | 开启简单群会话认证（全体成员）   |
| `simple_auth_list`     | list | `[]`    | 允许使用的群会话 ID 列表         |

### 其他平台（预留）
| 配置项           | 类型 | 默认值 | 说明                        |
| ---------------- | ---- | ------ | --------------------------- |
| `enable_steam`   | bool | `true` | 启用 Steam 平台追踪（预留） |
| `steam_sessions` | list | `[]`   | 允许追踪 Steam 的会话列表   |
| `enable_ps`      | bool | `true` | 启用 PS 平台追踪（预留）    |
| `ps_sessions`    | list | `[]`   | 允许追踪 PS 的会话列表      |
| `enable_xbox`    | bool | `true` | 启用 Xbox 平台追踪（预留）  |
| `xbox_sessions`  | list | `[]`   | 允许追踪 Xbox 的会话列表    |

## 🤖 指令

| 指令                        | 说明                                                         |
| --------------------------- | ------------------------------------------------------------ |
| `getdata [平台] [模式]`     | 手动触发任天堂游戏基础信息爬取。<br>平台：`ns`（原版 Switch）或 `ns2`（Switch2），默认 `ns2`。<br>模式：`inc`（增量，默认）或 `all`（全量，会重置断点）。 |
| `getsaledata [平台]`        | 手动触发任天堂游戏折扣信息增量爬取，平台默认 `ns2`。         |
| `remindhelp`                | 显示帮助信息（内容由 `help_text` 配置）。                    |
| `remindme <游戏名>`         | 模糊查询游戏，支持中/日文转写 ASCII 码和编辑距离匹配。<br>示例：`remindme 三国无双`、`remindme pokemon_pokopia`。 |
| `confirm [序号\|np\|pp\|0]` | 从 `remindme` 的结果中选择游戏：<br>- 数字序号（默认1）：确认该游戏。<br>- `np`：下一页<br>- `pp`：上一页<br>- `0`：若当前是第一层结果，则进入第二层（深层拼音/罗马字匹配） |
| `remindlist`                | 查看当前会话关注的游戏列表（开发中）。                       |
| `remindnow`                 | 立即检查当前会话的关注游戏是否有折扣（开发中）。             |
| `releaselist [月] [年]`     | 获取指定月份未发售游戏列表（月份在前，年份可选），生成图片。 |

## 📦 依赖

- Python 3.8+
- 核心依赖见 `requirements.txt`（包含 `aiohttp`, `python-dateutil`, `aiofiles`, `jinja2`, `playwright`, `aiosqlite`）

## 🔧 数据存储

- SQLite 数据库 `games.db`，位于 `data/plugin_data/astrbpt_plugin_gamesale_reminder/` 目录下。
- 包含以下表：
  - `ns_game_info`：游戏基础信息（`releaseDate` 为 TEXT 类型，存储 YYYY-MM-DD 格式）
  - `ns_game_sales`：游戏折扣周期（`start_time` 和 `end_time` 为 UTC 时间字符串）
  - `crawler_state`：全量爬取断点状态
  - `ns_credentials`：任天堂商店认证信息（Bearer token 和 client_id）
  - `ns_wishlist`：用户愿望单（按群+用户存储）
  - `image_cache`：图片缓存（文件名与数据版本）
- 图片缓存位于 `pushpng/` 子目录。

## 📝 注意事项

- **Playwright 安装**：插件首次需要安装 Playwright 及 Chromium 驱动，以自动获取任天堂商店的认证信息。

  ```bash
  pip install playwright
  playwright install chromium
  playwright install-deps chromium
  ```

- **额外依赖**：模糊查询需要 `pypinyin` 和 `pykakasi`，请手动安装（上述命令）。

  ```bash
  pip install pypinyin pykakasi
  ```

- **时区设置**：可通过 `timezone` 配置项指定偏移量（如 `+8`）或标准时区名（如 `Asia/Shanghai`）。定时任务 Cron 表达式基于此配置时区。

- **基础信息更新**：默认每天 02:00（配置时区）执行增量爬取，仅获取新发售或信息变化的游戏，不会重置现有数据。全量爬取需手动触发。

- **折扣信息更新**：默认每天 23:01（配置时区）执行增量爬取，获取当前所有打折游戏并与数据库对比，仅新增新的折扣周期。

- **数据按平台隔离**：NS 平台分为 HAC（原版 Switch）和 BEE（Switch2），爬取、存储均独立，切换平台后需重新爬取。

- **排序固定**：NS 商店搜索结果按“发售日从旧到新”排序，保证数据顺序稳定，便于断点续爬。