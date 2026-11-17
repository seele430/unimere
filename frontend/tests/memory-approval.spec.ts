import { test, expect, type Page } from "@playwright/test";

import {
  blockUnmockedApi,
  expectNoUnmockedApi,
  installAuthMocks,
} from "./support/mockAuth";

const TEST_TOKEN = "playwright-memory-auto-token";
const TEST_USER = {
  id: "playwright-memory-auto-user",
  username: "memory-auto",
  role: "member",
  active: true,
};

async function setupPage(page: Page) {
  const unexpected = await blockUnmockedApi(page);
  await installAuthMocks(page, TEST_TOKEN, TEST_USER);
  const items: Array<Record<string, unknown>> = [];

  await page.route("**/api/chat", async (route) => {
    const payload = route.request().postDataJSON() as { message?: string };
    const message = payload.message ?? "";
    if (message.startsWith("记住：")) {
      const text = message.slice(3);
      items.push({
        id: 1,
        key: "response.language",
        kind: "constraint",
        layer: "identity",
        text,
        trust: "user_confirmed",
        source_ref: "chat:req-auto",
        source_session_id: null,
        context_usage: "always",
        status: "active",
        valid_from: "2026-11-16T00:00:00+00:00",
        valid_to: null,
        supersedes_id: null,
        created_at: "2026-11-16T00:00:00+00:00",
        updated_at: "2026-11-16T00:00:00+00:00",
      });
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "completed", reply: "好的。" }),
    });
  });

  await page.route("**/api/memory/items?*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        mode: "list",
        items,
        total: items.length,
        limit: 20,
        offset: 0,
        has_more: false,
      }),
    });
  });

  return { unexpected, items };
}

async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.locator("#pa-login-username").fill(TEST_USER.username);
  await page.locator("#pa-login-password").fill("PlaywrightTestPassword2026!");
  await page.getByRole("button", { name: /登录工作空间/ }).click();
  await expect(page.getByRole("textbox", { name: "输入聊天消息" })).toBeVisible();
}

async function send(page: Page, text: string): Promise<void> {
  const input = page.getByRole("textbox", { name: "输入聊天消息" });
  await input.fill(text);
  await input.press("Enter");
  await expect(page.getByText("好的。", { exact: true })).toBeVisible();
}

test.describe("长期记忆自动捕获边界", () => {
  test("明确记住不需要旧工具审批，并可在长期记忆中查看", async ({ page }) => {
    const { unexpected, items } = await setupPage(page);
    await login(page);
    await send(page, "记住：以后用中文回答");

    await expect(page.getByRole("alertdialog", { name: "工具审批" })).toHaveCount(0);
    expect(items).toHaveLength(1);
    await page.getByRole("button", { name: "长期记忆" }).click();
    await expect(page.getByRole("button", { name: "以后用中文回答" })).toBeVisible();
    await expect(page.getByRole("table").getByText("新对话自动使用")).toBeVisible();
    expectNoUnmockedApi(unexpected);
  });

  test("普通聊天不会写入长期记忆", async ({ page }) => {
    const { unexpected, items } = await setupPage(page);
    await login(page);
    await send(page, "今天需要写一份周报");

    expect(items).toHaveLength(0);
    await page.getByRole("button", { name: "长期记忆" }).click();
    await expect(page.getByText("全部暂无记录")).toBeVisible();
    expectNoUnmockedApi(unexpected);
  });
});
