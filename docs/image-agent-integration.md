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
版本与内容校验。当前仓库已把锁定提交作为 submodule 固定在内嵌目录，
`prefer_embedded` 会直接选择该目录；启动时不会追踪或拉取 Image Agent 的浮动分支。

## 本地初始化与启动

克隆主仓库后无需手工摆放相邻源码或激活虚拟环境。启动器会初始化固定 submodule、校验
Git 指针与源码/依赖摘要、创建 Harness Python 环境、安装 Image Agent 隔离依赖，并按
`package-lock.json` 准备前端。锁摘要未变化时会安全跳过重复安装；前端 lock 变化或依赖
不完整时会在启动 Vite 前自动执行 `npm ci`。

Windows：

```powershell
py -3 scripts/dev.py
```

Linux：

```bash
python3 scripts/dev.py
```

需要分步排查时使用：

```bash
python3 scripts/dev.py setup
python3 scripts/dev.py doctor
python3 scripts/dev.py start
```

`start` 同时管理后端和前端，等待 `/healthz`、`/readyz` 与 Web 首页通过后才报告就绪；
任一子进程提前退出或收到 Ctrl+C 时，另一进程会同步关闭。CI 使用
`python scripts/dev.py start --check` 验证同一生产启动路径。

## DeliveryBundle 数据迁移

`delivery_bundle_migration_mode` 明确定义后续双资产交付切换目标：

- `legacy_only`：P0 默认值，只写现有交付数据。
- `dual_write`：迁移期同时写现有交付与 DeliveryBundle 数据，用于对账。
- `bundle_only`：对账完成后的目标模式，只写 Bundle 数据。

P0 只冻结契约和开关语义，不声称已经实现 P3 的双资产发布。契约源文件为
`delivery-bundle-candidate.schema.json` 与 `bundle-manifest.schema.json`；任一状态、摘要、
分支来源或人工决策字段不符合契约时必须失败关闭。

## 校验

完整基线、submodule 指针、源码与依赖清单校验：

```bash
python scripts/verify_image_agent_lock.py
```

`make check` 已包含完整校验；Linux 与 Windows CI 还会执行启动器 setup、doctor 和双服务
健康检查。若只运行 `git clone` 而没有递归拉取 submodule，先执行 `scripts/dev.py setup`。
