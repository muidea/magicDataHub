# 配置与运行

每个服务使用宿主配置及独立业务配置。`--config FILE [--check-config]` 为唯一运行方式；`--help` / `--version` 单独使用。未知字段、重复/未知参数及缺少必需库都明确拒绝。

| 服务 | 开发配置 | 监听 |
|---|---|---|
| mqtt-server | config/mqtt-development.toml | MQTT 127.0.0.1:1883 |
| fedengine-server | config/fedengine-development.toml | PG 15432、管理 HTTP 15433，均回环 |
| datahub-server | config/combined-development.toml | MQTT 2883、PG 25432、管理 HTTP 25433，均回环 |

传入相对宿主路径时先按启动 CWD 找到文件并 canonicalize；宿主引用相对宿主文件目录，业务文件再次 canonicalize 后，其 DB/temp/credentials/extensions/TLS 路径相对业务文件自身目录冻结为绝对路径。符号链接采用实际文件所在目录。业务数据路径不要求事先存在，不主动 canonicalize 数据文件。SecretRef 原样保留，源 `file:` 引用仍按库的 credentials_directory 解析。

FedEngine application.name 必须与宿主一致。默认用户 fedengine 的 PG 口令由 `FEDENGINE_PASSWORD` 提供；检查配置不读取它。开发配置没有远端源，显式空虚拟库 fedengine 允许本地表达式查询。管理状态分别存入 var/standalone、var/combined；重复启动复用当前状态，不清库、不迁移、不覆盖旧凭据。多实例必须自行分配独立可写目录和端口。管理 HTTP 为库现有回环接口；Origin 不是管理员鉴权，当前配置不承诺公网生产部署。

`--check-config` 在创建 Tokio Runtime 前完成：限量读取 TOML、公开库值校验、Profile、名称、路径、监听重叠及需求预算检查，不打开 DB/扩展/密钥、不监听或访问远端。宿主/FedEngine 文档上限1MiB，MQTT采用库64KiB上限。错误不回显 TOML 内容。它不证明端口空闲、数据库可锁定、凭据存在或远端可达。FedEngine HTTP-only 不读取或校验未选 PG 的专属认证/TLS字段。

## 开发预算

所有字段位于宿主 `[runtime]`，省略时采用下表有界默认。非法零值、过大值、相互矛盾的上限和需求溢出在 start 前拒绝。任务/等待队列上限最多1,000,000，线程1–4096，Hub各字节上限最多1GiB，具体关系由配置校验维护；这些是配置硬边界，不是推荐生产规模。

| 配置 | 默认 |
|---|---|
| async_threads / tokio_blocking_threads | 2 / 8 |
| max_tasks / max_timers / max_tasks_per_owner | 2048 / 64 / 512 |
| cpu_threads / cpu_capacity | 2 / 8 |
| blocking_io_threads / blocking_io_capacity | 8 / 128 |
| max_waiters / max_waiters_per_owner | 512 / 128 |
| hub_max_waiters / hub_max_waiters_per_target | 256 / 64 |
| hub_max_message_bytes | 1MiB |
| hub_max_in_flight_bytes / hub_max_in_flight_bytes_per_target | 16MiB / 8MiB |
| hub_max_waiting_bytes | 8MiB |
| queue_capacity | 64 |
| shutdown_timeout_ms / shutdown_retry_ms | 30000 / 100 |

宿主控制余量为8任务/1计时器；MQTT需求为连接数+2个长期任务、1计时器。FedEngine使用公开 resource_requirements 的I/O线程/容量及Owner建议，全局任务保守取Owner建议×所选组件数、1计时器。全局任务/计时器/执行需求 checked sum，Owner上限取最大值。这是保守准入下界检查，不保证联合满载或控制面优先级。公开库内部的协议、队列、连接和结果预算仍分别生效。

Tokio线程与框架CPU/阻塞I/O执行资源分别配置。Hub按声明的逻辑输入成本计费；FedEngine入口及下游控制请求均声明成本，不等于深度RSS、已生成目录或结果流总量。开发配置另有DuckDB memory_limit=1GB、管理元数据512MiB总/256MiB单操作，以及MQTT默认逻辑内存预算；这些不能直接相加视作进程RSS保证。生产容量、慢I/O混合流量和OS/container限制需另行验收。

库的请求/排空超时保持原义，宿主 shutdown_timeout_ms 仅控制每次 checked shutdown 的等待窗口；小于原生操作时可能产生 StopIncomplete 并继续重试。不要把窗口当作取消已回滚或总寿命上限。
