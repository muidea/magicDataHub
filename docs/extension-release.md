# 定制 DuckDB v2.0.0 扩展在线发布与锁定指南

`magicDataHub` 的核心镜像与按需 OCI 插件镜像依赖以下 5 个定制 DuckDB v2.0.0 扩展（不从官方 `extensions.duckdb.org` 下载）：

| 逻辑键（`sources.json`） | 上游仓库 | 分支 | 运行归属 |
|---|---|---|---|
| `postgres` | `muidea/duckdb-postgres` | `2.0.0` | 核心预装（PG / Greenplum / KingbaseES） |
| `mysql` | `muidea/duckdb-mysql` | `2.0.0` | 核心预装（MySQL / MariaDB / StarRocks） |
| `oracle` | `muidea/oracle_scanner` | `2.0.0` | 插件镜像 `datahub-plugin-oracle` |
| `sqlserver` | `muidea/sqlserver_scanner` | `2.0.0` | 插件镜像 `datahub-plugin-sqlserver` |
| `odbc_catalog` | `muidea/odbc_catalog_scanner` | `2.0.0` | 插件镜像 `datahub-plugin-{db2,hana,dameng}` 共享基础层 |

## 1. 为每个扩展仓库配置 GitHub Actions 发布流水线

将模板 [duckdb-extension-release.yml](../docker/templates/duckdb-extension-release.yml) 复制到每个扩展仓库的 `2.0.0` 分支 `.github/workflows/release-extension.yml`：

1. 在目标扩展仓（`2.0.0` 分支）中推送标签 `v2.0.0-<short_sha>` 或手动触发工作流；
2. 流水线在 `ubuntu-24.04` 上运行 `GEN=ninja STATIC_LIBCPP=1 make release`，校验 `ldd` 缺失依赖、ELF 导出 guarded CRUD 与 `*_duckdb_cpp_init` 符号（`nm -D`）以及文件尾标（`v2.0.0`、`linux_amd64`）；
3. 流水线自动发布 GitHub Release，附件包含：
   - `<EXT_NAME>-v2.0.0-linux_amd64.duckdb_extension.gz`
   - `SHA256SUMS.txt`
   - `release-metadata.json`

## 2. 在 magicDataHub 中更新并锁定扩展 SHA-256

当某扩展仓库发布了新的 GitHub Release 标签（如 `v2.0.0-abc1234567`）后，在 `magicDataHub` 中运行 [update-extension-lock.py](../scripts/update-extension-lock.py) 更新 [sources.json](../scripts/acceptance/sources.json)：

```sh
python3 scripts/update-extension-lock.py --ext postgres --tag v2.0.0-abc1234567
```

工具会自动下载 `.gz` 资产、校验 URL（禁止官方 `duckdb.org` 源）、计算解压后的 `.duckdb_extension` SHA-256、解析 512 字节 DuckDB 扩展尾标锁定完整 `v2.0.0-<rev>` 版本字符串并原子写入 `scripts/acceptance/sources.json`。若为私有扩展仓调试或临时缓存验证，可导出只读 `MDH_EXTENSIONS_TOKEN` 或运行：

```sh
python3 scripts/prepare-extensions.py --fetch-releases --only postgres --dest /tmp/test-ext
```
