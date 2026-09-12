# 计划解读：输出背压（对照 `assets/output-stream-v7-overview.png`）

## 实现核对（2026-09-13，落地后补）

两个 commit 已落地（结构 `98e1b55d` / 策略 `2a254767`，PR #28），逐条核对本文：

**对上的部分**（引用原文，均已对照代码确认）：

* §1.1 「`publish_data` **会等待**（订阅者队列满 → 等数据槽 → 管道满 → 子进程 `write()` 阻塞），这就是"背压端到端传到底"」✅ 与 `io.rs` 两个泵的 `bus.publish_data(...).await` 及真机实测一致（慢消费者零丢失）。
* §1.2 「终态槽用 `OwnedPermit` **真正占住**：它不进 `capacity()` 口径，所以数据 `reserve()` 永远拿不到它」✅ `TerminalChannel::new` 常驻 permit，`bus.rs` 单测 `data_never_consumes_the_reserved_terminal_slot` 覆盖。
* §1.3 三个机制（Drop guard / 驱逐闩锁 / terminal 缓存句柄）✅ 全部落在 `Subscription` 上。
* §1.4 六分支 **biased** 顺序 `closed → deadline → 驱逐闩锁 → body reserve → events.recv → keepalive` ✅ 与 `pump.rs` 一致；「帧在等之前留在 `pending`，拿到 permit 才 `take()`」✅ 扁平 arm，注释里也写明了不可改成 helper 的原因。
* §1.4/§2 「`Lagged(n)` 分支与 `n events dropped` 报错消失」✅ `BusError::Lagged` 已删除。
* §2 「`timeout` 只套 `read`，`publish_data` 在 timeout 外」✅ `io.rs` 用 `stop` watch 与 `read` 竞争，发布不参与超时。
* §4.1「最慢订阅者 pacing 整条流」✅ 真机实测：慢消费者会把整条流压到它的读取速度（这正是零丢失的代价）。
* §4.2「300 s 内子进程可能停在 `pipe_write`」✅ 有界，且被驱逐闩锁解开。
* §6 两条队列的分工与"为什么不合并"✅ 与最终实现一致（`reserve_data` + 每连接 framing 留在 `drive_stream`）。

**要更正的两处**：

1. §1.6 把 v9 §2.4 的循环依赖诊断得**完全正确**（`stalled_since` 只在"已被驱逐"分支置位 → 永不驱逐 → 泵被永久钉住），但它给的处方「改成裸 `last_progress`：订阅时初始化、每次成功投递刷新，reaper 比 `now - last_progress`」**会误杀空闲订阅者**：一个 300 s 没有输出的进程，其健康订阅者同样"很久没刷新"，会被判定卡死。
   实际实现是**等待前置位、成功即清除**的 `stalled_since`（`bus.rs::publish_data` 在进入 `reserve` 前 `get_or_insert_with(Instant::now)`，发送成功即置 `None`；reaper 比 `now - stalled_since`）——既覆盖"从订阅起就没收到过东西"，又不会碰从没被等待过的订阅者。`eviction_does_not_touch_an_idle_subscriber` 就是钉这条的。
2. §1.7 与 §5⑨ 写的是「`cache → completion → publish_terminal`」。实现**更强**：`completion` 在**子进程被 reap 的那一刻**就发出（两个分支都发），不等输出排空——否则"客户端不读 → 泵被背压 → 终结事件与回收一起被拖住"。真机/单测 `unread_full_response_does_not_block_deadline_or_reaping` 覆盖。

**补充一条原文没提到的实现细节**：驱逐闩锁是 `watch`，当**总线本身**被 drop 时 `changed()` 返回 `Err`。若不区分，会把"总线没了"误判成"被驱逐"，抢在 `events.recv()` 排空已入队的 `End` 之前发出错误帧——`pump.rs` 因此用 `eviction_latch_closed` 把 `Err` 交给 `recv` 排空后按 `Closed` 处理（`closed_output_bus_returns_explicit_error_frame` 钉这条）。

**过期项**：文首"实现尚未落地、改后行号会变"已不成立，正文行号仍是落地前的。资产文件名保留 `v7`（内容已是 v8/v9 结构）。

---

> 2026-09-12 · 对照 `output-stream-backpressure-plan-v9.md` 与当前代码（`cube-envd/src/process/**`、`src/protocol/stream.rs`）
> 读法：先看图 1 从左到右的每条边，再看"这个组件原来长什么样、改成什么样、为什么"。
> 代码行号以当前工作树为准（实现尚未落地，改后行号会变）。

## 0. 图可信吗

可信。图 1 与 v9 §图示图 1 的 mermaid 源一致，逐条边都对得上现状代码：

| 图上的说法 | 代码证据 |
|---|---|
| 管道泵 `READ_CHUNK` 32 KiB | `engine/io.rs:14` |
| 无订阅者：丢弃但排空，不卡子进程 | `io.rs:156`、`io.rs:217`（`receiver_count() == 0 → continue`，继续读） |
| 订阅者队列 `TerminalChannel(PumpEvent)`，24 槽 = 23 数据 + 1 终态 | 现状是 `broadcast::channel(64)`（`spawn.rs:285`、`pty.rs:176`）→ 计划改为 24 槽 |
| `drive_stream` select 顺序 closed → deadline → 驱逐闩锁 → 等数据槽 → recv → keepalive | 现状是 `next_delivery` 四分支非 biased（`stream.rs:73-86`、`pump.rs:57-64`）→ 计划改 §2.2 |
| body 队列 8 槽 = 7 数据 + 1 终态 | 现状 `mpsc::channel(65)`（`stream.rs:17`）→ 计划改为 8 槽 + 常驻 permit |
| supervisor 独立任务，不被背压影响 | `supervisor.rs:18`（独立 `tokio::spawn`，`command.rs:229`） |
| reaper 300 s | 新增组件，现状不存在 |
| Drop guard 断开立刻摘除 | 新增，现状 `drop(receiver)` 发送端无感知 |
| supervisor → `publish_control`（仅提前提示） | 现状 `supervisor.rs:66` 的 `sender.send(DeadlineExceeded)` |

唯一的"不一致"：资产文件名仍是 `v7`，内容已经是 v8/v9 结构（计划自己在图示一节也注明了）。另外阈值 300 s 是**计划参数**，不是现状。

---

## 1. 从左到右逐段解读

### 1.1 子进程 + 管道泵（图左下）

**原来**：`pump_pipe` / `pump_pty`（`io.rs:200`、`io.rs:137`）循环 `read` 32 KiB → base64 编码成 `DataEvent` → `broadcast::Sender::send`。
- `broadcast` 的 `send` **从不等待**：环满时最老的消息被顶掉，慢订阅者下一次 `recv` 拿到 `RecvError::Lagged(n)`。
- 无订阅者时 `receiver_count() == 0` → 跳过编码但仍 `continue` 读，保证子进程不被管道憋死。

**改成**：同一个循环位置，发送变为 `bus.publish_data(event).await`。
- `publish_data` **会等待**（订阅者队列满 → 等数据槽 → 管道满 → 子进程 `write()` 阻塞），这就是"背压端到端传到底"。
- "无订阅者丢弃但排空"保留（`subscriber_count() == 0`）。
- 等待可被**驱逐闩锁**打断（否则卡死订阅者会永久钉住子进程——这正是要修的）。

**为什么**：`send` 丢数据是 ≥3 MiB 截图断流的直接原因；要"零丢失"就必须让生产者停下来等。

### 1.2 订阅者队列（图中间，`TerminalChannel(PumpEvent)`）

**原来**：每个进程一条 `broadcast::channel::<PumpEvent>(64)`（`spawn.rs:285`）。
- 所有 attach 者共享一条环，各自有独立滞后游标；
- 慢订阅者的处理方式是**丢掉它落后的数据**，它自己收到 `Lagged` 后被断流；
- 容量 64 槽约 2.8 MiB/进程，且 `PumpEvent: Clone`（`io.rs:21-29` 注释明确说明是为 broadcast 扇出）。

**改成**：`OutputBus` + **每订阅者一条独立有界队列** `TerminalChannel<PumpEvent>`，24 槽 = 23 数据 + 1 终态（`OwnedPermit` 常驻）。
- 语义反转：从"共享环 + 丢旧数据"变成"独立队列 + 满了让生产者等"。
- 终态槽用 `OwnedPermit` **真正占住**：它不进 `capacity()` 口径，所以数据 `reserve()` 永远拿不到它，慢订阅者在队列全满时也能收到 `End`。

**为什么**：`Lagged` 丢数据是本议题要消灭的行为；而"队列满时终态仍必达"要求预留是**强制的**——`capacity() <= 1` 只能检查，一旦要等待就失效（v6 的结论，见 §1.5）。

### 1.3 Subscription（图上 `Subscription` 那条边）

**原来**：`broadcast::Receiver<PumpEvent>` 三个出处——`SpawnedProcess.initial`（`engine/mod.rs:42`）、`table.subscribe()` 返回（`table.rs:200-222`）、`ProcEntry.sender` 是 `Sender` clone（`engine/mod.rs:45`）。
- 断开 = `drop(receiver)`。**发送端无法感知**（`receiver_count()` 只是被动计数），订阅者列表里也不会被摘掉。

**改成**：`Subscription { bus: Weak<OutputBus>, id, rx: mpsc::Receiver<PumpEvent>, terminal, evicted_tx/rx }`，三个机制挂在它上面：
- **Drop guard**：`impl Drop` → `bus.remove(id)`，断开**立刻**摘除，不必等下一次 publish；
- **驱逐闩锁**：`watch::Receiver<bool>`，reaper 置 `true` 后，无论 pump 还是 `drive_stream` 在等什么都能醒；
- **terminal 缓存句柄**：`Arc<Mutex<Option<PumpEvent>>>`，晚 attach / 竞态时补发终态（延续 `table.rs:207-220` 的既有语义）。

**为什么**：现在"谁还挂着"只有环上的游标知道，没有可枚举、可移除、可打断的订阅者对象；背压与驱逐都需要它。

### 1.4 drive_stream（图右下，每连接）

**原来**（`pump.rs:29-137`）：`next_delivery` 四分支非 biased select（`closed` / `events.recv` / `keepalive` / `deadline`），数据一律走 `try_send_data_frame`（`stream.rs:92-105`）：
- `capacity() <= 1` → 直接给客户端发 `resource_exhausted: response queue full` 并结束；
- `try_send` 失败 → 丢弃 sender，结束。

**改成**：扁平 arm 的 **biased** 门控 select，六个分支（v9 §2.2）：

```
closed → deadline → 驱逐闩锁 → body reserve → events.recv → keepalive
```

- 数据路径：`body_q.reserve_data().await` **等到**有数据槽再发（不再断流）；帧在等之前留在 `pending`，拿到 permit 才 `take()`（v8 修的 cancel-safety 点）；
- `deadline` 排在 `recv` 之前 → 持续高速输出时定时器不会被饿死（这是 `biased` 必须重排的原因）；
- 驱逐闩锁分支 → 用 body 的终态槽发 `resource_exhausted: no progress`，`pending` 里那一帧随**显式报错**结束（不是静默截断）；
- 终态 `End`/`SpawnError` 走 `send_terminal`（常驻 permit），队列满也发得出。

**没变的**：`Start` 先发、keepalive 语义（`next_delivery` 里 keepalive 本来就只受"手里没帧"门控，`stream.rs:83`）、连接级 deadline 只结束该 attachment（killed_by 由带外判定）、`End+trailer` 合并成一帧（`stream.rs:53-59`）。
**消失的**：`Lagged(n)` 分支与 "`n events dropped`" 报错——不再有这种失败模式。

### 1.5 body 队列（图右侧，`TerminalChannel(Bytes)`）

**原来**：`mpsc::channel::<Bytes>(65)`（`stream.rs:17` `RESPONSE_QUEUE_CAPACITY`），靠 `try_send_data_frame` 里的 **`capacity() <= 1` 检查**给终态留一槽（`stream.rs:96`），终态用 `try_send_terminal_frame`（`stream.rs:117-124`）。

**改成**：`TerminalChannel<Bytes>`，8 槽 = 7 数据 + 1 常驻 `OwnedPermit`；`reserve_data()` 拿数据槽、`send_terminal()` 用 permit（`Mutex<Option<OwnedPermit>>` 以便 `Arc<Subscriber>` 下调用）。

**为什么**：`capacity() <= 1` 是**检查式**预留——现状能用只是因为它**从不等待**；一旦改为 `reserve().await`，被释放的槽可能恰好是终态槽，检查就失守。`OwnedPermit` 把槽**物理占住**，接收端仍是普通 `mpsc::Receiver<Bytes>`，`ReceiverStream` 直接可用。

### 1.6 reaper（图左下，新增）

**原来**：没有。策略是"慢就断"（`capacity() <= 1` 立即结束连接）——把"活着但落后 2 MiB"的消费者误判为 stuck。

**改成**：每 bus 1 s tick，看每个订阅者的**进度时间戳**（`last_progress`：订阅时初始化，**每次成功投递一帧就刷新**）：
- 阈值内刷新过 → "慢但活着"，不动它；
- 超过 `EVICT_AFTER`(300 s) 没刷新过 → `evicted_tx.send(true)` + 从 `Vec` 移除。闩锁写入使之后任何 `borrow()`/`changed()` 都能看到（无丢唤醒）；挂在 `reserve()` 上的等待立即被打断。

**为什么**：区分"慢 ≠ 死"。300 s 对应的吞吐下限 ≈ 149 B/s（阈值内只要腾出一槽就不算卡死），远低于任何真实弱网带宽。

> ⚠️ v9 §2.4 的伪代码把触发条件写反了：`stalled_since` 只在"已被驱逐"分支置位，而驱逐又依赖它 → 循环依赖，真卡死的订阅者永远不会被驱逐（泵被永久钉住，正是 Go 的 wedge）。实现必须按上面"成功即刷新、reaper 比 `now - last_progress`"来写，并补一条"连着但不读"的卡死用例（只测"断开"盖不住这条路径——断开会由 Drop guard 摘除，不经过 reaper）。

### 1.7 supervisor（图左上，独立任务）

**原来**：已经是独立任务（`command.rs:229` spawn `supervise_process`），负责 deadline kill、`completion`、cgroup 清理。`:66` 发 `DeadlineExceeded`、`:47/:101` 发 `SpawnError`（都是 `broadcast::send`）。

**改成**：发布语义分档——
- `DeadlineExceeded` → `publish_control`（best-effort，满即丢；正确性完全带外：`EndEvent.killed_by == "timeout"` 由 `metadata::with_cause` + `decorate_terminal` 保证，`io.rs:42-83`）；
- `SpawnError` → `publish_terminal`（必达）；
- **`completion` 提前**到终结发布之前（现状是 `cache → tx.send(terminal) → completion_tx.send`，`spawn.rs:331-340`；改成 `cache → completion → publish_terminal`），这样进程表回收不被慢订阅者拖慢。

**为什么**：图里写的"不被背压影响"必须落到实处——kill 路径塞进任何 `await` 都会推迟 kill（验收 #9），终结发布塞进背压会推迟 `List` 回收（#10）。

### 1.8 出口链路（图最右）

`frame_stream_response` → hyper → CubeProxy → SDK/终端：**完全不变**（字节格式、`EndStream` 语义、`/files` 下载与文件系统 watch 都不在本次范围内）。

---

## 2. 原组件 → 新组件 一览

| 组件 | 原来 | 改成 | 为什么 |
|---|---|---|---|
| 进程输出总线 | `broadcast::channel(64)`（`spawn.rs:285`/`pty.rs:176`） | `OutputBus` + 每订阅者 `TerminalChannel<PumpEvent>`(23+1) | 扇出从"丢旧数据"改为"背压等一等" |
| 订阅句柄 | `broadcast::Receiver` | `Subscription`（Drop guard + 驱逐闩锁 + terminal 缓存） | 需要可枚举、可摘除、可打断 |
| 连接驱动器 | `next_delivery` 非 biased + `try_send_data_frame`(满即断) | 扁平 arm 的 biased 门控 select + `reserve().await` | 满不再断流；定时器不被饿死 |
| body 队列 | `mpsc(65)` + `capacity()<=1` 检查式预留 | `TerminalChannel<Bytes>`(7+1) + `OwnedPermit` 强制预留 | 等待语义下检查式预留失效 |
| 卡死处理 | 无（慢即断） | reaper：`stalled_since` > 300 s，闩锁驱逐 | 慢 ≠ 死；只踢卡死的那一个 |
| 终结事件 | `broadcast::send`（可能因 `Lagged` 丢） | `publish_terminal`（常驻 permit，必达） | 不能丢退出码 / EndEvent |
| 控制事件 | `broadcast::send` 与数据同路 | `publish_control`（best-effort） | 不该让提示事件拖住 kill |
| 收尾 | `timeout(GRACE, whole_output)` 可能 cancel 发布（`spawn.rs:326`） | `timeout` 只套 `read`，`publish_data` 在 timeout 外 | 不丢管道内未读数据 |
| 回收时序 | 终结发布 → `completion` | `completion` → 终结发布 | `List` 不被订阅者拖慢 |
| attach 上限 | 无 | per-process 8 + 全局 64 | 内存上界（验收 #7） |

## 3. 有意"不变"的东西

- 线上字节格式与协议语义；`Start` / keepalive / `End+trailer` 的帧形态。
- 每连接 body 由 hyper **拉取**（pull-based）——正因如此背压必须锚在订阅者队列，而不是 body 写侧。
- 连接级 deadline 只结束该 attachment，不杀进程；进程 deadline 的 kill 由 supervisor 负责。
- 无订阅者时"丢弃但排空"（子进程永不因无人看而阻塞）。
- 不做**无安全阀的永久阻塞**（Go 现状，`multiplex.go` 无缓冲 `Fork()` 的代价）。

## 4. 图上两处要记住的代价

1. **最慢订阅者 pacing 整条流**：有界缓冲 + 零丢失 + 单管道下的固有结果——一个停读者会拖慢所有订阅者与子进程，其窗口内其他订阅者只有各自 ~1 MiB 缓冲。Go 同样如此且更糟（无安全阀）。阈值（300 s）后只驱逐该订阅者，流动恢复。
2. **300 s 内子进程可能停在 `pipe_write`**：这是"零丢失"的代价，有界、可被驱逐解开；验收 #5 要求的是"驱逐后不停在 `pipe_write`"。

---

## 5. 走一遍完整流程（一个例子）

**场景**：沙箱里启动刷屏进程 `Start({ cmd: "/bin/yes", tag: "flood" })` → 子进程 pid 4242。
两个消费者：**A** = SDK，读 `Start` 的响应流（很快）；**B** = 终端另外 `Connect(tag="flood")`，读了几个字后暂停（客户端不读了）。

### ① 进程启动的一刻

envd 先建总线：`OutputBus::new()` 创建**第一个订阅者队列 sub#1**（24 槽 = 23 数据 + 1 终态），交给 A 作为 `initial`，**然后**才 spawn 管道泵任务。
顺序的意义：先有人接、再开水龙头，开头几个 chunk 不会没人接。

### ② 泵开始干活

管道泵循环：`read(32 KiB)` → base64 编码成 `Data{stdout:"..."}` → **`publish_data(帧)`**。
`publish_data` 遍历订阅者表，逐个投递：此刻只有 sub#1，`sub#1.reserve_data()` 立刻拿到一个**数据槽**的 permit → 帧入队。

### ③ A 把数据取走

`drive_stream#1`（每连接一个任务）在 select 里 `events.recv()` 拿到帧 → 编码成线上帧 → 放进**body 队列**（这条连接自己的 7 数据 + 1 终态槽）→ hyper 拉走写进 HTTP body → A 看到 `y`。

整条链：

```
子进程 write → 内核管道 → 管道泵 → 订阅者队列 sub#1 → drive_stream#1 → body 队列 → hyper → A
```

### ④ B 中途接进来

`Connect(tag="flood")` → `table.subscribe()` 在订阅者表里新增 **sub#2**（新队列、新进度时间戳、新驱逐闩锁），返回 `Subscription` 句柄 → spawn `drive_stream#2` → 返回响应体。
从此泵每帧投两份（sub#1 + sub#2）。B 只能看到订阅之后的数据——队列是空的，不重放历史。

### ⑤ B 暂停读取：这就是"背压"

B 不读 → `drive_stream#2` 把 body 队列 7 槽填满 → 卡在 `body_q.reserve_data()` → 不再 `events.recv()` → **sub#2 涨到 23 槽满** → 泵下一次 `publish_data` 轮到 sub#2 时拿不到槽，**挂起** → 泵不再读管道 → 内核管道缓冲（64 KiB）填满 → `yes` 的 `write()` 阻塞/变慢。

结果：**零丢失**，子进程被自动限速。A 会感觉到暂停：泵是逐个订阅者投的，挂在 sub#2 上时整轮都停，A 吃完自己攒的缓冲后也暂时没新数据——"最慢者给整条流定速"，是固有权衡（§4.1）。

### ⑥ B 恢复读取

sub#2 腾出槽 → 泵的等待返回、帧入队 → 恢复流动。中间的数据一条没少。

### ⑦ 如果 B 是"真卡死"（连接还在但永远不读）

进度时间戳停在很久以前 → **reaper**（每总线每秒扫一次）判定超阈值：
1. 置 B 的**驱逐闩锁**（`watch` 置 true）；
2. 把 sub#2 从订阅者表摘掉。

闩锁一响：泵里挂在 sub#2 上的等待**立刻被打断**并跳过它；`drive_stream#2` 也被唤醒，用 body 的**终态槽**给 B 发 `resource_exhausted: no progress for 300s`，结束它的流。A 与子进程立即恢复。

### ⑧ 如果 B 直接断开

B 的 `Subscription` 被 drop → Drop guard **立刻**摘除 sub#2（不等下一次投递）；`drive_stream#2` 的 `body_tx.closed()` 也让任务退出。

### ⑨ `yes` 被杀 / 自己退出

泵读到 EOF → 合成 `PumpEvent::End{exit_status}`，`decorate_terminal` 把 `killed_by`（timeout / oom）填上 → 三步：
1. 写**终态缓存**（给"进程已死才来 Connect"的连接兜底）；
2. 通知 **completion** → supervisor 立刻 `remove_process`，`List` 不再报 pid 4242；
3. **`publish_terminal(End)`**。

第 3 步是"必达"：每个订阅者队列有**常驻终态 permit** 物理占住第 24 槽，所以哪怕 23 个数据槽全满、没人腾槽，`End` 也塞得进去、永不阻塞。
各 `drive_stream` 收到 End → 编码 `End + EndStream trailer`（OK 还是 `deadline_exceeded` 由 `killed_by` 决定）→ 用自己 body 的终态槽送出 → 两条流干净结束。

### ⑩ deadline 的情况

进程 deadline 到 → supervisor 先 `publish_control(DeadlineExceeded)`（best-effort，满了就丢，只是"提前提示"）→ 再 `with_cause("timeout")` + kill。
正确性不靠这条提示，靠 ⑨ 里 End 上的 `killed_by`：提示丢了也不会误报成正常结束。

### 组件职责速查

| 组件 | 一句话职责 |
|---|---|
| 管道泵 | 唯一生产者：读 32 KiB → 编码 → `publish_data`；没人订阅时丢弃但继续读（不憋死子进程） |
| `OutputBus` | 订阅者注册表 + 三档发布入口：数据（可等）/ 终结（必达）/ 控制（best-effort） |
| 订阅者队列 `TerminalChannel` | 每订阅者一条 mpsc：23 数据槽 + 1 常驻 permit 的终态槽；`reserve_data()` 等数据槽，`send_terminal()` 必达 |
| `Subscriber` | 总线侧持有：队列 + 进度时间戳 + 驱逐闩锁写端 |
| `Subscription` | `drive_stream` 持有：队列接收端 + 摘除句柄（Drop）+ 闩锁接收端 + 终态缓存句柄 |
| `drive_stream` | 每连接一个的交付状态机：编码、`pending` 暂存、六分支 select（断开 / deadline / 驱逐 / 等 body 槽 / 取事件 / keepalive） |
| body 队列 | 每连接一个，7 数据 + 1 终态；hyper 从这拉 |
| reaper | 每秒扫进度时间戳；"长时间零进展"才置闩锁并摘除——只踢卡死的那个 |
| supervisor | deadline kill、completion → 回收进程表项、cgroup 清理；不被背压拖慢 |
| `frame_stream_response` → hyper → CubeProxy | 出口，本次完全没动 |

三条不变量贯穿全例：**帧要么进队列、要么在 `pending`、要么随显式错误结束**（绝不静默丢）；**终态永远有位**（permit）；**任何等待都能被闩锁打破**（慢 ≠ 死）。

---

## 6. 为什么每个连接有两条队列？能不能只留一条？

**两条是**：

| 队列 | 装什么 | 容量 | 谁生产 | 谁消费 |
|---|---|---|---|---|
| 订阅者队列 | **进程事件** `PumpEvent`（Data / End / SpawnError / DeadlineExceeded） | 23 数据 + 1 终态 | 管道泵（`publish_data`/`publish_terminal`）、supervisor（`publish_control`） | `drive_stream` |
| body 队列 | **已编码的线上帧** `Bytes`（Start / 数据 / keepalive / `End+trailer` 合并帧） | 7 数据 + 1 终态 | 该连接的 `drive_stream` | hyper |

**能只留一条吗**：能，这就是计划里被否掉的备选 (A)——让订阅者队列直接当 HTTP body，把 framing（Start / keepalive / deadline / trailer）写进一个手写 `Stream`，由 hyper 直接 poll 它。计划取 (B)，理由有四条：

1. **单位与职责不同**。一条是"进程事件扇出"（泵只管把事件投给每个订阅者，不知道 wire 格式）；一条是"HTTP 交付"（已编码 `Bytes`，由 hyper 拉）。合并后泵必须在扇出循环里**逐连接做 wire 编码**（JSON 信封、Start、trailer），进程总线就再也说不清自己"只懂进程事件"。
2. **每连接的状态机用 `select!` 最省事**。`closed → deadline → 驱逐闩锁 → 等槽 → recv → keepalive` 六个分支 + 优先级；写成 `Stream::poll_next` 就得手写两个定时器（keepalive、deadline）的 waker 与取消安全——正是计划要避开的复杂度。
3. **多一层缓冲把 socket 抖动挡在背压锚点之外**。body 队列 7 帧（≈306 KiB）+ `pending` 1 帧让 drive_stream 能提前编码一小段；否则 hyper 的拉取节奏**直接**成为订阅者队列的消费节奏，背压锚点跟着 socket 抖动。
4. **代价很小**。多一条队列 ≈0.3 MiB/连接 + 一次任务唤醒。合并省下的就是这点内存和一跳，却要吃回 (1)(2)(3)。

当前规模（per-process 8 / 全局 64）下不值得合并；若将来订阅者数量级上升、内存成为瓶颈，再评估 (A)。

> 注：`frame_stream_response` 这个 framing helper 是 `protocol::stream` 里的通用件，文件系统 watch 也在用（`filesystem/watch/mod.rs:63,339`）；但 body 队列本身（`response_channel` / `try_send_*`）目前只有 process 服务在用。
