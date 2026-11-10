# magicDataHub 开发规则

本仓库交付 mqtt-server、fedengine-server、datahub-server。只使用公开 Modorch/业务 Facade；一个服务实例一个 Application/Hub。业务协议、SQL、数据模型和 Topic 归业务库，不复制其实现或测试宿主。

首轮显式 FailFast；MQTT 匿名回环、内存、3.1.1，扩展/桥接后续单列。保留原配置/SecretRef/既有管理状态；无历史迁移、自动清库或故障后重放写入。注册无资源副作用，实际 IDs 显式选择。宿主统一预算、信号、日志和 checked shutdown；StopIncomplete 保留 Runtime/Handle/实际资源，Forced 不当成功。

配置与依赖规则、运行边界见 docs/architecture/multi-application-composition.md。临时日志、报告和截图放临时目录/CI artifact；仓库只留可复用测试和正式能力说明。库的本地 path 接入不等于远端提交或镜像可复现，不自动提交、push、Tag 或发布。

提交文件范围和检查步骤见 docs/git-submission.md；保留源码、测试夹具、契约映射、锁文件和构建/运行入口，排除实例数据、凭据和逐次验收产物。清理不删除实例状态、共享构建缓存或业务库源码。
