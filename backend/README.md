# 后端边界

`harness` 是单机、单写者控制进程的 Python 包。首次启动请从仓库根目录按[安装与启动指南](../docs/getting-started.md)操作；API 使用方式见 [Master API 调用指南](../docs/master-api-guide.md)。

依赖方向固定为：

```text
api -> domain -> storage
 |       |          |
 +---- contracts ---+
         |
        core
```

- `api` 负责 HTTP、生命周期和公开错误序列化；
- `domain` 负责命令、合法转换和聚合，不直接操作文件；
- `storage` 负责事件先行的持久化、锁、恢复和 Repository；
- `contracts.py` 始终从仓库根 `contracts/v1` 编译 Schema；
- `core` 只放配置、日志和稳定错误等横切能力。

严禁导入 Image/PPT Agent 内部模块。Adapter 通过 HTTP、进程和持久化契约接入，并位于独立模块，不能向核心编排散落 Agent 类型分支。

从仓库根目录启动后端。Linux：

```bash
.venv/bin/python -m harness
```

Windows CMD 或 PowerShell：

```bat
.\.venv\Scripts\python.exe -m harness
```

默认监听 `127.0.0.1:18080`。`/healthz` 用于存活检查，`/readyz` 用于就绪检查，`/docs` 是交互式 API 文档；根路径 `/` 返回 404 是预期行为。
