# Webhook 功能验证指南

本指南介绍如何在真实 CubeSandbox 环境中验证 Webhook 分支的代码，用于 PR 提交前的 E2E 验证。

## 整体流程

```
本地机器                           CVM 云服务器
─────────                         ─────────────
git push origin <branch>  ──→    git clone <fork> && checkout <branch>
                                部署 CubeSandbox（首次）
                                停掉原有 cube-api
                                cargo build --release（CubeAPI + receiver）
                                运行验证脚本
                                收集截图 + 日志证据
```

---

## 第一步：本地 — 推送分支

```bash
# 在本地仓库，推送到你的 fork（不是 upstream）
git push origin feat/webhook-event-notification
```

## 第二步：CVM — 首次部署 CubeSandbox

> 如果 CVM 已部署过 CubeSandbox 且服务正常运行，跳过此步。

按 [快速开始](./quickstart.md) 完成一键部署，确认服务正常：

```bash
curl http://localhost:3000/health     # 应返回 {"status":"ok"}
curl http://localhost:3000/templates  # 应有至少一个可用模板
```

## 第三步：CVM — 拉取分支代码

```bash
# 如果还没 clone 过你的 fork
git clone https://github.com/<your-github-username>/CubeSandbox.git
cd CubeSandbox

# 切换到 webhook 分支
git fetch origin
git checkout feat/webhook-event-notification
```

## 第四步：CVM — 构建

```bash
# 构建 CubeAPI（含 webhook 改动）
cd CubeAPI
cargo build --release

# 构建接收器
cd ../examples/webhook-receiver
cargo build --release
```

## 第五步：CVM — 运行验证

### 停掉原有 CubeAPI

```bash
# 如果是 systemd 管理
sudo systemctl stop cube-api

# 确认 3000 端口已释放
ss -tlnp | grep 3000
```

### 终端 1：启动接收器

```bash
cd ~/CubeSandbox/examples/webhook-receiver
WEBHOOK_SECRET=test-secret ./target/release/webhook-receiver
# 输出: webhook-receiver listening on http://127.0.0.1:9090
```

### 终端 2：启动 CubeAPI（Webhook 模式）

```bash
cd ~/CubeSandbox/CubeAPI
CUBE_WEBHOOK_URLS=http://127.0.0.1:9090/webhook \
CUBE_WEBHOOK_SECRET=test-secret \
CUBE_WEBHOOK_EVENTS="sandbox.created,sandbox.deleted,sandbox.paused,sandbox.resumed" \
./target/release/cube-api
```

确认日志中出现 `webhook logger enabled`。

### 终端 3：触发生命周期

```bash
# 获取模板 ID
TEMPLATE=$(curl -s http://localhost:3000/templates | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['templateID'])")

# 创建沙箱
SANDBOX=$(curl -s -X POST http://localhost:3000/sandboxes \
  -H "Content-Type: application/json" \
  -d "{\"templateID\": \"$TEMPLATE\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['sandboxID'])")
echo "Created: $SANDBOX" && sleep 3

# 暂停
curl -s -X POST "http://localhost:3000/sandboxes/$SANDBOX/pause" -o /dev/null
echo "Paused" && sleep 3

# 恢复
curl -s -X POST "http://localhost:3000/sandboxes/$SANDBOX/resume" \
  -H "Content-Type: application/json" \
  -d '{"timeout": 300}' -o /dev/null
echo "Resumed" && sleep 3

# 删除
curl -s -X DELETE "http://localhost:3000/sandboxes/$SANDBOX" -o /dev/null
echo "Deleted" && sleep 3
```

### 检查接收器输出

终端 1 应输出 4 次 `=== Webhook Received ===`，对应：

```
sandbox.created  — sandbox_id + template_id
sandbox.paused   — sandbox_id
sandbox.resumed  — sandbox_id + template_id
sandbox.deleted  — sandbox_id
```

---

## 第六步：收集 PR 证据

需要两份内容：

**截图**（直接贴在 PR 评论区）：
- 终端 1（receiver）收到 4 个 webhook 的输出
- 终端 2（CubeAPI）启动日志，含 `webhook logger enabled`
- 终端 3 的 curl 命令执行结果

**日志**（打包为 ZIP 或 Gist）：
```
evidence/
├── receiver.log       # 接收器完整输出
├── cubeapi.log        # CubeAPI 启动日志
├── test-results.log   # 在 CubeAPI 目录执行 cargo test -- logging::http 的输出
└── SHA256SUMS.txt     # 校验（可选）
```

在 PR 中引用时说明验证环境（CVM 配置、OS 版本、CubeSandbox 版本）。

---

## 常见问题

| 问题 | 解决 |
|------|------|
| 无可用模板 | 通过 WebUI 或 `curl POST /templates` 创建一个 |
| 端口 3000 仍被占用 | 检查是否有残留进程：`ps aux \| grep cube-api` |
| `cargo build` 特别慢 | CVM 上装 Rust 工具链后首次编译会慢，正常现象 |
| receiver 没收到事件 | 检查 CubeAPI 日志中是否有 `webhook logger enabled`；确认 webhook_urls 地址正确 |
| HMAC 验证失败 | 确认 receiver 和 CubeAPI 使用相同的 `WEBHOOK_SECRET`/`CUBE_WEBHOOK_SECRET` |
