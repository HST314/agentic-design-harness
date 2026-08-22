# Image Agent 集成与迁移护栏

Image Agent 的正式源码位置为 `agents/image_agent_mvp`，运行版本唯一由
`agents/image-agent.lock.json` 决定。锁文件固定仓库、提交、包版本、契约版本、源码
摘要、依赖锁摘要和隔离依赖内容摘要；Adapter、CI 与本地校验不得再各自维护版本常量。

P0 基线记录位于 `config/baselines/p0-integration.json`。它同时固定 Harness 与 Image
Agent 的已验收提交、Git tree、源码摘要、依赖摘要和发布门证据。升级 Image Agent 时，
必须先在独立仓库形成可追溯提交并通过发布门，再同步源码指针和 lock；禁止使用浮动分支。

## 路径迁移

`image_agent_path_mode` 提供一个发布周期的迁移与回滚护栏：

- `prefer_embedded`：默认值；优先使用内嵌源码，内嵌尚未就绪时允许已有部署继续运行。
- `embedded_only`：切换完成后的收口模式；只接受内嵌源码。
- `external_only`：仅用于单发布周期内的紧急回滚，不是新部署方式。

无论选择哪种模式，revision、包版本和内容摘要都必须与 lock 一致；路径回滚不能绕过
版本与内容校验。P1 加入 submodule 后，`prefer_embedded` 会自动选择内嵌目录。

## DeliveryBundle 数据迁移

`delivery_bundle_migration_mode` 明确定义后续双资产交付切换目标：

- `legacy_only`：P0 默认值，只写现有交付数据。
- `dual_write`：迁移期同时写现有交付与 DeliveryBundle 数据，用于对账。
- `bundle_only`：对账完成后的目标模式，只写 Bundle 数据。

P0 只冻结契约和开关语义，不声称已经实现 P3 的双资产发布。契约源文件为
`delivery-bundle-candidate.schema.json` 与 `bundle-manifest.schema.json`；任一状态、摘要、
分支来源或人工决策字段不符合契约时必须失败关闭。

## 校验

结构与基线校验：

```bash
python scripts/verify_image_agent_lock.py
```

内嵌源码就绪后执行完整源码校验：

```bash
python scripts/verify_image_agent_lock.py --image-agent-root agents/image_agent_mvp
```

`make check` 已包含结构与基线校验；真实 Image CI 会额外校验完整源码与依赖锁集合。
