import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { useReviewStore } from "../src/stores/review.js";
import { useLlmStore } from "../src/stores/llm.js";
import * as expenseCorrections from "../src/api/expenseCorrections.js";
import * as reviewApi from "../src/api/review.js";
import * as expensesApi from "../src/api/expenses.js";
import * as receiptsApi from "../src/api/receipts.js";
import * as llmApi from "../src/api/adminLlm.js";
import { useCatalogStore } from "../src/stores/catalog.js";
import { useToastStore } from "../src/stores/toast.js";

beforeEach(async () => {
  await allure.epic("Review & Rules");
  await allure.feature("Frontend");
  await allure.story("Review store");
});

function mockOnLine(value) {
  const ownBefore = Object.getOwnPropertyDescriptor(navigator, "onLine");
  Object.defineProperty(navigator, "onLine", { configurable: true, get: () => value });
  return () => {
    if (ownBefore) {
      Object.defineProperty(navigator, "onLine", ownBefore);
    } else {
      delete navigator.onLine;
    }
  };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  // Default: page-1 loads trigger loadStuckReceipts() -> getReceiptQueue().
  // Mock it here so tests that don't care about the stuck-receipts queue
  // never fall through to a real fetch(). Tests that do care override with
  // mockResolvedValueOnce/mockRejectedValueOnce/etc.
  vi.spyOn(receiptsApi, "getReceiptQueue").mockResolvedValue({ items: [], has_more: false });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function seedCatalog() {
  const catalog = useCatalogStore();
  catalog.replaceSnapshot({
    catalog_version: 1,
    category_groups: [{ id: 1, name: "food", is_active: true }],
    categories: [{ id: 2, group_id: 1, name: "snacks", is_active: true }],
    events: [],
    tags: [],
  });
}

describe("review store: correct()", () => {
  it("calls approveRule with item.id (not expense_id)", async () => {
    const spy = vi
      .spyOn(reviewApi, "approveRule")
      .mockResolvedValueOnce({ updated_expenses_count: 1 });

    seedCatalog();
    const store = useReviewStore();
    store.items = [{ id: 7, expense_id: 42, is_doubtful: true, name: "hleb", count: 1 }];

    await store.correct(store.items[0], 2);

    expect(spy).toHaveBeenCalledWith(7, 2);
  });

  it("calls correctCategory with scope all for certain items", async () => {
    const spy = vi
      .spyOn(expenseCorrections, "correctCategory")
      .mockResolvedValueOnce({ count: 2, corrected_expense_id: 101, batch_updated_count: 1 });

    seedCatalog();
    const store = useReviewStore();
    store.items = [{ id: 100, expense_id: 101, is_doubtful: false, store: "Lidl", total: 200 }];

    await store.correct(store.items[0], 2, "all");

    expect(spy).toHaveBeenCalledWith(101, 2, "all");
  });

  it("calls correctCategory with scope for certain items with non-all scope", async () => {
    const spy = vi
      .spyOn(expenseCorrections, "correctCategory")
      .mockResolvedValueOnce({ count: 2, corrected_expense_id: 101, batch_updated_count: 1 });

    seedCatalog();
    const store = useReviewStore();
    store.items = [{ id: 100, expense_id: 101, is_doubtful: false, store: "Lidl", total: 200 }];

    await store.correct(store.items[0], 2, "month");

    expect(spy).toHaveBeenCalledWith(101, 2, "month");
  });

  it("confirms an expense correction without calling the rules API", async () => {
    const correctionSpy = vi
      .spyOn(expenseCorrections, "correctCategory")
      .mockResolvedValueOnce({ corrected_expense_id: 42, batch_updated_count: 0 });
    const ruleSpy = vi.spyOn(reviewApi, "approveRule");

    seedCatalog();
    const store = useReviewStore();
    store.items = [
      {
        id: 42,
        expense_id: 42,
        review_kind: "expense_correction",
        is_doubtful: true,
        count: 1,
      },
    ];
    store.expenses = [{ id: 42, category_id: 1, confidence_level: 1 }];
    store.doubtfulCount = 1;

    await store.correct(store.items[0], 2, "all");

    expect(correctionSpy).toHaveBeenCalledWith(42, 2, "single");
    expect(ruleSpy).not.toHaveBeenCalled();
    expect(store.items).toHaveLength(0);
    expect(store.doubtfulCount).toBe(0);
    expect(store.expenses[0]).toMatchObject({ category_id: 2, confidence_level: 4 });
  });

  it("does not remove a rule whose id matches a confirmed correction expense", async () => {
    vi.spyOn(expenseCorrections, "correctCategory").mockResolvedValueOnce({
      corrected_expense_id: 42,
      batch_updated_count: 0,
    });
    seedCatalog();
    const store = useReviewStore();
    store.items = [
      { id: 42, is_doubtful: true, name: "rule" },
      {
        id: 42,
        expense_id: 42,
        review_kind: "expense_correction",
        is_doubtful: true,
      },
    ];

    await store.correct(store.items[1], 2);

    expect(store.items).toEqual([{ id: 42, is_doubtful: true, name: "rule" }]);
  });

  it("uses count from correctCategory result for scoped correction toast", async () => {
    vi.spyOn(expenseCorrections, "correctCategory").mockResolvedValueOnce({
      count: 3,
      corrected_expense_id: 101,
      batch_updated_count: 2,
    });
    seedCatalog();
    const toast = useToastStore();
    const showSpy = vi.spyOn(toast, "show");
    const store = useReviewStore();
    store.items = [{ id: 100, expense_id: 101, is_doubtful: false, store: "Lidl", total: 200 }];

    await store.correct(store.items[0], 2, "month");

    expect(showSpy).toHaveBeenCalledWith(expect.stringContaining("3"), "success");
  });

  it("removes corrected doubtful item from items leaving remaining doubtful items intact", async () => {
    vi.spyOn(reviewApi, "approveRule").mockResolvedValueOnce({ updated_expenses_count: 1 });

    seedCatalog();
    const store = useReviewStore();
    store.items = [
      { id: 7, expense_id: 42, is_doubtful: true, name: "hleb", count: 1 },
      { id: 8, expense_id: 99, is_doubtful: true, name: "mleko", count: 1 },
    ];

    await store.correct(store.items[0], 2);

    expect(store.items).toHaveLength(1);
    expect(store.items[0].id).toBe(8);
    expect(store.items[0].is_doubtful).toBe(true);
  });

  it("keeps certain items in the list after correction and updates their category", async () => {
    vi.spyOn(expenseCorrections, "correctCategory").mockResolvedValueOnce({ count: 1 });

    seedCatalog();
    const store = useReviewStore();
    store.items = [{ id: 100, expense_id: 100, is_doubtful: false, store: "Lidl", total: 200, category_id: 1 }];

    await store.correct(store.items[0], 2);

    expect(store.items).toHaveLength(1);
    expect(store.items[0].category_id).toBe(2);
    expect(store.items[0].category_name).toBe("snacks");
  });

  it("removes corrected doubtful item from items and decrements doubtfulCount", async () => {
    vi.spyOn(reviewApi, "approveRule").mockResolvedValueOnce({ updated_expenses_count: 1 });

    seedCatalog();
    const store = useReviewStore();
    store.items = [{ id: 7, expense_id: 42, is_doubtful: true, name: "hleb", count: 1 }];
    store.doubtfulCount = 1;

    await store.correct(store.items[0], 2);

    expect(store.items).toHaveLength(0);
    expect(store.doubtfulCount).toBe(0);
  });

  it("clears confidence_level and updates category on expenses matching by rule_id", async () => {
    vi.spyOn(reviewApi, "approveRule").mockResolvedValueOnce({ updated_expenses_count: 3 });
    seedCatalog();
    const store = useReviewStore();
    store.items = [{ id: 7, expense_id: 42, is_doubtful: true, name: "hleb", count: 3 }];
    store.expenses = [
      { id: 42, rule_id: 7, confidence_level: 2, category_id: 1, category_name: "old" },
      { id: 43, rule_id: 7, confidence_level: 2, category_id: 1, category_name: "old" },
      { id: 44, rule_id: 9, confidence_level: 2, category_id: 1, category_name: "old" },
    ];

    await store.correct(store.items[0], 2);

    expect(store.expenses[0].confidence_level).toBeNull();
    expect(store.expenses[0].category_id).toBe(2);
    expect(store.expenses[0].category_name).toBe("snacks");
    expect(store.expenses[1].confidence_level).toBeNull();
    expect(store.expenses[1].category_id).toBe(2);
    expect(store.expenses[2].confidence_level).toBe(2);
    expect(store.expenses[2].category_id).toBe(1);
  });

  it("does not decrement doubtfulCount when correcting a certain item", async () => {
    vi.spyOn(expenseCorrections, "correctCategory").mockResolvedValueOnce({ updated_expenses_count: 1 });

    seedCatalog();
    const store = useReviewStore();
    store.items = [{ id: 100, is_doubtful: false, store: "Lidl", total: 200 }];
    store.doubtfulCount = 3;

    await store.correct(store.items[0], 2);

    expect(store.doubtfulCount).toBe(3);
  });

  it("uses updated_expenses_count from result for toast count", async () => {
    vi.spyOn(reviewApi, "approveRule").mockResolvedValueOnce({ updated_expenses_count: 5 });
    seedCatalog();
    const toast = useToastStore();
    const showSpy = vi.spyOn(toast, "show");
    const store = useReviewStore();
    store.items = [{ id: 7, expense_id: 42, is_doubtful: true, name: "hleb", count: 1 }];

    await store.correct(store.items[0], 2);

    expect(showSpy).toHaveBeenCalledWith(expect.stringContaining("5"), "success");
  });
});

describe("review store: markDirty()", () => {
  it("sets dirtyFlag to true and persists to localStorage", () => {
    const store = useReviewStore();
    expect(store.dirtyFlag).toBe(false);
    store.markDirty();
    expect(store.dirtyFlag).toBe(true);
    expect(localStorage.getItem("dinary:review:dirty")).toBe("1");
  });
});

describe("review store: loadIfNeeded()", () => {
  it("fetches (reset + page 1) when dirtyFlag is set", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [{ id: 1, is_doubtful: false }],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    const store = useReviewStore();
    store.markDirty();
    await store.loadIfNeeded();
    expect(reviewApi.getReviewFeed).toHaveBeenCalledTimes(1);
    expect(store.items).toHaveLength(1);
  });

  it("fetches when no lastFetchedAt (never loaded)", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    const store = useReviewStore();
    expect(store.lastFetchedAt).toBeNull();
    await store.loadIfNeeded();
    expect(reviewApi.getReviewFeed).toHaveBeenCalledTimes(1);
  });

  it("fetches when data is older than 24h", async () => {
    const old = Date.now() - 25 * 60 * 60 * 1000;
    localStorage.setItem("dinary:review:fetchedAt", String(old));
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    setActivePinia(createPinia());
    const store = useReviewStore();
    await store.loadIfNeeded();
    expect(reviewApi.getReviewFeed).toHaveBeenCalledTimes(1);
  });

  it("skips fetch when clean and data is recent", async () => {
    localStorage.setItem("dinary:review:fetchedAt", String(Date.now() - 60_000));
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    setActivePinia(createPinia());
    const store = useReviewStore();
    expect(store.dirtyFlag).toBe(false);
    await store.loadIfNeeded();
    expect(reviewApi.getReviewFeed).not.toHaveBeenCalled();
  });
});

describe("review store: receipts_queue clears dirty flag", () => {
  it("clears dirtyFlag when loadNextPage returns empty queue and doubtful_count=0", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    const store = useReviewStore();
    store.markDirty();
    await store.loadNextPage();
    expect(store.dirtyFlag).toBe(false);
    expect(localStorage.getItem("dinary:review:dirty")).toBeNull();
  });

  it("re-marks dirtyFlag when queue is non-empty after load (pending)", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 2, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    const store = useReviewStore();
    await store.loadNextPage();
    expect(store.dirtyFlag).toBe(true);
    expect(localStorage.getItem("dinary:review:dirty")).toBe("1");
  });

  it("re-marks dirtyFlag when queue has in_progress items", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 1, sleeping: 0, poisoned: 0 },
    });
    const store = useReviewStore();
    await store.loadNextPage();
    expect(store.dirtyFlag).toBe(true);
  });

  it("re-marks dirtyFlag when queue has sleeping or poisoned items", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 1, poisoned: 1 },
    });
    const store = useReviewStore();
    await store.loadNextPage();
    expect(store.dirtyFlag).toBe(true);
  });

  it("marks llm store dirty when receipt queue is non-empty", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 1, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    vi.spyOn(llmApi, "getStatus").mockResolvedValue({ providers: [], health: null });
    const store = useReviewStore();
    const llm = useLlmStore();
    await store.loadNextPage();
    expect(llm.dirtyFlag).toBe(true);
    expect(localStorage.getItem("dinary:llm:dirty")).toBe("1");
  });

  it("does not mark llm store dirty when receipt queue is empty", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    vi.spyOn(llmApi, "getStatus").mockResolvedValue({ providers: [], health: null });
    const store = useReviewStore();
    const llm = useLlmStore();
    await store.loadNextPage();
    expect(llm.dirtyFlag).toBe(false);
  });

  it("clears dirtyFlag even when doubtful_count > 0", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 3,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    const store = useReviewStore();
    store.markDirty();
    await store.loadNextPage();
    expect(store.dirtyFlag).toBe(false);
    expect(localStorage.getItem("dinary:review:dirty")).toBeNull();
  });

  it("populates receiptsQueue from feed response", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 1, in_progress: 2, sleeping: 3, poisoned: 0 },
    });
    const store = useReviewStore();
    await store.loadNextPage();
    expect(store.receiptsQueue.pending).toBe(1);
    expect(store.receiptsQueue.in_progress).toBe(2);
    expect(store.receiptsQueue.sleeping).toBe(3);
    expect(store.receiptsQueue.poisoned).toBe(0);
  });

  it("sets lastFetchedAt after successful loadNextPage", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    const store = useReviewStore();
    const before = Date.now();
    await store.loadNextPage();
    expect(store.lastFetchedAt).toBeGreaterThanOrEqual(before);
  });

  it("reset() clears lastFetchedAt from localStorage", () => {
    localStorage.setItem("dinary:review:fetchedAt", String(Date.now()));
    const store = useReviewStore();
    store.reset();
    expect(store.lastFetchedAt).toBeNull();
    expect(localStorage.getItem("dinary:review:fetchedAt")).toBeNull();
  });
});

describe("review store: loadNextPage() offline", () => {
  it("suppresses error toast when offline and API fails", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockRejectedValueOnce(new Error("Network error"));
    const restore = mockOnLine(false);
    try {
      const store = useReviewStore();
      const { useToastStore } = await import("../src/stores/toast.js");
      const toast = useToastStore();
      const showSpy = vi.spyOn(toast, "show");
      await store.loadNextPage();
      expect(showSpy).not.toHaveBeenCalled();
    } finally {
      restore();
    }
  });

  it("shows error toast when online and API fails", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockRejectedValueOnce(new Error("Server error"));
    const store = useReviewStore();
    const { useToastStore } = await import("../src/stores/toast.js");
    const toast = useToastStore();
    const showSpy = vi.spyOn(toast, "show");
    await store.loadNextPage();
    expect(showSpy).toHaveBeenCalledWith(expect.stringContaining("Server error"), "error");
  });
});

describe("review store: loadNextPage()", () => {
  it("appends items and tracks hasMore", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValueOnce({
      items: [{ id: 1, is_doubtful: false }],
      doubtful_count: 0,
      has_more: false,
    });

    const store = useReviewStore();
    await store.loadNextPage();

    expect(store.items).toHaveLength(1);
    expect(store.hasMore).toBe(false);
  });

  it("deduplicates incoming items by id to prevent double-display after local correction", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValueOnce({
      items: [{ id: 42, is_doubtful: false, name: "hleb" }],
      doubtful_count: 0,
      has_more: false,
    });

    const store = useReviewStore();
    // Simulate a locally-converted certain item already in the list (same id=42)
    store.items = [{ id: 42, is_doubtful: false, name: "hleb" }];

    await store.loadNextPage();

    expect(store.items).toHaveLength(1);
  });

  it("keeps a rule and correction expense that share the same numeric id", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValueOnce({
      items: [
        { id: 42, is_doubtful: true, name: "rule" },
        { id: 42, review_kind: "expense_correction", is_doubtful: true },
      ],
      doubtful_count: 2,
      has_more: false,
    });

    const store = useReviewStore();
    await store.loadNextPage();

    expect(store.items).toHaveLength(2);
  });
});

describe("review store: updateExpense()", () => {
  it("calls editExpense API with the given payload", async () => {
    const spy = vi.spyOn(expenseCorrections, "editExpense").mockResolvedValueOnce({
      id: 10,
      category_id: 20,
      category_name: "cafe",
      tag_ids: [5],
      event_id: 3,
      event_name: "Trip",
    });

    const store = useReviewStore();
    await store.updateExpense(10, { category_id: 20, tag_ids: [5], event_id: 3 });

    expect(spy).toHaveBeenCalledWith(10, { category_id: 20, tag_ids: [5], event_id: 3 });
  });

  it("shows error toast and rethrows when API fails", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockRejectedValueOnce(new Error("Server error"));
    const store = useReviewStore();
    const toast = useToastStore();
    const showSpy = vi.spyOn(toast, "show");
    await expect(store.updateExpense(10, {})).rejects.toThrow("Server error");
    expect(showSpy).toHaveBeenCalledWith(expect.stringContaining("Server error"), "error");
  });

  it("removes matching doubtful rule from items when update_rule is true", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockResolvedValueOnce({});
    const store = useReviewStore();
    store.items = [
      { id: 7, expense_id: 42, is_doubtful: true, name: "hleb" },
      { id: 8, expense_id: 99, is_doubtful: true, name: "mleko" },
    ];
    store.doubtfulCount = 2;

    await store.updateExpense(42, { update_rule: true });

    expect(store.items.map((i) => i.id)).toEqual([8]);
    expect(store.doubtfulCount).toBe(1);
  });

  it("removes a correction after its category is saved from the expense sheet", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockResolvedValueOnce({});
    const store = useReviewStore();
    store.items = [
      {
        id: 42,
        expense_id: 42,
        review_kind: "expense_correction",
        is_doubtful: true,
      },
      { id: 42, expense_id: 99, is_doubtful: true, name: "rule" },
    ];
    store.expenses = [{ id: 42, confidence_level: 1 }];
    store.doubtfulCount = 2;

    await store.updateExpense(42, { category_id: 2, update_rule: false });

    expect(store.items).toEqual([
      { id: 42, expense_id: 99, is_doubtful: true, name: "rule" },
    ]);
    expect(store.expenses[0].confidence_level).toBe(4);
    expect(store.doubtfulCount).toBe(1);
  });

  it("clears confidence_level on sibling expenses with same rule_id when update_rule is true", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockResolvedValueOnce({});
    const store = useReviewStore();
    store.items = [{ id: 7, expense_id: 42, is_doubtful: true, name: "hleb" }];
    store.expenses = [
      { id: 42, rule_id: 7, confidence_level: 2, category_id: 1, category_name: "old" },
      { id: 43, rule_id: 7, confidence_level: 2, category_id: 1, category_name: "old" },
      { id: 44, rule_id: 9, confidence_level: 2, category_id: 1, category_name: "old" },
    ];
    store.doubtfulCount = 1;

    await store.updateExpense(42, { update_rule: true, scope: "single", category_id: 2 });

    expect(store.expenses[0].confidence_level).toBeNull();
    expect(store.expenses[1].confidence_level).toBeNull();
    expect(store.expenses[2].confidence_level).toBe(2);
    // scope=single: category not updated on siblings
    expect(store.expenses[1].category_id).toBe(1);
    expect(store.expenses[1].category_name).toBe("old");
  });

  it("updates category on siblings when update_rule is true and scope is broader than single", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockResolvedValueOnce({});
    seedCatalog();
    const store = useReviewStore();
    store.items = [{ id: 7, expense_id: 42, is_doubtful: true, name: "hleb" }];
    store.expenses = [
      { id: 42, rule_id: 7, confidence_level: 2, category_id: 1, category_name: "old" },
      { id: 43, rule_id: 7, confidence_level: 2, category_id: 1, category_name: "old" },
      { id: 44, rule_id: 9, confidence_level: 2, category_id: 1, category_name: "old" },
    ];
    store.doubtfulCount = 1;

    await store.updateExpense(42, { update_rule: true, scope: "month", category_id: 2 });

    expect(store.expenses[0].confidence_level).toBeNull();
    expect(store.expenses[0].category_id).toBe(2);
    expect(store.expenses[0].category_name).toBe("snacks");
    expect(store.expenses[1].confidence_level).toBeNull();
    expect(store.expenses[1].category_id).toBe(2);
    expect(store.expenses[1].category_name).toBe("snacks");
    // different rule_id: untouched
    expect(store.expenses[2].confidence_level).toBe(2);
    expect(store.expenses[2].category_id).toBe(1);
  });

  it("does not modify items when update_rule is false", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockResolvedValueOnce({});
    const store = useReviewStore();
    store.items = [{ id: 7, expense_id: 42, is_doubtful: true, name: "hleb" }];
    store.doubtfulCount = 1;

    await store.updateExpense(42, { update_rule: false });

    expect(store.items).toHaveLength(1);
    expect(store.doubtfulCount).toBe(1);
  });

  it("does not remove certain items (is_doubtful: false) even when update_rule is true", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockResolvedValueOnce({});
    const store = useReviewStore();
    store.items = [{ id: 7, expense_id: 42, is_doubtful: false, name: "hleb" }];
    store.doubtfulCount = 0;

    await store.updateExpense(42, { update_rule: true });

    expect(store.items).toHaveLength(1);
    expect(store.doubtfulCount).toBe(0);
  });
});

describe("review store: patchExpense()", () => {
  it("merges patch fields onto the matching expense", () => {
    const store = useReviewStore();
    store.expenses = [
      { id: 10, category_id: 1, category_name: "old", tags: [], event_id: null },
      { id: 20, category_id: 2, category_name: "other", tags: [], event_id: null },
    ];

    store.patchExpense(10, {
      category_id: 5,
      category_name: "new",
      tags: [{ id: 3, name: "собака" }],
      event_id: null,
      event_name: null,
    });

    expect(store.expenses[0].category_id).toBe(5);
    expect(store.expenses[0].category_name).toBe("new");
    expect(store.expenses[0].tags).toEqual([{ id: 3, name: "собака" }]);
  });

  it("does not touch other expenses", () => {
    const store = useReviewStore();
    store.expenses = [
      { id: 10, category_name: "a", tags: [] },
      { id: 20, category_name: "b", tags: [] },
    ];

    store.patchExpense(10, { category_name: "updated" });

    expect(store.expenses[1].category_name).toBe("b");
  });

  it("is a no-op when the id is not found", () => {
    const store = useReviewStore();
    store.expenses = [{ id: 10, category_name: "a", tags: [] }];

    store.patchExpense(99, { category_name: "updated" });

    expect(store.expenses[0].category_name).toBe("a");
  });

  it("replaces the array reference so v-for re-renders", () => {
    const store = useReviewStore();
    const before = store.expenses;
    store.expenses = [{ id: 10, category_name: "a", tags: [] }];
    const afterSet = store.expenses;

    store.patchExpense(10, { category_name: "b" });

    expect(store.expenses).not.toBe(before);
    expect(store.expenses).not.toBe(afterSet);
  });
});

describe("review store: confirmAll()", () => {
  beforeEach(() => {
    vi.spyOn(reviewApi, "getExpensesFeed").mockResolvedValue({ items: [], has_more: false });
  });

  it("removes confirmed items from items list", async () => {
    vi.spyOn(reviewApi, "confirmAllRules").mockResolvedValueOnce({ confirmed: 2 });
    const store = useReviewStore();
    store.items = [
      { id: 1, is_doubtful: true },
      { id: 2, is_doubtful: true },
      { id: 3, is_doubtful: false },
    ];
    store.doubtfulCount = 2;
    await store.confirmAll([1, 2]);
    expect(store.items.map((i) => i.id)).toEqual([3]);
  });

  it("decrements doubtfulCount by confirmed count", async () => {
    vi.spyOn(reviewApi, "confirmAllRules").mockResolvedValueOnce({ confirmed: 2 });
    const store = useReviewStore();
    store.items = [
      { id: 1, is_doubtful: true },
      { id: 2, is_doubtful: true },
    ];
    store.doubtfulCount = 3;
    await store.confirmAll([1, 2]);
    expect(store.doubtfulCount).toBe(1);
  });

  it("never removes a correction expense with the same id as a confirmed rule", async () => {
    vi.spyOn(reviewApi, "confirmAllRules").mockResolvedValueOnce({ confirmed: 1 });
    const store = useReviewStore();
    store.items = [
      { id: 1, is_doubtful: true },
      { id: 1, review_kind: "expense_correction", is_doubtful: true },
    ];
    store.doubtfulCount = 2;

    await store.confirmAll([1]);

    expect(store.items).toEqual([
      { id: 1, review_kind: "expense_correction", is_doubtful: true },
    ]);
    expect(store.doubtfulCount).toBe(1);
  });

  it("shows success toast with count", async () => {
    vi.spyOn(reviewApi, "confirmAllRules").mockResolvedValueOnce({ confirmed: 3 });
    const store = useReviewStore();
    store.items = [{ id: 1, is_doubtful: true }, { id: 2, is_doubtful: true }, { id: 3, is_doubtful: true }];
    store.doubtfulCount = 3;
    const toast = useToastStore();
    const showSpy = vi.spyOn(toast, "show");
    await store.confirmAll([1, 2, 3]);
    expect(showSpy).toHaveBeenCalledWith(expect.stringContaining("3"), "success");
  });

  it("shows error toast when API fails", async () => {
    vi.spyOn(reviewApi, "confirmAllRules").mockRejectedValueOnce(new Error("Server error"));
    const store = useReviewStore();
    const toast = useToastStore();
    const showSpy = vi.spyOn(toast, "show");
    await store.confirmAll([1]);
    expect(showSpy).toHaveBeenCalledWith(expect.stringContaining("Server error"), "error");
  });

  it("reloads expenses after confirming so updated confidence_level is reflected", async () => {
    vi.spyOn(reviewApi, "confirmAllRules").mockResolvedValueOnce({ confirmed: 1 });
    vi.spyOn(reviewApi, "getExpensesFeed").mockResolvedValueOnce({
      items: [{ id: 10, confidence_level: 4, category_name: "food" }],
      has_more: false,
    });
    const store = useReviewStore();
    store.items = [{ id: 1, expense_id: 10, is_doubtful: true }];
    store.expenses = [{ id: 10, confidence_level: 2, category_name: "food" }];
    store.expensesPage = 1;

    await store.confirmAll([1]);

    expect(reviewApi.getExpensesFeed).toHaveBeenCalled();
    expect(store.expenses[0].confidence_level).toBe(4);
  });
});

describe("review store: setOpenRow()", () => {
  it("sets openRowId to the given id", () => {
    const store = useReviewStore();
    expect(store.openRowId).toBeNull();
    store.setOpenRow(42);
    expect(store.openRowId).toBe(42);
  });

  it("updates openRowId when called again with a different id", () => {
    const store = useReviewStore();
    store.setOpenRow(1);
    store.setOpenRow(2);
    expect(store.openRowId).toBe(2);
  });
});

describe("review store: deleteExpense()", () => {
  it("calls deleteExpense API and removes the expense from the list", async () => {
    vi.spyOn(expensesApi, "deleteExpense").mockResolvedValueOnce(null);
    const store = useReviewStore();
    store.expenses = [
      { id: 10, category_name: "food", receipt_id: null },
      { id: 20, category_name: "transport", receipt_id: null },
    ];

    await store.deleteExpense(10);

    expect(expensesApi.deleteExpense).toHaveBeenCalledWith(10);
    expect(store.expenses.map((e) => e.id)).toEqual([20]);
  });

  it("marks the cache dirty after deleting an expense", async () => {
    vi.spyOn(expensesApi, "deleteExpense").mockResolvedValueOnce(null);
    const store = useReviewStore();
    store.expenses = [{ id: 5, receipt_id: null }];

    await store.deleteExpense(5);

    expect(store.dirtyFlag).toBe(true);
  });

  it("does not swallow API errors", async () => {
    vi.spyOn(expensesApi, "deleteExpense").mockRejectedValueOnce(new Error("Not found"));
    const store = useReviewStore();
    store.expenses = [{ id: 7, receipt_id: null }];

    await expect(store.deleteExpense(7)).rejects.toThrow("Not found");
    expect(store.expenses).toHaveLength(1);
  });
});

describe("review store: deleteReceipt()", () => {
  it("calls deleteReceipt API", async () => {
    vi.spyOn(receiptsApi, "deleteReceipt").mockResolvedValueOnce(null);
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValueOnce({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    vi.spyOn(reviewApi, "getExpensesFeed").mockResolvedValueOnce({ items: [], has_more: false });
    const store = useReviewStore();
    store.expenses = [{ id: 1, receipt_id: 7 }];

    await store.deleteReceipt(7);

    expect(receiptsApi.deleteReceipt).toHaveBeenCalledWith(7);
  });

  it("reloads the rules feed after deleting a receipt", async () => {
    vi.spyOn(receiptsApi, "deleteReceipt").mockResolvedValueOnce(null);
    const feedSpy = vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValueOnce({
      items: [{ id: 99, is_doubtful: true }],
      doubtful_count: 1,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    vi.spyOn(reviewApi, "getExpensesFeed").mockResolvedValueOnce({ items: [], has_more: false });
    const store = useReviewStore();

    await store.deleteReceipt(5);

    expect(feedSpy).toHaveBeenCalledTimes(1);
    expect(store.items.map((i) => i.id)).toEqual([99]);
  });

  it("reloads the expenses feed after deleting a receipt", async () => {
    vi.spyOn(receiptsApi, "deleteReceipt").mockResolvedValueOnce(null);
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValueOnce({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    const expSpy = vi.spyOn(reviewApi, "getExpensesFeed").mockResolvedValueOnce({
      items: [{ id: 10, receipt_id: 9 }],
      has_more: false,
    });
    const store = useReviewStore();

    await store.deleteReceipt(5);

    expect(expSpy).toHaveBeenCalledTimes(1);
    expect(store.expenses.map((e) => e.id)).toEqual([10]);
  });

  it("does not swallow API errors", async () => {
    vi.spyOn(receiptsApi, "deleteReceipt").mockRejectedValueOnce(new Error("Not found"));
    const store = useReviewStore();
    store.expenses = [{ id: 1, receipt_id: 3 }];

    await expect(store.deleteReceipt(3)).rejects.toThrow("Not found");
    expect(store.expenses).toHaveLength(1);
  });
});

describe("review store: expenses localStorage cache", () => {
  it("initialises expenses from localStorage on store creation", () => {
    localStorage.setItem("dinary:review:expenses:v1", JSON.stringify({
      items: [{ id: 1, category_name: "food" }, { id: 2, category_name: "transport" }],
      page: 1,
      hasMore: false,
    }));
    setActivePinia(createPinia());
    const store = useReviewStore();
    expect(store.expenses).toHaveLength(2);
    expect(store.expensesPage).toBe(1);
    expect(store.expensesHasMore).toBe(false);
  });

  it("persists expenses to localStorage after loadExpensesNextPage succeeds", async () => {
    vi.spyOn(reviewApi, "getExpensesFeed").mockResolvedValueOnce({
      items: [{ id: 10, category_name: "food" }],
      has_more: false,
    });
    const store = useReviewStore();
    await store.loadExpensesNextPage();
    const cached = JSON.parse(localStorage.getItem("dinary:review:expenses:v1"));
    expect(cached.items).toHaveLength(1);
    expect(cached.items[0].id).toBe(10);
    expect(cached.page).toBe(1);
    expect(cached.hasMore).toBe(false);
  });

  it("loadExpensesIfNeeded skips fetch when not stale and cache is loaded (page > 0)", async () => {
    localStorage.setItem("dinary:review:fetchedAt", String(Date.now() - 60_000));
    localStorage.setItem("dinary:review:expenses:v1", JSON.stringify({
      items: [{ id: 5, category_name: "food" }],
      page: 1,
      hasMore: false,
    }));
    const feedSpy = vi.spyOn(reviewApi, "getExpensesFeed");
    setActivePinia(createPinia());
    const store = useReviewStore();
    await store.loadExpensesIfNeeded();
    expect(feedSpy).not.toHaveBeenCalled();
    expect(store.expenses).toHaveLength(1);
  });

  it("loadExpensesIfNeeded refetches when stale even if page > 0 from cache", async () => {
    localStorage.setItem("dinary:review:dirty", "1");
    localStorage.setItem("dinary:review:expenses:v1", JSON.stringify({
      items: [{ id: 5, category_name: "food" }],
      page: 1,
      hasMore: false,
    }));
    vi.spyOn(reviewApi, "getExpensesFeed").mockResolvedValueOnce({
      items: [{ id: 99, category_name: "new" }],
      has_more: false,
    });
    setActivePinia(createPinia());
    const store = useReviewStore();
    await store.loadExpensesIfNeeded();
    expect(reviewApi.getExpensesFeed).toHaveBeenCalledTimes(1);
    expect(store.expenses[0].id).toBe(99);
  });

  it("resetExpenses clears the localStorage cache", async () => {
    localStorage.setItem("dinary:review:expenses:v1", JSON.stringify({
      items: [{ id: 1 }], page: 1, hasMore: false,
    }));
    const store = useReviewStore();
    store.resetExpenses();
    expect(localStorage.getItem("dinary:review:expenses:v1")).toBeNull();
    expect(store.expenses).toHaveLength(0);
    expect(store.expensesPage).toBe(0);
  });

  it("reset() clears the expenses localStorage cache", () => {
    localStorage.setItem("dinary:review:expenses:v1", JSON.stringify({
      items: [{ id: 1 }], page: 1, hasMore: false,
    }));
    const store = useReviewStore();
    store.reset();
    expect(localStorage.getItem("dinary:review:expenses:v1")).toBeNull();
  });

  it("patchExpense persists updated expenses to localStorage", () => {
    localStorage.setItem("dinary:review:expenses:v1", JSON.stringify({
      items: [{ id: 10, category_name: "old" }], page: 1, hasMore: false,
    }));
    setActivePinia(createPinia());
    const store = useReviewStore();
    store.patchExpense(10, { category_name: "new" });
    const cached = JSON.parse(localStorage.getItem("dinary:review:expenses:v1"));
    expect(cached.items[0].category_name).toBe("new");
  });

  it("deleteExpense persists removal to localStorage", async () => {
    vi.spyOn(expensesApi, "deleteExpense").mockResolvedValueOnce(null);
    localStorage.setItem("dinary:review:expenses:v1", JSON.stringify({
      items: [{ id: 10 }, { id: 20 }], page: 1, hasMore: false,
    }));
    setActivePinia(createPinia());
    const store = useReviewStore();
    await store.deleteExpense(10);
    const cached = JSON.parse(localStorage.getItem("dinary:review:expenses:v1"));
    expect(cached.items.map((e) => e.id)).toEqual([20]);
  });
});

describe("review store: loadStuckReceipts()", () => {
  it("populates stuckReceipts from getReceiptQueue", async () => {
    vi.spyOn(receiptsApi, "getReceiptQueue").mockResolvedValueOnce({
      items: [{ receipt_id: 1, status: "poisoned", amount: 100, currency: "RSD" }],
      has_more: false,
    });
    const store = useReviewStore();

    await store.loadStuckReceipts();

    expect(store.stuckReceipts).toHaveLength(1);
    expect(store.stuckReceipts[0].receipt_id).toBe(1);
    expect(store.stuckReceiptsLoading).toBe(false);
  });

  it("does not run a second load while one is in flight", async () => {
    let resolveFirst;
    const spy = vi.spyOn(receiptsApi, "getReceiptQueue").mockReturnValueOnce(
      new Promise((resolve) => { resolveFirst = resolve; }),
    );
    const store = useReviewStore();

    const first = store.loadStuckReceipts();
    const second = store.loadStuckReceipts();
    resolveFirst({ items: [], has_more: false });
    await Promise.all([first, second]);

    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("shows a toast on failure while online", async () => {
    vi.spyOn(receiptsApi, "getReceiptQueue").mockRejectedValueOnce(new Error("boom"));
    const restoreOnline = mockOnLine(true);
    const toast = useToastStore();
    const showSpy = vi.spyOn(toast, "show");
    const store = useReviewStore();

    await store.loadStuckReceipts();

    expect(showSpy).toHaveBeenCalledWith("boom", "error");
    expect(store.stuckReceiptsLoading).toBe(false);
    restoreOnline();
  });

  it("triggers automatically as part of loadNextPage when the queue is non-empty", async () => {
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValueOnce({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 1, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    const queueSpy = vi.spyOn(receiptsApi, "getReceiptQueue").mockResolvedValueOnce({
      items: [{ receipt_id: 1, status: "pending", amount: 50, currency: "RSD" }],
      has_more: false,
    });
    const store = useReviewStore();

    await store.loadNextPage();

    expect(queueSpy).toHaveBeenCalledTimes(1);
    expect(store.stuckReceipts).toHaveLength(1);
  });

  it("refreshes (and clears) on the first page even when the queue is empty", async () => {
    vi.spyOn(receiptsApi, "getReceiptQueue").mockResolvedValueOnce({
      items: [{ receipt_id: 1, status: "poisoned", amount: 100, currency: "RSD" }],
      has_more: false,
    });
    const store = useReviewStore();
    await store.loadStuckReceipts();
    expect(store.stuckReceipts).toHaveLength(1);

    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValueOnce({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    const queueSpy = vi.spyOn(receiptsApi, "getReceiptQueue").mockResolvedValueOnce({
      items: [],
      has_more: false,
    });

    await store.loadNextPage();

    expect(queueSpy).toHaveBeenCalledTimes(1);
    expect(store.stuckReceipts).toHaveLength(0);
  });
});

describe("review store: resolveStuckReceipt()", () => {
  it("calls resolveReceipt and removes the item from stuckReceipts", async () => {
    vi.spyOn(receiptsApi, "resolveReceipt").mockResolvedValueOnce(null);
    vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    vi.spyOn(reviewApi, "getExpensesFeed").mockResolvedValue({ items: [], has_more: false });
    vi.spyOn(receiptsApi, "getReceiptQueue").mockResolvedValue({
      items: [{ receipt_id: 2, status: "pending", amount: 50, currency: "RSD" }],
      has_more: false,
    });
    const store = useReviewStore();
    store.stuckReceipts = [
      { receipt_id: 1, status: "poisoned", amount: 100, currency: "RSD" },
      { receipt_id: 2, status: "pending", amount: 50, currency: "RSD" },
    ];

    await store.resolveStuckReceipt(1, { categoryId: 3 });

    expect(receiptsApi.resolveReceipt).toHaveBeenCalledWith(1, { categoryId: 3 });
    expect(store.stuckReceipts.map((i) => i.receipt_id)).toEqual([2]);
  });

  it("shows a success toast and refreshes the review feed and expenses", async () => {
    vi.spyOn(receiptsApi, "resolveReceipt").mockResolvedValueOnce(null);
    const feedSpy = vi.spyOn(reviewApi, "getReviewFeed").mockResolvedValue({
      items: [],
      doubtful_count: 0,
      has_more: false,
      receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 },
    });
    const expensesSpy = vi.spyOn(reviewApi, "getExpensesFeed").mockResolvedValue({ items: [], has_more: false });
    vi.spyOn(receiptsApi, "getReceiptQueue").mockResolvedValue({ items: [], has_more: false });
    const toast = useToastStore();
    const showSpy = vi.spyOn(toast, "show");
    const store = useReviewStore();
    store.stuckReceipts = [{ receipt_id: 1, status: "poisoned", amount: 100, currency: "RSD" }];

    await store.resolveStuckReceipt(1, { categoryId: 3 });

    expect(feedSpy).toHaveBeenCalled();
    expect(expensesSpy).toHaveBeenCalled();
    expect(showSpy).toHaveBeenCalledWith("Expense created", "success");
  });

  it("propagates errors from resolveReceipt without removing the item", async () => {
    vi.spyOn(receiptsApi, "resolveReceipt").mockRejectedValueOnce(
      Object.assign(new Error("Receipt already resolved"), { status: 409 }),
    );
    const store = useReviewStore();
    store.stuckReceipts = [{ receipt_id: 1, status: "poisoned", amount: 100, currency: "RSD" }];

    await expect(store.resolveStuckReceipt(1, { categoryId: 3 })).rejects.toMatchObject({ status: 409 });
    expect(store.stuckReceipts).toHaveLength(1);
  });
});
