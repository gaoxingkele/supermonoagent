# 中央 mylib：MCP、Hook 与自动审阅

## 已确认的范围

用户选择：自动同步 mylib 新版本，并分析、生成和验证改进建议。服务器采用本地受版本控制的库副本；本地 `D:/aicoding/mylib` 继续作为维护源。可信上游的快进版本通过完整性校验后可自动切换；自动生成的候选不会写回并发布成生效技能，也不自动批准受保护变更或修改 metaRSI 引擎自身。

2026-10-26：独立 MCP 服务已上线，公网地址 `https://117.50.48.22/mylib/mcp`，标准 Streamable HTTP、Bearer Header 鉴权。它是 MCP 接口，不是普通网页或匿名技能下载目录。

用户进一步确认第一阶段只启用同步和结构检查，模型分析稍后配置。本次不配置模型账号或发起模型 API 调用。

## 为什么组合使用

| 组件 | 责任 |
| --- | --- |
| MCP | 多电脑／手机共用的检索、按需读取技能、查看建议和账本接口 |
| CLI Hook | 在支持它的 CLI 生命周期事件发生时记录信号；不承担整个技能库服务 |
| 服务器定时任务 | 自动同步固定 Git 分支、验证快照、运行结构性审阅；不依赖客户端窗口一直打开 |
| Skill | 模型执行任务时读取的流程说明及配套资源；通过 MCP 读取不等于在服务器执行所有脚本 |

MCP 暴露工具、资源和提示，Hook 提供宿主生命周期上的确定性触发。来源：[MCP SDK 维护线文档](https://py.sdk.modelcontextprotocol.io/v1/)、[Claude Code Hook 指南](https://code.claude.com/docs/en/hooks-guide)。各 CLI 的 Hook 能力和配置格式不同，不把 Claude 的 Hook 当成所有客户端均支持。

Hook 接入应保持轻量：只记录必要事件摘要和来源，网络失败不拦截用户输入；全库同步、模型分析或较长验证由后台执行。此次没有自动改写现有 CLI 的全局 Hook 配置。

## 副本与版本

准备时核对的源提交为 `78c7e3b530dd89a8800f0ede4218df42b2c64b58`。首次实际同步期间，可信 `main` 已快进到 `e4599f497823c87589e38d1c02f0b511bb58631e`，服务器按新提交及其固定子仓库构建，得到 1,855 个技能入口、15,428 个文件、343,800,498 字节，另有 11 个已知敏感／运行态内容被排除。四个子仓库均补齐，不使用子仓库当前 HEAD 冒充固定提交。未跟踪的 `Graphic/` 保留本地，不作为版本副本上传。

导出以 Git 对象和固定提交为准，不复制 `.git`、本机凭据和运行数据。完整内容使用不可变版本目录，`current.json` 原子切换；请求固定一个版本，旧版本保留。缺失子库、路径异常、校验失败或非快进更新不能悄悄替换当前版本。全库分页检索与按需读取，避免把上千份技能一起塞进模型上下文。

源路径依据：`D:/aicoding/mylib/README.md`、`WIKI.md`、`.gitmodules`、`skill-runtime/manifest.json`。

## 服务隔离

- 独立 `supermono-mylib` 账号和 `/opt/supermono-mylib` 运行目录；状态／缓存位于 `/var/lib/supermono-mylib`，私有配置在 `/etc/supermono-mylib`。
- 保留现有服务器 Python；从既有 3.10.20 安装复制隔离运行时，独立 venv 固定 MCP SDK 1.30.0 维护线，依赖清单在 `requirements-mylib-mcp.txt`。SDK v2 要求 Python 3.11+，本次不升级终端服务的解释器。
- MCP 仅监听 `127.0.0.1:8821`，现有 443 提供 `/mylib/mcp` HTTPS 入口，不占用 8817。新服务正常后才添加了精确代理路由，验证配置后平滑 reload；终端 relay PID 前后不变。
- 所有 MCP 请求鉴权，Bearer 不进入 URL／日志。读取与写信号是不同 scope；信号权限不包含运行任意命令、更新生效库、读取任意项目目录或批准规则。

## metaRSI 的实际边界

现有 metaRSI 是确定性的证据账本和准入工具，本身不调用模型、不自行生成或批准技能改写。中央自动审阅使用固定引擎副本、自己的 `mylib-review` 项目和独立验证产物；本机、3090 等项目的 `.metarsi` 不被合并或作为多机可写 SQLite 共享。

结构性检查的通过只证明对应格式、引用、完整性或候选保留条件，不证明科研质量或任务效果提高。未配置模型服务时，不声称后台正在执行通用模型创新。保留建议、证据与语义效果未验证的状态，不自动调用 `decide`、写入保护面或激活候选。

源路径依据：`D:/aicoding/mylib/skills/meta-rsi/SKILL.md`、`references/DESIGN.md`。这些约束也符合本次用户选择，而非额外要求每次只读查询都人工批准。

## 客户端接入边界

标准 MCP 服务可供支持 Streamable HTTP 和 Bearer Header 的客户端调用。现有 SuperMono 右侧助理仍有自己的本地检索／文本引擎，不能因服务器存在 MCP URL 就宣称两端侧栏已切换；实际接入需要另行验证。没有改写用户 CLI 的全局配置或自动安装 Hook。

当前 GUI 的本机 Codex 文本引擎会按既有隔离策略拒绝非空 MCP 配置。因此不能直接把中央 MCP 塞进它的全局配置再宣称兼容；后续需由独立 MCP 客户端取得受限上下文，或使用另行验证的入口，保持原文本引擎边界。依据：`src/supermonoagent/assistant_codex.py`、`docs/assistant-models.md`。

## 当前自动任务与运维

- `supermono-mylib-mcp.service`：独立常驻服务并已启用开机启动；只读 Token 可查询技能／审阅，不能追加信号。使用当前默认 Token 的只读权限，不在此文档或 Git 中记录其内容。
- `supermono-mylib-sync.timer`：已启用，每轮间隔约 15 分钟检查可信 `main`；由独立 oneshot unit 验证并原子发布，之后运行结构审阅。当前格式建议每轮最多 3 条，不把缺少验收章节当成已证实的能力故障。
- 内存／CPU／并发受独立服务配置约束；缓存上限 4 GiB，review 状态上限 256 MiB并保留至少 1 GiB 空间，超限停止写入，不自动删除既有账本或证据。
- 读取与 Hook 写信号权限分离，写入 Token 只保存在服务器私有文件，未分发给现有 CLI。认证文件必须 0600；修改私有 MCP auth 文件可撤销该服务凭据，不假定它与终端中继账号自动同步撤销。
- 服务解释器基于独立 `/opt` runtime，子进程只从可信 `sys.base_prefix/lib` 推导动态库路径，拒绝继承任意 `LD_*`／`PYTHONPATH`。实际 Linux 3.10 子进程与 hardened service 中的标准 SDK 调用均已验证。
- 现有 HTTPS nginx 使用 `/usr/local/nginx/sbin/nginx`，配置位于 `/usr/local/nginx/conf/conf.d/00-supermono.conf`，不是 `/etc/nginx` 或 PATH 中的同名命令。只修改实际 443 对应配置，没有修改名称相似的 Docker nginx。
- 回滚库时先暂停 timer 和正在运行的 sync unit，再用 operator CLI `package-mylib-snapshot.py activate` 指定旧版本和预期当前版本；确认修复后恢复 timer。否则可信上游较新提交会被下一次同步合法快进恢复。不通过 MCP 暴露 activate/sync/decide。

## 验收与剩余边界

四组件联合验证 **148 passed**；随后针对实际 Linux loader 环境的修复完整审阅组 **49 passed**。二者为不同检查轮次，不能相加为一次整套绿灯。源码通过独立安全复核，实际服务源码在服务器 Python 3.10 编译、systemd 单元验证通过。

首次全量镜像构建约 552 秒，在独立 CPU 配额内完成；初次发现 child loader 环境问题后已修复，不以该轮 `review_failed` 冒充成功。修复后真实完整库审阅耗时约 9.85 秒：扫描 1,855 个入口，1,846 个精确文本可检查，9 个截断／非精确文本明确跳过；首轮生成 3 条格式候选并验证账本。`complete:false` 表示受每轮 3 条限制仍有模板候选待处理，不表示已经发现同等数量的技能故障。

公网验收使用官方 SDK：TLS 1.3、标准初始化／8 工具／技能及资源读取通过；无 Token／错 Token 为 401，非法 Origin 为 403，超大请求为 413；只读 Token 拒绝追加，资源逃逸拒绝，3 条首轮建议均未应用、语义效果未验证。随后重复同步实测约 59.9 秒，`changed:false` 且 review 成功；服务 active、timer waiting/enabled、8821 仅 loopback 监听均已核实。

实施与测试证据：`.omx/plans/mylib-central-mcp.md`、`.omx/artifacts/central-mylib-final.xml`、`.omx/artifacts/mylib-evolution-loader-final.xml`、`.omx/artifacts/mylib-server-inventory/{first-sync-before-loader-fix.json,initial-sync.json,initial-review.json,loopback-sdk.json,public-sdk-verification.json,enabled-units.json,final-health.json}`。
