# 源码提交边界

提交应形成可构建、可验证的开发基线。服务、库、框架分别属于独立Git仓库，不在宿主中复制业务库、框架源码或外部方案历史。当前本地开发依赖要求modorch-mqtt与modorch-fedengine处于同级目录，精确约束和框架Git revision以Cargo.toml、Cargo.lock及源码身份门禁为准。

## 应提交内容

| 路径 | 内容 |
|---|---|
| Cargo.toml、Cargo.lock、rust-toolchain.toml | 七package workspace、锁定依赖、工具链；保留服务应用的根锁文件 |
| apps/、crates/ | 三个正式入口、公共配置/runner、两个独立业务适配及测试 |
| config/ | 匿名回环/内存MQTT及FedEngine开发配置；口令和源连接只保留SecretRef |
| tests/、scripts/ | 可复用协议/远端/镜像测试、契约映射与证据门禁、资源准备和构建入口 |
| tools/mqtt-broker-validation/ | 项目内 MQTT 黑盒验证工具、测试、场景目录和依赖锁；不含外部源码、缓存与逐次运行产物 |
| docker/、.github/workflows/ | 固定运行依赖、Dockerfile、Compose与CI；不包含构建后的镜像 |
| README.md、AGENTS.md、LICENSE、.gitignore、docs/ | 使用、开发、配置、架构、运行及能力边界 |

只读检查候选范围，包含未跟踪源码与已有文件的删除：

```sh
git status --short --untracked-files=all
git add --dry-run --all -- .
git diff --check
```

## 本地保留与可清理内容

`target/`、下载的原生文件/扩展、`var/`实例状态、`secrets/`、`.env*`、`config/**/*.local.toml`、临时验证目录和日志均排除提交。`.env.example`、`.env.sample`仅可放公开示例。实例数据库、WAL、实际凭据和本地配置必须按运行用途保留，不能当缓存删除；共享Cargo/Rustup/构建缓存同样保留。

Python字节码、编辑器临时文件及已结束测试创建的日志/目录可以清理。临时报告、截图、逐次计数和阶段过程记录不归档；长期维护能力边界、支持矩阵、固定查询和可复用预期夹具。清理不更改生产实现或为匹配文件而刷新依赖指纹。

## 验证与仓库顺序

按[验证入口](validation.md)执行对应检查。源码/配置变化执行适用的fmt/check/Clippy/test/doc及真实协议；单纯文档/忽略规则整理核对链接、格式、候选文件与排除规则。完整首次实现基线应保留此前已验证的测试和契约，不因其文件较多而删减。

正式Git集成顺序为：先形成MQTT显式监督入口与FedEngine公开Facade的真实业务库提交，再将宿主切至这些实际Git revision，更新锁/指纹并在干净checkout运行门禁。当前同级path可作为首次本地开发基线提交，但不能声明为仅克隆宿主即可远端复现的正式交付。MQTT与FedEngine也各自提交完整源码/测试/契约/文档及必要Skill快照；FedEngine的旧目录更名和旧服务入口删除必须一并包含。

经审阅实际暂存后，再核对：

```sh
git diff --cached --stat --find-renames
git diff --cached --name-status --find-renames
git diff --cached --check
git diff --cached
```

这些检查不自动暂存、创建提交、push、Tag或发布。已存在的暂存内容与此前业务修改应保留，由维护者审阅实际提交范围。
