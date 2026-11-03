import { expect, test } from "@playwright/test";
import path from "node:path";

test("shows the local project and task workbench", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "编码工作台" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "项目目录" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "任务需求" })).toBeVisible();
  await expect(page.getByRole("button", { name: /取消任务|暂停任务/ })).toHaveCount(0);

  await page.getByRole("textbox", { name: "项目目录" }).fill(path.resolve(process.cwd(), ".."));
  await page.getByRole("textbox", { name: "项目名称" }).fill("Browser acceptance");
  await page.getByRole("button", { name: "检查环境" }).click();
  await expect(page.getByText("Git 与 pytest 可用")).toBeVisible();
  await page.getByRole("button", { name: "保存为新项目" }).click();
  await expect(page.locator("#project-select")).toHaveValue(/.+/);

  await page.getByRole("textbox", { name: "任务需求" }).fill("完成一个浏览器端任务");
  await page.getByRole("button", { name: /提交任务/ }).click();
  await expect(page.getByText("需要你的补充说明")).toBeVisible();
  await page.getByRole("textbox", { name: "补充说明" }).fill("使用 JSON 格式");
  await page.getByRole("button", { name: "继续" }).click();
  await expect(page.getByText("已完成", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "任务差异" })).toBeVisible();
});
