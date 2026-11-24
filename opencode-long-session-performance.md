# opencode 长会话卡顿治理研究

> 研究时间：2026-11-24。opencode 以 `dev` 分支当前实现为准；AlexCode 以 2026-11-24 00:45 的工作区为准（record 契约刚落地）。

## 0. 结论先行

opencode 不是把“单次渲染”做得更快，而是**把挂载集合缩到一个可见窗口**：

- 尾部 40 行起步，向上每次回填 60 行（`TRANSCRIPT_TAIL_ROWS` / `TRANSCRIPT_BACKFILL_CHUNK`）；
- 预算按“渲染条目”计权，折叠组只算 1；
- 服务端每页 20 条消息，游标向上翻；
- 前插（prepend）串行化，布局完成后按锚点复位滚动；
- 事件只改“最后一个 part”，150ms 合并一次突发更新。

AlexCode 目前缺的正好是这三层：**挂载预算、历史分页、共享渲染上下文**。record 契约（`view_state.record` + `HISTORY_KINDS` + 一致性测试）已经在工作区落地，所以现在动窗口化不会再出现“重建后少东西”的问题。

---

## 1. opencode 卡在哪（根因）

### 1.1 渲染层

- OpenTUI（Zig）的 `calculateVirtualLinesGeneric` 在每次 token 更新时，都从 rope 根节点走一遍整棵树，复杂度 O(n)。行缓存的 key 含 line number，导致每个 token 都让缓存失效，滚回时命中的是一个空的 line map。
- `viewportCulling` 只跳过**绘制**，Solid 组件和 Yoga 布局节点仍然全部挂载。这一点和 Textual 的 `display:none` 完全同类：省了 paint，没省 layout，也没省内存。
- 修复方向不是“优化单行渲染”，而是让渲染遍历**视口感知**（按前缀高度做二分查找），并在组件层减少挂载数量。

### 1.2 数据与内存层

- 显示 store 曾经硬编码 100 条消息（后来改成配置项），但 `store.part[messageID]` 从不随消息被 shift 而删除，内存持续上涨。
- 压缩只给 tool part 打 `state.time.compacted` 标记，不清 `state.output`；模型侧靠 message 转换时替换成 “Old tool result content cleared”。结果是上下文小了，**内存没小**。
- 撤销/重做在裁剪过的 store 里搜消息，找不到时静默失败。

### 1.3 一份“如果重来”的设计稿

旧 Go TUI 的 PR #3346 是目前最完整的方案，虽然随着 TUI 重写被关闭，但思路可以直接抄：

- 每个 block 缓存 `blockContents / blockHeights / blockPrefix`（前缀累计行号）；
- 用 `sort.Search` 二分出第一个可见 block，只合成可视行；
- `blockLineCache` 缓存按宽度切分的行，`blockIndexByPartID` 让流式只重算受影响的 block；
- header 按宽度缓存；
- shimmer/动画只在 in-flight 且“在底部、积压 ≤ 2000 行”时运行；
- 有选区时关掉 fast path，避免和原生选择打架；
- resize / 主题 / 会话切换时显式清缓存。

---

## 2. opencode 现在的实现（代码级）

### 2.1 尾部窗口 + 向上回填

`packages/tui/src/routes/session/index.tsx`：

```ts
const TRANSCRIPT_TAIL_ROWS = 40
const TRANSCRIPT_BACKFILL_CHUNK = 60

const [hiddenRows, setHiddenRows] = createSignal(...)
const [visibleRowsEnd, setVisibleRowsEnd] = createSignal(...)
const visibleRows = createMemo(() => rows().slice(hiddenRows(), visibleRowsEnd()))
```

- 会话打开时只把**最新的 40 行**放进组件树；
- 滚到顶时 `revealOlderRows()` 一次往前补 60 行；
- 渲染层只遍历 `visibleRows()`，所以组件数量与总会话长度脱钩；
- 行本身高度稳定（注释明确写 “Rows never grow or shrink, so a change to one row lays out only that row”），改一行只重排一行。

### 2.2 预算按“渲染条目”计权

`mount-budget.ts`：

```ts
function rowWeight(row, expanded) {
  if (!expanded) return 1          // 折叠起来的 tool group 只算 1
  return row.parts.length + 1      // 展开时才按真实条目算
}
```

`rowsBefore()` / `rowsAfter()` 按预算从锚点向两侧走到边界。**换成 AlexCode 的语言：简洁模式下整轮折叠卡权重 1，开发者模式按真实块数算**。只按“消息条数”限流会误判——一条消息可能带 20 个工具行。

### 2.3 prepend 串行 + 布局后锚定

`routes/session/history.ts` 的 `createHistoryPrepend`：

- `pending` 标志把并发的前插请求串行化，第二个请求直接失败重试，避免两次插入互相踩；
- 插入前用 `pinAbove()` 把“视口顶部还看得见的那一行”记成锚点；
- `afterLayout()` 里按锚点 `scrollBy` 补偿，抵消“上方内容变高”导致的跳动；
- 已经在底部（sticky bottom）时跳过锚定，直接继续跟随。

### 2.4 服务端分页

`packages/client/src/solid/data.ts`：

```ts
const messagePageLimit = 20
// 首次按 order:"desc" 取最新 20 条，再 toReversed() 正序展示
// 返回游标 next；more() / loadMore() 用 before 游标取上一页
```

- 分页结果按 id 去重后批量 publish；
- `beforePublish` 钩子先 `pinAbove()`，再从 store 写入新页——**先记锚点、再改数据**，这是不跳的关键顺序；
- `initialMessageLimit()` 允许首屏只读更小的“原始消息窗口”，后续页恢复 20 条；
- `completeGroupBoundary()` 保证一页不会把一个 tool group 从中间切开。

### 2.5 增量更新与合并

- `editAssistant / editTool / editText / editReasoning` 只改指定 message 的**最后一个同类型 part**，不做全量重建；
- `messageIndex` 维护 id → 位置的 O(1) 映射；
- `settleMs = 150`：同一 key 的突发事件合并成一次刷新；
- 找不到目标 message 的事件直接丢弃，不制造孤儿组件。

### 2.6 共享终端尺寸

`render-context.tsx` 的注释写得很直白：

> Transcript-row components must read this instead of calling `useTerminalDimensions()`, which registers one renderer resize listener **per mounted component** and grows with transcript length.

也就是：resize 监听器必须挂在会话级上下文上，**不能每个消息组件各注册一个**。AlexCode 的 `MarkdownStatic` 现在正好是每块自己订阅主题、自己处理 resize。

### 2.7 重负载隔离

- `omitToolFileData`：入 store 前把 `data:` 内联附件（截图 base64 等）剥掉，避免几百 MB 进内存；
- 桌面版 v1.1.8 做了 markdown cache、diff 懒加载、分块消息加载、相邻会话预加载。

### 2.8 导航的“逃生口”

`ensureAllRows()` 会在跳转到未加载的消息时临时挂载全部行，并显示 “Loading session history…”；`NAVIGATION_SLACK_ID` 给滚动目标留出未加载区间的余量，避免“点得到但滚不过去”。

---

## 3. 他们踩过的坑（照抄会中招）

1. **只设固定条数上限**：100 条消息的硬上限让用户根本滚不到会话开头，最后还是要补分页。
2. **分页了但没接触发**：顶部不触发 `loadMore` 时，用户看到的是“只有一页”的假象。
3. **把 culling 当卸载**：跳过绘制不等于减少组件与布局节点。
4. **拿裁剪后的视图当事实源**：撤销/重做、搜索必须回服务端取，不能用窗口里的数据。
5. **只裁消息不裁 parts**：消息 shift 走了，part map 还留着。
6. **压缩只打标不降级**：`compacted` 标记不清 `output`，上下文降了、内存没降。

---

## 4. 对照 AlexCode（当前代码）

| 能力 | opencode | AlexCode 现状 | 差距 |
|---|---|---|---|
| 尾部窗口 | 40 行 + 60 回填 | `ChatLog._mount` 把每个块直接挂进 `#chat-body`（`alex/ui/panels_chat.py:220`） | 无预算，块数线性增长 |
| 重建 | 只渲染窗口 | `_render_history` 全量 `_build_history` + `mount_all`（`alex/ui/app_history.py:92`、`:611`） | 一次挂几百个控件 |
| 锚定 | `pinAbove()` + 布局后 `scrollBy` | `_restore_chat_scroll` 只恢复 `scroll_y`（`:175`） | 前插/高度变化会跳 |
| 分页 | 20 条/页 + 游标 | `_history_payload` 已去掉 400 截断（`alex/session/core.py:309`），但落盘仍截 400（`:143`），无游标 RPC | UI 一次拿全量，重启又只剩 400 |
| 渲染上下文 | 会话级共享尺寸 | `MarkdownStatic` 每块订阅 `theme_changed_signal`、每块 `on_resize`（`alex/ui/markdown.py:369-388`） | 监听器随历史长度增长 |
| 流式更新 | 只改最后一个 part + 150ms 合并 | `_flush_stream` 每 tick 整段 `Text(display + " ▍")` 重排（`alex/ui/app_chat_stream.py:374-386`） | 长回答每 0.15s 全量重排 |
| 重负载 | 剥离 base64 / diff 懒加载 | shell stdout 全文进 `shell_card`；图片已是文件路径（这点做对了） | 大输出仍进 history |
| 导航 | `ensureAllRows` 临时全挂 | `open_message_actions` 查 checkpoints；无分页 | 翻远后动作可能落空 |

已经有、不用重做的部分：

- `ChatViewState.record()` 白名单与去重（`alex/ui/view_state.py:98`）；
- daemon 事件自带 `record`（`alex/session/common.py:18`）；
- 压缩记录 `("compaction", ...)`（`alex/session/core.py:156`）；
- stats / changes / verifier_text 记录（`alex/session/reply.py:283`、`:676`）；
- 重建一致性测试 `tests/test_history_parity.py`。

---

## 5. 借鉴方案（AlexCode 版）

### 阶段 1：尾部挂载预算（1～1.5 天）

新增 `alex/ui/chat_window.py`，把“history 记录”映射为“行”，只暴露窗口内的行：

```python
TAIL_UNITS = 80          # 首屏：约 2～3 屏内容
BACKFILL_UNITS = 60      # 向上每次补一屏多一点
MAX_UNITS = 200          # 两段上限；超出后离屏段整块 remove

def unit_of(kind: str, payload: str) -> int:
    if kind == "compaction":
        return 1
    return 1 + len(payload) // 2000   # 长回复按体量加权，避免 30k 一边倒
```

- `_render_history` 只构建窗口内的记录；`_mount_history` 仍用 `mount_all`，一次只挂窗口；
- `ChatLog.on_scroll` 到顶 6 行内触发 `reveal_older()`，同时在窗口顶部放一行 **“↑ Load earlier · segment N · 30 messages”**（`design/history_segments_ui_review.html` 已有文案与交互），不靠滚轮事件“碰运气”；
- 前插串行：`_prepending` 标志 + 一次只处理一个请求；
- 锚定不能再用 `scroll_y`：记住“视口顶部第一个可见控件”的 `region.y - scroll_y`，`mount_all(before=...)` 后两次 `call_after_refresh` 复位；
- 新事件在窗口不处于尾部时只 `record`、不 `mount`，底部显示 `+N new` / `Jump to latest`；
- 权限卡 / 选择题一律强制跳回最新再挂（不能藏在历史里）。

### 阶段 2：历史分页（2～3 天）

- session 增加：

```python
async def history_page(self, before: int | None = None, limit: int = 20) -> dict:
    # 返回 {"items": [...], "next": int | None, "has_more": bool}
```

`server.py` 用 `getattr(session, method)` 分发，协议不用改。

- 首次只取最新 20 条（对齐 opencode），`view.history` 增加 `base_index`，所有记录使用绝对下标；
- 落盘改为按段：`data/sessions/<id>/history/seg-0001.jsonl` + `index.json`；重启只加载当前段和索引；
- `undo`、消息动作、搜索一律走 RPC 取权威数据，不读窗口；
- 页边界按“轮”对齐，绝不从一轮中间切（opencode 的 `completeGroupBoundary`）。

### 阶段 3：渲染上下文与缓存（1～2 天）

- 新增会话级 `RenderContext`：`render_width`、`theme_id`、`render_epoch`；`ChatLog` 统一广播，`MarkdownStatic` 不再各自订阅 `theme_changed_signal`、不再自己处理 `on_resize`；
- `MarkdownStatic` 渲染结果按 `(hash(source), width, theme_id)` 进 LRU；主题切换只换缓存 key，不重建全部；
- 流式拆成“已冻结块 + 尾部可变块”：每约 500 字符把前缀提交成固定块，只有尾块随 delta 重排；保留现有 5000 字符截断；
- 角色行、Rules 行、header 按宽度缓存，只在宽度变化时重算；
- spinner / shimmer 只在 in-flight 且贴底时跑，用户上翻立即停（`follow_bottom` 已有停止逻辑，复用）。

### 阶段 4：重负载与压缩降级（约 1 天）

- `shell_card` 只存 `line_count + 首尾各 30 行`，全文落 `data/shell/<request>.txt`，展开时按需读；
- `changes` / `tool_diff` 只存 `exchange_id + 路径 + 增删行数`（现在已经是索引，保持）；
- 压缩时把旧段的大字段降级为引用（opencode 恰好没做，这是它的泄漏点）。

---

## 6. 验收指标

| 指标 | 目标 |
|---|---|
| `len(#chat-body.children)`（1000 轮后） | ≤ 120 |
| `_render_history` p95 | < 300 ms |
| 冷启动首屏 | < 500 ms |
| 切换主题 / 拖拽 resize | < 500 ms，无长帧 |
| 滚动单帧 | 无 > 50 ms 的长任务 |
| 历史内存 | < 20 MB，无 part 泄漏 |
| 一致性 | `test_history_parity` 继续通过；窗口内与全量重建同范围文本一致 |

---

## 7. 不建议照搬

- OpenTUI 的 Zig 渲染器细节（AlexCode 是 Textual，机制不同）；
- 任何“固定消息条数上限”单独使用；
- 只做 `viewportCulling` / `display:none` 当作优化；
- 把 diff 正文、stdout 全文、base64 塞进 UI store；
- 用裁剪后的窗口当撤销/搜索的事实源。

---

## 8. 参考资料

- Issue #6172（O(n) rope、内存未回收、PR #3346 链接）：https://github.com/anomalyco/opencode/issues/6172
- PR #3346（block 缓存 + 二分 + shimmer gating）：https://github.com/anomalyco/opencode/pull/3346
- Issue #6548（分页/虚拟化方案，viewportCulling 仍挂载全部组件）：https://github.com/anomalyco/opencode/issues/6548
- 当前 TUI 会话路由（40/60 行预算）：https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/tui/src/routes/session/index.tsx
- 行预算权重：https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/tui/src/routes/session/mount-budget.ts
- prepend 串行与锚定：https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/tui/src/routes/session/history.ts
- 客户端分页（20 条/页、loadMore、omitToolFileData、150ms 合并）：https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/client/src/solid/data.ts
- 共享终端尺寸：https://raw.githubusercontent.com/anomalyco/opencode/dev/packages/tui/src/routes/session/render-context.tsx
- 桌面版 v1.1.8（markdown cache / lazy diff / 分块加载）：https://opencode.runman.ai/en/changelog/v1.1-56.html

AlexCode 相关文件：

- `alex/ui/panels_chat.py`（ChatLog、贴底跟随）
- `alex/ui/app_history.py`（`_render_history`、`_mount_history`、锚点恢复）
- `alex/ui/view_state.py`（record 契约、白名单、去重）
- `alex/ui/markdown.py`（MarkdownStatic 渲染与 resize/主题订阅）
- `alex/ui/app_chat_stream.py`（流式刷新、压缩块、stats 还原）
- `alex/session/core.py`（显示历史、压缩记录、payload 与落盘）
- `alex/session/reply.py`（stats / changes 记录）
- `termui/widgets/scroll_list.py`（Textual ScrollView + `render_line` 的现成虚拟化样例）
- `tests/test_history_parity.py`（重建一致性守门）
- `design/chat_window_cap_ui_review.html`、`design/history_segments_ui_review.html`（已有 UI 评审稿）
