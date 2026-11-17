# MQTT 剩余项与收口顺序

核对日期：2026-11-12。本页核对工具实际目录、规范草案、上游审计、父进程证据门禁及库内 F6/F7 计划。当前没有新增已确认的 Broker 运行时代码缺陷；工具验证缺口MQTT-GAP-001/002/003/004已在选定范围修复；以下分别记录验证缺口、待审范围、环境阻断和后续专项，不从“未审查”推断“未实现”。

## 已完成基线

- 工具归 magicDataHub，服务和镜像由该项目统一交付；MQTT 和 FedEngine 保持业务库。
- 23 个稳定用例、9 个内部证据范围；两个正式 MQTT3.1.1 宿主 Profile、十份执行 schema，加上游审计与官方映射草案共十二份，事件类型 56 种。计划/快照/路径/ownership 绑定、worker 副作用前校验和精确清理已有门禁。
- 双向 QoS2 的 AwaitPubRec/AwaitPubComp 四分支、实际 ACK 截断及十一种错误对端校准已具备；不重复登记为未开发。
- Topic v2已补16个通用/3个系统向量、候选健康与目标精确订阅对照、逐连接原始字节和ACK/窗口重建，MQTT-GAP-004仅在选定范围修复；四个新增官方编号仍待审。
- QoS2 v3保留v2的四个分支入口、十六个连接及ACK截断/恢复/交付的原始字节重建，MQTT-GAP-003仅在选定范围修复；三个选定编号仍待审；v3补输入ID完成后复用、读取PUBREC前排入重复PUBLISH及新Broker PUBLISH的DUP=0。两个复合列表及12个MUST已以inventory-only导入，逐项剩余边界见[复合义务清单](qos2-obligations.md)。
- FLOW v1新增两个clean连接、两消息同时在途及重复PUBREC/PUBREL逐次响应，完成后输入ID复用、三次应用交付和0.4秒观察由父进程原始字节重建。合法ACK置换、Broker ID复用与额外PUBREL有正例；缺字节/控制/交付/窗口和错时序有反例。只支持串行发送合法，选定出站窗口前提不足为BLOCKED。来源v6为11项复合原子增加补充绑定，全部仍Partial/pending，不提升官方映射或独立批准。
- FANOUT v1补两个独立QoS2精确订阅者的扇出：重复输入只在各订阅者交付一次，完成后输入ID复用，健康消息反向完成交换；三连接、四交付及每订阅者独立起止/0.4秒窗口由原始字节重建。相同/不同跨连接ID合法，ACK和交付不能跨连接借用。来源v7为接收方五项原子增加补充绑定，完整语义/充分性及更大扇出仍待；混合QoS/重叠订阅已有下述独立有限矩阵。
- DUPLEX v1补同一TCP连接双向同号的两个完成顺序：读取Broker ID后发起反向同ID交换，先完成任一方向后另一方向仍完成，随后双向健康交付及输入ID复用。四连接/八交付、各自观察起止和0.4秒窗口由父进程独立命名空间重建；来源v8为三个原子追加补充绑定。仅正常流程选定范围收口，库内已有故障矩阵和完整充分性仍分别核对。
- MIXED v1补九行输入×请求QoS的实际授予、逐级ACK/应用交付和十八连接/九个独立0.4秒观察；受参考注册顺序影响的同客户端重叠订阅行已从首轮必需用例剔除，诊断资料保留；新增待审内部范围，来源v8与18个选定官方编号不变。边界见[有限矩阵](mixed-qos-obligations.md)。
- 连接v2已补三个阶段、完整原始交换和本地关闭检查；四个官方编号待审，MQTT-GAP-002仅在选定范围修复。
- Retained v2 的 RETAIN=0 保留旧值、不创建保留记录、删除零载荷、合法 QoS1 DUP、父进程原始字节重建及有界观察已实现。[MQTT-GAP-001](normative-map.md)仅在选定范围实现，不能升级为完整父条款已验收。
- [上游审计](upstream-audit.md)已锁定三个 revision、41 个文件与21项选定资产；12次 Mosquitto B 类调用已集成，6项 Paho Testing、3项 EMQX 均为静态候选/参考。
- 最近一次混合QoS完整回归见[宿主验证说明](../../docs/validation.md)：181项工具测试通过；剔除参考注册顺序敏感的重叠行后完整联合验收为68 PASS / 1 BLOCKED / 0 FAIL / 0 ERROR，退出码0。临时产物已清理，该摘要不替代新独立评审需要的原始材料。源码门禁、固定OASIS原文、上游源码/行引用及依赖身份检查均通过；三个常驻容器ID与Running保持不变。2026-11-12 新增非门禁的重叠订阅诊断脚本及3项纯函数测试，工具测试现为184项；该脚本不进入 catalog，不产生 PASS/FAIL 或符合性结论。

## 当前待收口

下列 T 编号仅用于本工具的工作跟踪，不是官方条款或库内 F67 缺陷编号。

| 编号 | 优先级 / 分类 | 当前剩余项 | 收口出口 |
|---|---|---|---|
| MQTT-T01 | P1 / 官方映射与语义审查（工程台账已建，独立语义审查待审） | 已新增 [oracle-sufficiency.json](oracle-sufficiency.json)、[oracle-sufficiency.schema.json](schemas/oracle-sufficiency.schema.json) 与 [oracle_sufficiency.py](oracle_sufficiency.py)：将 18 个选定 ID、12 个 QoS2 MUST 原子、3 个 MIXED OASIS 原文段落及 SHA-256 摘要（`MQTT-3.3.5-1`/`3.8.4-5`/`3.8.4-6`）与 9 个内部义务全部绑定至病例、必需断言、事件类型与父进程校验器；`independent_approvals=0`、`review_status=pending` | 逐类完成人工独立语义复核后方可升级审定状态；不凭元数据通过批准 |
| MQTT-T02 | P1 / 完整数据模型（模型与聚合门禁已落地，审查待审） | [oracle_sufficiency.py](oracle_sufficiency.py) `evaluate_sufficiency` 与 [oracle-sufficiency.schema.json](schemas/oracle-sufficiency.schema.json) 已实现适用条件（`required`/`conditional`/`excluded` + `exclusion_rationale`）、`should_deviation` 记录、`alternative_groups`、未验证 PASS 转 `ERROR` 以及无抵消严格聚合（任一 `FAIL`/`ERROR`/`BLOCKED`/`NOT_TESTED` 阻止通过，全 `PASS` 在未独立批准下保持 `Partial` / `INCONCLUSIVE`）；[test_oracle_sufficiency.py](tests/test_oracle_sufficiency.py) 覆盖正反例 | 后续扩展全协议 M1 原子条目与人工独立批准后再升级总体结论；已知违规或漏项不能被其他 PASS 抵消 |
| MQTT-T03 | P1 / Oracle 与证据充分性（台账对照已落地） | [oracle-sufficiency.json](oracle-sufficiency.json) 已为 18+12+3+9 共 42 条目逐一登记 `required_assertions`、`captured_wire_checks`、`required_event_kinds`、可导入 `parent_verifier` callable、`positive_oracles`/`negative_counterexamples` 实存测试引用、`allowed_variants` 与 `parameter_rationale`；保留 MIXED/Topic 0.4s 窗口与重叠行剔除边界 | 保持合法提前交付、DUP/ID 复用及允许降级语义；后续如需扩充 M1 范围，先取得具体缺口反例再补校准与校验 |
| MQTT-T04 | P1 / 上游依赖与接入决策（依赖闭包校验已落地） | [upstream_audit.py](upstream_audit.py) 已对全量 21 项集成及候选资产（Mosquitto/Paho/EMQX）强制校验 `reuse_grade`、`adaptations`/`evidence_limits`/`next_steps` 决策依据以及 `.py`/`.erl`/`.sh` 依赖路径到锁定源码文件的闭包；[test_upstream_audit.py](tests/test_upstream_audit.py) 已增加负例门禁 | 首批静态盘点与依赖闭包已收口；后续若决定实际接入 Paho/EMQX 候选，再单列许可证、Profile 与运行 Oracle |
| MQTT-T05 | 环境阻断 / 参考观测 | 锁定 Mosquitto 拒绝客户端写字面 $SYS，缺少正向对照，参考场景BLOCKED；普通可写 $validation 命名空间及两个产品隔离目标已有通过 | 保留独立原因及目标门禁；若需该参考场景通过，另确定合法可控的正向前提并重验。不得改为PASS/N/A/协议违规，也不为全绿放宽产品规则 |
| MQTT-T07 | P1 / 参考行为差异（已从首轮必需用例剔除，诊断保留） | 锁定Mosquitto 2.0.22在MIXED重叠行呈现确定的注册顺序敏感性：`exact→+→#`仅观测QoS0，`#→+→exact`与`+→#→exact`首先观测QoS2；三种顺序均成功授予0/1/2。原始帧与固定源码的逐订阅QoS及dest_ids去重路径已核对，根因线索已收窄；2026-11-12新增[overlap_diagnostic.py](overlap_diagnostic.py)：`--requests 0,1,2`对锁定Mosquitto 2.0.22复现三种顺序的QoS0/QoS2差异；当前standalone临时实例三种顺序均为1个QoS2副本。输出固定为`diagnostic-only`/`INCONCLUSIVE`，不进入catalog或门禁 | 该行已从首轮必需 catalog 用例剔除，保留为独立诊断；下一出口是上游修复/解释或明确版本处置，并独立复核§3.3.5与§3.8.4语义。不能视参考为真值、改用最小QoS或调配置掩盖，不据此新增产品库缺陷 |
| MQTT-T06 | P1 / 可复现交付 | `modorch-mqtt` 已推送协议结项提交 `d0ae55f`（含 `F67-01–08` 协议验收闭环），宿主 [Cargo.toml](../../Cargo.toml) 已切换并锁定该 Git revision，工具及宿主本地重验与身份指纹已更新；尚无匹配最新提交的干净远端 CI run、新构建镜像与常驻服务替换 | 推送宿主提交并在干净 checkout/CI 执行三服务、远端/镜像/工作流（`verify.yml` 传入 `mqtt_revision=d0ae55ff3adb3b6364f5e827576d9fb3b79a35ad`），再部署匹配版本并验收；不以版本号或Workflow文件代替执行 |
| F67-01–08 | 已在协议验收范围结项 | `modorch-mqtt` 的 F6（437/437）与 F7（607/607）+ 18 矩阵/222 组合在 `c367fa4`/`f9474a9`（最终结论于 `d0ae55f`）经三轮 `F67-07` 独立复审（AI 评审、新会话、未参与实现，负责人认可）全部 `Passed`（1,064/1,064），`F67-08` 结项，范围限定于 `acceptance-profile.json` 的 Server 与 `linux-x86_64` | 若受控输入变更需重跑复审；不自动替代 `check-release.py` 的 178 项发布义务或工具侧 `MQTT-T01–T04` 的人工独立审批 |
| 发布 / SLO | P2 / 独立专项 | 178项发布义务独立批准0；三服务release镜像、代表性负载/长期RSS/吞吐/P99、跨平台/文件系统、硬件断电、安全、magicCommon及远端CI材料不全 | 按库仓库的 `docs/release.md`取得相应真实证据；dev镜像、短测、SIGKILL或协议门禁通过均不能替代这些出口 |
| 后续宿主范围 | 用户已明确后续单列 | MQTT5、Auth/TLS/Storage、WebSocket、Admin及MQTT→SQL业务桥接尚未作为首轮宿主能力验收；库中已有可选实现不等于宿主启用 | 每项先确定显式Profile、配置、资源/鉴权和验收；不自动扩大本轮。Enhanced Authentication/AUTH继续按明确不支持及11项UnsupportedConditional管理，启用时另立任务 |

## 建议执行顺序

1. 连接/Retained/QoS2/Topic v2、FLOW/FANOUT/DUPLEX及MIXED已完成各自选定范围的原文/证据链和正反例；QoS2完整列表已完成结构导入和12项初步证据定界；双消息在途及重复PUBREC/PUBREL已有独立FLOW选定证据，FANOUT双订阅者与DUPLEX同连接双向同号两完成顺序也已有选定证据；下一步先核对MQTT-T01/T03的逐项充分性、MIXED诊断边界和有限必需矩阵，再推进下面的具体证据项与四类草案语义审查。选定映射不等于完整义务清单；更大规模不是无上限的必做要求。
2. 用审查中发现的实际需要推进 MQTT-T02；MQTT-T04 补首轮资产闭包，具体缺口分别登记实现、验证、评审状态。不能将全部未知组合直接列成代码缺陷。
3. 功能与契约稳定后推进 MQTT-T06 的精确版本与干净CI。参考 $SYS 的 MQTT-T05 独立保留，不掩盖也不影响已显式选择的产品门禁语义。
4. F6/F7、发布/SLO和可选宿主范围按各自里程碑推进。完整M0/M1、F6/F7始终保持Partial，真实独立批准仍0，直到对应完成标准实际满足。


## 2026-11-11 本次复核结论

- **已收口（选定范围）**：MIXED v1的代码、schema、catalog反向绑定、父进程原始证据、正反例、产品镜像和live入口验证均已闭环；不再作为待开发项。
- **保留的真实差异**：参考Mosquitto MIXED重叠行存在可复现的注册顺序敏感性（`exact→+→#`缺最高授予QoS2，其他两种顺序首先出现QoS2），作为MQTT-T07诊断跟踪；当前首轮必需门禁不再FAIL；根因线索已收窄到逐订阅入队与同客户端`dest_ids`去重，该差异未归因到magicDataHub产品库，也未修改oracle掩盖。
- **环境阻断**：MQTT-T05的`$SYS`正向写入继续BLOCKED；不能改成PASS或N/A。
- **仍开放的P1**：MQTT-T01–T04 的人工独立语义审查（`independent_approvals=0`/`review_status=pending`，实现侧台账、非抵消聚合与依赖闭包已于 2026-11-16 落地）、MQTT-T06 精确版本/干净 CI/匹配部署。
- **仍开放的P2/后续范围**：发布/SLO（178 项发布义务 0 批准）以及宿主后续可选范围（MQTT5、Auth/TLS/Storage、WebSocket、Admin 和业务桥接）。`modorch-mqtt` 的 F67-01–08 协议验收已在 `d0ae55f` 范围结项。

## 2026-11-12 重叠诊断复现

`overlap_diagnostic.py --requests 0,1,2` 对锁定 `eclipse-mosquitto:2.0.22` 复现 MQTT-T07：`exact→+→#` 为 1 个 QoS0 副本，`#→+→exact` 与 `+→#→exact` 为 1 个 QoS2 副本；默认 `--requests 2,2,2` 控制组三种顺序均为 QoS2。当前 `standalone-broker` 临时实例在 `0,1,2` 下三种顺序均为 1 个 QoS2 副本，方向与“重叠订阅按最高匹配 QoS 投递”的 §3.3.5-1 一致，但这只是诊断观测，仍须独立语义审查。

该结果把 T07 从“参考差异记录”推进为“可由固定参数复现的参考差异”；产品侧未观测到同样的低 QoS 抑制。T07 仍保持开放：需要上游解释/版本处置，并独立核对 §3.3.5-1 与 §3.8.4-6 的完整语义；不能把参考行为当作规范真值，也不能由本轮诊断自行批准产品符合性。

## 本次复核后的具体边界

以下是MQTT-T01/T03的执行拆分，不新增官方义务、库运行时缺陷或独立批准；“未审定组合”须先判断是否适用于声明Profile，不能全部当作必做实现。

| 分项 | 已有事实 | 剩余工作与出口 |
|---|---|---|
| 双向同号的后续充分性 | DUPLEX已补宿主通用黑盒的两个正常完成顺序、逐方向ACK/单次交付和双向健康消息；库原有SQLite/提交故障/部分ACK/SIGKILL矩阵不重复登记为未实现。FANOUT跨两个连接同号与DUPLEX同一连接双向同号分别验证 | 正常流程选定范围已补；对照声明Profile与库已有矩阵判断还需哪些恢复/故障交点及证据，保留适用条件与排除理由，不机械复制全部库矩阵 |
| 混合QoS与重叠订阅的通用证据 | MIXED v1新增九行输入×请求QoS，实际授予、逐级握手/交付、十八连接和九个0.4秒窗口由原始字节重建；正确/错误合成对端与socket测试已补；同客户端重叠订阅因参考注册顺序敏感从首轮必需用例剔除 | 此有限矩阵的工具实现已补，不重复登记为待开发；实际授予可以降低，不能宣称全部有效QoS组合或跨客户端混合扇出已执行。后续按Profile审查恢复/故障与规模等必要交点，不能把合法额外副本当QoS2去重缺陷 |
| 在途规模、扇出和延迟/重试边界 | FLOW已有两个消息同时在途；FANOUT已有两个订阅者；重复PUBREC/PUBREL及ID复用已有选定证据 | 结合资源预算/已声明能力和风险确定是否需要新增等价类、规模点或延迟交错，列明必需项/排除理由；不要求穷举任意N或证明无界静默，不把合法串行发送当作违规 |
| 官方语义与证据充分性 | 18个选定ID、13个完整段落、两个复合列表/12个MUST、FLOW的11项及FANOUT的5项补充绑定已受检 | 逐项补完整上下文/跨章节义务、适用条件、已有库/宿主证据及不足原因；决定证据充分、验证缺口、代码缺陷或不适用。真实独立批准仍待，元数据检查和本次核对不产生批准 |

库内已有精确入口包括`crates/modorch-mqtt-core/src/tests/protocol.rs`的`packet_identifier_namespaces_are_independent_in_simultaneous_qos2_directions`，`crates/modorch-mqtt-core/src/tests/storage.rs`的同号双向SQLite/提交故障/SIGKILL矩阵及`crates/modorch-mqtt-core/src/connection/receive_tests.rs`的部分ACK矩阵；路径相对于modorch-mqtt仓库。相关Feature、传输与平台限制仍按源码及库内计划保留，本次仅核对源码/契约存在，没有重新执行这些专项。

MQTT-T01–T04 的实现侧台账/聚合/依赖闭包与单元测试已于 2026-11-16 补齐，独立语义审查（`independent_approvals=0`/`review_status=pending`）、MQTT-T05 参考环境前提、MQTT-T06 精确版本/干净 CI/匹配部署仍分别开放。FLOW/FANOUT/DUPLEX/MIXED 的选定范围不重复登记为待开发，也不关闭 F67-07–08、独立人工实审或发布/SLO 专项。

总体生态的非MQTT遗留项继续见工作区设计文档 `modorch_ecosystem_design_docs_2026-11-09/待收口核对清单_2026-11-09.md` §24：四个来源的激活兼容问题、GUI、框架组合指南及FedEngine类型/扩展准确性、生产专项未被本次工具收口关闭。本次未重查业务源状态；数据准确性/扩展更新保持用户已确认的后续待办。
