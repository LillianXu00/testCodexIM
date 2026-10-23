# 2026-10-22 重启后环境一致性检查

## 事故证据与结论边界

检查发生于北京时间 18:53 之后。机器约 3.5 GiB 内存、无 swap。上次改名验收时同时启动了多个 OpenClaw CLI（skill RPC、MCP probe），部分 90/100 秒超时。这种并发对当前机器不合适，可能加重 CPU/内存压力，不能据此宣称服务器故障与操作无关。

`last -x` 显示服务器于 17:51 重启；现有 syslog 的重启前记录停在约 17:35:50。本次启动内核日志记录 journald 文件损坏或未正常关闭。现有 kern.log/syslog 未检索到明确的 OOM、killed process、kernel panic 证据。因此能够确认非正常关闭/重启与检查超时，尚不能证明具体是 OOM、CPU 饱和或其它原因；缺失的日志也不能排除资源耗尽。

本次采用串行轻量检查，没有再次并发启动 OpenClaw CLI，也没有重启正常运行的中心、通用 MCP 或 OpenClaw 网关。

## 检查发现与修复

1. 中心 13900、通用 MCP 13901、OpenClaw 18789 已在运行。WorkBuddy 13902 未监听，其 `.runtime/workbuddy/process.json` 仍指向重启前 PID 59179。已通过原 `scripts/workbuddy_service.py start` 恢复独立适配器，复用原数据库与配置，并刷新 PID 记录。
2. 项目虚拟环境残留 `agent-agora-lite 0.1.0` 安装元数据，与 `planetbaltan 0.3.0` 并存。已卸载旧发行包并重新安装新版 editable 入口；`pip check` 通过，旧发行包不再被发现。`agora` 兼容命令仍由新版提供。
3. OpenClaw 三个连接已为 `planetbaltan-main`、`planetbaltan-english-learning`、`planetbaltan-agora-tester`，旧连接配置已移除。各自工具 allow/deny 与原身份隔离范围一致。
4. 共享 `planetbaltan-community` skill 文件与仓库正文一致，本机绑定使用新 MCP 前缀；旧 skill 文件不存在且配置中已禁用。上轮已有运行中网关 `skills.status` 的成功结果，本次未再启动重型宿主 CLI 刷新它。

## 数据与安装一致性

对比 `.runtime/before-planetbaltan-20261022-172215/central.sqlite`：

- agents：7 条，逐行一致。
- credentials：7 条，逐行一致。
- transport_bindings：6 条，逐行一致。
- 中心、通用通信、WorkBuddy 三个 SQLite 数据库 `PRAGMA quick_check` 均为 `ok`。
- 两个接入包 SHA256SUMS 全部通过，安装中的 skill 正文与源文件一致。
- 新旧 Python 导入指向同一实现；新版核心 0.3.0、WorkBuddy 适配器 0.1.1；新旧 CLI 帮助入口正常。

改名修改仍留在工作区，尚未提交或推送到远程；远程基线与本机工作区的差异是未发布状态，不应把远程基线当作最新安装包。

## 修复后的实际验证

串行使用 OpenClaw 保存的原连接 URL 和凭证：

- 13900、13901、13902 `/health` 均成功，首页为 PlanetBaltan。
- main、english-learning、agora-tester 的个人身份分别验证通过，三个 SSE MCP 连接各发现 19 个业务工具。
- WorkBuddy Streamable HTTP MCP 可初始化，发现 19 个业务工具，其中包含 `a2a_pull_messages`。
- 未发布任务、领取消息、清空队列或额外触发真实模型；因此不声称本次重新验证了真实 WorkBuddy 定时任务或模型处理闭环。

结果文件：`.runtime/post-crash-consistency-report.json`。

## 尚未消除的运维缺口

社区组件目前由项目脚本管理，没有统一的开机恢复机制；本次检查未发现相关用户级 systemd 单元。WorkBuddy 在重启后漏启说明启动恢复仍需完善。此次没有新增 systemd、swap 或自动重启策略，也没有改变部署架构。

后续本机验收应优先使用轻量 HTTP/MCP 读取并串行执行；需要宿主 CLI 时一次只运行一个，设置总超时，避免在这台无 swap 的小内存机器上同时启动多个 Node CLI。历史验收结果应结合当下健康检查使用，不能作为重启后仍正常的证据。
