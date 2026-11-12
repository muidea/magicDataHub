# MQTT Broker 代表性验证入口

本工具归属 magicDataHub，位于 `tools/mqtt-broker-validation/`，通过 MQTT 网络验证受限的 MQTT3.1.1 场景，不依赖 Broker 内部实现。正式 mqtt-server、datahub-server 服务和镜像由本项目交付；工具、测试、目录与锁文件随本项目统一维护。

完整符合性始终 **Partial**。23 个稳定用例包含工具校准、10 个通用场景和 12 次上游调用（11 个文件）；9 个内部证据范围引用标准章节，规范语义与充分性审查为 pending。它们不是完整官方原子义务清单，不能计算或宣称官方规范覆盖率。所选场景全部通过仅提供 TargetedEvidence，规范判定仍为 INCONCLUSIVE。

## 准备与执行

需要 Linux、Python3.12、Git 和 Docker。依赖在外部缓存中复用，不修改全局 Python；上游源码不复制进本项目。准备器支持没有 ensurepip 的 Python：创建无 pip 虚拟环境，校验并安装锁定 wheel。

```sh
cd /absolute/magicDataHub/tools/mqtt-broker-validation
python3 -B prepare.py --pull-reference
# 已缓存时可完全离线核验：
python3 -B prepare.py --offline

python3 -B run.py --check
python3 -B run.py --list
python3 -B -m unittest discover -s tests -v

python3 -B run.py --host-project ../.. \
  --mqtt-image sha256:ACTUAL_LOCAL_IMAGE_ID \
  --combined-image sha256:ACTUAL_LOCAL_IMAGE_ID \
  --required-targets mqtt-server,datahub-server \
  --output /absolute/new-temporary-output
```

镜像必须使用真实 64 位 SHA256 本地 ID 或 registry digest，参考镜像必须匹配 `upstream.lock.json`。镜像需提前构建/拉取；工具不构建服务。默认依赖清单位于 `~/.cache/mqtt-broker-validation/dependencies.json`，自定义缓存通过 `prepare.py --cache-dir …` 和 `run.py --dependencies …/dependencies.json` 配合使用。执行前后核验上游 commit/文件、wheel 与已安装 Paho 文件摘要；不接受仅版本号一致的依赖。

默认选择 reference、mqtt-server、datahub-server，每个目标的每个场景单独创建实例；校准总会运行。`--targets mqtt-server`、`--cases MQTT311-CONNECT-001,UPSTREAM-MOSQ-001` 可缩小范围，报告会列出未取得的证据，子集通过不代表完整目录通过。`--targets tool` 可仅执行本机错误对端校准，无需 Docker/Paho。

可追加 `--live mqtt-server=127.0.0.1:1883 --live datahub-server=127.0.0.1:2883`。常驻入口只执行 Paho 功能场景，使用独立 ClientID、唯一 Topic 和合成载荷，并清除自己创建的 Retained 值。常驻服务不会被重启；非法输入、固定上游 Topic/ClientID、根通配和故障只用于隔离实例。

## 隔离与有界退出

仅复制 magicDataHub 五个公开开发配置文件；拒绝外部源、实际口令和非开发监听入口，监听替换为随机回环端口。每个实例使用独立自有状态卷和公开测试 SecretRef 值，不读取实例配置、实际源凭据或业务数据库。运行清单记录工具及根目录 MQTT 工作流/目录/锁/依赖身份、配置源和生效摘要、宿主 HEAD/dirty 路径、请求和实际镜像 ID、端点、计划、预算与 UTC run_id；执行使用配置快照。

每个场景独立子进程，总预算默认 90 秒（`--case-timeout`）；超时或 SIGINT/SIGTERM 后先通知子进程，默认允许 80 秒清理（`--cleanup-grace`），之后才终止进程组并由父进程兜底。清理命令各自有界。只有精确资源名和随机 ownership label 都匹配才能删除；无全局 prune。magicDataHub 正常退出必须返回 0、记录 GracefullyStopped、端口可重绑，强制回收不算正常通过。中断后不再启动后续场景，未执行计划保留 NOT_TESTED，已有证据不覆盖。

若机器掉电或父进程被 SIGKILL，Python 无法保证 finally 执行；输出中的 `*.request.json` 保存精确 ownership journal，可核对标签后人工回收。不要全局删除 Docker 卷。常驻服务验证中断可能留下该次唯一命名空间的 Retained 值，不能据此宣称业务状态完全无变化。

## 结果和门禁

结果输出 `manifest.json`、逐场景 request/result、JSONL 字节/断言轨迹、日志、`summary.json`、JUnit 与 Markdown。输出目录必须不存在；失败与重跑分开保存，不复用旧 run/attempt 证据。PASS 还须通过独立证据校验：摘要/大小/路径、完整边界、必须断言、真实非空 TX/RX、Topic 固定向量内容、角色、完整交换及观察窗口、实际镜像和 checked cleanup。缺文件、漏跑、非零进程或失败断言不能成为 PASS。

| 状态 | 意义 |
|---|---|
| PASS | 所选场景和证据检查通过 |
| FAIL | 原始场景断言失败；上游差异须规范审查 |
| BLOCKED | 前提或环境控制不满足 |
| ERROR | 执行、超时、中断、证据或清理异常 |
| NOT_TESTED | 计划中未完成或该证据范围未取得的场景 |

默认全部所选目标为必需目标，任何 BLOCKED/ERROR/NOT_TESTED 使退出码为 3。显式 `--required-targets mqtt-server,datahub-server` 只为产品场景设置门禁，工具校准和所给 live 入口仍必需；参考结果继续完整展示，不会被改为 PASS。全局中断、输入变化或清理不完整始终阻断。退出码 0 仅表示必需目标所选场景通过，1 为断言失败，2 为输入错误，3 为执行/环境/证据阻断。`--require-conformance` 要求完整符合性，当前 Partial 必然非零。

通用 `$` 通配规则使用可写 `$validation`；字面 `$SYS` 对照单列。Mosquitto 参考实现拒绝客户端写 `$SYS`，不能取得正向对照，该场景明确 BLOCKED，不以静默通过、N/A 或协议违规代替。

上游测试为 B 类适配：保留原始函数及 ACK 断言，仅替换生命周期/目标、选择 MQTT3.1.1 调用，并捕获字节和断言。上游固定 Packet ID、顺序、有限观察窗口可能存在实现假设，FAIL 先记差异，不直接判 NON_CONFORMANT。上游版权/许可依据锁定 checkout 的 LICENSE.txt、edl-v10 和 epl-v20，保留在该 checkout，本项目不再分发其测试源码。

## 目标 Profile 与数据契约

[profiles/](profiles/) 固定两个正式宿主的独立声明：`mdh-mqtt311-standalone` / `mdh-mqtt311-combined`，均为版本 `1.0.0`；另有受控 Mosquitto 参考声明和外部常驻网络观察声明。通用 `mqtt311-tcp-anonymous-memory` 仅为场景兼容族，不能代替目标身份。Profile 声明协议/角色、传输、匿名访问、内存、组合方式、FailFast、配置文件、连接/会话限额、预期 QoS 和排除范围；规范审查仍 pending。

执行前将 Profile 原始文件复制到 `input/profiles/`。`manifest.target_profiles`、逐场景 request/result、case-start/profile-checked 轨迹与 summary 绑定 ID、版本、文件 SHA256 和有效配置逐文件 SHA256。两个执行进程分别核验，拒绝宿主互换、配置漂移、摘要不匹配、可选能力混入和结果漏记。产品隔离实例的 MQTT 配置只接受当前明确的匿名回环/内存参数；Profile 变更须同步实现、版本与验收，不自动接受新的配置字段。

常驻目标名为 `mqtt-server`/`datahub-server` 时关联对应声明，其他名字使用 `mqtt311-live-unverified`；所有 live 结果固定 `mode=live-network-only`、`configuration_status=unverified`，不读取常驻配置，也不推断其存储/限额/监督或构建来源已经核验。产品隔离实例为 `snapshot-checked`，参考实例为 `controlled-reference`。这些状态仅描述配置来源/校验，独立镜像 ID 和实际网络断言仍分别取证；配置匹配不证明镜像由当前源码构建。

[schemas/profile.schema.json](schemas/profile.schema.json) 和 [schemas/profile-binding.schema.json](schemas/profile-binding.schema.json) 使用 JSON Schema Draft 2020-12 描述当前契约。运行时离线校验器只支持本项目实际采用的明确子集，遇到未知校验关键字拒绝执行；不宣称实现通用 JSON Schema。工具自身检查无需增加 Python 依赖。Profile、绑定与配置正反例已进入根目录门禁和 Actions。用例、义务、目录、RunManifest 与 Request 的结构 schema 及计划绑定也已接入；官方义务映射与完整语义审查仍待补齐，M0 不因此整体关闭。

## 事件与结果契约

[事件 schema](schemas/event.schema.json)、[worker 原始结果 schema](schemas/worker-result.schema.json)和[汇总 schema](schemas/summary.schema.json)为 Draft 2020-12 的受检契约，已进入现有源码检查与 Actions。当前事件明确区分 56 种类型及其必需/允许字段；未知类型/字段、错误类型、非法十六进制与端点形状均拒绝。

Trace 为每个事件写入 `schema_version=1`、从 0 开始的连续 `sequence` 和单调相对 `seconds`，在同一锁内取时、编号和写入，保留并发事件的实际记录顺序。顺序号不代表网络因果关系或不同 TCP 连接的全局协议顺序。父进程在核对轨迹摘要后重验事件 schema、连续性、时间非递减、唯一首尾边界、Profile 与所需断言。原始 worker 结果只有校验通过才获得父进程 `evidence_validated=true`；它表示本工具完成证据检查，不是独立审查批准。

JSON 输入拒绝任何层级的重复键、NaN/Infinity 和溢出为非有限值的数字；输出拒绝非有限值。汇总按完整计划生成 NOT_TESTED，拒绝重复计划/重复结果、计划外结果、其他 run 的结果及没有有效证据标记的 PASS，不再以字典覆盖重复记录。汇总在写出前校验 schema，完整符合性固定 Partial，规范审查固定 pending。结构契约不能代替内容真实性和语义充分性审查，也不能以重新计算摘要掩盖失败。

[用例](schemas/case.schema.json)、[内部义务](schemas/obligation.schema.json)、[目录](schemas/catalog.schema.json)、[RunManifest](schemas/manifest.schema.json)和[Request](schemas/request.schema.json)均有 Draft 2020-12 schema。目录按 runner/entrypoint 区分允许字段，义务固定为待审的内部证据范围；上游参数必须等于锁文件中的调用，包含 `clean_session` 两种取值。schema、双向映射及跨字段正反例随源码门禁进入 Actions。

运行计划固定先校准、再隔离场景，常驻目标只能执行唯一命名空间的 Paho 场景。Manifest 记录 `input_files` 的完整文件摘要、计划摘要和不可变执行内容的 `execution_sha256`；进度标志及完成时间不参与执行摘要，但始终参与门禁。Request v1 用 run_id、执行摘要、计划索引和完整用例绑定该计划；同时核验目标 Profile、镜像 ID、回环端点、配置/上游路径、场景输出路径和 ownership token/资源名/目录标记。输入快照不得存在符号链接或额外文件。

父进程在产生请求前校验，worker 在创建 Trace、写日志、连接网络或启动容器前重验，父进程接收结果和汇总时再次校验。已完成/停止的运行及已有场景产物拒绝重放；有效旧请求也不能覆盖旧证据。请求校验失败可能没有 worker 结果，父进程将其记为 ERROR，并且只用自己持有的 ownership journal 清理。摘要用于发现漂移和错配，不是签名、权限边界或独立批准。

旧报告缺少新契约字段时不能补写字段后充当当前证据，应在当前工具下重新运行。逐次报告仍只保留在临时目录/CI artifact。完整原子官方义务模型、证据语义/充分性与独立审查继续单列；当前十份执行结构 schema 及上游审计、连接/Retained草案两份 schema 不表示完整设计的全部数据模型已实现，M0/M1 未整体关闭。

## 连接、Retained、QoS2、Topic v2 与官方映射草案

`MQTT311-CONNECT-001` v2.0.0新增三个有序阶段和客户端socket关闭检查。父进程从同一连接的实际TX/RX字节重建CONNECT、首个成功CONNACK及Session Present=0、PINGREQ/PINGRESP、DISCONNECT；缺少或错配字节/阶段、仅成功标签、异常首包和错误PINGRESP均不能PASS。MQTT-GAP-002已在选定范围修复；不扩展为全部连接拒绝、持久会话或KeepAlive行为已验收。

`MQTT311-RETAIN-DELETE-001` v2.0.0 新增 RETAIN=0 不替换旧保留值的独立订阅对照，并验证删除后不保存零载荷、后续 RETAIN=0 不创建保留记录。两个晚到订阅均有实时正向对照及 0.4 秒观察；合法 QoS1 DUP 重投逐次 ACK，不算多余应用交付。父进程从 TX/RX 字节重建连接、订阅、六次发布确认、接收 ACK、六个阶段及窗口，缺少原始证据或只有成功标签无法通过。

[官方映射草案](normative-map.md)绑定十八个选定官方 ID（连接四个、Retained七个、QoS2三个、Topic四个）、十三个完整正文段落及固定 OASIS HTML 摘要；映射schema版本8明确区分场景、版本、断言、阶段和缺口绑定。`normative_map.py` 默认离线核验元数据和场景绑定；`--source PATH` 核验原文，`--fetch-source` 显式获取缺失原文，已有不匹配缓存保留报错。手动镜像 Actions 额外核验原文。当前共十二份 schema（十份执行、上游审计及草案映射）；草案固定 draft/pending，真实独立批准 0，目录内部 `official_ids=[]` 不变。

`MQTT-GAP-001` 的两项验证缺口已在选定范围实现；QoS0/2、更多 Filter/重连及持久化组合、完整官方义务/语义/充分性和独立审查仍待完成，不关闭 M0/M1 或 F6/F7。

## Topic匹配与证据

`MQTT311-TOPIC-MATCH-001` v2.0.0以固定16行覆盖精确匹配、单层/多层通配、零层/空层、普通`$`命名空间、大小写/尾斜线/相邻分隔符及Unicode不归一化的选定对照。每行建立三个唯一ClientID的clean连接；先用同一候选订阅接收匹配健康消息，再建立目标Topic精确订阅并发布目标消息。正向行要求候选交付；反向行只接受健康消息的合法DUP，目标消息不能进入候选连接。两个订阅者各观察0.4秒；首次DUP、已知相同交换的DUP及完成后Packet ID复用均按QoS1规则处理，逐次PUBACK。半包、EOF、超时、额外交付或缺失ACK均不能当作成功静默。

父进程重建每行三个连接（通用48个、系统9个）的原始TX/RX、完整握手、订阅/授予、两次发布和ACK、逐次接收/确认、健康/发布/观察屏障及最终DISCONNECT；校验固定计划与每行内容，不能仅补标签/向量编号。`MQTT-GAP-004`在选定范围修复。`MQTT311-SYS-TOPIC-001`也使用v2证据契约，但仍单列字面`$SYS`的环境前提，参考正向对照不可用时保持BLOCKED。所有根通配仅用于自有隔离实例；不作为live场景。

新增四个官方草案编号仅关联通用16行的选定证据：SUBACK响应及ID、根通配排除`$`、匹配不归一化。合法通配向量不是非法Filter语法拒绝的证据；完整匹配充分性、重叠订阅、非法UTF-8和所有允许时序仍待审。

## 通用 QoS2 恢复与校准

`MQTT311-QOS2-RECOVER-001` v3.0.0保留v2的四个有序分支入口，绑定命名空间/载荷和实际接收连接。父进程从原始字节重建十六个连接的角色、会话标志、ACK屏障、代理截断/转发流、重投及接收端状态和应用交付；仅标签或缺少原始握手不再能PASS。MQTT-GAP-003仅在选定范围修复；完整列表已导入为inventory-only，语义及跨段充分性仍待审查。

v3在两条Client→Broker分支中，原交换收到PUBCOMP后复用输入Packet ID发送不同健康载荷；在读取健康PUBREC前排入同ID的DUP PUBLISH，再逐次接收两个PUBREC并完成一次PUBREL/PUBCOMP，检查应用只交付一次。新Broker初始PUBLISH核验DUP=0；父进程从实际帧校验上述输入、ACK数量与屏障，Broker向接收方分配的ID仍独立。

[QoS2复合义务清单](qos2-obligations.md)现完整导入MQTT-4.3.3-1/-2的两个列表、12个MUST原子及四段上下文。新增结构/来源/原子位置、角色与v3绑定门禁，schema版本8；两个父编号仅为inventory-only，18个选定映射不增加，真实独立批准0。源码门禁检查元数据；固定OASIS原文/手动镜像Actions核验完整ul/li结构及附录前位置，不用孤立编号替代正文。

`MQTT311-QOS2-RECOVER-001` 覆盖 client-to-broker / broker-to-client × AwaitPubRec / AwaitPubComp 四分支。客户端发送方向通过单连接代理实际接收并截断 Broker 的 PUBREC/PUBCOMP，再断开；代理记录两侧实际字节，原字节转发，不重编码。Broker 发送方向由客户端在 PUBREC/PUBCOMP 尚未发送时关闭 TCP。每分支检查 Clean Session=False 的 Session Present 从 0 到 1、方向内 Packet ID 保留、适用的 DUP/内容或 PUBREL、原消息与恢复后健康消息各一次应用交付，之后观察 0.4 秒并清除自有持久 Session。发布与订阅方向的 ID 不要求相同；该场景不覆盖同号双向并发、Broker 进程重启、磁盘持久化或硬件断电。

错误对端校准共 11 种变异，含错误PINGRESP固定头以及会话丢失、错误重投 ID、缺少 DUP、载荷变化、错误 PUBREL ID 和重复应用交付；每种必须触发指定断言。工具正反向测试同时拒绝缺分支、断线、清理、真实 ACK 截断、交付或断言证据，避免漏跑后仍得到 PASS。完整语义/充分性和独立审查仍待完成。

## 双消息在途与重复控制

`MQTT311-QOS2-FLOW-001` v1.0.0使用两个clean连接，不重连。发布侧在读取确认前排入两个不同ID的QoS2 PUBLISH，逐次接收PUBREC；每个ID排入两次PUBREL并逐次接收PUBCOMP。订阅侧先收到两个不同Broker ID的PUBLISH，再为每个消息排入PUBREC和一次重复PUBREC；逐次接收PUBREL、逐次PUBCOMP，每个载荷只产生一次应用交付。之后复用已完成的输入ID发送不同健康载荷，并观察0.4秒。Broker独立分配订阅侧ID，完成后允许复用；合法额外PUBREL必须再次确认且不能重复交付。

父进程从两连接原始字节重建握手、订阅、每个ACK、并发ID、四个阶段、三次应用交付和最终DISCONNECT；缺真实重复控制、错ID/顺序/交付、半包、EOF或观察不足均不能PASS。乱序ACK、已完成ID复用、合法重投和额外PUBREL有合成正例。选定探针要求至少两个出站QoS2消息同时在途；只支持串行发送也是合法实现，无法取得第二条完整消息且无部分字节时为BLOCKED，不能当作协议违规。该能力前提不扩大为live场景。

来源结构v6为12个复合原子中的11项增加此用例的补充证据绑定（case/版本、Broker角色、四阶段及父进程入口），仍为partial-selected-evidence/pending。重复PUBLISH应PUBREC的原子继续由恢复v3提供证据；不因新用例增加18个选定官方编号、独立批准或完整符合性结论。

## 上游资产审计

[审计与复用决策](upstream-audit.md)、[机器可读审计表](upstream-audit.json)和[审计 schema](schemas/upstream-audit.schema.json)固化三个固定 revision 的 21 项选定资产：12 次已集成 Mosquitto B 类调用、6 项 Paho Testing 基础设施/套件和 3 项 EMQX 套件。后九项均未集成，A/B/C/D 分类只作为复用建议，不计为运行结果或规范覆盖率。每项记录源文件摘要/行引用、依赖、耦合、适配成本与证据边界；Paho 文件头/根许可证版本表述、EMQX 当前 BSL 1.1 及固定 Topic/根通配清理等限制均明确保留。

`python3 -B upstream_audit.py` 只核验本地元数据及其与目录/锁/adapter 的绑定；`--verify-sources` 离线核验外部缓存，`--fetch` 显式获取缺失的固定 commit 再核验。校验器不导入或执行候选源码，已有不匹配缓存保留并报错。普通源码门禁无需这些 checkout；手动镜像 Actions 额外核验源文件。integrated asset 的路径依赖现已核对锁定文件；完整依赖闭包、候选接入、规范/充分性与独立审查仍待。

## CI 与后续边界

当前可执行的剩余项、已完成基线和建议顺序统一见[剩余项与收口顺序](remaining-work.md)。该清单分别跟踪工具 M0/M1、库内 F6/F7、参考环境阻断及可复现交付；不把未完成审查直接当作 Broker 缺陷。

仓库根目录的 [`.github/workflows/verify-mqtt.yml`](../../.github/workflows/verify-mqtt.yml) 在 push/PR 检查语法、目录和工具负向测试，无需 Docker/Paho。本地可在仓库根目录运行 `./scripts/check-mqtt-validation.sh`，该检查也接入 `./scripts/check.sh`。手动镜像验证要求两个可拉取的不可变镜像 digest，使用触发该工作流的同一 magicDataHub checkout 提供工具和公开配置；准备锁定客户端/参考镜像，执行产品门禁并保留参考观测。镜像身份和配置来源均记录，但不据此断言镜像必由给定源码构建。工作流已提供，远端 CI 是否通过须以实际运行记录为准。

仍待后续处理：完整官方义务/证据模型、官方条款清单、规范与独立审查、更多校准/异常和时序矩阵、MQTT5、TLS/认证、持久化/重启、可选扩展/业务桥接、性能容量和长期稳定性。本入口不批准这些范围。

逐次报告/轨迹/日志不提交源码；本地诊断完成后清理临时输出，保留可复用代码、目录、锁和正式能力说明；CI artifact 限期保留。

## 双订阅者扇出补充证据

`MQTT311-QOS2-FANOUT-001` v1.0.0使用一个发布者及两个独立clean订阅者，均精确订阅唯一Topic、授予QoS2。原始PUBLISH及同ID/DUP重复在读取PUBREC前排入，逐次确认两份PUBREC；完成输入交换后两个订阅者按各自实际Broker ID独立完成QoS2，每个载荷只交付一次。之后复用已完成输入ID发送不同健康载荷，反向完成两个订阅者的交换。每个订阅者各有明确开始/结束的0.4秒观察及最终DISCONNECT，共三连接、四次应用交付、五个阶段。额外合法PUBREL逐次PUBCOMP且不重交付。

父进程从原始TX/RX重建三次输入PUBLISH、全部ACK、各订阅者的订阅/交换/交付/窗口及清理；一个连接的ACK、相同Packet ID或交付事件不能借给另一个连接。Broker可在两个连接使用相同或不同ID，交换完成后也可复用；合法提前转发、DUP重投及重复PUBREL有正例。场景不要求跨订阅者同时在途，不强制Broker在某个输入ACK边界转发，不推断内部Method A/B或持久化。

来源v7在既有11项FLOW绑定外，给接收方五个复合原子追加FANOUT补充绑定，分别固定case/版本、receiver角色、全部五阶段和父进程入口。父编号inventory-only、原子partial-selected-evidence/pending、18个选定编号及独立批准0均不升级。两个订阅者不是任意扇出、混合QoS/重叠订阅、全部时序或完整符合性证明。


## 同一连接双向同号补充证据

`MQTT311-QOS2-DUPLEX-001` v1.0.0包含incoming-first/outgoing-first两个独立正常流程分支。每分支先由helper发起向owner的QoS2消息，owner读取Broker实际分配的ID但不发PUBREC，再用相同ID发送反向PUBLISH并接收PUBREC，形成同一TCP连接两个方向同时在途。分别先完成输入方向或输出方向，另一方向仍须完成；之后复用已完成的输入ID发送不同健康载荷，并完成反向健康消息。共四个clean连接、八次应用交付、每分支五阶段和每连接独立起止/0.4秒观察，最终DISCONNECT。

父进程按原始字节为同一连接分别维护发送/接收命名空间，核对原始交换真实重叠、ID相等、两个完成顺序、逐方向ACK及后续健康交付。PUBREC/PUBREL/PUBCOMP不能因ID相同而互相替代。Broker ID由实际PUBLISH读取，健康出站ID可以复用或改变；合法提前转发、PUBLISH重投和重复PUBREL有正例。helper输入PUBCOMP不被强制要求先于owner收到原消息，测试不规定Broker的内部转发ACK边界。

固定OASIS原文§2.3.1说明“Client and Server assign Packet Identifiers independently of each other”，因此可同时使用相同ID；这里没有新增官方MUST编号或完整符合性结论。来源v8仅给`pubrel-original-id`、`pubcomp-on-pubrel`、`id-reuse-is-new`三项原子追加DUPLEX补充证据，绑定角色、版本、两个分支及全部五阶段；既有FLOW的11项/FANOUT的5项绑定保留，仍partial-selected-evidence/pending。18个选定编号、13个完整段落及独立批准0不变。

库已有同号双向命名空间、SQLite/提交故障/部分ACK/SIGKILL限定矩阵，本用例补宿主通用黑盒的正常流程证据，不重复登记为库功能未实现，也不宣称本次重新运行库故障专项。恢复/存储/重启、更多故障交点及完整充分性仍按Profile和库证据逐项定界。



## 混合 QoS 与重叠订阅 v1

`MQTT311-MIXED-QOS-001`（1.0.0）执行九行发布QoS×请求QoS的3×3矩阵。各行使用独立命名空间、两个clean连接、一条非保留输入、实际SUBACK授予及0.4秒观察，共十八个连接。单订阅投递QoS为输入与实际授予的最小值；QoS1→QoS0的合法重复和QoS2重复控制均按规范保留。受Mosquitto注册顺序影响的同客户端重叠订阅行已从首轮必需用例剔除，诊断记录见 [mixed-qos-obligations.md](mixed-qos-obligations.md)。

[有限矩阵与判定依据](mixed-qos-obligations.md)明确协议边界。父进程独立重建每连接原始TX/RX、完整握手、逐项订阅/授予、发布者ACK、订阅者各级QoS握手/交付、窗口和DISCONNECT；QoS2按交换去重，QoS1重传、QoS1→QoS0允许的副本不误报。实际授予可以降低，拒绝订阅或缺少QoS0正向观测标为BLOCKED。正确/错误合成对端及socket接收测试拒绝错QoS/内容、借用ACK/交付、超额独立QoS2副本、漏掉最高QoS投递、半包/EOF或伪造窗口。这些测试不增加十一种真实故障对端校准的计数。

新增一个待审内部范围`OBL-311-QOS-NEGOTIATION`，官方映射结构v8、18个选定ID/13段与独立批准0不变；矩阵的请求组合全部执行不代表实际授予组合、全部跨客户端混合扇出、恢复/故障/规模或完整规范充分性已验收。完整M0/M1和F6/F7继续Partial。

## 重叠订阅诊断（非门禁）

[overlap_diagnostic.py](overlap_diagnostic.py) 不是 catalog 用例，也不参与 `run.py` 计划或必需门禁。它对受控 MQTT3.1.1 端点依次执行 `exact→+→#`、`#→+→exact`、`+→#→exact` 三种注册顺序，记录 SUBACK 授予、0.4 秒窗口内的应用副本及 QoS，输出固定为 `status=diagnostic-only`、`conformance=INCONCLUSIVE`、`independent_review=NotRun`。`--requests` 按注册顺序指定三个请求 QoS；默认 `2,2,2` 是控制组，`0,1,2` 可复现 MQTT-T07 的参考差异。

```sh
python3 -B overlap_diagnostic.py --endpoint 127.0.0.1:1884 \
  --requests 0,1,2 --output /absolute/new/diagnostic.json
```

2026-11-12 对锁定 `eclipse-mosquitto:2.0.22` 执行 `--requests 0,1,2`：`exact→+→#` 为 QoS0，`#→+→exact` 与 `+→#→exact` 为 QoS2；当前 `standalone-broker` 临时实例三种顺序均为 QoS2。后者只是产品诊断观测，不是参考结果或符合性结论。该工具不把 Mosquitto 行为当作规范真值，不产生 PASS/FAIL，也不改变 MIXED 首轮必需矩阵或任何官方映射。若输出已存在则拒绝覆盖；诊断失败仍按异常退出，不能当作通过。
