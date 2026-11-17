import { test, expect, type Page, type Route } from "@playwright/test";

import {
  blockUnmockedApi,
  expectNoUnmockedApi,
  installArtifactMocks,
  installAuthMocks,
  installReminderMocks,
} from "./support/mockAuth";

const TEST_TOKEN = "playwright-memory-token";
const TEST_USER = {
  id: "playwright-memory-user",
  username: "memory",
  role: "member",
  active: true,
};

const ITEM = {
  id: 2,
  key: "drink",
  kind: "project_fact",
  layer: "project",
  text: "改为喜欢红茶",
  trust: "user_confirmed",
  source_ref: "chat:request-2",
  source_session_id: "session-source-2",
  context_usage: "related",
  status: "active",
  valid_from: "2026-11-11T00:00:00+00:00",
  valid_to: null,
  supersedes_id: 1,
  created_at: "2026-11-11T00:00:00+00:00",
  updated_at: "2026-11-11T00:00:00+00:00",
};

const EMPTY_HISTORY = {
  anchor_id: 2,
  current_id: 2,
  items: [],
  count: 0,
  total_count: 0,
  truncated: false,
  window_has_current: true,
};

async function setupPage(page: Page): Promise<string[]> {
  const unexpected = await blockUnmockedApi(page);
  await installAuthMocks(page, TEST_TOKEN, TEST_USER);
  await installReminderMocks(page);
  await installArtifactMocks(page);
  await page.route("**/api/sessions", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ web_sessions: [], qq_sessions: [] }),
  }));
  return unexpected;
}

async function mockList(page: Page, item = ITEM): Promise<void> {
  await page.route("**/api/memory/items?*", async (route: Route) => {
    const url = new URL(route.request().url());
    const search = url.searchParams.has("query");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(search
        ? { mode: "search", items: [item], count: 1, limit: 50, search_mode: "fts" }
        : { mode: "list", items: [item], total: 1, limit: 20, offset: 0, has_more: false }),
    });
  });
}

async function mockDetail(page: Page, item = ITEM): Promise<void> {
  await page.route(`**/api/memory/items/${item.id}`, async (route: Route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(item) });
  });
  await page.route(`**/api/memory/items/${item.id}/history`, async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...EMPTY_HISTORY, anchor_id: item.id, current_id: item.id }),
    });
  });
}

async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.locator("#pa-login-username").fill("memory");
  await page.locator("#pa-login-password").fill("PlaywrightTestPassword2026!");
  await page.getByRole("button", { name: /登录工作空间/ }).click();
  await expect(page.getByRole("textbox", { name: "输入聊天消息" })).toBeVisible();
}

async function openMemory(page: Page): Promise<void> {
  await page.getByRole("button", { name: "长期记忆" }).click();
  await expect(page.getByRole("heading", { name: "长期记忆", exact: true, level: 2 })).toBeVisible();
}

test.describe("长期记忆列表", () => {
  test("展示用户字段并隐藏内部 key", async ({ page }) => {
    const unexpected = await setupPage(page);
    await mockList(page);

    await login(page);
    await openMemory(page);
    await expect(page.getByText("drink", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "改为喜欢红茶" })).toBeVisible();
    await expect(page.getByRole("table").getByText("项目事实", { exact: true })).toBeVisible();
    await expect(page.getByText("相关时使用", { exact: true })).toBeVisible();
    await expect(page.getByText("对话捕获", { exact: true })).toBeVisible();
    expectNoUnmockedApi(unexpected);
  });

  test("搜索参数、分类参数与清空", async ({ page }) => {
    const unexpected = await setupPage(page);
    const urls: string[] = [];
    await page.route("**/api/memory/items?*", async (route: Route) => {
      urls.push(route.request().url());
      const search = route.request().url().includes("query=");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(search
          ? { mode: "search", items: [ITEM], count: 1, limit: 50, search_mode: "fts" }
          : { mode: "list", items: [ITEM], total: 1, limit: 20, offset: 0, has_more: false }),
      });
    });

    await login(page);
    await openMemory(page);
    await page.getByRole("button", { name: "偏好与约束", exact: true }).click();
    await expect.poll(() => urls.some((url) => url.includes("category=stable"))).toBe(true);
    await page.getByLabel("搜索记忆").fill("红茶");
    await page.getByRole("button", { name: "搜索", exact: true }).click();
    await expect.poll(() => urls.some((url) => url.includes("query=") && url.includes("category=stable"))).toBe(true);
    await page.getByRole("button", { name: "清空搜索" }).click();
    await expect.poll(() => urls.some((url) => !url.includes("query=") && url.includes("category=stable"))).toBe(true);
    expectNoUnmockedApi(unexpected);
  });

  test("详情、历史链与来源对话", async ({ page }) => {
    const unexpected = await setupPage(page);
    await mockList(page);
    await page.route("**/api/memory/items/2", async (route: Route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ITEM) });
    });
    await page.route("**/api/memory/items/2/history", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...EMPTY_HISTORY,
          items: [
            { ...ITEM, id: 1, text: "喜欢绿茶", supersedes_id: null, deleted_at: null, is_current: false, version_status: "superseded" },
            { ...ITEM, deleted_at: null, is_current: true, version_status: "current" },
          ],
          count: 2,
          total_count: 2,
        }),
      });
    });
    await page.route("**/api/sessions/session-source-2", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session: { id: "session-source-2", title: "来源对话", source: "web", created_at: ITEM.created_at, updated_at: ITEM.updated_at, message_count: 0 },
          read_only: false,
          messages: [],
        }),
      });
    });

    await login(page);
    await openMemory(page);
    await page.locator(".memory-content").click();
    await expect(page.locator(".memory-history").getByText("旧版本", { exact: true })).toBeVisible();
    await expect(page.locator(".memory-history").getByText("当前版本", { exact: true })).toBeVisible();
    await expect(page.getByText("喜欢绿茶", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "打开来源对话" }).click();
    await expect(page.getByRole("textbox", { name: "输入聊天消息" })).toBeVisible();
    expectNoUnmockedApi(unexpected);
  });
});

test.describe("长期记忆新增、编辑与删除", () => {
  test("手动新增携带分类与用途所需字段", async ({ page }) => {
    const unexpected = await setupPage(page);
    const posts: string[] = [];
    await mockList(page);
    await page.route("**/api/memory/items", async (route: Route) => {
      if (route.request().method() !== "POST") return route.continue();
      posts.push(route.request().postData() ?? "");
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({ ...ITEM, id: 3, kind: "preference", layer: "identity", context_usage: "always", text: "报告默认使用中文" }),
      });
    });

    await login(page);
    await openMemory(page);
    await page.getByRole("button", { name: "新增记忆" }).click();
    await page.getByLabel("新增记忆内容").fill("报告默认使用中文");
    await page.getByRole("button", { name: "保存记忆" }).click();
    await expect.poll(() => posts.length).toBe(1);
    expect(JSON.parse(posts[0])).toEqual({ text: "报告默认使用中文", kind: "preference", layer: "identity" });
    expectNoUnmockedApi(unexpected);
  });

  test("编辑使用 PATCH 生成新版本，不暴露内部 key", async ({ page }) => {
    const unexpected = await setupPage(page);
    const patches: string[] = [];
    await mockList(page);
    await mockDetail(page);
    await page.route("**/api/memory/items/2", async (route: Route) => {
      if (route.request().method() !== "PATCH") return route.continue();
      patches.push(route.request().postData() ?? "");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ...ITEM, id: 3, text: "改为喜欢乌龙", supersedes_id: 2 }),
      });
    });
    await page.route("**/api/memory/items/3", async (route: Route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...ITEM, id: 3, text: "改为喜欢乌龙", supersedes_id: 2 }) });
    });
    await page.route("**/api/memory/items/3/history", async (route: Route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...EMPTY_HISTORY, anchor_id: 3, current_id: 3 }) });
    });

    await login(page);
    await openMemory(page);
    await page.locator(".memory-content").click();
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByLabel("编辑记忆内容").fill("改为喜欢乌龙");
    await page.getByRole("button", { name: "保存新版本" }).click();
    await expect.poll(() => patches.length).toBe(1);
    expect(JSON.parse(patches[0])).toEqual({ text: "改为喜欢乌龙" });
    expectNoUnmockedApi(unexpected);
  });

  test("删除二次确认后才发 DELETE", async ({ page }) => {
    const unexpected = await setupPage(page);
    const deletes: string[] = [];
    let accepted = false;
    page.on("dialog", async (dialog) => {
      accepted = true;
      await dialog.accept();
    });
    await mockList(page);
    await mockDetail(page);
    await page.route("**/api/memory/items/2", async (route: Route) => {
      if (route.request().method() !== "DELETE") return route.continue();
      deletes.push(route.request().url());
      await route.fulfill({ status: 204, body: "" });
    });

    await login(page);
    await openMemory(page);
    await page.locator(".memory-content").click();
    await page.getByRole("button", { name: "删除", exact: true }).click();
    await expect.poll(() => deletes.length).toBe(1);
    expect(accepted).toBe(true);
    expectNoUnmockedApi(unexpected);
  });

  test("无 key 的旧记忆也可以编辑", async ({ page }) => {
    const unexpected = await setupPage(page);
    const item = { ...ITEM, key: "" };
    const patches: string[] = [];
    await mockList(page, item);
    await mockDetail(page, item);
    await page.route("**/api/memory/items/2", async (route: Route) => {
      if (route.request().method() !== "PATCH") return route.continue();
      patches.push(route.request().postData() ?? "");
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...item, id: 3, text: "旧记忆的新版本" }) });
    });
    await page.route("**/api/memory/items/3", async (route: Route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...item, id: 3, text: "旧记忆的新版本" }) });
    });
    await page.route("**/api/memory/items/3/history", async (route: Route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...EMPTY_HISTORY, anchor_id: 3, current_id: 3 }) });
    });

    await login(page);
    await openMemory(page);
    await page.locator(".memory-content").click();
    await expect(page.getByRole("button", { name: "编辑", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByLabel("编辑记忆内容").fill("旧记忆的新版本");
    await page.getByRole("button", { name: "保存新版本" }).click();
    await expect.poll(() => patches.length).toBe(1);
    expectNoUnmockedApi(unexpected);
  });
});
