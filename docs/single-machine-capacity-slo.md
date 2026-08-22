# 单机容量 SLO 与基准

## 适用边界

本 SLO 只覆盖单进程、单写者、Linux/POSIX、本地支持 `fsync` 的 SSD。参考机器为 4 个独占 x86_64 vCPU、16 GiB 内存；网络文件系统、共享卷、多进程写入和跨主机 Agent 不在承诺范围内。

支持包络定义在 `config/single-machine-capacity-slo.json`：最多 1,000 个保留任务、每任务 25 个控制面事件、100 个活动任务、30 个并发专业 Agent 实例、10 GiB 控制面元数据。单任务资产仍受运行配置中的文件/任务字节上限约束，不计入本存储基准。

在参考包络内：

- 任务创建和元数据更新延迟 P95 各不超过 500 ms；
- 任务索引读取 P95 不超过 250 ms；
- 干净关闭后的冷恢复不超过 30 s；
- 控制面元数据平均不超过 1 MiB/任务。

这些是单机容量门槛，不是多租户或跨主机可用性承诺。超过任一包络或连续两次 qualification 失败，应先优化增量索引/事件查找；仍不满足时触发 PostgreSQL、对象存储和持久队列迁移评估。

## 执行方式

CI 运行小数据集、同代码路径的回归守卫：

```bash
make capacity-benchmark
```

发布候选在参考机器运行完整 qualification：

```bash
PYTHONPATH=backend:.test-deps python scripts/benchmark_storage_recovery.py \
  --profile qualification --output build/capacity-qualification.json
```

结果是 `capacity-benchmark.v1` JSON，包含数据集、环境、P95、恢复时间、阈值及逐项 PASS/FAIL。基准使用真实 `TaskCommandService`、事件提交、全局任务索引和 `FileStateStore.start()` 恢复路径；不以 mock 替代存储或恢复。

## 运行纪律

qualification 期间必须使用空的本地 SSD 目录、关闭其他高 I/O 负载，并记录 Python 版本、CPU 数和提交 SHA。CI smoke 只用于发现数量级回退，不能替代参考机器 qualification。恢复产生任何 warning 都视为失败。
