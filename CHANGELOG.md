# Changelog

本文件记录 maibot-impression-card-plugin（印象卡片）的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

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
