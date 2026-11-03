import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import * as api from "./api/client";
import type {
  ArtifactContent,
  ArtifactRef,
  DiffResult,
  ProjectInspection,
  ProjectProfile,
  ServiceSnapshot,
  TaskEvent,
  TaskSnapshot,
} from "./types";

const terminal = new Set(["succeeded", "failed", "no_changes", "dismissed", "interrupted"]);

function App() {
  const [projects, setProjects] = useState<ProjectProfile[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [tasks, setTasks] = useState<TaskSnapshot[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const [task, setTask] = useState<TaskSnapshot | null>(null);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [inspection, setInspection] = useState<ProjectInspection | null>(null);
  const [name, setName] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [python, setPython] = useState("");
  const [requirement, setRequirement] = useState("");
  const [answer, setAnswer] = useState("");
  const [services, setServices] = useState<ServiceSnapshot[]>([]);
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactRef[]>([]);
  const [artifactContent, setArtifactContent] = useState<ArtifactContent | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === selectedProjectId) ?? null,
    [projects, selectedProjectId],
  );

  const refreshProjects = useCallback(async () => {
    const page = await api.getProjects();
    setProjects(page.items);
    if (!selectedProjectId && page.items[0]) setSelectedProjectId(page.items[0].id);
  }, [selectedProjectId]);

  const refreshTasks = useCallback(async (projectId?: string) => {
    const page = await api.getTasks(projectId);
    setTasks(page.items);
  }, []);

  const refreshServices = useCallback(async () => {
    const page = await api.getServices();
    setServices(page.items);
  }, []);

  useEffect(() => {
    void Promise.all([refreshProjects(), refreshServices()]).catch(showError);
  }, [refreshProjects, refreshServices]);

  useEffect(() => {
    void refreshTasks(selectedProjectId || undefined).catch(showError);
  }, [selectedProjectId, refreshTasks]);

  useEffect(() => {
    if (!selectedTaskId) {
      setTask(null);
      setEvents([]);
      setDiff(null);
      setArtifacts([]);
      return;
    }
    let disposed = false;
    setEvents([]);
    void Promise.all([api.getTask(selectedTaskId), api.getArtifacts(selectedTaskId)])
      .then(([current, artifactPage]) => {
        if (disposed) return;
        setTask(current);
        setArtifacts(artifactPage.items);
        return api.getTaskDiff(selectedTaskId).then((currentDiff) => {
          if (!disposed) setDiff(currentDiff);
        });
      })
      .catch((reason: unknown) => !disposed && showError(reason));
    const close = api.subscribeTaskEvents(
      selectedTaskId,
      0,
      (event) => {
        if (disposed) return;
        setEvents((current) =>
          current.some((item) => item.seq === event.seq)
            ? current
            : [...current, event].sort((left, right) => left.seq - right.seq),
        );
        void api.getTask(selectedTaskId).then(setTask).catch(showError);
        if (event.kind === "task_status_changed" && terminal.has(String(event.payload.status))) {
          void refreshTasks(selectedProjectId || undefined).catch(showError);
          void refreshServices().catch(showError);
          void api.getTaskDiff(selectedTaskId).then(setDiff).catch(showError);
          void api.getArtifacts(selectedTaskId).then((page) => setArtifacts(page.items)).catch(showError);
        }
      },
      () => setNotice("事件连接暂时中断，正在等待自动重连。"),
    );
    return () => {
      disposed = true;
      close();
    };
  }, [selectedTaskId, selectedProjectId, refreshTasks, refreshServices]);

  async function inspect() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.inspectProject(workspace.trim());
      setInspection(result);
      if (result.selected_interpreter) setPython(result.selected_interpreter);
      if (!name.trim()) setName(workspace.split(/[\\/]/).filter(Boolean).at(-1) ?? "本地项目");
      setNotice(result.ready ? "项目环境已就绪，可以保存配置。" : "项目还未就绪，请查看检查结果。 ");
    } catch (reason) {
      showError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function saveProject() {
    if (!inspection?.ready || !python) return;
    setBusy(true);
    setError("");
    try {
      const project = await api.saveProject({
        name: name.trim(),
        workspace: inspection.workspace,
        python_executable: python,
        pytest_args: inspection.suggested_pytest_args,
        extra_checks: [],
      });
      await refreshProjects();
      setSelectedProjectId(project.id);
      setNotice("项目配置已保存。");
      setInspection(null);
    } catch (reason) {
      showError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function submitTask(event: FormEvent) {
    event.preventDefault();
    if (!selectedProject) return;
    setBusy(true);
    setError("");
    try {
      const created = await api.createTask(selectedProject.id, requirement.trim());
      setRequirement("");
      setSelectedTaskId(created.id);
      setTask(created);
      setNotice("任务已提交，后台正在处理。");
      await refreshTasks(selectedProject.id);
    } catch (reason) {
      showError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function answerTask(event: FormEvent) {
    event.preventDefault();
    if (!task) return;
    setBusy(true);
    setError("");
    try {
      const updated = await api.answerTask(task.id, answer.trim());
      setTask(updated);
      setAnswer("");
      setNotice("补充说明已提交，任务会从原进度继续。");
    } catch (reason) {
      showError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function dismissTask() {
    if (!task) return;
    setBusy(true);
    try {
      setTask(await api.dismissTask(task.id));
      setNotice("已放弃等待补充说明。");
      await refreshTasks(selectedProjectId || undefined);
    } catch (reason) {
      showError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function stopService(service: ServiceSnapshot) {
    setBusy(true);
    try {
      await api.stopService(service.id);
      await refreshServices();
    } catch (reason) {
      showError(reason);
    } finally {
      setBusy(false);
    }
  }

  async function openArtifact(artifact: ArtifactRef) {
    try {
      setArtifactContent(await api.readArtifact(artifact.id));
    } catch (reason) {
      showError(reason);
    }
  }

  function showError(reason: unknown) {
    setError(reason instanceof Error ? reason.message : "请求未能完成。");
    setNotice("");
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#workspace" aria-label="My Agent 首页">
          <span className="brand-mark">M</span>
          <span>my agent <small>LOCAL CODING WORKSPACE</small></span>
        </a>
        <div className="topbar-right"><span className="local-indicator" />仅本机访问</div>
      </header>

      <main className="layout" id="workspace">
        <aside className="sidebar">
          <div className="section-heading"><span>项目</span><span className="quiet">{projects.length}</span></div>
          <label className="field-label" htmlFor="project-select">当前项目</label>
          <select
            id="project-select"
            value={selectedProjectId}
            onChange={(event) => setSelectedProjectId(event.target.value)}
          >
            <option value="">选择一个项目</option>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
          {selectedProject && (
            <div className="project-path" title={selectedProject.workspace}>{selectedProject.workspace}</div>
          )}

          <div className="divider" />
          <div className="section-heading"><span>最近任务</span></div>
          <div className="task-list">
            {tasks.map((item) => (
              <button
                className={`task-list-item ${selectedTaskId === item.id ? "selected" : ""}`}
                key={item.id}
                onClick={() => setSelectedTaskId(item.id)}
              >
                <span className={`status-dot status-${item.status}`} />
                <span className="task-list-copy">
                  <strong>{item.requirement}</strong>
                  <small>{statusLabel[item.status]} · {new Date(item.updated_at).toLocaleString()}</small>
                </span>
              </button>
            ))}
            {tasks.length === 0 && <p className="empty-hint">还没有任务记录。</p>}
          </div>

          <div className="divider" />
          <button className="nav-link" onClick={() => void refreshServices()}>
            <span className="nav-icon">↗</span> 本地服务
            {services.some((service) => service.status === "running") && <span className="live-pill">运行中</span>}
          </button>
        </aside>

        <section className="main-column">
          <div className="page-title-row">
            <div>
              <div className="eyebrow">LOCAL WORKSPACE / PHASE 02</div>
              <h1>编码工作台</h1>
              <p>配置本地项目，提交任务并查看真实执行记录。</p>
            </div>
            <div className="connection-chip"><span className="local-indicator" />本机后端已连接</div>
          </div>

          {error && <div className="notice error-notice" role="alert">{error}</div>}
          {notice && <div className="notice success-notice" role="status">{notice}</div>}

          <section className="panel project-panel">
            <div className="panel-heading">
              <div className="step-number">01</div>
              <div><h2>项目配置</h2><p>选择已有项目，或检查并保存一个新的 Git + pytest 项目。</p></div>
            </div>
            <div className="project-form-grid">
              <label className="field">
                <span>项目目录</span>
                <input
                  value={workspace}
                  onChange={(event) => setWorkspace(event.target.value)}
                  placeholder="例如 C:\\work\\my-project"
                  aria-label="项目目录"
                />
              </label>
              <label className="field">
                <span>项目名称</span>
                <input value={name} onChange={(event) => setName(event.target.value)} placeholder="我的项目" />
              </label>
            </div>
            <div className="form-actions">
              <button className="button secondary" disabled={busy || !workspace.trim()} onClick={() => void inspect()}>
                检查环境
              </button>
              {inspection && (
                <span className={`inspection-state ${inspection.ready ? "ready" : "not-ready"}`}>
                  {inspection.ready ? "Git 与 pytest 可用" : "环境未就绪"}
                </span>
              )}
              {inspection?.ready && (
                <button className="button primary" disabled={busy || !name.trim()} onClick={() => void saveProject()}>
                  保存项目
                </button>
              )}
            </div>
            {inspection && (
              <div className={`inspection-result ${inspection.ready ? "" : "problem"}`}>
                <div className="inspection-meta">
                  <span><b>Git</b> {inspection.is_git_repository ? `已连接 · ${inspection.head?.slice(0, 8)}` : "未找到有效仓库"}</span>
                  <span><b>pytest</b> {inspection.pytest_available ? "可运行" : "不可运行"}</span>
                </div>
                {inspection.ready && inspection.candidate_interpreters.length > 0 && (
                  <label className="field interpreter-field">
                    <span>Python 解释器</span>
                    <select value={python} onChange={(event) => setPython(event.target.value)}>
                      {inspection.candidate_interpreters.map((candidate) => <option key={candidate}>{candidate}</option>)}
                    </select>
                  </label>
                )}
                {inspection.issues.map((issue) => <p className="issue-line" key={issue.code}>{issue.message}</p>)}
              </div>
            )}
          </section>

          <section className="panel task-submit-panel">
            <div className="panel-heading">
              <div className="step-number">02</div>
              <div><h2>提交任务</h2><p>系统会检查当前项目状态；同一时间只运行一个任务。</p></div>
            </div>
            <form onSubmit={submitTask}>
              <label className="field">
                <span>你想完成什么？</span>
                <textarea
                  value={requirement}
                  onChange={(event) => setRequirement(event.target.value)}
                  placeholder="描述目标、期望行为和需要遵守的约束…"
                  rows={4}
                  maxLength={20_000}
                  aria-label="任务需求"
                />
              </label>
              <div className="form-actions submit-row">
                <span className="form-footnote">支持自然语言任务 · 最多 20,000 字符</span>
                <button className="button primary" type="submit" disabled={busy || !selectedProject || !requirement.trim()}>
                  提交任务 <span aria-hidden="true">→</span>
                </button>
              </div>
            </form>
          </section>

          {task && (
            <section className="panel task-detail-panel">
              <div className="panel-heading task-heading">
                <div className={`large-status-dot status-${task.status}`} />
                <div className="task-heading-copy">
                  <div className="task-status-line"><span>{statusLabel[task.status]}</span><span className="quiet">任务 {task.id.slice(0, 8)}</span></div>
                  <h2>{task.requirement}</h2>
                  <p>{task.summary || "正在处理任务。"}</p>
                </div>
              </div>
              <div className="metrics-row">
                <div><span>阶段</span><b>{phaseLabel[task.phase] ?? task.phase}</b></div>
                <div><span>实现轮次</span><b>{task.attempt} / 3</b></div>
                <div><span>模型请求</span><b>{task.model_requests}</b></div>
                <div><span>事件</span><b>{task.last_event_seq}</b></div>
              </div>
              {task.error && <div className="notice error-notice"><b>{task.error.code}</b> · {task.error.message}</div>}
              {task.status === "needs_clarification" && (
                <div className="clarification-box">
                  <div className="clarification-title"><span>需要你的补充说明</span><span className="quiet">回答后从原进度继续</span></div>
                  <ul>{task.questions.map((question) => <li key={question}>{question}</li>)}</ul>
                  <form className="answer-form" onSubmit={answerTask}>
                    <input value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder="输入补充说明…" aria-label="补充说明" />
                    <button className="button primary" type="submit" disabled={busy || !answer.trim()}>继续</button>
                    <button className="button text-button" type="button" disabled={busy} onClick={() => void dismissTask()}>放弃等待</button>
                  </form>
                </div>
              )}

              <div className="detail-grid">
                <section className="detail-card">
                  <div className="detail-title"><h3>执行过程</h3><span>{events.length} 条新事件</span></div>
                  <ol className="event-list">
                    {events.map((event) => (
                      <li key={event.seq}>
                        <span className="event-index">{String(event.seq).padStart(2, "0")}</span>
                        <div><b>{eventLabel(event)}</b><small>{new Date(event.timestamp).toLocaleTimeString()}</small></div>
                      </li>
                    ))}
                    {events.length === 0 && <li className="empty-hint">等待后端执行事件…</li>}
                  </ol>
                </section>
                <section className="detail-card diff-card">
                  <div className="detail-title"><h3>任务差异</h3><span>{diff?.files.length ?? 0} 个文件</span></div>
                  <pre>{diff?.text || "完成修改后，任务差异会显示在这里。"}</pre>
                  {diff?.truncated && <small className="quiet">差异内容已截断，可查看产物。</small>}
                </section>
              </div>
              <section className="artifacts-section">
                <div className="detail-title"><h3>任务产物</h3><span>按安全登记 ID 查看</span></div>
                <div className="artifact-list">
                  {artifacts.map((artifact) => (
                    <button className="artifact-chip" key={artifact.id} onClick={() => void openArtifact(artifact)}>
                      <span className="artifact-icon">▤</span>{artifact.kind}<small>{formatBytes(artifact.size_bytes)}</small>
                    </button>
                  ))}
                  {artifacts.length === 0 && <span className="empty-hint">尚无可查看产物。</span>}
                </div>
                {artifactContent && (
                  <div className="artifact-preview">
                    <div className="detail-title"><h3>{artifactContent.artifact.kind}</h3><button className="close-preview" onClick={() => setArtifactContent(null)}>关闭</button></div>
                    <pre>{artifactContent.text}</pre>
                  </div>
                )}
              </section>
            </section>
          )}

          <section className="panel services-panel">
            <div className="panel-heading compact-heading">
              <div className="step-number service-step">03</div>
              <div><h2>受管服务</h2><p>任务启动的预览服务会留在这里，直到你停止它或关闭应用。</p></div>
              <button className="button secondary refresh-button" onClick={() => void refreshServices()}>刷新</button>
            </div>
            <div className="service-list">
              {services.map((service) => (
                <div className="service-row" key={service.id}>
                  <span className={`status-dot status-${service.status}`} />
                  <div className="service-copy"><b>{service.name}</b><small>{service.url ?? service.command_summary}</small></div>
                  <span className="service-status">{service.status}</span>
                  {service.status === "running" && <button className="button small danger" disabled={busy} onClick={() => void stopService(service)}>停止</button>}
                </div>
              ))}
              {services.length === 0 && <p className="empty-hint">当前没有受管服务。</p>}
            </div>
          </section>

          <footer className="footer-note">My Agent · 本机运行 · 模型密钥仅保存在后端环境</footer>
        </section>
      </main>
      {busy && <div className="busy-indicator" aria-live="polite">正在处理…</div>}
    </div>
  );
}

const statusLabel: Record<string, string> = {
  running: "正在运行", needs_clarification: "等待补充", succeeded: "已完成", failed: "失败",
  no_changes: "无需修改", dismissed: "已放弃", interrupted: "已中断", starting: "启动中",
  stopping: "停止中", stopped: "已停止", exited: "已退出", unknown: "状态未知",
};

const phaseLabel: Record<string, string> = {
  preflight: "启动检查", baseline: "回归基线", preparation: "需求准备", implementation: "实现修改",
  verification: "验证检查", delivery: "结果交付", finished: "已结束",
};

function eventLabel(event: TaskEvent): string {
  const tool = typeof event.payload.tool === "string" ? event.payload.tool : "";
  const labels: Record<string, string> = {
    task_status_changed: "任务状态更新", tool_started: `开始调用 ${tool}`, tool_completed: `${tool} 已返回结果`,
    tool_error: `${tool} 调用失败`, model_request: "请求模型", command_output: "命令输出",
    file_changed: `文件${event.payload.operation === "create" ? "已创建" : "已修改"}`,
    verification_completed: "验证完成", verification_error: "验证未通过", commit_created: "已创建 Git 提交",
    commit_error: "提交未完成", git_snapshot: "已记录 Git 基线", extra_check_completed: "额外检查完成",
    clarification_requested: "需要补充说明", clarification_answered: "已收到补充说明",
    service_status_changed: "服务状态更新", artifact_created: "已生成任务产物",
  };
  return labels[event.kind] ?? event.kind.replaceAll("_", " ");
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  return `${(size / 1024).toFixed(1)} KB`;
}

export default App;
