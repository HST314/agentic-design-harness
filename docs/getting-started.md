# 安装与启动指南

本指南用于从零启动 Agentic Design Harness 的后端 API 和 Web 控制台。只查看界面或调用控制面 API 时，不需要先准备 Image Agent；只有运行真实 Image 工作流时才需要它。

## 1. 环境要求

| 工具 | 要求 | 检查命令 |
| --- | --- | --- |
| Git | 当前稳定版本 | `git --version` |
| Python | 3.10+ | Linux：`python3 --version`；Windows：`py -3 --version` |
| Node.js | 22+ | `node --version` |
| npm | 随 Node.js 安装 | `npm --version` |

正式支持 Windows 10/11、Windows Server 2022 和 Linux。macOS 尚未进入持续集成支持矩阵。

## 2. 获取代码

```bash
git clone https://github.com/HST314/agentic-design-harness.git
cd agentic-design-harness
```

后续每个终端都必须先进入这个仓库根目录。

## 3. 创建隔离环境并安装依赖

### Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps -e .
npm --prefix frontend ci
```

### Windows CMD 或 PowerShell（推荐）

```bat
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
npm --prefix frontend ci
```

直接调用 `.venv` 中的解释器比依赖激活状态更可靠，并且 CMD 与 PowerShell 的命令相同。若机器同时安装了多个 Python，可把 `py -3` 改为已安装的明确版本，例如 `py -3.10` 或 `py -3.13`。

安装后验证模块确实来自当前仓库的虚拟环境。

Linux：

```bash
.venv/bin/python -c "import sys, harness; print(sys.executable); print(harness.__file__)"
```

Windows：

```bat
.\.venv\Scripts\python.exe -c "import sys, harness; print(sys.executable); print(harness.__file__)"
```

第一行必须指向当前仓库的 `.venv`，第二行必须指向当前仓库下的 `backend\harness` 或 `backend/harness`。

### 可选：激活虚拟环境

只有希望直接输入 `python` 时才需要激活。请使用与当前终端匹配的命令：

| 终端 | 命令 |
| --- | --- |
| Linux Bash | `source .venv/bin/activate` |
| Windows CMD | `.venv\Scripts\activate.bat` |
| Windows PowerShell | `.\.venv\Scripts\Activate.ps1` |

CMD 的提示符通常形如 `C:\>`，PowerShell 通常以 `PS C:\>` 开头。不要在 CMD 中粘贴 PowerShell 专用语法；如果 PowerShell 禁止执行 `Activate.ps1`，无需修改系统策略，继续使用上面的显式 `python.exe` 命令即可。

## 4. 启动后端（终端一）

Linux：

```bash
.venv/bin/python -m harness
```

Windows CMD 或 PowerShell：

```bat
.\.venv\Scripts\python.exe -m harness
```

看到 `Application startup complete` 和 `Uvicorn running on http://127.0.0.1:18080` 后，保持终端一运行。用以下地址验证：

- <http://127.0.0.1:18080/healthz>：进程存活；
- <http://127.0.0.1:18080/readyz>：控制面可接收请求；
- <http://127.0.0.1:18080/docs>：交互式 API 文档。

后端没有定义 `/` 页面路由，因此打开 <http://127.0.0.1:18080/> 得到 `404 Not Found` 是正常的。

## 5. 启动前端（终端二）

打开第二个终端，重新进入仓库根目录：

```bash
npm --prefix frontend run dev
```

保持终端二运行，在浏览器打开 <http://127.0.0.1:18180/>。Vite 会把界面的 API 请求代理给终端一的后端。

终端二不依赖 Python；它是否显示 `(.venv)` 不影响 npm。两个终端的关系是：

```text
浏览器 :18180 -> Vite 前端 -> /api、/healthz、/readyz -> FastAPI 后端 :18080
```

若后端不在默认地址，启动前端前设置代理目标：

Linux Bash：

```bash
export HARNESS_BACKEND_URL=http://127.0.0.1:19080
npm --prefix frontend run dev
```

Windows CMD：

```bat
set HARNESS_BACKEND_URL=http://127.0.0.1:19080
npm --prefix frontend run dev
```

Windows PowerShell：

```powershell
$env:HARNESS_BACKEND_URL = "http://127.0.0.1:19080"
npm --prefix frontend run dev
```

## 6. 可选配置

默认配置可以直接启动空控制平面。需要调整数据目录、端口或 Image Agent 路径时，复制样例文件：

Linux：

```bash
cp config/harness.example.yaml config/harness.local.yaml
export HARNESS_CONFIG=config/harness.local.yaml
```

Windows CMD：

```bat
copy config\harness.example.yaml config\harness.local.yaml
set HARNESS_CONFIG=config\harness.local.yaml
```

Windows PowerShell：

```powershell
Copy-Item config/harness.example.yaml config/harness.local.yaml
$env:HARNESS_CONFIG = "config/harness.local.yaml"
```

`config/harness.local.yaml` 已被 Git 忽略。不要把 API Key 写入 YAML、`.env`、任务目录或版本库；凭据应通过受控 Key Pool API 写入。

## 7. 停止与再次启动

在两个终端中分别按 `Ctrl+C`。再次启动时不需要重复安装依赖，只需重新打开两个终端并执行第 4、5 节的命令。

下一步可阅读 [Master API 调用指南](master-api-guide.md) 创建任务，或阅读 [接入与运行手册](operations.md) 准备真实 Image Agent。遇到异常先查看[常见问题排查](troubleshooting.md)。
