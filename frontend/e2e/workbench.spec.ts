import { expect, test } from "@playwright/test";

test("shows the local project and task workbench", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "编码工作台" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "项目目录" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "任务需求" })).toBeVisible();
  await expect(page.getByRole("button", { name: /取消任务|暂停任务/ })).toHaveCount(0);
});
