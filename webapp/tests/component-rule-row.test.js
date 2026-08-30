import { describe, it, expect, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import RuleRow from "../src/components/RuleRow.vue";
import { useCatalogStore } from "../src/stores/catalog.js";

beforeEach(async () => {
  await allure.epic("Review & Rules");
  await allure.feature("Frontend");
  await allure.story("RuleRow");
});

const CATALOG = {
  catalog_version: 1,
  category_groups: [
    { id: 1, name: "Food", is_active: true },
    { id: 2, name: "Transport", is_active: true },
  ],
  categories: [
    { id: 10, group_id: 1, name: "groceries", is_active: true },
    { id: 11, group_id: 1, name: "cafe", is_active: true },
    { id: 20, group_id: 2, name: "taxi", is_active: true },
  ],
  events: [],
  tags: [],
};

function makeDoubtful(overrides = {}) {
  return {
    id: 1,
    is_doubtful: true,
    name: "Chocolate bar",
    store: "Lidl",
    confidence_level: 3,
    category_id: 10,
    suggested_category_id: 11,
    alternative_categories: [],
    tags: [],
    ...overrides,
  };
}

function makeCertain(overrides = {}) {
  return {
    id: 2,
    is_doubtful: false,
    name: "mleko",
    store: "Maxi",
    category_id: 10,
    category_name: "groceries",
    alternative_categories: [],
    tags: [],
    ...overrides,
  };
}

beforeEach(() => {
  const pinia = createPinia();
  setActivePinia(pinia);
  useCatalogStore(pinia).replaceSnapshot(CATALOG);
});

describe("RuleRow — tag chips", () => {
  it("renders tag chips from item.tags", () => {
    const tags = [
      { id: 1, name: "собака", icon: "🐾" },
      { id: 2, name: "зож" },
    ];
    const w = mount(RuleRow, { props: { item: makeDoubtful({ tags }) } });
    expect(w.findAll(".tag-chip").length).toBe(2);
    expect(w.text()).toContain("собака");
    expect(w.text()).toContain("зож");
  });

  it("renders no tag chips when tags is empty", () => {
    const w = mount(RuleRow, { props: { item: makeDoubtful({ tags: [] }) } });
    expect(w.findAll(".tag-chip").length).toBe(0);
  });
});

describe("RuleRow — alternative chips", () => {
  it("renders up to 2 alternative chips from item.alternative_categories", () => {
    const alts = [
      { id: 20, name: "taxi" },
      { id: 11, name: "cafe" },
      { id: 99, name: "extra" }, // should be cut off
    ];
    const w = mount(RuleRow, { props: { item: makeDoubtful({ alternative_categories: alts }) } });
    expect(w.findAll(".alt-chip").length).toBe(2);
    expect(w.find('[data-testid="alt-chip-20"]').exists()).toBe(true);
    expect(w.find('[data-testid="alt-chip-11"]').exists()).toBe(true);
    expect(w.find('[data-testid="alt-chip-99"]').exists()).toBe(false);
  });

  it("each alt chip emits approve with correct categoryId", async () => {
    const alts = [
      { id: 20, name: "taxi" },
      { id: 11, name: "cafe" },
    ];
    const item = makeDoubtful({ alternative_categories: alts });
    const w = mount(RuleRow, { props: { item } });
    await w.find('[data-testid="alt-chip-20"]').trigger("click");
    expect(w.emitted("approve")?.[0]).toEqual([{ item, categoryId: 20 }]);
  });
});

describe("RuleRow — ✎ emits tap", () => {
  it("edit button emits tap", async () => {
    const w = mount(RuleRow, { props: { item: makeDoubtful() } });
    await w.find('[data-testid="edit-btn"]').trigger("click");
    expect(w.emitted("tap")).toBeTruthy();
  });
});

describe("RuleRow — approve chip emits approve", () => {
  it("approve chip emits approve with suggestedCategoryId", async () => {
    const item = makeDoubtful({ category_id: 10, suggested_category_id: 11 });
    const w = mount(RuleRow, { props: { item } });
    await w.find(`[data-testid="approve-chip-11"]`).trigger("click");
    expect(w.emitted("approve")?.[0]).toEqual([{ item, categoryId: 11 }]);
  });
});

describe("RuleRow — certain row", () => {
  it("has no approve chip", () => {
    const w = mount(RuleRow, { props: { item: makeCertain() } });
    expect(w.find(".approve-chip").exists()).toBe(false);
  });

  it("has no alternative chips", () => {
    const w = mount(RuleRow, { props: { item: makeCertain() } });
    expect(w.find(".alt-chip").exists()).toBe(false);
  });

  it("shows category breadcrumb with group and name", () => {
    const w = mount(RuleRow, { props: { item: makeCertain() } });
    expect(w.text()).toContain("Food");
    expect(w.text()).toContain("groceries");
  });

  it("uses testid certain-row", () => {
    const w = mount(RuleRow, { props: { item: makeCertain() } });
    expect(w.find('[data-testid="certain-row"]').exists()).toBe(true);
  });
});

describe("RuleRow — doubtful row", () => {
  it("uses testid doubtful-row", () => {
    const w = mount(RuleRow, { props: { item: makeDoubtful() } });
    expect(w.find('[data-testid="doubtful-row"]').exists()).toBe(true);
  });

  it("does not show a confidence text pill (colour border is enough)", () => {
    const w = mount(RuleRow, { props: { item: makeDoubtful({ confidence_level: 3 }) } });
    expect(w.find(".confidence-pill").exists()).toBe(false);
  });

  it("has warning class on row-wrap", () => {
    const w = mount(RuleRow, { props: { item: makeDoubtful() } });
    expect(w.find(".row-wrap--warning").exists()).toBe(true);
  });

  it("shows item name and store", () => {
    const w = mount(RuleRow, { props: { item: makeDoubtful() } });
    expect(w.text()).toContain("Chocolate bar");
    expect(w.text()).toContain("Lidl");
  });
});

describe("RuleRow — journal correction totals", () => {
  it("shows both the correction amount and the full receipt total", () => {
    const w = mount(RuleRow, {
      props: {
        item: makeDoubtful({
          review_kind: "expense_correction",
          total: 150,
          receipt_total: 1250,
          currency: "RSD",
        }),
      },
    });

    expect(w.get('[data-testid="correction-amount"]').text()).toContain("150 RSD");
    expect(w.get('[data-testid="receipt-total"]').text()).toContain("1,250 RSD");
  });

  it("does not add correction totals to a normal doubtful rule", () => {
    const w = mount(RuleRow, {
      props: { item: makeDoubtful({ total: 150, receipt_total: 1250, currency: "RSD" }) },
    });

    expect(w.find('[data-testid="correction-amount"]').exists()).toBe(false);
    expect(w.find('[data-testid="receipt-total"]').exists()).toBe(false);
  });
});

describe("RuleRow — confidence-coloured borders", () => {
  it("confidence 1 gets row-wrap--c1 class", () => {
    const w = mount(RuleRow, { props: { item: makeDoubtful({ confidence_level: 1 }) } });
    expect(w.find(".row-wrap--c1").exists()).toBe(true);
    expect(w.find(".row-wrap--c2").exists()).toBe(false);
  });

  it("confidence 2 gets row-wrap--c2 class", () => {
    const w = mount(RuleRow, { props: { item: makeDoubtful({ confidence_level: 2 }) } });
    expect(w.find(".row-wrap--c2").exists()).toBe(true);
    expect(w.find(".row-wrap--c1").exists()).toBe(false);
  });

  it("confidence 3 gets row-wrap--c3 class", () => {
    const w = mount(RuleRow, { props: { item: makeDoubtful({ confidence_level: 3 }) } });
    expect(w.find(".row-wrap--c3").exists()).toBe(true);
    expect(w.find(".row-wrap--c1").exists()).toBe(false);
  });

  it("certain rows do not get a confidence class", () => {
    const w = mount(RuleRow, { props: { item: makeCertain() } });
    expect(w.find(".row-wrap--c1").exists()).toBe(false);
    expect(w.find(".row-wrap--c2").exists()).toBe(false);
    expect(w.find(".row-wrap--c3").exists()).toBe(false);
  });
});

describe("RuleRow — frequent-category quick picks", () => {
  function mountWithFrequent(item, freqCats) {
    const pinia = createPinia();
    setActivePinia(pinia);
    useCatalogStore(pinia).replaceSnapshot({ ...CATALOG, frequent_categories: freqCats });
    return mount(RuleRow, { global: { plugins: [pinia] }, props: { item } });
  }

  it("renders freq-chip pills for frequent categories", () => {
    const item = makeDoubtful({ category_id: 10, suggested_category_id: 10, alternative_categories: [] });
    const w = mountWithFrequent(item, [
      { id: 20, name: "taxi" },
      { id: 11, name: "cafe" },
    ]);
    expect(w.find('[data-testid="freq-chip-20"]').exists()).toBe(true);
    expect(w.find('[data-testid="freq-chip-11"]').exists()).toBe(true);
  });

  it("deduplicates: does not show freq pick if same id as approve chip", () => {
    const item = makeDoubtful({ category_id: 10, suggested_category_id: 11, alternative_categories: [] });
    const w = mountWithFrequent(item, [{ id: 11, name: "cafe" }]);
    expect(w.find('[data-testid="freq-chip-11"]').exists()).toBe(false);
  });

  it("deduplicates: does not show freq pick if same id as alt chip", () => {
    const item = makeDoubtful({
      category_id: 10,
      suggested_category_id: 10,
      alternative_categories: [{ id: 20, name: "taxi" }],
    });
    const w = mountWithFrequent(item, [{ id: 20, name: "taxi" }]);
    expect(w.find('[data-testid="freq-chip-20"]').exists()).toBe(false);
  });

  it("freq-chip click emits approve with that categoryId", async () => {
    const item = makeDoubtful({ category_id: 10, suggested_category_id: 10, alternative_categories: [] });
    const w = mountWithFrequent(item, [{ id: 20, name: "taxi" }]);
    await w.find('[data-testid="freq-chip-20"]').trigger("click");
    expect(w.emitted("approve")?.[0]).toEqual([{ item, categoryId: 20 }]);
  });
});
