# 第三阶段详细设计：Harness 接入

版本：v1.0。日期：2026-11-03。状态：依据已确认设计共识编写，待代码实施与阶段验收。

## 1. 依据、目标与阶段范围

本文依据 [总需求](coding-agent-requirements.md)、[总概要设计](coding-agent-overview-design.md) 第三阶段约定，以及 [第二阶段详细设计](phase-2-detailed-design.md) 和 [第二阶段实现记录](../tests/phase-2-validation.md)，定义 Harness 接入所需的模块、类型、接口、调用顺序、错误和验证安排。本文中的新增目录和接口是待实现设计，不代表当前仓库已经具有这些能力。

本阶段由 Deep Agents 接管模型与工具执行循环；应用程序负责阶段切换、任务授权、累计预算、验收固定、真实结果判定和 Git 交付。继续复用 Windows 原生工具、单活动任务、SQLite、FastAPI、React 和 SSE。

已确认的选择如下：

- 使用 DeepSeek 非思考模式作为本阶段兼容验收范围，模型名称和端点保留服务端配置。
- 采用可信本机执行；用户发起任务即授权其范围内的开发操作，不增加逐步确认。
- 每个 Web 编码任务暂按一个编码子任务处理，固定最多三轮实现；一轮内可以多次修改和自测，正式交验失败后修复进入下一轮。
- 默认累计执行时限为 60 分钟，等待用户澄清不计时；默认最多 40 次模型请求、单命令 120 秒、stdout 和 stderr 各 1 MiB。
- 验收条件自动生成，用户可以在任务表单中补充；固定后展示条件、测试位置和实际检查依据。
- 补答只澄清原目标。固定验收后改变目标或条件时，用户结束当前等待并另建任务，已有改动和记录保留。
- 将验证和 Git 交付核心抽离为共享实现，新 Harness 与旧 CLI 共用。
- 本阶段不交付复杂规划、任务暂停或运行中取消、跨重启恢复、Skill、RAG、多 Agent，以及完整 JS/TS 项目开发。

网页继续面向环境已就绪的 Python、pytest、已有 Git HEAD 的项目；JS/TS 文件和命令工具继续回归。完整语言能力按概要设计在第五阶段验收。

## 2. 当前实现与模块落位

### 2.1 当前事实

当前 TaskService 已通过 TaskRuntime 协议调用 AgentRuntime，但 AgentRuntime 仍委托 AgentWorkflow。回归、验收固定、验证反馈和提交算法主要位于 app/workflow.py，JUnit 执行及判定位于 app/checks.py；现有 app/git_ops 提供快照、查询、写入检查及交付前保护，没有完整的 commit_verified 实现。

实际应用装配位于 app/web/application.py，本阶段直接修改该入口，不假定第二阶段设计中的 app/composition.py 已存在。现有任务状态、工具、进程及存储模块按实际代码演进，不以重做第二阶段为前提。

### 2.2 功能与模块对应

| 功能模块 | 代码落位 | 职责和直接依赖 |
| --- | --- | --- |
| Harness 运行 | app/harness/runtime.py、model.py、contracts.py | 实现 TaskRuntime；装配 SDK、模型和阶段协议；依赖工具适配、预算、授权、验证、Git 和存储 |
| 工具适配 | app/harness/tool_bridge.py、middleware.py | 框架工具转换、工具名称映射、调用检查与 SDK 异常转换；实际操作仍委托 app/tooling |
| 任务授权 | app/tasks/policy.py | TaskScope、调用授权、阶段写入范围；复用 app/workspaces 的路径规则和 app/git_ops 的已有工作保护 |
| 执行预算 | app/tasks/budget.py | 实现轮次、实际模型请求数和活动耗时；接收时钟与状态保存依赖 |
| 验证与交付 | app/verification/service.py、contracts.py；app/git_ops/service.py | 提取检查、验收固定、证据和任务提交，供新旧入口共用 |
| 状态与 Web | app/tasks/service.py、app/storage、app/web、frontend/src | 任务互斥、配置快照、事件、产物、HTTP 和页面增量 |

app/models_v2.py 保持现有项目和任务公开类型入口，不再增加 models_v3.py。跨模块类型放在其所属模块，公开 HTTP 模型引用这些类型；不引入一个依赖所有模块的公共大包。

TaskRunConfig、TaskRunOutcome 和 TaskRuntime 移到 app/tasks/contracts.py，由原 app/tasks/service.py 重新导出以兼容已有导入。存储只引用这些契约，不反向依赖 TaskService，避免配置读取引入循环依赖。

### 2.3 依赖与装配

装配顺序为：存储及脱敏 → 工作区、ProcessManager 和 Git → 共享验证 → 授权策略及工具注册 → HarnessRuntime → TaskService → Web。

HarnessRuntime 接收模型工厂、ToolRegistry、VerificationService、GitService、StateStore、ProcessManager 和时钟。每个任务创建自己的执行会话、授权快照和 BudgetLedger，复用应用级进程管理器。旧 AgentWorkflow 改为委托共享验证和提交实现，不保留一份复制算法。

框架消息、BaseTool、CompiledStateGraph 和内存检查点只在 app/harness 内使用。TaskService、验证、Git、存储和 HTTP 不接收 SDK 状态对象。通过构造参数替换外部模型，测试与生产跨越相同的运行接口。

## 3. 公共类型和默认值

### 3.1 通用约定

沿用 Pydantic 严格校验、拒绝额外字段、UUID 字符串、UTC ISO 8601 时间、整数毫秒耗时和字节限额。工具路径为相对工作区路径，输出使用正斜杠。布尔值不作为整数接受。

下表是需要新增或扩展的类型；既有工具参数、ToolResult、ArtifactRef、CommandSpec 和 CheckOutcome 继续复用。内部对象不因列在表中而自动成为 HTTP 或模型输入。

| 类型 | 字段与语义 |
| --- | --- |
| ExecutionLimits | 保留 max_model_requests、command_timeout_seconds、output_limit_bytes；新增 task_timeout_seconds，默认 3600，允许 60–86400 秒；三轮实现固定，不作为项目可编辑字段 |
| TaskScope，内部不可变对象 | task_id、project_id、规范化 workspace、project_revision、授权的工具名称集合、acceptance_paths、protected_paths；由后端根据项目与阶段构造 |
| TaskRunConfig，内部 | 保留 task、project、task_dir；增加用户 acceptance_criteria、runtime_version 和 TaskScope；project 为创建任务时的快照 |
| ToolInvocation，内部 | call_id、registry_name、已校验 arguments、operation；框架名称先完成精确映射，不允许模型填写工作区、阶段或预算 |
| AuthorizationDecision | allowed、可选 ToolError；允许时没有 error，拒绝时必须提供 code 和原因 |
| BudgetState，内部持久化 | attempt、model_requests、elapsed_ms、attempt_open；不持久化 monotonic 的原始值 |
| BudgetSnapshot，公开只读 | attempt、max_attempts=3、model_requests、max_model_requests、elapsed_ms、remaining_ms、remaining_model_requests、counting、captured_at |
| AcceptanceCriterion | id、text、source: user 或 generated；id 由后端分配，source 由输入来源确定 |
| AcceptanceSnapshot | task_id、revision=1、criteria、test_paths、file_hashes、expected_test_ids、frozen_at；公开值不含验收备份的磁盘位置 |
| PhaseOutcome，Harness 内部协议 | kind: acceptance_ready、implementation_ready 或 needs_clarification；summary、questions、generated_criteria；kind 与当前阶段须匹配 |
| ClarificationReview，Harness 内部协议 | kind: clarification、requirement_change 或 needs_clarification；summary、questions；只用于判断补答是否可继续原任务 |
| VerificationConfig，内部 | task_id、workspace、artifact_dir、checks、acceptance_paths、limits、mode: web 或 legacy；新旧入口分别提供配置读取适配，预算对象只在后端传入 |
| VerificationResult | id、task_id、attempt、purpose: formal 或 self_check、acceptance_revision、checks、workspace_fingerprint、passed、created_at；仅验证模块生成并保存 |
| CommitResult | commit、diff_artifact、delivered_paths、verification_id、workspace_fingerprint；发布部分成功后的失败通过交付错误另外携带实际提交位置 |
| StopReason | code、message、phase；不包含凭据、原始模型消息或任意内部路径 |

TaskSnapshot 保留原状态、phase、attempt、model_requests、summary、questions、commit 和 error，新增：

- acceptance_criteria：用户提交的原始条件列表，默认空数组。
- acceptance：可空的 AcceptanceSnapshot，固定前为空。
- budget：可空的 BudgetSnapshot，旧任务没有记录时为空，不补造历史耗时。
- stop_reason：可空的 StopReason。
- requires_new_task：默认 false，仅明确改变已固定目标时设为 true。
- runtime_version：新任务为 harness-v1，旧任务缺失时按 legacy 展示。

TaskRunOutcome 保留现有返回形状，补充 acceptance、budget、stop_reason 和 requires_new_task。顶层 attempt、model_requests 与 budget 中的值必须来自同一份 BudgetLedger，不能由事件处理器再独立递增。

PhaseOutcome 和 ClarificationReview 的 summary 最多 2000 字符，questions 最多 10 条、每条 1–2000 字符；needs_clarification 必须含问题。generated_criteria 只在 acceptance_ready 时允许非空，最多 20 条、每条 1–2000 字符；用户未提供条件时至少生成一条。用户条件和生成条件分别保留，固定清单为 1–40 条。

### 3.2 限制与运行约定

| 项目 | 本阶段规则 |
| --- | --- |
| 并发 | 单实例、单后端 worker、一个活动任务位置，不排队；每任务模型请求及前台工具串行 |
| 实现轮次 | 固定最多三轮；准备、澄清和模型重试均不单独增加实现轮次 |
| 模型请求 | 默认 40 次，沿用 1–1000 可配置范围；失败的实际请求也计数；SDK 传输自动重试为 0 |
| 单模型请求 | 沿用 45 秒上限，实际超时取该值和任务剩余时间的较小值 |
| 普通命令 | 默认 120 秒，沿用 1–3600 可配置范围；实际超时还受任务剩余时间约束 |
| 输出与读取 | 保留第二阶段的每路输出、单文件、分页、搜索和日志轮转限制 |
| 累计时间 | 默认 3600 秒，从后台任务执行开始累计；涵盖基线、模型、工具、自测、正式验证和交付，等待澄清不累计 |
| 框架循环保护 | 每次图执行配置 recursion_limit=1000；触达时作为 HARNESS_LIMIT 失败，不能重建图以清零应用预算 |
| 受管服务 | 应用级最多三个同时运行的受管服务，任务结束后保留的服务也计入；达到上限拒绝新启动，不自动停止已有服务。任务结束后用户可停止，应用正常关闭清理；服务存活时间不计入任务执行时间 |

新建项目默认带上完整限制；旧 ProjectProfile 缺少 task_timeout_seconds 时读取默认值，下一次用户保存时正常写入。不批量改写旧任务、不安装新依赖作为本次文档落盘的一部分。

## 4. SDK 与模型接入

### 4.1 版本与兼容验证入口

以 deepagents 0.7.32 为首个候选发布版，通过 uv 与 Python 3.13 解析 LangChain、LangGraph 和 langchain-openai 依赖，并在 Windows 本机完成兼容验证后写入锁文件。这里是候选方案，不宣称已经安装、联调或验证通过。[发布信息](https://pypi.org/project/deepagents/0.7.32/)

代码实施的首个验证切片必须证明：SDK 可导入；可装配当前模型；能完成一次真实读取工具调用及其后续响应；自定义工具不重名；阶段结构化结果和工具异常可处理；额外框架能力被阻断。失败时停止该接入方案并记录兼容问题，不静默换回旧模型循环、不修改 SDK 私有源码继续宣称接入成功。

HarnessRuntime 使用 create_deep_agent，而不是另写一套模型—工具 while 循环。应用保留准备、实现、验证和交付的阶段控制，这些控制不重复实现框架内部的工具往返。[SDK 定制接口](https://docs.langchain.com/oss/python/deepagents/customization)

### 4.2 模型工厂

~~~python
class ModelFactory:
    def validate_configuration(self) -> None: ...
    def build(self, timeout_seconds: float) -> BaseChatModel: ...
~~~

该接口仅在 app/harness 内使用。默认适配器构造 langchain_openai.ChatOpenAI，显式传入既有 LLM_BASE_URL、LLM_MODEL_ID、LLM_API_KEY，设置 max_retries=0、streaming=False，并使用 extra_body 中的 thinking.type=disabled。真实默认模型沿用配置，不硬编码模型名称。[DeepSeek 非思考工具调用](https://api-docs.deepseek.com/guides/tool_calls/)

请求超时在实际调用前按剩余预算更新。密钥不进入 TaskRunConfig、Scope、HTTP、SQLite、模型消息或事件；子进程环境继续过滤凭据。传输异常不另起实现轮次，返回 MODEL_ERROR 或 MODEL_TIMEOUT，保留此前证据后终止本次任务。

本阶段显式关闭框架向外部托管平台发送的自动追踪，不以框架调试日志保存原始提示词、消息或工具参数。保留本机已经脱敏的应用事件作为执行证据。

### 4.3 框架能力配置

后端使用 StateBackend 作为框架私有临时后端，不将它映射到项目目录，也不启用 LocalShellBackend。模型访问项目只经过应用工具。

本候选版通过公开 HarnessProfile 配置关闭默认通用子 Agent，且不传入其他子 Agent、memory 或 skills；排除 SummarizationMiddleware，把上下文整理留给第四阶段。注册配置按实际模型适配器报告的 provider:model 标识匹配，在应用装配时完成一次；测试验证所选模型实例确实命中配置。[候选版 Profile 接口](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.32/libs/deepagents/deepagents/profiles/harness/harness_profiles.py)

覆盖 FilesystemMiddleware 时使用 tools=["read_file"]，并关闭工具及用户消息的自动外移。该候选版要求该列表包含 read_file，不能用空列表关闭全部工具；框架 read_file 随后由 Profile 排除，并由应用调用检查再次拒绝。项目工具使用独立名称，避免重名。框架大结果外移在本阶段不使用，由已有输出限制和产物承担。[候选版文件中间件](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.32/libs/deepagents/deepagents/middleware/filesystem.py)

无论配置效果如何，模型可见工具和实际执行都按应用清单检查。框架 ls、read_file、write_file、edit_file、delete、glob、grep、execute、task、write_todos 及未登记工具均不得作为项目操作入口。不能只在提示词中声明禁用。

内存检查点只为同一应用进程中的阶段往返和澄清补答保留图状态，thread_id 使用后端生成的 task_id。任务结束后释放会话；重启后未结束任务标为 interrupted，不从内存或 SQLite 推导可恢复承诺。

上下文超过所选模型容量时如实报告模型错误，保留已有证据；不为继续执行而自动启用本阶段尚未验收的上下文整理能力。

## 5. Harness 运行模块

### 5.1 外部接口

~~~python
class HarnessRuntime:
    def validate_configuration(self) -> None: ...
    def start(
        self, config: TaskRunConfig,
        emit: Callable[[dict[str, object]], None],
    ) -> TaskRunOutcome: ...
    def continue_with_answer(
        self, config: TaskRunConfig, answer: str,
        emit: Callable[[dict[str, object]], None],
    ) -> TaskRunOutcome: ...
~~~

保持现有 TaskRuntime 三个方法的调用方式。start 仅接受新任务，重复启动返回 INVALID_TASK_STATE；continue_with_answer 只接受当前等待中的同任务、同配置快照和存活会话。会话缺失返回 TASK_SESSION_LOST，不重建一个空会话继续。

验证配置不发送模型请求。TaskService 在提交后台任务前检查静态配置；后台 start 创建 BudgetLedger、Git 快照、文件变更记录和 Harness 会话后执行阶段流程。所有正常终止和可预期失败均返回 TaskRunOutcome，不能因异常丢失已消耗的预算。

### 5.2 阶段结果协议

使用 SDK 的 response_format 和 ToolStrategy 为 PhaseOutcome 提供结构化输出。它是框架内部的阶段结果协议，不是授予模型验收或提交权限。结构化结果只能单独返回；如果同一响应同时要求执行项目工具和提交阶段结果，执行前报告 PROTOCOL_ERROR，不产生该批工具副作用。

| 当前调用目的 | 接受结果 | 程序动作 |
| --- | --- | --- |
| 准备验收 | acceptance_ready | 合并用户条件与模型补充条件，固定并运行验收基线 |
| 准备或实现中的歧义 | needs_clarification，questions 非空 | 保存当前阶段、轮次和会话，停止活动计时，等待补答 |
| 当前轮实现 | implementation_ready | 关闭本轮模型执行入口，转程序正式验证 |
| 任意阶段结果不匹配或字段非法 | 不接受为完成 | 结构化纠正请求仍计入模型预算；最多一次纠正，仍无效则 PROTOCOL_ERROR |

普通自然语言、工具输出中的完成声明，以及模型提供的 passed、commit 或预算数字，均不更新程序事实。阶段协议中的 summary 仅供说明，错误原因、检查状态和提交记录以程序结果为准。

### 5.3 任务执行顺序

1. 进入 preflight：锁定创建时的项目配置，检查工作区、解释器和 Git；建立初始内容、HEAD 及暂存区基线。
2. 进入 baseline：实际执行项目回归及配置的额外检查。基线失败则停止，不能先修改代码来掩盖原始失败。
3. 进入 preparation：以需求和用户验收条件驱动 Harness 检索与编写任务专属验收；行为歧义先澄清。
4. 收到 acceptance_ready 后，程序固定验收条件、文件版本和测试标识，执行原始实现上的验收。无效报告或验收不可执行时停止。
5. 若验收已经通过，复核所有必要检查和工作区，归档并清理本任务生成的验收文件，返回 no_changes，不制造提交。
6. 否则进入 implementation，调用 begin_attempt 开始第一轮；Harness 可以读取、修改和自测。
7. 收到 implementation_ready 后，进入 verification，程序执行全部正式检查。普通测试失败且有剩余轮次时，将真实失败反馈交还 Harness，开始下一轮。
8. 正式验证通过后进入 delivery：禁止新的模型或项目修改调用，校验证据和用户工作保护，提交本任务变更；成功后返回 succeeded。
9. 第三轮仍未通过，或遇到授权、预算、环境、验收完整性及交付错误，返回真实失败和已有成果。失败不回滚项目改动。

预算限制只阻止需要新预算的操作。最后一次允许的模型请求返回合法 implementation_ready 后，如执行时间足够，程序仍可验证和交付；不能仅因请求计数等于上限而跳过已经触发的必要验证。

## 6. 工具适配与统一执行

### 6.1 接口和名称映射

~~~python
class ToolBridge:
    def build_tools(
        self, context: Callable[[], ToolContext],
    ) -> Sequence[BaseTool]: ...

class ToolRegistry:
    def schemas(self) -> list[dict[str, object]]: ...
    def invoke(
        self, name: str, arguments: dict[str, object], context: ToolContext,
    ) -> ToolResult[dict[str, object]]: ...
~~~

ToolBridge 只在 Harness 内使用 BaseTool。根据现有注册项和参数模型创建 StructuredTool：框架名称为 project_<注册名称>，例如 project_read_file、project_edit_file 和 project_run_checks；维护固定双向映射，不接受任意去前缀后分发。底层注册名称、参数和 ToolResult 不改变。

build_tools 的 context 参数为后端上下文提供函数，每个工具获得串行锁后再调用它，读取当前阶段、轮次及固定保护范围。不能在图创建时捕获准备阶段上下文并沿用到实现或澄清；提供函数本身不作为模型参数。

现有十七个操作继续可用：九个工作区操作，run_command、四个服务操作、git_status、get_diff 和 run_checks。run_checks 从旧 workflow 的特殊分支提取为正常注册项，调用共享验证模块，参数仍为 scope="all"。

ToolContext 增加后端持有的 policy、budget 和 execution_purpose: normal 或 clarification_review。Workspace、artifact_dir、phase、attempt 和这些对象均不进入工具参数 Schema。

### 6.2 调用链和中间件

真实调用顺序为：框架名称清单检查 → 映射并严格校验参数 → 当前任务与阶段授权 → 检查累计时间 → 记录调用开始 → ToolRegistry 调度 → 原有文件或进程检查 → 执行 → 脱敏结果 → 记录结束。

ToolRegistry 增加后端的授权和预算前置检查，避免内部直接调用注册器时遗漏控制。Bridge 负责协议转换，不复制文件或进程逻辑。

- 模型调用中间件在每次实际模型请求前预占计数、设置剩余超时并记录请求事件；放在可能重发请求的框架中间件内部，确保每次真实调用都经过计数。
- 工具调用中间件检查所有执行路径，包括伪造框架原生工具、结构化结果工具和未知名称。结构化结果工具只允许写阶段结果，不委托项目操作。
- 使用图执行 max_concurrency=1 和任务内工具串行锁；批量工具调用按响应顺序执行，每个调用前重新核对时间和阶段。前一个调用触发任务终止后，其余调用返回拒绝，不继续产生副作用。
- 未知工具、参数错误、过期哈希和范围拒绝返回原有 ToolError，可供模型修正；预算耗尽、关键存储失败和阶段协议失败直接退出本次图调用。

文件变更和命令记录分开：record_change 只记录 FileChange 或 DirectoryChange；命令通过 command_started、command_completed 和输出事件登记，不能将不含 path 的命令字典传给文件变更处理器。命令不会自动产生可提交的文件所有权。

## 7. 任务授权模块

### 7.1 接口

~~~python
class TaskPolicy:
    def authorize(
        self, invocation: ToolInvocation, context: ToolContext,
    ) -> AuthorizationDecision: ...

class WorkspaceService:
    def resolve_path(
        self, relative: str, context: ToolContext, *, for_write: bool,
    ) -> Path: ...
~~~

提取现有路径解析为可复用接口，由授权检查和原有工具实现共用。Windows 大小写、空格、中文、父级遍历、绝对路径和目录联接均按最终解析位置校验；移动必须检查源和目标，删除不得借助目录操作变成递归删除。

凭据配置和 Git 元数据维持保护。固定验收允许读取，禁止模型写入、移动或删除；不能因复用原有 protected_paths 而连验收读取也一并禁止。已有未跟踪文件、已暂存文件及无法区分的用户修改继续按既有规则保护。

### 7.2 阶段权限

| 阶段或目的 | 模型允许操作 | 额外规则 |
| --- | --- | --- |
| preparation | 项目只读工具、Git 查询、验收文件及必要父目录创建或修改 | 不修改实现文件，不开放 run_command、run_checks、服务启动和停止 |
| implementation | 授权工具全集及自测 | 固定验收不可写，run_checks 仅生成 self_check 证据 |
| clarification_review | 只返回补答分类结构 | 不暴露项目工具，不执行文件、命令或服务操作 |
| baseline、verification、delivery | 不调用模型项目工具 | 由程序执行基线、正式验证和提交 |
| 等待、终态或时限耗尽 | 不执行新项目操作 | 仅允许程序记录结果、保存证据和清理本次前台执行 |

授权范围来自提交任务时的项目和任务目标，不能由模型修改。遇到范围不适用时，模型可发起澄清；本阶段不通过补答把工作区扩展为其他项目。

run_command 和 start_service 保留可信本机开发能力；工作目录校验不等于操作系统沙箱，脚本可能访问工作区外资源。应用不以解析任意 PowerShell 文本保证完全隔离。模型收到任务范围约束，工具和交付检查提供可实施的保护；越界元数据变化或无法归属的工作区变化必须如实阻断交付。框架内置文件权限也不自动约束自定义工具或任意脚本。[框架权限范围](https://docs.langchain.com/oss/python/deepagents/permissions)

## 8. 执行预算模块

### 8.1 接口

~~~python
class BudgetLedger:
    def begin_attempt(self) -> int: ...
    def finish_attempt(self) -> BudgetSnapshot: ...
    def before_model_call(self) -> BudgetSnapshot: ...
    def check_deadline(self) -> None: ...
    def suspend(self) -> BudgetSnapshot: ...
    def resume(self) -> BudgetSnapshot: ...
    def snapshot(self) -> BudgetSnapshot: ...
~~~

构造参数为 ExecutionLimits、已保存的 BudgetState、可注入的 monotonic 时钟和保存回调。所有变更在任务锁内完成；计数必须保存成功后才允许相应执行，保存失败不发送模型请求或开始新一轮。

### 8.2 计数与计时

- begin_attempt 在进入新实现轮次时占用次数，首次为 1；已有未结束轮次不得再次调用。实现中澄清和自测保留当前次数，正式检查失败后才允许进入下一轮。
- finish_attempt 仅由程序在正式验证结束后调用，将 attempt_open 置为 false；不减少次数，不在自测或等待澄清时关闭轮次。终止任务可以保留未完成轮次的记录。
- before_model_call 检查时间和请求余额，预增模型计数后发送实际请求；网络失败仍消耗一次。结构化纠正、补答分类和工具后续推理都计数；没有未计费的后台模型请求。
- 每个活动执行区间记录进程内 monotonic 起点，elapsed_ms 为历史累计值加当前区间耗时。suspend 在等待澄清前结算，resume 从同一累计值开始；计数和轮次不重置。
- snapshot 返回非负剩余值，只读不消费预算。remaining_model_requests 为 0 时只禁止下一次模型请求；check_deadline 只检查时间，不能把最后一次合法响应后的验证错误地拦截为模型次数耗尽。
- 查询页面、等待补答和已经脱离任务的服务存活不增加耗时；基线和测试收集不得绕过总时限或设置不受约束的固定超时。

### 8.3 超限处理

到达累计时限后不再发送请求、执行后续工具或开始交付。前台命令、测试及模型请求使用不超过剩余时间的超时，并在运行期间检查任务截止时间。ProcessManager 和共享检查执行器使用同一 Windows 进程树清理能力，不能只停止等待线程而留下命令继续修改文件。

终止及证据保存允许最多 10 秒清理宽限，不在宽限中执行新任务动作。清理未完成时，报告具体进程或服务状态，不宣称已全部停止。原有长期服务延续第二阶段保留策略。

超限记录 MODEL_REQUEST_LIMIT、IMPLEMENTATION_LIMIT 或 TASK_TIME_LIMIT，并返回 failed。若 Git 已经发布提交后时限才到达，先核对发布事实并记录提交和部分交付状态，不能误报“未提交”或自动撤回 HEAD。

## 9. 共享验证与 Git 交付

### 9.1 验证接口

~~~python
class VerificationService:
    def run_baseline(self, task_id: str) -> list[CheckOutcome]: ...
    def freeze_acceptance(
        self, task_id: str, generated_criteria: list[str],
    ) -> AcceptanceSnapshot: ...
    def run_acceptance_baseline(self, task_id: str) -> CheckOutcome: ...
    def verify(
        self, task_id: str, attempt: int, *, purpose: str = "formal",
    ) -> VerificationResult: ...
    def restore_initial_acceptance(self, task_id: str) -> None: ...

class GitService:
    def commit_verified(
        self, task_id: str, verification_id: str,
    ) -> CommitResult: ...
~~~

VerificationService 通过构造参数接收 VerificationConfig 读取、任务状态读取、工作区、检查执行器、产物和事件能力。配置读取分别适配旧 TaskStore 和新 StateStore；legacy 不启用 Web 新增的任务总时限。purpose 在实现中使用 formal、self_check 的 Literal 类型。基线、验证和交付不接收模型提供的命令、预期标识、文件哈希或 passed。

### 9.2 验收固定与真实判定

验收位置沿用 tests/agent_acceptance/<task_id>/test_requirement.py，由后端分配，目标已存在则拒绝覆盖。可在该文件内生成多个 test_requirement 前缀的测试函数；收集实际 pytest 标识后绑定，不再只假定一个固定函数标识。

用户条件不可被模型删除、改写或伪装为 generated。模型只补充 generated_criteria，后端分配条件 ID，生成不可变 revision=1 快照，并登记条件与测试文件、报告之间的证据关系。此关系表示测试设计依据，不宣称程序已经自动证明每项自然语言语义都被完整覆盖。

固定时校验文件存在、内容无已知凭据、非空测试集可收集，并保存字节哈希和备份。首次原始实现验收需要有效报告：实际断言失败可以进入实现，语法错误、收集失败、零测试、缺失报告或跳过不作为有效的需求失败基线。

verify 实际执行固定验收、原有回归和项目额外检查。回归必须保留基线必需标识；测试报告须与本次执行对应，命令返回 0 不代替报告判定。self_check 和 formal 均保存独立 UUID 命名报告，避免同一轮多次自测覆盖证据。

验证前后检查固定验收和待验证内容。若脚本改变验收，保存异常证据，拒绝通过，在路径仍安全时恢复任务自有固定版本；不能恢复后掩盖本次异常。检查修改源代码、外部修改相关文件或执行结果不完整时，证据不得标为通过。

工作区指纹涵盖 HEAD、真实索引摘要、受验证的跟踪文件及非忽略源码和配置文件内容、任务新增或删除清单及固定验收版本；不把 .git、应用产物、依赖目录和测试缓存纳入内容指纹。对用户显式跟踪或任务主动修改的文件不能仅因命中默认忽略项而漏检。不能只使用 mtime、文件大小或任务变更路径作为整个验证依据。

全部正式检查通过才生成可交付证据。自测通过不能直接提交；正式验证失败且属于普通测试失败时返回下一轮反馈，验收完整性、环境和报告有效性错误则停止并说明原因。

### 9.3 提交规则与旧入口复用

GitService 从后端保存的 verification_id 读取证据，要求同任务、formal、passed，且当前 HEAD、索引和工作区仍匹配。无证据、证据过期或无法明确归属的变更均拒绝交付。

提交算法从现有 AgentWorkflow 提取，并补齐第二阶段既定的索引保护要求：独立临时索引构造任务树、对用户已有未暂存修改重放可区分的任务修改、保留原有暂存意图、发布前版本复核及预期旧 HEAD 更新。不得使用 reset、clean 或 stash 消除冲突。

普通命令可以用于测试、构建和运行，但其修改不自动获得文件所有权。通过命令产生的源码变化只有具备完整前后内容且能与原有工作明确隔离时才登记；本阶段无法证明归属时返回 UNATTRIBUTED_CHANGE，不通过执行一次 git add 将其归为任务成果。

提交失败报告 COMMIT_FAILED 或明确的冲突代码，并携带实际 HEAD、生成的 commit、是否已发布引用及索引是否已发布。成功状态要求提交和索引发布完成；不因 Git 生成了一个对象就宣布交付完成。验证对象含用户原有改动时，报告明确该事实，不宣称单独检出任务 commit 已经过相同验证。

旧 AgentWorkflow 保留原模型循环、请求、五工具协议、报告字段和受控暂停/恢复行为，仅将验证及提交转为共享模块调用。兼容适配保留显式 CheckSpec、预期标识和实现预算，不强加新 Web 自动验收规则、60 分钟字段或新状态。

## 10. 澄清、状态与异常

### 10.1 任务管理接口

~~~python
class TaskService:
    def start_task(
        self, project_id: str, requirement: str,
        acceptance_criteria: list[str] | None = None,
    ) -> TaskSnapshot: ...
    def answer_clarification(self, task_id: str, answer: str) -> TaskSnapshot: ...
    def dismiss_waiting_task(self, task_id: str) -> TaskSnapshot: ...
~~~

用户补充的条件最多 20 条，每条去除首尾空白后为 1–2000 字符，总计最多 20000 字符；未提供或空数组表示自动生成。拒绝空白条目和非字符串，不静默丢弃用户条件。

继续前读取创建任务时的配置快照，复核 HEAD、索引、已读取及已修改文件和固定验收。变化导致 WORKSPACE_CHANGED，保留成果并失败，不能重新取基线使冲突消失。

补答先在无项目工具的 clarification_review 中分类；分类调用计入累计时间及模型请求。只澄清原目标时返回原阶段、原轮次继续；验收未固定时允许完善准备内容。若改变已固定目标或无法判断，保持 needs_clarification，不执行修改。明确改变时设 requires_new_task=true，提供结束等待、按新目标重新提交的说明；原条件和固定版本保持不变。

requires_new_task 为 true 时，继续提交 answers 返回 NEW_TASK_REQUIRED，用户通过原 dismiss 接口结束等待。新任务不会自动撤销旧任务文件，也不会自动将旧任务部分成果提交为新任务成果。

### 10.2 状态转换

| 当前状态 | 条件 | 下一状态 |
| --- | --- | --- |
| 无任务 | 项目及配置可用、获得活动位置 | running |
| running | 需要补答 | needs_clarification，保留活动位置，停止计时 |
| needs_clarification | 可接受的补答、会话及工作区仍有效 | running，恢复累计计时 |
| needs_clarification | 补答要求另建任务 | needs_clarification，requires_new_task=true，停止计时 |
| needs_clarification | 用户放弃等待 | dismissed，释放位置，保留改动 |
| running | 原实现已满足全部要求且无实现变更 | no_changes |
| running | 正式验证和交付完成 | succeeded |
| running | 预算、环境、协议、验证完整性或交付失败 | failed |
| 未终结任务 | 应用重启 | interrupted，不自动恢复 |

phase 继续使用既有七个值；clarification_review 是内部调用目的，不新增一个公开阶段。普通工具可纠正错误不会立即变为 failed，但其后续调用仍受任务预算限制。终态不可被迟到工具事件或模型结果改回 running。

正常关闭停止接收新任务，仍按第二阶段生命周期等待当前工作在预算内退出，再清理受管进程和存储。不增加运行中取消或暂停的 HTTP 入口。

### 10.3 错误归类

| 错误 | 对外行为 |
| --- | --- |
| UNKNOWN_TOOL、INVALID_ARGUMENTS、INVALID_PHASE、OUTSIDE_WORKSPACE、PROTECTED_PATH、PROTECTED_ACCEPTANCE | 工具拒绝，无本次副作用；记录 execution_rejected，可将修正信息返回模型 |
| MODEL_CONFIG、HARNESS_CONFIG | 任务启动前返回配置错误，不占用活动任务；后台发现则记录失败 |
| MODEL_ERROR、MODEL_TIMEOUT、PROTOCOL_ERROR、HARNESS_LIMIT | 任务失败，保存预算、已有差异和脱敏原因 |
| MODEL_REQUEST_LIMIT、IMPLEMENTATION_LIMIT、TASK_TIME_LIMIT | 任务失败，显示耗尽维度、实际累计值和已有成果 |
| WORKSPACE_CHANGED、GIT_BASE_CHANGED、GIT_INDEX_CHANGED、USER_CHANGE_CONFLICT、UNATTRIBUTED_CHANGE | 停止继续或交付，不覆盖用户工作 |
| REPORT_INVALID、EXPECTED_TESTS_MISSING、REQUIRED_TEST_SKIPPED、ACCEPTANCE_CHANGED | 不判通过，保留报告和异常证据 |
| NEW_TASK_REQUIRED | 409，原任务保持等待；说明使用结束等待和新任务操作 |
| TASK_SESSION_LOST | 返回失败或对继续请求返回 409，不伪造会话恢复 |
| COMMIT_FAILED、STORAGE_ERROR | 停止任务并报告已发生事实；存储不可用时保留本地产物，不编造已持久化事件 |

## 11. 存储、事件与产物

### 11.1 状态保存接口

~~~python
class StateStore:
    def get_run_config(self, task_id: str) -> TaskRunConfig: ...
    def record_progress(
        self, task_id: str, changes: dict[str, object],
        budget: BudgetState, kind: str, payload: dict[str, object],
    ) -> TaskSnapshot: ...
    def save_verification(self, result: VerificationResult) -> None: ...
    def get_verification(self, verification_id: str) -> VerificationResult: ...
~~~

get_run_config 从已有 config_json 重建后端配置，不把其内部目录返回 HTTP。record_progress 在一个 SQLite 事务中保存预算、快照变化和递增事件序号；changes 只允许阶段、预算投影、验收快照及终止说明等后端字段，不接受模型字典直接作为更新。

现有 web_tasks.config_json 保存创建时的项目、用户条件和 runtime_version；runtime_json 保存 BudgetState、当前阶段、文件变更记录、验收备份及验证索引。VerificationResult 以独立不可变 JSON 产物登记，verification_id 与 ArtifactRef.id 对应，读取时验证任务归属及内容哈希，不为此复制一套数据库。

不将图消息、模型密钥、数据库连接或进程句柄写入公开快照；图连续状态仅在本进程中保留。旧 JSON 字段缺失按兼容默认读取，旧证据保持原样。新版本在启动时仍将未终结任务标记 interrupted。

### 11.2 事件契约

沿用 TaskEvent 的 task_id、seq、kind、timestamp 和 payload。seq 仍为同任务严格递增，SDK 不分配或覆盖该序号。

| kind | payload 核心字段 |
| --- | --- |
| model_request | phase、attempt、model_requests、budget |
| attempt_started | attempt、max_attempts、budget |
| budget_updated | budget；活动执行期间最多每秒一条，等待时停止 |
| acceptance_frozen | acceptance |
| execution_rejected | call_id、tool、code、message；只展示必要且已脱敏参数摘要 |
| tool_started、tool_completed、tool_error | 保留第二阶段含义，增加 call_id 和当前 attempt |
| verification_completed | verification_id、purpose、attempt、passed、检查及报告引用 |
| commit_created、commit_error | 验证依据、实际 commit 和发布状态 |
| clarification_requested | questions、requires_new_task、budget |
| task_status_changed | status、summary、stop_reason、budget |

TaskService 不再根据 model_request 事件独立加一；事件和页面数字都来自保存成功的 BudgetState。前端增加对 model_request、attempt_started、budget_updated、acceptance_frozen 和 execution_rejected 的订阅，不能只增加后端事件而遗漏命名 SSE 监听。

程序事件先脱敏再持久化，统一过滤内部路径、凭据和不应公开字段。框架原始流只在运行模块解析，不直接作为 SSE 转发；本阶段不增加 token 级文字流或展示模型内部推理。

### 11.3 产物

登记固定条件与验收清单、每次检查的 JUnit 及有界输出、任务 diff、最终报告及提交依据。自测和正式检查分别标记，报告说明哪些条件有实际测试证据、哪些事项未完成。原有产物 ID、分页、偏移、哈希和访问控制继续适用。

GET 任务详情根据正在运行的 Ledger 计算最新 budget；历史和终态读取已保存值。网页倒计时只用于展示，服务端累计时钟决定是否还能执行。

## 12. HTTP 与 React 增量

### 12.1 HTTP 契约

| 接口 | 变化 |
| --- | --- |
| POST /api/tasks | 保留 project_id、requirement；新增可选 acceptance_criteria 字符串数组，默认空数组；202 返回扩展 TaskSnapshot |
| GET /api/tasks、GET /api/tasks/{task_id} | 返回新增验收、预算、停止说明、requires_new_task 和 runtime_version；历史缺失值显示为空 |
| POST /api/tasks/{task_id}/answers | 仍接收 answer；等待期间按原任务补答，目标变化走 NEW_TASK_REQUIRED 规则 |
| POST /api/tasks/{task_id}/dismiss | 原有等待任务结束操作，语义不变 |
| 项目创建及 PATCH | limits 支持 task_timeout_seconds；沿用 expected_revision 和活动任务禁止修改配置规则 |
| SSE、diff、产物及服务接口 | 复用现有路由；增加上述事件和产物，不开放框架状态查询 |

400/422 继续表示请求和参数无效，409 表示任务状态、配置版本或工作区冲突；后台执行错误通过 TaskSnapshot 和事件返回。一次工具拒绝不作为独立 HTTP 500，也不意味着整个任务已经终止。

首次补答在静态校验通过后仍返回 202，分类在后台执行；若发现目标变化，以后续快照及 SSE 告知。requires_new_task 已经为 true 时再次补答才同步返回 409，不为等待模型分类而阻塞 HTTP 请求。

沿用回环监听、来源校验、修改请求 JSON 要求、凭据不下发前端和产物路径保护。不新增允许用户注入 state_dir、工具实现、框架 backend、API key 或检查报告路径的字段。

### 12.2 页面行为

- 项目设置增加累计执行分钟数输入，显示默认 60 分钟，提交时转换为秒，沿用 revision 更新。
- 任务表单增加可选“验收条件”，多行输入且一行一条；明确不填写也可提交。
- 详情页保留原需求和用户补答，展示用户条件与生成条件、固定版本、验收文件和检查报告入口。
- 预算显示“当前第几轮/最多三轮”、模型调用已用/上限、累计执行和剩余时间；等待澄清时标注停止计时。
- 自测和正式验证使用明确标签；成功页显示真实 commit、差异及正式验证依据，失败页显示已有成果、停止原因和剩余事项。
- 改变固定目标时展示“结束当前等待并新建任务”，不把补答按钮作为修改固定验收的入口；新建表单可预填新目标，但必须由用户提交。
- 日志、模型摘要和 diff 作为文本显示；不提供框架工具调试台、任意命令输入、代码编辑器、暂停、运行中取消或跨重启恢复按钮。

## 13. 测试设计、兼容验证与实施顺序

本节约束后续代码开发，当前落盘只检查文档。按 [概要中的 TDD 规范](coding-agent-overview-design.md) 一次推进一个可观察行为：先失败测试，再最小实现，再保留回归；不先批量编写全部测试，也不补造历史 TDD 记录。

### 13.1 测试入口与场景

| 编号 | 场景 | 入口与可观察结果 |
| --- | --- | --- |
| P3-01 | 候选 SDK、Python 3.13、Windows 和非思考模型 | 真实 DeepSeek 完成工具往返和结构化阶段结果；保存依赖、环境与脱敏证据 |
| P3-02 | 工具 Schema、名称映射及严格参数 | 实际 Harness 调用真实 ToolRegistry；十七个应用工具无重名，非法参数无副作用 |
| P3-03 | 原生工具、未知工具和伪造上下文 | 模型强行请求 execute、read_file、task、write_todos 等时被拒绝，不触达项目或进程 |
| P3-04 | 准备阶段写实现、验收写入及读取 | 准备只允许验收写入；实现阶段使用新上下文；固定后可读不可写；Windows 路径及联接越界拒绝 |
| P3-05 | 文件、命令和服务经 Harness 执行 | 临时仓库及真实受管进程产生正确结果；命令记录不进入文件变更解析；服务上限生效且不误停原有服务 |
| P3-06 | 多工具批次与阶段结果混用 | 工具串行，终止后后续调用不执行；混合交验响应在副作用前拒绝 |
| P3-07 | 基础完整编码闭环 | 自然语言任务经真实 Harness、文件修改、正式检查和 Git 交付，检查工作区及 commit 树 |
| P3-08 | 三轮与自测 | 多次自测不增加轮次；正式失败后依次进入第二、三轮；第三轮失败不产生第四轮修改 |
| P3-09 | 请求与累计时间上限 | 注入时钟及可控模型验证边界；传输失败和结构纠正计数；最后一次合法交验可继续正式验证 |
| P3-10 | 截止时间发生在命令、测试或模型请求中 | 用短预算运行真实子进程树，确认超时清理和停止后不再写文件，不等待真实 60 分钟 |
| P3-11 | 澄清继续与目标改变 | 同会话、同轮次、预算保留且等待不计时；需求改变不改固定验收，要求另建任务 |
| P3-12 | 补答前文件、HEAD 或索引变化 | 返回工作区冲突，不覆盖、不重新取基线或重置预算 |
| P3-13 | 自动条件和用户条件 | 用户条件原文保留，多测试标识正确固定；不能只展示条件而没有真实测试执行 |
| P3-14 | 模型声称成功但实际失败 | 非零退出、缺失报告、零测试、跳过及过期报告均不产生成功或提交 |
| P3-15 | 验收篡改、检查改源码和验证后外部变化 | 异常证据保留，正式结果无效，不能恢复文件后继续沿用错误成功 |
| P3-16 | 用户已有修改、无关暂存和未跟踪文件 | 提交只含可隔离任务变更；用户工作及暂存意图保留，命令未归属变更阻断交付 |
| P3-17 | 无需修改、提交失败及部分发布 | no_changes 无空提交；提交和索引失败如实报告实际阶段及已有 commit |
| P3-18 | 存储、快照、事件及隐私 | 计数只增长一次，序号单调，失败记录一致；无凭据、原始图状态和内部路径泄露 |
| P3-19 | HTTP、SSE、旧记录及重启 | 新字段兼容缺失值，重连去重；旧任务可读，未结束任务 interrupted，不自动恢复 |
| P3-20 | React 和浏览器流程 | 表单到验收、预算、澄清、正式验证、diff 和 commit；页面状态与真实证据一致 |
| P3-21 | 前两阶段累计行为 | 原 CLI、报告、暂停/恢复、Windows 工具、进程、JS/TS 基础命令及用户工作保护继续通过 |

后端使用 pytest 和真实临时项目、文件、SQLite、Git 与受管进程。模型替身只替换外部模型响应，不模拟修改、检查结果或提交。预算时钟可以注入，进程超时清理仍需在 Windows 上实际验证。

浏览器测试使用真实 FastAPI、HarnessRuntime、Deep Agents、工具及验证交付链，只替换外部模型；不能沿用仅返回 TaskRunOutcome 的任务运行器替身证明 Harness 已经贯通。至少覆盖一次真实文件修改与 commit，以及一次补答后继续交验。

### 13.2 真实模型联调

独立于确定性回归，在临时 Python 仓库中执行以下真实 DeepSeek 场景：一次需求明确的函数修改及交付、一次有歧义任务补答后继续、一次低模型请求上限的受控停止。每个场景执行一次并逐项记录，不用反复重跑后只保留成功样本。

记录 SDK 和依赖锁、Python 和 Windows 版本、模型名称、非思考配置、实际工具调用、预算、JUnit、差异、提交及停止原因，不记录密钥和内部推理。基准是实际结果和协议正确，不要求固定模型文案或特定工具调用次序。任一场景失败则保留事实、修复后重新执行受影响联调；没有可用凭据或端点时标为未验证，不能宣称阶段交付。

### 13.3 纵向实施次序

1. 重跑前两阶段基线，记录已有失败与环境限制；完成 P3-01 的最小兼容切片，验证后才固定依赖。
2. 从共享基线检查和一次正式验证开始提取旧实现，接着提取一次真实 Git 交付；持续保持旧 CLI 兼容。
3. 以一次 Harness 读取、一次真实编辑推进工具适配，加入授权、原生工具阻断、预算和事件。
4. 接通验收准备、三轮、验证反馈及交付；加入命令、服务和异常行为，不复制原模型循环。
5. 接通同进程澄清、配置快照、存储一致性和历史兼容，再完成 HTTP 与页面增量。
6. 完成浏览器贯通、累计回归及真实模型联调，形成关联实现 commit 的第三阶段验收记录。

代码交付前运行后端累计测试、前端测试与构建、关键浏览器测试和受影响路径 Ruff 检查，执行 git diff --check。已有未修改的 lint 问题按第二阶段记录单独说明，不删测试、不放宽断言，也不将真实功能缺陷归为环境限制。[仓库交付要求](../../AGENTS.md)

## 14. 阶段交付标准与本次落盘

- 新 Web 任务默认经过 Deep Agents；旧循环只保留在明确的 CLI 兼容入口。
- 权限、时间、请求和实现轮次在实际执行入口生效，不能通过框架附带工具、批次或补答绕开。
- 用户可输入并查看验收条件，页面预算、事件、真实验证和提交依据一致。
- 正式验证与 Git 交付为共享实现，保留验收固定、报告真实性、用户工作保护和安全提交行为。
- Windows 本机兼容验证、新增场景、前两阶段累计回归及真实模型联调达到上述标准，记录所选版本及当前边界。
- 不把内存会话描述为跨重启恢复，不把可信本机执行描述为沙箱，也不提前宣称规划、Skill、RAG 或多 Agent 已交付。

本次只新增第三阶段详细设计文档，不修改运行代码、总需求、概要或第二阶段文档，不安装依赖、不创建运行数据，不执行本节所列产品行为验收。落盘后运行既有文档检查和差异检查，并按 AGENTS.md 创建对应 Git commit；后续代码实施和阶段验收另行记录。
