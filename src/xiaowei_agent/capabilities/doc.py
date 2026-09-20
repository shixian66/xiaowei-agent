"""从 Registry 快照生成能力地图。

``docs/CAPABILITIES.md`` **由代码生成、由 CI 检查**，不手工维护
（AGENTS.md「文档与交接纪律」）。手工维护的能力总表会与声明漂移，而漂移的方向
通常是"文档说有、代码没有"。
"""

from typing import Final

from xiaowei_agent.contracts import CapabilitySnapshot

_HEADER: Final[str] = """# 能力地图

> **本文件由 `xiaowei_agent.capabilities.doc.render_capabilities_doc()` 生成，
> 由 `tests/security/test_capabilities_doc.py` 检查一致性。不要手工编辑。**

能力状态目前最强为 `tests`：已有确定性测试与离线 eval，**未部署、未 canary、
未用户验收**，也未连接任何真实系统。
"""

_TABLE_HEAD: Final[str] = (
    "| capability | version | domain | operation | gateway | effect_class "
    "| read_class | side_effect |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- |"
)


def render_capabilities_doc(snapshot: CapabilitySnapshot) -> str:
    """把快照渲染成 Markdown。输出对同一快照恒定。"""
    lines = [_HEADER, "", f"快照标识：`{snapshot.snapshot_id}`", "", _TABLE_HEAD]
    for spec in snapshot.specs:
        for operation in spec.operations:
            lines.append(
                f"| `{spec.capability_id}` | `{spec.version}` | `{spec.domain}` "
                f"| `{operation.operation}` | `{operation.gateway}` "
                f"| `{operation.effect_class.value}` "
                # read_class 决定一次读取是否被 Admission 放行（ADR-017 / I1-C）。
                # 能力地图漏掉它，读者就无法从这份表分辨 BOUNDED 与 RESTRICTED。
                f"| `{'-' if operation.read_class is None else operation.read_class.value}` "
                f"| `{str(operation.side_effect).lower()}` |"
            )
    lines.extend(
        [
            "",
            "## 契约引用",
            "",
        ]
    )
    for spec in snapshot.specs:
        lines.extend(
            [
                f"### `{spec.capability_id}`",
                "",
                f"- policy profile：`{spec.policy_profile}`",
                f"- evidence contract：`{spec.evidence_contract}`",
                f"- eval：`{spec.eval_ref}`",
                "",
            ]
        )
    return "\n".join(lines)
