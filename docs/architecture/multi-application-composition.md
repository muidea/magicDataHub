# 多应用编排

magicDataHub 交付三个固定身份的服务。每个进程只创建一个 Tokio Runtime、一个 Modorch Application；datahub-server 的 MQTT 与 FedEngine 共用该 Application 的 Hub、Registry 和 Scheduler。框架精确固定为 Modorch 0.0.3 / `336ba20196044e1b81fb2ca297a18b5e258a5800`。

| Package | 职责 | 业务依赖 |
|---|---|---|
| mqtt-server | MQTT 独立装配入口 | datahub-mqtt |
| fedengine-server | 数据库独立装配入口 | datahub-fedengine |
| datahub-server | 两库联合装配入口 | 两个适配 |
| datahub-app | 统一预算、Ready、信号和 checked drain | 无 |
| datahub-config | 严格宿主 TOML、CLI 和路径解析 | 无 |
| datahub-mqtt | MQTT 公开 Bundle 配置/注册/观察 | modorch-mqtt |
| datahub-fedengine | FedEngine 公开 Bundle/Profile 配置/注册/观察 | modorch-fedengine |

适配 crate 不相互依赖。业务协议、Topic、SQL、目录和权限归业务库；宿主只使用公开 Facade。FedEngine 的开发 test-host 不进入服务依赖闭包。本地依赖暂用精确版本加相邻仓库 path，Modorch 始终使用同一 Git Source。`scripts/verify-dependency-identity.py` 核对 locked/offline metadata 中的唯一框架身份、各 package 的 normal/build 闭包及实际源码指纹，dev-dependency 不计入服务闭包。

一个 binary 必须配置其固定能力，组合入口不可静默退化为单库。首轮 MQTT 只启匿名回环、内存、3.1.1 Core，明确 FailFast；FedEngine 沿用公开 Bundle 的默认 FailFast，支持 PG+HTTP、PG-only、HTTP-only，拒绝 EmbeddedCore。MQTT 可选扩展和 MQTT→SQL 桥接不在首轮。

注册仅计算需求并注册 Factory，选择使用返回实际 IDs。之后统一检查预算/监听冲突、设置 Builder 一次、build/start 一次。start 返回后观察整个应用及所有选中库的 Ready，不能以 setup 已绑定地址推断已激活。启动失败保留 StartupError 主因与 cleanup，cleanup 不完整时继续持原 Handle/Runtime 排空；观察失败也排空。

Unix 信号订阅先于 start 建立，启动期间的信号会保留至启动结束。Ready/Stopping 为单行 JSON，其他日志使用 tracing；未另建未鉴权健康 HTTP 服务。正常 SIGINT/SIGTERM 返回0，应用失败即使清理成功也返回非零。StopIncomplete 持同一个应用、Runtime 和资源反复 checked shutdown；每次 timeout 是等待窗口，不是总退出时限，不自动 force/exit。外部强杀不证明资源已排空，也不允许自动重放未知写入。

当前代码以 Linux x86_64 / Rust 1.94 为开发基线，正式binary远端与联合契约、三个dev镜像原生依赖和只读运行已通过。MQTT 库的 Rust 1.85 MSRV 未在本宿主实跑。发布前仍需包含新接口的业务库真实提交、正式Git依赖、干净远端CI及独立容量证据；本地 path/指纹不能替代远端可复现性。详见[配置](../configuration.md)、[验证边界](../validation.md)与[部署手册](../deployment.md)。
