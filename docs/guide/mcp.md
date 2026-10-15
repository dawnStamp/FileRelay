# MCP 接入

FileCodeBox 提供 **Model Context Protocol (MCP)** 接口，支持通过 Streamable HTTP 创建文本或文件分享，以及由管理员创建、删除寄件码。

## 连接配置

在 MCP 客户端添加远程 HTTP 服务：

- **传输方式**：Streamable HTTP
- **服务地址**：`http://<FileCodeBox 地址>:12345/mcp`
- **认证**：分享上传默认无需 Token；创建、删除寄件码始终需要管理员 Token

例如本机部署时，地址为 `http://localhost:12345/mcp`。若管理员关闭了游客上传、启用了 MCP Token 校验，或需要管理寄件码，请在客户端设置 `Authorization: Bearer <管理员 Token>`。寄件码及寄件上传凭证不能代替管理员 Token。不要把 Token 提交到公开配置或代码仓库。

## 可用工具

- `share_text`：创建文本分享。必填参数 `text`；可选 `expire_value`、`expire_style`。
- `share_file`：上传 Base64 文件并创建分享。必填参数 `filename`、`content_base64`；可选 `content_type`、`expire_value`、`expire_style`。
- `create_delivery_code`：管理员创建寄件码。必填 `name`；支持 `code`、`is_permanent`、`expires_at`、`max_uploads`、`note`、`tags`。
- `delete_delivery_code`：管理员按正整数 `code_id` 删除寄件码；ID 可从创建结果的 `item.id` 或后台列表取得。

分享工具的 `expire_style` 支持 `day`、`hour`、`minute`、`forever` 和 `count`，`expire_value` 必须大于 0。调用成功后返回分享码、链接和 Markdown 链接。

## 寄件码管理

`create_delivery_code` 与后台表单使用相同校验：自定义 `code` 支持 8～32 位字母、数字、下划线或短横线，省略或留空时自动生成；重复口令会返回工具错误。

- `is_permanent: true` 表示永久有效且不限次数，返回的期限、最大次数和剩余次数均为 `null`。
- 不选择永久模式时，必须提供未来的 `expires_at`，建议使用带时区的 ISO 8601 时间；`max_uploads` 默认为 1，范围为 1～100000。
- 返回 `item`（包含 `id`、状态、期限和计数）、`code`（寄件口令）、`url`（寄件链接）。寄件码只能用于投递，不是用于下载文件的取件码。

创建永久自定义寄件码的工具参数示例：

```json
{"name": "项目资料收集", "code": "Project_2026", "is_permanent": true}
```

删除时使用创建结果中的实际 ID，例如工具参数 `{"code_id": 123}`。删除采用软删除，并立即撤销旧寄件凭证；已收文件及其取件码继续遵循各自期限。不存在或已删除的 ID 返回工具错误，不会删除其他记录。只有用户明确要求撤销指定寄件码时才应调用删除工具。

## 实现位置

- `apps/base/mcp.py`：`/mcp` 协议入口、工具清单 `_tool_definitions` 和调用分发 `_call_tool`。
- `apps/delivery/schemas.py`：寄件码参数模型，MCP 工具清单直接复用这些模型的 JSON Schema。
- `apps/delivery/services.py`：创建和软删除业务，后台 REST 接口与 MCP 共同调用。

## 安全与限制

MCP 上传遵循 FileCodeBox 的游客上传开关、管理员 Token 校验、上传大小限制、文件类型校验及上传频率限制。仅向可信任的 AI 客户端提供服务地址；使用公网部署时请配置 HTTPS 并妥善保管管理员 Token。

寄件管理工具独立强制校验管理员身份，不受游客上传开关影响，也不占用普通上传频率额度。创建结果包含寄件口令，MCP 响应禁止缓存；参数校验错误不回显原始输入。
