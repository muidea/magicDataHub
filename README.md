# magicDataHub

基于 Modorch-RS `=0.0.3` 的服务编排仓库，提供 `mqtt-server`、`fedengine-server`、`datahub-server`。单服务分别装配 MQTT / FedEngine 库，组合服务在一个 Application/EventHub 中装配两库。三个服务共用宿主预算、Ready、日志、信号与 checked shutdown，首轮统一 FailFast。

当前发布基线需要 Rust 1.94；框架与业务库（`modorch-rs`、`modorch-mqtt`、`modorch-fedengine`）均已在 [Cargo.toml](Cargo.toml) 中固定至精确 Git revision。

```sh
cargo build -p mqtt-server --locked
cargo run -p mqtt-server --locked -- --config config/mqtt-development.toml --check-config
cargo run -p mqtt-server --locked -- --config config/mqtt-development.toml
```

数据库及组合服务分别使用 config/fedengine-development.toml、config/combined-development.toml，PG入口读取 `FEDENGINE_PASSWORD`。独立构建mqtt-server不包含FedEngine/DuckDB。数据库服务需要匹配libduckdb.so及其系统动态链接环境；`--check-config`不启动Runtime、不读口令或创建数据库。开发监听均为回环。

- [架构与依赖边界](docs/architecture/multi-application-composition.md)
- [配置、路径与预算](docs/configuration.md)
- [验证入口与能力边界](docs/validation.md)
- [MQTT Broker 黑盒验证工具](tools/mqtt-broker-validation/README.md)
- [Docker 快速启动与按需插件部署](deploy/README.md)
- [本地镜像、协议探针与停机](docs/deployment.md)
- [定制 DuckDB 扩展发布与锁定](docs/extension-release.md)
- [线上构建与发布交接清单](docs/release-checklist.md)
- [源码提交边界](docs/git-submission.md)

框架固定 Git `https://github.com/muidea/modorch-rs`，rev `336ba20196044e1b81fb2ca297a18b5e258a5800`。FedEngine 仓库为 `git@github.com:muidea/modorch-fedengine.git`，只作为库交付；所有正式服务和镜像归本项目。

三个服务、模块化容器镜像与线上发行流水线已实现，运行镜像支持非 root（65532:65532）、只读根文件系统、独立数据卷及按需 OCI 数据库插件挂载；框架与两业务库已固定正式 Git revision。MQTT→SQL桥接与MQTT可选扩展后续单列。
