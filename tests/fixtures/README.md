# 第一阶段验收数据

## 示例项目

`projects/average_project/` 是独立的小型 Python 项目：

- `sample_stats/statistics.py`：目标数字平均值函数，原始实现对空列表抛出异常。
- `sample_stats/summary.py`：现有调用方，验证修改没有破坏原有格式化行为。
- `sample_stats/text.py`：同名但无关的函数，用于检验代码检索与修改范围。
- `tests/test_statistics.py`：六项原有回归测试，原始实现全部通过。

验收时将项目复制到临时目录并初始化独立 Git 仓库，保留此处的原始样例。

## 需求与独立判定

- [明确需求](requirements/average_empty.json)：空列表返回 `None`，其余数字行为保持不变。
- [歧义需求](requirements/ambiguous_average.json)：应先询问用户明确异常输入的语义。
- [需求判定测试](checks/average_acceptance.py)：根据需求中的明确期望编写，不从被测实现推导结果。

独立判定测试在原始样例上应失败于空列表用例，在满足需求的实现上应通过。它与 Agent 自行生成、固定的验收测试分别保存，用于核对最终代码的真实行为。

上述数据不包含真实 API 凭据，确定性测试不需要调用 DeepSeek 服务。
