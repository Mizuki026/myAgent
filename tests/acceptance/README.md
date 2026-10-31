# Agent 行为验收

场景、需求和预期结果见 [第一阶段验收说明](../../docs/tests/phase-1-acceptance.md)。示例项目、需求及独立判定数据见 [fixtures](../fixtures/README.md)。

当前状态：待确认 Agent 的公开验收入口，以及本次是否允许尚未实现的行为验收处于 RED 状态。确认后在本目录补充对应的可执行行为测试。

规划覆盖的行为包括：首次实现与两次修复、无需修改、需求澄清、受控文件修改、命令超时、验收版本保护、真实测试结果判定、任务恢复和交付证据。

## 已可运行的自检

在仓库根目录执行：

```powershell
python -B -m unittest discover -s tests -p 'test_*.py' -v
```

当前命令只验证文档、样例项目和独立需求判定数据。它不证明 Agent 行为通过验收，也不会调用 DeepSeek 或读取真实 `.env`。
