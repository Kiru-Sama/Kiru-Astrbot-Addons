# 链路记忆（astrbot_plugin_ncd_memory）

按会话保存的纯文本记忆，补 AstrBot 只有 token 压缩、没有“沉淀式记忆”的空白。

## 能力

- 工具 `memory_write(content, kind)`：写一条本会话笔记
- 工具 `memory_search(query, limit)`：按相关度查本会话记忆（中文二元组 + 整串命中）
- 工具 `memory_forget(id)`：忘掉一条（群聊仅管理员）
- 每次写入新记忆后，下一轮只追加一行存在性提示；注入一次即复位，直到下次写入
- `on_llm_request` 扫 contexts 里的压缩摘要，按 hash 去重后自动入库；不额外调 LLM
- 面板：按会话列出条目，显示私聊对端昵称/头像（群聊显示群名/群头像），可编辑、删除

## 存储

`data/plugin_data/ncd_memory/<sha1(umo)[:16]>/`

```text
index.json          # umo / label / next_id / hint_pending / entries 元数据
entries/0001.txt    # 一条记忆一个纯文本文件
```

- 文件名与 id 由插件生成，模型只拿到数字 id
- 索引原子替换；会话内 asyncio.Lock 串行
- 条目正文按 `hash + updated_ts` 缓存，改一条只失效那一条
- 默认每会话 100 条 / 200KB；单条普通笔记 500 字，摘要 1200 字

## 边界

- 群聊允许写（本群自己的记忆），但 `memory_forget` 只允许私聊或管理员
- 记忆是“过往笔记”，不是系统指令；检索结果只作为资料返回
- 不做向量检索；用中文二元组重合率 + 整串命中 + kind/时间微调，停用词查询有兜底
- 面板走 AstrBot WebUI 登录态；头像由服务端拉取后返回 data URL，不依赖浏览器外链

## 面板

AstrBot 面板 → 插件 → 链路记忆。接口前缀：

`/astrbot_plugin_ncd_memory/ncd-memory/`
