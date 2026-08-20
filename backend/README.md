# Backend boundary

`harness` 是单控制进程的 Python 包。依赖方向固定为：

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

严禁导入 Image/PPT Agent 内部模块。未来 Adapter 通过 HTTP、进程和持久化契约
接入，并位于独立模块，不能向核心编排散落 Agent 类型分支。
