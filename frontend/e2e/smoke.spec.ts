import { expect, test } from "@playwright/test";

test("プロジェクトを作成して制作画面を表示できる", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Doujin Studio")).toBeVisible();
  await page.getByTitle("新規プロジェクト").click();
  const dialog = page.getByRole("dialog", { name: "新しい本" });
  await dialog.getByLabel("タイトル").fill("E2Eテスト本");
  await dialog.getByRole("button", { name: "作成" }).click();
  await expect(page.getByLabel("本のタイトル")).toHaveValue("E2Eテスト本");
});
