"""通知独立配置：仅管理员可读取，密钥不进入公开配置或浏览器缓存。"""

import re
import uuid
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from tortoise.transactions import in_transaction

from apps.base.models import DeliveryNotification, KeyValue

CONFIG_KEY = "delivery_notifications"
SECRET_FIELDS = ("smtp_password", "webhook_secret")


class NotificationConfig(BaseModel):
    """邮件与 Webhook 可分别启用；保存时先合并旧值，再校验完整配置。"""

    model_config = ConfigDict(extra="forbid")
    email_enabled: bool = False
    smtp_host: str = Field(default="", max_length=253)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["starttls", "tls", "plain"] = "starttls"
    smtp_username: str = Field(default="", max_length=320)
    smtp_password: str = Field(default="", max_length=2048)
    email_from: str = Field(default="", max_length=320)
    email_to: list[str] = Field(default_factory=list, max_length=20)
    webhook_enabled: bool = False
    webhook_url: str = Field(default="", max_length=2048)
    webhook_secret: str = Field(default="", max_length=2048)

    @field_validator("smtp_host", "smtp_username", "email_from", "webhook_url")
    @classmethod
    def clean_single_line(cls, value):
        # 阻止邮件头注入与 URL 中的控制字符；密码按原文保留。
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("配置不能包含控制字符")
        return value.strip()

    @field_validator("email_to")
    @classmethod
    def validate_recipients(cls, values):
        result = []
        for value in values:
            if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value):
                raise ValueError("收件邮箱格式不正确，请每行填写一个邮箱")
            if value not in result:
                result.append(value)
        return result

    @model_validator(mode="after")
    def validate_channels(self):
        if self.email_from and not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", self.email_from):
            raise ValueError("发件邮箱格式不正确")
        if self.smtp_host and (any(char.isspace() for char in self.smtp_host) or "/" in self.smtp_host):
            raise ValueError("SMTP 主机应填写域名或 IP，不包含协议和路径")
        if self.email_enabled and not (self.smtp_host and self.email_from and self.email_to):
            raise ValueError("启用邮件前请填写 SMTP 主机、发件邮箱和收件邮箱")
        if self.webhook_url:
            try:
                url = urlsplit(self.webhook_url)
                port = url.port
                valid = url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password
                valid = valid and not url.fragment and not any(char.isspace() for char in self.webhook_url)
                if not valid or (port is not None and not 1 <= port <= 65535):
                    raise ValueError()
            except ValueError:
                raise ValueError("Webhook 须为 HTTP/HTTPS 地址，不能包含用户密码或片段") from None
        if self.webhook_enabled and not self.webhook_url:
            raise ValueError("启用 Webhook 前请填写接收地址")
        return self


async def read_notification_config(conn=None):
    query = KeyValue.filter(key=CONFIG_KEY)
    if conn is not None:
        query = query.using_db(conn)
    row = await query.first()
    return {**NotificationConfig().model_dump(), **(row.value if row else {})}


def public_notification_config(config):
    """管理员也只取得密钥是否已保存的标记；不回显真实密码。"""
    result = {key: value for key, value in config.items() if key != "revision"}
    for key in SECRET_FIELDS:
        result[key + "_set"] = bool(config[key])
        result[key] = ""
    return result


async def save_notification_config(data):
    # 单事务合并配置并取消旧队列，避免管理员关闭通知后重发积压消息。
    if not isinstance(data, dict):
        raise HTTPException(422, "通知配置必须是对象")
    changes = dict(data)
    async with in_transaction() as conn:
        current = await read_notification_config(conn)
        for key in SECRET_FIELDS:
            clear = changes.pop("clear_" + key, False)
            if not isinstance(clear, bool):
                raise HTTPException(422, "清除密钥选项必须是布尔值")
            if clear:
                changes[key] = ""
            elif changes.get(key) == "":
                changes.pop(key)
        candidate = {key: value for key, value in current.items() if key != "revision"}
        candidate.update(changes)
        try:
            valid = NotificationConfig.model_validate(candidate).model_dump()
        except ValidationError as exc:
            # 不返回 Pydantic input/context，避免校验错误带回提交的密码或带密钥的 URL。
            errors = "; ".join(error["msg"] for error in exc.errors(include_input=False, include_context=False))
            raise HTTPException(422, errors) from None
        previous = {key: value for key, value in current.items() if key != "revision"}
        if valid != previous or "revision" not in current:
            valid["revision"] = str(uuid.uuid4())
            await KeyValue.update_or_create(key=CONFIG_KEY, defaults={"value": valid}, using_db=conn)
            await DeliveryNotification.filter(status="pending").using_db(conn).update(status="cancelled", last_error="配置已变更")
        else:
            valid = current
    return public_notification_config(valid)
