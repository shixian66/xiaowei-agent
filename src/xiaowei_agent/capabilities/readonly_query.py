"""``starrocks.readonly_query`` 的能力声明与 F1 target 目录（设计 §5.4、§6）。

本模块只做声明与目录投影：不连接 StarRocks、不解析 DNS、不读 Secret。目录在 task-worker
启动时从 resources 域构造一次；配置变化要重启生效，旧计划因 config revision 改变而漂移。
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Final, Self

from xiaowei_agent.contracts import (
    CapabilitySpec,
    EffectClass,
    OperationSpec,
    ReadClass,
    ResourcesConfig,
    StarRocksResource,
)
from xiaowei_agent.contracts.enums import QueryRequirement
from xiaowei_agent.contracts.intent import READONLY_QUERY_INTENT
from xiaowei_agent.contracts.sql_query import QualifiedRelation, ReadonlyQueryBudget

READONLY_QUERY_CAPABILITY_ID: Final[str] = READONLY_QUERY_INTENT
READONLY_QUERY_CAPABILITY_VERSION: Final[str] = "1.0.0"
OP_EXECUTE_READONLY_QUERY: Final[str] = "execute_readonly_query"
READONLY_QUERY_GATEWAY: Final[str] = "starrocks"
READONLY_QUERY_POLICY_PROFILE: Final[str] = "readonly.starrocks.readonly_query.v1"
READONLY_QUERY_INPUT_SCHEMA_REF: Final[str] = "input.starrocks.readonly_query.v1"

READONLY_QUERY_SPEC: Final[CapabilitySpec] = CapabilitySpec(
    capability_id=READONLY_QUERY_CAPABILITY_ID,
    version=READONLY_QUERY_CAPABILITY_VERSION,
    domain="starrocks",
    input_schema_ref=READONLY_QUERY_INPUT_SCHEMA_REF,
    operations=(
        OperationSpec(
            operation=OP_EXECUTE_READONLY_QUERY,
            gateway=READONLY_QUERY_GATEWAY,
            effect_class=EffectClass.READ,
            read_class=ReadClass.RESTRICTED,
            side_effect=False,
            argument_schema_ref="schema.starrocks.readonly_query.execute.v1",
            query_requirement=QueryRequirement.CONFIRMED_ARTIFACT,
        ),
    ),
    policy_profile=READONLY_QUERY_POLICY_PROFILE,
    evidence_contract="evidence.starrocks.readonly_query.v1",
    eval_ref="evals.starrocks.readonly_query.v1",
)


def readonly_query_config_revision(resource: StarRocksResource) -> str:
    """F1 target 的 config revision：连接身份、账号、黑名单、上限、展示名与启用状态。

    Secret 不参与（不从口令派生任何可外泄的摘要）；任一参与字段变化都得到新 revision，
    进而改变 plan_hash，旧计划按现有漂移规则拒绝（设计 §5.6 第 1 步）。
    """
    payload = {
        "revision": "f1.readonly_query.target.v1",
        "resource_id": resource.resource_id,
        "environment": resource.environment,
        "display_name": resource.display_name,
        "host": resource.host,
        "port": resource.port,
        "database": resource.database,
        "username": resource.username,
        "tls_mode": resource.tls_mode,
        "enabled": resource.enabled,
        "f1_enabled": resource.f1_enabled,
        "blocked_relation_names": list(resource.blocked_relation_names),
        "budget": resource.f1_budget.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ReadonlyQueryTarget:
    """一个可被 F1 选中的 StarRocks 目标；只含策略所需字段，不含连接凭据。"""

    resource_id: str
    display_name: str
    environment_id: str
    default_database: str
    blocked_relation_names: tuple[str, ...]
    budget: ReadonlyQueryBudget
    config_revision: str

    @property
    def blocked_relations(self) -> frozenset[QualifiedRelation]:
        """SQLGuard ``confirmed_readonly`` 黑名单的输入；默认库见 ``default_database``。"""
        relations = set()
        for name in self.blocked_relation_names:
            database, _, relation = name.partition(".")
            relations.add(QualifiedRelation(database=database, name=relation))
        return frozenset(relations)


@dataclass(frozen=True, slots=True)
class ReadonlyQueryTargetCatalog:
    """启动时加载的 F1 目标目录；租户来自部署配置，环境来自资源登记。"""

    tenant_id: str | None
    targets: tuple[ReadonlyQueryTarget, ...]

    @classmethod
    def empty(cls) -> Self:
        """没有任何 F1 目标：每次查询都得到“当前环境没有可查询的 StarRocks”。"""
        return cls(tenant_id=None, targets=())

    @classmethod
    def from_resources(cls, *, tenant_id: str, config: ResourcesConfig) -> Self:
        """只收录已启用且开启 F1 的 StarRocks 资源，按展示名排序。"""
        targets = tuple(
            ReadonlyQueryTarget(
                resource_id=resource.resource_id,
                display_name=resource.display_name,
                environment_id=resource.environment,
                default_database=resource.database,
                blocked_relation_names=resource.blocked_relation_names,
                budget=resource.f1_budget,
                config_revision=readonly_query_config_revision(resource),
            )
            for resource in config.resources
            if isinstance(resource, StarRocksResource)
            and resource.enabled
            and resource.f1_enabled
        )
        return cls(
            tenant_id=tenant_id,
            targets=tuple(sorted(targets, key=lambda item: item.display_name)),
        )

    def targets_for(
        self, *, tenant_id: str, environment_id: str
    ) -> tuple[ReadonlyQueryTarget, ...]:
        """请求上下文内的全部候选；租户不符时为空，不回退、不猜默认。"""
        if self.tenant_id is None or tenant_id != self.tenant_id:
            return ()
        return tuple(
            target for target in self.targets if target.environment_id == environment_id
        )
