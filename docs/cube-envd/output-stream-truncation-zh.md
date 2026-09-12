# cube-envd 的进程/PTY 输出在约 2 MiB 突发后被截断

> 2026-09-12 · 真机全栈实测（PVM，v0.7.1）· 被测 `c1cd0a0d`（PR #27）
>
> 结论：**cube-envd 会把输出量超过约 2 MiB 的命令流/PTY 流主动掐断**
> （`resource_exhausted`，且没有 EndEvent），而 Go envd 用背压处理，一个字节都不丢。
> 这是本次全栈测试里**用户最能感受到、且确实不如 Go** 的一处，优先级高于之前记录的
> 下载吞吐差异。

---

## 1. 现象（同一个客户端，同一台机器，同一个 CubeProxy）

| 场景 | stock（Go envd 0.5.11） | pr27（cube-envd `c1cd0a0d`） |
|---|---|---|
| `timeout 2 yes ...`，裸读不解析 | **437 MB** / 2.02 s，无错 | **1.7 MB**，0.06 s 结束 |
| `timeout 2 yes ...`，SDK 风格逐帧解析 | **302 MB**，有 EndEvent | **3.5 MB**，`resource_exhausted` |
| `timeout 2 yes ...`，每帧 sleep 1 ms | **22.7 MB**，被拖慢到 4.1 s，**一字节不丢**，有 EndEvent | **81 KB**，`64 events dropped` |
| `cat` 一个 50 MiB 文件 | **完整 67,947,660 B**，有 EndEvent | **65,536 B** 后报错 |
| PTY 里 `yes` 1.5 s | **96 MB**，会话继续可用 | **40 KB 后流失效**，SDK 抛 `RuntimeError: resource_exhausted: 322 events dropped` |
| 之后 envd 是否还健康 | 正常 | 正常（只断这条流，守护进程没事） |

用 **curl（C 客户端，全速写盘）**复测，排除"我的 Python 太慢"：

```
stock  cat 50 MiB : 90,676,488 B 完整，EndEvent，0.50 s
pr27   cat 50 MiB :  2,886,251 B，resource_exhausted，0.33 s
stock  yes 2 s    : 488,259,529 B 完整，EndEvent，3.84 s
pr27   yes 2 s    :   2,227,513 B，resource_exhausted，0.22 s
```

即：**换多快的客户端都没用**，瓶颈不在消费端。

## 2. 阈值：约 2 MiB

同一个 `cat <N MiB>`、同一个 curl 客户端：

| 文件大小 | 送达 | 结果 |
|---|---|---|
| 2 MiB | 2,798,761 B | 完整，EndEvent ✓ |
| 3 MiB | 3,585,915 B | `resource_exhausted` ✗ |
| 4 MiB | 2,361,501 B | ✗ |
| 8 MiB | 3,585,915 B | ✗ |
| 32 MiB | 1,924,211 B | ✗ |

也就是 **≥ 3 MiB 的突发输出基本必被截断**，能收到多少取决于生产/消费的赛跑。

## 3. 报错长什么样

cube-envd 会**显式**发一个 Connect EndStream 错误帧（这点做得很规范，问题在策略本身）：

```json
{"error":{"code":"resource_exhausted","message":"output consumer too slow: response queue full"}}
{"error":{"code":"resource_exhausted","message":"output consumer too slow: 64 events dropped"}}
```

SDK 会把它转成异常抛给用户（实测 PTY 路径抛 `RuntimeError`），`commands.run` 同样。
**注意它没有 EndEvent**，所以拿不到退出码——调用方只能得到一个错误。

## 4. 根因（代码位置）

* `cube-envd/src/protocol/stream.rs:17` — `RESPONSE_QUEUE_CAPACITY: usize = 65`
* `cube-envd/src/protocol/stream.rs:92-112` — `try_send_data_frame()`：

```rust
if tx.capacity() <= 1 {
    try_send_terminal_frame(output, protocol::end_stream_error(
        ConnectError::new(ConnectCode::ResourceExhausted,
            "output consumer too slow: response queue full")));
    return false;
}
```

* `cube-envd/src/process/pump.rs:41,70,131` — 数据帧都走 `try_send_data_frame`
**丢数据其实有两个独立关卡**，只修第二个不够：

1. **fan-out 广播环**：`cube-envd/src/process/engine/spawn.rs:285` 与
   `process/engine/pty.rs:176` 都是 `broadcast::channel::<PumpEvent>(64)`。
   管道读取任务往里发事件，某个订阅者跟不上就得到 `RecvError::Lagged(n)`，
   `process/pump.rs:104-113` 直接以 `resource_exhausted` 结束该连接——
   **这时事件已经被环覆盖，无法补救**。实测消息：`output consumer too slow: 64 events dropped`、
   `1 events dropped`、`322 events dropped`。
2. **每条连接的响应队列**：`mpsc::channel(65)`，见 `protocol/stream.rs:17` 与
   `try_send_data_frame()`。实测消息：`response queue full`。

两级各 64/65 个槽位，乘上读块大小只有几百 KB，所以约 2 MiB 的突发就会踩到。

* `cube-envd/src/process/command.rs:246-250` — 设计说明：

> Frames channel: the HTTP body reads from `rx`. **The driver never waits for
> capacity here: a slow client must not prevent deadline handling or process
> reaping.** Keep one slot reserved for an EndStream frame.

设计意图是"慢客户端不能拖住驱动循环（deadline/回收）"。代价是：**为了不阻塞，直接丢数据并掐断整条流。**
Go envd 的选择相反：HTTP 写变慢 → 子进程的管道写阻塞 → 子进程变慢，但**输出零丢失**（实测 1 ms/帧的慢消费者下，Go 把 `yes` 从 300 MB/2 s 拖到 22.7 MB/4.1 s，最后仍给 EndEvent）。

## 5. 这不是本次 PR 引入的

```
$ git log --oneline -S 'output consumer too slow' -- cube-envd/src
b9695b59 fix(cube-envd): harden PTY stream lifecycle and input
e0b00b10 feat(cube-envd): fan out process output and parameterize keepalive   ← 引入
$ git merge-base --is-ancestor e0b00b10 407adc69   # PR 基线
是
```

`e0b00b10` 是 #25/#27 基线的祖先，所以这是 **cube-envd 已有的行为**，与 fork-free spawn / one-spawn-path 无关。本次实测只是第一次把它放到真实用户路径上量出来。

## 6. 用户会怎么撞上（都很日常）

* 终端里 `cat` 一个大文件 / `tail -n 100000` / `grep -r` 大量命中 → **会话报错**
* agent 场景打印大 diff、完整构建日志、`npm install` / `pip install` 输出 → **输出被截断并抛异常**
* `python -c "print(json.dumps(big))"`、打印大 JSON/大表 → 同上
* 反过来，AI 逐 token 的慢速流式输出**没有问题**（见下）

## 7. 顺便确认：慢速流式与终端手感没问题

同一批探针里，这些维度 cube-envd 与 Go 一致或更好，**不是**差异来源：

| 指标 | stock | pr27 |
|---|---|---|
| 逐 token 到达间隔（20 ms 节奏） | 中位 23.99 ms，最大 29.15 ms | 中位 23.99 ms，最大 24.29 ms |
| 逐 token 到达间隔（50 ms 节奏） | 中位 51.97 ms，最大 52.27 ms | 中位 51.98 ms，最大 52.21 ms |
| 按键回显延迟（20 次） | 中位 2.10 ms，最大 7.01 ms | 中位 2.31 ms，最大 2.89 ms |
| `commands.run("echo hi")` 10 次 | 中位 9.43 ms | 中位 9.45 ms |
| 命令首字节（`/bin/true` 10 次） | 中位 10.88 ms | 中位 10.85 ms |
| PTY 会话建立 | 中位 2.81 ms | 中位 2.03 ms |
| 30 帧 TUI（2 KiB/帧，100 ms 节奏） | 间隔 ~104 ms | 间隔 ~104 ms |

也就是说：**"AI 逐字输出的速度"和"终端按键跟不跟手"两者都没问题**（都在毫秒级、两边一致）；
真正会被用户察觉的是**大突发输出被掐断**这一条。

## 8. Go 的慢订阅者语义（实测，决定"该抄什么、不能抄什么"）

看源码就清楚，Go envd 的选择是**等待（阻塞所有）**，不是丢弃：

```go
// internal/services/process/handler/multiplex.go:21-30
for v := range c.Source {
    c.mu.RLock()
    for _, cons := range c.channels {
        cons <- v          // Fork() 给的是无缓冲 channel，阻塞式发送
    }
    c.mu.RUnlock()
}
```

`Fork()` 返回 `make(chan T)`（无缓冲），所以最慢的订阅者会拖住 fan-out goroutine，
进而停止排空 `Source`，再进而让管道读取停止 → 子进程 `write()` 阻塞。**谁都跑不掉。**

真机验证（stock 模板，同一个进程挂两个订阅者）：

| 观测 | 结果 |
|---|---|
| A（慢，8 KiB/150 ms ≈ 43 KiB/s） | 4 s 收到 174,448 B，符合预期 |
| B（快） | 首秒收到 8,089,308 B（socket/nginx 缓冲），**之后 3 s 收到 0 B** |
| 子进程产出 | 4 s 合计约 8.2 MB → 缓冲排空后被限速到 A 的速度 |

也就是说：**Go 用"拖慢所有人（含子进程）"换"零丢失"。**

### 但这套设计有一个致命边角：慢订阅者断开会让进程永久卡死

`multiplex.go:23-29` 在**持有 `mu.RLock()` 的情况下做阻塞发送**，而订阅者取消走
`remove()` → `mu.Lock()`（写锁）。两者互等，`remove` 永远执行不了；此时消费者已经不在读了，
`cons <- v` 永远阻塞 → 管道读取停止 → 子进程卡在 `pipe_write`。

实测（`yes` 不带 timeout，排除"进程已退出"这一解释）：

```
/proc/30 comm=yes state=S wchan=pipe_write     ← 子进程还活着，阻塞在写管道
慢订阅者断开后其字节计数冻结在 87,364
同一 pid 重新 Connect → ReadTimeout 15~20 s（两次实验都是如此，一个字节都收不到）
沙箱里跑新命令仍然正常（守护进程没事，是"这个进程"的 fan-out 卡死了）
```

### 因此修法要"抄一半"

要抄的：**背压到子进程**（活的订阅者落后时，让管道读取停一停，子进程自然限速，**零丢失**）。
不能抄的：阻塞发送持锁 + 无缓冲 channel（会死锁），以及"对已断开的订阅者继续等待"。

正确形状是：

1. fan-out 时**先在锁内快照订阅者列表，再在锁外发送**，绝不跨阻塞持锁；
2. 每个订阅者带"已断开"信号，发送用 `select { case cons <- v: case <-gone: }`，
   断开的订阅者立刻摘除、永不阻塞别人；
3. 只有当**活着的**订阅者落后时才阻塞生产者（管道读取），从而限速子进程；
4. 给"活着但长期没有任何进展"的订阅者一个安全阀（例如 30 s 未排空任何字节就判为断开），
   避免它把进程永久钉住；
5. 全程不丢事件，流要么完整、要么带 EndEvent 结束。

### keepalive 能不能当"慢订阅者检测"用？不能

两边都有 keepalive，但**都不是慢订阅者检测器**：

* **Go**：`permissions.GetKeepAliveTicker` 读 `Keepalive-Ping-Interval` 头（秒），默认 **90 s**；
  在 `start.go:145` / `connect.go:58` 的 `select` 里每 tick 发一个 `KeepAlive`，只有
  `stream.Send` **报错**才 cancel。而 keepalive 的 `Send` 与数据帧的 `Send` 在**同一个
  select 循环**里——慢客户端把数据 `Send` 阻塞住时，循环根本回不去，tick 到了也发不出去。
  何况"慢"不是"连不上"，`Send` 最终会成功，所以既检测不到、也不会丢弃。
* **cube-envd**：同样的头，默认 **30 s**（注释说明 CubeProxy 前面的 LB 空闲超时未知，30 s 更安全）。
  tick 发 keepalive 走 `try_send_data_frame`（`pump.rs:130-137`），**确实会看队列**，
  队列满就 `resource_exhausted` 断流。但这是**瞬时队列压力**判定，不是"长时间慢"，
  而且实测那 2 MiB 截断来自**数据帧路径**（`pump.rs:70`，0.06 s 就触发），压根没等到 keepalive tick。

已断开（连不上）的客户端是另一条路径，两边都靠"写失败"发现：
cube-envd 用 `output.closed()` → `Delivery::Disconnected` → 立即摘除，进程不受影响（实测新命令正常）；
Go 靠 `stream.Send` 报错 → handler 返回 → `dataCancel()` → 然后撞上上面那个死锁。

| 情况 | Go | cube-envd（现状） |
|---|---|---|
| 客户端已断开 | 能发现，但清理死锁 → 该进程永久卡死 | 立即摘除，进程正常 ✓ |
| 客户端慢但活着 | 阻塞所有 + 背压子进程（零丢失），keepalive 无法介入 | 队列满即丢数据 + 断流（≥3 MiB 突发） |
| 活着但完全不动 | 永久阻塞（同上） | 立即断（无宽限期） |

"给一个时间窗、长期无进展才丢弃"这种中间策略，**两边目前都没有**——这正是下面修法里的安全阀。

---

## 9. 建议的修法

原注释担心的是"等队列会让驱动循环没法处理 deadline/回收"。但 deadline 与进程回收已经由
**独立的 supervisor 任务**负责（`command.rs:232-241` 把 supervisor 句柄交给了单独任务），
所以数据路径可以安全地改成背压，而不会拖住生命周期管理：

1. **fan-out 必须能背压到子进程**（关键的一步）。`broadcast` 天然不支持"等最慢的订阅者"，
   所以要么换成每订阅者一条有界通道、由管道读取任务 `await` 最慢的那条；
   要么在读取任务里检查各订阅者的积压并在超过水位时暂停读取。
   效果：管道被填满 → 子进程 `write()` 阻塞 → 自动限速，**与 Go 的行为一致，零丢失**。
   订阅者断开时沿用现有的 `Disconnected` 处理把它摘掉，不能让它拖住别人。
2. **每条连接的响应队列**改成 `tx.reserve().await`（保留专供 EndStream 的那个槽位），
   不再 `try_send` + 丢弃。
3. 队列容量按**字节预算**而不是固定 65 帧来定（65 × 读块 ≈ 0.5 MiB，远低于 HTTP 路径吞吐）。
4. 若确实要保护守护进程内存，安全阀也应该是"客户端长时间无进展"（例如 30 s 没有排空任何字节，
   或 socket 已关）才放弃，而**不是**"慢就丢事件"。
5. 无论怎么改，都应保证**流要么完整、要么带 EndEvent 结束**；现在调用方拿不到退出码。

代价与风险：`pump.rs`、`engine/{spawn,pty}.rs`、`protocol/stream.rs` 三个点要一起动，
属于独立于 #25/#27 的一个 PR；需要补上"cat ≥3 MiB / PTY 洪流"这类回归测试，
并在真机上重跑本目录的 `repro_bp.py` + 288 用例套件确认没有回归。

改完之后，`cat 50 MiB` 这类操作在 cube-envd 上应当和 Go 一样完整送达，最坏情况只是变慢。

## 10. 复现资产

* `repro_bp.py` — 三种消费者速度 × `yes`/`cat`/PTY 四类场景
* `repro_curl.py` — 用 curl（C 客户端）复测，排除客户端因素
* `threshold.py` — 1/2/3/4/6/8/16/32 MiB 的阈值扫描
* `probe_stream.py` / `probe_pty_lat.py` — 逐 token 与终端手感的对照
* 原始结果：`bp-{stock,pr27}.json`、`curl-{stock,pr27}.json`、`stream-*.json`、`pty-*.json`
