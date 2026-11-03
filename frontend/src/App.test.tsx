import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import * as api from "./api/client";

vi.mock("./api/client", () => ({
  inspectProject: vi.fn(),
  getProjects: vi.fn(),
  getTasks: vi.fn(),
  getServices: vi.fn(),
  saveProject: vi.fn(),
  createTask: vi.fn(),
  getTask: vi.fn(),
  getTaskDiff: vi.fn(),
  getArtifacts: vi.fn(),
  subscribeTaskEvents: vi.fn(() => () => undefined),
  readArtifact: vi.fn(),
  answerTask: vi.fn(),
  dismissTask: vi.fn(),
  stopService: vi.fn(),
}));

describe("workspace project setup", () => {
  beforeEach(() => {
    vi.mocked(api.getProjects).mockResolvedValue({ items: [], total: 0, offset: 0, limit: 100 });
    vi.mocked(api.getServices).mockResolvedValue({ items: [], total: 0, offset: 0, limit: 100 });
    vi.mocked(api.getTasks).mockResolvedValue({ items: [], total: 0, offset: 0, limit: 100 });
    vi.mocked(api.inspectProject).mockResolvedValue({
      workspace: "C:/work/sample",
      is_git_repository: true,
      head: "0123456789",
      candidate_interpreters: ["C:/work/sample/.venv/Scripts/python.exe"],
      selected_interpreter: "C:/work/sample/.venv/Scripts/python.exe",
      pytest_available: true,
      suggested_pytest_args: [],
      ready: true,
      issues: [],
    });
    vi.mocked(api.saveProject).mockResolvedValue({
      id: "project-1",
      name: "sample",
      workspace: "C:/work/sample",
      python_executable: "C:/work/sample/.venv/Scripts/python.exe",
      pytest_args: [],
      extra_checks: [],
      limits: { max_model_requests: 40, command_timeout_seconds: 120, output_limit_bytes: 1048576 },
      revision: 1,
      created_at: "2026-11-03T00:00:00Z",
      updated_at: "2026-11-03T00:00:00Z",
    });
  });

  it("checks and saves a ready project through the visible form", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByRole("textbox", { name: "项目目录" }), "C:/work/sample");
    await user.click(screen.getByRole("button", { name: "检查环境" }));
    expect(await screen.findByText("Git 与 pytest 可用")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存项目" }));
    expect(api.inspectProject).toHaveBeenCalledWith("C:/work/sample");
    expect(api.saveProject).toHaveBeenCalledWith(expect.objectContaining({
      workspace: "C:/work/sample",
      python_executable: "C:/work/sample/.venv/Scripts/python.exe",
    }));
  });

  it("does not expose running-task cancellation or a command console", () => {
    render(<App />);
    expect(screen.queryByRole("button", { name: /取消任务|暂停任务|运行命令/i })).not.toBeInTheDocument();
  });
});
