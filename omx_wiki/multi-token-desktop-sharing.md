# 多 Token 桌面共享：已支持的与缺的一块

2026-11-17。用户想「开两个桌面、各用不同 token 连中继，让其他电脑的用户独立共享这台机器」。逐层核实＋实测后的结论。

## 已支持（实测，tests/test_workspace_second_token.py）

- **账号层**：邮箱注册即得独立 token（accounts.json，可 rotate 吊销）；daemon `allowed()` 接受全部账号 token。
- **隔离层**：每个 token 经 `ensure_owner` 拿到自己的分组＋独立频道；`owns`/`_channel_ok`/`summary(token)` 三处都按 token 过滤——窗口、订阅、输出帧互不可见。
- **中继层**：daemon 把 `access_tokens()`（默认 token＋全部账号 token）发布进房间；异机用户打开 `http://<中继>/?code=<房间>` 输入自己的 token，走 phone 路径认证，只看到自己名下的窗口。
- 实测：第二 token 页面（经中继）初始目录为空；在其名下开窗口后 owner 页面零泄漏、零输出串流。

## 缺的一块：本机双桌面分身份

- `workspace.js` `tokenlessLocal`（环回连接一律省略 token，永远以默认 owner 身份连）＋ `desktop.py` 本地模式只填默认 token。
- 后果：同一台电脑开第二个桌面窗口仍是 owner 视角，无法以 token B 身份操作。
- 小改方案（已给用户、待确认）：① 页面显式给了 token 时跳过免 token 路径；② `desktop --token` 本地模式传进页面 fragment。改完本机桌面 B 开的窗口就是异机 B 用户看到的那套。

## 移植提示

多租户终端宿主的隔离三件套：目录按 owner 过滤、订阅校验 owner＋频道、中继只认 host 发布的 token 集合。查「是否已支持多用户」时先看这三处，再看客户端有没有「环回免认证」捷径——那个捷径往往是最后一道缺口。

相关：[[claude-wheel-native-paging]]
