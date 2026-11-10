# 宿主契约证据

[mapping.json](mapping.json) 将实施方案的 MDH-COMP-001—012 映射到真正执行的 Rust 用例与正式 binary 远端套件。镜像单列 MDH-IMAGES-001，完整管理生命周期与真实 JDBC 单列 MDH-WORKFLOW-001。每条规则的所有 scope 都通过才覆盖该规则，单个 scope 不代表整个交付通过。

`scripts/check.sh`、`scripts/check-federation.sh`、`scripts/check-images.sh`、`scripts/check-workflow.sh` 先要求进程成功退出，再核对精确的 `test NAME ... ok` 行。missing、ignored、名称近似、FAILED 不能充当成功；Python 反例保护这一行为。直接 `--results` 是对已成功 runner 输出的复核，不能证明日志来源或单独代替 runner 的退出码。

远端 marker 在每个正式 binary 的协议、真实 PG/MySQL 操作、并发 MQTT（组合服务）、Portal/取消/慢 I/O、CAS、信号和原状态重开全部完成后才输出。权限及共享 Client 准入另要求真实执行 owned-fixture ignored Rust 用例。

workflow scope 要求两个宿主各自的闭环和 JDBC 共四个 marker；真实 Java 进程成功并核对探针结束标记后才输出 JDBC marker，停用/恢复、重启、最终删除与排空完成后才输出闭环 marker。驱动/JDK/扩展前提缺失是失败，不跳过必需范围。它不证明 GUI 操作、任意源兼容或扩展数据准确性。

这些验收限定于固定版本源、受控流量和逻辑配额；没有证明生产容量、长期稳定性、进程 RSS 硬上限或任意原生永久卡死可中止。
