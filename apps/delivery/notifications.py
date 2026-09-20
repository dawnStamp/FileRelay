"""收件通知发件箱：与上传事务一起入队，由后台投递邮件或签名 Webhook。"""

import asyncio
import hashlib
import hmac
import json
import smtplib
import ssl
import uuid
from datetime import timedelta
from email.message import EmailMessage

import aiohttp
from fastapi import HTTPException
from tortoise.expressions import F, Q

from apps.base.models import DeliveryCode, DeliveryNotification
from apps.base.config import refresh_settings
from apps.delivery.notification_settings import read_notification_config
from core.logger import logger
from core.settings import settings
from core.utils import get_now

MAX_ATTEMPTS = 3
LEASE_SECONDS = 300


async def enqueue_received(share, conn):
    """仅接受同事务产生的成功分享；队列不记录正文、口令或任何下载凭证。"""
    config = await read_notification_config(conn)
    channels = [channel for channel in ("email", "webhook") if config[channel + "_enabled"]]
    if not channels:
        return
    code = await DeliveryCode.get(id=share.delivery_id).using_db(conn)
    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "type": "delivery.received",
        "site_name": str(settings.name),
        "received_at": share.created_at.isoformat(),
        "delivery": {"id": code.id, "name": code.name},
        "file": {"id": share.id, "name": share.prefix + share.suffix, "size": share.size,
                 "kind": "text" if share.text is not None else "file"},
    }
    for channel in channels:
        await DeliveryNotification.create(
            event_id=event_id, channel=channel, revision=config["revision"], payload=payload,
            next_attempt_at=await get_now(), using_db=conn,
        )


async def enqueue_test(channel):
    """测试消息也走真实队列，但不伪造文件记录，不含任何实际收件内容。"""
    config = await read_notification_config()
    if channel not in {"email", "webhook"} or not config[channel + "_enabled"]:
        raise HTTPException(400, "请先保存并启用对应通知渠道")
    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id, "type": "delivery.test", "site_name": str(settings.name),
        "received_at": (await get_now()).isoformat(),
        "delivery": {"id": None, "name": "通知配置测试"},
        "file": {"id": None, "name": "测试通知（无实际文件）", "size": 0, "kind": "test"},
    }
    await DeliveryNotification.create(
        event_id=event_id, channel=channel, revision=config["revision"], payload=payload,
        next_attempt_at=await get_now(), is_test=True,
    )
    return {"message": "测试通知已加入队列", "event_id": event_id}


def send_email(config, payload):
    """SMTP 在工作线程执行，避免阻塞异步上传；TLS 默认校验证书。"""
    message = EmailMessage()
    message["Subject"] = "[FileRelay] 通知测试" if payload["type"] == "delivery.test" else "[FileRelay] 收到新寄件"
    message["From"] = config["email_from"]
    message["To"] = ", ".join(config["email_to"])
    # 稳定 Message-ID 可辅助邮件服务识别重试；网络确认丢失仍可能导致重复投递。
    message["Message-ID"] = f"<{payload['event_id']}@filerelay.local>"
    message.set_content(
        f"站点：{payload['site_name']}\n寄件用途：{payload['delivery']['name']}\n"
        f"文件：{payload['file']['name']}\n大小：{payload['file']['size']} 字节\n"
        f"收件时间：{payload['received_at']}\n事件编号：{payload['event_id']}\n\n"
        "请登录站点后台的寄件管理查看收件。此通知不包含文件内容或取件凭证。"
    )
    smtp_type = smtplib.SMTP_SSL if config["smtp_security"] == "tls" else smtplib.SMTP
    options = {"timeout": 10}
    if config["smtp_security"] == "tls":
        options["context"] = ssl.create_default_context()
    with smtp_type(config["smtp_host"], config["smtp_port"], **options) as smtp:
        smtp.ehlo()
        if config["smtp_security"] == "starttls":
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if config["smtp_username"]:
            smtp.login(config["smtp_username"], config["smtp_password"])
        refused = smtp.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


async def send_webhook(config, payload):
    """通用 JSON POST，禁止自动跟随重定向，避免签名和元数据被转发到其他主机。"""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-FileRelay-Event": payload["type"], "X-FileRelay-Event-Id": payload["event_id"],
    }
    if config["webhook_secret"]:
        signature = hmac.new(config["webhook_secret"].encode(), body, hashlib.sha256).hexdigest()
        headers["X-FileRelay-Signature"] = "sha256=" + signature
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as session:
        async with session.post(config["webhook_url"], data=body, headers=headers, allow_redirects=False) as response:
            if not 200 <= response.status < 300:
                # 不保存响应体或完整 URL；它们可能包含第三方返回的密钥、内部信息。
                raise NotificationSendError(f"Webhook HTTP {response.status}")


class NotificationSendError(Exception):
    """只允许经过白名单处理的错误文本展示在后台。"""


def safe_error(error):
    if isinstance(error, NotificationSendError):
        return str(error)
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "发送超时"
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return "SMTP 认证失败"
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "SMTP 收件人被拒绝，请检查收件地址"
    if isinstance(error, smtplib.SMTPException):
        return "SMTP 发送失败，请检查服务器和安全连接配置"
    return "连接失败，请检查通知地址、网络或 TLS 证书"


async def process_notifications_once():
    """数据库条件抢占防多进程重复发送，过期租约允许进程重启后继续处理。"""
    now = await get_now()
    query = DeliveryNotification.filter(
        Q(status="pending") | Q(status="sending"), next_attempt_at__lte=now,
    ).order_by("id").limit(20)
    for event in await query:
        if event.attempts >= MAX_ATTEMPTS:
            await DeliveryNotification.filter(id=event.id, status__in=["pending", "sending"], next_attempt_at__lte=now).update(
                status="failed", last_error="发送确认中断，已达到尝试上限",
            )
            continue
        # 将到期时间作为租约标识；最终更新仅由持有该租约的工作进程完成。
        lease = now + timedelta(seconds=LEASE_SECONDS)
        claimed = await DeliveryNotification.filter(
            id=event.id, status__in=["pending", "sending"], next_attempt_at__lte=now,
        ).update(status="sending", attempts=F("attempts") + 1, next_attempt_at=lease)
        if not claimed:
            continue
        attempt = event.attempts + 1
        owned = DeliveryNotification.filter(id=event.id, status="sending", next_attempt_at=lease)
        config = await read_notification_config()
        if config.get("revision") != event.revision or not config[event.channel + "_enabled"]:
            await owned.update(status="cancelled", last_error="配置已变更或渠道已关闭")
            continue
        try:
            if event.channel == "email":
                await asyncio.to_thread(send_email, config, event.payload)
            else:
                await send_webhook(config, event.payload)
        except asyncio.CancelledError:
            # 应用停止时保留 sending 租约，避免仍在执行的 SMTP 线程被立即重复投递。
            raise
        except Exception as exc:
            error = safe_error(exc)
            await owned.update(
                status="failed" if attempt >= MAX_ATTEMPTS else "pending", last_error=error,
                next_attempt_at=await get_now() + timedelta(seconds=30 if attempt == 1 else 120),
            )
            logger.warning("寄件通知发送失败 id=%s channel=%s attempt=%s", event.id, event.channel, attempt)
        else:
            await owned.update(status="sent", sent_at=await get_now(), last_error="")
    # 只清理已终结且超过七天的元数据，队列不会随长期运行无限增长。
    await DeliveryNotification.filter(
        status__in=["sent", "failed", "cancelled"], created_at__lt=now - timedelta(days=7),
    ).delete()


async def notification_loop():
    while True:
        try:
            await refresh_settings()
            await process_notifications_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("寄件通知队列暂不可用，将在下一轮重试")
        await asyncio.sleep(5)


async def list_notification_events():
    """返回最近状态，不返回消息 payload 或渠道配置，页面可安全手动刷新。"""
    items = await DeliveryNotification.all().order_by("-id").limit(20).values(
        "id", "event_id", "channel", "status", "attempts", "last_error", "created_at", "sent_at", "is_test",
    )
    return {"items": items, "total": await DeliveryNotification.all().count()}
