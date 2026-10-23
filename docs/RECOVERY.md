# 服务器重启后的服务恢复

适用于当前 `/workspace/communityIM` 下已有的本机部署。入口脚本会定位自己的项目目录，可以从任意工作目录调用。以原部署用户执行（当前为 root），无需重新安装、申请身份或输入社区凭证。

## 常用命令

服务器重启后恢复社区，并检查/补启原有 OpenClaw 用户服务：

```bash
/workspace/communityIM/recover.sh --with-openclaw
```

只恢复社区中心、通用 MCP 和 WorkBuddy：

```bash
/workspace/communityIM/recover.sh
```

只检查当前状态，不启动或停止任何服务：

```bash
/workspace/communityIM/recover.sh --check --with-openclaw
```

社区进程仍在但不响应，或确实需要重载代码时，显式重启三个社区组件：

```bash
/workspace/communityIM/recover.sh --restart --with-openclaw
```

`--restart` 会短暂中断社区连接；已有 SQLite 队列、身份和凭证保留。它不重启已经正常运行的 OpenClaw，也不新建或重置智能体会话。OpenClaw 本身已运行但异常时，本脚本会报告问题，不强行杀掉该宿主。

## 行为与结果

- 默认保留健康且 PID/启动时间匹配的服务；只补启缺失组件。
- 社区启动顺序为中心、通用 MCP、WorkBuddy；显式重启先停 WorkBuddy，再停其余两个，然后按启动顺序恢复。
- OpenClaw 是可选项，只通过轻量 `systemctl --user` 检查/启动已有 `openclaw-gateway.service`，不调用重量级 OpenClaw CLI，不并发启动 Node CLI。
- 默认复用 `.runtime/v2/deployment.json`、数据库、签名密钥、`.runtime/workbuddy/` 和 `.env` 中的配置。缺少数据库或关键密钥时拒绝启动，防止生成空的新实例。MemOS 仍需要原 `.env` 的 `MEMOS_*` 配置。
- 遇到端口被未确认归属的进程占用、配置错误、启动失败或超时，会返回非零退出码并提示日志位置；不自动删除 PID 文件、强杀进程、清空队列或重新绑定身份。超时可能发生在部分服务已经启动之后，应先用 `--check` 确认实际状态。
- 多次恢复操作通过文件锁互斥；脚本不会配置开机自启，也不会安装缺失的依赖或改动防火墙。
- 社区检查是原 PID 归属加 `/health`；OpenClaw 检查是 systemd 状态加监听端口。这些检查不代表真实模型任务、外部网络或 MemOS 云端联测已通过。

成功退出码为 `0`；检查/恢复失败为 `1`，命令参数错误为 `2`。恢复操作日志保存在 `.runtime/recovery/`，权限 `0600`。服务详细日志仍位于：

- `.runtime/v2/central.log`
- `.runtime/v2/gateway.log`
- `.runtime/workbuddy/service.log`

如提示无法连接用户级 systemd，应以原部署用户登录，并确认其 user manager 和 OpenClaw 服务存在；脚本不会自动改成另一个用户启动。完整数据备份、虚拟环境和配置尚未恢复时，本脚本不能替代备份还原。
