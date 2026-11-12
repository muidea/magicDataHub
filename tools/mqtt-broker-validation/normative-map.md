# 连接、Retained、QoS2 与 Topic 官方条款映射草案

本入口绑定 MQTT 3.1.1 连接四个、Retained七个、QoS2恢复三个及Topic四个选定官方编号与对应版本化证据，不是完整原子义务清单。机器记录见 [normative-map.json](normative-map.json)，结构见 [schema](schemas/normative-map.schema.json)；映射结构版本为8，来源使用明确sections清单。状态固定为 draft、规范审查 pending、真实独立批准 0；不产生运行 PASS 或完整覆盖率。目录内八个内部范围仍保留 `official_ids=[]`，待审草案不能替代审定映射。

来源为 [OASIS MQTT 3.1.1 OS 原文](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)，原始 SHA256 为 `fc1f493558339850740a57ea19c4b2bdf39db6589edf9a69a984afbec098aa9b`。保留十三个完整正文段落、各段 UTF-8 摘要和原始编码 cp1252；原文校验在附录之前查找唯一全文匹配，避免将附录重复汇总当正文。Retained第一段含 QoS0 的 `MQTT-3.3.1-7` 上下文，连接Session Present段落含持久会话和拒绝分支上下文；这些附带编号没有映射为已覆盖。

## 连接正常路径

| 官方编号 | 选定行为 | 连接v2证据阶段 |
|---|---|---|
| MQTT-3.1.4-4 | 声明Profile下接受合法CONNECT，以零返回码确认 | connack-accepted |
| MQTT-3.2.0-1 | 服务端首个报文必须是CONNACK | connack-accepted |
| MQTT-3.2.2-1 | Clean Session=1时成功CONNACK的Session Present=0 | connack-accepted |
| MQTT-3.12.4-1 | 响应PINGREQ发送PINGRESP | pingresp-received |

`MQTT311-CONNECT-001` v2.0.0使用匿名、无Will/凭据、MQTT级别4、Clean Session=1、非空唯一ClientID、KeepAlive=30的输入。父进程只接受一个匹配连接，按实际TX/RX字节重建合法CONNECT、首个成功CONNACK、PINGREQ、PINGRESP、DISCONNECT以及三个有序阶段；RX标签必须与此前捕获字节相同。`client-disconnected`还要求客户端本地关闭检查，DISCONNECT不要求服务端返回不存在的确认包。

稳定验证缺口`MQTT-GAP-002`已取得反例：旧父进程校验能接受成功标签、无效CONNECT字节及一份CONNACK，缺少完整往返和DISCONNECT仍可PASS。v2已在选定范围修复，正反例拒绝遗漏/额外/乱序帧、错连接/ID/阶段、Session Present/返回码错误及错误PINGRESP。真实回环对端还验证半包EOF不能成功，运行校准新增错误PINGRESP固定头，共十一种变异。

该场景等待CONNACK后再发PINGREQ只是自身输入屏障，不能推导为所有客户端都必须等待；标准允许CONNECT之后提前发送后续控制报文。四个映射均只提供选定正常路径证据，不覆盖拒绝连接、重复ClientID、持久Session恢复/清除、KeepAlive超时、Will或全部DISCONNECT义务；本地socket关闭标签不是服务端所有资源释放的证明。

## QoS2四分支恢复

| 官方编号 | 选定行为 | 必需恢复分支 |
|---|---|---|
| MQTT-2.3.1-6 | PUBREC/PUBREL对应原PUBLISH的Packet ID | 两方向各AwaitPubRec/AwaitPubComp |
| MQTT-3.2.2-2 | Clean Session=0且已有Session时，CONNACK的Session Present=1 | 两方向各AwaitPubRec/AwaitPubComp |
| MQTT-4.4.0-1 | 重连时沿用原Packet ID重投未完成PUBLISH或PUBREL | 两方向各AwaitPubRec/AwaitPubComp |

`MQTT311-QOS2-RECOVER-001` v3.0.0保留v2的四个有序`qos2-branch-start`，绑定唯一ClientID、Topic和原始/健康载荷；应用交付事件明确绑定实际接收连接。父进程重建十六个连接的角色、CONNECT/CONNACK、订阅/授予、发送端ACK屏障、重投ID/DUP/内容、接收端PUBLISH/PUBREC/PUBREL/PUBCOMP及应用交付。客户端发送方向还逐流比较代理receive/forward与客户端字节，证明ACK实际到达代理而未交付客户端。每个接收帧的ACK分别计数，不能复用一个ACK掩盖缺项。

接收端重放保留跨重连状态，原消息与恢复后健康消息各交付一次；允许正确的重投、Packet ID在交换完成后复用、重复PUBREL再次确认而不重交付，以及Broker在不同ACK阶段转发。测试客户端选择在PUBREL时交付，不把该选择强加给Broker。观察使用绝对帧期限，半包/EOF/超时不能成为成功静默窗口；清理只验证自有Clean Session重置请求及成功确认，不声称完成磁盘或硬件故障恢复。

稳定验证缺口`MQTT-GAP-003`已复现：旧父进程可接受成功分支/会话/交付标签和无关字节，没有真实连接/握手仍能PASS。v2已在选定范围修复，测试拒绝缺少或错配连接、原始字节、ACK截断、会话标志、Packet ID、应用交付关联、观察或清理证据。正反例使用可复用合成轨迹和真实回环对端，逐次Broker报告不归档。

三个映射仍只覆盖选定恢复路径。v3新增两项实际输入：完成原交换后以同一发布侧ID发送不同健康载荷，以及在读取健康PUBREC前排入同ID的DUP PUBLISH，要求逐次PUBREC且后续消息只交付一次；Broker初始新PUBLISH还核验DUP=0。缺少或改变这些输入/ACK/屏障不能以v3身份PASS。

`MQTT-4.3.3-1/-2`现已以完整引言/ul/li/编号结构导入`compound_requirements`，保留四段ID复用/并发及Method A/B上下文；两个父编号保持inventory-only，未加入18个选定映射编号。发送方7个/接收方5个MUST原子均绑定完整列表项、具体MUST位置、角色、v3场景/分支、父进程检查及明确剩余项；全部为partial-selected-evidence/pending。清单和逐项边界见[QoS2复合义务](qos2-obligations.md)。导入不等于语义/充分性审定，也不关闭并发同号、完整重复/分发时序、所有权、存储或重启组合。

## 双消息在途补充证据

`MQTT311-QOS2-FLOW-001` v1.0.0单独验证两个输入ID和两个独立Broker出站ID、重复PUBREC/PUBREL逐次响应、三次单独应用交付及完成后输入ID复用；两个clean连接无重连，四个阶段以原始字节重建。合法ACK置换、完成后Broker ID复用、额外PUBREL均可接受。该探针要求出站窗口至少2；串行发送合法，缺该前提为BLOCKED而非协议失败。

结构v6在12个复合原子中为11项加入`supplemental_evidence`，明确用例/版本、Broker角色、全四阶段、父进程入口和pending状态。`repeat-pubrec`仍仅绑定恢复v3的重复PUBLISH输入。双消息只是选定并发规模，未证明压力分配、双向同号、扇出或全部合法时序；原子与父编号的审查/充分性状态不升级。

## Topic匹配

| 官方编号 | 选定行为 | 通用v2证据行（从0编号） |
|---|---|---|
| MQTT-3.8.4-1 / -2 | 响应SUBSCRIBE且SUBACK沿用其Packet ID | vector-0至vector-15，每行候选/精确订阅 |
| MQTT-4.7.2-1 | 根`#`/`+`不匹配以`$`起始的Topic | vector-6/7负向，vector-8显式`$validation/#`对照 |
| MQTT-4.7.3-4 | 不归一化Topic/Filter，非通配层逐字符匹配 | vector-0精确匹配，vector-9/10/13/14大小写、尾斜线、空层和Unicode负向对照 |

固定16行另保留单层/多层、零层和空层通配的解释性行为对照；这些合法输入不能证明非法通配语法的MUST已验证，未导入`MQTT-4.7.1-2/-3`。完整上下文中的SHOULD预防客户端交换`$`消息与产品策略分开；通用使用可控`$validation`，字面`$SYS`另有三行并保留环境阻断，不从MAY用途或参考策略推导协议违规。

每行v2包含三个唯一clean连接、候选订阅匹配健康消息、独立精确订阅目标正向对照及两次QoS1非保留发布。父进程从原始字节核验角色/握手、SUBSCRIBE/SUBACK、发送与接收逐次ACK、固定向量内容、控制屏障及两个0.4秒窗口和最终DISCONNECT。合法DUP逐次ACK，首次DUP和ID复用不误拒；半包、EOF、缺对照或额外目标交付不能成为成功静默。候选只在目标应匹配时接收目标；错误向量内容/标签不再能替代实际交换。

`MQTT-GAP-004`已复现旧父进程接受错误Filter/Topic标签及无关字节的反例；v2在选定范围修复，并加入缺字节/握手/ACK/订阅/窗口、错角色/身份、跨向量藏帧与原始字节一致但协议内容错误的反例。通用48连接、系统9连接分别重建；完整匹配/时序、所有UTF-8/非法输入、重叠与重复订阅仍待审，四个编号仍draft/pending。

## Retained路径

| 官方编号 | 选定行为 | Retained v2 证据阶段 |
|---|---|---|
| MQTT-3.3.1-5 / -6 / -8 | 保存 QoS1 旧值、新订阅回放、回放 RETAIN=1 | old-replayed、non-retained-preserved |
| MQTT-3.3.1-9 | 已有订阅实时交付 RETAIN=0 | delete-delivered、late-control-delivered、post-control-empty |
| MQTT-3.3.1-10 | 零载荷实时交付并删除旧值 | delete-delivered、late-control-delivered、post-control-empty |
| MQTT-3.3.1-11 | 不保存零载荷删除消息 | late-control-delivered、post-control-empty |
| MQTT-3.3.1-12 | RETAIN=0 不替换旧值，也不创建保留记录 | non-retained-preserved、post-control-empty |

`MQTT311-RETAIN-DELETE-001` 的版本为 2.0.0：发布旧值并回放；发布 RETAIN=0 临时值后另建订阅确认仍回放旧值；删除并等待已有订阅收到零载荷；建立晚到订阅、发送实时正向对照、观察 0.4 秒；再次建立订阅验证上一步 RETAIN=0 没有生成保留值，同样以正向对照和 0.4 秒观察确认接收有效；最后删除自有 Topic。六个有序阶段包含 cleanup-completed。

接收器分别确认每次 QoS1 PUBLISH。已知 Packet ID、Topic、payload、QoS、RETAIN 完全一致且 DUP=1 的重投单独 ACK，不算新应用交付；首次观察 DUP=1 和新交换复用 Packet ID 可以接受。观察窗口拒绝新交付、旧保留值、零载荷、未标记重复、半包、EOF 和超时。SUBACK 前合法 PUBLISH 保留并检查，不丢弃。

父进程重新从实际 TX/RX 字节构建帧、角色连接、CONNACK、订阅/授予、六次发布/确认、逐次接收 ACK、四个订阅者的交付序列、删除/保留屏障及观察窗口。仅补成功标签、缺字节/ACK/正向对照、错 Topic 或阶段乱序均不能 PASS。

稳定缺口 `MQTT-GAP-001` 分类为 validation-gap：v1 缺少 RETAIN=0 保留旧值的对照，且可能误拒合法 QoS1 重投；v2 已补齐选定范围。`implemented-selected-scope` 不表示官方父条款、全部 QoS/Filter、重连、进程重启、磁盘持久化或完整 M0/M1 已关闭。

```sh
python3 -B normative_map.py
# 指定已缓存的原始 HTML，完全离线核验：
python3 -B normative_map.py --source /absolute/mqtt-v3.1.1-os.html
# 显式获取缺失原文并核验；已有不匹配缓存不覆盖：
python3 -B normative_map.py --fetch-source
```

普通源码门禁无需网络或外部原文缓存，校验元数据、段落摘要、编号集合及场景版本/断言/逐编号阶段绑定；手动镜像 Actions 额外核验固定 OASIS 原始字节和正文。缓存拒绝符号链接及替换已有文件。元数据一致与原文一致分别取证，均不替代语义、充分性或独立审查。

## 双订阅者扇出补充证据

`MQTT311-QOS2-FANOUT-001` v1.0.0使用一个发布者及两个独立clean订阅者，均精确订阅唯一Topic、授予QoS2。原始PUBLISH及同ID/DUP重复在读取PUBREC前排入，逐次确认两份PUBREC；完成输入交换后两个订阅者按各自实际Broker ID独立完成QoS2，每个载荷只交付一次。之后复用已完成输入ID发送不同健康载荷，反向完成两个订阅者的交换。每个订阅者各有明确开始/结束的0.4秒观察及最终DISCONNECT，共三连接、四次应用交付、五个阶段。额外合法PUBREL逐次PUBCOMP且不重交付。

父进程从原始TX/RX重建三次输入PUBLISH、全部ACK、各订阅者的订阅/交换/交付/窗口及清理；一个连接的ACK、相同Packet ID或交付事件不能借给另一个连接。Broker可在两个连接使用相同或不同ID，交换完成后也可复用；合法提前转发、DUP重投及重复PUBREL有正例。场景不要求跨订阅者同时在途，不强制Broker在某个输入ACK边界转发，不推断内部Method A/B或持久化。

来源v7在既有11项FLOW绑定外，给接收方五个复合原子追加FANOUT补充绑定，分别固定case/版本、receiver角色、全部五阶段和父进程入口。父编号inventory-only、原子partial-selected-evidence/pending、18个选定编号及独立批准0均不升级。两个订阅者不是任意扇出、混合QoS/重叠订阅、全部时序或完整符合性证明。


## 同一连接双向同号补充证据

`MQTT311-QOS2-DUPLEX-001` v1.0.0包含incoming-first/outgoing-first两个独立正常流程分支。每分支先由helper发起向owner的QoS2消息，owner读取Broker实际分配的ID但不发PUBREC，再用相同ID发送反向PUBLISH并接收PUBREC，形成同一TCP连接两个方向同时在途。分别先完成输入方向或输出方向，另一方向仍须完成；之后复用已完成的输入ID发送不同健康载荷，并完成反向健康消息。共四个clean连接、八次应用交付、每分支五阶段和每连接独立起止/0.4秒观察，最终DISCONNECT。

父进程按原始字节为同一连接分别维护发送/接收命名空间，核对原始交换真实重叠、ID相等、两个完成顺序、逐方向ACK及后续健康交付。PUBREC/PUBREL/PUBCOMP不能因ID相同而互相替代。Broker ID由实际PUBLISH读取，健康出站ID可以复用或改变；合法提前转发、PUBLISH重投和重复PUBREL有正例。helper输入PUBCOMP不被强制要求先于owner收到原消息，测试不规定Broker的内部转发ACK边界。

固定OASIS原文§2.3.1说明“Client and Server assign Packet Identifiers independently of each other”，因此可同时使用相同ID；这里没有新增官方MUST编号或完整符合性结论。来源v8仅给`pubrel-original-id`、`pubcomp-on-pubrel`、`id-reuse-is-new`三项原子追加DUPLEX补充证据，绑定角色、版本、两个分支及全部五阶段；既有FLOW的11项/FANOUT的5项绑定保留，仍partial-selected-evidence/pending。18个选定编号、13个完整段落及独立批准0不变。

库已有同号双向命名空间、SQLite/提交故障/部分ACK/SIGKILL限定矩阵，本用例补宿主通用黑盒的正常流程证据，不重复登记为库功能未实现，也不宣称本次重新运行库故障专项。恢复/存储/重启、更多故障交点及完整充分性仍按Profile和库证据逐项定界。



混合QoS/重叠订阅的`MQTT311-MIXED-QOS-001` v1另以待审内部范围接入，有限矩阵、完整段落上下文和允许结果见[mixed-qos-obligations.md](mixed-qos-obligations.md)。尚未给MQTT-3.3.5-1、MQTT-3.8.4-5/-6新增正式映射/批准；来源结构版本8、18个选定编号/13段及原有复合绑定保持不变，不能将章节引用当作充分性审定。
