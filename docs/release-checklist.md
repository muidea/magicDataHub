# 线上构建与发布运行手册

本文档记录 `magicDataHub` 的线上发行版本锁定基线，以及通过 [release-images.yml](../.github/workflows/release-images.yml) 在线编译并发布核心与按需插件 OCI 镜像至 `ghcr.io/muidea` 的标准流程。

## 1. 线上发行基线档案

### `v0.2.0` 锁定基线（9-DB DDL/DML 补齐与跨库时间/会话时区统一）

- **固定上游 Git revision**（见 [Cargo.toml](../Cargo.toml#L13-L16)）：
  - `modorch`：`https://github.com/muidea/modorch-rs` @ `336ba20196044e1b81fb2ca297a18b5e258a5800`
  - `modorch-mqtt`：`https://github.com/muidea/modorch-mqtt` @ `ebc502edb12324f563bc06c4bee20c297f84fb0d`
  - `modorch-fedengine`：`https://github.com/muidea/modorch-fedengine` @ `e924bb966e8013aa7b69c8270733e23149b0960d`
- **已锁定的 5 个定制 DuckDB v2.0.0 扩展**（全部公开于 GitHub Releases 并锁定于 [sources.json](../scripts/acceptance/sources.json)）：
  - `postgres`（`muidea/duckdb-postgres`）：`v2.0.0-b0e0dca61e`
  - `mysql`（`muidea/duckdb-mysql`）：`v2.0.0-eb4f93233f`
  - `oracle`（`muidea/oracle_scanner`）：`v2.0.0-8d68b62aad`
  - `sqlserver`（`muidea/sqlserver_scanner`）：`v2.0.0-0be5f0416c`
  - `odbc_catalog`（`muidea/odbc_catalog_scanner`）：`v2.0.0-d7b5e18e55`

### 已发布的 `v0.1.0` 历史基线

- **发行标签与流水线**：Git 标签 `v0.1.0`（commit `f81742ea20bbaa5afa8eb427f86ed2b57de9dbe7`）已由 GitHub Actions `Release Core and Plugin Images`（Run `45567971698`）全绿完成，通过 `MDH-IDENTITY-001`、`MDH-IMAGES-001` 与 `MDH-PLUGINS-001` 全部在线门禁。
- **已公开推送的 `ghcr.io/muidea` 镜像**（均带 `:v0.1.0` 与 `:latest` 标签）：
  - 核心服务镜像：`ghcr.io/muidea/mqtt-server`、`ghcr.io/muidea/fedengine-server`、`ghcr.io/muidea/datahub-server`
  - 按需插件 Init Container 镜像：`ghcr.io/muidea/datahub-plugin-sqlserver`、`ghcr.io/muidea/datahub-plugin-oracle`、`ghcr.io/muidea/datahub-plugin-db2`、`ghcr.io/muidea/datahub-plugin-hana`、`ghcr.io/muidea/datahub-plugin-dameng`
- **固定上游 Git revision**（见 [Cargo.toml](../Cargo.toml#L13-L16)）：
  - `modorch`：`https://github.com/muidea/modorch-rs` @ `336ba20196044e1b81fb2ca297a18b5e258a5800`
  - `modorch-mqtt`：`https://github.com/muidea/modorch-mqtt` @ `ebc502edb12324f563bc06c4bee20c297f84fb0d`
  - `modorch-fedengine`：`https://github.com/muidea/modorch-fedengine` @ `f858bb5a77d90231499823ca543f623489e725a5`
- **已锁定的 5 个定制 DuckDB v2.0.0 扩展**（全部公开于 GitHub Releases 并锁定于 [sources.json](../scripts/acceptance/sources.json)）：
  - `postgres`（`muidea/duckdb-postgres`）：`v2.0.0-919558cd4a`
  - `mysql`（`muidea/duckdb-mysql`）：`v2.0.0-ac9da98f9a`
  - `oracle`（`muidea/oracle_scanner`）：`v2.0.0-c03164082e`
  - `sqlserver`（`muidea/sqlserver_scanner`）：`v2.0.0-897d4bc005`
  - `odbc_catalog`（`muidea/odbc_catalog_scanner`）：`v2.0.0-b0ca053cf1`

## 2. 后续版本（`vX.Y.Z`）标准发版流程

1. **如升级定制 DuckDB 扩展**：按[定制扩展发布指南](extension-release.md)在目标扩展仓库的 `2.0.0` 分支推送 `v2.0.0-<rev>` 标签，发布完成后在本仓库更新并校验锁定文件：
   ```sh
   python3 scripts/update-extension-lock.py --ext <key> --tag v2.0.0-<rev>
   python3 scripts/prepare-extensions.py --fetch-releases --dest /tmp/verify-fetch-ext
   ```
2. **如升级上游业务库 / 框架**：先将对应仓库真实提交推送至 `origin/master`，更新 [Cargo.toml](../Cargo.toml#L13-L16) 固定 `rev`，随后刷新锁文件、更新源码身份指纹并运行全量门禁：
   ```sh
   cargo update -p modorch -p modorch-mqtt -p modorch-fedengine
   python3 scripts/verify-dependency-identity.py --write-identity
   ./scripts/check.sh
   ```
3. **预演验证并推送正式发行标签**：
   - 将审核后的改动提交并推送至 `origin master`；
   - 建议先在 GitHub Actions 手动触发 `Release Core and Plugin Images`（`workflow_dispatch`：`target=all`，`profile=release`，`push=false`）确认完整构筑与验证全绿（若商业 ODBC 驱动需由默认固定基础镜像替换为独立厂商源，可在仓库 Variables 设置 `MDH_<VENDOR>_ODBC_URL` 与 `MDH_<VENDOR>_ODBC_SHA256`）；
   - 验证无误后推送新发行标签触发正式发布至 `ghcr.io/muidea`：
     ```sh
     git tag vX.Y.Z
     git push origin vX.Y.Z
     ```
     流水线会自动构筑并验证全部 3 个核心镜像与 5 个插件镜像，全部门禁通过后推送 `:vX.Y.Z` 与 `:latest`。随后可在 GitHub Releases 页面为该标签补充发行说明。

## 3. 本地开发实例迁移说明

当前运行于 `var/deployment` 的开发实例保留原有端口、数据卷与 `mdh-clean/*:dev` 镜像，不会在未确认的情况下被脚本修改。如需切换为模块化 bridge 容器形态，可先按停机手册优雅排空后使用 [deploy/docker-compose.yml](../deploy/docker-compose.yml) 启动。
