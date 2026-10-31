# Agent 行为验收

场景、需求和预期结果见 [第一阶段验收说明](../../docs/tests/phase-1-acceptance.md)。示例项目、需求及独立判定数据见 [fixtures](../fixtures/README.md)。

当前状态：用户已确认 CLI JSON 验收入口，并允许 Agent 尚未实现时行为验收处于 RED 状态。[CLI 契约](../../docs/tests/cli-contract.md) 定义输入、报告、退出码和本地模型替身协议。

规划覆盖的行为包括：首次实现与两次修复、无需修改、需求澄清、受控文件修改、命令超时、验收版本保护、真实测试结果判定、任务恢复和交付证据。

## 运行行为验收

在仓库根目录执行：

```powershell
python -B -m unittest discover -s tests/acceptance -t . -p 'test_*.py' -v
```

默认启动 `python -B -m app`，也可用 `AGENT_ACCEPTANCE_COMMAND` 指定命令前缀。测试调用真实 CLI，使用临时 Git 项目及本地 HTTP 模型替身；不会代替 Agent 完成实现工作。CLI 尚未实现时测试明确失败，不标记为跳过或预期失败通过。

## 运行测试工具自检

在仓库根目录执行：

```powershell
python -B -m unittest tests.test_product_docs tests.test_acceptance_fixtures tests.test_acceptance_harness -v
```

该命令验证文档、样例、独立需求判定、JUnit 生成、模型替身、CLI 失败处理及超时探针清理。它不证明 Agent 行为通过验收，不会调用真实 DeepSeek 或读取真实 `.env`。

行为测试使用标准库 `unittest`，不需要安装第三方测试依赖。完整运行 `tests/` 会同时包含自检和行为验收，尚未实现时整体退出码为非零。
