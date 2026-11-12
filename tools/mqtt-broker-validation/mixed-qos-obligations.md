# 混合 QoS 独立订阅的有限证据

本页为MQTT-T01/T03的初步语义定界，不是官方映射批准或完整义务清单。`MQTT311-MIXED-QOS-001` v1.0.0与`OBL-311-QOS-NEGOTIATION`均保持待审；首轮必需用例只覆盖独立订阅者，官方映射结构v8和18个选定ID/13个段落不变，完整M0/M1、F6/F7继续Partial，独立批准0。

## 来源与上下文

[OASIS MQTT3.1.1 OS](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)，固定HTML SHA256 `fc1f493558339850740a57ea19c4b2bdf39db6589edf9a69a984afbec098aa9b`，cp1252。以下是正文完整段落；本页引用尚未接入已选官方映射机器审定模型。

> When Clients make subscriptions with Topic Filters that include wildcards, it is possible for a Client’s subscriptions to overlap so that a published message might match multiple filters. In this case the Server MUST deliver the message to the Client respecting the maximum QoS of all the matching subscriptions [MQTT-3.3.5-1]. In addition, the Server MAY deliver further copies of the message, one for each additional matching subscription and respecting the subscription’s QoS in each case.

> The SUBACK Packet sent by the Server to the Client MUST contain a return code for each Topic Filter/QoS pair. This return code MUST either show the maximum QoS that was granted for that Subscription or indicate that the subscription failed [MQTT-3.8.4-5]. The Server might grant a lower maximum QoS than the subscriber requested. The QoS of Payload Messages sent in response to a Subscription MUST be the minimum of the QoS of the originally published message and the maximum QoS granted by the Server. The server is permitted to send duplicate copies of a message to a subscriber in the case where the original message was published with QoS 1 and the maximum QoS granted was QoS 0 [MQTT-3.8.4-6].

> MQTT delivers Application Messages according to the Quality of Service (QoS) levels defined here. The delivery protocol is symmetric, in the description below the Client and Server can each take the role of either Sender or Receiver. The delivery protocol is concerned solely with the delivery of an application message from a single Sender to a single Receiver. When the Server is delivering an Application Message to more than one Client, each Client is treated independently. The QoS level used to deliver an Application Message outbound to the Client could differ from that of the inbound Application Message.

§3.8.4后续非规范示例明确：授予QoS1时输入QoS0仍按QoS0投递，输入QoS2会降为QoS1；授予QoS0时输入QoS2可能丢失但不应重复，输入QoS1可能丢失或重复。§4.3.1规定QoS0无ACK/重试，PUBLISH的QoS=0、DUP=0。§4.3.2/4.3.3的每跳握手、方向内ID及重传规则分别适用，不能以输入QoS代替出站QoS。

## 有限必需矩阵

| 行 | 输入/请求 | 断言与允许结果 |
|---|---|---|
| 0–8 | 发布0/1/2 × 单一精确订阅请求0/1/2 | 每行独立连接/唯一Topic；实际授予≤请求，观测投递QoS=min(输入,授予)；QoS0无ACK、QoS1逐次PUBACK、QoS2完成独立交换 |

每行输入仅发送一次、不重试、不保留。实际授予值从SUBACK原始字节读取：较低授予合法，矩阵是输入×请求的组合，不能因此声称实际授予QoS2或九种有效投递组合均已执行。拒绝订阅为正向前提不足BLOCKED；QoS0正向消息未观测到同样BLOCKED，不能据此判协议FAIL。QoS1/2在本工具正向预算内未完成为场景失败，不据超时直接新增规范运行时缺陷。

每行独立订阅者的副本预算为一份。合法QoS1重传不占新槽，仍逐次记录接收/ACK；QoS2重复PUBLISH重复PUBREC、重复PUBREL重复PUBCOMP但不重交付。输入QoS1且授予QoS0允许重复副本，不强加一份上限。实际授予对应的最高可交付QoS须在观察前存在，各连接完整观察0.4秒；窗口内出现半包、EOF、欠ACK或非法独立副本均不能PASS。此有界窗口不证明无限时间无重复。

## 证据与后续边界

`mixed_qos.py`执行，`mixed_qos_evidence.py`由父进程从原始TX/RX独立重建：九行顺序、十八个不同clean连接及身份、CONNECT/CONNACK、各SUBSCRIBE/SUBACK、发送端ACK完成、接收端逐级ACK与应用交付、九个观察起止、最终DISCONNECT。允许提前转发、合法重传/重复PUBREL与完成后ID复用；不能跨连接借用同号ACK或交付。

合成正确对端覆盖低授予、QoS1与QoS2重传、重复PUBREL和输入ACK前转发；错误对端覆盖错内容/QoS/授予、缺原始字节/ACK/交付、未交付最高QoS、错误身份及伪造窗口，另有socket半包/EOF/重复与QoS0缺正向前提测试。这不新增真实故障对端运行校准数量。

本矩阵先补已确认的通用证据缺口；多客户端同时按不同授予扇出、重叠订阅、Retained/持久会话/恢复、延迟故障、资源规模、跨章节完整原子和真实独立实审仍需结合声明Profile决定有限必需项及排除理由，不能从未知组合直接推断代码缺陷或无限扩展测试。FANOUT的两个QoS2接收者和Topic固定向量继续提供各自选定证据，不被本例替代。

## 剔除的重叠订阅行

原先的同客户端精确/`+`/`#`三重重叠行已从首轮必需 catalog 用例剔除，原因是锁定 Mosquitto 2.0.22 对订阅注册顺序敏感，导致参考场景无法稳定作为通用黑盒基线。该行为保留在 T07 诊断记录中，不作为产品 PASS/FAIL 的首轮必需判定，也不删除规范语义。后续若确定需要重叠订阅互操作验收，应先固定实现/版本处置，再单列可复现 Profile 和 oracle。

## 锁定参考的差异

Mosquitto 2.0.22 默认参考配置对重叠订阅呈现确定的注册顺序敏感性：按 `exact → + → #` 注册并均授予 QoS0/1/2 时，仅观测到一个 QoS0 副本；按 `# → + → exact` 或 `+ → # → exact` 注册时，首先观测到 QoS2。三种顺序均成功 SUBACK。该结果使 T07 从“偶发缺失”收窄为“低 QoS 匹配先入队后，同客户端去重路径抑制后续匹配”的可复现参考差异，但仍需上游处置确认。

参考场景保留 FAIL，不通过修改 oracle、交换正式用例 Filter 顺序或启用 `allow_duplicate_messages` 来制造 PASS。固定源码 `src/subs.c` 的逐订阅 min QoS 和 `src/database.c` 的 `dest_ids` 去重路径提供根因线索；这不是已完成的上游修复或独立语义审定。跟踪 MQTT-T07，参考 `$SYS` 的 MQTT-T05 继续独立 BLOCKED。

## 2026-11-12 诊断复现

[overlap_diagnostic.py](overlap_diagnostic.py) 以 `--requests 0,1,2`（按注册顺序请求 QoS0/1/2）对锁定 `eclipse-mosquitto:2.0.22` 复现上述差异：`exact→+→#` 的 SUBACK 为 `[0,1,2]`、窗口内仅 1 个 QoS0 副本；`#→+→exact` 与 `+→#→exact` 的 SUBACK 同样为 `[0,1,2]`，但窗口内为 1 个 QoS2 副本。默认 `--requests 2,2,2` 是控制组，三种顺序均为 1 个 QoS2 副本，不呈现差异。

同一参数对当前 `standalone-broker` 临时实例执行时，三种顺序均为 1 个 QoS2 副本，即按最高匹配 QoS 投递；这只是产品诊断观测，是否符合§3.3.5-1的全部语义仍需独立审查。该诊断不产生 PASS/FAIL，不把 Mosquitto 行为当作规范真值，也不替代 MQTT-T07 的上游/版本处置。
