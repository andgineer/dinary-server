import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ReviewView from "../src/views/ReviewView.vue";
import { useReviewStore } from "../src/stores/review.js";
import { useCatalogStore } from "../src/stores/catalog.js";
import * as reviewApi from "../src/api/review.js";

beforeEach(async () => {
  await allure.epic("Review & Rules");
  await allure.feature("Frontend");
  await allure.story("ReviewView");
});

vi.mock("../src/api/review.js", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    getReviewFeed: vi.fn(async () => ({ items: [], doubtful_count: 0, has_more: false, receipts_queue: { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 } })),
    getExpensesFeed: vi.fn(async () => ({ items: [], has_more: false, total: 0 })),
  };
});

// Page-1 loads trigger loadStuckReceipts() -> getReceiptQueue(); mock it so
// real loadIfNeeded()/loadNextPage() runs (e.g. via onMounted) never fall
// through to a real fetch().
vi.mock("../src/api/receipts.js", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    getReceiptQueue: vi.fn(async () => ({ items: [], has_more: false })),
  };
});

const FEED_PAGE_1 = {
  doubtful_count: 2,
  has_more: false,
  items: [
    {
      id: 1,
      is_doubtful: true,
      name: "Chocolate bar",
      store: "Lidl",
      total: 150,
      currency: "RSD",
      count: 3,
      confidence_level: 3,
      current_category_id: 10,
      suggested_category_id: 10,
      datetime: "2026-05-10T10:00:00",
    },
    {
      id: 2,
      is_doubtful: false,
      store: "Maxi",
      items_count: 8,
      total: 2000,
      currency: "RSD",
      datetime: "2026-05-09T14:00:00",
      top_categories: [{ id: 10, n: 5 }],
    },
  ],
};

const CATALOG = {
  catalog_version: 1,
  category_groups: [{ id: 1, name: "Food", is_active: true }],
  categories: [{ id: 10, group_id: 1, name: "groceries", is_active: true }],
  events: [],
  tags: [],
};

const TELEPORT_STUB = { props: ["to", "disabled"], template: "<div><slot /></div>" };

const CategorySheetStub = {
  name: "CategorySheet",
  props: ["open", "title", "suggestions"],
  emits: ["select", "close"],
  template:
    '<div v-if="open" data-testid="category-sheet-stub">' +
    '<button data-testid="pick-category" type="button" @click="$emit(\'select\', 5)">pick</button>' +
    "</div>",
};

function mountView(pinia) {
  return mount(ReviewView, {
    global: {
      plugins: [pinia],
      stubs: { Teleport: TELEPORT_STUB, CategorySheet: CategorySheetStub },
    },
  });
}

let observerCallbacks = [];
let observerInstance = null;

// Convenience: fires the first (rules) observer
function fireRulesObserver(entries) {
  if (observerCallbacks[0]) observerCallbacks[0](entries);
}

beforeEach(() => {
  localStorage.clear();
  const pinia = createPinia();
  setActivePinia(pinia);

  observerCallbacks = [];
  observerInstance = { observe: vi.fn(), disconnect: vi.fn() };
  globalThis.IntersectionObserver = vi.fn((cb) => {
    observerCallbacks.push(cb);
    return observerInstance;
  });
});

afterEach(() => {
  localStorage.clear();
  delete globalThis.IntersectionObserver;
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

describe("ReviewView offline", () => {
  it("does not call loadNextPage when offline on mount", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const restore = mockOnLine(false);
    try {
      const review = useReviewStore(pinia);
      const spy = vi.spyOn(review, "loadNextPage").mockResolvedValue();
      const wrapper = mountView(pinia);
      await flushPromises();
      expect(spy).not.toHaveBeenCalled();
      wrapper.unmount();
    } finally {
      restore();
    }
  });

  it("does not trigger loadNextPage from IntersectionObserver when offline", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const restore = mockOnLine(false);
    try {
      const review = useReviewStore(pinia);
      const spy = vi.spyOn(review, "loadNextPage").mockResolvedValue();
      const wrapper = mountView(pinia);
      await flushPromises();

      review.hasMore = true;
      fireRulesObserver([{ isIntersecting: true }]);
      await flushPromises();

      expect(spy).not.toHaveBeenCalled();
      wrapper.unmount();
    } finally {
      restore();
    }
  });

  it("enables the Refresh button while online (regression: isOnline.value in template)", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const restore = mockOnLine(true);
    try {
      const review = useReviewStore(pinia);
      vi.spyOn(review, "loadNextPage").mockResolvedValue();
      const wrapper = mountView(pinia);
      await flushPromises();

      const btn = wrapper.find('[aria-label="Refresh"]');
      expect(btn.attributes("disabled")).toBeUndefined();
      wrapper.unmount();
    } finally {
      restore();
    }
  });

  it("keeps the Refresh button enabled while offline (user-initiated actions always proceed, per specs/reference/pwa-offline.md)", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const restore = mockOnLine(false);
    try {
      const review = useReviewStore(pinia);
      vi.spyOn(review, "loadNextPage").mockResolvedValue();
      const wrapper = mountView(pinia);
      await flushPromises();

      const btn = wrapper.find('[aria-label="Refresh"]');
      expect(btn.attributes("disabled")).toBeUndefined();
      wrapper.unmount();
    } finally {
      restore();
    }
  });

  it("disables the Refresh button while a load is already in flight", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const restore = mockOnLine(true);
    try {
      const review = useReviewStore(pinia);
      vi.spyOn(review, "loadNextPage").mockResolvedValue();
      const wrapper = mountView(pinia);
      await flushPromises();

      review.loading = true;
      await flushPromises();

      const btn = wrapper.find('[aria-label="Refresh"]');
      expect(btn.attributes("disabled")).toBeDefined();
      wrapper.unmount();
    } finally {
      restore();
    }
  });
});

describe("ReviewView", () => {
  it("renders the view container", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadNextPage").mockResolvedValue();
    const wrapper = mountView(pinia);
    await flushPromises();
    expect(wrapper.find('[data-testid="review-view"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("shows doubtful and certain rows after loading", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const catalog = useCatalogStore(pinia);
    catalog.replaceSnapshot(CATALOG);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.items = FEED_PAGE_1.items;
      review.doubtfulCount = FEED_PAGE_1.doubtful_count;
      review.hasMore = FEED_PAGE_1.has_more;
      review.page = 1;
    });
    const wrapper = mountView(pinia);
    await flushPromises();
    expect(wrapper.find('[data-testid="doubtful-row"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="certain-row"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("shows skeleton rows while loading", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(
      () => new Promise(() => {}),
    );
    review.loading = true;
    const wrapper = mountView(pinia);
    await flushPromises();
    expect(wrapper.find('[aria-label="Loading"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("calls loadNextPage when sentinel intersects with more items available", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useCatalogStore(pinia).replaceSnapshot(CATALOG);
    const review = useReviewStore(pinia);
    let nextPageCalls = 0;
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.items = FEED_PAGE_1.items;
      review.doubtfulCount = FEED_PAGE_1.doubtful_count;
      review.hasMore = true;
      review.page = 1;
    });
    vi.spyOn(review, "loadNextPage").mockImplementation(async () => {
      nextPageCalls += 1;
      review.hasMore = true;
      review.page += 1;
    });

    const wrapper = mountView(pinia);
    await flushPromises();

    review.loading = false;
    fireRulesObserver([{ isIntersecting: true }]);
    await flushPromises();
    expect(nextPageCalls).toBe(1);
    wrapper.unmount();
  });

  it("does not call loadNextPage when sentinel intersects but hasMore is false", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useCatalogStore(pinia).replaceSnapshot(CATALOG);
    const review = useReviewStore(pinia);
    let nextPageCalls = 0;
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.items = FEED_PAGE_1.items;
      review.hasMore = false;
      review.page = 1;
    });
    vi.spyOn(review, "loadNextPage").mockImplementation(async () => {
      nextPageCalls += 1;
    });
    const wrapper = mountView(pinia);
    await flushPromises();

    review.loading = false;
    fireRulesObserver([{ isIntersecting: true }]);
    await flushPromises();
    expect(nextPageCalls).toBe(0);
    wrapper.unmount();
  });

  it("shows confirm-all button after all items are loaded when doubtful items remain", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const catalog = useCatalogStore(pinia);
    catalog.replaceSnapshot(CATALOG);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.items = FEED_PAGE_1.items;
      review.doubtfulCount = FEED_PAGE_1.doubtful_count;
      review.hasMore = false;
      review.page = 1;
    });

    const wrapper = mountView(pinia);
    await flushPromises();
    expect(wrapper.find('[data-testid="confirm-all-btn"]').exists()).toBe(true);
    expect(wrapper.text()).not.toContain("end ·");
    wrapper.unmount();
  });

  it("does not show confirm-all button when hasMore is true", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useCatalogStore(pinia).replaceSnapshot(CATALOG);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.items = FEED_PAGE_1.items;
      review.doubtfulCount = FEED_PAGE_1.doubtful_count;
      review.hasMore = true;
      review.page = 1;
    });

    const wrapper = mountView(pinia);
    await flushPromises();
    expect(wrapper.find('[data-testid="confirm-all-btn"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("calls reviewStore.confirmAll when confirm-all button is clicked", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useCatalogStore(pinia).replaceSnapshot(CATALOG);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.items = [FEED_PAGE_1.items[0]];
      review.doubtfulCount = 1;
      review.hasMore = false;
      review.page = 1;
    });

    const confirmSpy = vi.spyOn(review, "confirmAll").mockResolvedValue();
    const wrapper = mountView(pinia);
    await flushPromises();
    await wrapper.find('[data-testid="confirm-all-btn"]').trigger("click");
    await flushPromises();
    expect(confirmSpy).toHaveBeenCalledWith([FEED_PAGE_1.items[0].id]);
    wrapper.unmount();
  });

  it("excludes correction expenses from confirm all", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useCatalogStore(pinia).replaceSnapshot(CATALOG);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.items = [
        FEED_PAGE_1.items[0],
        {
          id: 99,
          review_kind: "expense_correction",
          is_doubtful: true,
          confidence_level: 1,
          category_id: 10,
        },
      ];
      review.doubtfulCount = 2;
      review.hasMore = false;
      review.page = 1;
    });

    const confirmSpy = vi.spyOn(review, "confirmAll").mockResolvedValue();
    const wrapper = mountView(pinia);
    await flushPromises();
    await wrapper.find('[data-testid="confirm-all-btn"]').trigger("click");
    await flushPromises();

    expect(confirmSpy).toHaveBeenCalledWith([FEED_PAGE_1.items[0].id]);
    wrapper.unmount();
  });

  it("does not offer confirm all when only correction expenses remain", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useCatalogStore(pinia).replaceSnapshot(CATALOG);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.items = [
        {
          id: 99,
          review_kind: "expense_correction",
          is_doubtful: true,
          confidence_level: 1,
          category_id: 10,
        },
      ];
      review.doubtfulCount = 1;
      review.hasMore = false;
      review.page = 1;
    });

    const wrapper = mountView(pinia);
    await flushPromises();

    expect(wrapper.find('[data-testid="confirm-all-btn"]').exists()).toBe(false);
    wrapper.unmount();
  });
});

describe("ReviewView — on-mount calls", () => {
  it("only calls loadIfNeeded on mount (no separate expenses call)", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const review = useReviewStore(pinia);
    const spyLoad = vi.spyOn(review, "loadIfNeeded").mockResolvedValue();
    const wrapper = mountView(pinia);
    await flushPromises();
    expect(spyLoad).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it("does not render RECENT EXPENSES section", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockResolvedValue();
    const wrapper = mountView(pinia);
    await flushPromises();
    expect(wrapper.text()).not.toContain("RECENT EXPENSES");
    wrapper.unmount();
  });
});

describe("ReviewView — receipt queue chips", () => {
  it("shows queue section when any bucket > 0", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.receiptsQueue = { pending: 2, in_progress: 0, sleeping: 0, poisoned: 0 };
    });
    const wrapper = mountView(pinia);
    await flushPromises();
    expect(wrapper.find('[data-testid="queue-section"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("hides queue section when all buckets are zero", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.receiptsQueue = { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 };
    });
    const wrapper = mountView(pinia);
    await flushPromises();
    expect(wrapper.find('[data-testid="queue-section"]').exists()).toBe(false);
    wrapper.unmount();
  });
});

describe("ReviewView — stuck receipts section", () => {
  it("hides the section when there are no stuck receipts", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockResolvedValue();
    vi.spyOn(review, "loadExpensesIfNeeded").mockResolvedValue();
    const wrapper = mountView(pinia);
    await flushPromises();
    expect(wrapper.find('[data-testid="stuck-section"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("shows a row with the decoded amount and an enabled action", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.stuckReceipts = [
        {
          receipt_id: 1,
          status: "poisoned",
          retry_count: 4,
          last_error: "boom",
          created_at: "2026-06-01 10:00:00",
          store_name_raw: "Maxi",
          amount: 123.45,
          currency: "RSD",
          purchase_date: "2026-05-04T12:30:00+00:00",
        },
      ];
    });
    vi.spyOn(review, "loadExpensesIfNeeded").mockResolvedValue();
    const wrapper = mountView(pinia);
    await flushPromises();
    expect(wrapper.find('[data-testid="stuck-section"]').exists()).toBe(true);
    const row = wrapper.find('[data-testid="stuck-row"]');
    expect(row.text()).toContain("Maxi");
    expect(row.text()).toContain("123.45");
    expect(row.text()).toContain("failed");
    expect(row.text()).toContain("boom");
    const btn = row.find('[data-testid="stuck-resolve-btn"]');
    expect(btn.attributes("disabled")).toBeUndefined();
    wrapper.unmount();
  });

  it("shows store name placeholder and 'amount unknown' with a disabled action when decode failed", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.stuckReceipts = [
        {
          receipt_id: 2,
          status: "pending",
          retry_count: 0,
          last_error: null,
          created_at: "2026-06-01 10:00:00",
          store_name_raw: "",
          amount: null,
          currency: null,
          purchase_date: null,
        },
      ];
    });
    vi.spyOn(review, "loadExpensesIfNeeded").mockResolvedValue();
    const wrapper = mountView(pinia);
    await flushPromises();
    const row = wrapper.find('[data-testid="stuck-row"]');
    expect(row.text()).toContain("Store name: —");
    expect(row.text()).toContain("amount unknown");
    expect(row.find(".stuck-reason").exists()).toBe(false);
    const btn = row.find('[data-testid="stuck-resolve-btn"]');
    expect(btn.attributes("disabled")).toBeDefined();
    wrapper.unmount();
  });

  it("resolves a stuck receipt via the category picker", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.stuckReceipts = [
        {
          receipt_id: 1,
          status: "poisoned",
          retry_count: 4,
          last_error: "boom",
          created_at: "2026-06-01 10:00:00",
          store_name_raw: "Maxi",
          amount: 123.45,
          currency: "RSD",
          purchase_date: "2026-05-04T12:30:00+00:00",
        },
      ];
    });
    vi.spyOn(review, "loadExpensesIfNeeded").mockResolvedValue();
    const resolveSpy = vi.spyOn(review, "resolveStuckReceipt").mockResolvedValue();
    const wrapper = mountView(pinia);
    await flushPromises();

    await wrapper.find('[data-testid="stuck-resolve-btn"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-testid="category-sheet-stub"]').exists()).toBe(true);

    await wrapper.find('[data-testid="pick-category"]').trigger("click");
    await flushPromises();

    expect(resolveSpy).toHaveBeenCalledWith(1, { categoryId: 5 });
    wrapper.unmount();
  });
});

describe("ReviewView — ExpenseEditSheet opening", () => {
  it("opens ExpenseEditSheet when RuleRow emits tap", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useCatalogStore(pinia).replaceSnapshot(CATALOG);
    const review = useReviewStore(pinia);
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.items = [
        {
          id: 1,
          is_doubtful: true,
          name: "item",
          store: "Lidl",
          confidence_level: 3,
          category_id: 10,
          suggested_category_id: 10,
          alternative_categories: [],
          tags: [],
        },
      ];
      review.doubtfulCount = 1;
      review.hasMore = false;
    });
    const wrapper = mountView(pinia);
    await flushPromises();
    await wrapper.findComponent({ name: "RuleRow" }).vm.$emit("tap");
    await flushPromises();
    expect(wrapper.find('[data-testid="expense-edit-sheet"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("opens a correction as an expense rather than as a rule", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useCatalogStore(pinia).replaceSnapshot(CATALOG);
    const review = useReviewStore(pinia);
    const correction = {
      id: 99,
      expense_id: 99,
      review_kind: "expense_correction",
      is_doubtful: true,
      confidence_level: 1,
      category_id: 10,
      receipt_id: 7,
      item_name: null,
      tags: [],
      amount_original: 50,
      currency_original: "RSD",
    };
    vi.spyOn(review, "loadIfNeeded").mockImplementation(async () => {
      review.items = [correction];
      review.doubtfulCount = 1;
      review.hasMore = false;
    });
    const wrapper = mountView(pinia);
    await flushPromises();
    await wrapper.findComponent({ name: "RuleRow" }).vm.$emit("tap");
    await flushPromises();

    const sheet = wrapper.findComponent({ name: "ExpenseEditSheet" });
    expect(sheet.props("expense")).toEqual(correction);
    expect(sheet.props("ruleItem")).toBeNull();
    wrapper.unmount();
  });
});
