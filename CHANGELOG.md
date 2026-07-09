# 更新日志

## 1.3.0 (2026-07-10)
### 新增
- 知识库检索支持：播报时自动检索 AstrBot 知识库获取小说上下文
- 小说简介自动抓取与持久化
- 过往章节历史回顾（最近5章）
- 自定义剧情概要（novel_summaries）
- WebUI 知识库选择器（select_knowledgebase）

### 改进
- AI 播报改为「追更读者」视角，而非分析总结
- 播报格式升级：预设信息 + AI 人格回复
- 支持多本小说同时监控
- 数据持久化完善：章节历史、小说简介持久化到 data.json

### 修复
- ProviderRequest 参数名修正（text_message -> prompt）
- get_persona 改为 get_persona_v3_by_id
- metadata.yaml 添加 repo 字段，支持从 Git 更新插件

## 1.2.0 (2026-07-10)
### 新增
- 自定义人格支持（persona_id）
- 乱码正文作为上下文喂给 AI
- 强制拉取指令 /fanqie force

## 1.1.0
### 新增
- 基础番茄小说更新监控
- 推送到 QQ 群功能
- /fanqie 系列管理指令
