# 验证入口与能力边界

验证只使用可复用脚本与专用夹具。逐次输出保留终端、临时目录或 CI artifact，不提交日志、运行快照、测试数据库或阶段验收报告。缺少工具、进程失败、missing 或 ignored 均不能作为必需范围通过的证据。

## 检查入口

| 入口 | 范围 | 前提 |
|---|---|---|
| `./scripts/check.sh` | 依赖身份、Python门禁、fmt/check/Clippy、workspace测试、Rustdoc、三个独立构建、正式协议/信号退出、MQTT专项及local契约 | 同级业务库、Rust1.94、Python3.11+、locked Cargo缓存、匹配的libduckdb.so |
| `./scripts/check-federation.sh …` | 正式fedengine-server/datahub-server的PG/MySQL、并发MQTT及remote契约 | 已构建的正式binary、Docker、固定扩展与动态库 |
| `./scripts/check-workflow.sh … --jdbc-jar /absolute/postgresql-42.7.8.jar` | 两个正式数据库宿主的完整管理生命周期、真实 JDBC 目录/读取与组合 MQTT | 正式 binary、Docker、固定扩展/动态库、校验匹配的 JDBC 驱动和固定 JDK 镜像 |
| `./scripts/check-images.sh --tag-prefix PREFIX` | 三个镜像的原生依赖隔离、非root/只读运行、协议/真实远端/排空与原卷重开 | 已构建的三个镜像与Docker |
| `python3 scripts/verify-compose.py --tag-prefix PREFIX` | 默认Compose三实例、六个回环端口、外部协议探针与信号退出 | Docker Compose、三个镜像、开发端口未被占用 |
| `python3 scripts/verify-source-organization.py --plan PLAN --management PORT --pgwire PORT` | 原生库分组、同库贡献合并、Schema 原名、HTTP/PGWire 目录一致与已激活源实际读取 | 显式本地部署计划、已运行服务及环境中的 PG 登录口令 |

`CARGO_TARGET_DIR` 可复用构建缓存；`DUCKDB_LIBRARY_DIR` 可指定匹配的库目录。未指定时，协议冒烟仅在当前target的duckdb-download中查找唯一匹配库。三个normal/build依赖闭包分别检查，独立mqtt-server不得包含FedEngine/DuckDB，公共app/config不得依赖业务库。

## 本地协议与宿主

Rust集成测试直接使用两库公开Facade，在临时目录创建和清理当前管理状态。覆盖纯注册无I/O、同App公开STATUS/Client、未选库零资源、多Application隔离、预算/监听冲突、部分入口激活失败和观察失败补偿。受管测试Module制造真实任务错误验证FailFast及旧Scheduler/Client失效；生产服务没有故障注入或强制释放入口。

Task与Blocking I/O准入饱和要求明确Backpressure、任务数有界、MQTT STATUS仍能响应，释放后查询和排空恢复。慢HTTP请求验证StopIncomplete后保留同一个Application继续等待。

`scripts/smoke-services.py` 从外部CWD运行三个正式binary，用临时目录、随机端口和临时口令检查配置无资源副作用、错误脱敏、MQTT心跳、PG SCRAM/查询、HTTP、SIGINT/SIGTERM及原状态重开。`scripts/verify-mqtt-service.py` 进一步检查MQTT3.1.1 QoS0/1/2、订阅、Retained/删除、Will、持久/clean重连、连接满额后存量连接可用及停机后无DB文件。

## 一次性PG/MySQL与联合验收

```sh
./scripts/check-federation.sh --bin-dir /absolute/target/debug \
  --extensions-dir /absolute/prepared-extensions \
  --duckdb-library-dir /absolute/target/duckdb-download/x86_64-unknown-linux-gnu/v2.0.0
```

入口只创建随机名、随机回环端口的一次性PG17.7/MySQL5.7.44容器，不接受用户业务DSN。固定镜像digest、DuckDB2.0.0及扩展校验和见[sources.json](../scripts/acceptance/sources.json)，准备资源见[部署手册](deployment.md)。输出记录平台、三个Git HEAD/dirty、锁/源码身份和实际binary摘要。

正式PG覆盖正确/错误SCRAM、参数/NULL、Portal挂起/续取/Close、CancelRequest和连接复用。真实PG/MySQL执行参数化CRUD、跨源JOIN、CREATE/DROP，与原生客户端对照写入结果并检查目录刷新；对象授权、跨源写和精确DDL能力门禁必须拒绝对应不支持操作。

管理验证虚拟库/源CAS、同key回执、过期revision409/40001、原DB重开保持API状态。PG完成/Close/Cancel回执不保证原生租约已释放：仅对明确的发布前409/55P03保持同key有界重试，未知写入不重放。Portal持源租约时disable等待，Close后完成并apply恢复。

组合服务保持两个MQTT客户端持续QoS0/1/2收发，在真实单源写、跨源JOIN、源排空和原生等待/取消期间检查进展。慢写夹具通过PG触发器、非事务序列及advisory lock确认实际进入原生等待：取消57014后worker仍存活，至少两次StopIncomplete且宿主仍存活，解锁后排空。序列值1确认不重放；最终写入0或1保留unknown语义。停止后端口可重绑，原DB重启保持虚拟库OID/revision。

公开Client远端Rust用例在普通本地测试中显式ignored，仅由带owned-fixture marker的一次性配置驱动。验证同App跨源结果、SQL对象拒绝42501、管理默认拒绝与精确scope、clone共享max_in_flight=1的53300拒绝、期间MQTT STATUS，以及最终tasks/workers归零。普通本地ignored不计为通过。

## 整体管理流程与 JDBC

```sh
./scripts/check-workflow.sh --bin-dir /absolute/target/debug \
  --extensions-dir /absolute/prepared-extensions \
  --duckdb-library-dir /absolute/target/duckdb-download/x86_64-unknown-linux-gnu/v2.0.0 \
  --jdbc-jar /absolute/postgresql-42.7.8.jar
```

此入口只创建一次性 PG/MySQL，不接受业务 DSN，不修改常驻服务、配置或数据卷。通过两个正式 binary 的管理 HTTP 和 PGWire 验证：实例多库发现且无登记副作用、登记/探测/发现 Schema、PATCH 与 Schema 选择、CAS/幂等重放、激活、原生库拆分/同库合并、目录分页与详情、只读 SQL 任务/取消、连接与对象权限、刷新、贡献去重/移除、停用/恢复、同状态重启与持久操作历史、删除本地登记且原生数据仍在。缺失凭据夹具必须明确 Failed/Degraded，不能发布为可用贡献。组合服务全过程检查 MQTT QoS0/1/2 进展，最终分别以 SIGTERM/SIGINT 排空。

重启可生成新采集快照及独立的 `startup-reconciliation` 挂载恢复记录；验收保留全部旧操作内容/幂等回执，并校验新增记录的来源、目标 revision 和成功/失败状态。库/Schema/对象 OID、配置、revision 及对象身份 epoch 必须保持（本入口使用默认 preserve 身份策略），不能用快照指针变化解释身份变化或未知 SQL 写入重放。

真实 Java `ServerCatalogProbe` 复用 FedEngine 的客户端探针和 `tests/fixtures/metadata/clients.json` 中锁定的 JDBC SHA/JDK digest；驱动文件由调用方预先下载并校验，JDK 通过 Docker 使用，不要求安装本机 Java 或 DBeaver。检查 getCatalogs/getSchemas/getTables/getColumns、目录与 pg_class 一致、业务样本读取、内部对象及不存在库拒绝。JDBC 成功不等于 DBeaver/Web GUI 实际操作成功；GUI 环境不可用时保持 NotRun，不能以 HTTP 流程替代该结论。

本轮优先验证整体执行流程。已经明确需要更新扫描/扩展模块的数据准确性（数值保真、类型、结构变化缓存等）单列后续专项，不通过修改业务数据、放宽只读或更换后端绕过。实际来源无法激活属于兼容/可用性阻断，必须继续列出失败来源；可用源和一次性夹具流程通过不意味着全部来源或数据准确性已通过。

## 契约证据门禁

[映射](../tests/contracts/mapping.json)要求每个ID的所有scope证据；[门禁说明](../tests/contracts/README.md)解释真实成功行与进程退出码的关系。runner先要求成功退出，再核对精确`test NAME ... ok`行，不能用单个scope或静态映射放行全部交付。

| scope | 规则 |
|---|---|
| local | MDH-COMP-001/002/006/007/009/010/011 |
| remote | MDH-COMP-002/003/004/005/006/008/009/012 |
| images | MDH-IMAGES-001，三个镜像完整场景全部结束后输出 |
| workflow | MDH-WORKFLOW-001，两个正式宿主的完整闭环及各自真实 JDBC 四个成功 marker 缺一不可 |

源码/Feature/锁指纹仅在对应改动重新验证后使用`python3 scripts/verify-dependency-identity.py --write-identity`刷新，不能为绕过失败门禁刷新。两个业务库当前使用同级path；正式Git固定与干净远端CI要求真实包含接口的提交，不用旧HEAD占位。

## 验收边界

已实现的验收限定于Linux x86_64、固定版本数据源、开发profile和受控流量。镜像要求UID65532、只读root、独立数据卷、固定base/native/扩展，并验证真实协议、慢原生I/O和原卷重开。Compose及外部协议探针见[部署手册](deployment.md)。CI在push/PR运行源码检查，完整集成手动触发并要求两个实际业务SHA；其同级checkout是过渡验证入口，不能替代正式Git依赖，自动源码检查不能代替联合执行证据。

生产容量/RSS/长时、release profile、其他平台、永久卡死/MySQL全部故障、完整公网保护、registry发布、MQTT可选扩展及业务桥接继续独立验收。协议冒烟不代表完整规范或性能SLO；Hub逻辑成本不等于进程RSS，StopIncomplete等待窗口不保证总退出期限。
