# Backend boundary

Phase 0 仅冻结契约，不包含控制平面运行时代码。

Phase 1 后端将以 `contracts/v1` 为边界实现任务编排、实例与进程监管、文件化状态、资产发布、审批、通知、用量和配置服务。它不得把 Image Agent 或 PPT Agent 作为同进程 Python 模块导入。
