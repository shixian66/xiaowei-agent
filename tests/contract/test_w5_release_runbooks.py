"""W5 runbook 与证据清单的契约：把计划 Task 8 的每条运维承诺钉在文档上。

这些文档是 W5-C 的执行依据。它们一旦丢掉"不得回到 recording"、"首次部署停服"或
"LAN 不是 canary"这类句子，照做的人就可能把合成执行事实写进发布数据库，或把本地体验
登记成 canary。CI 全绿看不出错误的运维指令，所以由这里正向钉住。
"""

import re
from pathlib import Path

from scripts import release_compose

_ROOT = Path(__file__).resolve().parents[2]
_RUNBOOK = "docs/runbooks/w5-product-deployment.md"
_EDGE = "docs/checklists/w5-edge-rate-limit-evidence.md"
_DEPLOYMENT = "docs/checklists/w5-deployment-evidence.md"
_CANARY = "docs/checklists/w5-canary-evidence.md"
_UAT = "docs/checklists/w5-user-acceptance.md"


def _text(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = re.search(r"\n## ", text[start + len(heading) :])
    end = len(text) if following is None else start + len(heading) + following.start()
    return text[start:end]


def test_every_linked_document_exists() -> None:
    documents = (_RUNBOOK, _EDGE, _DEPLOYMENT, _CANARY, _UAT)
    for name in (*documents, "docs/examples/w5-release.env.example"):
        assert (_ROOT / name).is_file(), name
    for source in documents:
        for target in re.findall(r"\]\(([^)#]+)\)", _text(source)):
            assert (_ROOT / source).parent.joinpath(target).resolve().is_file(), (source, target)


def test_edge_checklist_covers_the_five_route_groups_and_log_hygiene() -> None:
    edge = _text(_EDGE)
    for route in (
        "/login/api/login",
        "/oauth/feishu/start",
        "/oauth/feishu/callback",
        "/admin/api/activations/approve",
        "/admin/api/activations/reject",
    ):
        assert f"| `{route}` |" in edge, route
    for column in ("正常请求", "429 反例", "窗口恢复", "告警触发", "owner"):
        assert column in edge
    assert "可信 client-IP 链" in edge
    assert "edge 产品与配置版本" in edge
    hygiene = _section(edge, "## 日志禁止项")
    forbidden_fields = (
        "query",
        "`code`",
        "`state`",
        "Cookie",
        "密码",
        "Secret",
        "subject",
        "请求正文",
    )
    for forbidden in forbidden_fields:
        assert forbidden in hygiene, forbidden
    assert "不能拿来冒充 edge rate limit" in edge


def test_deployment_checklist_records_each_fact_separately() -> None:
    deployment = _text(_DEPLOYMENT)
    for item in (
        "source SHA",
        "image digest",
        "Compose 文件集合",
        "generation / readback",
        "DB 备份",
        "migration revision",
        "身份迁移（count-only）",
        "全库保留清理（count-only）",
        "初始改密",
        "health / readiness",
        "回滚 owner",
    ):
        assert item in deployment, item
    assert "不得含 raw actor" in deployment
    assert "新数据库 / 单独批准的数据处置" in deployment
    assert "`docker-compose.yml` + `docker-compose.release.yml`" in deployment


def test_runbook_never_restores_service_through_the_recording_stack() -> None:
    rollback = _section(_text(_RUNBOOK), "## 4. 回滚")
    assert "更早的、已经支持 W5 release 契约的不可变 digest" in rollback
    assert "反向切换前同样运行" in rollback and "drain/history 预检" in rollback
    assert "**首次部署**" in rollback and "**停止全部应用服务**" in rollback
    assert "保留数据库与证据" in rollback
    assert "**不得回到 recording 镜像**" in rollback
    assert "`offline_recording`" in rollback
    assert "forward-only" in rollback
    for forbidden in ("改回 offline_recording 并重启", "回滚即改回"):
        for name in (_RUNBOOK, _CANARY, _DEPLOYMENT):
            assert forbidden not in _text(name), (name, forbidden)
    assert "绝不启动 recording 栈" in _text(_CANARY)


def test_runbook_offers_no_history_deletion_shortcut() -> None:
    runbook = _text(_RUNBOOK)
    assert "**不提供**删除历史 plan/evidence 的便捷命令" in runbook
    assert "不允许用临时 SQL 绕过" in runbook
    lowered = runbook.lower()
    for statement in ("delete from", "truncate", "drop table", "--force"):
        assert statement not in lowered, statement


def test_runbook_uses_only_the_canonical_release_file_set() -> None:
    runbook = _text(_RUNBOOK)
    assert "-f docker-compose.yml -f docker-compose.release.yml" in runbook
    assert "python -m scripts.release_compose --env-file" in runbook
    assert "release-compose: ok" in runbook
    shell = _section(runbook, "## 3. 备份、迁移、预检与首次启动")
    for line in shell.splitlines():
        stripped = line.strip()
        if stripped.startswith(("compose ", "docker compose", "docker-compose")):
            raise AssertionError(f"bypasses the release() wrapper: {stripped}")
    for override in (
        "docker-compose.lan.yml",
        "docker-compose.model.yml",
        "docker-compose.smoke.yml",
        "docker-compose.barrier.yml",
        "docker-compose.m6b-test.yml",
    ):
        assert override in _section(runbook, "## 2. 准备 shell 与部署模板"), override
    assert "不加 `--profile m7-channels`" in runbook
    assert "禁止 `config --environment`" in runbook
    assert "release up -d --wait --no-deps api worker web-app" in runbook


def test_runbook_steps_follow_the_approved_order() -> None:
    steps = _section(_text(_RUNBOOK), "## 3. 备份、迁移、预检与首次启动")
    markers = [
        "**恢复点**",
        "**镜像**",
        "**数据库**",
        "**三域配置**",
        "**旧身份（一次性）**",
        "**保留清理与历史门**",
        "**启动**",
        "**首登改密**",
    ]
    positions = [steps.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert "`W5 provider-off product-shell deployed SHA`" in steps
    assert "**连续运行两次**" in steps and "`created_count=0`" in steps
    assert '`{"status": "not_applicable"}`' in steps
    assert "/run/xiaowei-legacy/feishu-identities.json:ro" in steps
    assert "`historical_execution_data_present` 立即停止" in steps
    assert "**每日调度与失败告警**" in steps
    assert "**不启动** listener/channel-worker" in steps


def test_runbook_keeps_evidence_grades_and_live_gates_apart() -> None:
    runbook = _text(_RUNBOOK)
    assert "**W5 GO 不代替任何真实调用 GO。**" in runbook
    for gate in ("RI2", "RI3", "RI4", "H 层", "RI6"):
        assert gate in runbook, gate
    assert "`deployed SHA`" in runbook and "`canary`" in runbook and "`user-accepted`" in runbook
    assert "**本地体验，不是 canary**" in runbook
    assert "`lan_http` 不可晋升 canary" in runbook
    assert "不能拿来冒充 edge rate limit" in runbook
    assert "不得写 RI6 或只读 V1 完成" in runbook


def test_canary_and_uat_scope_is_provider_off_only() -> None:
    canary = _text(_CANARY)
    uat = _text(_UAT)
    assert "Local Admin 验收人" in canary
    for item in ("时间窗", "停止条件", "空能力工作台", "进程重启", "配置 readback"):
        assert item in canary, item
    assert "OAuth、群消息、模型与资源目标**不得**进入观测流量" in canary
    not_applicable = _section(uat, "## 本轮不适用")
    for path in ("Operator", "普通用户", "OAuth", "群消息", "Gemini", "StarRocks", "深链"):
        assert path in not_applicable, path
    assert "不能用假 Session 或合成结果补齐角色矩阵" in not_applicable
    assert "工作台如实显示当前无可执行能力" in uat
    assert "已保存，尚未接入/未授权" in uat


def test_release_template_example_is_the_closed_key_set_with_no_values() -> None:
    lines = [
        line
        for line in _text("docs/examples/w5-release.env.example").splitlines()
        if line and not line.startswith("#")
    ]
    assert {line.partition("=")[0] for line in lines} == release_compose.RELEASE_TEMPLATE_KEYS
    assert all(line.endswith("=") for line in lines)
