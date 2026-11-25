# 本地镜像与运行

当前验收平台为 Linux x86_64，开发构建 profile 为 dev。三个核心服务镜像与按需插件镜像均由本仓库生成；FedEngine 仓库只交付库。`modorch-mqtt` 与 `modorch-fedengine` 均已在 `Cargo.toml` 中固定正式 Git revision，本地与线上镜像均记录真实 Git HEAD、dirty、Cargo.lock/Feature/源码与二进制指纹；本地未提交改动或 dev profile 构建不等于远端发行版本。

## 构建与验收

需要 Rust1.94、Python3.11+、Docker/Compose、curl、strip，以及 locked Cargo 缓存（两业务库已固定 Git revision，仅本地临时改库调试才需同级目录）。DuckDB v2.0.0 动态库来自 locked duckdb crate 的 download-lib 构建；镜像脚本强制比对 [runtime.lock.json](../docker/runtime.lock.json) 中的 SHA256。核心镜像只预装 `postgres`/`mysql` 两个固定校验和扩展及生成的 manifest，其余扩展由按需 OCI 插件镜像分发，不在服务启动时下载。

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
./scripts/check-workflow.sh --bin-dir "$CARGO_TARGET_DIR/debug" \
  --extensions-dir /absolute/path/to/prepared-extensions \
  --duckdb-library-dir "$DUCKDB_LIBRARY_DIR" \
  --jdbc-jar /absolute/path/to/postgresql-42.7.8.jar
python3 scripts/build-images.py --tag-prefix mdh-local \
  --extensions-dir /absolute/path/to/prepared-extensions \
  --duckdb-library-dir "$DUCKDB_LIBRARY_DIR"
./scripts/check-images.sh --tag-prefix mdh-local
python3 scripts/verify-compose.py --tag-prefix mdh-local
```

`prepare-extensions.py` 下载前有大小边界，展开后比对固定 SHA256，缓存也重新校验。`check-workflow.sh` 验证完整管理生命周期与真实 JDBC，驱动校验和及 JDK 镜像固定要求见[验证入口](validation.md#整体管理流程与-jdbc)。`build-images.py --require-clean` 在任一宿主/业务仓库未提交时拒绝构建，不自行提交或推送。`--profile release` 可选择优化构建；构建、容量和长时结果须按实际revision与profile单独验收。镜像构建不包含 push。

远端验收只创建随机名称、回环随机端口的一次性 PG17.7/MySQL5.7.44 容器，不读取用户业务 DSN。镜像专项用只读配置、独立临时数据卷运行真实协议、CAS、CRUD/JOIN/DDL、Portal/取消、阻塞原生 I/O 与 StopIncomplete，并重开原卷核对状态。Compose 专项使用六个默认开发端口；被占用时明确失败，不停止原有实例。用后只清理测试自身资源。

## 三个独立实例

真实源接入使用业务库的实例发现和 `organization` 登记流程，按产品、规范化实例身份及原生数据库精确名称分组。同一原生库的多个来源合并到一个自动虚拟库，不同端口或不同原生库分开；PG 保留原 Schema，MySQL 在独立虚拟库中保留源 database 同名 Schema。不要把所有来源集中到测试汇总库，也不要用来源别名替换 Schema 名。具体接口见业务库的管理 API 文档。

可用 `scripts/verify-source-organization.py` 对照本地部署计划核对 HTTP、PGWire 目录与实际读取。计划文件不提交，`sources` 每项包含 `id/kind/host/port/database/schema/virtual_database`，`account_databases` 列出保留的账号默认库；凭据仍通过 SecretRef 和环境变量传入。登记失败目标单列为 inactive，组织检查通过不代表全部来源已激活。

```sh
python3 scripts/verify-source-organization.py --plan /absolute/path/to/native-organization.json \
  --management 15433 --pgwire 15432
python3 scripts/verify-source-organization.py --plan /absolute/path/to/native-organization.json \
  --management 25433 --pgwire 25432
```

```sh
export MDH_IMAGE_PREFIX=mdh-local
export MDH_IMAGE_PROFILE=dev
# 由当前会话或受控环境注入 FEDENGINE_PASSWORD，不写入镜像或仓库。
docker compose -f docker/compose.yaml up -d
```

[compose.yaml](../docker/compose.yaml) 使用标准 Docker `bridge` 网络配合 `-p` 端口映射（默认绑定 `${MDH_BIND_HOST:-127.0.0.1}` 保留回环边界，可设 `MDH_BIND_HOST=0.0.0.0` 对外映射），并保持 UID/GID 65532、只读根文件系统、cap-drop ALL、no-new-privileges、临时 `/tmp` 与 60s 优雅停机宽限期。MQTT 镜像不包含 DuckDB；数据库及组合镜像各用独立 named volume 挂载 `/opt/magicdatahub/var`，DB、WAL、管理状态、凭据目录与 temp 不共享，并额外为可选插件挂载只读共享卷 `/opt/magicdatahub/plugins`。默认不配置远端源。需要自定义时把完整配置树只读挂载到 `/opt/magicdatahub/config`；相对路径仍按每份文件解析，状态须落到可写卷，SecretRef 保持原格式。

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

## 模块化发行镜像与按需 OCI 插件

面向最终用户的 `ghcr.io/muidea` 发布镜像采用“核心预装常用开源库 + 按需 OCI 插件 Init Container + 标准 bridge 网络 `-p` 端口映射”架构：

- **核心服务镜像**：`fedengine-server` 与 `datahub-server` 基于轻量开源运行时（[runtime-core.Dockerfile](../docker/runtime-core.Dockerfile)），采用 DuckDB 核心库 / 预装扩展（`postgres`、`mysql`）/ 服务二进制与配置三层拆包（[Dockerfile](../docker/Dockerfile)），默认内置支持 PostgreSQL / Greenplum / KingbaseES / MySQL / MariaDB / StarRocks，监听容器端口 `5432`（PGWire）、`8080`（HTTP/Web UI）、`1883`（MQTT）。
- **按需 OCI 插件镜像**：`datahub-plugin-{sqlserver,oracle,db2,hana,dameng}` 以非 root 只读 distroless Init Container（`mdh-plugin-init`）分发，并将对应扩展与厂商 ODBC 驱动同步到共享只读插件卷 `/opt/magicdatahub/plugins`，由 `unixODBC` 通过 `ODBCSYSINI=/opt/magicdatahub/plugins/odbc` 与预置 `LD_LIBRARY_PATH` 加载；其中 `db2`/`hana`/`dameng` 共享同一个 `odbc_catalog_scanner` 基础层。
- **能力查询与用户编排**：管理 API `GET /api/v1/plugins` 返回 11 类数据源的扩展与 ODBC 驱动就绪状态；用户直接使用 [deploy/docker-compose.yml](../deploy/docker-compose.yml)（如 `docker compose up -d` 或 `docker compose --profile oracle --profile dameng up -d`）。
- **统一打包、在线构建与验证**：
  - [release-images.yml](../.github/workflows/release-images.yml) 支持按 `v*` 标签或 `workflow_dispatch`（`all` / `core-only` / `plugins-only`）在线编译并发布全部核心与按需插件镜像至 `ghcr.io/muidea`；
  - [prepare-extensions.py](../scripts/prepare-extensions.py) 支持 `--fetch-releases` 从 `scripts/acceptance/sources.json` 固定的 5 个定制扩展 GitHub Release 资产拉取、解压并校验 SHA-256（强制屏蔽官方 `extensions.duckdb.org`）；
  - [stage-vendor-drivers.py](../scripts/stage-vendor-drivers.py) 支持 `--vendor-drivers-source image` 从 digest 固定的 `muidea/magicdatahub-runtime-base` 提取商业 ODBC 驱动，并支持通过 `MDH_{ORACLE,DB2,HANA,DAMENG}_ODBC_URL` / `MDH_<VENDOR>_ODBC_SHA256` 变量单独覆盖单个厂商驱动；
  - `scripts/release-images.py --tag-prefix ghcr.io/muidea --version v0.1.0 --also-latest --verify` 统一完成构建、[verify-images.py](../scripts/verify-images.py) 核心验证与 [verify-plugins.py](../scripts/verify-plugins.py) 插件及 `ldd` 完整性检查（并支持 `--target plugins-only --plugins <list>` 单独验证指定插件镜像）。

## CI 与真实版本固定

[verify.yml](../.github/workflows/verify.yml) 在 push/PR 自动执行不依赖外部 checkout 的源码格式、Python 语法、YAML 与工作流校验、证据门禁与 Compose 配置检查；[release-images.yml](../.github/workflows/release-images.yml) 在手动触发或 `v*` 标签推送时在线下载已固定的定制扩展发布包、编译 Rust 二进制、打包核心与插件 OCI 镜像并完成全套验证后推送至 `ghcr.io/muidea`。

框架与两业务库均已固定正式 Git revision，首个线上发行版 `v0.1.0` 已发布至 `ghcr.io/muidea`；版本基线与后续发版执行步骤见[线上构建与发布运行手册](release-checklist.md)与[定制 DuckDB 扩展发布与锁定](extension-release.md)。
