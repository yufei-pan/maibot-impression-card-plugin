# Changelog

本文件记录 maibot-impression-card-plugin（印象卡片）的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.3.3] - 2026-09-20

### 修复

- LLM 调用改为传 `task_name`（Host 模型任务名）。SDK 2.8.1 会默认附带 `task_name="utils"`，若再把 `utils`/`planner`/`replyer` 放进 `model`，Host 会当成具体模型名并报「未找到名为 'utils' 的模型」

### 新增

- 用 `llm.get_available_models()` 判断配置值：命中任务名走 `task_name`，否则走 `model_name` 直选具体模型

## [0.3.2] - 2026-08-19

### 修复

- 定期提醒按聊天流的适配器实际账号排除机器人自身，多账号环境不再把其他机器人账号列为人物
- 旧 Host 未提供聊天流账号时继续回退到 `bot.qq_account`

## [0.3.1] - 2026-08-13

### 变更

- `/卡片` 查询微调改为出厂关闭；后台由 `[proactive]` 定期提醒 + `nudge_impression` 更新。设 `[light_refresh] enabled = true` 可恢复查询时微调

## [0.3.0] - 2026-08-13

### 新增

- `/卡片` 可选在渲染前用较小上下文做 LLM 增量微调（`[light_refresh] enabled`，0.3.1 起默认关闭）
- 工具 `nudge_impression`：同样的增量微调，供规划器在定期提醒时使用
- `[proactive]`：默认每 6 小时对有过消息的聊天流提醒规划器是否微调印象（默认少说话）

## [0.2.7] - 2026-08-01

### 变更

- 将带注释的配置模板改为 `config.default.toml`，运行期 `config.toml` 不再入库
- 在 `create_plugin` / `on_load` 中从模板补齐或恢复 Runner 生成的空壳配置

## [0.2.6] - 2026-07-20

### 新增

- `[general] llm_rpc_timeout_ms`：本插件所有 `llm.generate` 的 cap.call RPC 超时（毫秒），默认 120000（120 秒）；冷启动/刷新印象与简介精简共用

## [0.2.5] - 2026-07-11

### 修复

- WebUI 清空可选字段时按默认值处理，避免空字符串触发校验错误

## [0.2.4] - 2026-07-10

### 变更

- 默认 compact / cold-start prompt 以具名 AI 生命体的「印象卡片」模块自居，并标注性格与表达风格

## [0.2.3] - 2026-06-23

### 修复

- 归一化配置时去除 `None`，使 WebUI 可切换插件启用状态
- 评分工具注入维度目录；拒绝空印象写入；接受常见 LLM 参数别名

## [0.2.2] - 2026-06-16

### 变更

- `/刷新印象` 默认仅配置管理员可用
- 明确「好感度」为独立总评，而非各维度之和
- 迁移旧配置字段以兼容 WebUI

## [0.2.1] - 2026-06-14

### 新增

- `fetch_avatar` 开关，便于离线环境关闭 QQ 头像拉取

### 文档

- 收紧 QQ ID 校验为 ASCII 数字；README 说明声明能力与唯一出站请求
- 补充 Docker MaiBot 版本信息

## [0.2.0] - 2026-06-14

### 新增

- 首次发布：好感 / 印象卡片（HTML 模板、SQLite、LLM 冷启动、Host 能力集成）
- 持久印象笔记与 `send_impression_card` 工具
- 冷启动 / 刷新经 `knowledge.search` 加载更丰富长期记忆
- 离线字体打包与卡片工作流改进
- 添加 MIT LICENSE

### 变更

- 缩短卡片布局、正面化维度表述、简化默认通知
- 扩展默认维度并优化身份展示
- 记忆检索推迟到冷启动 / 刷新；细化卡片命令
- 拆分卡片展示上限与存储上限；提高最近聊天上下文默认值
