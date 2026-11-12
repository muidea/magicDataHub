# 上游测试资产审计与复用决策

本页与 `upstream-audit.json` 是可维护的选定资产审计基线，归属 magicDataHub。范围为三个固定 revision 的 21 项资产，不是三个项目的完整目录；规范语义、证据充分性和真实独立审查仍 pending，独立批准为 0。静态审计不生成 Broker 执行结果，也不增加规范覆盖率。

## 固定输入与核对方法

| 项目 | 固定 revision | 锁定文件数 | 选定资产数 |
|---|---|---|---|
| mosquitto | [766fa2c5a9be](https://github.com/eclipse-mosquitto/mosquitto/tree/766fa2c5a9bed6c65249cf555f430b982c417480) | 20 | 12 |
| paho-testing | [fdab9d2b1ce1](https://github.com/eclipse-paho/paho.mqtt.testing/tree/fdab9d2b1ce125385135e5d9f282a0ad315268cc) | 12 | 6 |
| emqx | [ef2beebdc29f](https://github.com/emqx/emqx/tree/ef2beebdc29f89540827d7663c2f9a0e63130c7b) | 9 | 3 |

Mosquitto 对应 v2.0.22；Paho Testing、EMQX 为明确 commit 的源码快照，不用可变 HEAD 作为依赖身份。仓库地址、revision、每个文件 SHA256、原文行引用、依赖、耦合、拟采用的适配、证据限制和后续动作均在 JSON 中维护；当前 `adapters.py` 的摘要也绑定审计。上游源码仅保存在外部缓存，不复制进本仓库。

```sh
cd /absolute/magicDataHub/tools/mqtt-broker-validation
python3 -B upstream_audit.py                  # 只校验元数据，无需缓存/网络
python3 -B upstream_audit.py --verify-sources # 离线核验已有 checkout 的身份/字节/行引用
python3 -B upstream_audit.py --fetch          # 显式按 commit 获取缺失源码，再核验
```

默认缓存为 `~/.cache/mqtt-broker-validation`，也可用 `--cache-dir` 指定。获取缺失缓存时使用临时目录，固定 revision、稀疏文件和禁用 Git hooks，核验后才放入指定目录；已有 checkout 即使不匹配也不 reset/覆盖。校验器不导入、构建或执行候选 Python/Erlang/Shell 文件。普通源码 CI 只检查元数据；手动镜像工作流额外获取并核验审计源码。

## Mosquitto：现有 B 类调用

下列条目已接入执行目录；“已集成”描述代码接入，运行是否通过仍由每次 request/result/trace 单独证明。B 类适配保留原函数和包比较断言，替换 Broker 启停、配置/端口与 socket 取证，省略文件顶层其他协议调用，因此不能登记为 A。全部只用于自有隔离实例。

| 调用 | 原文件及关键行 | 主要证据限制 |
|---|---|---|
| UPSTREAM-MOSQ-001 | [03-publish-qos1.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/03-publish-qos1.py#L9) | PUBACK for client-selected Packet ID only; no subscriber delivery, retry or duplicate proof. |
| UPSTREAM-MOSQ-002 | [03-publish-qos2.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/03-publish-qos2.py#L13) | Normal PUBLISH/PUBREC/PUBREL/PUBCOMP only; no interruption or application exactly-once proof. |
| UPSTREAM-MOSQ-003 | [02-subpub-qos1.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/02-subpub-qos1.py#L21) | Broker-assigned downlink Packet ID is fixed at 1; either ACK/PUBLISH ordering is accepted by receive_unordered; no retry proof. |
| UPSTREAM-MOSQ-004 | [02-subpub-qos2.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/02-subpub-qos2.py#L23) | Broker-assigned downlink Packet ID is fixed at 1; accepts two ACK/PUBLISH orderings; socket closes after sending PUBCOMP without an application duplicate window. |
| UPSTREAM-MOSQ-005 | [04-retain-qos0-clear.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/04-retain-qos0-clear.py#L46) | Uses one-second sleep plus PING ordering after deletion; supplies bounded upstream evidence, not an unbounded absence or cross-connection causal proof. |
| UPSTREAM-MOSQ-006 | [03-publish-b2c-disconnect-qos2.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/03-publish-b2c-disconnect-qos2.py#L37) | Fixed downlink Packet ID 1 and implementation ordering assumptions; helper defaults to MQTT3.1.1; comment says QoS1 although actual packets are QoS2; no broker crash or application duplicate window. |
| UPSTREAM-MOSQ-007 | [03-publish-c2b-disconnect-qos2.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/03-publish-c2b-disconnect-qos2.py#L46) | The client actually receives ACK then pretends it was lost; not a real ACK-cut proof. Fixed IDs; no crash or single application-delivery observation. |
| UPSTREAM-MOSQ-008 | [03-publish-invalid-utf8.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/03-publish-invalid-utf8.py#L16) | One Topic containing NUL vector; helper expects EOF via recv(1) == empty bytes, not a timeout/absence pass; does not cover every malformed UTF-8 class. |
| UPSTREAM-MOSQ-009 | [02-subscribe-invalid-utf8.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/02-subscribe-invalid-utf8.py#L16) | One Topic Filter containing NUL vector; helper expects EOF via recv(1) == empty bytes; no exhaustive malformed-filter proof. |
| UPSTREAM-MOSQ-010 | [02-subpub-recover-subscriptions.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/02-subpub-recover-subscriptions.py#L20) | MQTT3.1.1 branch checks ordinary subscription delivery before/after reconnect. It also accepts a $share filter but only MQTT5 branch expects the shared publication; do not infer shared-subscription semantics in MQTT3.1.1. |
| UPSTREAM-MOSQ-011；clean_session=true | [07-will-qos0.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/07-will-qos0.py#L31) | Unexpected TCP close triggers one QoS0 Will with clean_session true/false; no keepalive expiry, Will suppression, crash or complete Will matrix. |
| UPSTREAM-MOSQ-012；clean_session=false | [07-will-qos0.py](https://github.com/eclipse-mosquitto/mosquitto/blob/766fa2c5a9bed6c65249cf555f430b982c417480/test/broker/07-will-qos0.py#L31) | Unexpected TCP close triggers one QoS0 Will with clean_session true/false; no keepalive expiry, Will suppression, crash or complete Will matrix. |

所有调用均为 `proto_ver=4`。固定 Packet ID、精确字节及有限等待是上游判定条件；发现差异先核对标准，不自动把 Mosquitto 的实现选择当作唯一合法行为。尤其注意：client-to-broker 断线脚本是在客户端收到 ACK 后模拟丢失，不能代替通用恢复场景的真实 ACK 截断；Session 脚本在 MQTT3.1.1 分支不核对 shared publication，不能据此批准共享订阅。非法 UTF-8 脚本只覆盖 Topic/Filter 内 NUL，空预期由 `recv(1)` 等待 EOF，不把超时当作通过。

## Paho Testing：基础设施初评

| 资产 | 拟复用等级 | 依据与下一步 |
|---|---|---|
| MQTT3.1.1 编解码 | B 候选 | 输入校验依赖 Python assert；需固定非优化执行、补 EOF/半包/合法与非法向量校准，原始字节独立取证 |
| MQTT3.1.1 测试客户端 | C 候选 | `sendtosocket` 部分发送后仍提交完整 data，且没有零进度检查；这是静态控制流观察，尚无运行反例。移植前补短写/零写和线程退出控制 |
| MQTT3.1.1 unittest 套件 | B 候选 | 原有 host/port 参数不能解决清理范围；`cleanup` 订阅 `#` 并删除收到的 Retained，必须用自有隔离 Broker。固定 ClientID/Topic、模块全局和 sleep 仍须审计 |
| restart 网络代理 | C 候选 | 另需 localhost:7777 控制 Broker、固定控制主题与旧式 Paho Client 构造；兼容性、字节透明性和有界退出尚未执行验证 |
| 模型回放入口 | D 参考 | `MQTTV311_spec` 的导入路径与所见 specifications 布局需要先闭合；另有全局 Retained 清理及模型/轨迹比较依赖 |
| MQTT5 套件 | D 参考 | 当前宿主 Profile 为 MQTT3.1.1；后续在显式 MQTT5 宿主下逐方法评估，不计为已执行 |

六项均为 `not-integrated`，等级是复用建议；没有新增可执行用例或 live-safe 声明。完整依赖闭包、候选运行、许可证衔接和规范/Oracle 审查仍在后续处理。

Paho Testing 根 LICENSE 写明 EPL-2.0 与 EDL-1.0，所选 Python 文件头写明 EPL-1.0 与 EDL-1.0。审计同时保留两处依据，不把根说明自动套用到所有文件；接入/复制/修改前仍需处理版本表述及相应保留义务。当前没有复制或重新许可候选源码。

## EMQX：耦合与迁移成本初评

| 资产 | 拟复用等级 | 依据与下一步 |
|---|---|---|
| session_mem 套件 | C 候选 | 使用 meck、EMQX 应用启动、内部 session records/定时器效果和 `emqx_session_mem`；不能将套件地址直接指向任意 Broker。可在标准复核后提取线帧/ACK/重连序列 |
| MQTT5 protocol 套件 | C 候选 | 虽有 emqtt 网络交互，但组装本地 EMQX listener、固定端口、Common Test/snabbkaffe 和可选 quicer；须逐方法分离生命周期与内部断言，并先建立 MQTT5 Profile |
| frame 套件 | D 参考 | 直接测试内部 parser/serializer 与解析状态默认值；parser 能接纳某输入不等于 Broker 可将其作为连接首包。只参考向量思路，不移植实现默认值为协议预期 |

所选 revision 的根 LICENSE 为 Business Source License 1.1，包含额外使用授权及未来更换许可证的条件；所选套件头未见独立许可证覆盖。该观察只针对当前固定输入，不推断其他 EMQX 版本，也不构成再分发或生产使用批准。当前三项均未集成、不执行，不要求首期迁移整个 Erlang/Common Test 环境。

## 后续收口边界

- 本轮关闭的是三个固定 revision 的首批资产静态评估和可重复核验入口；integrated asset 的路径依赖已核对锁定文件，完整上游目录与依赖闭包仍未审计。
- 如需接入候选，先处理许可证、Profile、逐方法依赖与 Oracle 校准，再新增明确的目录/适配/执行证据。
- 更新上游或当前 adapter 时重审摘要、断言差异与分类，不能静默替换稳定依赖。
- 官方义务映射、完整语义与充分性、真实独立审查及完整 M0/M1 仍未关闭；不得把审计元数据计为 PASS 或覆盖率。
