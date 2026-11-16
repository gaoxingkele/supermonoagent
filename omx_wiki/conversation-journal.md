# 后台会话记录：边界与验收

Page type: decision. Updated: 2026-11-12.

用户确认记录按同一个后台 CLI 会话合并，桌面与手机共用；关闭显示页面不截断。终端生命期与记录器生命期分离，只有可信退出才收尾。来源、房间和权限不通过路径/标题/时间猜测。

source_path: [功能与限制](../docs/conversation-journal.md)、[本地记录](../src/supermonoagent/conversation_store.py)、[生命周期管理](../src/supermonoagent/conversation_manager.py)、[服务部署](../deploy/journal/README.md)。

采用私有逐会话 SQLite WAL 原始账本、受证据约束的独立对话投影、可重建导出、同中继域名下独立记录服务。镜像回执只证明完整内容与对应事件复制，不能抹掉原采集缺口。关闭记录器仅暂停活会话；关闭时已接收的 EOF 必须在释放存储前落成 ended/incomplete。

source_path: [关闭回归](../tests/test_conversation_manager.py)、[来源证据与收尾](../tests/test_conversation_sources.py)、[镜像与缺口真相](../tests/test_conversation_sync.py)。

Codex 0.167.0 已有匹配版本私有快照的被动读取证据。Claude Stop 只可作为候选回复；未知人类输入来源不进入已确认对话。用户已选择新增独立对话记录模式，保留原始 CLI；此模式按应用提交与提供商完成协议记录。Codex 已通过一次真实无工具请求；本机 Claude 直接路径缺少可用认证，用户明确通常通过其他编程工具接入，不能要求转用官方登录。11 月 11 日默认 SSH 路径在认证前关闭、观察时桌面锁屏，不能用本地合成测试代替生产或真机验收。

2026-11-12 用户实测：发起端 Windows 的默认 Meta 虚拟网卡路径 TCP 后断开，指定本机 WLAN Wi-Fi 网卡后，同一远端 `117.50.48.22:22` 返回 SSH 标识并完成密钥交换。只确认接口路径的结果不同；转发环节或云端对出口 IP 的处理仍未定，认证／部署尚无完成证据。source_path：[规范交接第 7 节](../../../../docs/handoff-2026-11-11-conversation-journal.md#7-ssh--服务器状态与未解决问题)；source：用户 2026-11-12 实测说明。

source_path: [来源适配器](../src/supermonoagent/conversation_sources.py)、[独立会话协调](../src/supermonoagent/managed_conversation.py)、[CLI 驱动与权限](../src/supermonoagent/managed_claude.py)、[候选界面测试](../tests/test_workspace_journal.py)、[Android 合成连接](../tests/test_android_journal_fixture.py)。Inference: 已通过单元/合成集成测试不代表现有所有提供商版本可完整记录，也不代表已部署。
