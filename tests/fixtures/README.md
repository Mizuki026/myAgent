# 第一阶段验收数据

## 示例项目

`projects/average_project/` 是独立的小型 Python 项目：

- `sample_stats/statistics.py`：目标数字平均值函数，原始实现对空列表抛出异常。
- `sample_stats/summary.py`：现有调用方，验证修改没有破坏原有格式化行为。
- `sample_stats/text.py`：同名但无关的函数，用于检验代码检索与修改范围。
- `tests/test_statistics.py`：六项原有回归测试，原始实现全部通过。

验收时将项目复制到临时目录并初始化独立 Git 仓库，保留此处的原始样例。

本目录通过 `.gitattributes` 固定 LF 换行，保证 Windows 与其他平台检出后的样例哈希和模型脚本一致。

## 需求与独立判定

- [明确需求](requirements/average_empty.json)：空列表返回 `None`，其余数字行为保持不变。
- [歧义需求](requirements/ambiguous_average.json)：应先询问用户明确异常输入的语义。
- [需求判定测试](checks/average_acceptance.py)：根据需求中的明确期望编写，不从被测实现推导结果。

独立判定测试在原始样例上应失败于空列表用例，在满足需求的实现上应通过。它与 Agent 自行生成、固定的验收测试分别保存，用于核对最终代码的真实行为。

上述数据不包含真实 API 凭据，确定性测试不需要调用 DeepSeek 服务。

## 模型响应与命令探针

- [实现候选](model_outputs/implementations.json)：正确实现，以及空列表返回 0、始终返回 None、计算错误、修改输入等错误候选。
- [真实测试执行器](commands/run_suite.py)：运行示例项目的 unittest 测试并生成 JUnit XML，供 Agent 读取与核验。
- [异常报告探针](commands/report_fault.py)：生成零测试、跳过、缺失、损坏和原有失败等报告场景。
- [进程探针](commands/process_probe.py)：产生受控的大量输出，或启动子进程持续写入心跳，用于验证超时与进程清理。

`projects/average_project/checks/test_requirement.py` 是待写入的验收文件占位符。模型替身只返回工具请求，实际生成、固定和保护该验收文件应由被测 Agent 完成。

命令探针只作用于测试临时目录。超时场景另设测试侧兜底清理，避免 Agent 尚未正确清理时留下测试进程；是否通过仍取决于兜底清理之前 Agent 的实际行为。
