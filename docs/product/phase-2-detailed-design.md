# 第二阶段详细设计：工具体系与最小 Web

版本：v1.0。日期：2026-11-02。状态：设计已确认，代码实现与验收记录见[阶段二验收记录](../tests/phase-2-validation.md)。

## 1. 依据、范围与完成目标

本文依据 [总需求](coding-agent-requirements.md) 和 [总概要设计](coding-agent-overview-design.md) 的第二阶段约定，定义代码实施所需的模块、类型、工具参数、Python 接口、HTTP 契约、状态及测试边界。本文中的接口和目录是待实现设计，不表示仓库已具有对应能力。

本阶段完善完整本机开发工具集，并建立 FastAPI、React、TypeScript、Vite 构成的最小 Web。沿用现有同步 DeepSeek 调用流程，不接入新的 Harness。

已确认的边界如下：

- 按职责逐步拆分复用现有代码，保留 [第一阶段 CLI 验收契约](../tests/cli-contract.md)。CLI 不作为产品使用入口。
- 用户先保存项目配置，再提交自然语言任务；内部报告路径、验收位置和测试标识由后端管理。
- 全局最多一个活动任务，不建立排队系统。待澄清任务保留活动位置，可以补答继续或放弃等待。
- 网页编码任务面向环境已就绪的 Python、pytest 项目。JS/TS 在本阶段验证文件及命令工具，不承诺完整编码任务。
- 本阶段不提供运行中取消、暂停和跨重启恢复；放弃等待不终止运行中的任务。
- 支持程序与参数列表、显式 PowerShell 脚本两种执行模式，不提供交互式终端。
- 任务启动的开发服务在任务结束后保留，由用户停止；应用正常关闭时清理受管服务。
- 程序统一验证和提交任务改动，网页不提供代码编辑器或通用命令执行调试台。
- 不实现 Harness、复杂规划、Skill、RAG、多 Agent、自动创建项目环境和自动安装初始依赖。

阶段完成需要同时满足：工具正常与失败行为可验证；Web 可完成项目配置、任务提交、澄清继续和结果查看；服务可独立查看及停止；第一阶段兼容回归与本阶段新增回归通过。

## 2. 目录、模块与依赖

### 2.1 目录落位

按职责增加包，不在本阶段把整个工程统一重构为多层目录：

```text
app/
├── tooling/           # 工具协议、注册、调度、参数模型
├── workspaces/        # 文件、检索、路径规则、变更记录
├── execution/         # 命令、Windows 进程树、服务管理
├── verification/      # 检查执行、报告判定、固定验收
├── git_ops/           # 版本快照、差异、验证后提交
├── tasks/             # 项目管理、任务运行、原执行流程提取
├── storage/           # SQLite、事件、产物
├── web/               # FastAPI、HTTP 模型、SSE、应用生命周期
├── composition.py     # 构造运行依赖
├── models.py          # 保留第一阶段公开模型
├── workspace.py       # 第一阶段工作区接口兼容适配
├── checks.py          # 第一阶段检查接口兼容适配
├── persistence.py     # 第一阶段存储接口兼容适配
├── workflow.py        # 第一阶段执行入口兼容适配
└── __main__.py        # 保留第一阶段自动化验收入口
frontend/
└── src/
    ├── api/           # HTTP 客户端、事件客户端、公开类型
    ├── projects/      # 项目配置
    ├── tasks/         # 任务提交、详情、澄清、差异
    └── services/      # 受管服务面板
```

原文件中的实现逐步提取至新包，兼容文件负责转换和委托，不复制维护两套文件、命令或任务执行实现。新包使用 `workspaces`，避免与现有 `workspace.py` 发生同名导入冲突。

### 2.2 功能与模块对应关系

| 模块 | 功能与职责 | 直接依赖 |
| --- | --- | --- |
| 工具注册与调度 | 定义 Schema、校验参数、执行分发、结果统一、调用记录 | 工具参数模型、工作区、执行、验证、Git 只读查询 |
| 工作区与代码检索 | 列举、搜索、读取、创建、局部修改、移动、删除、目录操作，记录任务变更 | 路径策略、变更记录、事件及产物接口 |
| 命令与服务执行 | 一次性命令、长期服务、输出、超时、进程树清理 | Windows 进程封装、脱敏、事件及产物接口 |
| 检查验证 | 原有测试基线、验收固定、真实检查、结果判定 | 命令执行、项目配置、工作区版本及产物 |
| Git 交付 | 记录版本及初始改动、差异、隔离任务修改、验证后提交 | 变更记录、验证证据、Git 执行、事件及产物 |
| 项目与任务管理 | 项目检查与保存、任务位置互斥、模型循环、澄清继续、预算 | 前述能力、状态存储、模型调用 |
| 状态、事件与产物 | 项目、任务和事件持久化，日志及报告索引，线程连接管理 | SQLite、任务独立目录 |
| Web 交互 | HTTP、SSE、页面、服务操作和应用启停 | 项目与任务管理、存储查询、服务管理 |

Web 不执行模型循环或直接修改项目文件；模型不能调用 HTTP 路由来绕开工具调度。工作区、验证和 Git 模块不依赖 FastAPI 或 React。现有模型 SDK 留在任务运行侧，为第三阶段替换执行循环保留清晰入口。

应用装配入口为 `build_services(settings: AppSettings) -> AppServices`，Web 工厂为 `create_app(settings: AppSettings, services: AppServices) -> FastAPI`。AppSettings 保存监听地址、应用数据位置及服务端模型配置引用；AppServices 汇集已构造的项目、任务、存储及进程管理对象，不是对外响应类型。

构造顺序为存储、脱敏和路径规则 → 工作区与进程能力 → Git 与验证 → 工具注册 → 项目和任务管理 → Web。StateStore 只保存连接配置，不长期持有跨线程连接；其余模块通过构造参数接收所需依赖，不在每次调用中重新创建服务管理器。Git 读取验证证据通过存储完成，避免与 VerificationService 形成循环依赖。

## 3. 公共类型与通用约定

### 3.1 类型约定

- 标识由后端生成 UUID 字符串。时间使用 UTC ISO 8601，耗时使用整数毫秒，文件及命令输出限额使用字节。
- 公开请求、响应及工具参数使用 Pydantic，拒绝未声明字段；布尔值不作为整数字段接受。
- 工具路径为相对工作区路径，输出使用正斜杠。项目配置可以包含用户选择的绝对目录和解释器路径。
- JSON 响应不序列化凭据、数据库连接、进程句柄、完整内部执行状态或未脱敏模型消息。
- 下列 Python 签名描述模块公共接口；`ToolContext` 为后端内部对象，不生成模型参数 Schema。

| 类型 | 字段与语义 |
| --- | --- |
| `ToolContext` | `task_id`、`project_id`、`workspace: Path`、`artifact_dir: Path`、`phase`、`attempt: int`、`limits: ExecutionLimits`；关联后端注入的路径策略、脱敏及变更记录能力 |
| `ToolError` | `code: str`、`message: str`、`retryable: bool`；可重试表示修正参数或重新读取后可能成功，不自动触发重试 |
| `ToolResult[T]` | `ok: bool`、`data: T or null`、`error: ToolError or null`、`artifacts: list[ArtifactRef]`、`duration_ms: int`；成功仅有 data，失败仅有 error |
| `ExecutionLimits` | `max_model_requests`、`command_timeout_seconds`、`output_limit_bytes`；新 Web 任务固定最多三轮实现，不在可修改配置中开放实现轮数 |
| `ProjectProfile` | `id`、`name`、规范化 `workspace`、`python_executable`、`pytest_args: list[str]`、`extra_checks: list[CommandSpec]`、`limits`、`revision: int`、`created_at`、`updated_at` |
| `ProjectInspection` | 规范化目录、Git 基线及状态、候选解释器、pytest 可用性、建议 pytest 参数、`ready: bool`、`issues: list[ToolError]` |
| `TaskSnapshot` | `id`、`project_id`、`requirement`、`status`、`phase`、`attempt`、`model_requests`、`summary`、`questions: list[str]`、`commit: str or null`、`last_event_seq`、时间；失败时可带 `error` |
| `TaskEvent` | `task_id`、`seq: int`、`kind: str`、`timestamp`、`payload: dict`；seq 在同一任务中严格递增 |
| `ArtifactRef` | `id`、`task_id`、可选 `service_id`、`kind`、`media_type`、`size_bytes`、`mutable: bool`、`sha256: str or null`、创建时间；公开值不包含磁盘路径 |
| `CommandSpec` | `mode: exec or powershell` 默认 exec、`program: str or null`、`args: list[str]` 默认空数组、`script: str or null`、`cwd: str` 默认 `.`、`timeout_seconds: int or null` 默认继承项目限制、`encoding: str` 默认 UTF-8 |
| `CommandResult` | `returncode`、`timed_out`、`output_truncated`、`stdout_tail`、`stderr_tail`、`duration_ms`、stdout/stderr 产物引用；普通命令成功不等同于任务验证通过 |
| `ServiceSpec` | `name`、`command: CommandSpec`、`url: str or null`；不使用普通命令的总运行超时限制服务存活时间 |
| `ServiceSnapshot` | `id`、`task_id`、`project_id`、`name`、`status`、已脱敏启动信息、`url`、stdout/stderr 产物引用、创建及结束时间、可选退出码；不暴露可操作进程句柄 |
| `CheckOutcome` | 检查种类、命令结果、报告引用、实际用例及跳过标识、错误码、是否通过 |
| `VerificationResult` | `task_id`、`attempt`、本轮全部检查、固定验收版本、已验证工作区指纹、`passed`；只能由验证模块生成并登记 |
| `CommitResult` | `commit`、任务差异产物、交付文件列表、验证依据；失败时通过错误说明实际发布到哪一步 |

`TaskSnapshot.status` 使用 `running`、`needs_clarification`、`succeeded`、`failed`、`no_changes`、`dismissed`、`interrupted`。`phase` 使用 `preflight`、`baseline`、`preparation`、`implementation`、`verification`、`delivery`、`finished`。服务状态使用 `starting`、`running`、`stopping`、`stopped`、`exited`、`failed`、`unknown`。

其余接口结果定义如下，文件正文和命令输出均指已脱敏文本：

| 类型 | 字段 |
| --- | --- |
| `FileEntry`、`FilePage` | 条目为 `path`、`kind: file or directory`、`size_bytes: int or null`；分页为 `entries`、`truncated` |
| `CodeMatch`、`SearchResult` | 匹配为 `path`、`line: int`、`text`；结果为 `matches`、实际观测的 `match_count`、`truncated`，截断时不声称知道完整总数 |
| `FileContent` | `path`、`start_line`、`end_line`、`content`、`sha256`、`encoding`、`truncated` |
| `FileChange` | `operation: create or edit or move or delete`、`path`、`target: str or null`、`before_sha256: str or null`、`after_sha256: str or null`、`changed` |
| `DirectoryChange` | `path`、`operation: create or remove`、`changed` |
| `ProjectInput` | `name`、`workspace`、`python_executable`、`pytest_args`、`extra_checks`、`limits`；标识和 revision 由服务接口管理 |
| `AcceptanceVersion` | `task_id`、工作区内验收 `path`、`sha256`、`backup_artifact_id`、`expected_test_ids`；标识集合在首次有效报告后绑定，不由模型填写 |
| `VerificationDecision` | `passed: bool`、`errors: list[ToolError]`、`verification_artifact_id` |
| `GitSnapshot` | `head`、`index_fingerprint`、`staged_paths`、`unstaged_paths`、`untracked_paths`、`workspace_fingerprint` |
| `GitStatus` | `head`、原有用户改动路径、任务改动路径、当前暂存和未暂存路径、冲突标识 |
| `DiffResult` | `scope`、`text`、变更文件条目 `files`、`truncated`、`artifact: ArtifactRef or null`；文件条目为路径、可选源路径及变更种类 |
| `TaskOutcome` | `status`、`summary`、`questions`、`commit`、`verification_artifact_id`、可选 `error`，由 TaskService 发布到公开状态 |
| `ArtifactRecord` | ArtifactRef 的字段加后端内部 `storage_path`；该路径不得序列化到 HTTP |
| `ArtifactContent` | `artifact: ArtifactRef`、`offset`、`next_offset`、`size_bytes`、`text`、`eof`、`truncated` |

新工具结果与第一阶段形状不相同的部分须显式转换：例如搜索条目转为旧版匹配文本，编辑的 `after_sha256` 转为旧版 `sha256`，错误的 message 转为 reason。不得只去掉 ok 包裹就声称协议完全兼容。

### 3.2 默认值与限制

| 项目 | 第二阶段默认约定 |
| --- | --- |
| Web 监听 | `127.0.0.1:8000`；前端开发时使用本机 Vite 并代理 `/api`，交付时由后端提供构建产物 |
| 应用数据 | `MY_AGENT_HOME` 指定目录，否则使用 `%LOCALAPPDATA%/MyAgent`；不得把应用数据作为模型可读写工作区暴露 |
| 并发 | 单实例、单后端 worker、一个活动任务位置、一个任务后台执行线程；不排队 |
| 实现预算 | Web 编码任务固定三轮；首次实现算一轮，澄清继续不增加新的轮次 |
| 模型调用 | 默认最多 40 次请求，可配置为 1–1000；沿用 45 秒请求超时，不增加隐式 SDK 自动重试 |
| 普通命令 | 默认 120 秒，允许 1–3600 秒；需要更长时间时由项目配置显式指定 |
| 命令输出 | stdout、stderr 各默认最多 1 MiB，可配置为 256 字节至 10 MiB；超限停止该普通命令并返回失败及截断信息 |
| 工具文本读取 | 每次最多 2000 行、20000 字节；返回截断标志；单个可编辑文本文件最多 1 MiB |
| 代码搜索 | 每次最多 200 条、20000 字节，返回截断标志；默认排除 Git 元数据、虚拟环境、依赖缓存、`node_modules` 和敏感配置 |
| 模型命令摘要 | 每路最多返回末尾 8192 字节的已脱敏输出，完整的有界输出登记为产物供网页检查 |
| 服务日志 | 每路使用最多两个 1 MiB 的轮转文件，超限轮转而不因日志量终止服务；公开产物标明是否仍在变化 |
| 记录查询 | 列表默认 20 条，最多 100 条；产物正文每次默认及最多读取 65536 字节 |
| SSE | 每批续读最多 100 条，空闲时约每 250 毫秒查询，15 秒发送一次心跳注释 |

这些是本阶段实现默认值，不改变总需求。第一阶段 CLI 显式传入的验收参数继续由兼容适配保留，不替换为 Web 默认值。

## 4. 工具注册与调度

```python
class ToolRegistry:
    def schemas(self) -> list[dict[str, object]]: ...
    def invoke(
        self, name: str, arguments: dict[str, object], context: ToolContext
    ) -> ToolResult[dict[str, object]]: ...
```

工具注册在应用装配时完成，运行期间不接受模型注册或替换工具。每个工具声明名称、说明、Pydantic 参数模型、执行函数、允许阶段及是否修改项目。Schema 只包含模型可以提供的参数。

调用顺序固定为：查找工具 → 严格校验参数 → 检查任务和阶段 → 校验执行范围 → 记录开始事件 → 执行 → 脱敏并封装结果 → 持久化结束事件。校验失败不产生文件或进程操作。调用不得修改 ToolContext 中的任务身份、预算、工作区或保护范围。

模型可调用的工具为工作区九个操作、`run_command`、`start_service`、`get_service`、`list_services`、`stop_service`、`run_checks`、`git_status` 和 `get_diff`。`freeze_acceptance`、版本提交、项目配置修改、任务位置释放及历史状态恢复不注册为模型工具。

第一阶段模型仍看到原有五个工具及原参数。兼容适配将统一成功结果转换为原数据对象，将错误转换为原 `{error: {code, reason}}`；不把新返回包裹结构强加给既有模型替身和验收契约。

## 5. 工作区、检索与变更记录

### 5.1 公共接口与参数

```python
class WorkspaceService:
    def list_files(self, args: ListFilesArgs, ctx: ToolContext) -> FilePage: ...
    def search_code(self, args: SearchCodeArgs, ctx: ToolContext) -> SearchResult: ...
    def read_file(self, args: ReadFileArgs, ctx: ToolContext) -> FileContent: ...
    def create_file(self, args: CreateFileArgs, ctx: ToolContext) -> FileChange: ...
    def edit_file(self, args: EditFileArgs, ctx: ToolContext) -> FileChange: ...
    def move_file(self, args: MoveFileArgs, ctx: ToolContext) -> FileChange: ...
    def delete_file(self, args: DeleteFileArgs, ctx: ToolContext) -> FileChange: ...
    def create_directory(self, args: DirectoryArgs, ctx: ToolContext) -> DirectoryChange: ...
    def remove_empty_directory(self, args: DirectoryArgs, ctx: ToolContext) -> DirectoryChange: ...
```

| 操作 | 参数 | 输出要点 |
| --- | --- | --- |
| `list_files` | `path="."`、可选 `glob`、`limit=200` | 相对路径、文件或目录类型、大小和截断标志；按路径稳定排序 |
| `search_code` | `pattern`、`path="."`、`literal=false`、`limit=200` | 匹配路径、行号、片段和截断标志 |
| `read_file` | `path`、`start_line=1`、`end_line` 默认起点后 199 行 | 文本、实际行范围、原始字节 SHA-256、编码和截断标志 |
| `create_file` | `path`、`content` | 新文件哈希和变更标识；UTF-8 无 BOM、LF；父目录须已存在，不覆盖任何现存目标 |
| `edit_file` | `path`、`old_text`、`new_text`、`expected_hash` | 前后哈希和变更标识；保留既有编码与未修改部分的换行 |
| `move_file` | `source`、`target`、`expected_hash` | 源、目标和内容哈希；只移动普通文件，不覆盖目标，目标父目录须存在 |
| `delete_file` | `path`、`expected_hash` | 删除标识及原哈希；目标必须为普通文件 |
| `create_directory` | `path` | 允许创建缺失父目录，已存在目录返回未改变；同名文件为冲突 |
| `remove_empty_directory` | `path` | 只删除已存在空目录；拒绝工作区根及非空目录 |

`FileChange` 包含操作、源及可选目标、前后哈希和是否改变；文件完整内容不重复放入变更事件。已有 `edit_file` 的唯一原文匹配及文件版本校验继续保留。

### 5.2 路径、内容与冲突

- 所有操作解析实际目标和已有父目录，处理 Windows 大小写、符号链接及目录联接；解析后越出工作区则拒绝。写入前重新核对路径与文件版本。
- 拒绝模型访问 `.git` 元数据、`.env`、`.env.*`、应用数据目录及其别名。实际搜索和读取结果还需经过已知凭据脱敏。
- 本阶段只编辑文本；拒绝二进制文件及无法可靠识别编码的内容。保留现有 Python 编码声明、UTF-8 BOM、CRLF 及混合换行行为。
- 哈希基于原始完整文件字节。过期哈希、空旧文本、旧文本零次或多次匹配均拒绝写入；不以模糊匹配自动猜测目标。
- 写入采用同目录临时文件和原子替换；创建使用不覆盖语义。含已知凭据的文本不得被模型写入，不能把凭据复制到变更备份或公开产物。
- 已固定验收文件在实现阶段不可修改、移动或删除。操作错误不得因模型随后返回阶段完成而被忽略。

### 5.3 任务变更记录

在第一次修改某路径前记录初始 Git 归属、用户原有改动状态和文件版本；每次成功操作后追加顺序变更记录，包含操作、路径、前后哈希和重建任务变更所需的数据。记录存于后端任务目录，模型不能提供或修改。

初始用户未跟踪文件不能被同名创建覆盖。初始已暂存路径不允许本任务修改、移动或删除；对初始有未暂存改动的跟踪文件，只允许能够与用户内容明确区分的局部修改。涉及该类文件的整文件移动、删除或无法独立重放的替换，直接报告 `USER_CHANGE_CONFLICT`。不通过覆盖、自动暂存或重置用户工作解决冲突。

Git 交付使用该记录处理新增、修改、移动和删除，不能继续假定所有变更都是已有文件的文本替换。

## 6. 命令与服务执行

### 6.1 公共接口

```python
class ProcessManager:
    def run_command(self, spec: CommandSpec, ctx: ToolContext) -> CommandResult: ...
    def start_service(self, spec: ServiceSpec, ctx: ToolContext) -> ServiceSnapshot: ...
    def get_service(self, service_id: str, ctx: ToolContext) -> ServiceSnapshot: ...
    def list_services(self, ctx: ToolContext) -> list[ServiceSnapshot]: ...
    def stop_service(self, service_id: str, ctx: ToolContext) -> ServiceSnapshot: ...
    def shutdown(self) -> None: ...
```

`run_command` 的模型参数为 CommandSpec；`start_service` 为 ServiceSpec；查询和停止使用 `service_id`，列表无模型参数。模型只能查看及停止当前项目中由本应用管理的服务；Web 用户可操作本应用的全部受管服务，不能按外部 PID 操作进程。

### 6.2 执行规则

| 模式 | 参数及执行方式 |
| --- | --- |
| `exec` | `program` 必填、`args` 为字符串数组、`script` 不提供；使用独立程序与参数执行，不拼接 shell 字符串 |
| `powershell` | `script` 为非空脚本，`program` 和 `args` 不提供；后端选择 PowerShell 7，缺失时使用系统 Windows PowerShell，以非交互、无用户 profile 的方式启动 |

`cwd` 默认 `.`，必须位于项目内。`timeout_seconds` 默认继承项目限制，模型只能请求不超过该项目已授权上限的值，不能借工具参数扩大限制。输出编码默认 UTF-8，允许显式指定 `gb18030` 或 `utf-16-le`。解释器路径由配置或程序解析器确定，不能把带参数的一整段字符串当作可执行文件路径。

对 Windows 的 npm、npx 启动包装，优先解析为 Node 与对应 CLI 入口的参数调用；无法可靠解析时返回明确错误并提示显式脚本模式。其他 `.cmd`、`.bat` 不悄悄转换为字符串 shell 调用，要求使用脚本模式。脚本原文记录为已脱敏执行证据，脚本整体退出码不等同项目测试通过。

子进程 stdin 为关闭状态，不等待交互输入。环境继承必要系统配置和选定项目解释器路径，移除应用模型凭据及已知敏感变量；不把后端 `.env` 的全部内容导出给项目程序。命令参数、脚本、异常和输出均须在进入模型、事件、日志或页面前脱敏。

结构化调用中的 Git 写操作不作为通用工具能力开放，任务提交走程序交付接口。任意脚本仍具有当前 Windows 账号权限，不能靠字符串检查建立操作系统沙箱。每次命令后核对 Git HEAD 与暂存区是否被意外改动；发现越过约定交付路径的版本写入时停止自动交付，记录事实，不隐式重置用户仓库。

自动提交只纳入受控文件操作已登记且版本一致的任务变更。普通命令或后台服务额外生成、删除或修改 Git 可见源文件时，必须识别为未归属变化并展示；本阶段不因“由模型启动过该命令”就把这些内容全部当作任务自有改动。不能明确归属时停止自动提交并说明原因，保留证据和用户工作。虚拟环境、依赖缓存等被项目忽略的运行产物不因此进入提交。这是第二阶段自动交付的边界，不影响命令工具本身返回实际执行结果。

### 6.3 Windows 进程树

Windows 封装通过标准库 `ctypes` 调用 Job Objects 和进程 API：先创建 Job 并启用关闭句柄时清理进程的限制，再以挂起状态、隐藏窗口创建目标进程，加入 Job 后恢复执行。不能先放任子进程运行，再仅凭父 PID 尝试关联。

普通命令的 Job 生命周期覆盖该命令，超时或输出超限时结束整个受管进程树，再等待输出读取线程退出并关闭句柄。若进程树管理无法建立，拒绝执行，不降级为只杀父进程。普通命令不用于偷偷保留后台程序，需长期运行的工作使用服务接口。

服务 Job 和句柄由应用级 ProcessManager 持有，不由任务线程退出时释放。只操作仍持有的受管句柄，不依据数据库中的历史 PID 杀进程。[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

### 6.4 输出与服务状态

- stdout、stderr 分别读取和限额，保留有界产物及尾部摘要。已知凭据跨读取块时仍须完整脱敏；使用增量解码和跨块保留区，不能仅逐块独立替换。
- 普通命令超时或输出超限返回失败及实际执行记录。输出截断不能表示为完整输出。
- 服务启动后观察进程树是否立即退出，约 1 秒后仍存活可标为 `running`；该状态只表示受管进程存活，不表示 HTTP 或业务已经就绪。
- 服务访问地址为可选显式值，仅展示本机 HTTP/HTTPS 地址；本阶段不自动抓取地址判断就绪，也不自动打开浏览器。
- 服务日志轮转时记录新产物及截断信息，原产物引用保持可解释；不将无限服务输出逐块无限写入 SQLite。事件保存状态和日志更新引用，刷新提示合并发送。
- 任务成功、失败或放弃后，已登记服务继续由应用管理。`stop_service` 对已停止服务幂等返回；外部进程、其他项目的模型调用或未知服务标识不得成为停止目标。
- 正常关闭应用时先停止接收任务，再等待当前模型请求返回或超时、阻止后续动作，并关闭命令和服务 Job。该关闭清理不是向网页提供运行中取消。
- 重启后不按旧 PID 接管服务；历史运行记录标为 `unknown`，不宣称服务仍运行或已经被停止。

## 7. 项目配置、检查验证与 Git 交付

### 7.1 项目配置接口

```python
class ProjectService:
    def inspect_project(self, workspace: str) -> ProjectInspection: ...
    def save_project(
        self, profile: ProjectInput, project_id: str | None = None,
        expected_revision: int | None = None
    ) -> ProjectProfile: ...
    def get_project(self, project_id: str) -> ProjectProfile: ...
    def list_projects(self, offset: int = 0, limit: int = 20) -> list[ProjectProfile]: ...
```

`ProjectInput` 包含名称、目录、解释器、pytest 参数、额外检查和执行限制，不包含模型密钥、报告路径、测试标识或验收文件位置。创建时 project_id 为空；修改时由路由提供 project_id，并要求预期 revision。PATCH 的未提供字段保留原值，服务端合并后按完整 ProjectInput 校验；显式清空必需字段拒绝，实际发生配置变化才递增 revision。项目正被活动任务使用时拒绝修改。

项目检查规范化目录，确认 Git 仓库及已有 HEAD，按项目 `.venv` 和可用 Python 查找候选环境，检查解释器与 pytest 是否可运行。只读发现项目配置并提出建议；不创建虚拟环境、安装依赖、执行项目测试或改写项目配置文件。

保存项目时复核所选环境。默认 pytest 参数为空，由 pytest 按项目配置发现回归测试；用户可以明确指定相对项目的测试参数，不允许覆盖系统生成的 JUnit 路径或将执行改成仅收集测试。检查操作和每次任务启动都反馈真实可用性，不依赖过期的 ready 标志。

规范化后指向同一工作区的重复项目配置报告冲突。任务创建时保存项目配置快照，后续配置变化不改变已有任务。

### 7.2 验证接口与报告依据

```python
class VerificationService:
    def run_baseline(self, task_id: str) -> CheckOutcome: ...
    def freeze_acceptance(self, task_id: str) -> AcceptanceVersion: ...
    def run_checks(self, task_id: str, attempt: int) -> VerificationResult: ...
    def evaluate(self, result: VerificationResult) -> VerificationDecision: ...
```

新 Web 流程使用如下规则，第一阶段显式 CheckSpec 则继续由兼容适配执行：

1. 后端为任务生成独立产物目录和 `tests/agent_acceptance/<task_id>/test_requirement.py` 验收位置。若目标已存在则报告冲突，不覆盖。
2. 使用所选解释器执行 pytest 和项目回归参数，并注入本次独立 JUnit 报告路径。基线必须实际执行非空测试集，必需测试不得跳过；保存报告中的实际用例标识作为后续回归依据。
3. 模型在准备阶段使用文件工具编写验收。后端固定该文件的字节哈希和受保护版本；验收命令只使用任务专属路径，不继承回归筛选参数。
4. 固定验收先在原始实现上运行。原始实现已经满足时验证所需检查并报告无需修改；任务新建的临时验收文件归档后清理，保持原有工作不变。
5. 否则进入最多三轮实现。每轮实际运行固定验收、原有回归及明确配置的额外检查；验证前后核对保护文件和来源版本。
6. 验收失败、回归缺少原有必需标识、用例跳过、零测试、命令失败、报告无效或保护文件被修改，均不得判为通过。不能只依据命令返回 0 或模型声明。
7. 验证报告绑定本轮工作区指纹。普通命令、文件修改或用户外部修改使该依据过期时，不允许直接提交。

额外检查以其明确配置的命令退出结果判定，不冒充测试覆盖；测试检查仍要求结构化报告。检查不得静默改写源文件；发现检查过程修改待交付内容时，记录原因并重新进入处理，不能沿用先前验证结论。

### 7.3 Git 接口与提交步骤

```python
class GitService:
    def inspect_repository(self, workspace: Path) -> RepositoryInfo: ...
    def snapshot(self, task_id: str) -> GitSnapshot: ...
    def status(self, task_id: str) -> GitStatus: ...
    def diff(self, task_id: str, scope: str = "task") -> DiffResult: ...
    def commit_verified(self, task_id: str) -> CommitResult: ...
```

`GitSnapshot` 保存 HEAD、初始暂存区版本、用户原有跟踪及未跟踪改动、任务涉及路径的初始版本；`GitStatus` 区分这些内容和任务改动。模型只注册 `git_status` 与 `get_diff`，其中差异 scope 可为 `task` 或 `workspace`；Web 默认展示 task 差异，并标明用户原有改动。

`inspect_repository` 供尚无任务标识的项目检查使用，RepositoryInfo 包含规范化目录、是否为仓库、可选 HEAD 和冲突状态。内部只读 Git 查询关闭可选索引刷新，避免自己的查询改变被比较的索引状态。

提交由程序执行：

1. 从后端读取最近有效 VerificationResult，检查全部要求通过、存在实现变更、HEAD 未变化、暂存区没有外部变化、工作区与已验证内容一致。
2. 按顺序变更记录在独立临时索引中构建任务提交树：干净路径可使用对应新内容或删除状态；用户原已有未暂存改动的文件，只重放能与原改动明确区分的任务替换。
3. 初始已暂存路径本任务不能修改；在候选交付索引上重新合并用户原有暂存差异，保证任务完成后用户暂存意图仍保留，不把任务新增提交反向显示为待暂存撤销。
4. 在发布前复核原索引和路径版本，并在 Git 索引锁保护下准备更新。冲突、索引锁占用或无法安全重建时拒绝提交，不使用 reset、clean、stash 等命令处理用户工作。
5. 以开始的 HEAD 为预期旧值更新提交引用，再发布已准备的索引。若发布中发生错误，报告实际 HEAD、索引及生成提交情况，停止自动交付；不能声称成功或盲目回滚其他人的后续变化。
6. 保存任务 diff、验证和提交证据。当前验证对象是包含用户原有改动的工作区时，报告明确说明该事实，不宣称仅检出任务 commit 就经过同样验证。

无需修改不创建空提交。失败、等待放弃或中断保留已有差异，不自动回滚用户项目。模型或脚本对仓库元数据的越界写入只能作为异常被记录和终止交付，不能以当前账户原生执行冒充安全沙箱保证。

## 8. 任务运行、澄清与生命周期

### 8.1 公共接口

```python
class TaskService:
    def start_task(self, project_id: str, requirement: str) -> TaskSnapshot: ...
    def get_task(self, task_id: str) -> TaskSnapshot: ...
    def list_tasks(
        self, project_id: str | None = None, offset: int = 0, limit: int = 20
    ) -> list[TaskSnapshot]: ...
    def answer_clarification(self, task_id: str, answer: str) -> TaskSnapshot: ...
    def dismiss_waiting_task(self, task_id: str) -> TaskSnapshot: ...

class TaskRuntime:
    def start(self, config: RunConfiguration) -> TaskOutcome: ...
    def continue_with_answer(self, task_id: str, answer: str) -> TaskOutcome: ...
```

TaskService 负责互斥和提交后台工作；TaskRuntime 负责提取后的现有执行流程。RunConfiguration 包含项目快照、模型配置引用、验证配置、任务目录和兼容模式，由后端构造，不是新的网页请求格式。

### 8.2 单活动任务

应用生命周期建立一个后台执行线程和进程内互斥位置；同一数据目录使用 Windows 文件锁限制为单应用实例，避免另一个实例错误处理仍活跃的状态。生产运行明确为单 worker，不以多个 Uvicorn worker 共享一个内存锁。

创建任务依次检查项目和模型配置、原子占用位置、保存任务及配置快照、提交后台线程，随后返回 HTTP 202。抢占失败返回 `TASK_BUSY`，不创建排队任务。后台提交本身失败时标记任务失败并释放位置。

待澄清任务保留位置但不持续占用执行线程。补答仅对该任务重新提交后台执行；放弃等待释放位置，不操作运行中的线程。用户刷新或关闭网页不影响后端任务。

### 8.3 状态转换

| 当前状态 | 动作或结果 | 下一状态与副作用 |
| --- | --- | --- |
| 无任务 | 创建且配置检查、互斥成功 | `running`，保存快照并提交线程 |
| `running` | 模型提出有效澄清问题 | `needs_clarification`，保存确切阶段、轮次、问题和消息，保留活动位置 |
| `needs_clarification` | 用户补答且版本核对通过 | `running`，继续原阶段，不重新获取预算 |
| `needs_clarification` | 用户放弃等待 | `dismissed`，保留证据和差异，释放活动位置 |
| `running` | 验证及任务提交完成 | `succeeded`，登记证据并释放位置 |
| `running` | 原始实现已满足且验证完成 | `no_changes`，归档临时验收并释放位置 |
| `running` 或 `needs_clarification` | 检查、协议、执行或版本冲突导致无法继续 | `failed`，保存原因与证据，释放位置 |
| 上次记录为 `running` 或 `needs_clarification` | 应用重新启动 | `interrupted`，保留记录，不恢复运行或继续接收补答 |
| 任意结束状态 | 再次补答或放弃 | 返回 `INVALID_TASK_STATE`，不改变任务 |

空补答返回参数错误。重复或并发补答用状态及版本比较拒绝，不能执行两次。任务额度在开始实现轮次时增加；某轮中提出澄清，补答后仍继续该轮。单纯准备与澄清不作为新的实现轮次，但真实模型请求继续累计。

继续前检查 HEAD、暂存区、相关已读及已改文件版本。发现外部变化时停止继续并记录 `WORKSPACE_CHANGED`，不自动合并或重新执行已完成步骤。未改变的固定验收继续使用原版本，不重新生成以获取更宽松结果。

### 8.4 正常关闭

后端先关闭新任务和补答入口，设置停止继续执行标志；当前工具调用结束或受管普通命令清理后，不再开始下一次文件修改或模型请求。正在等待的模型请求受现有 45 秒超时限制，返回后保存未完成记录。

正在进行的 Git 发布关键区允许完成，不在 HEAD 和索引发布中途强行结束内部 Git 操作。应用随后关闭所有受管服务和命令 Job，等待读写线程收尾，保存任务及日志状态，关闭数据库和单实例锁。已经完成的任务保留真实终态，未完成任务标为 `interrupted`。该过程不增加前端取消接口，也不提供异常崩溃恢复承诺。

## 9. 状态、事件与产物

### 9.1 存储接口

```python
class StateStore:
    def get_project(self, project_id: str) -> ProjectProfile: ...
    def list_projects(self, offset: int, limit: int) -> list[ProjectProfile]: ...
    def save_project(self, profile: ProjectProfile, expected_revision: int | None) -> None: ...
    def create_task(self, task: TaskSnapshot, config: RunConfiguration) -> None: ...
    def get_task(self, task_id: str) -> TaskSnapshot: ...
    def list_tasks(
        self, project_id: str | None, offset: int, limit: int
    ) -> list[TaskSnapshot]: ...
    def load_runtime_state(self, task_id: str) -> dict[str, object]: ...
    def save_runtime_state(self, task_id: str, state: dict[str, object]) -> None: ...
    def transition_task(
        self, task_id: str, expected_status: str, changes: dict[str, object]
    ) -> TaskSnapshot: ...
    def append_event(
        self, task_id: str, kind: str, payload: dict[str, object]
    ) -> TaskEvent: ...
    def events_after(self, task_id: str, seq: int, limit: int = 100) -> list[TaskEvent]: ...
    def register_artifact(self, artifact: ArtifactRecord) -> ArtifactRef: ...
    def list_artifacts(self, task_id: str, offset: int, limit: int) -> list[ArtifactRef]: ...
    def read_artifact(self, artifact_id: str, offset: int, limit: int) -> ArtifactContent: ...
    def save_service(self, service: ServiceSnapshot) -> None: ...
    def get_service(self, service_id: str) -> ServiceSnapshot: ...
    def list_services(
        self, project_id: str | None, task_id: str | None, offset: int, limit: int
    ) -> list[ServiceSnapshot]: ...
```

应用数据目录包含 `state.sqlite3` 和 `runs/<task_id>/`，后者保存检查报告、日志、固定验收副本和任务差异。受管服务日志放在所属任务目录下，但生命周期由应用管理。配置中的模型凭据不写入数据库。

### 9.2 SQLite 记录

所有表使用参数化 SQL，启用 WAL、外键和 5000 毫秒 busy timeout；每个线程或请求创建并关闭自身连接，不把同一连接跨线程共享。

| 表 | 主要字段与约束 |
| --- | --- |
| `projects` | `id` 主键，`workspace_key` 规范化后唯一，`profile_json`，`revision`，创建及更新时间 |
| `web_tasks` | `id` 主键，`project_id` 外键，需求、状态、阶段、轮次、请求数、配置快照 JSON、内部运行状态 JSON、公开摘要 JSON、`next_event_seq`、版本及时间 |
| `web_events` | `task_id` 外键、`seq`、`kind`、时间、已脱敏 `payload_json`；联合主键为任务和序号 |
| `artifacts` | `id` 主键，任务外键、可选服务标识、种类、内部相对存储路径、大小、可变标志、可选哈希及时间 |
| `services` | `id` 主键，所属任务和项目外键、已脱敏启动信息、观察到的状态、日志产物标识、地址和时间；不保存可重新操作的进程句柄 |

状态转换与对应事件在同一事务内完成；事件序号从任务记录原子分配，不采用先查询数量再加一的方式。服务观察与任务执行都可能写事件，不能假设只有一个事件写线程。

保存内部运行状态时，同步更新公开阶段、实现次数和模型请求数；发生变化时发布对应任务状态事件。TaskRuntime 返回 TaskOutcome 后，由 TaskService 的完成处理统一发布等待或终态并管理活动位置，不能让多个入口各自决定任务是否已经结束。

Web 使用新增的记录，不覆盖第一阶段 `tasks`、`events` 历史表。兼容入口继续读写其原 state_dir 和契约，通过适配复用执行能力；本阶段不批量导入旧任务。变更记录、内部模型状态与公开 TaskSnapshot 分开，HTTP 不返回内部 JSON。

### 9.3 事件、SSE 与产物读取

事件类型固定包括 `task_status_changed`、`tool_started`、`tool_completed`、`command_output`、`service_status_changed`、`service_log_updated`、`clarification_requested`、`verification_completed`、`artifact_created`。事件不包含私有推理、凭据或任意未脱敏异常对象。

调用先脱敏、持久化，再对外可见。普通命令输出事件包含有界片段及产物引用；服务输出只合并通知日志更新，避免无限复制日志正文到数据库。超过本任务 10000 条输出提示后，记录一次合并提示并停止新增输出提示，保留状态、错误和交付事件，正文仍按日志限额管理。

SSE 使用 `text/event-stream`：`id` 为事件 seq，`event` 为 kind，`data` 为 TaskEvent JSON。首次读取使用 `after=0` 或已有序号；重连存在 `Last-Event-ID` 时优先使用该值，否则使用查询参数。游标须为不大于当前最新序号的非负整数。心跳使用注释，不分配事件序号。

客户端按 seq 去重。首次打开先读取任务快照，再从 0 续读历史事件；历史事件可以补全日志，但不能把快照状态回退到旧状态。终态任务发送完已有事件后关闭事件流，待澄清状态保持可续读。

终态之后服务可能继续产生信息；服务面板在有受管服务活跃时每 2 秒查询服务状态，并通过日志产物读取展示输出，不依赖已关闭的任务 SSE 永久维持服务监控。

产物读取只接受后端登记的标识；解析并确认实际文件仍位于该任务目录，拒绝路径别名越界。文本读取返回 `ArtifactContent`：`artifact`、`offset`、`next_offset`、`size_bytes`、`text`、`eof`、`truncated`。偏移按字节计算，返回下一个完整 UTF-8 字符边界；无效偏移拒绝，不把文件名或绝对路径作为调用参数。仍在写入或轮转的日志明确标为可变，不把临时哈希当作最终版本证明。

## 10. HTTP 契约

### 10.1 响应约定

成功响应直接返回相应公开类型；列表为 `{items, total, offset, limit}`。错误统一为 `{error: {code, message, retryable}}`，不返回堆栈、请求原始凭据或内部目录。异步任务接受成功使用 202；执行后失败反映在任务状态及事件中，不把已接受请求事后解释为 HTTP 成功完成。

| 方法与路由 | 请求 | 成功响应 | 主要错误 |
| --- | --- | --- | --- |
| `POST /api/projects/inspect` | `{workspace}` | 200 `ProjectInspection`；检查不就绪时返回 ready=false 及问题 | 参数错误、目录不可访问 |
| `GET /api/projects` | offset、limit | 200 项目列表 | 无效分页 |
| `POST /api/projects` | ProjectInput | 201 `ProjectProfile` | 环境未就绪、目录重复 |
| `GET /api/projects/{id}` | 无 | 200 `ProjectProfile` | 项目不存在 |
| `PATCH /api/projects/{id}` | 可修改配置及 `expected_revision` | 200 新版本 `ProjectProfile` | 版本冲突、活动任务占用、环境未就绪 |
| `POST /api/tasks` | `{project_id, requirement}` | 202 `TaskSnapshot` | 忙碌、项目不存在、环境或模型配置无效 |
| `GET /api/tasks` | 可选 project_id、offset、limit | 200 任务列表，按创建时间倒序 | 无效分页或项目 |
| `GET /api/tasks/{id}` | 无 | 200 `TaskSnapshot` | 任务不存在 |
| `POST /api/tasks/{id}/answers` | `{answer}` | 202 `TaskSnapshot` | 非待澄清状态、空回答、外部版本变化 |
| `POST /api/tasks/{id}/dismiss` | 空对象 | 200 `TaskSnapshot` | 非待澄清状态；不能用于运行中任务 |
| `GET /api/tasks/{id}/events` | after，及可选重连头 | 200 SSE | 任务不存在、无效游标 |
| `GET /api/tasks/{id}/diff` | scope 默认 task，可选 workspace | 200 `DiffResult`，包含文本、文件摘要、产物及截断标志 | 任务不存在、差异不可读 |
| `GET /api/tasks/{id}/artifacts` | offset、limit | 200 产物列表 | 任务不存在 |
| `GET /api/artifacts/{id}` | offset=0、limit=65536 | 200 `ArtifactContent` | 不存在、非法偏移、实际路径越界 |
| `GET /api/services` | 可选 project_id、task_id、offset、limit | 200 当前及历史受管服务列表 | 无效过滤或分页 |
| `GET /api/services/{id}` | 无 | 200 `ServiceSnapshot` | 服务不存在 |
| `POST /api/services/{id}/stop` | 空对象 | 200 `ServiceSnapshot`；已结束时幂等返回 | 不存在或句柄状态异常 |

需求和补答为去除首尾空白后的非空字符串，最多 20000 字符。ProjectInput 的 pytest 参数是数组，不接收待拆分的命令字符串。项目限制不允许新增未定义字段，也不提供实现轮数配置。

不增加 `POST /api/tools/invoke`、任意命令 HTTP 接口、任务运行中取消、暂停或恢复接口。程序内部有工具接口不代表把它直接开放给网页。

### 10.2 错误分类

| 类别 | 错误码示例 | HTTP 语义 |
| --- | --- | --- |
| 参数或配置无效 | `INVALID_ARGUMENTS`、`ENVIRONMENT_NOT_READY`、`MODEL_CONFIG`、`PROGRAM_NOT_FOUND` | 422 |
| 授权与保护 | `OUTSIDE_WORKSPACE`、`PROTECTED_PATH`、`PROTECTED_ACCEPTANCE`、`CREDENTIAL_PROTECTION` | 403 |
| 资源不存在 | `PROJECT_NOT_FOUND`、`TASK_NOT_FOUND`、`SERVICE_NOT_FOUND`、`ARTIFACT_NOT_FOUND`、`FILE_NOT_FOUND` | 404 |
| 状态或版本冲突 | `TASK_BUSY`、`INVALID_TASK_STATE`、`REVISION_CONFLICT`、`PROJECT_CONFLICT`、`PROJECT_IN_USE`、`STALE_FILE`、`NON_UNIQUE_MATCH`、`WORKSPACE_CHANGED`、`USER_CHANGE_CONFLICT`、`GIT_BASE_CHANGED`、`GIT_INDEX_CHANGED` | 409 |
| 游标或范围无效 | `INVALID_CURSOR`、`INVALID_OFFSET` | 400 |
| 执行或检查失败 | `COMMAND_TIMEOUT`、`OUTPUT_LIMIT_EXCEEDED`、`EXECUTION_FAILED`、`REPORT_INVALID`、`REPORT_MISSING`、`EXPECTED_TESTS_MISSING`、`REQUIRED_TEST_SKIPPED`、`VERIFICATION_STALE`、`PROCESS_CONTROL_UNAVAILABLE` | 对已启动任务写入失败结果；直接操作服务时执行失败为 500 |
| 未预期错误 | `INTERNAL_ERROR` | 500，返回脱敏摘要 |

模型工具使用相同错误码并封装为 ToolResult，不把 HTTP 状态码作为工具协议。可修复的参数或过期读取可标记 retryable=true；授权拒绝、固定验收保护及预算耗尽不能通过自动重试绕过。

### 10.3 监听、来源与凭据

仅监听回环地址，限制 Host 为本机配置地址，不使用通配来源。修改操作要求 JSON 内容及受允许的网页 Origin；开发前端通过代理保持同源，不将 CORS 开放给任意网站。本阶段不引入多用户登录系统。

模型配置继续从服务端环境及应用启动目录的 `.env` 读取，环境变量优先；不从网页项目配置接收任意 env_file 或 API key。项目表单和 GET 响应不包含凭据，项目程序的环境也不自动获得模型凭据。

## 11. React 页面与交互接口

最小 Web 为单页工作台，不引入代码编辑器或全局状态管理库。使用 React 状态和明确的 HTTP 客户端组织三块主视图及服务面板：

| 视图 | 输入及操作 | 行为 |
| --- | --- | --- |
| 项目配置 | ProjectProfile、ProjectInspection；检查、保存 | 输入目录并检查，选择解释器、修改 pytest 参数和限制，显示不就绪原因；内部报告及测试标识不出现为表单项 |
| 任务提交 | 项目及需求；提交 | 确认 ready 后提交，忙碌时明确提示，不显示排队；不自动重试重复 POST |
| 任务详情 | TaskSnapshot、事件、diff、产物；回答或放弃澄清 | 展示阶段、预算、调用和结果；仅待澄清时出现补答及放弃按钮，不出现运行中取消或暂停 |
| 服务面板 | ServiceSnapshot；查看和停止 | 任务结束后仍可查看服务；状态注明进程运行不等同服务就绪，停止后更新，不按任意 PID 操作 |

前端客户端提供 `inspectProject`、`saveProject`、`getProjects`、`createTask`、`getTask`、`getTasks`、`answerTask`、`dismissTask`、`subscribeTaskEvents`、`getTaskDiff`、`getArtifacts`、`readArtifact`、`getServices`、`stopService`，字段与第 10 节公开契约一致。

SSE 管理重连及序号去重，页面卸载时关闭订阅，但不结束任务。日志和 diff 以文本显示，不把模型、项目或命令输出作为 HTML 执行。前端不持有后端模型密钥。检查失败、报告读取失败或提交失败分别展示，不合并成笼统成功状态。

## 12. 第一阶段兼容方式

- 保留 `python -m app --request ... --report ...` 的已有自动化验收契约、退出码、报告字段和五个模型工具参数。
- 兼容入口将 AgentRequest 转为内部 RunConfiguration，保留请求给定的检查命令、预期标识、显式实现预算及受控暂停测试参数，不套用新 Web 的 pytest 自动管理规则。
- 旧 `WorkspaceTools`、`CheckRunner`、`TaskStore` 和 `AgentWorkflow` 保留所需公开接口，委托新核心；已有测试不因目录整理而被删除或放宽。
- 第一阶段未跟踪文件保护、已知编码、固定验收、测试报告、预算、恢复及提交隔离行为继续作为回归。兼容入口的受控恢复不作为新 Web 已实现恢复的依据。
- Web 无 `pause_after_attempt`、任意 state_dir、env_file、原始 checks 或 API key 输入。内部兼容适配不作为新增产品功能。
- 报告和旧任务证据保留，不把历史未完成任务转换为可继续的 Web 任务，也不在应用启动时改写其他 state_dir 中的旧数据库。

## 13. TDD、回归与实施顺序

本节描述我们开发第二阶段代码的方法，不约束 Agent 在用户任务中只能使用模拟模型，不新增逐步审批要求。本次编写详细设计仅检查文档，不运行以下产品场景。

### 13.1 已确定的公共测试入口

| 入口 | 验证方式 | 不采用的替代方式 |
| --- | --- | --- |
| ToolRegistry.invoke | 真实临时文件、仓库、受管进程和工具结果 | 直接测试私有辅助方法，或模拟文件修改和进程结果来证明工具成功 |
| ProjectService、TaskService | 可控模型响应配合真实配置、存储、检查和 Git | 直接改数据库字段后宣称任务流程成立 |
| HTTP | FastAPI 测试支持，真实应用生命周期、状态和事件契约 | 用内部调用结果代替 HTTP 行为 |
| React 及浏览器 | Vitest、React Testing Library、Playwright；浏览器关键流程贯通后端与本地模型替身 | 只测试组件内部状态或只检查按钮存在 |
| 第一阶段兼容入口 | 保留原 CLI 请求、模型替身、真实行为及报告校验 | 跳过原测试或修改断言迎合重构 |

### 13.2 场景与接口映射

| 编号 | 场景 | 主要入口 | 可观察结果 |
| --- | --- | --- | --- |
| P2-01 | 未知工具、非法类型、额外参数、伪造工作区 | ToolRegistry.invoke | 明确错误，无文件或进程副作用 |
| P2-02 | Windows 中文、空格、大小写、越界及目录联接 | 工作区工具 | 合法路径成功，实际越界拒绝，保护对象保持不变 |
| P2-03 | UTF-8 BOM、CRLF、混合换行及 Python 编码声明 | read_file、edit_file | 修改结果正确，未修改内容和编码保持 |
| P2-04 | 过期哈希、零次或多次匹配、现存目标 | 文件修改工具 | 返回冲突，目标内容不变 |
| P2-05 | 新建、移动、删除和空目录操作 | 文件工具及 get_diff | 任务记录和真实文件结果一致，不递归删除目录 |
| P2-06 | 普通参数命令、PowerShell 组合脚本、路径空格 | run_command | 参数边界正确，退出码及已脱敏输出真实 |
| P2-07 | 非零退出、超时、大量输出和多级子进程 | run_command | 真实失败可见，整个受管树清理，输出有界 |
| P2-08 | 凭据跨输出块、异常或脚本展示 | 工具及产物 HTTP | 页面、模型结果和产物不出现已知凭据 |
| P2-09 | 服务继续预览、停止、立即退出、应用关闭 | 服务接口、应用生命周期 | 任务结束后可查看和停止，关闭清理，不误停外部服务 |
| P2-10 | 无仓库、解释器缺失、pytest 缺失、重复目录 | 项目接口 | ready 和错误真实，不创建环境或开始编码 |
| P2-11 | 项目 revision 冲突及活动任务中修改配置 | 项目 HTTP | 明确拒绝，原配置和任务快照不变 |
| P2-12 | 并发提交、运行中再提交及等待澄清占位 | TaskService、HTTP | 仅一个活动任务，无隐式排队 |
| P2-13 | 准备或实现阶段澄清、补答、重复补答 | 任务接口 | 同任务继续、原阶段保留、累计轮次和请求数不重置 |
| P2-14 | 放弃等待和运行中错误调用放弃接口 | 任务 HTTP | 仅等待任务可放弃，保留差异，运行任务不被终止 |
| P2-15 | 补答前用户修改文件或版本基线 | 任务接口 | 发现冲突，不覆盖或重新获取预算 |
| P2-16 | pytest 标识自动建立、独立报告和固定验收 | 验证接口、任务入口 | 无需用户填写测试标识；报告与当前执行对应 |
| P2-17 | 零测试、跳过、缺失或损坏报告、验收被篡改 | 验证接口、任务入口 | 不判成功，保存真实失败依据 |
| P2-18 | 验证后再改文件或索引、HEAD 被外部修改 | commit_verified | 拒绝沿用过期验证或夹带新变化 |
| P2-19 | 用户原有未暂存改动、无关暂存项及未跟踪文件 | 任务入口、Git 查询 | 用户工作保留，提交仅包含可隔离任务变更 |
| P2-20 | 新增、移动、删除文件的交付与暂存区保护 | Git 交付接口 | 提交树正确，用户暂存意图保留，冲突不靠重置解决 |
| P2-21 | 无需修改或 Git 提交失败 | 任务入口、HTTP | 无空提交，不以模型声明覆盖实际失败 |
| P2-22 | SSE 断线、重连、重复事件及旧历史状态 | 事件接口、浏览器 | 序号连续可追踪，去重，页面状态不回退 |
| P2-23 | 猜测产物路径、非法偏移及日志轮转 | 产物 HTTP | 只读取登记产物，越界拒绝，可变内容不伪装为固定版本 |
| P2-24 | 应用重启后的未完成任务及历史服务 | HTTP、应用生命周期 | 任务中断且不自动恢复，不按旧 PID 操作进程 |
| P2-25 | 项目配置到自然语言任务、补答、diff 和结果 | Playwright | 用户无需 CLI 或内部配置完成流程，不出现代码编辑和运行中取消入口 |
| P2-26 | JS/TS 文件与 Node/npm 普通命令 | 工具公共入口 | 通用操作可用，不据此宣称完整 JS/TS 编码任务已交付 |
| P2-27 | 第一阶段已有行为 | 原 CLI 验收入口 | 原请求、工具协议、报告、预算与恢复场景兼容 |

### 13.3 纵向实施次序

1. 先确认第一阶段可重复的行为基线，记录环境限制和真实缺陷。
2. 从工具注册到一次读取、一次修改的完整行为切片开始；每次先失败测试，再最小实现，再保留回归，逐个补齐文件操作。
3. 以普通命令和一个受管服务分别推进完整切片，加入 Windows 进程树、超时、输出和生命周期回归。
4. 提取验证与交付，覆盖报告、当前内容绑定、新增移动删除以及用户工作保护，同时持续验证原 CLI 兼容。
5. 加入项目保存和单任务执行，再逐个加入澄清、持久化事件、产物、SSE 和 HTTP 操作。
6. 按真实用户流程逐步完成前端，最后贯通浏览器、后端、本地模型替身及真实临时项目。

不先批量写完所有场景再统一实现。修复缺陷先补可重现用例；测试通过后整理实现并保持回归。开发阶段按 [AGENTS.md](../../AGENTS.md) 对代码变更验证并提交，不通过删除、跳过或放宽测试制造完成结论。

## 14. 阶段交付标准

- 本文八类模块的公共行为有对应实现和测试，工具 Schema 与实际参数校验一致。
- Web 能完成已就绪 pytest 项目的保存、任务提交、同任务澄清继续、日志和 diff 查看；系统生成内部验证配置。
- 文件及命令操作处理正常与失败情况，Windows 进程树和服务生命周期在本机得到验证。
- 每个任务最多三轮实现，实际验证后程序提交；用户已有工作、任务预算和事实证据保持可追踪。
- 单活动任务约束生效，应用重启不自动继续任务，网页不出现本阶段未提供的取消、暂停或恢复能力。
- 本阶段新增回归和第一阶段兼容回归通过；真实模型联调与确定性回归分别记录，不用文档检查证明产品功能完成。
- 代码质量、前端类型及构建检查完成；实现与验证记录能够对应具体 Git commit。

本次落盘仅包含详细设计文档，不修改总需求、概要设计或运行代码，不安装依赖、不创建上述运行数据目录，也不执行产品行为场景。
