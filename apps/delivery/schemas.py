"""只校验寄件授权参数，存储和文件规则由系统设置统一控制。"""

import re
from datetime import datetime, timezone, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.base.metadata import normalize_metadata_note, normalize_metadata_tags


class DeliveryCodeConfig(BaseModel):
    """寄件授权的公共字段，文件和存储配置不在此模型中。"""
    # 禁止静默接受 owner_id 等越权字段，未来多用户必须由服务端身份决定归属。
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # 字段说明同时供后台接口与 MCP 工具清单使用，避免两处参数约定漂移。
    name: str = Field(min_length=1, max_length=100, description="寄件用途名称")
    is_permanent: bool = Field(default=False, description="为 true 时永久有效且不限次数，忽略期限和次数设置")
    expires_at: datetime | None = Field(default=None, description="非永久模式必填，须晚于当前时间；推荐带时区的 ISO 8601 时间")
    max_uploads: int = Field(default=1, ge=1, le=100000, description="有限模式的最大成功寄件次数，默认为 1")

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value):
        if value is None:
            return value
        # 无时区的后台输入明确解释为北京时间，接口返回保留时区。
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone(timedelta(hours=8)))
        if value <= datetime.now(timezone.utc):
            raise ValueError("有效期必须晚于当前时间")
        return value

    @model_validator(mode="after")
    def require_limited_expiry(self):
        # 永久模式由服务层同时清空期限和次数；普通模式必须提供有效期限。
        if not self.is_permanent and self.expires_at is None:
            raise ValueError("非永久寄件码必须设置有效期")
        return self


class CreateDeliveryCode(DeliveryCodeConfig):
    # 新建自定义寄件码最多 32 位；自动生成仍固定为 16 位。
    code: str = Field(default="", max_length=32, description="自定义寄件码，8～32 位字母、数字、下划线或短横线；留空自动生成")
    note: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value):
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{8,32}", value):
            raise ValueError("寄件码须为 8 至 32 位字母、数字、下划线或短横线")
        return value

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: Any) -> str:
        # 与文件管理的备注规则相同：非文本值转文本并截断。
        return normalize_metadata_note(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: Any) -> list[str]:
        return normalize_metadata_tags(value)


class UpdateDeliveryCode(BaseModel):
    """编辑寄件码时只更新提交的字段，历史过期记录可单独维护备注。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    is_permanent: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)
    expires_at: datetime | None = None
    max_uploads: int | None = Field(default=None, ge=1, le=100000)
    code: str = Field(default="", max_length=32)
    note: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, value):
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{8,32}", value):
            raise ValueError("寄件码须为 8 至 32 位字母、数字、下划线或短横线")
        return value

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value):
        # 编辑时可省略旧的过期时间；新提交的无时区时间按北京时间处理。
        return value.replace(tzinfo=timezone(timedelta(hours=8))) if value and value.tzinfo is None else value

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value):
        return None if value is None else normalize_metadata_note(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value):
        return None if value is None else normalize_metadata_tags(value)

    @model_validator(mode="after")
    def reject_explicit_null(self):
        # 可选字段表示可以省略，不能用 null 意外清空已有配置。
        forbidden = {"name", "is_permanent", "max_uploads", "note", "tags"}
        if not self.is_permanent:
            forbidden.add("expires_at")
        if any(field in self.model_fields_set and getattr(self, field) is None for field in forbidden):
            raise ValueError("编辑字段不能为 null；请省略不修改的字段")
        return self


class BatchDeliveryCodes(BaseModel):
    """批量管理只允许指定动作涉及的字段，防止整表配置被意外覆盖。"""

    model_config = ConfigDict(extra="forbid")
    ids: list[int] = Field(min_length=1, max_length=1000)
    action: str
    is_permanent: bool | None = None
    expires_at: datetime | None = None
    max_uploads: int | None = Field(default=None, ge=1, le=100000)

    @field_validator("expires_at")
    @classmethod
    def normalize_expiry(cls, value):
        # 批量期限与单条编辑使用相同的后台时间解释规则。
        return value.replace(tzinfo=timezone(timedelta(hours=8))) if value and value.tzinfo is None else value

    @model_validator(mode="after")
    def validate_batch(self):
        # 只有显式选择永久才允许清空期限；省略模式保持原设置。
        if "is_permanent" in self.model_fields_set and self.is_permanent is None:
            raise ValueError("永久选项不能为 null")
        if self.is_permanent is False and (self.expires_at is None or self.max_uploads is None):
            raise ValueError("切换为有限寄件码时，请同时设置有效期和最大上传次数")
        self.ids = list(dict.fromkeys(self.ids))
        if not self.ids:
            raise ValueError("请至少选择一个寄件码")
        if self.action not in {"enable", "disable", "delete", "update"}:
            raise ValueError("不支持的批量操作")
        if self.action == "update" and self.is_permanent is None and self.expires_at is None and self.max_uploads is None:
            raise ValueError("批量更新至少需要有效期或上传次数")
        if self.action != "update" and (self.is_permanent is not None or self.expires_at is not None or self.max_uploads is not None):
            raise ValueError("该批量操作不接受有效期或上传次数")
        return self


class VerifyDeliveryCode(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(min_length=8, max_length=64)


class SetDeliveryEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class DeleteDeliveryCode(BaseModel):
    """MCP 按明确的记录 ID 撤销授权，拒绝布尔值、浮点数和多余参数。"""

    model_config = ConfigDict(extra="forbid")
    code_id: int = Field(gt=0, strict=True, description="要删除的寄件码记录 ID，可从创建结果 item.id 或后台列表取得")
