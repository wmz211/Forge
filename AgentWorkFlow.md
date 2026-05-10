# Claude Code Agent 工作流程详解

> 基于 Claude Code v2.1.88 反编译源码分析
> 源码路径：`claude-code-source-code/src/`

---

## 目录

1. [总体架构](#总体架构)
2. [第一层：会话管理（QueryEngine）](#第一层会话管理queryengine)
3. [第二层：主循环（queryLoop）](#第二层主循环queryloop)
4. [上下文压缩四道防线](#上下文压缩四道防线)
5. [工具执行系统](#工具执行系统)
6. [权限系统](#权限系统)
7. [API调用层](#api调用层)
8. [文件索引速查表](#文件索引速查表)

---

## 总体架构

```
用户输入 (CLI / SDK)
        │
        ▼
  entrypoints/cli.tsx          ← Ink/React 渲染终端UI
        │
        ▼
  QueryEngine.ts               ← 会话管理（跨轮状态）
        │
        ▼
  query.ts → queryLoop()       ← 核心 Agent 循环
        │
   ┌────┴────────────────────┐
   ▼                         ▼
services/api/claude.ts    services/tools/
（调用 Claude API）        toolOrchestration.ts
                          （工具调度与执行）
```

**核心设计模式**：ReAct（Reasoning + Acting）
- Claude 输出文本 / 选择工具 → 执行工具 → 结果反馈 → 继续推理 → 循环直到无工具调用

---

## 第一层：会话管理（QueryEngine）

### 文件
`src/QueryEngine.ts`

### 核心类：`QueryEngine`

```typescript
export class QueryEngine {
  private config: QueryEngineConfig
  private mutableMessages: Message[]        // 消息历史（跨轮持久）
  private abortController: AbortController  // 取消控制
  private permissionDenials: SDKPermissionDenial[]  // 权限拒绝记录
  private totalUsage: NonNullableUsage      // 累计 token 用量
  private readFileState: FileStateCache     // 文件内容快照缓存
  private discoveredSkillNames = new Set<string>()   // 技能发现追踪
  private loadedNestedMemoryPaths = new Set<string>() // 已加载记忆路径

  constructor(config: QueryEngineConfig) { ... }

  async *submitMessage(
    prompt: string | ContentBlockParam[],
    options?: { uuid?: string; isMeta?: boolean },
  ): AsyncGenerator<SDKMessage, void, unknown> { ... }
}
```

### `QueryEngineConfig` 关键字段

```typescript
export type QueryEngineConfig = {
  cwd: string                    // 工作目录
  tools: Tools                   // 可用工具列表
  commands: Command[]            // 斜杠命令列表
  mcpClients: MCPServerConnection[]  // MCP 服务器连接
  agents: AgentDefinition[]      // Agent 定义
  canUseTool: CanUseToolFn       // 权限判断函数
  getAppState: () => AppState    // 读取全局状态
  setAppState: (f) => void       // 更新全局状态
  maxTurns?: number              // 最大循环轮次
  maxBudgetUsd?: number          // 最大花费预算
  taskBudget?: { total: number } // token 预算
  thinkingConfig?: ThinkingConfig // 思考模式配置
  customSystemPrompt?: string    // 自定义系统提示
}
```

### `submitMessage()` 执行流程

```
submitMessage(prompt)
    │
    ├─ 1. 重置 discoveredSkillNames
    ├─ 2. setCwd(cwd)                          // 设置工作目录
    ├─ 3. fetchSystemPromptParts()             // 构建系统提示
    │      └── src/utils/queryContext.ts
    ├─ 4. loadMemoryPrompt()                   // 加载记忆（如有）
    │      └── src/memdir/memdir.ts
    ├─ 5. 包装 canUseTool（追踪权限拒绝）
    ├─ 6. processUserInput()                   // 处理用户输入/斜杠命令
    │      └── src/utils/processUserInput/processUserInput.ts
    ├─ 7. 调用 query()                         // 进入主循环
    │      └── src/query.ts
    └─ 8. yield SDKMessage 流式返回给调用方
```
这段代码就像是一个“指挥官”。当你在终端输入一句话（prompt）后，它作为入口接收这句话，然后立刻拉出抽屉（this.config），把所有的武器（工具）、军费（预算限制）、地图（当前目录）和战术指导（系统提示词）都摆在桌面上，准备开始指挥 AI 帮你干活，并持续向你汇报战况（流式返回 SDKMessage）。


---

## 第二层：主循环（queryLoop）

### 文件
`src/query.ts`

### 入口函数

```typescript
// 对外暴露的入口，query() 包装了 queryLoop()
export async function* query(
  params: QueryParams,
): AsyncGenerator<StreamEvent | RequestStartEvent | Message | TombstoneMessage | ToolUseSummaryMessage, Terminal> {
  const consumedCommandUuids: string[] = []
  const terminal = yield* queryLoop(params, consumedCommandUuids)
  // 循环结束后通知命令生命周期完成
  for (const uuid of consumedCommandUuids) {
    notifyCommandLifecycle(uuid, 'completed')
  }
  return terminal
}
```


### 循环状态机（`State` 类型）

```typescript
type State = {
  messages: Message[]                        // 当前消息列表
  toolUseContext: ToolUseContext             // 工具调用上下文
  autoCompactTracking: AutoCompactTrackingState | undefined
  maxOutputTokensRecoveryCount: number       // 超出输出token的恢复计数
  hasAttemptedReactiveCompact: boolean
  maxOutputTokensOverride: number | undefined
  pendingToolUseSummary: Promise<ToolUseSummaryMessage | null> | undefined
  stopHookActive: boolean | undefined
  turnCount: number                          // 当前轮次计数
  transition: Continue | undefined           // 上次迭代的继续原因
}
```
接收任务 (params)： 之前那个 submitMessage 函数负责接收你输入的原始 prompt，并结合各种配置，打包成一个完整的任务包（也就是这里的 params）。然后，它会调用这个 query 函数。

核心业务全部“外包” (yield* queryLoop)：query 函数自己完全不写“如何调用大模型、如何解析返回的 JSON、如何执行命令”这些脏活累活。它直接把循环流程甩给了底层的 queryLoop 去执行，并且通过 yield* 让 queryLoop 直接向外输出结果。

自己只负责“善后收尾”： query 函数留在这里的唯一目的，就是为了兜底。等 queryLoop 把活儿都干完、正常退出后，query 就负责把运行期间记录下来的那些命令 ID (consumedCommandUuids) 拿出来，挨个向系统发送“这些命令已彻底完结”的通知，做一个完美的收尾。

### 主循环结构（`queryLoop`）

```typescript
async function* queryLoop(params, consumedCommandUuids) {
  // 初始化状态
  let state: State = {
    messages: params.messages,
    toolUseContext: params.toolUseContext,
    turnCount: 1,
    // ...
  }

  // ★ 核心：无限循环，每次迭代 = 一次 LLM 调用 + 工具执行
  while (true) {
    let { toolUseContext } = state
    const { messages, ... } = state

    // ① 预取技能发现（并行，不阻塞）
    const pendingSkillPrefetch = skillPrefetch?.startSkillDiscoveryPrefetch(...)

    yield { type: 'stream_request_start' }

    // ② 上下文压缩（四道防线，见下节）
    let messagesForQuery = [...getMessagesAfterCompactBoundary(messages)]
    messagesForQuery = await applyToolResultBudget(...)   // 工具结果大小限制
    // snip → microcompact → contextCollapse → autocompact
    ...

    // ③ token 预检（超限直接退出）
    const { isAtBlockingLimit } = calculateTokenWarningState(...)
    if (isAtBlockingLimit) {
      yield createAssistantAPIErrorMessage({ content: PROMPT_TOO_LONG_ERROR_MESSAGE })
      return { reason: 'blocking_limit' }
    }

    // ④ 调用 Claude API（流式）
    for await (const message of deps.callModel({
      messages: prependUserContext(messagesForQuery, userContext),
      systemPrompt: fullSystemPrompt,
      tools: toolUseContext.options.tools,
      signal: toolUseContext.abortController.signal,
      options: { model: currentModel, ... },
    })) {
      // 收集 assistant 消息和 tool_use blocks
      if (message.type === 'assistant') {
        assistantMessages.push(message)
        for (const block of message.message.content) {
          if (block.type === 'tool_use') {
            toolUseBlocks.push(block)
            needsFollowUp = true
          }
        }
      }
      yield message  // 流式传给上层
    }

    // ⑤ 执行工具
    if (needsFollowUp) {
      for await (const { message, newContext } of runTools(
        toolUseBlocks, assistantMessages, canUseTool, toolUseContext,
      )) {
        if (message) {
          toolResults.push(message)
          yield message
        }
        toolUseContext = newContext
      }

      // 把工具结果追加到消息，继续下一轮
      state = {
        ...state,
        messages: [...messages, ...assistantMessages, ...toolResults],
        toolUseContext,
        turnCount: turnCount + 1,
        transition: { reason: 'tool_use' },
      }
      continue  // ← 回到 while(true) 顶部
    }

    // ⑥ 无工具调用 → 退出循环
    return { reason: 'stop' }  // Terminal
  }
}
```
可以把这个 while(true) 循环想象成你和 AI 之间的**“回合制游戏”**：

把你的问题发给 AI。

收到 Assistant 消息。

检查这个消息：

如果 AI 说：“我需要调用 cat file.txt 工具。”（触发工具调用）

代码在第 ⑤ 步帮你悄悄在后台跑完 cat 命令。

把读取到的文件内容作为一条新的消息（Tool Result），和之前的记录拼在一起（state = { ...messages, toolResults }）。

continue 触发，回到 while(true) 顶部，开启下一轮。AI 看到文件内容后，继续思考。

直到某一轮收到的 Assistant 消息全是纯文本，没有工具调用了，循环才会彻底结束（第 ⑥ 步）。


### 循环退出条件（`Terminal` 类型）

```typescript
// src/query/transitions.ts
type Terminal =
  | { reason: 'stop' }            // 正常停止（无工具调用）
  | { reason: 'max_turns' }       // 达到最大轮次
  | { reason: 'blocking_limit' }  // token 超出阻塞限制
  | { reason: 'abort' }           // 用户中断
  | { reason: 'budget_exceeded' } // 预算超出
```

---

## 上下文压缩四道防线

每次循环迭代开始时，按顺序执行以下压缩，防止上下文超出模型限制：

```
messagesForQuery
    │
    ▼ [第1道] applyToolResultBudget()
    │   src/utils/toolResultStorage.ts
    │   → 限制单条工具结果大小，超出部分写磁盘
    │
    ▼ [第2道] snipCompactIfNeeded()
    │   src/services/compact/snipCompact.ts
    │   → 删除中间旧消息，保留头尾
    │   → feature gate: HISTORY_SNIP
    │
    ▼ [第3道] microcompact()
    │   src/services/compact/compact.ts
    │   → 压缩单条过长的工具输出（截断+摘要）
    │   → 可选 CACHED_MICROCOMPACT（缓存版本）
    │
    ▼ [第4道] applyCollapsesIfNeeded()
    │   src/services/contextCollapse/index.ts  *(内部模块)*
    │   → 折叠旧轮次为摘要块
    │   → feature gate: CONTEXT_COLLAPSE
    │
    ▼ [第5道] autocompact（触发条件最重）
        src/services/compact/autoCompact.ts
        → 超过阈值时调用一次 Claude 对全部历史做摘要
        → 关键函数：
            isAutoCompactEnabled()
            calculateTokenWarningState(tokenCount, model)
        → 成功后调用 buildPostCompactMessages(compactionResult)
              src/services/compact/compact.ts
```

### autocompact 触发判断

```typescript
// src/services/compact/autoCompact.ts
const { isAtBlockingLimit, isAtWarningLimit } = calculateTokenWarningState(
  tokenCountWithEstimation(messagesForQuery) - snipTokensFreed,
  toolUseContext.options.mainLoopModel,
)
// isAtBlockingLimit → 直接阻塞，让用户手动 /compact
// isAtWarningLimit  → 触发自动压缩
```

---

## 工具执行系统

### 文件结构

```
src/services/tools/
├── toolOrchestration.ts   ← 工具调度（串行/并发分组）
├── toolExecution.ts       ← 单个工具执行（runToolUse）
└── StreamingToolExecutor.ts ← 流式工具执行器

src/tools/                 ← 40+ 具体工具实现
├── BashTool/
├── FileReadTool/
├── FileEditTool/
├── FileWriteTool/
├── GlobTool/
├── GrepTool/
├── AgentTool/             ← 子 Agent（递归调用 query）
├── WebSearchTool/
├── WebFetchTool/
├── TaskCreateTool/
├── SkillTool/
├── MCPTool/
├── EnterPlanModeTool/
├── TodoWriteTool/
└── ...（共40+个）
```

### 并发调度策略

**文件**：`src/services/tools/toolOrchestration.ts`

```typescript
export async function* runTools(
  toolUseMessages: ToolUseBlock[],
  assistantMessages: AssistantMessage[],
  canUseTool: CanUseToolFn,
  toolUseContext: ToolUseContext,
): AsyncGenerator<MessageUpdate, void> {
  let currentContext = toolUseContext

  // 按 isConcurrencySafe 分批
  for (const { isConcurrencySafe, blocks } of partitionToolCalls(
    toolUseMessages, currentContext,
  )) {
    if (isConcurrencySafe) {
      // 只读工具 → 并发执行（默认最多10个）
      for await (const update of runToolsConcurrently(blocks, ...)) { ... }
    } else {
      // 写入工具 → 串行执行
      for await (const update of runToolsSerially(blocks, ...)) { ... }
    }
  }
}
```

### 分组算法：`partitionToolCalls`

```typescript
function partitionToolCalls(toolUseMessages, toolUseContext): Batch[] {
  return toolUseMessages.reduce((acc, toolUse) => {
    const tool = findToolByName(toolUseContext.options.tools, toolUse.name)
    const parsedInput = tool?.inputSchema.safeParse(toolUse.input)
    const isConcurrencySafe = parsedInput?.success
      ? Boolean(tool?.isConcurrencySafe(parsedInput.data))
      : false

    // 连续的只读工具合并为一个并发批次
    if (isConcurrencySafe && acc[acc.length - 1]?.isConcurrencySafe) {
      acc[acc.length - 1]!.blocks.push(toolUse)
    } else {
      acc.push({ isConcurrencySafe, blocks: [toolUse] })
    }
    return acc
  }, [])
}
```
这段代码的分批结果会是这样：

Batch 1 (并发安全): [读 A, 读 B] 👉 这两个会被一起扔进线程池同时跑。

Batch 2 (不安全): [写 C] 👉 等 Batch 1 跑完，它才开始自己孤单地跑。

Batch 3 (并发安全): [读 D] 👉 等写 C 结束了，读 D 才能跑（万一读 D 需要写 C 的结果呢？所以必须等）。

Batch 4 (不安全): [Shell 命令] 👉 等前面的全跑完，最后跑这个。/


**并发数量限制**：
```typescript
function getMaxToolUseConcurrency(): number {
  return parseInt(process.env.CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY || '', 10) || 10
}
```

### 工具接口（`Tool.ts`）

```typescript
// src/Tool.ts
export type Tool = {
  name: string
  description: string
  inputSchema: ZodSchema           // 用 Zod 做输入校验
  isConcurrencySafe: (input) => boolean  // 是否可并发
  maxResultSizeChars?: number      // 结果大小限制
  backfillObservableInput?: (input) => void  // 补全可观测字段
  call: (input, context) => AsyncGenerator<ToolResult>  // 执行函数
}
```

---

## 权限系统

### 文件

| 文件 | 职责 |
|------|------|
| `src/hooks/useCanUseTool.tsx` | 权限判断主 hook |
| `src/types/permissions.ts` | 权限类型定义 |
| `src/utils/permissions/denialTracking.ts` | 拒绝记录追踪 |
| `src/hooks/toolPermission/` | 权限弹窗 UI 组件 |

### 权限模式

```typescript
// src/types/permissions.ts
export type PermissionMode =
  | 'auto'    // 自动允许所有工具
  | 'plan'    // 只允许只读工具，写入需确认
  | 'manual'  // 每次工具调用都需用户确认
```

### 权限判断流程

```typescript
// src/hooks/useCanUseTool.tsx
export type CanUseToolFn = (
  tool: Tool,
  input: unknown,
  toolUseContext: ToolUseContext,
  assistantMessage: AssistantMessage,
  toolUseID: string,
  forceDecision?: boolean,
) => Promise<PermissionResult>

// PermissionResult
export type PermissionResult =
  | { behavior: 'allow' }
  | { behavior: 'deny'; reason: string }
  | { behavior: 'ask'; prompt: string }  // 弹出确认框
```

### QueryEngine 中的权限包装

```typescript
// QueryEngine.ts：包装 canUseTool，自动记录所有拒绝
const wrappedCanUseTool: CanUseToolFn = async (tool, input, ...) => {
  const result = await canUseTool(tool, input, ...)
  if (result.behavior !== 'allow') {
    this.permissionDenials.push({
      tool_name: sdkCompatToolName(tool.name),
      tool_use_id: toolUseID,
      tool_input: input,
    })
  }
  return result
}
```

---

## API调用层

### 文件

| 文件 | 职责 |
|------|------|
| `src/services/api/claude.ts` | 调用 Anthropic API、流式处理、token 统计 |
| `src/services/api/withRetry.ts` | 重试逻辑（含 fallback 模型） |
| `src/services/api/errors.ts` | 错误分类（prompt-too-long 等） |
| `src/services/analytics/index.ts` | 遥测上报（双路：Anthropic + Datadog） |

### `callModel()` 调用示意

```typescript
// 在 queryLoop 中调用
for await (const message of deps.callModel({
  messages: prependUserContext(messagesForQuery, userContext),
  systemPrompt: fullSystemPrompt,
  thinkingConfig: toolUseContext.options.thinkingConfig,
  tools: toolUseContext.options.tools,
  signal: toolUseContext.abortController.signal,
  options: {
    model: currentModel,                  // 主模型（如 claude-sonnet-4-6）
    fastMode: appState.fastMode,          // 快速模式
    fallbackModel,                        // 降级模型
    querySource,                          // 调用来源标识
    effortValue: appState.effortValue,    // 思考力度（extended thinking）
    taskBudget: { total, remaining },     // token 预算
    maxOutputTokensOverride,
  },
})) { ... }
```

### 遥测事件示例

```typescript
// src/services/analytics/index.ts
logEvent('tengu_auto_compact_succeeded', {
  originalMessageCount: messages.length,
  preCompactTokenCount,
  postCompactTokenCount,
  compactionInputTokens,
  compactionOutputTokens,
  queryChainId,
  queryDepth,
})
```

> **注意**：`tengu` 是 Claude Code 的内部代号之一（动物代号系统：Capybara v8 → Tengu → Fennec/Opus 4.6 → Numbat 待发布）

---

## 文件索引速查表

| 模块 | 文件路径 | 关键函数/类 |
|------|---------|-----------|
| 会话管理 | `src/QueryEngine.ts` | `class QueryEngine`, `submitMessage()` |
| 主循环 | `src/query.ts` | `query()`, `queryLoop()`, `yieldMissingToolResultBlocks()` |
| 循环配置 | `src/query/config.ts` | `buildQueryConfig()` |
| Token 预算 | `src/query/tokenBudget.ts` | `createBudgetTracker()`, `checkTokenBudget()` |
| Stop Hook | `src/query/stopHooks.ts` | `handleStopHooks()` |
| 循环退出类型 | `src/query/transitions.ts` | `Terminal`, `Continue` |
| 依赖注入 | `src/query/deps.ts` | `productionDeps()`, `QueryDeps` |
| 工具类型定义 | `src/Tool.ts` | `Tool`, `ToolUseContext`, `findToolByName()` |
| 工具调度 | `src/services/tools/toolOrchestration.ts` | `runTools()`, `partitionToolCalls()` |
| 工具执行 | `src/services/tools/toolExecution.ts` | `runToolUse()` |
| 流式工具 | `src/services/tools/StreamingToolExecutor.ts` | `class StreamingToolExecutor` |
| API 调用 | `src/services/api/claude.ts` | `callModel()`, `accumulateUsage()` |
| 重试逻辑 | `src/services/api/withRetry.ts` | `withRetry()`, `FallbackTriggeredError` |
| 自动压缩 | `src/services/compact/autoCompact.ts` | `isAutoCompactEnabled()`, `calculateTokenWarningState()` |
| 压缩重建 | `src/services/compact/compact.ts` | `buildPostCompactMessages()` |
| Snip压缩 | `src/services/compact/snipCompact.ts` | `snipCompactIfNeeded()` |
| 工具结果限制 | `src/utils/toolResultStorage.ts` | `applyToolResultBudget()` |
| 消息工具函数 | `src/utils/messages.ts` | `createUserMessage()`, `normalizeMessagesForAPI()`, `getMessagesAfterCompactBoundary()` |
| 系统提示 | `src/utils/queryContext.ts` | `fetchSystemPromptParts()` |
| 记忆加载 | `src/memdir/memdir.ts` | `loadMemoryPrompt()` |
| 附件处理 | `src/utils/attachments.ts` | `getAttachmentMessages()`, `startRelevantMemoryPrefetch()` |
| 权限判断 | `src/hooks/useCanUseTool.tsx` | `CanUseToolFn`, `PermissionResult` |
| 权限类型 | `src/types/permissions.ts` | `PermissionMode`, `PermissionResult` |
| 全局状态 | `src/state/AppState.tsx` | `AppState` |
| 遥测上报 | `src/services/analytics/index.ts` | `logEvent()` |
| 插件加载 | `src/utils/plugins/pluginLoader.ts` | `loadAllPluginsCacheOnly()` |
| 技能加载 | `src/skills/bundledSkills.ts` | bundled skills 定义 |
| CLI 入口 | `src/entrypoints/cli.tsx` | CLI 参数解析与启动 |
| 主入口 | `src/main.tsx` | 应用启动，MDM/Keychain 预取 |

---

## 完整流程图

```
┌─────────────────────────────────────────────────────────────┐
│                    用户输入一条消息                           │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
            QueryEngine.submitMessage()
            ┌─────────────────────────┐
            │ 1. 构建系统提示          │  fetchSystemPromptParts()
            │ 2. 加载记忆              │  loadMemoryPrompt()
            │ 3. 处理斜杠命令          │  processUserInput()
            │ 4. 包装权限检查          │  wrappedCanUseTool()
            └────────────┬────────────┘
                         │
                         ▼
                 query() → queryLoop()
┌────────────────────────────────────────────────────────────┐
│                     while (true)                            │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ ① 上下文压缩（4道防线）                              │   │
│  │    applyToolResultBudget()  → 工具结果大小限制       │   │
│  │    snipCompactIfNeeded()    → 删旧消息               │   │
│  │    microcompact()           → 压缩工具输出           │   │
│  │    applyCollapsesIfNeeded() → 折叠旧轮               │   │
│  │    autocompact()            → 全量摘要（最重）        │   │
│  └─────────────────────────────────────────────────────┘   │
│                          │                                  │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ ② token 预检                                         │   │
│  │    calculateTokenWarningState()                      │   │
│  │    超限 → return { reason: 'blocking_limit' }        │   │
│  └─────────────────────────────────────────────────────┘   │
│                          │                                  │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ ③ 调用 Claude API（流式）                            │   │
│  │    deps.callModel({ messages, systemPrompt, tools }) │   │
│  │    for await (message of stream)                     │   │
│  │      → 收集 assistantMessages[]                      │   │
│  │      → 收集 toolUseBlocks[]                          │   │
│  │      → yield message 给上层                          │   │
│  └─────────────────────────────────────────────────────┘   │
│                          │                                  │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ ④ 工具执行（runTools）                               │   │
│  │    partitionToolCalls() 分组：                       │   │
│  │    ├── isConcurrencySafe=true  → 并发（≤10个）       │   │
│  │    └── isConcurrencySafe=false → 串行               │   │
│  │    结果 → toolResults[]                              │   │
│  └─────────────────────────────────────────────────────┘   │
│                          │                                  │
│        needsFollowUp?    │                                  │
│        ┌────────────────┴────────────────┐                 │
│        ▼ 是                              ▼ 否               │
│  追加工具结果到 messages             return Terminal        │
│  state.turnCount++                  { reason: 'stop' }     │
│  continue ──────────────────────────────────────────────►  │
│                     回到循环顶部                             │
└────────────────────────────────────────────────────────────┘
                          │
                          ▼
              返回最终回复给用户
```
