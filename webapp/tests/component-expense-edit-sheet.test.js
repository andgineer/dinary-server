/**
 * Interaction tests for ExpenseEditSheet.save().
 *
 * Unit tests for individual store functions (updateExpense, patchExpense) cannot
 * catch bugs where save() forgets to call patchExpense at all. These tests
 * exercise the full save path: mount the sheet with a real Pinia store, trigger
 * save, and assert that reviewStore.expenses reflects the change — no server
 * round-trip needed to see the update.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ExpenseEditSheet from "../src/components/ExpenseEditSheet.vue";
import { useReviewStore } from "../src/stores/review.js";
import { useCatalogStore } from "../src/stores/catalog.js";
import * as expenseCorrections from "../src/api/expenseCorrections.js";
import * as expensesApi from "../src/api/expenses.js";
import * as receiptsApi from "../src/api/receipts.js";

beforeEach(async () => {
  await allure.epic("Expenses");
  await allure.feature("Frontend");
  await allure.story("ExpenseEditSheet");
});

const TELEPORT_STUB = { template: "<div><slot /></div>" };

function seedCatalog() {
  const catalog = useCatalogStore();
  catalog.replaceSnapshot({
    catalog_version: 1,
    category_groups: [{ id: 1, name: "food", is_active: true }],
    categories: [
      { id: 1, group_id: 1, name: "еда", is_active: true },
      { id: 2, group_id: 1, name: "cafe", is_active: true },
    ],
    events: [],
    tags: [
      { id: 10, name: "собака", is_active: true },
      { id: 11, name: "аня", is_active: true },
    ],
  });
}

function mountSheet(expense, pinia) {
  return mount(ExpenseEditSheet, {
    props: { open: true, expense, suggestions: [], ruleItem: null },
    global: { plugins: [pinia], stubs: { Teleport: TELEPORT_STUB, CategorySheet: true } },
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ExpenseEditSheet — save() patches local expense list", () => {
  it("adds a tag to the expense in reviewStore.expenses without a page reload", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockResolvedValue({
      id: 42,
      category_id: 1,
      category_name: "еда",
      tag_ids: [10],
      event_id: null,
      event_name: null,
    });

    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();

    const reviewStore = useReviewStore();
    reviewStore.expenses = [
      { id: 42, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: null, amount_original: 100, currency_original: "RSD" },
    ];

    const expense = reviewStore.expenses[0];
    const wrapper = mountSheet(expense, pinia);

    await wrapper.find('[data-testid="tag-toggle-10"]').trigger("click");
    await wrapper.find('[data-testid="save-btn"]').trigger("click");
    await flushPromises();

    expect(reviewStore.expenses[0].tags.map((t) => t.id)).toContain(10);
    wrapper.unmount();
  });

  it("updates the category in reviewStore.expenses immediately after save", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockResolvedValue({
      id: 42,
      category_id: 2,
      category_name: "cafe",
      tag_ids: [],
      event_id: null,
      event_name: null,
    });

    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();

    const reviewStore = useReviewStore();
    reviewStore.expenses = [
      { id: 42, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: null, amount_original: 100, currency_original: "RSD" },
    ];

    const expense = reviewStore.expenses[0];
    const wrapper = mountSheet(expense, pinia);

    reviewStore.patchExpense(42, { category_id: 2, category_name: "cafe" });
    await flushPromises();

    expect(reviewStore.expenses[0].category_id).toBe(2);
    expect(reviewStore.expenses[0].category_name).toBe("cafe");
    wrapper.unmount();
  });

  it("does NOT update other expenses when one is saved", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockResolvedValue({
      id: 42,
      category_id: 1,
      category_name: "еда",
      tag_ids: [10],
      event_id: null,
      event_name: null,
    });

    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();

    const reviewStore = useReviewStore();
    reviewStore.expenses = [
      { id: 42, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: null, amount_original: 100, currency_original: "RSD" },
      { id: 99, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: null },
    ];

    const expense = reviewStore.expenses[0];
    const wrapper = mountSheet(expense, pinia);

    await wrapper.find('[data-testid="tag-toggle-10"]').trigger("click");
    await wrapper.find('[data-testid="save-btn"]').trigger("click");
    await flushPromises();

    expect(reviewStore.expenses[1].tags).toEqual([]);
    wrapper.unmount();
  });
});

describe("ExpenseEditSheet — amount field (manual expense)", () => {
  it("shows the amount block for a manual expense (receipt_id null)", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();
    const reviewStore = useReviewStore();
    reviewStore.expenses = [
      { id: 42, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: null, amount_original: 480, currency_original: "RSD" },
    ];
    const wrapper = mountSheet(reviewStore.expenses[0], pinia);
    expect(wrapper.find('[data-testid="amount-block"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("hides the amount block for a receipt-backed expense", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();
    const reviewStore = useReviewStore();
    reviewStore.expenses = [
      { id: 10, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: 7, amount_original: 200, currency_original: "RSD" },
    ];
    const wrapper = mountSheet(reviewStore.expenses[0], pinia);
    expect(wrapper.find('[data-testid="amount-block"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("patches amount_original and currency_original after save for manual expense", async () => {
    vi.spyOn(expenseCorrections, "editExpense").mockResolvedValue({});
    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();
    const reviewStore = useReviewStore();
    reviewStore.expenses = [
      { id: 42, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: null, amount_original: 100, currency_original: "RSD" },
    ];
    const expense = reviewStore.expenses[0];
    const wrapper = mountSheet(expense, pinia);

    await wrapper.find('[data-testid="amount-input"]').setValue("250");
    await wrapper.find('[data-testid="save-btn"]').trigger("click");
    await flushPromises();

    expect(reviewStore.expenses[0].amount_original).toBe(250);
    wrapper.unmount();
  });
});

describe("ExpenseEditSheet — FROM RECEIPT pill", () => {
  it("shows FROM RECEIPT pill for receipt-backed expense", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();
    const reviewStore = useReviewStore();
    reviewStore.expenses = [
      { id: 10, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: 7, amount_original: 200, currency_original: "RSD" },
    ];
    const wrapper = mountSheet(reviewStore.expenses[0], pinia);
    expect(wrapper.find('[data-testid="from-receipt-pill"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("hides FROM RECEIPT pill for manual expense", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();
    const reviewStore = useReviewStore();
    reviewStore.expenses = [
      { id: 42, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: null, amount_original: 100, currency_original: "RSD" },
    ];
    const wrapper = mountSheet(reviewStore.expenses[0], pinia);
    expect(wrapper.find('[data-testid="from-receipt-pill"]').exists()).toBe(false);
    wrapper.unmount();
  });
});

describe("ExpenseEditSheet — correction classification", () => {
  it("hides rule scope controls for a correction without a receipt item", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();
    const correction = {
      id: 90,
      category_id: 1,
      category_name: "еда",
      tags: [],
      event_id: null,
      review_kind: "expense_correction",
      receipt_id: 7,
      item_name: null,
      has_rule: false,
      amount_original: 80,
      receipt_total: 100,
      currency_original: "RSD",
    };

    const wrapper = mountSheet(correction, pinia);

    expect(wrapper.find('[data-testid="scope-selector"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="update-rule-wrap"]').exists()).toBe(false);
    expect(wrapper.get('[data-testid="correction-summary-amount"]').text()).toContain("80 RSD");
    expect(wrapper.get('[data-testid="correction-summary-receipt-total"]').text()).toContain(
      "100 RSD",
    );
    wrapper.unmount();
  });

  it("keeps scope controls for a classified receipt item", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();
    const expense = {
      id: 10,
      category_id: 1,
      category_name: "еда",
      tags: [],
      event_id: null,
      receipt_id: 7,
      item_name: "hleb",
      has_rule: true,
      amount_original: 80,
      currency_original: "RSD",
    };

    const wrapper = mountSheet(expense, pinia);

    expect(wrapper.find('[data-testid="scope-selector"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="correction-summary"]').exists()).toBe(false);
    wrapper.unmount();
  });
});

describe("ExpenseEditSheet — delete flow (manual)", () => {
  it("shows delete button for an expense", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();
    const reviewStore = useReviewStore();
    reviewStore.expenses = [
      { id: 42, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: null, amount_original: 100, currency_original: "RSD" },
    ];
    const wrapper = mountSheet(reviewStore.expenses[0], pinia);
    expect(wrapper.find('[data-testid="delete-btn"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("calls reviewStore.deleteExpense and emits close after confirming delete", async () => {
    vi.spyOn(expensesApi, "deleteExpense").mockResolvedValueOnce(null);
    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();
    const reviewStore = useReviewStore();
    reviewStore.expenses = [
      { id: 42, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: null, amount_original: 100, currency_original: "RSD" },
    ];
    const wrapper = mountSheet(reviewStore.expenses[0], pinia);

    await wrapper.find('[data-testid="delete-btn"]').trigger("click");
    await wrapper.find('[data-testid="confirm-delete"]').trigger("click");
    await flushPromises();

    expect(expensesApi.deleteExpense).toHaveBeenCalledWith(42);
    expect(wrapper.emitted("close")).toBeTruthy();
    wrapper.unmount();
  });
});

describe("ExpenseEditSheet — delete flow (receipt-backed)", () => {
  it("calls reviewStore.deleteReceipt and emits close after confirming cascade delete", async () => {
    vi.spyOn(receiptsApi, "getReceipt").mockResolvedValueOnce({
      id: 7,
      merchant: "Maxi",
      captured_at: "2026-05-10T12:00:00",
      expenses: [
        { id: 10, item_name: "hleb", amount: 100, currency: "RSD" },
        { id: 11, item_name: "mleko", amount: 80, currency: "RSD" },
      ],
      total: { amount: 180, currency: "RSD" },
    });
    vi.spyOn(receiptsApi, "deleteReceipt").mockResolvedValueOnce(null);
    const pinia = createPinia();
    setActivePinia(pinia);
    seedCatalog();
    const reviewStore = useReviewStore();
    vi.spyOn(reviewStore, "deleteReceipt").mockImplementation(async (id) => {
      await receiptsApi.deleteReceipt(id);
    });
    reviewStore.expenses = [
      { id: 10, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: 7, amount_original: 100, currency_original: "RSD" },
      { id: 11, category_id: 1, category_name: "еда", tags: [], event_id: null, receipt_id: 7, amount_original: 80, currency_original: "RSD" },
    ];
    const wrapper = mountSheet(reviewStore.expenses[0], pinia);

    await wrapper.find('[data-testid="delete-btn"]').trigger("click");
    await flushPromises();
    await wrapper.find('[data-testid="confirm-delete"]').trigger("click");
    await flushPromises();

    expect(receiptsApi.deleteReceipt).toHaveBeenCalledWith(7);
    expect(wrapper.emitted("close")).toBeTruthy();
    wrapper.unmount();
  });
});
