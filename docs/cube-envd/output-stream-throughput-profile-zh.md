# cube-envd 进程流吞吐：profile、修复与对照（中文）

本文回答一个问题：**同一台机器、同一个镜像、同一个客户端，为什么 cube-envd 的
`process.Process/Start` 流式输出比 Go envd 慢，慢在哪里，改了什么，改完多快。**

面向对象：`perf/cube-envd-output-backpressure`（PR #28）的评审，以及上游
[TencentCloud/CubeSandbox#1610](https://github.com/TencentCloud/CubeSandbox/pull/1610)
（in-repo Rust cube-envd）的对照。

结论先说：

1. **不是 Nagle/TCP_NODELAY**：给 acceptor 加 `TCP_NODELAY` 后吞吐在噪声内
   （见 §3.1），Go 的 `net` 确实默认关 Nagle，但这项对吞吐不是主因（对交互式
   小帧延迟仍有意义，保留）。
2. **不是网络、不是 hyper、不是 `/files` 那条写路径**：guest 内 `/files` 下载
   33.5 MiB 只要 **57 ms**（同一个 envd、同一个 hyper/body/socket 写路径），
   而同一时刻进程流要 **2.1 s**，CPU 3990 ms vs 50 ms。
3. **是每帧固定开销**：32 KiB 一帧时，1024 帧的进程流要 ~4.4 s CPU
   （≈4.3 ms/帧），而同机微基准里 base64+JSON 编码整帧只要 **233 µs**
   （占 ~5%）。剩下 ~95% 花在“每帧一次”的总线交接、队列、唤醒上；沙箱只有
   2 vCPU，这部分开销直接被放大成吞吐上限。
4. **修复 = 让帧变大 + 让每帧更便宜**：读块 32 KiB→128 KiB 并把子进程管道
   放大到同尺寸（`F_SETPIPE_SZ`）、`publish_data` 加无等待快路径、JSON 单缓冲
   直写、队列深度按新帧长重算。结果（32 MiB，5 次中位）：

   | 版本 | 32 MiB 吞吐 | 是否完整 |
   |---|---|---|
   | Go envd 0.5.11（镜像自带） | 111.6 MB/s | 完整 |
   | 本分支（优化前，sha `02467a00`） | 34.4 MB/s | 完整 |
   | **本分支（优化后，sha `78401d93`）** | **103.7 MB/s**（guest 内单次 147 MB/s） | 完整 |
   | PR #1610（sha `2912f8a2`） | *无意义*（见下） | **截断：33.5 MB 只交付 1.4–5.2 MB** |

   guest 内单次对照（`path_diff.py`，客户端在 guest 内走 loopback）：
   **0.228 s / 320 ms CPU**，对比优化前 **2.114 s / 3990 ms**（9.3× 墙钟、
   12.5× CPU），对比 Go **0.294 s / 440 ms**（快 1.3×）。

---

## 1. 测试环境与被测对象

同一台云主机（8 核，AMD EPYC 9K65 = 宿主），同一镜像
`cube-sandbox-cn.tencentcloudcr.com/cube-sandbox/sandbox-code:latest`
（@sha256:467494c3…），同一套组件（CubeMaster/Cubelet/CubeProxy），只换注入的
envd 二进制。guest 内：`nproc=2`，`cpu.max=200000 100000`（=2 CPU 配额），
内核 `6.6.69-opencloudos9.cubesandbox.pvm.guest`。

| 标签 | 模板 | 实际服务的 envd | sha256 |
|---|---|---|---|
| stock | `tpl-d4d23eaf66c24c218caa0b4f` | `/usr/bin/envd`（Go 0.5.11 / `b781ad4`） | `4c712122…` |
| pr1610 | `tpl-79b19801602340e08d481c8d` | 注入的 `/usr/local/bin/envd`（PR #1610 的 Rust 实现） | `2912f8a2…` |
| bp（本 PR 优化前） | `tpl-bd86946fcfe94e639d576afc` | 同上，本分支 `b99f3bfe` 构建 | `02467a00…` |
| fast（本 PR 优化后） | `tpl-7693bf4d485c495b9bd4c5b0` | 同上，本分支 + 本文 §4 的改动 | `78401d93…` |

每个模板都用 `/proc/<pid>/exe` + `sha256sum` 复核“到底是谁在服务”，而不是相信配置。
注入命令（以 fast 为例）：

```bash
cubemastercli tpl create-from-image \
  --image cube-sandbox-cn.tencentcloudcr.com/cube-sandbox/sandbox-code:latest \
  --writable-layer-size 1G --expose-port 49999 --expose-port 49983 \
  --probe 49999 --probe-path /health \
  --enable-inject-envd --envd-path /root/cube-envd-fast \
  --env ENVD_BIN=/usr/local/bin/envd
```

> 注意：`tpl-bd86…`/`tpl-7693…`（fast）的测量是**同一次会话连续跑完**的
> （见 §3 的原始 JSON），pr1610 与它们的吞吐测量并行执行，`/dev/null` 客户端、
> 5 次取中位；guest 内单次对照（§2）不并行。

---

## 2. 决定性对照：进程流 vs `/files`（同一 envd、同一客户端）

`path_diff.py`：在 guest 内用 `python3` 造 32 MiB 文件，客户端**也在 guest 内**
走 `127.0.0.1:49983`，分别跑

* `POST /process.Process/Start`（`cat` 该文件，即进程流路径：pump → 总线 →
  driver → body → hyper）
* `GET /files?path=…`（同一条 body/hyper/socket 写路径，但无 base64、无 JSON、
  无总线）

并读 envd `/proc/<pid>/stat` 前后差值：

| 版本 | 进程流 | envd user+sys | `/files` 下载 | envd user+sys |
|---|---|---|---|---|
| stock Go | 0.294 s（44.78 MB） | 440 ms | 0.0050 s（33.55 MB） | ~0 ms |
| pr1610 | 0.050 s，**只交付 1,137,089 B** | 20 ms | 0.0759 s | 80 ms |
| bp（优化前） | 2.114 s（44.78 MB） | 3990 ms | 0.0596 s | 70 ms |
| **fast（优化后）** | **0.228 s（44.75 MB）** | **320 ms** | 0.0577 s | 50 ms |

读法：

* `/files` 在 guest 内 57 ms 走完 33.5 MB（≈580 MB/s），**证明 hyper/body/socket
  写路径和网络路径都不是瓶颈**；瓶颈只可能在进程流自己的机制里。
* 优化前进程流比 `/files` 慢 **37×**、CPU 多 **57×**，这是整件事的核心事实。
* 优化后进程流与 `/files` 同量级（0.23 s vs 0.058 s），且已快过 Go 的 0.294 s。
* `/files` 相对 Go 仍有 ~11× 的差距（Go 5 ms vs 我们 58 ms，缺 sendfile/零拷贝），
  这是**另一条独立议题**，与本文的进程流修复无关，记录在 §6。

---

## 3. 吞吐矩阵（`prof_thr2.py`，5 次中位，客户端在宿主）

```
python3 prof_thr2.py <template-id> 4 32
```

| 版本 | 4 MiB | 32 MiB | 交付完整性 |
|---|---|---|---|
| stock Go | 79.6 MB/s | 111.6 MB/s | 完整（44.78 MB） |
| pr1610 | 47.9 MB/s | 336.0 MB/s* | **截断** |
| bp（优化前） | 28.0 MB/s | 34.4 MB/s | 完整 |
| **fast（优化后）** | **82.6 MB/s** | **103.7 MB/s** | 完整（44.75 MB） |

\* pr1610 的 MB/s 是**假数**：探针按“期望字节数 ÷ 耗时”计算，而它的流很早就断了。
原始 `delivered`（32 MiB 那一组，期望 33,554,432 B）：

```
3,323,542 B / 0.103 s    3,673,371 B / 0.100 s    5,247,618 B / 0.109 s
1,443,192 B / 0.068 s    1,355,735 B / 0.065 s
4 MiB 组同样是 1.49–5.60 MB 不等 —— 五次都不一样，典型的丢数据/截断
```

这与 PR #1610 评审里对 `cube-envd/src/process/model.rs` 的意见一致：stdout/stderr/PTY
共用一个 **64 事件的 `broadcast` 环**（≈2 MiB / 32 KiB 帧），生产者 `let _ = send(…)`
从不阻塞，订阅者落后就 `Lagged`，随后 `ProcessStream::poll_next` **用
`resource_exhausted` 终止整个 RPC**，把一次成功的命令变成失败、丢输出与退出码。
本 PR（#28）替换的正是这套机制：per-subscriber 有界队列 + 背压到子进程 + 终态槽位
保证 `End` 一定送达，因此**全量交付**。

### 3.1 TCP_NODELAY 的 A/B（被否定的假设）

Go 的 `net` 默认对 TCP 关 Nagle，cube-envd 之前没有设置，这看起来很像原因，于是
写了 `src/app/serve.rs`（自己 accept，逐个连接 `set_nodelay(true)`，其余语义与
`axum::serve` 一致，含 101 upgrade）。同一负载 A/B：

| 32 MiB | 中位 |
|---|---|
| bp（Nagle 开） | 34.4 MB/s |
| 加 nodelay 的同一构建 | 25.8 MB/s（5 次 24.6–36.7，落在噪声内） |

**结论：Nagle 不是吞吐差距的原因。** 该项作为与 Go 的默认行为对齐保留（对
PTY/交互式小帧的尾延迟仍有益），但不计入本次吞吐归因。

---

## 4. 慢在哪：profile 过程与证据

### 4.1 先量化“每帧要多少 CPU”

未插桩的 `cpu_split.py`（32 MiB，宿主客户端，`-o /dev/null`，3 次）：

| 版本 | 墙钟 | envd user | envd sys | 每 32 KiB 帧 CPU |
|---|---|---|---|---|
| stock Go | 0.20–0.24 s | 180–200 ms | 140–190 ms | ≈0.33 ms |
| bp（优化前） | 2.30–2.37 s | 2050 ms | 2370–2490 ms | ≈4.3 ms |

即：**同样 33.5 MB，我们的实现比 Go 多花 ~13× CPU**，而且 2 个 tokio worker
（`worker_threads(2)`）在 2 vCPU 的 guest 里跑满 —— 是 CPU 饱和，不是等网络。

### 4.2 编码路径只占 ~5%

把同一份 musl 测试二进制（`cube-envd --ignored frame_pipeline_cost`，纯编码
微基准）在宿主和 **guest 内**分别跑，32 KiB 输入 / 43,724 B JSON：

| 操作 | 宿主（gnu） | 宿主（musl） | guest（musl） |
|---|---|---|---|
| `base64_encode` | 10.2 µs | 25.0 µs | 49.5 µs |
| `serde_json::to_value` | 1.6 µs\* | 36.8 µs | 84.4 µs |
| `Value::to_string` | 13.6 µs | 46.8 µs | 105.8 µs |
| `message_frame`（to_string+封套） | 17.4 µs | 64.3 µs | 148.2 µs |
| `event_frame`（旧路径合计） | 30.4 µs | 102.2 µs | 233.3 µs |
| `serde_json::to_vec` 直写 | 13.4 µs | 63.0 µs | 145.4 µs |

\* 宿主 gnu 那次 `to_value` 的字符串是 move 进去的，与另外两列不完全可比。

**guest 里整条编码 233 µs/帧**，而实测整条流水线要 ≈4.3 ms/帧：
**编码 ≈5%，其余 ≈95% 是每帧一次的机制开销**（总线快照、`watch` 订阅、
`stalled_since` 锁、队列 permit、跨任务唤醒、body 交接）。这也解释了为什么
“只优化 serde_json”最多只能拿回几个百分点。

### 4.3 沙箱环境本身是正常的（排除“guest 太慢”）

guest 内用 `python3` 做同口径基准（宿主 vs guest）：

| 指标 | 宿主 | guest |
|---|---|---|
| 忙循环（解释器迭代/秒） | 12.49 M | 2.50 M（≈5× 慢，符合 2 vCPU 共享宿主） |
| `getpid()` | 0.13 µs | 0.31 µs |
| 管道读 32 KiB | 9.82 µs | 7.56 µs |
| `epoll.poll(0)` | 0.37 µs | 0.73 µs |
| epoll 就绪往返 | 0.95 µs | 1.89 µs |
| 新分配 44 KB（保持存活） | 15 µs | 53 µs |
| 12000 次/2000 次线程交接 | 51.5 µs | 21.6 µs |
| CPU 时钟自洽性（忙循环 wall≈process_time） | ✅ | ✅ |

**系统调用、epoll、内存、线程交接在 guest 里都是正常量级**，所以 4.3 ms/帧不能
归因于“沙箱系统调用慢”。它是**每帧固定次数的交接/唤醒 × 帧数**在小机器上被放大。

### 4.4 插桩本身的坑（方法论，写下来避免下次踩）

第一版插桩用 `Instant::now()` 逐阶段计时，第二版改用
`CLOCK_THREAD_CPUTIME_ID` 线程 CPU 时间（不受调度影响）。结果：

```
drive iters=1026 frames=2048 bytes=33554432
proc_cpu_ms=14000  read=1905 b64=1378 publish=3728
encode=6920 (to_value=1885 to_string=3651 envelope=687)
```

* 阶段划分方向是对的（编码远小于交接），但**总量被测量行为本身放大了**：
  同一负载未插桩时只有 ~4.4–5 s CPU，插桩版变成 14 s，阶段和也随之变成 13.9 s。
  每个阶段边界两个时钟调用 + 计数在 2 vCPU 上改变了调度形态。
* 因此本文的 CPU 预算以**未插桩**的 `cpu_split.py`（4.3 ms/帧）+ **隔离微基准**
  （233 µs/帧）为准，阶段划分只作方向参考。
* `iters=1026`（≈帧数）说明 **两个循环都没有自旋**，`read`/`publish` 都只做了一次
  真正的等待，问题不是忙等。

---

## 5. 改了什么（本 PR 新增的 6 项）

| # | 改动 | 文件 | 归因 |
|---|---|---|---|
| 1 | `READ_CHUNK` 32 KiB → **128 KiB**，并在 pump 启动时 `F_SETPIPE_SZ` 把子进程管道放大到同尺寸（`widen_pipe`，失败只记 debug） | `process/engine/io.rs` | **主因**：每帧固定开销 × 帧数，帧数降到 1/4（受 `cat` 单次写大小与管道容量限制，实测 4–8×） |
| 2 | 订阅者队列 24→**4** 槽、body 队列 8→**4** 槽 | `process/bus.rs` | 帧变大 4× 后按每 attach ≈1 MiB 内存预算重算；深度只买“突发读”的余量，吞吐下限仍是“每 300 s 一帧” |
| 3 | `publish_data` 无等待快路径（`try_reserve_data`），常见路径不再创建 `watch::Receiver`、不再取 `stalled_since` 锁、不进 `select!` | `process/bus.rs` | 去掉每帧一次分配 + 锁 + 唤醒注册 |
| 4 | JSON 单缓冲直写 `protocol::json_message_frame`（先写 5 B 帧头，`serde_json::to_writer` 直接写进同一个 buffer） | `protocol/frames.rs`、`process/pump.rs` | 去掉 `Value` 树与两次整帧拷贝：宿主 musl 102→63 µs/帧，guest 233→145 µs/帧 |
| 5 | 自写 accept 循环并逐连接 `TCP_NODELAY`（与 `axum::serve` 同语义，含 upgrade） + 冒烟测试 | `app/serve.rs`、`main.rs` | 与 Go 默认对齐；吞吐噪声内（§3.1），保留 |
| 6 | F6：`stalled_since` 改为“等待中的发布者计数 + `StallWait` Drop guard”，标记由**最后一个**等待者清除，另加“取消等待后标记被清除”的单测 | `process/bus.rs` | 关掉评审里“两个发布者互相清标记”的窗口，同时消除我加快路径后可能残留的**过期标记**（会导致 300 s 后误驱逐健康订阅者） |

> 第 6 项在**测量之后**才落地；它只作用于“队列满、发布者真的开始等待”的慢路径，
> 不在热路径上（热路径是 `try_reserve_data` 快路径），所以不影响 §2/§3 的数字。

---

## 6. PR #1610 对照小结

* **它是什么**：`feat(cube-envd): introduce in-repo Rust cube-envd and replace upstream
  Go envd`（head `xboHodx:feat/cube-envd`，base `TencentCloud:master`，42 commits /
  +18,818 行，含构建、镜像、CI、文档、SDK 兼容链路）。它的 PR 描述与 27 条评审
  里**没有吞吐数据**；有一条自动评审明确指出 `model.rs` 的 64 事件 `broadcast` 环
  + `Lagged` → `resource_exhausted` 会丢大输出。
* **它的实测**（`tpl-79b198…`，sha `2912f8a2`，即 PR 分支构建）：
  * 32 MiB 进程流：**五次全部截断**，只交付 1.36–5.25 MB；4 MiB 组同样 1.49–5.60 MB。
  * guest 内单次：交付 1,137,089 B / 33.55 MB 后结束（0.050 s，20 ms CPU）——
    不是慢，是**提前断**。
  * `/files` 下载正常（0.0759 s / 33.55 MB）。
* **和我们的关系**：本 PR（#28）就是替换那套丢输出的机制（有界 per-subscriber 队列、
  背压到子进程、终态槽位保 `End`、慢消费者按“无进展窗口”驱逐而不是丢数据）。
  按本 PR 的验收（§7），全量与慢消费者都零丢失。
* **它的 `tests/e2e/sdk_compat/raw_compare.py`**：评审指出“没有入口点”（`argparse`
  导入但无 `main()`/`__main__`），故本次对照用的是我们自己的
  `threshold.py`/`path_diff.py`/`prof_thr2.py`/`cpu_split.py`。

---

## 7. 正确性（同分支在真机上的验收，供对照参考）

| 项 | 结果 |
|---|---|
| 大输出完整（`threshold.py` 1/2/3/4/6/8/16/32 MiB） | 全部完整 + `EndEvent`，`exhausted=false` |
| 慢但活着（每帧 sleep 1 ms） | 13,263,094 B 零丢失 |
| PTY 洪流 1.5 s | 60,319,193 B，会话存活 |
| 不读的客户端不拖住 deadline/回收 | 单测 `unread_full_response_does_not_block_deadline_or_reaping` |
| 驱逐连接显式报错 | `an_evicted_connection_ends_with_resource_exhausted` |
| SDK 兼容套件 288 用例 | 271 passed / 16 skipped / 1 xfailed，与 stock 基线逐条一致 |
| 单元 + 分层规则 | `cargo test` 315 passed / 2 ignored；`layer_rule` 通过；clippy/fmt 干净 |

---

## 8. 复现物料（全部已入库，服务器关停不影响）

`docs/cube-envd/assets/measurements/`：

| 文件 | 用途 |
|---|---|
| `prof_thr2.py` | 吞吐矩阵：`python3 prof_thr2.py <tpl> 4 32`（5 次，中位/极值） |
| `path_diff.py` | 决定性对照：同 guest 内进程流 vs `/files`，并读 envd CPU |
| `cpu_split.py` | 未插桩 CPU 预算：user/sys + 每线程热点 + `/proc/<pid>/io` |
| `deep_split.py` | 深入版：`/proc/io` 的 syscr/syscw/rchar/wchar、上下文切换、guest NIC 计数、guest 内客户端那一档 |
| `bench_guest.py` / `syscall_bench.py` / `epoll_bench.py` / `clock_bench.py` / `alloc_probe.py` | 沙箱环境基准（分配、系统调用、epoll、时钟自洽） |
| `recon.py` | guest 规格侦察（`nproc`、`cpu.max`、工具可用性、内核版本） |
| `p3-*.json` | 四个版本的吞吐原始结果（含每次 `delivered`，可核截断） |
| `p2-*.json`、`prof-*.json` | 第一轮（nodelay 前）与更早一轮的原始结果 |
| `stage_run.py` | 逐阶段插桩的跑法（插桩代码未入库，见 §4.4 的方法论说明） |

构建被测二进制（musl 静态，与线上注入一致）：

```bash
cd cube-envd && cargo build --release --locked --target x86_64-unknown-linux-musl
```

`fast` = 本文 §5 全部改动，`sha256 78401d93…`；`bp` = 仅背压改动，
`sha256 02467a00…`；两者都在 `/root/cube-envd-{fast,bp}` 同步过（服务器关停后
以仓库内源码 + 上述 sha 为准）。

---

## 9. 未决项 / 建议

1. **`/files` 下载仍比 Go 慢 ~10×**（guest 内 58 ms vs 5 ms，33.5 MB）。
   这条路径读文件 + 走 body，缺 sendfile/零拷贝；与进程流无关，建议独立 PR。
2. **进程流每帧机制开销仍是主项**：本次靠“把帧做大 4×”摊薄。若还要往上走，
   下一步是把两跳交接并成一跳（让 hyper 直接 poll 订阅者队列，把 keepalive/
   deadline/驱逐做成 `Stream` 包装），预期还能再降一档；本次不做，避免在背压
   语义刚验完时大改结构。
3. **README/使用文档**：`READ_CHUNK` 与队列深度的内存预算（每 attach ≈1 MiB、
   每进程 2 MiB 管道）已写进常量注释，未写进面向用户的文档。
4. **pr1610 的截断**值得反馈给其作者：这不是“慢”，是**丢输出并把成功的命令
   变成 RPC 错误**，与本 PR 修复的是同一类问题。
