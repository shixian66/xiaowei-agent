# 能力地图

> **本文件由 `xiaowei_agent.capabilities.doc.render_capabilities_doc()` 生成，
> 由 `tests/security/test_capabilities_doc.py` 检查一致性。不要手工编辑。**

能力状态目前最强为 `tests`：已有确定性测试与离线 eval，**未部署、未 canary、
未用户验收**，也未连接任何真实系统。


快照标识：`snapshot.m6a.starrocks-prometheus-asset.v1`

| capability | version | domain | operation | gateway | effect_class | read_class | side_effect |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `starrocks.slow_query.diagnose` | `1.0.0` | `starrocks` | `list_slow_queries` | `starrocks` | `read` | `bounded` | `false` |
| `starrocks.slow_query.diagnose` | `1.0.0` | `starrocks` | `count_queries_in_window` | `starrocks` | `read` | `bounded` | `false` |
| `prometheus.alert.evidence` | `1.0.0` | `prometheus` | `get_active_alerts` | `alertmanager` | `read` | `bounded` | `false` |
| `prometheus.alert.evidence` | `1.0.0` | `prometheus` | `query_metric_range` | `prometheus` | `read` | `bounded` | `false` |
| `asset.inventory.lookup` | `1.0.0` | `asset` | `lookup_asset` | `asset_inventory` | `read` | `bounded` | `false` |

## 契约引用

### `starrocks.slow_query.diagnose`

- policy profile：`readonly.starrocks.slow_query.v1`
- evidence contract：`evidence.starrocks.slow_query.v1`
- eval：`evals.starrocks.slow_query.v1`

### `prometheus.alert.evidence`

- policy profile：`readonly.prometheus.alert.evidence.v1`
- evidence contract：`evidence.prometheus.alert.v1`
- eval：`evals.prometheus.alert.v1`

### `asset.inventory.lookup`

- policy profile：`readonly.asset.inventory.lookup.v1`
- evidence contract：`evidence.asset.inventory.v1`
- eval：`evals.asset.inventory.v1`
