# MCP Integration

FileCodeBox provides a **Model Context Protocol (MCP)** endpoint over Streamable HTTP. Compatible AI clients can create text or file shares through it.

## Connection

Add a remote HTTP server in your MCP client:

- **Transport**: Streamable HTTP
- **Server URL**: `http://<FileCodeBox host>:12345/mcp`
- **Authentication**: sharing allows tokenless uploads by default; delivery-code management always requires an admin token

For a local instance, use `http://localhost:12345/mcp`. If the administrator disables guest uploads or enables MCP token validation in the admin settings, configure `Authorization: Bearer <admin token>` in the client. Never commit the token to public configuration or source code.

## Available tools

- `share_text`: create a text share. Required: `text`; optional: `expire_value`, `expire_style`.
- `share_file`: upload a Base64-encoded file and create a share. Required: `filename`, `content_base64`; optional: `content_type`, `expire_value`, `expire_style`.
- `create_delivery_code`: create an upload authorization. Requires an admin token and `name`; accepts `code`, `is_permanent`, `expires_at`, `max_uploads`, `note`, and `tags`.
- `delete_delivery_code`: revoke an upload authorization using a positive integer `code_id` from the creation result's `item.id` or the admin list. Always requires an admin token.

For sharing tools, `expire_style` accepts `day`, `hour`, `minute`, `forever`, or `count`; `expire_value` must be greater than zero. Successful calls return a share code, URL, and Markdown link.

## Delivery-code management

Creation uses the same validation as the admin UI. A custom `code` accepts 8–32 ASCII letters, digits, underscores, or hyphens; omit it or pass an empty string to generate a code. Duplicate codes are rejected.

Set `is_permanent: true` for an authorization that never expires and has unlimited uploads. Otherwise, provide a future `expires_at` (prefer ISO 8601 with a timezone); `max_uploads` defaults to 1 and accepts 1–100000. The result contains `item` (including its record ID and limits), `code`, and a delivery `url`. Permanent limits and remaining uploads are returned as `null`.

```json
{"name": "Project collection", "code": "Project_2026", "is_permanent": true}
```

Delete using the actual record ID, for example `{"code_id": 123}`. This soft-deletes the authorization and revokes its previous upload credentials, preserving received files and their retrieval codes. Missing or already-deleted IDs produce a tool error. Call this tool only when the user explicitly requests deletion of that specific delivery code.

Delivery management always requires `Authorization: Bearer <admin token>`, regardless of the guest-upload or MCP-token switches. A delivery code or delivery-session token does not grant administrative access. Responses use `Cache-Control: no-store`, and validation errors omit raw inputs.

Tool definitions and dispatch live in `apps/base/mcp.py`; shared validation lives in `apps/delivery/schemas.py`, and shared creation/deletion logic lives in `apps/delivery/services.py`.

## Security and limits

MCP uploads follow FileCodeBox's guest-upload switch, admin-token validation, upload-size and file-type limits, and upload rate limits. Expose the endpoint only to trusted AI clients. Use HTTPS for public deployments and keep the admin token secure.
