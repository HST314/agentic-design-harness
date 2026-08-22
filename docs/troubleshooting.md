# 常见问题排查

先确认所有命令都在仓库根目录执行，并区分两个本地服务：后端 API 使用 `18080`，Web 控制台使用 `18180`。

## 快速诊断表

| 现象 | 常见原因 | 处理 |
| --- | --- | --- |
| 打开 `127.0.0.1:18080` 显示 `404` / `Not Found` | 后端没有定义根页面 | 这是正常结果；界面访问 `18180`，API 文档访问 `18080/docs` |
| `No module named harness` | 当前 Python 不是项目 `.venv`，或项目尚未安装 | 使用 `.venv` 内的解释器重新安装并启动 |
| 前端显示“服务不可达”或请求 500 | Vite 无法连接后端 | 保持终端一运行，先检查 `18080/readyz` |
| 终端二出现 `ECONNREFUSED 127.0.0.1:18080` | 后端未启动、已退出或端口不同 | 启动后端，或正确设置 `HARNESS_BACKEND_URL` |
| `# 不是内部或外部命令` | 把 Bash/PowerShell 注释粘贴到了 CMD | 只复制代码块中的可执行命令，并使用对应终端语法 |
| PowerShell 拒绝运行 `Activate.ps1` | 本机脚本执行策略限制 | 不必改策略；直接运行 `.\.venv\Scripts\python.exe ...` |
| `address already in use` / 端口被占用 | 已有进程监听 18080 或 18180 | 找到已知进程并正常停止，或调整端口 |
| `writer lease` 冲突 | 两个后端共用同一 `control_root` | 只保留一个后端，或为不同实例配置独立数据目录 |

## Python 环境错误

Windows：

```bat
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
.\.venv\Scripts\python.exe -c "import harness; print(harness.__file__)"
```

Linux：

```bash
.venv/bin/python -c "import sys; print(sys.executable)"
.venv/bin/python -c "import harness; print(harness.__file__)"
```

若第二条命令失败，用同一个解释器修复当前 `.venv`：

Windows：

```bat
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

Linux：

```bash
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps -e .
```

不要根据提示符中是否有 `(.venv)` 猜测环境；以 `sys.executable` 的实际输出为准。

## 前端已打开但数据加载失败

按顺序访问：

1. <http://127.0.0.1:18080/healthz>
2. <http://127.0.0.1:18080/readyz>
3. <http://127.0.0.1:18180/>

前两项失败说明问题在后端，而不是前端。检查终端一是否仍在运行、是否有启动异常，以及实际监听端口是否为 `18080`。前两项成功但第三项失败时，检查终端二的 Vite 日志和 `18180` 端口。

如果自定义了 `HARNESS_PORT`，前端不会自动发现新端口。必须在启动前端的同一个终端中把 `HARNESS_BACKEND_URL` 设置为新的完整后端地址，详见[安装与启动指南](getting-started.md#5-启动前端终端二)。

## 端口冲突

Windows：

```bat
netstat -ano | findstr :18080
netstat -ano | findstr :18180
```

Linux：

```bash
ss -ltnp | grep -E ':18080|:18180'
```

确认 PID 属于已知的 Harness 或 Vite 进程后，回到对应终端按 `Ctrl+C` 正常停止。不要仅凭端口号强制终止未知进程。

## 健康检查与就绪检查的区别

- `healthz` 返回成功：后端进程存活。
- `readyz` 返回成功且状态为 `ready`：契约已加载，并且当前进程持有唯一写者租约。

自动化、前端和业务调用应以 `readyz` 为准。`healthz` 成功但 `readyz` 失败时，优先检查配置路径、契约目录、状态目录权限和 writer lease。

## 仍无法解决

提交问题时请提供：操作系统与终端类型、Python/Node 版本、执行的原始命令、两个终端从启动开始的完整错误片段，以及 `healthz`/`readyz` 结果。提交前删除 API Key、Authorization、Cookie、完整 Provider URL、用户素材和本机敏感路径。
