# 本地镜像与运行

当前验收平台为 Linux x86_64，开发构建 profile 为 dev。三个服务镜像均由本仓库生成；FedEngine 仓库只交付库。业务库暂以同级 path 接入，镜像记录真实 Git HEAD、dirty、Cargo.lock/Feature/源码与二进制指纹。当前本地镜像不等于干净 Git 或远端可复现发布。

## 构建与验收

需要 Rust1.94、Python3.11+、Docker/Compose、curl、strip，以及同级两业务库与 locked Cargo 缓存。DuckDB v2.0.0 动态库来自 locked duckdb crate 的 download-lib 构建；镜像脚本强制比对 [runtime.lock.json](../docker/runtime.lock.json) 中的 SHA256。基础镜像固定 digest；只复制两个固定校验和扩展及生成的 manifest，不在服务启动时下载。

```sh
export CARGO_TARGET_DIR=/absolute/path/to/target
export DUCKDB_LIBRARY_DIR="$CARGO_TARGET_DIR/duckdb-download/x86_64-unknown-linux-gnu/v2.0.0"
cargo fetch --locked
cargo build --workspace --locked
python3 scripts/prepare-extensions.py --dest /absolute/path/to/prepared-extensions
./scripts/check.sh
./scripts/check-federation.sh --bin-dir "$CARGO_TARGET_DIR/debug" \
  --extensions-dir /absolute/path/to/prepared-extensions \
  --duckdb-library-dir "$DUCKDB_LIBRARY_DIR"
python3 scripts/build-images.py --tag-prefix mdh-local \
  --extensions-dir /absolute/path/to/prepared-extensions \
  --duckdb-library-dir "$DUCKDB_LIBRARY_DIR"
./scripts/check-images.sh --tag-prefix mdh-local
python3 scripts/verify-compose.py --tag-prefix mdh-local
```

`prepare-extensions.py` 下载前有大小边界，展开后比对固定 SHA256，缓存也重新校验。`build-images.py --require-clean` 在任一宿主/业务仓库未提交时拒绝构建，不自行提交或推送。`--profile release` 可选择优化构建；构建、容量和长时结果须按实际revision与profile单独验收。镜像构建不包含 push。

远端验收只创建随机名称、回环随机端口的一次性 PG17.7/MySQL5.7.44 容器，不读取用户业务 DSN。镜像专项用只读配置、独立临时数据卷运行真实协议、CAS、CRUD/JOIN/DDL、Portal/取消、阻塞原生 I/O 与 StopIncomplete，并重开原卷核对状态。Compose 专项使用六个默认开发端口；被占用时明确失败，不停止原有实例。用后只清理测试自身资源。

## 三个独立实例

```sh
export MDH_IMAGE_PREFIX=mdh-local
export MDH_IMAGE_PROFILE=dev
# 由当前会话或受控环境注入 FEDENGINE_PASSWORD，不写入镜像或仓库。
docker compose -f docker/compose.yaml up -d
```

[compose.yaml](../docker/compose.yaml) 使用 Linux host 网络保留回环边界，UID/GID65532、只读根文件系统、cap-drop ALL、no-new-privileges 与临时 /tmp。MQTT 镜像不包含 DuckDB；数据库及组合镜像各用独立 named volume 挂载 `/opt/magicdatahub/var`，DB、WAL、管理状态、凭据目录与 temp 不共享。默认不配置远端源。需要自定义时把完整配置树只读挂载到 `/opt/magicdatahub/config`；相对路径仍按每份文件解析，状态须落到可写卷，SecretRef 保持原格式。

| 实例 | MQTT | PGWire | 管理 HTTP |
|---|---|---|---|
| mqtt-server | 127.0.0.1:1883 | — | — |
| fedengine-server | — | 127.0.0.1:15432 | 127.0.0.1:15433 |
| datahub-server | 127.0.0.1:2883 | 127.0.0.1:25432 | 127.0.0.1:25433 |

从宿主机执行真实协议探针，镜像无需携带 shell、Python 或测试口令：

```sh
python3 scripts/probe-service.py --mqtt 127.0.0.1:1883
python3 scripts/probe-service.py --pgwire 127.0.0.1:15432 --management 127.0.0.1:15433
python3 scripts/probe-service.py --mqtt 127.0.0.1:2883 \
  --pgwire 127.0.0.1:25432 --management 127.0.0.1:25433
```

探针检查 MQTT CONNECT/PING、PG SCRAM/SELECT1 和 HTTP sources；PG 默认账号/数据库均为 fedengine，口令读取 FEDENGINE_PASSWORD，可显式指定名称及环境变量。只接受数字回环地址，失败返回非零且不输出远端正文/密钥。它证明所选入口能响应，不代表所有远端源健康。Ready/Stopping/GracefullyStopped 与框架日志用 `docker compose logs` 观察；StopIncomplete 阶段不应继续接收业务流量。

## 停机与恢复

正常停机只发送信号，然后等待实际退出：

```sh
docker compose -f docker/compose.yaml kill --signal SIGTERM
docker wait $(docker compose -f docker/compose.yaml ps --all --quiet)
```

检查退出码及 GracefullyStopped。StopIncomplete 表示原生 I/O/任务仍未排空，进程会持原 Runtime/Handle/租约继续等待，不因再次信号强制成功。Compose 的 stop/down 带超时强杀语义，正常排空应使用上述信号和等待方式；确认容器全部退出后才执行 `docker compose ... down`，保留数据卷。删除卷是显式数据销毁操作，运行手册不自动执行。

FailFast 运行故障返回非零；外部进程管理器可在进程实际退出后按运维策略重启。未知 SQL/DDL/管理写入不可自动重放；读取原管理状态及相同幂等 key 的回执后再判断。强杀或永不返回的原生 I/O 不在正常排空保证内，不能把强杀当成功。当前配置限回环；公网认证/TLS、MQTT 扩展、容量/RSS 与持久存储另行验收。

## CI 与真实版本固定

[CI](../.github/workflows/verify.yml)在push/PR自动执行不依赖外部checkout的源码格式、Python语法、证据门禁与Compose配置检查。完整三服务联合与镜像验收由手动触发，要求两项无默认值的实际40位提交SHA，分别checkout `muidea/modorch-mqtt`、`muidea/modorch-fedengine`，逐一核对HEAD/clean后执行依赖身份、本地/一次性远端、镜像与Compose运行门禁，日志只存CI artifact。读取私有仓库时需要该CI具备对应仓库读取权限。自动源码检查通过不等于联合验收通过。

首次源码提交可记录当前本地开发基线；正式Git集成须先使两个业务库包含当前接口的源码形成真实提交，再把宿主依赖改为这些实际 Git revision、更新锁和指纹，并在干净 checkout 执行所有门禁。工作流文件存在不代表远端执行通过，不能使用未包含接口的旧 HEAD 代替。手动工作流的同级 checkout 是过渡验证入口，不表示 Cargo 的 path 已切成正式 Git source。签名、镜像 registry 发布、release profile 与生产容量证据仍独立记录。
