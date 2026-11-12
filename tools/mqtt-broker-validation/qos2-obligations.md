# QoS2 复合义务清单与选定证据

核对日期：2026-11-10。来源为固定 [OASIS MQTT 3.1.1 OS §4.3.3](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)，原始文件摘要及正文见 [机器记录](normative-map.json)。这里导入两个完整复合列表，不将其计入已建立的18个选定编号映射，也不增加协议覆盖率或独立批准。

`MQTT-4.3.3-1`含发送方六个列表项、七个MUST/MUST NOT；`MQTT-4.3.3-2`含接收方四个列表项、五个MUST/MUST NOT。下表key是工具内部定位符，不是新官方编号。每个原子记录保留完整所属列表项及MUST的序号、字符偏移，避免拆句时丢失主语或适用条件。原文清单完整性、场景执行和充分性分别管理，全部原子当前均为`partial-selected-evidence` / `pending`，父编号为`inventory-only`。

## 来源门禁

`compound_source.py`保留正文引言、ul边界、每个li和编号段落的连续结构；校验块摘要、全部MUST位置、原子数量/次序、角色、场景版本、方向/恢复点和父进程检查入口。固定原文核验要求整段结构在附录前唯一出现，拒绝只导入孤立编号、丢列表项、乱序、扁平化成p或附录重复表。

发送方另保留紧邻编号后的PUBCOMP后ID可复用及不同ID可并发发送的上下文，不能误取QoS1的PUBACK说明。接收方保留所有权转移脚注及Method A/B的实现选择说明；不要求在同一个ACK边界分发消息。普通源码门禁检查元数据，显式原文/手动镜像CI检查完整来源；这些检查不替代独立语义审查。

## 发送方（Broker → Client）

| 内部key | 义务要点 | 当前选定证据 | 仍需审查或补验 |
|---|---|---|---|
| unused-id | 新应用消息分配未使用的ID | v3恢复及FLOW双在途不同ID、完成后复用 | 更多在途规模、资源压力下的分配 |
| new-publish-qos2-dup0 | 新PUBLISH使用QoS2、DUP=0 | 恢复及FLOW三条新消息有原始帧及断言 | 更多新消息、Will/Retain组合与时序 |
| publish-unacknowledged | 至收到对应PUBREC前保持PUBLISH未确认 | AwaitPubRec恢复及FLOW两消息PUBREC前ID独立 | 更多延迟/并发与内部/持久状态 |
| pubrel-on-pubrec | 收到PUBREC推进PUBREL | 恢复及FLOW每消息重复PUBREC均有实际PUBREL | 更多重复次数与合法交错 |
| pubrel-original-id | PUBREL保持原PUBLISH ID | 恢复/FLOW及DUPLEX双向同号的两种完成顺序逐连接对应 | 更大并发及与故障/存储的交点 |
| pubrel-unacknowledged | 至收到PUBCOMP前保持PUBREL未确认 | AwaitPubComp恢复及FLOW重复PUBREL逐次确认 | 更多延迟、重复与存储组合 |
| no-publish-after-pubrel | 发PUBREL后不得重发原PUBLISH | 所选捕获序列/有界窗口内无禁止帧 | 所有重试路径和无界性质不能由短测证明 |

## 接收方（Client → Broker）

| 内部key | 义务要点 | 当前选定证据 | 仍需审查或补验 |
|---|---|---|---|
| pubrec-on-publish | 接受所有权后以对应ID发PUBREC | 原始/恢复/健康交换的真实PUBREC | ACK不能独自证明内部所有权或磁盘状态 |
| repeat-pubrec | PUBREL前同ID重复PUBLISH仍应PUBREC | v3两分支均在读取健康PUBREC前排入DUP帧，逐次确认；另有恢复重投 | 更多重复次数、标志及合法交错 |
| no-duplicate-onward | 上述重复不得导致重复后续交付 | 恢复/FLOW及FANOUT两个订阅者分别各两消息、独立0.4秒窗口 | 更大扇出、QoS混合/重叠订阅、Method A/B及完整时序充分性 |
| pubcomp-on-pubrel | 对PUBREL发同ID PUBCOMP | 恢复及FLOW双在途、重复PUBREL逐次PUBCOMP | 未知PUBREL与更多完成重试 |
| id-reuse-is-new | 发PUBCOMP后，同ID后续PUBLISH当作新发布 | v3/FLOW及DUPLEX两完成顺序的健康消息复用已完成输入ID，内容不同且真实交付 | 更多Topic、标志和状态组合 |

v3仍为`MQTT311-QOS2-RECOVER-001`（3.0.0），四恢复分支、十六个连接与原始ACK截断不变。重复健康PUBLISH先以DUP=0发送新消息、再以DUP=1发送相同消息，两帧均在客户端读取PUBREC之前排入；收到确认后推进PUBREL。不会收到PUBREC后仍伪称未收到，也不强制Broker向订阅者分配与发布侧相同的ID。原消息完成后才复用输入ID，不能用相同ID覆盖未完成交换。

这12项仅是两个列表内部的完整MUST清单；Packet ID、会话、重投、顺序、分发及其他章节的关联义务仍需逐项审核，M0/M1、F6/F7与完整QoS2验收保持Partial。

FLOW为`MQTT311-QOS2-FLOW-001`（1.0.0），独立于恢复v3，使用两个clean连接、两消息同时在途、逐消息重复PUBREC/PUBREL及一条完成后复用输入ID的健康消息。订阅侧每个原始PUBLISH有PUBREC加一次重复，全部在读取PUBREL前排入；每个实际PUBREL逐次PUBCOMP，只在首个PUBREL产生应用交付。父进程逐字节校验ACK、四阶段和三次交付，观察期间额外合法PUBREL仍需确认，不强求Broker ID与输入ID相同。

来源结构v6为除`repeat-pubrec`外的11项原子增加FLOW补充证据绑定，原恢复v3绑定全部保留。两消息出站窗口是探针能力前提，串行发送合法；没有第二条完整消息且无部分字节时BLOCKED，半包/EOF不转为能力阻断。这里没有要求Broker必须并发，也没有用短窗口证明所有重试/时序或无界性质。

## 双订阅者扇出补充证据

`MQTT311-QOS2-FANOUT-001` v1.0.0使用一个发布者及两个独立clean订阅者，均精确订阅唯一Topic、授予QoS2。原始PUBLISH及同ID/DUP重复在读取PUBREC前排入，逐次确认两份PUBREC；完成输入交换后两个订阅者按各自实际Broker ID独立完成QoS2，每个载荷只交付一次。之后复用已完成输入ID发送不同健康载荷，反向完成两个订阅者的交换。每个订阅者各有明确开始/结束的0.4秒观察及最终DISCONNECT，共三连接、四次应用交付、五个阶段。额外合法PUBREL逐次PUBCOMP且不重交付。

父进程从原始TX/RX重建三次输入PUBLISH、全部ACK、各订阅者的订阅/交换/交付/窗口及清理；一个连接的ACK、相同Packet ID或交付事件不能借给另一个连接。Broker可在两个连接使用相同或不同ID，交换完成后也可复用；合法提前转发、DUP重投及重复PUBREL有正例。场景不要求跨订阅者同时在途，不强制Broker在某个输入ACK边界转发，不推断内部Method A/B或持久化。

来源v7在既有11项FLOW绑定外，给接收方五个复合原子追加FANOUT补充绑定，分别固定case/版本、receiver角色、全部五阶段和父进程入口。父编号inventory-only、原子partial-selected-evidence/pending、18个选定编号及独立批准0均不升级。两个订阅者不是任意扇出、混合QoS/重叠订阅、全部时序或完整符合性证明。


## 同一连接双向同号补充证据

`MQTT311-QOS2-DUPLEX-001` v1.0.0包含incoming-first/outgoing-first两个独立正常流程分支。每分支先由helper发起向owner的QoS2消息，owner读取Broker实际分配的ID但不发PUBREC，再用相同ID发送反向PUBLISH并接收PUBREC，形成同一TCP连接两个方向同时在途。分别先完成输入方向或输出方向，另一方向仍须完成；之后复用已完成的输入ID发送不同健康载荷，并完成反向健康消息。共四个clean连接、八次应用交付、每分支五阶段和每连接独立起止/0.4秒观察，最终DISCONNECT。

父进程按原始字节为同一连接分别维护发送/接收命名空间，核对原始交换真实重叠、ID相等、两个完成顺序、逐方向ACK及后续健康交付。PUBREC/PUBREL/PUBCOMP不能因ID相同而互相替代。Broker ID由实际PUBLISH读取，健康出站ID可以复用或改变；合法提前转发、PUBLISH重投和重复PUBREL有正例。helper输入PUBCOMP不被强制要求先于owner收到原消息，测试不规定Broker的内部转发ACK边界。

固定OASIS原文§2.3.1说明“Client and Server assign Packet Identifiers independently of each other”，因此可同时使用相同ID；这里没有新增官方MUST编号或完整符合性结论。来源v8仅给`pubrel-original-id`、`pubcomp-on-pubrel`、`id-reuse-is-new`三项原子追加DUPLEX补充证据，绑定角色、版本、两个分支及全部五阶段；既有FLOW的11项/FANOUT的5项绑定保留，仍partial-selected-evidence/pending。18个选定编号、13个完整段落及独立批准0不变。

库已有同号双向命名空间、SQLite/提交故障/部分ACK/SIGKILL限定矩阵，本用例补宿主通用黑盒的正常流程证据，不重复登记为库功能未实现，也不宣称本次重新运行库故障专项。恢复/存储/重启、更多故障交点及完整充分性仍按Profile和库证据逐项定界。
