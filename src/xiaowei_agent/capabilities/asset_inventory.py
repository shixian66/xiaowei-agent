"""``asset.inventory.lookup`` 的版本化声明常量。"""

from typing import Final

from xiaowei_agent.contracts import CapabilitySpec, EffectClass, OperationSpec

ASSET_INVENTORY_CAPABILITY_ID: Final[str] = "asset.inventory.lookup"
ASSET_INVENTORY_CAPABILITY_VERSION: Final[str] = "1.0.0"
OP_LOOKUP_ASSET: Final[str] = "lookup_asset"
ASSET_INVENTORY_GATEWAY: Final[str] = "asset_inventory"
ASSET_INVENTORY_POLICY_PROFILE: Final[str] = "readonly.asset.inventory.lookup.v1"
ASSET_INVENTORY_INPUT_SCHEMA_REF: Final[str] = "input.asset.lookup.v1"
ASSET_INVENTORY_ENVIRONMENT_IDS: Final[tuple[str, ...]] = ("dev", "test")
"""fake 资产目录允许的非生产环境闭集。"""

ASSET_INVENTORY_SPEC: Final[CapabilitySpec] = CapabilitySpec(
    capability_id=ASSET_INVENTORY_CAPABILITY_ID,
    version=ASSET_INVENTORY_CAPABILITY_VERSION,
    domain="asset",
    input_schema_ref=ASSET_INVENTORY_INPUT_SCHEMA_REF,
    operations=(
        OperationSpec(
            operation=OP_LOOKUP_ASSET,
            gateway=ASSET_INVENTORY_GATEWAY,
            effect_class=EffectClass.READ,
            side_effect=False,
            argument_schema_ref="schema.asset.inventory.lookup.v1",
        ),
    ),
    policy_profile=ASSET_INVENTORY_POLICY_PROFILE,
    evidence_contract="evidence.asset.inventory.v1",
    eval_ref="evals.asset.inventory.v1",
)
