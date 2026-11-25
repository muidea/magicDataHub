# 线上构建与发布交接清单

本文档记录将 `modorch-fedengine` 与 `magicDataHub` 推送至 GitHub 并触发 [release-images.yml](../.github/workflows/release-images.yml) 在线编译发布至 `ghcr.io/muidea` 的完整执行顺序（所有步骤由维护者核对后手动执行）。

## 1. 发布 5 个定制 DuckDB v2.0.0 扩展的 GitHub Release

经核查当前 GitHub 状态：
- `muidea/duckdb-postgres` 与 `muidea/duckdb-mysql` 已公开且存在 `2.0.0` 分支，但当前均无 GitHub Release 资产；
- `muidea/oracle_scanner`、`muidea/sqlserver_scanner`、`muidea/odbc_catalog_scanner` 当前在 GitHub 公开 API 上不可见（需推送对应仓库至 `github.com/muidea/` 的 `2.0.0` 分支并确保公开或配置只读 Token）。

按[定制扩展发布指南](extension-release.md)为 5 个仓库部署 `.github/workflows/release-extension.yml`、推送 `v2.0.0-<rev>` 标签后，在 `magicDataHub` 更新 `scripts/acceptance/sources.json`：

```sh
python3 scripts/update-extension-lock.py --ext postgres     --tag v2.0.0-<rev>
python3 scripts/update-extension-lock.py --ext mysql        --tag v2.0.0-<rev>
python3 scripts/update-extension-lock.py --ext oracle       --tag v2.0.0-<rev>
python3 scripts/update-extension-lock.py --ext sqlserver    --tag v2.0.0-<rev>
python3 scripts/update-extension-lock.py --ext odbc_catalog --tag v2.0.0-<rev>
python3 scripts/prepare-extensions.py --fetch-releases --dest /tmp/verify-fetch-ext
```

## 2. 提交并推送 `modorch-fedengine`

当前 `modorch-fedengine` 本地已有 `6ae9422`（领先 `origin/master` 1 个提交）以及模块化插件能力目录改造（`plugins.rs`、multi-dir 扩展加载与 `/api/v1/plugins`）。审核暂存并推送后记下新的 40 位 `HEAD` SHA（记为 `<FEDENGINE_SHA>`）：

```sh
cd ../modorch-fedengine
git status --short
# 审核并提交全部改动后执行:
git push origin master
git rev-parse HEAD
```

当前 `modorch-mqtt` 的 `origin/master` 已固定在 `ebc502edb12324f563bc06c4bee20c297f84fb0d`。

## 3. 将 `magicDataHub/Cargo.toml` 切换为固定 Git revision

`modorch-fedengine` 推送完成后，将 [Cargo.toml](../Cargo.toml#L15-L17) 的两处同级 `path` 依赖切换为固定 Git `rev`（完全脱离本地同级源码依赖）：

```diff
--- a/Cargo.toml
+++ b/Cargo.toml
@@ -14,7 +14,6 @@ repository = "https://github.com/muidea/magicDataHub"
 modorch = { version = "=0.0.3", git = "https://github.com/muidea/modorch-rs", rev = "336ba20196044e1b81fb2ca297a18b5e258a5800" }
-# Local source until updated modorch-mqtt changes are pushed to origin/master.
-modorch-mqtt = { version = "=0.1.0-alpha.1", path = "../modorch-mqtt/crates/modorch-mqtt", features = ["toml", "mqtt5"] }
-modorch-fedengine = { version = "=0.1.0", path = "../modorch-fedengine/crates/modorch-fedengine" }
+modorch-mqtt = { version = "=0.1.0-alpha.1", git = "https://github.com/muidea/modorch-mqtt", rev = "ebc502edb12324f563bc06c4bee20c297f84fb0d", features = ["toml", "mqtt5"] }
+modorch-fedengine = { version = "=0.1.0", git = "https://github.com/muidea/modorch-fedengine", rev = "<FEDENGINE_SHA>" }
```

随后刷新锁文件、更新源码身份指纹并运行本地全量校验：

```sh
cargo update -p modorch-mqtt -p modorch-fedengine
python3 scripts/verify-dependency-identity.py --write-identity
./scripts/check.sh
```

## 4. 推送 `magicDataHub` 并触发 Actions 在线编译发布

1. 提交并推送 `magicDataHub`（含已更新的 `sources.json`、`Cargo.toml`、`Cargo.lock`、`scripts/source-identity.json` 与 `.github/workflows/release-images.yml`）：
   ```sh
   git status --short
   # 审核并提交全部改动后执行:
   git push origin master
   ```
2. 建议先在 GitHub Actions 手动运行 `Release Core and Plugin Images`（`workflow_dispatch`：`target=all`，`profile=release`，`push=false`）确认完整构筑与验证全绿（若 4 套商业 ODBC 驱动需由默认固定基础镜像替换为独立厂商源，可在仓库 Variables 设置 `MDH_<VENDOR>_ODBC_URL` 与 `MDH_<VENDOR>_ODBC_SHA256`）；
3. 验证无误后推送发行标签触发正式发布至 `ghcr.io/muidea`：
   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```
   流水线会自动构筑并验证全部 3 个核心镜像与 5 个插件镜像，全部门禁通过后推送 `:v0.1.0` 与 `:latest`。

## 5. 可选：本地开发实例迁移说明

当前运行于 `var/deployment` 的开发实例保留原有端口、数据卷与 `mdh-clean/*:dev` 镜像，不会在未确认的情况下被脚本修改。如需切换为模块化 bridge 容器形态，可先按停机手册优雅排空后使用 [deploy/docker-compose.yml](../deploy/docker-compose.yml) 启动。
