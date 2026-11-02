# PlanetBaltan 新对话交接

请在 `/workspace/communityIM` 继续维护 PlanetBaltan。先阅读 `DEVELOP.md`、`docs/LARGE_OBJECTS.md`、`docs/RECOVERY.md`，再查看 `git status` 和最近提交；当前功能状态以代码及实际运行结果为准。

## 项目目标与边界

这是独立、异构智能体的松耦合社区，不是多智能体工作流框架。成员称为 Baltanian / Baltanians。中心管理身份、目录、审批、任务/资源记录和看板；智能体自行决定处理任务、使用个人记忆及如何学习。A2A 点对点通信，中心不转发聊天消息，不用心跳推断在线，不同步等待任务答复。

普通智能体使用原生接收入口，已有 OpenClaw 适配会把消息交给宿主。WorkBuddy 缺少外部接收入口，使用独立适配器代收，由其自己的原生定时任务拉取；社区不新建调度器。

## 本次已经完成的大对象改造

- 核心版本 `planetbaltan 0.4.0`；独立 WorkBuddy 适配器 `planetbaltan-workbuddy 0.2.0`。
- `src/planetbaltan/objects.py`：中心对象目录、空间登记、agent/空间映射、配额、对象授权及 S3 请求签名。
- `src/planetbaltan/file_transfer.py`：本地 SDK 流式上传/下载，每块约 1 MiB，下载校验完整 SHA256；不暴露本地路径作为远程 MCP 工具。
- CLI 新增 `planetbaltan upload`、`planetbaltan download`；SDK 新增 `upload_file`、`download_file`。
- 通用及 WorkBuddy MCP 均新增 6 个对象工具，部署时各共 25 个业务工具。远程宿主用自身文件/HTTP 能力上传和下载，不把二进制或 Base64 塞入工具参数。
- A2A 的聊天、任务发布、状态、回复支持 `objects` 参数；签名事件保存到原有 `artifacts.objects`，包括对象 ID、空间、作者、名称、稳定 S3 URI、字节数、SHA256、类型。不新增顶层事件字段，旧协议标识 `agora.peer.v3` 等保持不变。
- 中心登记 `data_spaces`、`agent_data_spaces`、`large_objects`、`object_grants`。现有有效 agent 在部署时已映射同一共享空间；此后可登记新空间、独立凭证/桶并按 agent 分配，历史对象不随默认空间修改而自动迁移。
- 管理员成员列表可以查看和更新数据空间映射。共享空间内有效成员可读已完成对象；私有空间需要所有者或显式授权，跨空间新授权由对象所有者操作。
- 两个接入包已更新：`agent_connect/`、`agent_connect_workbuddy/`，含 skill、SDK 源码包、`OBJECTS.md`、升级说明及无密钥配置示例。

## 文件传输与票据的准确含义

文件二进制由智能体的本地 SDK 或宿主 HTTP 能力直接传到数据胶囊 S3、从 S3 下载；中心只处理元数据、权限和票据，通信 MCP 只转交控制请求。

“票据”不是向数据胶囊另外申请的令牌或 STS 凭证。中心使用长期 S3 Access Key ID / Secret，按 SigV4 为具体方法、对象路径和上传声明计算签名，返回 URL 与请求头。智能体持有签名头，数据胶囊据 Access Key ID 验证签名，不需要知道 agent 的社区身份。Secret 不下发给智能体。数据胶囊原生 MCP token 用于其检索/文本工具，与本次 S3 文件签名无关。

实测兼容性必须保留：

- Rclone 类型密钥使用 `rclone/v1.68.2` 格式 User-Agent。
- 查询参数式预签名 PUT 返回 401，本项目采用 Authorization 请求头签名。
- HEAD 不返回自定义 SHA256 元数据；中心确认大小，若有摘要元数据则核对，完整内容由下载方校验。
- 提供方不执行 If-None-Match 覆盖保护，不能声称是不可变/WORM 存储。已完成对象不再签发上传票据，但之前的票据可能重放；下载 SDK 在内容变化时拒绝交付。
- `expires_at` 使用 15 分钟建议刷新窗口；最终签名有效性由云端校验，不能承诺独立的强制 15 分钟到期、一次性使用或中心即时撤销能力。
- 当前为单请求流式 PUT/GET，默认单对象 1 GiB、上限可配置至 5 GiB；尚无分片/断点续传。
- 社区登记侧共享空间额度为 18,000,000,000 字节；预留和已完成对象共同计入。云端既有文件及外部客户端写入另占空间，不能据此保证整个桶仍有固定余量。
- 未完成预留超过 24 小时加票据窗口后可 abort；没有自动删除已完成文件或定时清理策略。

## 私有配置与当前运行

公共中心 `http://8.221.104.44:13900`，通用通信 MCP `:13901/mcp`（保留 `/sse`），WorkBuddy MCP `:13902/mcp`。OpenClaw 网关为 18789。具体服务状态应实时检查，以上不是可用性保证。

- `.runtime/v2/deployment.json`：既有服务地址及管理/查询配置。
- `.runtime/object-storage.env`：长期 S3 密钥及云端 MCP token，0600；不要打印、提交 Git 或分发给 agent。
- `.runtime/object-spaces.json`：默认空间、提供方、桶与凭证环境变量前缀，0600。
- 只有中心进程加载 S3 长期密钥，通用通信与 WorkBuddy 节点不持有它。
- `scripts/services_v2.py` 管理核心服务；`scripts/workbuddy_service.py` 管理独立适配器。
- `recover.sh --check --with-openclaw` 检查；`recover.sh --with-openclaw` 补启；`recover.sh --restart` 显式重启三个社区服务。没有配置统一开机自启。
- 本机共享 skill `/root/.openclaw/skills/planetbaltan-community/SKILL.md` 已更新并保留本地绑定。OpenClaw MCP 连接名为 `planetbaltan-main`、`planetbaltan-english-learning`、`planetbaltan-agora-tester`；个人身份不随连接名变更。

服务器资源有限，曾在并发运行多个 OpenClaw CLI 检查期间出现异常重启。不要并发启动重量级 CLI 或模型回合；优先串行、轻量 HTTP/MCP 检查。不要把宿主接受回执当作模型执行成功。

## 验证证据

- 65 项完整回归测试通过；最后的流式错误处理调整另通过 7 项对象测试。
- 覆盖共享/隔离空间、授权、配额、凭证撤销、上传/下载、损坏拒绝、A2A 对象引用和 WorkBuddy 拉取引用。
- `scripts/verify_objects_cloud.py` 用隔离测试身份直传 3 MiB 文件，再由另一身份下载核验，验证通过；该脚本清理自身随机云端测试对象，不调用实际 agent/模型。
- `.runtime/object-cloud-report.json`：真实 S3 往返及条件写限制。
- `.runtime/object-deployment-report.json`：部署与身份记录保持一致。
- `.runtime/object-mcp-report.json`：线上两个 MCP 的对象工具与空间查询。
- `.runtime/before-object-support-20261102-100153/`：部署前数据库与 skill 备份。
- 部署时 6 个有效身份全部映射共享空间，原 agents/credentials/transport_bindings 不变；实时人数以后查询为准。
- 两个接入包的校验值和敏感信息排除检查通过。具体实际宿主端文件操作仍需用户逐个联测，不将后端测试宣称为所有平台已验收。

## 其它既有约定与后续工作

用户已允许当前 HTTP/静态凭证调试部署，查询与管理按其要求共用调试凭证；个人身份仍使用独立社区凭证。勿在交接文本或公共包复制私有凭证。

管理员删除成员会撤销凭证、端点并保留历史。当前被删除的 agent ID 禁止重新申请；此前曾因此阻止 workbuddy-winter-02 再次申请，这项规则尚未改成“原 ID 重新审批”。接入包升级通常不需要删除身份。

后续优先按用户新指令开展工作；没有默认授权去重发历史任务、删除真实对象、调整桶策略或调用真实智能体。若仅需要更新接入，保留 profile/身份，更新同名 skill、SDK 并刷新宿主 MCP 工具缓存即可。

Git 远程为 `git@github.com:LillianXu00/testCodexIM.git`，工作分支 `main`。本次提交号以 `git log -1` 为准。`DEVELOPMENT_example.ts` 是用户提供的本地参考材料，未纳入项目版本；不要自动上传它。
