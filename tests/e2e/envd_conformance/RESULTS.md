# 验收测试记录 / Acceptance Test Results — 2026-08-07

环境：本地部署 CubeSandbox（dev-env QEMU 虚拟机，CubeAPI @127.0.0.1:13000），
基线 Go envd 0.5.13（`ghcr.io/tencentcloud/cubesandbox-base:2026.16`），
cube-envd 0.1.0（`make cube-envd` 产物，含独立评审整改，见 §2b）。

## 1. 单元测试

`make cube-envd-test` → **45 passed, 0 failed**（含 Connect envelope 编解码、
proto3 JSON 映射、路径/用户解析、降权执行、错误映射、进程组信号、句柄化进程表
防 PID 复用、常量时间令牌比较）。clippy `-D warnings` 通过。

**2026-08-31 重跑**（阶段 1 项 1.5 CORS 落地后）：`cargo test --locked` →
**72 passed, 0 failed**（+6 个 CORS 用例：预检回显、方法/Origin 准入、实际请求的
Expose-Headers 六项、预检判定、handler 自有 Vary 优先）；
`cargo clippy --release --all-targets --locked -- -D warnings` 与
`cargo fmt --check` 均干净。

## 2. 一致性对拍（cube-envd vs Go envd 0.5.13）

同一镜像起两个容器（cube-envd 经 `ENVD_BIN` 开关注入），49 个协议场景逐报文对比：

```
PASS 40  FAIL 0  DECLARED-DIFF 9  SKIP 0  MISSING 0
```

完整输出可按 [README.md](README.md) 的步骤用 `conformance.py` 复现。
9 项 DECLARED-DIFF 均为设计声明的 MVP 差异（PTY、watch 家族、/files/compose、
gzip 编码、嵌套 selector 宽容性、解析器错误措辞、符号链接 lstat vs follow），
allowlist 见 `conformance.py` `DECLARED_DIFFERENT`。注：allowlist 共 10 条，
本次 fixture 集命中 9 条——gzip 下载场景（`rest_files_gzip_accept`）的录制
后补进了 `capture.py`，下次重录基线后将作为第 10 条命中。

### 2a. CORS 对照（阶段 1 项 1.5，2026-08-31 新增）

同镜像双容器，逐个请求比对 `Access-Control-*` 与 `Vary`（大小写无关）：

| 请求 | 结果 |
|---|---|
| `OPTIONS /health` + Origin + `ACRM: POST` | 一致（204 + ACAO `*` + ACAM 回显 `POST` + Max-Age 7200 + 预检 Vary）|
| 同上 + `ACRH: content-type, x-access-token` | 一致（额外回显 ACAH）|
| `OPTIONS` + `ACRM: TRACE`（不在允许方法集）| 一致（仅 Vary，无 ACAO）|
| `OPTIONS` + ACRM 但无 Origin | 一致（仅 Vary）|
| `GET /health` + Origin | 一致（ACAO `*` + Expose-Headers 六项 + `Vary: Origin`）|
| `GET /health` 无 Origin | 一致（仅 `Vary: Origin`）|
| `POST /envs` + Origin | 一致 |
| `GET /files?path=...` + Origin | **CORS 头一致**；`Vary` 不同（Go `Accept-Encoding`，cube-envd `Origin`）|
| `OPTIONS /health` + Origin、无 `ACRM` | 一致（405 + ACAO `*` + Expose-Headers）——review 补测 |

最后一行是项 1.3 的既有缺口（上游 `download.go:118` 设置 `Vary: Accept-Encoding`，
cube-envd 的 download 尚未发该头），不是 CORS 差异：上游 CORS 中间件在 handler
**之前**写 `Vary`，会被 handler 自己的 `Set` 覆盖，故 1.3 落地后 cube-envd 同样
只剩 `Accept-Encoding`——`cors.rs` 的 `apply()` 已按此语义实现（响应已有 `Vary`
则不动）。`Vary` 不在 `conformance.py` 的 `HEADERS_KEPT` 比对面内，不影响对拍结论。

**Review 补测（2026-08-31）**：`OPTIONS /x`（无 `ACRM`）对 rs/cors 是 actual 请求，
`isMethodAllowed` 对 OPTIONS 恒放行（cors.go:490-492）——cube-envd 原实现把
OPTIONS 排除在方法集外，该形状只回 `Vary: Origin`，与上游（405 + ACAO +
Expose-Headers）不一致；已修（`is_method_allowed` 对 OPTIONS 恒 true）并新增
conformance 场景 `rest_cors_options_actual` 锁定。

对拍侧新增 7 个 CORS 场景；`conformance.py` 的 `HEADERS_KEPT` 已扩展纳入五个
`Access-Control-*` 头（`Vary` 因 1.3 缺口仍不在比对面），CORS 头差异从此会被
fixture 对拍自动抓到。全新容器重录后 **PASS 47 / FAIL 0 / DECLARED-DIFF 10**
（57 场景）。

## 2b. 独立评审整改（三个独立 sub-agent 复核）

代码评审 / 协议一致性 / E2E 三路独立 agent 复核后发现并已修复的缺陷：

| 编号 | 缺陷 | 修复 |
|---|---|---|
| C1 | 未建进程组，`kill_pid` 注释谎称"组长"，超时/信号只杀直接子进程、泄漏孙进程 | pre_exec 中 `setpgid(0,0)`；`kill_process_group` 对 `-pid` 发信号，整组回收（相对 Go 泄漏为有意改进，已文档化）|
| C2 | `child.id().unwrap_or_default()` 可返回 pid=0 | 显式取 pid，spawn 失败按缺失二进制事件流处理 |
| C3/C4 | 进程表以 OS pid 为键，PID 复用时误删/误杀 | 引入单调 `ProcHandle`，表以句柄为键，`find_pid` 取最新句柄 |
| C5 | multipart 上传无大小上限 | `multer` `Constraints::size_limit`，超限→413 |
| S2 | chown 跟随符号链接 | 改用 `libc::lchown` |
| S3 | access token 非常量时间比较 | `constant_time_eq` |
| R1 | `lock().unwrap()` 遇毒锁 panic | `unwrap_or_else(PoisonError::into_inner)` 恢复 |
| F1 | proto3 零值未省略（size/mode）；`.current_dir()` 以 root 身份先 chdir；无效 cwd 静默降到 `/` | 零值 `skip_serializing_if`；chdir 移入 pre_exec 且在降权之后；无效 cwd 返回 `invalid_argument`（不再静默成功）|
| F3 | 嵌套 selector 被展开，畸形 SendSignal 可误杀存活进程 | 嵌套 selector 解析为空 → `not_found`，无副作用 |
| F6 | not_found 措辞与 Go 不一致 | 按 pid/tag 逐字对齐 Go 文案 |

以上均在活体对拍中逐条对 Go 基线复验通过（空文件省 size、mode-000 省 mode、
无效 cwd 返回字节级一致的 `invalid_argument`、嵌套 selector 双方均不动进程）。

覆盖 issue #1227 要求的五类路径：成功 / 错误 / 超时（`Connect-Timeout-Ms`
到期杀进程 + `deadline_exceeded`）/ 取消（断连后进程存活）/ 大输出（2 MiB
字节级一致）。

## 3. SDK 端到端（三大验收场景）

模板 `tpl-49213eb35f7a44f89f42995c`（基于含 §2b 全部整改的 cube-envd 镜像
`create-from-image` 创建）；Python SDK（`sdk/python`）经
CubeProxy 访问。**19 passed, 0 failed**。

| 场景 | 断言 |
|---|---|
| 1 健康检查 | 沙箱达到 RUNNING（就绪探测 :49983/health 通过）、基础命令往返 |
| 2 命令执行 | stdout/stderr 分流、退出码、env 注入、用户切换、cwd、大输出管道、超时强制生效（2s 抛错） |
| 3 文件读写 | 文本/二进制写读一致、list/stat/make_dir/rename/remove、缺失文件报 404 |
| 回滚验证 | Go envd 模板 `tpl-72f50185f0c8428a99620480`（`ENVD_BIN=/usr/bin/envd`）命令 + 文件 smoke 通过 |

## 4. 性能对比（同镜像同宿主，`perf.py` 实测）

| 指标 | Go envd 0.5.13 | cube-envd 0.1.0 | 变化 |
|---|---|---|---|
| 稳态 RSS | 16.1 MiB | 2.3 MiB | −86% |
| 冷启动至 /health 204（均值，10 次） | 38.9 ms | 13.2 ms | −66% |
| `echo hi` 端到端延迟 P50 / P95（100 次） | 6.3 / 8.3 ms | 4.3 / 5.6 ms | −32% / −33% |
| 静态二进制体积 | 10.5 MB | 2.6 MB | −75% |

## 复现

见 [README.md](README.md)。E2E 需要本地部署环境与两个模板：

```bash
cubemastercli tpl create-from-image --image <cube-envd 镜像> --expose-port 49983 ...
CUBE_API_URL=... CUBE_PROXY_NODE_IP=... TEMPLATE_CUBE=<tpl> TEMPLATE_GO=<tpl> \
  python3 e2e_sdk.py
```
