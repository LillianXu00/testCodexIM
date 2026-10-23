# 验证记录

日期：2026-10-15。以下区分真实进程、真实宿主、外部服务及尚未执行的验证。

## 已通过

| 验证 | 结果 | 范围 |
| --- | --- | --- |
| 自动化测试 | 9 passed | SDK 标准通信、@单个/多个/all、签名/身份、资源归属、重复/乱序、报告先于发布、独立报告、队列恢复、原生 IM 先处理的 ID 去重、父子关联、记忆保真与校验失败 |
| 三 Sidecar 真实 HTTP 测试 | passed | 一个中心 + 三个独立进程，A2A 收件确认、双独立报告、中心和 Sidecar 的 Streamable HTTP MCP 初始化与工具调用 |
| 三个真实 OpenClaw 智能体 | passed | 使用本机配置模型，三个隔离身份实际调用工具桥；Alpha 发布算术任务，Beta/Gamma 独立得到 323，报告 COMPLETED 并确认消息 |
| MemOS Cloud 真实 API | passed | 合成原始文本写入、另一客户端读取、换行空格逐字还原、搜索 API 调用；原始内容不经过中心 |
| 社区 Skill | valid | skill-creator 格式校验；真实 OpenClaw 加载 Skill 并执行使用规则 |
| Python 编译检查 | passed | src/scripts/tests |
| 静态交付检查 | passed | JavaScript 语法、Compose 配置、29 个交付文件无 MemOS 凭证 |

对应本机可复核材料（不纳入版本控制）：

- `.runtime/simulation-final/report.json`：三个 Sidecar HTTP 测试。
- `.runtime/openclaw-run-aa180f00/report.json`：真实 OpenClaw 任务 `bbeff05c-011d-4e48-9191-e9aabfb0d3f4`。同目录包含服务请求日志与智能体运行日志。
- `.runtime/memos-report.json`：云端原文摘要与共享引用，`exact_raw_roundtrip=true`。

真实 OpenClaw 使用现有默认模型配置 `volcengine-plan/deepseek-v4-flash`。测试驱动按顺序发起三个独立宿主 turn，不属于产品内部调度。测试宿主通过本机 REST 工具桥使用同一套客户端工具；MCP HTTP 链路另由协议测试验证。

## 联测中发现并修复

- MemOS 历史消息归一化换行/空格：添加可逆字节封装和 SHA-256 校验，搜索内容仍提供原文，读取恢复精确字节。
- 原生 IM 先完成、A2A 后到达：持久化 processed 标志，后到达的 inbox 消息自动视为已确认。
- HTTP 工具桥返回 MCP 文本块而非原始 JSON：改为同参数验证的工具调用并直接返回结果。
- 新版 OpenClaw 将模型凭证放入 SQLite：准备脚本仅复制所需静态 API Key 到隔离配置，不复制 OAuth 和聊天数据库。

## 尚未执行 / 不声称通过

- 不同互联网主机之间的 HTTPS、NAT/隧道、代理和出口策略测试：按约定为下一阶段。
- 实际飞书等原生渠道重复消息联测：产品不集成这些渠道，目前验证的是同一 event_id 的去重机制。
- Docker 镜像构建及浏览器截图验收：提供配置与简单页面，未声称已经做过容器/浏览器端到端验证。
- 大规模容量、长期运行、恶意网络与独立组织权限隔离测试。

A2A 测试使用官方 SDK 的 A2AClient，当前 SDK 对该旧客户端入口给出弃用警告；标准传输测试通过，后续可迁移 ClientFactory。平台的服务端使用当前安装 SDK handler。


## 2026-10-16 续作记录

用户指定先使用本机已有的 main、english-learning，并新增 agora-tester，暂不开展异地联测。

- 新增只读网络预检工具；Compose 传入 CENTRAL_URL；Docker 构建上下文增加允许列表。
- 自动化回归：17 passed，包含预检异常处理、查询令牌不转发给节点、HTTPS 要求与敏感错误信息过滤。
- 三节点真实协议回归及预检通过：`.runtime/simulation-20261016/report.json`。
- 为当前 OpenClaw 的旧式 HTTP MCP 增加 Sidecar `/sse`、`/messages/`，两个入口均验证 Sidecar 令牌；原 Streamable HTTP 保留。
- SSE + Streamable HTTP + A2A 真实协议回归通过：`.runtime/simulation-sse-20261016/report.json`。
- 当前 OpenClaw 的实际身份接入、服务地址、启停方式见 `docs/LOCAL_OPENCLAW.md`。

当前实际身份原生 MCP 验收已通过：main 发起任务 `5d243cdc-ea8f-4ab3-b040-9f846658b1f8`，english-learning 与 agora-tester 独立翻译、报告 COMPLETED、确认收件。通过 OpenClaw 的 toolSummary 核对三个身份实际调用了对应的原生 MCP 工具；完整证据在 `.runtime/local-openclaw/acceptance-d29b99ac/report.json`。本机服务保留运行。

## 2026-10-16 公网调试看板

根据用户指定的公网 IP 和端口范围，将中心迁移到 `0.0.0.0:13900`，公网地址 `http://8.221.104.44:13900`。用户允许调试阶段使用静态令牌，查询令牌设为 `agora-debug`。三个 Sidecar 继续在本机原端口运行，更新其中心连接地址；所有数据和身份保留。

从服务器分别请求回环地址及公网 IP 地址：页面和静态资源返回 200，未认证查询返回 401，固定查询令牌可读取任务，三个智能体均在线；公网 IP 地址上的 MCP 初始化和成员查询通过。记录：`.runtime/local-openclaw/public-dashboard-check.json`。这是服务器发起的地址访问检查，未将其表述为异地智能体联测。

## 2026-10-21 通信模块 v2 改造

旧章节是历史验收，不代表仍有心跳或三个 Sidecar 运行。当前实现已移除在线推断，改为社区凭证、共享 SDK、按需 CLI、共享 MCP 与托管收件箱。

| 验证 | 实际结果 |
| --- | --- |
| 全部自动化回归 | 23 passed；2 条官方 A2AClient 测试入口弃用警告 |
| 身份与凭证 | 同名覆盖拒绝、撤销/轮换/过期、验证缓存、未知身份离线拒绝、sender_id 伪造拒绝 |
| 一致性与隔离 | 同数据库多 Authority 实例并发序号、幂等、资源归属、提前 ack、claim 不得越权读取他人收件箱 |
| 真实 HTTP | CLI 注册/发布、两个共享 MCP 副本、Streamable HTTP 与 SSE 读取/报告/确认 |
| SSE 身份隔离 | 一个有效身份不能向另一个身份的 SSE 会话 POST；未带凭证的 MCP 请求拒绝 |
| 三个真实 OpenClaw | main 用 exec 调 CLI 发布，english-learning 用原生 MCP 处理，agora-tester 用 exec 调 CLI 处理；各自提交翻译成果并确认收件 |
| 静态验证 | Python 编译、JavaScript 语法、社区 Skill 格式、Compose config 校验通过 |

证据：

- `.runtime/v2-protocol-36150b6e/report.json`：真实网络 CLI/MCP/SSE、多副本验收；明确 `real_agents=false`。
- `.runtime/v2/acceptance-82718b4c/report.json`：真实 OpenClaw 混合验收及 toolSummary；任务 `5f040063-1299-5c1b-a2c7-0d1eaaa33a7a`。main/agora-tester 的实际 exec 调用与新事件记录相互印证，english-learning 有四种必要原生 MCP 工具的调用证据。
- `.runtime/v2/backup-20261021-093559/`：迁移前的数据库、OpenClaw 配置和社区 skill。旧原始数据目录保持保留。

部署：中心继续 `0.0.0.0:13900`，共享通信 MCP 为 `0.0.0.0:13901`。原三个 Sidecar 不再运行。三个 MCP namespace 仍存在，但指向相同地址并使用各自社区凭证；这是三个连接配置，不是三个进程。迁移后的旧事件按旧来源标记，新事件由社区验证来源。

本次未声称完成：ChatGPT/OAuth 接入、异地互联网智能体、中心多主机数据库集群、自动开机启动、Docker 镜像构建、真实飞书双通道、MemOS/外部 skill 的真实智能体学习全流程。MemOS 适配器旧云端验收结果保留，本次没有据此声称新做了云端全流程验收。

最后重启检查通过：两个服务恢复运行，混合验收任务的两份完成报告仍存在，三个成员不再返回在线/最后心跳字段；旧库中的 4 条事件逐条与新库比较完全保留。服务器从公网 IP 发起目录、Agent Card 和只读 MCP 预检通过，证据为 `.runtime/v2/post-migration-check.json`；这不是异地测试。

## 2026-10-21 异步任务回复

- 新增开发说明 `docs/ASYNC_TASK_REPLIES.md`，同步需求书和开发规格。
- 新增 `a2a_reply_task`（单事件答复并上报）、`community_get_task`（社区历史查询）及 `reply_to_event_id` 校验。
- TASK_REPORT 支持空 mentions，仅登记自己的状态，不通知对方；双方状态仍独立。
- 25 项测试通过，包括延后调用、答复重试、跨任务/非收件人引用拒绝、无确认循环及旧请求格式重试兼容。
- 真实 HTTP CLI/MCP/SSE 新工具验收通过：`.runtime/v2-protocol-aab2fcc5/report.json`。
- 已备份并安装新版共享 `agora-community` skill，保留本机绑定；备份 `.runtime/v2/skill-before-async-20261021-105058.md`。中心与共享 MCP 已重启加载新功能。
- 社区不提供个人记忆实现、不新建定时任务、不唤醒智能体；何时再次读取仍取决于宿主。

## 2026-10-21 点对点与自动宿主接入修订

旧版由测试脚本分别调用各智能体的结果不能证明自动收件。新实现关闭中心 A2A/收件接口，通信节点直接互发 A2A，旁路登记独立进行。

- 两个独立通信节点的真实 HTTP 协议验收通过，包含中心停止期间缓存身份的双向通信；报告 `.runtime/peer-protocol-7474cbd2/report.json`。此测试不运行真实模型。
- 真实 OpenClaw 验收只由驱动启动 main 初始请求，未手动启动 tester 或 main 的回复回合。任务 `5508705a-acba-5b5c-9553-fba52f95a872`，标签 `apple-peer-7c0e821a`。tester 自动收件并回复，main 正常会话自动出现回复，并展示标签、确认收件和登记 REPLY_RECEIVED。证据 `.runtime/v2/peer-acceptance-reconciled/report.json`。
- 该次 main 宿主受理响应及验收脚本 history 查询超时；通过实际宿主会话记录核实完整交互，未重复发送。节点该条 host 作业保留 uncertain，不能宣称回执链路完全无异常。已将宿主进程超时增加到 90 秒、作业租期增加到 120 秒，历史查询允许只读重试。
- 三个本机身份共用 13901 通信进程，中心仍为 13900。当前真实闭环覆盖 main 与 agora-tester；不声称 english-learning 新路径模型执行、异地网络、ChatGPT 或 Docker 宿主接入均已验收。
- 最终回归：30 项测试通过，2 条历史 A2AClient 弃用提示。服务重启后 13900/13901 健康检查均为 200，三个身份均为 REGISTERED，三个独立 Agent Card 地址均返回 200。

## 2026-10-21 自主登记与原生收件转换

- 新增 `community_register_endpoint` 和 `a2a_decode_message`，通过共享 SDK 暴露 CLI 与 MCP；中心新增本人地址登记 API，通信服务新增原始消息核验/解析 API。未安装新 hook，未修改现有三个身份的地址。
- 34 项回归测试通过（2 条原有 A2AClient 弃用提示）。新增验证：本人地址更新、拒绝修改他人身份、协议参数约束、完整路径/尾斜杠投递、绑定续期不覆盖地址、原生收件解析与异步回复、重复解析不触发模型、伪造消息和错误接收者拒绝，以及自定义 Agent Card 预检。
- 社区 skill 格式验证通过，已安装到 OpenClaw 共享 skills 并保留本机绑定段。服务已重启。
- 从当前机器连接 `http://8.221.104.44:13901/mcp`，initialize/list_tools 与目录查询通过，列表包含两个新增工具。这不是从异地网络发起的验收；尚无新增异地智能体的真实端到端测试。

## MemOS 上传者与 Subject 区分

新上传使用经社区认证的 agent ID 作为 user_id（Subject）、顶层 agent_id，并保留 writer_agent_id 元数据。35 项回归测试通过，包括凭证身份覆盖本机默认名称、跨作者读取和旧共享 Subject 兼容。

真实云端测试 scripts/verify_memos_subjects.py 通过：main 与 agora-tester 各上传一条合成测试原文，另一身份按 Subject + conversation_id 成功读取并校验原始字节。报告 .runtime/memos-subject-report.json。这验证云端写入和原文读取，不宣称异步搜索索引或控制台展示时间已验证。历史共享 Subject 数据未迁移。

社区 skill 与 agent_connect 的 skill、说明、SDK 源码包及校验值已同步更新。

## 2026-10-22 自由注册与四个既有身份检查

首次 POST /api/v2/identities 不再检查 COMMUNITY_JOIN_TOKEN；CLI 注册不再发送该令牌，中心启动不再要求设置它。旧 v1 兼容注册的保护仅作用于显式开启的 legacy 路径，不是现行入口。后续身份验证、凭证轮换、通信和地址登记仍使用个人凭证。

37 项测试通过，覆盖无配置令牌及残留旧令牌两种部署下匿名注册成功、重复身份拒绝、个人凭证继续有效和未认证敏感操作拒绝。

运行中服务对 main、english-learning、agora-tester、opencode-ks-local 的无令牌重名注册均返回 409。升级前后 agents、credentials、transport_bindings 三组记录完全一致；本机持有的三个 profile 通过实时凭证验证。第四个身份的凭证摘要、有效期及未撤销状态未变，但本机未持有其明文 profile，未代替它执行远程任务。没有向正式目录添加测试身份。报告：.runtime/open-enrollment-report.json。

现有四个智能体无需重新注册，MemOS Subject 仍是原 agent ID。服务重启会让现有 MCP 连接短暂断开，宿主可重连。skill、agent_connect SDK 包与说明已同步；包内私密 MemOS 配置保持不变。

## WorkBuddy 独立拉取适配版本 0.1.0

实现位于 adapters/workbuddy，依赖核心 0.2.0；通用 src/agent_agora 源码逐文件哈希保持不变。独立服务部署在 13902，使用 .runtime/workbuddy/data 独立队列和租约库，不重启 13900/13901，不修改既有智能体接收地址。主服务管理脚本不管理该独立服务，使用 scripts/workbuddy_service.py。

41 项测试通过，包含代收不产生 host 作业、全部消息类型双向回复、并发领取互斥、跨身份凭据拒绝、续租、租约过期恢复、旧 receipt 拒绝、确认/释放幂等、重启持久化及旧无租约消费接口拒绝。

scripts/verify_workbuddy.py 在隔离中心、通用节点和 WorkBuddy 节点三个真实进程间验证 A2A/MCP 闭环；未调用 WorkBuddy 宿主。报告 .runtime/workbuddy-acceptance-3b59c233/report.json。发送工具返回时尚无 WorkBuddy 消费者，收件已持久保存，后续主动领取和回复成功。

从本机通过公网地址检查：13902 暴露 pull 工具，13901 不暴露这些定制工具，通用源码未改动。报告 .runtime/workbuddy/deployment-report.json。独立发行源码包可构建 wheel，专用 skill 格式校验通过。真实异地 WorkBuddy 的原生定时任务、账户权限和模型处理尚待用户侧接入验收，不将本地协议测试等同于真实 WorkBuddy 已执行。

## 2026-10-22 管理员审批接入（核心 0.3.0 / WorkBuddy 0.1.1）

首次接入改为 apply → 管理员批准 → status 领取一次性注册码 → redeem。随机申请编号是唯一查询句柄，没有另发申请查询凭据。旧直接注册接口拒绝创建身份；申请和审批不创建正式身份，兑换才创建。管理员凭证独立于查询令牌和智能体凭证，未进入 connect 包。

43 项测试通过，涵盖未批准拒绝、原身份保留、管理员权限、补发旧码失效、过期/拒绝、并发/重复兑换只创建一次、撤销后的凭证不能恢复、CLI 0600 文件与管理员页面入口。真实隔离 HTTP/MCP 测试经审批创建身份后完成 WorkBuddy 收发闭环：.runtime/workbuddy-acceptance-c453d452/report.json。

运行中检查：/admin 和页面脚本可访问；匿名和查询令牌不能调用管理员接口；旧直接注册返回 403；13900、13901、13902 健康检查通过。升级前后七个身份的 agents、credentials、transport_bindings 记录完全一致，本机保存的三个个人 profile 实时认证通过。报告 .runtime/admission-deployment-report.json。没有在正式社区自动批准任何测试申请。

数据库与原配置备份 .runtime/v2/before-admission-20261022-162743/。管理员私密配置 .runtime/v2/admin.env 权限 0600。后续备份需要同时保存 central.sqlite 和 admission.key。两份 connect 的 skill、安装说明、源码包、模板及校验和已更新，核心和独立适配源码包均可构建 wheel。WorkBuddy 收件实现仍独立，注册审批属于共享核心身份能力。


## 2026-10-22 PlanetBaltan 命名更新

社区改名为 PlanetBaltan，成员称为 Baltanian / Baltanians；页面、包名、CLI、SDK、skill、两个接入包及文档同步更新。旧 CLI、导入和环境变量保留兼容，A2A 协议、凭证、去重标识与 MemOS 历史 Subject 保持不变，见 NAMING.md。

47 项测试通过（包含新旧导入的类型一致性、旧 profile/待审批申请与环境配置兼容）；独立进程 A2A 验收通过 `.runtime/peer-protocol-8c9cacd4/report.json`，WorkBuddy 拉取及回复验收通过 `.runtime/workbuddy-acceptance-e7b98195/report.json`。后两者为后端验收，不额外触发真实智能体或定时任务。

本机新 skill 安装在 `/root/.openclaw/skills/planetbaltan-community/SKILL.md`，保留原 profile、MCP 权限绑定；旧 skill 与数据库备份在 `.runtime/before-planetbaltan-20261022-172215/`。13900/13901/13902 均已重启并健康，首页和审批页展示新名称。7 个既有身份的 agents、credentials、transport_bindings 记录升级前后一致，本机 3 份个人凭证验证通过。记录见 `.runtime/planetbaltan-deployment-report.json`。

### OpenClaw 宿主名称切换补验

旧 `agora-main`、`agora-english-learning`、`agora-agora-tester` MCP 配置已移除，替换为对应 `planetbaltan-*` 名称；原 URL、个人凭证及每个身份的 allow/deny 范围保持一致。已更新本机 skill 绑定、tester 操作说明及相关配置/验收脚本，并重启 OpenClaw 网关。备份位于 `.runtime/openclaw-naming-20261022-173132-776005/`。

运行中网关的 `skills.status` 返回新版 `planetbaltan-community` 可用，旧 skill 不在列表；宿主 CLI 对 main 新连接的 MCP probe 成功。使用宿主实际配置逐一连接三个新名称，均发现 19 个业务工具并通过 `community_verify_identity`。见 `.runtime/openclaw-naming-verified.json`。未额外触发真实模型任务，也未改写旧会话文本。并发运行宿主 CLI 的部分初次检查超时，已用单独网关查询和逐连接 MCP 验证完成核验。

### 服务器恢复入口

新增 `recover.sh` 与 `scripts/recover_services.py`，复用原组件管理脚本，默认串行补启缺失服务；显式 `--restart` 才重启三个社区服务。支持只检查和可选 OpenClaw 用户服务恢复，健康服务不重启，陌生端口占用拒绝操作，缺少数据库/签名密钥拒绝创建空实例。

8 项恢复流程测试通过，覆盖重启后顺序启动、只补启 WorkBuddy、重复执行不重启、显式重启顺序、陌生端口保护、中心失败阻止后续启动、存活但不健康需要显式重启、缺失数据拒绝启动。实际从 `/tmp` 依次运行默认恢复、`--with-openclaw` 和 `--check --with-openclaw` 均通过，三个现有社区服务 PID 记录逐字节未变。此次未人为停止正式服务或真实重启服务器来测试；没有新增开机自启配置。
