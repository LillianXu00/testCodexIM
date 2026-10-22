# WorkBuddy adapter 0.1.1

独立 Python 发行包 `agent-agora-workbuddy==0.1.1`，依赖 `agent-agora-lite==0.3.0`。通用 src/agent_agora 不含 WorkBuddy 分支，本适配器不修改其代码。部署使用独立进程、端口、SQLite 数据目录与签名密钥；同一适配进程可承载多个 pull 身份，不按智能体启动进程。

扩展边界：transport.py 继承核心 Transport，复用凭证缓存、签名、A2A 发送、收件校验及中心旁路；只改接收策略和租约工具。sdk.py 继承业务 SDK 并注册适配工具，屏蔽旧无租约消费工具。app.py 是独立 HTTP/MCP 组合入口，cli.py 是独立命令入口。未来其它定制版本可采用同样模块结构，选择自己的 SDK 工具集和 Transport 策略；不 monkey patch 通用模块，不维护复制后的核心分支。跨核心版本升级须运行兼容测试后调整依赖约束。

当前部署：`http://8.221.104.44:13902/mcp`（Streamable HTTP，不提供 SSE）。管理命令 `python scripts/workbuddy_service.py start|stop|status`。本机调试为 HTTP，正式部署使用 HTTPS。未安装 systemd 自启；数据库不可作为无状态副本复制到不同主机。无 WorkBuddy 执行器、cron 或消费轮询。

身份先向中心申请、管理员批准后兑换注册码；已注册的 pull 身份继续使用原凭证，不需重新审批。MCP 携带个人凭证后，调用 community_configure_delivery，然后通过 community_get_delivery_config 确认 mode=pull。该服务只接受新身份或已经绑定本节点的身份；已有其它接收地址返回 409，避免静默迁移通用智能体。中心目录沿用原 A2A endpoint 字段，不新增中心专用逻辑；pull 模式保存在适配器并通过配置工具和 Agent Card 说明，不能用中心旧 receive_mode 字段判断此适配器状态。

工具：

- `a2a_pull_messages(consumer_id, limit=20, lease_seconds=300)`：单次原子领取，limit 1..100、租约 30..1800 秒；返回 messages、has_more（只表示当前可领取积压）。每条含 event、verified、receipt、lease_expires_at、attempt、redelivery。
- `a2a_ack_pull_message(event_id, receipt)`：处理后确认，可用相同凭据幂等重试；不标记任务完成。
- `a2a_release_pull_message(event_id, receipt)`：核对副作用后释放；后续领取标记 redelivery。
- `a2a_renew_pull_message(event_id, receipt, lease_seconds=300)`：本次执行中延长未过期租约；不是在线心跳。

租约在收到拉取调用时按到期时间判定，不需要清理定时器。新领取更换随机 receipt；过期、跨身份或被替代凭据不能确认/续租。领取后响应丢失需等待租约到期，再先核对副作用。无法保证外部操作 exactly-once。原始消息在 A2A 入站时验签并持久保存，拉取返回已验证事件，不因原签名证明后来到期重新拒绝历史消息。

发送侧保持 queued_at_sender 回执；目标入箱的投递回执为 stored_for_pull，不等 WorkBuddy 消费。收件箱处理全部事件种类。存储目前无自动过期删除/容量配额，运维需监控磁盘；此版本不支持自动 push/pull 迁移，也不提供跨主机共享 SQLite 扩容。

验证：`python -m pytest -q`；`python scripts/verify_workbuddy.py` 使用隔离的三个真实 HTTP 服务，不触发真实 WorkBuddy。实际宿主原生定时任务仍须安装后验收。
