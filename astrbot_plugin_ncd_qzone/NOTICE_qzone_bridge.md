# 第三方组件声明（NOTICE）

## qzone_bridge

本仓库 `qzone_bridge/` 目录中的代码来自 AstrBot 插件 **QQ空间Ultra**：

- 上游仓库：https://github.com/diaomin66/astrbot_plugin_qzone_ultra
- 作者：雪碧bir
- 用途：提供 QQ 空间 Web 协议的客户端、Cookie 获取、响应解析等基础能力

本插件只保留了其中被实际引用的模块（依赖闭包），并删除了上游的渲染资源（assets）与无关业务模块。
`qzone_bridge/` 的版权与许可归其原作者所有，使用时请同时遵守上游仓库的许可条款。

## 其他

- QQ 官方表情映射（`faces.py`）数据来源于 QQ NT 客户端 `default-emojis/default_config.json`。
- 空友爱看接口能力来自 NapCat 的 `qzone-public` 实现。
