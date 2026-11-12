# 验证入口与能力边界

验证只使用可复用脚本与专用夹具。逐次输出保留终端、临时目录或 CI artifact，不提交日志、运行快照、测试数据库或阶段验收报告。缺少工具、进程失败、missing 或 ignored 均不能作为必需范围通过的证据。

## 检查入口

| 入口 | 范围 | 前提 |
|---|---|---|
| `./scripts/check.sh` | 依赖身份、Python门禁、fmt/check/Clippy、workspace测试、Rustdoc、三个独立构建、正式协议/信号退出、MQTT专项及local契约 | 同级业务库、Rust1.94、Python3.12、locked Cargo缓存、匹配的libduckdb.so |
| `./scripts/check-mqtt-validation.sh` | MQTT 黑盒工具语法、场景目录和负向门禁测试；无需启动服务 | Python3.12；镜像执行另见工具说明 |
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

## 独立 MQTT 黑盒验证

项目内 [tools/mqtt-broker-validation](../tools/mqtt-broker-validation/README.md) 提供受限 MQTT3.1.1 代表性场景、原始帧/Paho/锁定上游 B 类适配、错误对端校准、逐场景进程预算与自有容器清理、运行清单和逐目标门禁。它使用本项目公开开发配置快照和实际不可变 mqtt-server/datahub-server 镜像身份，不构建或发布服务；常驻入口仅执行唯一命名空间的 Paho 功能验证。工具随本项目统一维护，根目录 `.github/workflows/verify-mqtt.yml` 提供源码检查与手动镜像验证；CI 文件存在不代表远端已执行。

产品所选场景可单独设为必需门禁；参考 Mosquitto 的字面 `$SYS` 写入策略阻断仍显示 BLOCKED。场景 PASS 与规范符合性分开：当前仅 23 个稳定用例、9 个内部证据范围，完整官方条款映射/语义与独立审查仍 pending，完整符合性保持 Partial。它补充本项目 `verify-mqtt-service.py` 的宿主与限额验证，不能替代完整规范、容量、扩展或桥接专项。

通用 QoS2 断线恢复覆盖两个发送方向各自 AwaitPubRec/AwaitPubComp 四分支，验证真实 ACK 截断、Session Present、方向内 ID/DUP/内容、单次应用交付与恢复后健康消息；错误对端校准现为11种变异（含错误PINGRESP固定头）。该范围不包括进程/磁盘重启或同号双向并发，详细证据契约见工具说明。

MQTT 工具已提供两个宿主的版本化 Profile 与 Draft 2020-12 Profile/绑定 schema，执行计划、子进程、原始轨迹与结果均校验声明及有效配置摘要。live 入口只观察网络，配置固定标为 unverified；事件（56 种）、worker 原始结果和汇总 schema 已接入产生端及父进程门禁；顺序、重复 JSON 键/重复计划/结果、计划外结果、旧 run 和未核验证据的 PASS 均会被拒绝。当前代表性用例/义务/目录及 RunManifest/Request schema 也已接入产生端、worker 副作用前、结果接收与汇总；执行摘要、输入快照、计划索引、用例、端点、路径及 ownership 交叉核验，拒绝快照漂移和旧请求覆盖，校验失败仍由父进程自有 journal 清理。完整官方义务/证据模型、官方映射与语义/充分性和独立审查仍待完成，M0/M1 整体保持 Partial。

上游首批审计见工具 [upstream-audit.md](../tools/mqtt-broker-validation/upstream-audit.md)：固定三个 revision、21 项选定资产与逐文件/行引用，现有 12 次 Mosquitto B 类调用逐项绑定；Paho Testing 六项和 EMQX 三项仅为静态候选/参考，未集成执行。源码门禁校验审计 schema 与目录/依赖锁/adapter 绑定；手动镜像 Actions 额外按 commit 获取缺失缓存并核验字节/行引用，不执行候选脚本。根通配 Retained 清理、内部运行时耦合和所见许可证表述均保留明确依据。integrated asset 的路径依赖已核对锁定文件；首批初评仍不等于完整依赖闭包、语义/独立审查或 M1 完成。

Retained v2 已补 RETAIN=0 保留旧值、不创建新保留值及合法 QoS1 DUP 的验证，父进程按原始字节重建完整阶段和 ACK/对照/窗口。七个选定官方 ID 的 [原文映射草案](../tools/mqtt-broker-validation/normative-map.md)已绑定固定 OASIS HTML、完整段落和场景版本/断言/阶段；源码门禁核验元数据，手动镜像 Actions 额外核验原文。稳定缺口 MQTT-GAP-001 仅在选定范围实现；共十二份 schema，规范/充分性与独立审查仍 pending，不产生完整符合性结论。

2026-11-10 的 Retained v2 基线：工具 97 项及宿主 15 项测试通过，十二份 schema 通过独立 Draft 2020-12 校验。该轮源码的两个临时隔离镜像、锁定参考及两个常驻网络入口共 57 组执行，56 PASS / 1 BLOCKED / 0 FAIL / 0 ERROR / 0 NOT_TESTED，必需产品/tool/live 门禁退出 0；参考 `$SYS` 正向写入受限仍 BLOCKED。临时镜像不替换常驻部署，live 配置与构建来源仍 unverified，完整 M0/M1 与 F6/F7 保持 Partial。

当前 MQTT 工具剩余交付与建议顺序见[剩余项清单](../tools/mqtt-broker-validation/remaining-work.md)：官方映射、完整义务/证据模型、Oracle充分性、上游资产闭包、参考环境阻断与精确版本/干净CI分别跟踪。前轮清单核对仅重新执行源码门禁及文档检查，未重跑完整e2e，不升级上述限定验证或独立审查状态。

连接v2新增三个有序阶段及本地socket关闭检查；父进程从同一连接原始TX/RX重建完整正常交换，MQTT-GAP-002的旧校验反例已在选定范围修复。连接v2阶段的官方映射为结构版本2、十一个选定ID/八个完整段落（连接四个/Retained七个），仍draft/pending；事件共31种、运行校准十一种。工具105项测试和固定OASIS原文核验已通过。不会将场景自身等待CONNACK的屏障当作所有客户端的规范义务，也不宣称覆盖全部连接拒绝、持久会话、KeepAlive或DISCONNECT行为。

连接v2阶段完整回归：工具105项、宿主15项测试通过；两个当前源码临时镜像、锁定参考及两个常驻入口共57组，56 PASS / 1 BLOCKED / 0 FAIL / 0 ERROR / 0 NOT_TESTED，必需产品/tool/live门禁退出0。参考$SYS写入前提继续BLOCKED。十二份schema及实际请求/结果/事件经独立Draft 2020-12与证据复核，无输入漂移、中断、超时或清理失败；54组自有实例/卷/目录均已回收，三个常驻容器ID保持不变。映射与完整M0/M1、F6/F7及真实独立批准状态不升级。

QoS2 v2新增四个有序分支入口，父进程由原始字节重建十六个连接、持久会话恢复、实际代理ACK截断及应用交付，验证缺口MQTT-GAP-003在选定范围修复。正例保留合法提前转发、完成后Packet ID复用和重复PUBREL；负例拒绝只有标签、缺真实握手/截断/ACK/交付及越过分支边界的证据。观察窗口按完整帧绝对期限执行，半包不能延长窗口。112项工具测试通过；这属于工具验证收口，没有新增已确认的库运行时缺陷。

官方映射最新为结构版本3、十四个选定ID/十个完整正文段落（连接四个、Retained七个、QoS2三个），仍draft/pending。QoS2新增MQTT-2.3.1-6、MQTT-3.2.2-2、MQTT-4.4.0-1，绑定场景2.0.0及四分支；MQTT-4.3.3-1/-2的完整发送方/接收方列表尚未导入审核，不能用孤立编号段落代替完整义务。事件最新共32种，十二份schema、十一种运行校准保持；内部official_ids仍为空，真实独立批准仍0。

QoS2 v2本轮完整回归：工具112项、宿主15项测试通过；两个当前源码临时镜像、锁定参考及两个常驻入口共57组，56 PASS / 1 BLOCKED / 0 FAIL / 0 ERROR / 0 NOT_TESTED，必需产品/tool/live门禁退出0。十二份schema及57份请求/原始结果、6120条事件经独立Draft 2020-12校验；父进程重新校验证据通过，三个隔离目标分别有四分支、十六连接、八次应用交付。源码/输入摘要一致，无中断、超时或清理失败；54组自有实例/卷/目录均已回收，三个常驻容器ID和运行状态保持不变。参考$SYS前提仍BLOCKED；当前脏源码镜像和live观测不证明干净远端CI或匹配部署，完整符合性与独立审查状态不升级。

Topic v2已补固定16个通用/3个系统向量，新增大小写、尾斜线、空层及Unicode不归一化对照。每行包含候选匹配健康消息、独立精确订阅目标正向对照、两次QoS1非保留发布、逐次ACK、两个0.4秒观察及三个clean连接的最终DISCONNECT；父进程从原始字节重建完整交换和控制屏障，MQTT-GAP-004标签/错误向量内容反例在选定范围修复。首次DUP、已知相同交换的DUP及ID复用保留正例，半包、EOF和超时不能成为成功静默。122项工具测试通过，未新增已确认的Broker运行时缺陷。

最新官方草案为结构版本4、十八个选定ID/十三个完整正文段落，新增Topic四个编号（MQTT-3.8.4-1/-2、MQTT-4.7.2-1、MQTT-4.7.3-4）；十二份schema、35种事件、十一种运行校准。合法通配向量不证明非法Filter拒绝，系统$SYS前提独立保留；完整义务、语义/充分性与真实独立审查仍待，内部official_ids=[]，完整M0/M1和F6/F7仍Partial。

Topic v2本轮完整回归：工具122项、宿主15项测试通过；两个当前源码临时镜像、锁定参考及两个常驻入口共57组，56 PASS / 1 BLOCKED / 0 FAIL / 0 ERROR / 0 NOT_TESTED，必需产品/tool/live门禁退出0。十二份schema、57份请求/原始结果及8039条事件经独立Draft 2020-12与父进程证据复核通过；三个隔离目标的通用16行各重建48个连接，两个产品系统3行各重建9个连接。源码/输入摘要一致，无中断、超时或清理失败；54组自有实例/卷/目录已回收，三个常驻容器ID与运行状态保持不变。参考$SYS环境前提继续BLOCKED；临时脏源码镜像与live观测不证明干净远端CI或匹配部署，完整符合性和真实独立批准状态不升级。

QoS2 v3（3.0.0）在两条Client→Broker恢复分支中，原交换收到PUBCOMP后以同一输入ID发送不同健康载荷，并在读取健康PUBREC前排入同ID的DUP PUBLISH；逐次确认两个PUBREC、完成一次PUBREL/PUBCOMP且健康消息只交付一次。父进程按实际帧核验输入/ACK数量和顺序；Broker初始新PUBLISH另校验DUP=0，订阅侧ID仍由Broker独立分配。128项工具测试和参考QoS2专项通过；这些是工具验证扩展，不是新增库运行时修复。

官方来源模型最新结构版本5：十八个选定编号/十三个完整段落不变，另以inventory-only导入MQTT-4.3.3-1/-2完整引言/ul/li/编号及四段上下文。发送方六项七个MUST、接收方四项五个MUST均保留完整列表项和MUST位置，绑定角色、v3分支、父进程检查和明确剩余项；全部partial-selected-evidence/pending。来源门禁拒绝孤立编号、遗漏/乱序列表、附录汇总替代、错误QoS上下文及原子漏项/批准升级。正式边界见[QoS2复合义务](../tools/mqtt-broker-validation/qos2-obligations.md)；两个父编号不增加已映射数量，十二份schema、35种事件、十一种运行校准不变，完整M0/M1、F6/F7及独立批准状态不升级。

QoS2 v3与复合义务本轮完整回归：工具128项、宿主15项测试通过；两个当前源码临时镜像、锁定参考及两个常驻入口共57组，56 PASS / 1 BLOCKED / 0 FAIL / 0 ERROR / 0 NOT_TESTED，必需产品/tool/live门禁退出0。十二份schema、57份请求/原始结果及8082条事件经独立Draft 2020-12与父进程复核通过；三个隔离目标分别有v3四分支、十六连接、八次应用交付，完成后输入ID复用及重复健康PUBLISH/逐次PUBREC由实际帧约束。源码/输入摘要一致，无中断、超时或清理失败；完整回归54组自有实例/卷/目录已回收，连同两个参考诊断共56组资源均不存在；三个常驻容器ID和运行状态保持不变。参考$SYS前提仍BLOCKED。来源清单导入与选定场景通过不替代完整语义、充分性、干净远端CI、匹配部署或独立批准。

QoS2 FLOW v1（MQTT311-QOS2-FLOW-001，1.0.0）新增两个clean连接、两消息同时在途、重复PUBREC/PUBREL逐次响应、完成后输入ID复用及三次单独应用交付，父进程按原始字节重建四阶段和0.4秒窗口。合法ACK置换、Broker ID完成后复用、额外PUBREL有正例；缺字节/重复控制/ACK/交付、错误顺序、半包/EOF及不足观察均不能PASS。只支持串行发送合法，缺选定出站窗口前提为BLOCKED，不能作为协议违规。来源结构v6为12个复合原子中的11项增加FLOW补充绑定，全部仍partial-selected-evidence/pending；十八个选定编号、独立批准0及完整Partial状态不变。140项工具测试、15项宿主测试通过，库运行时未改。前轮工具20用例、8个内部范围、12份schema、38种事件、11种运行校准。

FLOW v1本轮完整回归：140项工具、15项宿主测试通过；最新临时镜像、锁定参考及两个常驻入口共60组，59 PASS / 1 BLOCKED / 0 FAIL / 0 ERROR / 0 NOT_TESTED，必需产品/tool/live门禁退出0。十二份schema、60份请求/原始结果和8519条事件经独立Draft 2020-12与父进程复核通过；三个隔离目标的新FLOW各有两个连接、三次应用交付，旧恢复v3四分支/十六连接/八次交付保持通过。源码/输入摘要一致，无中断、超时、漂移或清理失败；57组完整回归自有实例/卷/目录及一组参考专项资源均已回收，三个常驻容器ID和运行状态不变。参考$SYS前提仍BLOCKED；本轮无新增已确认的库运行时缺陷，未形成干净远端CI、匹配常驻部署或真实独立批准，完整符合性仍Partial。

FANOUT v1（MQTT311-QOS2-FANOUT-001，1.0.0）补一个发布者、两个独立clean订阅者的QoS2扇出：读取PUBREC前排入原始及同ID/DUP重复PUBLISH，逐次确认；每个订阅者各一次原始/健康交付，健康输入复用已完成ID并反向完成订阅者交换。三连接、四次交付、五阶段、每订阅者明确起止且各0.4秒观察和最终DISCONNECT均从原始字节重建，不能跨连接借用ACK、ID或交付。相同/不同跨连接Broker ID、完成后复用、合法提前转发和重投保留正例；不要求跨订阅者同时在途或特定输入ACK分发边界。来源v7给接收方五个复合原子追加FANOUT补充绑定，18个选定编号及独立批准0不变，完整M0/M1和F6/F7仍Partial。153项工具、15项宿主测试通过；本轮未改库运行时。前轮21用例、8个内部范围、12份schema、43种事件及11种运行校准。

FANOUT v1本轮完整回归：153项工具、15项宿主测试通过；最新临时mqtt-server/datahub-server镜像、锁定参考及两个常驻入口共63组，62 PASS / 1 BLOCKED / 0 FAIL / 0 ERROR / 0 NOT_TESTED，必需产品/tool/live门禁退出0。十二份schema、63份请求/原始结果及8959条事件通过独立Draft 2020-12与父进程复核；三个隔离目标的新FANOUT分别有三连接、四次应用交付、五阶段及每订阅者独立观察起止，原恢复v3和FLOW v1保持通过。源码/输入摘要一致，无中断、超时、漂移或清理失败；60组完整回归自有实例/卷/目录和一组前期参考诊断资源均已回收，三个常驻容器ID与Running保持不变。前期诊断不替代最终用例证据，最终参考结果以本次完整回归为准。参考$SYS前提仍BLOCKED；完整M0/M1、F6/F7、干净远端CI、匹配常驻部署和独立批准状态不升级，当前无新增已确认的库运行时缺陷。

DUPLEX v1（MQTT311-QOS2-DUPLEX-001，1.0.0）新增同一连接双向同号的两个正常完成顺序。owner读取Broker实际出站ID且不确认，再以同ID发起反向QoS2交换并收到PUBREC；分别先完成输入方向或输出方向，另一方向仍完成。随后输入方向复用已完成ID发送不同健康内容，并验证反向健康交付；共四个clean连接、八次应用交付、每分支五阶段及每连接独立起止/0.4秒观察。父进程以独立发送/接收命名空间逐字节重建，ACK不能因ID相同而互相替代；合法提前转发、重投、重复PUBREL及完成后Broker ID复用均保留正例。来源v8只给三个复合原子追加DUPLEX角色/版本/两分支/五阶段绑定，18个选定ID/13个段落与独立批准0不变。167项工具、15项宿主测试通过；该轮为22用例/8内部范围、12份schema、49种事件、11种运行校准。本轮未改库运行时；库原有同号双向故障矩阵未重新执行，选定正常流程通过不升级完整M0/M1或F6/F7。

DUPLEX v1本轮完整回归：167项工具、15项宿主测试通过；最新临时mqtt-server/datahub-server镜像、锁定参考及两个常驻入口共66组，65 PASS / 1 BLOCKED / 0 FAIL / 0 ERROR / 0 NOT_TESTED，必需产品/tool/live门禁退出0。十二份schema、66份请求/原始结果及9885条事件通过独立Draft 2020-12和父进程复核；三个隔离目标的新DUPLEX各有两个完成顺序、四连接、八次应用交付、十个阶段事件和四个独立观察窗口，旧恢复v3/FLOW/FANOUT保持通过。源码/输入摘要一致，无中断、超时、漂移或清理失败；63组完整回归自有实例/卷/目录和一组参考专项资源均已回收，三个常驻容器ID与Running不变。参考$SYS写入前提仍BLOCKED，当前无新增已确认库运行时缺陷；本轮未重跑库故障专项、未更新常驻部署，干净远端CI、真实独立批准及完整M0/M1/F6/F7仍待。


2026-11-11 混合 QoS/重叠订阅收口：新增 `MQTT311-MIXED-QOS-001` v1.0.0，执行九行发布 QoS×独立订阅请求 QoS 矩阵；同客户端重叠订阅行因锁定参考注册顺序敏感已从首轮必需用例剔除。实际 SUBACK 授予值、按最小 QoS 的投递、QoS1→QoS0 合法重复、QoS2 去重和逐连接原始帧均由父进程重建；新增 14 项工具正反例测试，工具总数 181 项通过。固定 Mosquitto 参考的重叠订阅诊断具有可复现注册顺序敏感性：`exact→+→#`时仅投递 QoS0，`#→+→exact`与`+→#→exact`首先投递 QoS2；该诊断已从首轮必需用例剔除并登记 MQTT-T07，不影响独立订阅矩阵。这不是把参考行为当作规范真值，也未据此修改产品代码。拒绝订阅和缺少 QoS0 正向观测按前提不足 BLOCKED，`$SYS` 继续 MQTT-T05 BLOCKED。

剔除注册顺序敏感的参考重叠行后，完整联合验收为 68 PASS / 1 BLOCKED / 0 FAIL / 0 ERROR / 0 NOT_TESTED，退出码 0；仅含九行独立订阅矩阵的 MIXED 在两个产品目标和两个 live 入口均通过。临时报告、实例和镜像标签已清理；三个常驻容器 ID 与 Running 状态保持不变。完整规范、语义/充分性、干净远端 CI、匹配部署和独立批准仍不升级，M0/M1 与 F6/F7 继续 Partial。

2026-11-12 新增非门禁的 `overlap_diagnostic.py`，以 `--requests 0,1,2` 对锁定 `eclipse-mosquitto:2.0.22` 复现 MQTT-T07：`exact→+→#` 为 1 个 QoS0 副本，`#→+→exact` 与 `+→#→exact` 为 1 个 QoS2 副本；默认 `2,2,2` 控制组三种顺序均为 QoS2。当前 `standalone-broker` 临时实例在 `0,1,2` 下三种顺序均为 1 个 QoS2 副本，仅为产品诊断观测，不是参考结果或符合性结论。输出固定为 `diagnostic-only` / `INCONCLUSIVE`，不进入 catalog 或必需门禁。新增 3 项纯函数测试后工具测试为 184 项通过；MQTT-T07 的上游解释/版本处置与独立语义复核仍开放。
