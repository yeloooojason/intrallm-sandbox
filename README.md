# IntraLLM Sandbox

企业级沙箱控制平面，供 **IntraLLM agent** 调用。它包含四部分：

- **沙箱运行时**：每个沙箱是一个加固的 Docker 容器，有 CPU、内存和进程数限制，默认不能联网，也没有任何 Linux capability。
- **桌面沙箱（Computer Use）**：沙箱内带虚拟桌面和 Chromium。agent 既可以像人一样看截图、按坐标点击和打字，也可以用 Playwright 按元素编号操作同一个浏览器。用户可以在 Dashboard 上实时观看，并随时接管。
- **控制 API 与 Agent 工具**：提供 REST API，以及 OpenAI 兼容格式的 function-calling 工具定义，IntraLLM agent 可以直接接入。
- **监控 Dashboard**：显示运行中的沙箱数量、每个沙箱分配给了谁、CPU 和内存占用、宿主机负载趋势，以及审计日志。

![dashboard](docs/dashboard-light.png)

![detail](docs/drawer.png)

![desktop](docs/desktop-live.png)

## 架构

```
 IntraLLM agent ──(agent token, owner=alice)──┐
 浏览器 Dashboard ──(admin / user token)───────┤
                                               ▼
                        ┌──────────── FastAPI 控制平面 ────────────┐
                        │  auth (admin/agent/user)  · 配额 · 审计    │
                        │  SandboxManager                           │
                        │   ├─ 后台采样线程 → MetricsStore (CPU/内存) │
                        │   └─ 回收线程 (TTL / 空闲超时 / 容器退出)   │
                        │  SQLite: sandboxes · tokens · audit       │
                        └───────────────┬───────────────────────────┘
                                        ▼ Runtime 抽象
                         DockerRuntime（生产） / LocalRuntime（仅开发）
                                        ▼
                    intrallm-sbx-xxxx 容器 (cap_drop=ALL, network=none,
                    no-new-privileges, cpu/mem/pids 限制, tini init)
```

| 模块 | 说明 |
|---|---|
| `intrallm_sandbox/runtime/docker_rt.py` | Docker 运行时：创建、exec（带超时）、读写文件、统计数据、销毁 |
| `intrallm_sandbox/runtime/local_rt.py` | 本地子进程运行时，**没有隔离**，只用于开发和测试 |
| `intrallm_sandbox/manager.py` | 生命周期、配额、TTL 和空闲回收、指标采样、重启后与容器状态对齐 |
| `intrallm_sandbox/agent_tools.py` | 给 LLM 用的工具定义和调度逻辑 |
| `intrallm_sandbox/api.py` | REST API、`/metrics`（Prometheus 格式），以及 Dashboard 静态页面 |
| `intrallm_sandbox/client.py` | Python SDK，以及给 agent 用的 `AgentToolkit` |
| `intrallm_sandbox/static/` | Dashboard 前端（原生 JS 加 SVG 图表，没有外部 CDN 依赖，可以直接在内网部署） |
| `sandbox-desktop/` | 桌面沙箱镜像：Xvfb、Openbox、Chromium，以及沙箱内的控制器 `desktopctl` |
| `deploy/egress-proxy/` | 桌面沙箱的出口代理（Squid）配置和域名白名单 |

## 快速开始

### 用 Docker Compose 部署（推荐）

```bash
export SANDBOX_ADMIN_TOKEN=$(openssl rand -hex 24)
docker compose up -d --build
# 打开 http://<host>:8080 ，用 SANDBOX_ADMIN_TOKEN 登录
```

给 IntraLLM agent 签发一个 token：

```bash
docker compose exec control-plane intrallm-sandbox create-token intrallm-agent --role agent
# 也可以调 API：POST /api/v1/tokens {"principal": "intrallm-agent", "role": "agent"}
```

### 本地开发

```bash
pip install -e '.[dev]'
docker build -t intrallm/sandbox:latest sandbox-image/      # 默认沙箱镜像
SANDBOX_ADMIN_TOKEN=dev python -m intrallm_sandbox serve --port 8080

# 机器上没有 Docker 时（注意：没有隔离）
SANDBOX_RUNTIME=local SANDBOX_ADMIN_TOKEN=dev python -m intrallm_sandbox serve

pytest            # 单元测试和 API 测试；检测到 Docker 时会自动跑 Docker 集成测试
```

## 桌面沙箱：Computer Use + Playwright

```
                      IntraLLM agent
           ┌───────────────┴────────────────┐
     computer 工具                       browser 工具
  (截图 / 坐标点击 / 键盘)        (navigate / snapshot / click ref / fill)
           │   POST /desktop/computer        │   POST /desktop/browser
           ▼                                 ▼
  ┌──────────────── 桌面沙箱容器 (docker exec desktopctl) ────────────────┐
  │  Xvfb :1 (1280x800)  ←── XTEST 鼠标键盘 / 截图 (python-xlib)            │
  │    └─ Chromium  ←── Playwright connect_over_cdp(127.0.0.1:9222)        │
  └────────────────────────────┬──────────────────────────────────────────┘
                               │ 独立的 internal 网络，只连着代理
                        Squid 出口代理 (域名白名单，默认禁止访问内网网段)
                               │
                   允许的内网系统 / 外网网站
```

两类工具操作的是**同一个、看得见的 Chromium**，可以混着用：

- **`browser`（优先使用）**：返回页面的文字快照，例如 `[3] button "提交" @418,604`。agent 用编号点击和填写，快速、稳定、省 token，纯文本模型也能用。快照里还带有元素的屏幕坐标，可以直接交给 `computer` 使用。
- **`computer`（兜底）**：截图加坐标点击和键盘，支持中文输入。适用于 canvas、原生对话框、需要视觉确认的场景和非浏览器程序。需要具备视觉能力的模型，比如 Qwen2.5-VL、UI-TARS。每个动作默认都会返回一张新截图。

使用方式：

```bash
# 创建桌面沙箱
POST /api/v1/sandboxes {"template": "desktop", "owner": "alice"}
# 或者让 agent 调用 sandbox_create {"template": "desktop"}

POST /api/v1/sandboxes/{id}/desktop/browser  {"action": "navigate", "url": "http://oa.corp"}
POST /api/v1/sandboxes/{id}/desktop/browser  {"action": "fill", "ref": 1, "text": "张伟"}
POST /api/v1/sandboxes/{id}/desktop/computer {"action": "left_click", "coordinate": [418, 604]}
POST /api/v1/sandboxes/{id}/desktop/computer {"action": "type", "text": "上海出差"}
GET  /api/v1/sandboxes/{id}/desktop/screen?format=jpeg   # 当前画面
```

- **截图怎么交给模型**：OpenAI 兼容接口的 `tool` 消息只能放文本，所以 `AgentToolkit.messages()` 会把截图拆成一条带 `image_url` 的 user 消息。`prune_screenshots()` 只保留最近几张截图，避免撑爆上下文。完整示例见 `examples/intrallm_agent_loop.py`。
- **实时画面与人工接管**：在 Dashboard 里打开桌面沙箱，可以看到实时画面（约 1 秒刷新一次）。勾选“接管控制”后，可以直接在画面上点击、双击、右键、滚动和键盘输入；中文请用下方的输入框发送。人工操作和 agent 操作分别以各自的身份记入审计日志。
- **审计**：每个动作都会记入审计日志。`type` 和 `fill` 只记录字符数，不记录内容，以免泄露密码。

构建镜像：

```bash
docker build -t intrallm/sandbox-desktop:latest sandbox-desktop/
# 离线或内网构建（没有 apt 源时，跳过 Openbox、xterm 和中文字体）：
docker build --build-arg DESKTOP_EXTRAS=0 -t intrallm/sandbox-desktop:latest sandbox-desktop/
```

### 桌面沙箱的网络隔离

浏览器必须能联网，所以这是安全上最需要注意的部分。`docker compose` 默认的部署方式是：

- **每个桌面沙箱一个独立的 internal 网络**（`SANDBOX_DESKTOP_NETWORK=isolated`），网络上只连着出口代理。沙箱之间无法互访，也不能绕过代理直接出网。
- **Squid 出口代理**：只有 `deploy/egress-proxy/allowlist.txt` 里的外网域名可以访问。内网地址段（10/8、172.16/12、192.168/16、169.254/16 等，含云平台元数据地址）一律禁止，除非域名列在 `intranet-allowlist.txt` 里。这样可以防止 agent 被网页上的提示注入引导去访问内网的其他系统（SSRF）。代理的访问日志会记录每个沙箱访问过的地址。
- **代码沙箱**仍然是 `network=none`，完全断网。

在本仓库的开发环境里实测过：允许的内网系统可以打开；未列入白名单的内网系统和外网域名会被代理拒绝；绕过代理直连（按域名或按 IP）都不通；沙箱之间互相访问也不通；销毁沙箱时，它的网络会一并删除。

## 角色与权限

| 角色 | 能做什么 |
|---|---|
| `admin` | 查看和控制所有沙箱、管理 token，能看到宿主机指标和全局审计日志 |
| `agent` | 服务身份（IntraLLM agent）。可以为任意用户（`owner`）分配沙箱，但只能操作自己创建的沙箱。以 `owner=X` 身份调用工具时，碰不到其他用户的沙箱 |
| `user` | 终端用户。只能看到和控制分配给自己的沙箱，Dashboard 也只显示自己的数据 |

## 接入 IntraLLM agent

方式一：走 HTTP，不限语言。

```bash
# 1. 获取工具定义（OpenAI function-calling 格式），作为 tools 传给 LLM
GET /api/v1/agent/tools

# 2. LLM 返回 tool_call 之后转发过来；owner 填当前对话的用户
POST /api/v1/agent/invoke
{"tool": "sandbox_exec", "owner": "alice",
 "arguments": {"sandbox_id": "sbx-...", "command": "python3 main.py"}}
# 返回 {"ok": true, "result": {...}}，或者 {"ok": false, "error": "..."}
# 出错时不会抛异常，而是把错误作为结果返回，LLM 可以据此自行修正
```

方式二：用 Python SDK，完整示例见 `examples/intrallm_agent_loop.py`。

```python
from intrallm_sandbox.client import SandboxClient, AgentToolkit
kit = AgentToolkit(SandboxClient(SANDBOX_URL, AGENT_TOKEN), owner="alice")
resp = llm.chat.completions.create(model=..., messages=messages, tools=kit.tools)
for call in resp.choices[0].message.tool_calls:
    messages.append({"role": "tool", "tool_call_id": call.id,
                     "content": kit.call(call.function.name, call.function.arguments)})
```

提供的工具：`sandbox_create`、`sandbox_exec`、`sandbox_write_file`、`sandbox_read_file`、`sandbox_list_files`、`sandbox_list`、`sandbox_destroy`，以及桌面沙箱专用的 `computer` 和 `browser`。

## REST API

所有请求都要带 `Authorization: Bearer <token>`。完整的交互式文档在 `/docs`（Swagger）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/sandboxes` | 创建沙箱 `{owner, name, image, cpu, memory_mb, ttl_seconds, env, labels}` |
| GET | `/api/v1/sandboxes?active=true&owner=` | 列出沙箱，返回结果带最新的 CPU 和内存数据 |
| GET / DELETE | `/api/v1/sandboxes/{id}` | 查看详情 / 停止并销毁 |
| POST | `/api/v1/sandboxes/{id}/exec` | 执行命令 `{command, timeout, workdir, env}` |
| PUT / GET | `/api/v1/sandboxes/{id}/files` | 写文件 / 读文件（`?path=`） |
| GET | `/api/v1/sandboxes/{id}/ls?path=` | 列出目录 |
| POST | `/api/v1/sandboxes/{id}/extend` | 延长有效期 `{seconds}` |
| POST | `/api/v1/sandboxes/{id}/reassign` | 改分配给别的用户 `{owner}`（admin 和 agent 可用） |
| POST | `/api/v1/sandboxes/{id}/desktop/computer` | Computer Use 动作：截图、点击、键盘、滚动、拖拽 |
| POST | `/api/v1/sandboxes/{id}/desktop/browser` | Playwright 动作：打开网页、页面快照、点击、填写、管理标签页 |
| GET | `/api/v1/sandboxes/{id}/desktop/screen` | 当前桌面画面（PNG 或 JPEG） |
| GET | `/api/v1/sandboxes/{id}/metrics` | 该沙箱的 CPU 和内存历史 |
| GET | `/api/v1/sandboxes/{id}/audit` | 该沙箱的审计日志 |
| GET | `/api/v1/dashboard/summary` | Dashboard 汇总数据：运行数、各用户分配情况、资源占用、宿主机趋势 |
| GET | `/metrics` | Prometheus 指标，可以接 Grafana 和告警 |
| POST / GET / DELETE | `/api/v1/tokens` | 管理 token（仅 admin） |

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SANDBOX_RUNTIME` | `docker` | 可选 `docker` 或 `local` |
| `SANDBOX_ADMIN_TOKEN` | — | 初始管理员 token |
| `SANDBOX_DB_PATH` | `./data/sandbox.db` | SQLite 文件路径 |
| `SANDBOX_DEFAULT_IMAGE` | `intrallm/sandbox:latest` | 默认沙箱镜像 |
| `SANDBOX_ALLOWED_IMAGES` | 空（不限制） | 镜像白名单，逗号分隔 |
| `SANDBOX_DEFAULT_CPU` / `SANDBOX_MAX_CPU` | `1` / `4` | CPU 核数 |
| `SANDBOX_DEFAULT_MEMORY_MB` / `SANDBOX_MAX_MEMORY_MB` | `1024` / `8192` | 内存限制（MB） |
| `SANDBOX_PIDS_LIMIT` | `256` | 进程数上限 |
| `SANDBOX_NETWORK` | `none` | Docker 网络模式；需要联网时可以指定一个受控网络 |
| `SANDBOX_DEFAULT_TTL` / `SANDBOX_MAX_TTL` | `3600` / `86400` | 沙箱有效期（秒） |
| `SANDBOX_IDLE_TIMEOUT` | `1800` | 空闲多少秒后自动回收 |
| `SANDBOX_EXEC_TIMEOUT` / `SANDBOX_MAX_EXEC_TIMEOUT` | `60` / `600` | 单条命令的超时（秒） |
| `SANDBOX_MAX_PER_OWNER` / `SANDBOX_MAX_TOTAL` | `5` / `100` | 每个用户和全集群的沙箱数量上限 |
| `SANDBOX_METRICS_INTERVAL` | `5` | 指标采样间隔（秒） |
| `SANDBOX_DESKTOP_IMAGE` | `intrallm/sandbox-desktop:latest` | 桌面沙箱镜像 |
| `SANDBOX_DESKTOP_CPU` / `SANDBOX_DESKTOP_MEMORY_MB` / `SANDBOX_DESKTOP_SHM_MB` | `2` / `2048` / `1024` | 桌面沙箱的默认资源 |
| `SANDBOX_DESKTOP_NETWORK` | `bridge` | 生产环境请设为 `isolated`（每个沙箱一个独立网络），见上文 |
| `SANDBOX_DESKTOP_NETWORK_PEERS` | 空 | `isolated` 模式下连入沙箱网络的容器（出口代理） |
| `SANDBOX_DESKTOP_PROXY` | 空 | 浏览器使用的代理，例如 `http://intrallm-egress-proxy:3128` |
| `SANDBOX_DESKTOP_HOME` | `about:blank` | 浏览器启动后打开的首页 |
| `SANDBOX_MAX_DESKTOPS_PER_OWNER` | `2` | 每个用户最多同时拥有的桌面沙箱数 |

## 安全说明

- 容器的默认配置：`cap_drop=ALL`、`no-new-privileges`、`network=none`、CPU/内存/pids 限制、swap 关闭；默认镜像以非 root 用户（uid 1000）运行。
- 控制平面需要挂载 `docker.sock`，这相当于宿主机的 root 权限。建议把控制平面部署在专用节点上，并且只让它在内网可访问。
- 隔离要求更高时，可以给 Docker 配置 [gVisor](https://gvisor.dev)（`runsc`）或 Kata Containers 作为 runtime。
- 数据库里只存 token 的 SHA-256 哈希。所有 exec 和文件写入都会记入审计日志。
- `/metrics` 不需要认证，但输出里带有用户名，应当只开放给内网的 Prometheus。
- 桌面沙箱里的 Chromium 以 `--no-sandbox` 启动：Chromium 自带的沙箱需要用户命名空间，而加固后的容器不提供。此时容器本身就是隔离边界。需要两层隔离时，可以给容器配置专门的 seccomp 策略，或者换用 gVisor。
- 网页内容可能包含提示注入。示例里的系统提示词要求模型把网页内容当作数据而不是指令，并在付款、删除、发送消息之前先征得用户同意。真正的防线还是出口白名单和人工接管。

## 后续可以扩展

- 增加 Kubernetes 运行时（每个沙箱一个 Pod），实现多节点调度
- 对接企业 SSO（OIDC/LDAP），替代静态 token
- 持久化工作区卷，以及沙箱快照
- 提供 MCP Server 形式的工具接口
- 用 noVNC 提供更流畅的实时画面，以及录屏回放
- 使用 Windows 软件时，增加 Windows 虚拟机运行时
