# W5 provider-off 产品壳部署 Runbook

> 适用范围：W5-C 在**获准目标主机**上部署 provider-off 产品壳。本文件是离线资产；它存在不表示
> 已取得部署 GO，也不表示已部署。执行前必须有负责人书面的 W5-C GO（见 §1）。

## 0. 本轮是什么、不是什么

- 是：固定作用域 `dev-local/dev`、`runtime_profile=release`、`starrocks_adapter_mode=disabled`
  的产品壳。Local Admin 登录/强制改密、Admin 页面、资源登记、工作台如实显示“当前无可执行能力”。
- 不是：RI2（真实飞书）、RI3（Gemini）、RI4（StarRocks 只读）、H 层授权或 RI6（正式只读发布验收）。
  **W5 GO 不代替任何真实调用 GO。** 本 runbook 里没有打开任何 Provider/target 的步骤，
  `docker-compose.release.yml` 把这些开关写成字面 `false`，Settings 在 release 下也拒绝它们。
- 证据分三层、不能合并：`deployed SHA`（§3 完成）→ `canary`（§5）→ `user-accepted`（§6）。
  本地 Docker、CI Compose、一次性 PostgreSQL、截图或 `readyz` 都不是部署/canary/UAT 证据。
- 没有 edge 运行证据的 LAN 访问只能算**本地体验，不是 canary**；`lan_http` 不可晋升 canary。

## 1. 开工前：冻结执行输入（W5-C Task 10）

负责人书面指定并写进 [部署证据清单](../checklists/w5-deployment-evidence.md)：

- 目标主机、维护窗口、回滚 owner、canary 验收人、DB 与三域配置的备份位置；
- release source SHA 与不可变镜像 `name@sha256:<64 hex>`；
- HTTPS edge 与其限流证据引用（见 [edge 清单](../checklists/w5-edge-rate-limit-evidence.md)）；
- **回滚目标**：已有 W5-compatible release 时写下它的不可变 digest；首次部署写“停止全部应用服务”。
  不得把 W5 前的 recording 镜像列作恢复目标；
- 首次 release 的历史数据处置：在“**新数据库**”与“**单独批准的数据处置**”之间书面二选一，
  并保存 §3 第 6 步的预检结果。本 runbook **不提供**删除历史 plan/evidence 的便捷命令，
  也不允许用临时 SQL 绕过。

RI2、RI3、RI4、H 层与 RI6 在本次执行中全部保持 NO-GO；即使某项另行取得 GO，也要退出本 runbook，
先形成对应的真实 release 设计与复审。

核对目标主机 Docker/Compose 版本（Compose ≥ 2.24.4）、磁盘、端口、DNS/NTP、证书链与
Secret/config 文件权限；记录时脱敏。

## 2. 准备 shell 与部署模板

1. 复制 [部署模板](../examples/w5-release.env.example) 到**仓库外**的私有路径并填写六个键，
   不提交真实值：

   ```bash
   export RELEASE_ENV=/srv/xiaowei-private/w5-release.env
   ```

2. 使用干净 shell：Compose 让 shell 环境优先于 `--env-file`，残留的 `XIAOWEI_*` / `COMPOSE_*`
   会让检查过的值与实际部署的值不是同一份。下面这条必须没有输出：

   ```bash
   env | grep -E '^(XIAOWEI_|COMPOSE_)'
   ```

3. 定义唯一合法的 release 文件集合（沿用 README 的 `compose` 探测函数）：

   ```bash
   release() {
     compose --env-file "$RELEASE_ENV" \
       -f docker-compose.yml -f docker-compose.release.yml "$@"
   }
   ```

   不叠加 `docker-compose.lan.yml`、`docker-compose.model.yml`、`docker-compose.smoke.yml`、
   `docker-compose.barrier.yml` 或 `docker-compose.m6b-test.yml`，也不加 `--profile m7-channels`。

4. 部署前检查（只输出一行闭集结果，绝不打印解析后的配置）：

   ```bash
   python -m scripts.release_compose --env-file "$RELEASE_ENV"
   release config --quiet
   ```

   只有 `release-compose: ok` 可以继续。禁止 `config --environment` 或输出完整解析配置。
   检查器直接复用运行时的 Web 绑定地址与 public origin 规则，需在已安装项目依赖的仓库检出里
   运行（与 `python -m scripts.compose_smoke` 同一环境）。

## 3. 备份、迁移、预检与首次启动（W5-C Task 11）

执行顺序固定。**任一步失败即停止并按 §4 回滚**；禁止在线改源码。

1. **恢复点**。记录旧服务/进程；取得数据库与 `.config/{ai,feishu,resources}` 的恢复点并记录位置。
   证明旧 listener 已停（避免双消费）。
2. **镜像**。`release pull`，核对容器将使用的镜像就是模板里的 digest；再跑一次 §2 第 4 步。
3. **数据库**。`release up -d --wait postgres`（若由本 Compose 管理），然后
   `release up --no-deps migrate`，确认退出码 0。schema 只前进，不做普通 downgrade。
4. **三域配置**。从 RI5 单文件升级的主机先停止长期进程，再显式迁移旧 `integrations.json` 并预检：

   ```bash
   release stop web-app worker
   release run --rm --no-deps -v "$PWD/.config:/run/xiaowei-config" \
     web-app python -m xiaowei_agent.interfaces.integration_config_migrate
   release run --rm --no-deps -v "$PWD/.config:/run/xiaowei-config" \
     web-app python -m xiaowei_agent.interfaces.config_preflight
   ```

   预检只接受 `preflight: ok`。

5. **旧身份（一次性）**。只有确有旧文档时才以只读临时挂载运行，并**连续运行两次**：

   ```bash
   release run --rm --no-deps \
     -v "/srv/xiaowei-private/feishu-identities.json:/run/xiaowei-legacy/feishu-identities.json:ro" \
     migrate python -m xiaowei_agent.interfaces.legacy_identity_migration
   ```

   只保存 count-only 结果：首次 `created_count/skipped_count/deferred_count`，第二次必须
   `created_count=0`。不保存 raw actor、subject 或 deferred label。没有旧文档时不挂载，记录
   `{"status": "not_applicable"}`，不要造空文件。命令退出后没有容器继续持有该挂载；旧文件离线
   隔离保管一个发布周期，下一次正式发布前由 owner 确认销毁或继续隔离。
6. **保留清理与历史门**。

   ```bash
   release run --rm --no-deps migrate python -m xiaowei_agent.interfaces.activation_retention
   release run --rm --no-deps -v "$PWD/.config:/run/xiaowei-config" \
     migrate python -m xiaowei_agent.interfaces.release_preflight
   ```

   保留清理是全库所有作用域、固定 30 天，只输出四个计数；确认 owner 已配置**每日调度与失败告警**，
   否则不得晋升 canary。发布预检只有 `result=ok` 才能继续：`tasks_not_drained` 先排空；
   `historical_execution_data_present` 立即停止，按 §1 已批准的新数据库/数据处置决定执行。
7. **启动**。只启动三个长期进程，**不启动** listener/channel-worker：

   ```bash
   release up -d --wait --no-deps api worker web-app
   ```

   核对并记录：health/readiness、三个容器的 image 等于模板 digest、容器标签
   `io.xiaowei.release.source-sha` 等于 source SHA、端口面（只有 web-app 发布在模板地址的 8080，
   api/worker/postgres 无宿主端口）、配置 generation/load receipt，以及所有 Provider/target 开关
   仍为 `false/disabled`。
8. **首登改密**。经 HTTPS edge 用 `admin/admin` 首登并立即完成强制改密；确认旧 Session 失效。
   维护窗口内 edge 只放行验收人；改密前可达是已接受风险，不表示可以跳过改密。

完成本节只能标记 `W5 provider-off product-shell deployed SHA`。

## 4. 回滚

回滚只回滚**镜像与非秘密配置**；数据库 schema 默认 forward-only。

- 回滚目标必须是**更早的、已经支持 W5 release 契约的不可变 digest**。反向切换前同样运行
  drain/history 预检（§3 第 6 步），核对 readback 后才启动。
- **首次部署**不存在安全的旧 release digest：回滚定义为**停止全部应用服务**，保留数据库与证据
  等待修复：

  ```bash
  release stop api worker web-app
  ```

- 任何情况下都**不得回到 recording 镜像**，也不得把 `XIAOWEI_RUNTIME_PROFILE` 改回
  `offline_recording` 来“恢复服务”——那会让 fake/recording 往发布数据库里写合成执行事实。

真实用户进入前必须做一次受控回滚演练：有 W5-compatible 旧 digest 才演练镜像回退；首次部署只演练
停止应用服务再恢复候选。演练结果写进 [canary 清单](../checklists/w5-canary-evidence.md)。

## 5. canary（W5-C Task 12）

前置：edge 五组路由的正常、429、窗口恢复与告警证据齐全（缺一项不得开始）；回滚演练已完成。
应用内的 OAuth state 容量与 Activation 容量**不是**限流，不能拿来冒充 edge rate limit。

- 只开放给指定的 Local Admin 验收人；固定时间窗、观测项与停止条件。
- 观察：本地登录/强制改密、Admin 页面、空能力工作台、进程重启、DB、edge 拒绝和配置 readback。
- OAuth、群消息、模型与资源目标不得进入观测流量；命中停止条件立即按 §4 回滚。

## 6. UAT

只验 provider-off 功能矩阵，见 [UAT 清单](../checklists/w5-user-acceptance.md)。Operator、普通用户、群
与真实 OAuth/Provider 路径本轮记为**不适用**，不能用假 Session 或合成结果补齐角色矩阵。

三层证据分别签署后更新 handoff；三层齐备也只允许归档 W5 产品壳，不得写 RI6 或只读 V1 完成。
