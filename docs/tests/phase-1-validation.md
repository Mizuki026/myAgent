# 第一阶段验收测试验证记录

日期：2026-11-01。环境：Windows、Python 3.13.12、uv 0.12.4。

## 1. 当前结论

第一阶段 CLI 与任务闭环已实现。行为验收调用真实 CLI、文件工具、命令执行器、SQLite 状态存储和 Git 提交；模型端由本地 HTTP 替身控制，不调用 DeepSeek 服务。

`tests/generated_acceptance` 是 Coding Agent 为用户任务生成验收测试的位置，不属于 Agent 自身的开发测试集合；pytest 配置会排除该目录。

## 2. 验证结果

| 验证项 | 结果 |
| --- | --- |
| Agent 行为验收 A01–A14 | 29 项通过 |
| 文档、样例、模型替身及测试工具自检 | 18 项通过 |
| Agent 工作区工具自测 | 6 项通过 |
| pytest 开发测试集合 | 53 项通过，56 个子测试通过 |
| Ruff 检查与格式验证 | 通过 |
| uv 锁文件一致性 | 通过 |
| DeepSeek 实际 API 调用 | 未执行，离线验收不需要真实凭据 |

行为验收覆盖修改预算、无修改结果、澄清、受控编辑、越界路径、进程超时和输出限制、固定验收保护、测试报告判定、任务恢复、提交隔离及凭据不泄漏。

## 3. 复现命令

项目环境和锁文件由 uv 管理。运行代码检查：

```powershell
uv --cache-dir .uv-cache run --offline ruff check app tests/test_agent_workspace.py
uv --cache-dir .uv-cache run --offline ruff format --check app tests/test_agent_workspace.py
uv --cache-dir .uv-cache lock --check --offline
```

运行文档、样例和工具自检：

```powershell
python -B -m unittest tests.test_product_docs tests.test_acceptance_fixtures tests.test_acceptance_harness tests.test_agent_workspace -v
```

运行 Agent 行为验收：

```powershell
python -B -m unittest discover -s tests/acceptance -t . -p 'test_*.py' -v
```

运行全部 Agent 自测与行为验收：

```powershell
python -B -m pytest -q
```

测试命令应使用 Python 3.13。此次 pytest 使用 Python 3.13.12 运行器，并从 uv 创建的 `.venv` 加载项目依赖；行为验收通过 `AGENT_ACCEPTANCE_COMMAND` 启动 `.venv` 中的 CLI。普通环境下可直接用已激活的项目环境运行以上命令。完整需求、场景和报告格式见 [验收说明](phase-1-acceptance.md) 与 [CLI 契约](cli-contract.md)。
