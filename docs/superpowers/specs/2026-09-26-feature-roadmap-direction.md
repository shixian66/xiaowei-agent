# 小维新新功能与平台方向（Direction V0.2，已定方向、暂缓实施）

> **状态**：用户 2026-09-26 在会话中确定方向，暂缓实施；额度恢复后再排期。本次修订在 PR #100 的
> Direction V0.1 基础上补记部署形态和对话能力两个平台方向。本文只记录方向、顺序和待拍板问题，**不是**
> 详细计划，不授权任何源码、migration、Compose、真实连接、部署或 E1。每一项开工前仍须先写详细计划、经
> exact-SHA 独立复审并取得开工口令；真实调用另需各自现场 GO。
> 当前进度与证据只看 [当前状态](../../../AGENT_HANDOFF.md#current-status)。

## 1. 已定方向

1. **不做旧小维能力整体迁移。** 旧项目的实现（关键词路由、文件型导出/审批/访问存储、完整 rows 写入
   trace/会话等）与新架构不相容，不搬代码，也不以“功能对齐 51 项”为目标。
2. **按当前真实需求重新设计。** 每个功能都从新架构的契约链路重新做：CapabilitySpec → 确定性 Planner →
   Policy / SQLGuard → ToolGateway adapter → Evidence → Render → Eval；审批与导出走 ADR 固定的治理组件。
3. **旧能力清单只作需求备忘。** 排优先级时用来对照有无常用功能遗漏、参考当年踩过的坑；它不是路线、
   不是验收标准。本地未跟踪的旧迁移草案（`legacy-capability-migration-matrix.md`、
   `development-route-v3-proposal.md`）不再作为候选路线推进。
4. **第一优先是“数据库查询 → 结果导出 → 导出审批”**，其次是 Admin 查看任务/聊天内容。
5. **部署形态方向**：正式实施时目标收敛为两个容器：`postgres` 与一个 `xiaowei` 容器；
   `web-app`、`api`、`worker`、`feishu-listener`、`channel-worker` 五个进程暂同容器运行。HTTPS 与域名入口
   由公司网关负责，不在 Xiaowei Compose 内增加 edge。现有分容器编排作为迁移前事实保留；单进程故障导致整容器
   重启、worker 无法单独扩容、listener 单实例等权衡已知并接受，具体实施仍需详细计划与独立验收。
6. **对话能力横向方向**：提升小维与用户的自然沟通、缺槽澄清、有限多轮任务上下文、进度/结果说明，以及
   通用、知识、运维和诊断请求的分流能力。它不新增独立的 F 编号，不改变 F1–F5 的依赖顺序；涉及真实系统
   的请求仍必须回到现有确定性安全链，不授权模型直接选择工具、目标或执行。正式实施前需单独设计、评测和验收。

## 2. 当前事实（main `28ff497`，只读核对）

- 能力只有 `starrocks.slow_query.diagnose`、`prometheus.alert.evidence`、`asset.inventory.lookup` 三个只读
  能力，证据上限 `tests`。
- **没有通用数据库查询**：M6b 只为慢查询诊断接了真实 StarRocks adapter，只支持 list/count 两个固定
  operation，默认关闭，`test` 目标仍是占位，从未连接真实库。
- **没有结果导出与导出审批**：`src/` 无查询结果 artifact、导出或 CSV 代码；运行时装配
  `NeverGrantingApprovalGate`；ADR-005 未编写。设计规格只固定了规则“结果预览/导出仅限该次申请人与
  审批人”，实现归独立阻塞门 R1。
- **没有 Admin 查看聊天/任务内容**：`AdminCapability.VIEW_PRIVATE_TASK_CONTENT` 已定义但无消费者；
  Admin 目前只在任务列表看到全部安全摘要与前 240 字原文预览，且不写查看审计。

## 3. 路线（按依赖顺序）

| 序 | 功能 | 要做什么 | 复用 | 主要前置 / 门 |
| --- | --- | --- | --- | --- |
| F1 | 数据库只读查询 | 先 StarRocks：列库、列表、建表语句/字段、有界预览、精确计数；确定性 SQL 模板 + AST 校验，不接受任意 SQL | M6b 的 target-bound adapter、preflight、SQLGuard、超时与 Evidence 归属 | ADR-007 新增能力与调用许可；真实库需 RI4 式现场 GO；release 需“真实能力准入”设计（总控计划 C2/C3） |
| F2 | 审批规则（ADR-005） | 审批主体、渠道（Web/飞书）、有效期、拒绝/过期/冲突、可否自审；**导出审批与 E1 写审批分成不同 policy profile** | 现有 `ApprovalGate`、`ApprovalRequest` 绑定、Admin 审计 | 负责人拍板第 4 节问题 |
| F3 | 结果域（R1） | 不可枚举 `result_ref`、有界结果 artifact、requester/approver ACL 唯一真源、保留与删除 | TaskStore、EvidenceLedger 的持久化与 fencing 模式 | F1 产生真实结果；F2 冻结审批语义；数据保留/脱敏获批 |
| F4 | 导出 + 导出审批 | 申请导出 → 审批人在 Web/飞书批准 → 短期单用途下载授权 → 过期清理；CSV 公式注入防护、行数/字节预算；结果页只对申请人/审批人可见 | W1b/W3 的审批 UI 与两阶段审计模式、M7 飞书卡片投影 | F1–F3 完成；Admin 不因角色越过结果 ACL |
| F5 | Admin 查看任务/聊天内容 | 按人/按任务查看完整提交原文与安全结果；**每次查看写独立 Admin 审计**；不扩展到数据库真实结果行 | `VIEW_PRIVATE_TASK_CONTENT`、`AdminAuditStore`、`/admin` shell | 先定“聊天记录”真源（任务提交原文 / 飞书私聊事件），当前不保存普通对话历史 |

F5 与 F1–F4 无依赖，可在额度允许时先做；F1–F4 必须按序。

## 4. 开工前需要用户拍板的问题

1. F1 先支持哪些数据库：只 StarRocks，还是同时 MySQL / TiDB？
2. 查询范围：允许哪些库/表、预览最大行数、是否需要按表或字段脱敏？
3. 谁能审批导出：Admin、指定 DBA，还是按资源绑定审批人？能否自己审批自己的导出？
4. 在哪审批：Web、飞书卡片，还是两者都要？审批有效期多久？
5. 导出格式与上限：只 CSV？最大行数/文件大小？下载链接多久失效、文件保留几天？
6. F5 的“聊天记录”指什么：Web/飞书提交给小维的任务原文，还是飞书私聊全部消息？保留多久？

## 5. 其余候选（未排序，待用户按需求挑选）

StarRocks 其他运维查询（当前查询、profile、节点、导入、物化视图等）、巡检与报告、Dinky/Flink 只读诊断、
Jenkins 只读状态、告警主动诊断、I3 资料查询、I4 用户日志分析、任务取消/重跑、DBA/值班职责与可靠通知。
每项入选时单独立项，不从本文外推授权。

## 6. 与既有文档的关系

- 不修改 `DEVELOPMENT_PLAN.md` 的里程碑与退出门；R1、M8、W4c、RI4 仍是独立阻塞门，本文只给出建议顺序。
- 与 [完整功能部署验收总控计划](../plans/2026-09-25-full-feature-deployment-acceptance.md) 的 C1–C10 缺口
  并行存在：本文补的是“新增业务功能”方向，总控计划补的是“已有能力真实接入与发布”。
- E1（修改被管运维目标）不因本路线开放；导出是内部数据产物，但仍受导出 policy、审批与审计约束。
