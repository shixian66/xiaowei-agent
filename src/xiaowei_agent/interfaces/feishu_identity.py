"""飞书主体到内部身份的静态、闭集映射。"""

import os
import stat
from collections.abc import Mapping
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelPermission,
    Contract,
    ControlledPii,
    IdentitySource,
    StrictStr,
)

_MAX_IDENTITY_FILE_BYTES: Final[int] = 1_048_576
_MAX_IDENTITY_ENTRIES: Final[int] = 10_000


class FeishuIdentityConfigurationError(RuntimeError):
    """身份目录文件缺失、越界或不满足闭集契约。"""


class FeishuIdentityNotFoundError(LookupError):
    """外部主体不在显式身份 allowlist 中。"""


class FeishuIdentityUnavailableError(LookupError):
    """外部主体已绑定，但账号或当前作用域授权不可用。"""


class FeishuIdentityDirectory(Protocol):
    """把飞书 ``open_id`` 精确映射到已认证内部主体。"""

    async def resolve(self, *, subject_ref: str) -> AuthenticatedPrincipal:
        """返回精确匹配的主体；未知主体必须 fail-closed。"""


class LegacyIdentityEntry(Contract):
    """旧静态文档里的一条身份，**保留原始 labels**。

    它是 ``_IdentityEntry`` 改名公开的结果：值域、数量边界、校验规则一字未改，
    只补了 ``subject_ref`` 的两条外泄通道。``labels`` 必须保持这个六成员闭集与
    1–6 的数量边界——放宽成 ``tuple[StrictStr, ...]`` 会让今天被拒的空标签、未知
    标签与 7 项标签全部变成合法输入，**改变既有飞书身份解析行为**；而未知标签
    一路走到规格 §6.6 的映射表时会变成一个非结构化的 ``KeyError``，不是
    fail-closed 的拒绝。

    ``actor`` 在旧文档里**没有长度上限**，这里也不加——上限属于新契约
    (``BoundedActor`` = 256)，两端怎么接上是切片 C 的转换策略，不是解析器的事。

    它和 ``contracts.identity.LegacyIdentityMigrationEntry`` 是**两个类型**：
    后者的字段已经是派生完的结果（角色已映射、``user_id`` 已摘要）。
    """

    subject_ref: ControlledPii = Field(exclude=True, repr=False)
    actor: StrictStr
    labels: tuple[
        Literal["operator", "dba", "oncall", "viewer", "approver", "admin"], ...
    ] = Field(min_length=1, max_length=6)


class _IdentityDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    version: Literal[1]
    tenant_id: StrictStr
    environment_id: StrictStr
    entries: tuple[LegacyIdentityEntry, ...] = Field(max_length=_MAX_IDENTITY_ENTRIES)

    @model_validator(mode="after")
    def _identifiers_are_unique(self) -> "_IdentityDocument":
        subjects = [entry.subject_ref for entry in self.entries]
        actors = [entry.actor for entry in self.entries]
        if len(subjects) != len(set(subjects)) or len(actors) != len(set(actors)):
            raise ValueError("identity references and actors must be unique")
        return self


_LABEL_PERMISSIONS: Final[Mapping[str, frozenset[ChannelPermission]]] = {
    "operator": frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
        }
    ),
    "dba": frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
        }
    ),
    "oncall": frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
        }
    ),
    "viewer": frozenset({ChannelPermission.VIEW_SAFE_TASK}),
    "approver": frozenset({ChannelPermission.VIEW_SAFE_TASK}),
    "admin": frozenset(ChannelPermission),
}


class StaticFeishuIdentityDirectory:
    """只接受配置文件中逐项声明的飞书主体，不做姓名或角色推断。"""

    def __init__(self, *, principals: Mapping[str, AuthenticatedPrincipal]) -> None:
        self._principals = dict(principals)

    async def resolve(self, *, subject_ref: str) -> AuthenticatedPrincipal:
        """按 ``open_id`` 精确查找，未知主体统一返回安全错误。"""
        try:
            return self._principals[subject_ref]
        except (KeyError, TypeError):
            raise FeishuIdentityNotFoundError("feishu identity not found") from None


def _read_identity_file(path: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("not a regular file")
        payload = os.read(descriptor, _MAX_IDENTITY_FILE_BYTES + 1)
        if not payload or len(payload) > _MAX_IDENTITY_FILE_BYTES:
            raise ValueError("identity document size is invalid")
        return payload
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_identity_document(
    *, path: str, tenant_id: str, environment_id: str
) -> _IdentityDocument:
    """读取一次静态身份文件并校验 scope；任何失败都不回显文件内容。

    两个公开入口共用这一个解析器。各自再实现一遍，就是第二套校验规则——两套规则
    迟早会分叉，而分叉的那一侧会安静地放行一批本该被拒的旧数据。
    """
    document: _IdentityDocument | None = None
    invalid = False
    try:
        document = _IdentityDocument.model_validate_json(_read_identity_file(path))
        if document.tenant_id != tenant_id or document.environment_id != environment_id:
            invalid = True
    except (OSError, ValueError, UnicodeError, ValidationError):
        invalid = True
    if invalid or document is None:
        raise FeishuIdentityConfigurationError(
            "feishu identity configuration invalid"
        ) from None
    return document


def read_legacy_identity_document(
    *, path: str, tenant_id: str, environment_id: str
) -> tuple[LegacyIdentityEntry, ...]:
    """只读地取出旧文档里的条目，**保留原始 labels**。

    ``load_feishu_identity_directory`` 返回的主体已经把 labels 压成了权限位闭集，
    压完之后 ``viewer`` 与 ``approver``、``operator`` 与 ``dba`` / ``oncall`` 完全
    一样；而规格 §6.6 的迁移表要按原始标签分流，所以迁移必须走这个入口。

    它不做任何映射、不写任何东西，也不改变本模块既有调用方的行为。
    """
    return _load_identity_document(
        path=path, tenant_id=tenant_id, environment_id=environment_id
    ).entries


def load_feishu_identity_directory(
    *, path: str, tenant_id: str, environment_id: str
) -> StaticFeishuIdentityDirectory:
    """读取一次静态身份文件并把 labels 压成权限位；失败不回显文件内容。"""
    document = _load_identity_document(
        path=path, tenant_id=tenant_id, environment_id=environment_id
    )

    principals = {
        entry.subject_ref: AuthenticatedPrincipal(
            tenant_id=document.tenant_id,
            environment_id=document.environment_id,
            actor=entry.actor,
            source=IdentitySource.FEISHU,
            subject_ref=entry.subject_ref,
            permissions=frozenset(
                permission
                for label in entry.labels
                for permission in _LABEL_PERMISSIONS[label]
            ),
        )
        for entry in document.entries
    }
    return StaticFeishuIdentityDirectory(principals=principals)


__all__ = [
    "FeishuIdentityConfigurationError",
    "FeishuIdentityDirectory",
    "FeishuIdentityNotFoundError",
    "FeishuIdentityUnavailableError",
    "LegacyIdentityEntry",
    "StaticFeishuIdentityDirectory",
    "load_feishu_identity_directory",
    "read_legacy_identity_document",
]
