"""无状态 MCP 接口：提供快速分享以及仅限管理员的寄件授权管理。"""
import base64
import binascii
import html
import io
import json
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response

from apps.base.auth import _require_admin_payload
from apps.base.file_validation import validate_file_type, validate_upload_file
from apps.base.services import FileUploadService, validate_file_size
from apps.base.utils import ip_limit, validate_expire_style
from apps.delivery import services as delivery_services
from apps.delivery.schemas import CreateDeliveryCode, DeleteDeliveryCode
from core.settings import settings
from core.utils import sanitize_filename

router = APIRouter(tags=["MCP"])

MCP_PROTOCOL_VERSION = "2025-03-26"
TEXT_SHARE_MAX_BYTES = 222 * 1024
MAX_MCP_BODY_BYTES = 64 * 1024 * 1024


def _jsonrpc_error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _jsonrpc_result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _validate_origin(request: Request) -> None:
    """Reject browser cross-site POSTs before parsing any upload tool."""
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="跨站请求已拒绝")
    if not origin:
        return  # Native MCP clients do not send Origin and use no ambient cookies.
    try:
        parsed = urlsplit(origin)
        host = request.headers.get("host", "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.netloc.lower() != host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("origin mismatch")
    except (ValueError, TypeError):
        raise HTTPException(status_code=403, detail="跨站请求已拒绝")


async def _read_limited_body(request: Request) -> bytes:
    max_size = min(
        MAX_MCP_BODY_BYTES,
        max(1024 * 1024, int(settings.upload_size * 4 / 3) + 256 * 1024),
    )
    declared_size = request.headers.get("content-length")
    if declared_size:
        try:
            if int(declared_size) > max_size:
                raise HTTPException(status_code=413, detail="MCP 请求体过大")
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length 无效")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_size:
            raise HTTPException(status_code=413, detail="MCP 请求体过大")
    return bytes(body)


def _tool_definitions() -> list[dict]:
    return [
        {
            "name": "share_text",
            "description": "将文本快速上传到 FileCodeBox 并创建分享链接。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要分享的文本"},
                    "expire_value": {"type": "integer", "minimum": 1, "default": 1},
                    "expire_style": {"type": "string", "enum": ["day", "hour", "minute", "forever", "count"], "default": "day"},
                },
                "required": ["text"],
            },
        },
        {
            "name": "share_file",
            "description": "将 Base64 编码的文件上传到 FileCodeBox 并创建分享链接。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名"},
                    "content_base64": {"type": "string", "description": "文件内容的标准 Base64 编码"},
                    "content_type": {"type": "string", "default": "application/octet-stream"},
                    "expire_value": {"type": "integer", "minimum": 1, "default": 1},
                    "expire_style": {"type": "string", "enum": ["day", "hour", "minute", "forever", "count"], "default": "day"},
                },
                "required": ["filename", "content_base64"],
            },
        },
        {
            "name": "create_delivery_code",
            "description": "管理员创建寄件码，授权他人上传文件。支持自定义码或自动生成、有限期次数或永久不限次。"
                           "非永久模式必须提供 expires_at。返回 item（含记录 ID）、code 和寄件链接 url；始终需要管理员 Token。",
            # 直接复用后台参数模型，长度、额外字段及时间规则由同一份模型校验。
            "inputSchema": CreateDeliveryCode.model_json_schema(),
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        },
        {
            "name": "delete_delivery_code",
            "description": "管理员按 code_id 删除寄件码，立即撤销该码及旧上传凭证，保留已收文件和已有取件码。"
                           "只在用户明确要求删除指定寄件码时调用；始终需要管理员 Token，不能使用寄件凭证。",
            "inputSchema": DeleteDeliveryCode.model_json_schema(),
            # 删除会撤销授权，明确标记为破坏性操作，供客户端展示或审批。
            "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        },
    ]


def _share_result(request: Request, code: str, name: str) -> dict:
    base_url = str(request.base_url).rstrip("/")
    share_url = f"{base_url}/#/?code={quote(code, safe='')}"
    markdown_name = html.escape(name).replace("[", "\\[").replace("]", "\\]")
    return {
        "code": code,
        "name": name,
        "url": share_url,
        "markdown": f"[{markdown_name}]({share_url})",
    }


async def _call_tool(request: Request, tool_name: str, arguments: dict) -> dict:
    authorization = request.headers.get("authorization", "")
    if tool_name in {"create_delivery_code", "delete_delivery_code"}:
        # 管理能力不随游客上传开关开放，也不占用普通文件上传的频率额度。
        _require_admin_payload(authorization)
        if tool_name == "create_delivery_code":
            create_data = CreateDeliveryCode.model_validate(arguments)
            result = await delivery_services.create_code(create_data)
            base_url = str(request.base_url).rstrip("/")
            return {**result, "url": f"{base_url}/#/delivery?code={quote(result['code'], safe='')}"}
        delete_data = DeleteDeliveryCode.model_validate(arguments)
        result = await delivery_services.delete_code(delete_data.code_id)
        return {"code_id": delete_data.code_id, **result}

    if settings.mcp_require_token or not settings.open_upload:
        _require_admin_payload(authorization)
    ip = ip_limit["upload"](request)

    if tool_name == "share_text":
        text = arguments.get("text")
        if not isinstance(text, str):
            raise ValueError("text 必须是字符串")
        if len(text.encode("utf-8")) > TEXT_SHARE_MAX_BYTES:
            raise ValueError("内容过多，请改用文件分享")
        expire_value = int(arguments.get("expire_value", 1))
        expire_style = str(arguments.get("expire_style", "day"))
        if expire_value < 1:
            raise ValueError("expire_value 必须大于 0")
        validate_expire_style(expire_style)
        code = await FileUploadService.create_text_share(text, expire_value, expire_style)
        ip_limit["upload"].add_ip(ip)
        return _share_result(request, code, "Text")

    if tool_name == "share_file":
        filename = arguments.get("filename")
        encoded_content = arguments.get("content_base64")
        content_type = str(arguments.get("content_type", "application/octet-stream"))
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("filename 不能为空")
        if not isinstance(encoded_content, str):
            raise ValueError("content_base64 必须是字符串")
        try:
            content = base64.b64decode(encoded_content, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("content_base64 不是有效的标准 Base64")
        safe_name = await sanitize_filename(filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        validate_file_type(safe_name, content_type)
        upload = UploadFile(filename=safe_name, file=io.BytesIO(content), size=len(content), headers=Headers({"content-type": content_type}))
        size = await validate_file_size(upload, settings.upload_size)
        await validate_upload_file(upload)
        expire_value = int(arguments.get("expire_value", 1))
        expire_style = str(arguments.get("expire_style", "day"))
        if expire_value < 1:
            raise ValueError("expire_value 必须大于 0")
        validate_expire_style(expire_style)
        share = await FileUploadService.create_file_share(upload, size=size, expire_value=expire_value, expire_style=expire_style)
        ip_limit["upload"].add_ip(ip)
        return _share_result(request, share["code"], share["name"])

    raise LookupError(f"未知工具：{tool_name}")


@router.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request):
    _validate_origin(request)
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="MCP 请求必须使用 application/json")
    try:
        message = json.loads(await _read_limited_body(request))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(_jsonrpc_error(None, -32700, "Parse error"), status_code=400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return JSONResponse(_jsonrpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request"), status_code=400)

    request_id = message.get("id")
    method = message["method"]
    if request_id is None and method.startswith("notifications/"):
        return Response(status_code=202)
    result: dict[str, Any] = {}
    try:
        if method == "initialize":
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "filecodebox", "version": "1.0.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_definitions()}
        elif method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return JSONResponse(_jsonrpc_error(request_id, -32602, "Invalid params"))
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                return JSONResponse(_jsonrpc_error(request_id, -32602, "Invalid params"))
            try:
                # 创建寄件码包含 datetime，统一转换后再生成文本与结构化结果。
                tool_value = jsonable_encoder(await _call_tool(request, params["name"], arguments))
                result = {"content": [{"type": "text", "text": json.dumps(tool_value, ensure_ascii=False)}], "structuredContent": tool_value}
            except LookupError as exc:
                return JSONResponse(_jsonrpc_error(request_id, -32602, str(exc)))
            except HTTPException as exc:
                if exc.status_code in {401, 403}:
                    raise
                result = {"content": [{"type": "text", "text": str(exc.detail)}], "isError": True}
            except ValidationError as exc:
                # 不回显 Pydantic 的原始 input/context，避免错误响应包含寄件口令等输入值。
                message = "；".join(
                    error["msg"] for error in exc.errors(include_input=False, include_context=False, include_url=False)
                )
                result = {"content": [{"type": "text", "text": message}], "isError": True}
            except (TypeError, ValueError) as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            return JSONResponse(_jsonrpc_error(request_id, -32601, "Method not found"))
        # 创建结果包含上传口令，不允许浏览器或中间代理缓存 MCP 响应。
        return JSONResponse(_jsonrpc_result(request_id, result), headers={
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION, "Cache-Control": "no-store",
        })
    except HTTPException as exc:
        return JSONResponse(
            _jsonrpc_error(request_id, -32001 if exc.status_code == 401 else -32003, str(exc.detail)),
            status_code=exc.status_code,
        )
