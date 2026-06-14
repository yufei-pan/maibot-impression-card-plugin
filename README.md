# 印象卡片 (Impression Card)

为麦麦引入一套**欢乐向**的好感度 / 印象系统：群友可以查询自己（或他人）的「印象卡片」，
麦麦可以凭自己的喜好给人加减分、写印象笔记。一切数值都没有上下限——高分会顶出雷达边界、
负分会穿过圆心凹陷进去，好感量表条也能越过 `scale_min`–`scale_max` 两端。

> 这是一个 meme 向的插件，不必太当真。

## 功能一览

- **`/卡片`**（别名 `/card`、`/印象卡片`、`/impression_card`）：生成并发送一张印象卡图片。
  - `/卡片` 查自己；`/卡片 @某人`、引用某人后 `/卡片`、或 `/卡片 名字` 查他人（可在配置里关闭查他人）。
  - 可选 **`雷达:N`**（或 `radar:` / `top_n:` / `雷达数:`）指定雷达图显示几个得分最高的维度；省略则用配置 `radar_top_n`（默认 5）。
  - 卡片布局（T 字型）：**顶部 title bar** = 头像+昵称（左）· 标题（中）· 好感度数字（右）；
    其下一条 **横向好感量表条**（映射 `scale_min`–`scale_max`，可双向越界）；
    **下方主区域** = 左侧多维属性**雷达图**、右侧放大的**印象笔记**。
- **`/刷新印象`**（别名 `/refresh_impression`）：结合记忆与最近聊天重新评估某人，再发卡（同样支持 `雷达:N`）。
- **`/印象卡片帮助`**（别名 `/impression_card_help`、`/卡片帮助`、`/card_help`）：发送命令与雷达维度用法说明。
- 麦麦可调用的工具（LLM tools）：
  | 工具 | 作用 |
  | --- | --- |
  | `adjust_score` | 给某人某项加 / 减分（可负），按配置以【系统通知】播报 |
  | `set_score` | 直接把某项设为指定值（重置 / 校准） |
  | `append_impression` | 追加人物印象笔记（超长后台 LLM 精简） |
  | `rewrite_impression` | 覆盖人物印象笔记 |
  | `get_impression_detail` | 以 Markdown 返回某人的完整档案 |
  | `refresh_impression` | 结合长期记忆 + 最近聊天 + 既有数据重算分值与简介 |
  | `send_impression_card` | 向当前聊天主动发送印象卡片图片（同 `/卡片`；可选 `refresh_first`、`radar_top_n`） |

  > 工具的 `target` 传对方 QQ 号、昵称、别名、群名片或 person_id；省略则默认当前发言者。`dimension` 传 `total`（好感度总值）或某个维度 key。

## 数值与维度

- **好感度（总值）**始终存在、独立加减，用顶部数字 + 横向量表条展示，可越界。
- **子项维度**可在 `config.toml` 的 `[[dimensions]]` 里任意增删改，默认内置 **22 项**
  （混合养成 / 抽象 meme / RPG 属性，数量对齐大阿尔卡纳）：熟悉、信赖、欢乐、省心、贴贴、抽象、
  含金量、合拍、安心、慷慨、智力、魅力、活跃、幸运、混沌、中二、缘分、直觉、不羁、神秘、虔诚、希望。
  - 各子项均为**正向表述**（越高越好）：加分=认可，扣分=不满。
  - 默认不在 config 里写维度即用内置 22 项，插件升级新增维度也能自动用上。
  - 改 `label` / 顺序而保持 `key` 不变**不会丢数据**；改 `key` 视为新维度（旧值默认保留）。
  - 新增维度、未评分项、未生成用户的**默认值由 `default_score` 配置**（出厂默认 5）。
  - 图表参考区间由 **`scale_min` / `scale_max`** 配置（出厂默认 0–10）；冷启动 / 刷新 prompt 会注入当前配置值。
- **雷达图维度选取**（`radar_top_n`，默认 5）：
  - 取得分最高的 N 项；若最低分为负数，则显示最高 N−1 项 + 最低 1 项。
  - 命令 / 工具可临时覆盖：`雷达:N` 或 `radar_top_n` 参数（0 表示用配置值）。

## 印象笔记与持久化

- **`size_limit`（默认 256 字符）**：卡面右侧印象笔记的显示上限。
- **`impression_note_size_limit`（默认 81920 字节）**：`persistent_impression` 开启时的数据库存储上限；超出后强制 LLM 精简并写回。
- **`persistent_impression`（默认开启）**：
  - 库中可累积长文（在 `impression_note_size_limit` 内）；`append_impression` / `rewrite_impression` 逻辑相同。
  - 生成卡片时若超出 `size_limit`，**临时**用 LLM 精简卡面显示，**不改数据库**。
  - 超出 `impression_note_size_limit` 时，后台 LLM 精简并**写回数据库**（与 append / rewrite 无关）。
- **关闭 `persistent_impression`**：忽略 `impression_note_size_limit`，沿用旧行为——以 `size_limit` 为唯一上限，超出后在写入时后台精简入库；卡面直接读库。

## 图片格式

卡片本身**没有任何合成动画效果**。是否出动图，**只取决于用户头像本身是不是动图**：

- 头像是动图（多帧 gif / webp / apng）→ 把头像各帧合进卡片，输出动图（`animated_format`：`animated_webp` 默认 / `apng` / `gif`）。
- 头像是静态图、拉不到头像、或关掉 `animate_with_avatar` → 输出静态单张（`static_format`：`webp` 默认 / `png` / `jpg`）。

其它 `[image]` 项：`max_avatar_frames`（动图头像取帧上限，超出抽样）、`avatar_frame_fallback_ms`、
`loop`、`jpg_quality`、`webp_quality`、`background_color`（jpg/gif 填充底色）、`send_as_emoji`（走表情通道）。

> 实现：`render.html2png` 只出一张 PNG。动图路径下，卡片先把头像位置用纯色块（色键）渲染一次，
> 再由 Pillow 把头像每帧贴进色键区域——形状由模板自身的圆角/边框决定，对自定义模板也通用，且**只渲染一次浏览器**。

## 卡片外观

`[card]` 的 `card_template` 指向 HTML 模板（相对插件目录），内置三套：

- `assets/parchment.html` — 羊皮纸
- `assets/holo.html` — 全息科技
- `assets/cute.html` — 可爱贴纸

指向你自己的 HTML 即可完全自定义。模板可用的占位符与必须保留的 SVG 着色类，见各内置模板顶部注释。

## WebUI 配置提示

每个配置项都带中文 `description`，WebUI 默认在鼠标悬浮该项标签时弹出中文说明。

## 数据存储

- 存于插件目录下的 SQLite（默认 `data/affinity.sqlite3`），按 `person_id` 主键。
- **跨私聊 / 群聊共用一份**：同一个人在哪都查到同一张卡。

## 关于「从记忆系统调取信息」

新用户（库里没有）或刷新时，麦麦会结合以下来源生成各维度分值与简介：

1. **`PersonInfo.memory_points`**（若仍有数据）
2. Host 能力 **`knowledge.search`**（底层 A_Memorix，按 `person_id` 与昵称等多路检索长期记忆）
3. 全部已知身份指代（QQ 号、昵称、别名、群名片等）
4. 最近 N 条聊天记录（默认 1024 条，`recent_messages_limit` 可配）

`[cold_start]` 下还可调 `memory_search_limit`、`memory_max_items`、`memory_max_chars` 控制写入 prompt 的长期记忆规模。刷新印象时有独立的重评指引（`refresh_guidance`），避免分数惯性膨胀。

> 无需修改 MaiBot 主程序：记忆检索走内置 `knowledge.search` 能力（已在 `_manifest.json` 声明）。

## 配置

所有可调项见 `config.default.toml`。除 `[[dimensions]]` 外的字段**留空 / 注释掉即用内置默认**；
插件升级调整默认时，留空字段会自动跟随新值。

## 安装

1. 把本目录放进（或软链到）`MaiBot/plugins/`。
2. 依赖：`httpx`、`pillow`（见 `_manifest.json`）。卡面中文渲染使用插件内置 `assets/fonts/` 的 Noto Sans SC（woff2），**无需访问 Google Fonts 或外网**。
3. 启动 MaiBot 自动加载，或在 WebUI 中管理。

## 开发 / 测试

离线冒烟测试（不依赖 Host）：

```bash
PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
```

覆盖：能力声明与源码一致、配置一致性、维度解析、SVG 生成（雷达 + 量表条双向越界）、
模板占位符渲染、头像帧解析与色键合成、身份 / 别名解析、雷达 top_n 选取、长期记忆 helper 等。

## License

MIT
