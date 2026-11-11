# 后台会话记录：边界与验收

Page type: decision. Updated: 2026-11-11.

用户确认记录按同一个后台 CLI 会话合并，桌面与手机共用；关闭显示页面不截断。终端生命期与记录器生命期分离，只有可信退出才收尾。来源、房间和权限不通过路径/标题/时间猜测。

source_path: [功能与限制](../docs/conversation-journal.md)、[本地记录](../src/supermonoagent/conversation_store.py)、[生命周期管理](../src/supermonoagent/conversation_manager.py)、[服务部署](../deploy/journal/README.md)。

采用私有逐会话 SQLite WAL 原始账本、受证据约束的独立对话投影、可重建导出、同中继域名下独立记录服务。镜像回执只证明完整内容与对应事件复制，不能抹掉原采集缺口。关闭记录器仅暂停活会话；关闭时已接收的 EOF 必须在释放存储前落成 ended/incomplete。

source_path: [关闭回归](../tests/test_conversation_manager.py)、[来源证据与收尾](../tests/test_conversation_sources.py)、[镜像与缺口真相](../tests/test_conversation_sync.py)。

Codex 0.167.0 已有匹配版本私有快照的被动读取证据。Claude Stop 只可作为候选回复；未知人类输入来源不进入已确认对话。完整 Claude 自动记录模式仍是未决产品分支。现有服务器 SSH 在认证前关闭、原生电脑控制无法可靠识别浏览器 URL，均不能用本地合成测试代替生产或真机验收。

source_path: [来源适配器](../src/supermonoagent/conversation_sources.py)、[候选界面测试](../tests/test_workspace_journal.py)、[Android 合成连接](../tests/test_android_journal_fixture.py)。Inference: 已通过单元/合成集成测试不代表现有所有提供商版本可完整记录，也不代表已部署。
