# 第一阶段 CLI 验收契约

日期：2026-10-31。用户已确认通过 CLI 的 JSON 输入和报告验收，并允许本次 Agent 尚未实现时行为测试处于 RED 状态。

本契约固定测试可见的输入、结果和模型替身协议。内部模块、框架、数据库表结构和提示词组织可自行实现；已有实现也可通过薄 CLI 适配器接入。

## 1. 入口与退出码

默认命令：

```powershell
python -B -m app --request request.json --report report.json
```

测试通过环境变量 `AGENT_ACCEPTANCE_COMMAND` 指定其他命令前缀，值为 JSON 字符串数组，例如：

```powershell
$env:AGENT_ACCEPTANCE_COMMAND = '["python", "-B", "-m", "my_agent"]'
```

测试在临时目录运行 CLI，将仓库目录加入 `PYTHONPATH`。命令前缀应使用可导入模块或绝对脚本路径。测试只追加 `--request` 和 `--report` 参数，不经 shell 拼接执行。

| 报告状态 | 退出码 | 含义 |
| --- | --- | --- |
| `succeeded` | 0 | 修改后的代码通过全部检查，并创建 commit |
| `no_changes` | 0 | 原始实现已满足需求，验证完成且无交付变更 |
| `failed` | 1 | 验证失败、预算耗尽或工具操作失败 |
| `needs_clarification` | 2 | 需要用户明确行为语义，实现尚未修改 |
| `paused` | 3 | 在指定检查点保存状态，供新进程继续执行 |

即使失败也必须写出 JSON 报告。CLI 不存在、崩溃或未生成报告时，验收直接失败。

## 2. JSON 请求

请求字段如下，完整构造方式见 [验收支持代码](../../tests/acceptance/support.py)。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `schema_version` | 整数 | 固定为 1 |
| `operation` | 字符串 | `run` 或 `resume` |
| `task_id` | 字符串 | 稳定任务标识，恢复执行时保持一致 |
| `workspace` | 字符串 | 目标 Git 项目绝对路径 |
| `state_dir` | 字符串 | 数据库、事件、补丁、日志和报告的保存目录 |
| `env_file` | 字符串 | 本次任务使用的配置文件；验收中为临时测试配置 |
| `requirement` | 字符串 | 原始自然语言需求 |
| `model` | 对象 | `base_url`、`name`、`api_key_env`、`stream` |
| `limits` | 对象 | 累计实现次数、API 请求数、命令超时秒数、每路输出字节上限 |
| `checks` | 数组 | 验收与原有测试命令、报告格式和预期测试标识 |
| `acceptance_paths` | 字符串数组 | 实现修改前需要生成、固定和保护的验收文件 |
| `pause_after_attempt` | 可选整数 | 指定累计尝试完成并验证后暂停，用于可复现的任务恢复验收 |

`limits` 使用 `max_implementation_attempts=3`、`max_model_requests=40`、`command_timeout_seconds=2`、`output_limit_bytes=4096`。这些是验收环境参数，产品默认值可以另行配置。三次预算在恢复时累计，不能变为新的三次。

每个 `checks` 条目包含：

- `name`：`regression` 或 `acceptance`。
- `argv`：参数数组，其中 `{report}` 由 Agent 替换为本次命令独立的绝对报告路径。
- `report_format`：本阶段为 `junit_xml`。
- `expected_test_ids`：必须实际执行的 `classname.name` 标识集合。

命令在 `workspace` 下执行。每次验证使用新的报告路径，不能复用上次成功报告。原有回归测试先记录基线；验收文件生成固定后，先对原始实现执行，再开始修改实现。

`no_changes` 场景仍需生成并执行验收；临时验收文件与证据可归档到 `state_dir`，工作区恢复原样，不因生成测试而误判为需要修改实现。

## 3. 可控模型替身

本地 HTTP 服务只模拟外部模型的响应，绝不代替 Agent 读写文件、执行工具、运行测试、存储状态或提交 Git。

- 接口为非流式 `POST /v1/chat/completions`，也接受 `/chat/completions`。
- 请求使用标准 `messages` 与函数工具调用，响应提供 `choices[0].message`、`finish_reason` 和 `usage`。
- 校验测试凭据的 Bearer 请求头；凭据只能用于认证，不能出现在提示词或任务产物中。
- 响应按场景脚本逐条提供；脚本耗尽后再请求会明确报错，用于暴露多余的实现请求。
- 完整请求记录由测试侧持有，用于核对真实模型调用及失败反馈。

外部协议格式依据 [DeepSeek Chat Completions 官方文档](https://api-docs.deepseek.com/api/create-chat-completion/)。当前验收不覆盖网络故障重试、流式响应和真实模型质量。

模型可调用的工具参数固定为：

| 工具 | 参数 |
| --- | --- |
| `search_code` | `pattern` |
| `read_file` | `path`、`start_line`、`end_line` |
| `edit_file` | `path`、`old_text`、`new_text`、`expected_hash` |
| `run_checks` | `scope`，可为 `all` |
| `get_diff` | 空对象 |

`expected_hash` 是修改前整个文件原始字节的 SHA-256；`old_text` 是需要唯一匹配的局部原文，不要求覆盖整个文件。原文不存在或出现多次均拒绝替换。

模型工具调用结束后，最终 `message.content` 为 JSON 字符串，使用以下阶段结果：

| `kind` | 其他字段 | 程序后续行为 |
| --- | --- | --- |
| `acceptance_ready` | 可选 `summary` | 固定验收版本，在原始实现上验证，再进入实现或报告无需修改 |
| `implementation_ready` | 可选 `summary` | 运行真实检查，据结果结束或进入下一次修复 |
| `needs_clarification` | `questions` 数组 | 输出澄清问题，保持实现不变 |

阶段结果仅表示模型这一轮输出结束；即使 `summary` 声称全部通过，程序仍必须验证。工具失败不能因阶段结束消息而被覆盖为成功。

同一实现轮次可能包含工具调用及结束消息等多次 HTTP 请求。修改次数按实现轮次计算，完整 HTTP 请求数量另外受总请求预算约束。

## 4. JSON 报告与公开证据

| 字段 | 含义 |
| --- | --- |
| `schema_version`、`task_id` | 与请求一致 |
| `status` | 本文规定的结果状态 |
| `implementation_attempts` | 已使用的累计实现轮次，范围 0–3 |
| `summary` | 人可读的结果或失败原因 |
| `questions` | 需要澄清时的问题数组 |
| `commit` | 成功变更的完整提交标识；失败、暂停、澄清或无需修改时为 `null` |
| `events` | 按 `seq` 排序的公开事件数组 |
| `checks` | 每次真实执行的命令及测试报告引用 |
| `artifacts` | `kind` 与绝对 `path` 组成的产物数组 |

必要事件包括 `acceptance_frozen` 和 `implementation_changed`。固定事件包含 `sha256`，且序号先于任何实现变更。工具事件提供 `tool`、适用时的 `path`；失败事件提供 `code` 与可读原因。恢复时可通过公开报告观察此前记录，不要求测试查询 SQLite 私有表。

每项命令执行记录至少包含：`name`、`attempt`、`argv`、`cwd`、`returncode`、`timed_out`、`output_truncated`、`report_path`、`stdout_path`、`stderr_path`。原始实现的执行使用 `attempt=0`，后续验证使用相应实现次数。

`diff`、`events`、`verification` 三类产物均需要保存。测试会独立读取最终 JUnit XML、重新运行目标代码，并核对 Git 提交、工作区及产物中的凭据泄漏。

| 错误码 | 场景 |
| --- | --- |
| `INVALID_ARGUMENTS` | 工具参数不合法 |
| `STALE_FILE` | 文件哈希不匹配 |
| `NON_UNIQUE_MATCH` | 原文匹配次数不是 1 |
| `OUTSIDE_WORKSPACE` | 解析后的路径越界 |
| `COMMAND_TIMEOUT` | 命令超时 |
| `PROTECTED_ACCEPTANCE` | 尝试削弱、删除或跳过固定验收 |
| `EXPECTED_TESTS_MISSING` | 零测试或缺少预期用例 |
| `REQUIRED_TEST_SKIPPED` | 必需测试被跳过 |
| `REPORT_MISSING`、`REPORT_INVALID` | 报告不存在或无效 |
| `BASELINE_FAILED` | 原有测试基线已失败 |
| `FILE_NOT_FOUND`、`IO_ERROR` | 参数合法但实际文件操作失败 |

## 5. 恢复与用户已有改动

恢复场景先以 `pause_after_attempt=2` 在验证完成后退出，再以同一 `task_id` 和 `state_dir` 启动新进程并设置 `operation=resume`。累计次数为 2 时最多剩一次实现；验收版本及前两轮证据必须保留。受控暂停用于稳定重现持久化恢复，强制结束进程或断电后的崩溃恢复属于后续扩展测试。

用户已有改动需保留，包括目标文件内的未提交内容和无关未跟踪文件。Agent 的 commit 只包含本任务的变更，用户原有改动继续留在工作区。
