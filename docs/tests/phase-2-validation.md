# 第二阶段实现与验收记录

记录日期：2026-11-03
设计依据：[第二阶段详细设计](../product/phase-2-detailed-design.md)

## 实现提交

阶段二实现及验证工作由以下提交组成：

- `b9995c4`：注册工作区文件操作工具。
- `0f0a5f9`：增加受限命令执行和受管服务，并加入 Web 运行依赖。
- `293d894`：增加 Git 查询、项目与任务服务、SQLite 状态/事件/产物、FastAPI、React 工作台及对应测试。
- `5c9af96`：补齐 pytest 参数和执行限制编辑、项目 revision 更新界面。
- `9b4b134`：加入浏览器贯通验收。

## 已交付行为

- 模型工具注册和严格参数校验，支持九类工作区操作、命令与服务管理、Git 状态/差异读取、验证工具入口。
- 工作区路径约束、编码和换行保持、变更记录、受限输出及 Windows 受管进程树。
- 项目环境检查、保存和 revision 更新；SQLite 持久化项目、任务、递增事件及按登记 ID 读取的产物。
- 单活动任务、后台执行、澄清补答和放弃等待；SSE 事件、任务差异和服务 API。
- 回环访问限制的 FastAPI 应用、构建后的前端静态文件托管，以及 React 项目/任务/服务工作台。
- Web 项目配置可修改 pytest 参数、模型请求上限、命令超时和输出字节上限；所选项目通过 expected revision 更新。
- Web 任务仍复用第一阶段的 `AgentWorkflow`，由 `AgentRuntime` 注入第二阶段工具和 Git 交付检查，以保留 CLI 兼容行为。没有另外建立 `app/verification` 包；验证、报告判定及提交核心仍位于既有 workflow。

## 验证结果

- `uv run pytest -q`：84 passed，121 subtests passed。
- `npm test`：3 passed。
- `npm run build`：TypeScript 检查和 Vite 生产构建通过。
- `npm run test:e2e`：1 passed。Playwright 启动真实 FastAPI 和 Vite 服务，在真实 Git 工作区中完成项目检查与保存、任务提交、澄清补答及差异查看；任务运行器使用确定性测试替身并将应用数据写入临时目录。
- 本阶段应用、测试及端到端替身文件的 `ruff check` 通过；`ruff format --check` 检查的 37 个文件均已格式化。
- `git diff --check` 通过。

全仓 `uv run ruff check app tests` 仍报告 19 条既有问题，位置在原有 `tests/acceptance` 和 `tests/fixtures` 用例。本阶段修改的应用代码、phase-two 测试及浏览器后端替身使用独立路径检查，结果通过；这次没有改动上述旧用例来消除无关 lint 差异。

## 验收边界

- 浏览器验收、HTTP 测试使用确定性任务运行器；本记录不表示已对真实模型服务完成联调。使用者配置本地 `.env` 后仍需在实际模型端点上确认模型响应、工具调用和超时行为。
- Playwright 流程会在真实后端中检查和保存工作区，但不调用真实模型，也不改写该工作区文件。
- JS/TS 支持通过文件工具及普通命令验证，不作为完整 JS/TS 编码任务交付。
- 按阶段设计，运行中取消、暂停及跨重启恢复不属于本阶段能力。
