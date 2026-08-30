import { defineStore } from "pinia";
import { ref } from "vue";
import { getReviewFeed, getExpensesFeed, confirmAllRules, approveRule } from "../api/review.js";
import { correctCategory, editExpense } from "../api/expenseCorrections.js";
import { deleteExpense as apiDeleteExpense } from "../api/expenses.js";
import {
  deleteReceipt as apiDeleteReceipt,
  getReceiptQueue,
  resolveReceipt as apiResolveReceipt,
} from "../api/receipts.js";
import { useStaleCache } from "../composables/useStaleCache.js";
import { useToastStore } from "./toast.js";
import { useCatalogStore } from "./catalog.js";
import { useLlmStore } from "./llm.js";

const CACHE_KEY = "dinary:review:v1";
const DIRTY_KEY = "dinary:review:dirty";
const FETCHED_KEY = "dinary:review:fetchedAt";
const EXPENSES_CACHE_KEY = "dinary:review:expenses:v1";

export const useReviewStore = defineStore("review", () => {
  const { dirtyFlag, lastFetchedAt, markDirty, stampFresh, isStale, readCache, writeCache, clearCache } = useStaleCache({
    dirtyKey: DIRTY_KEY,
    fetchedKey: FETCHED_KEY,
    dataKey: CACHE_KEY,
  });
  const cached = (() => {
    const c = readCache();
    if (!Array.isArray(c?.items) || c.items.length === 0) return null;
    return c;
  })();
  const _emptyQueue = { pending: 0, in_progress: 0, sleeping: 0, poisoned: 0 };
  const items = ref(cached?.items ?? []);
  const doubtfulCount = ref(cached?.doubtfulCount ?? 0);
  const receiptsQueue = ref(cached?.receiptsQueue ?? { ..._emptyQueue });
  const stuckReceipts = ref([]);
  const stuckReceiptsLoading = ref(false);
  const hasMore = ref(cached?.hasMore ?? true);
  const page = ref(cached?.page ?? 0);
  const loading = ref(false);
  const openRowId = ref(null);

  const cachedExpenses = (() => {
    try {
      const raw = localStorage.getItem(EXPENSES_CACHE_KEY);
      if (!raw) return null;
      const c = JSON.parse(raw);
      return Array.isArray(c?.items) && c.items.length > 0 ? c : null;
    } catch { return null; }
  })();
  const expenses = ref(cachedExpenses?.items ?? []);
  const expensesPage = ref(cachedExpenses?.page ?? 0);
  const expensesHasMore = ref(cachedExpenses?.hasMore ?? true);
  const expensesLoading = ref(false);

  function _persistExpenses() {
    try {
      localStorage.setItem(EXPENSES_CACHE_KEY, JSON.stringify({
        items: expenses.value,
        page: expensesPage.value,
        hasMore: expensesHasMore.value,
      }));
    } catch {}
  }

  function _persistState() {
    writeCache({
      items: items.value,
      doubtfulCount: doubtfulCount.value,
      receiptsQueue: receiptsQueue.value,
      hasMore: hasMore.value,
      page: page.value,
    });
  }

  async function loadIfNeeded() {
    if (isStale()) {
      reset();
      await loadNextPage();
    }
  }

  async function loadNextPage() {
    if (loading.value) return;
    if (page.value > 0 && !hasMore.value) return;
    loading.value = true;
    try {
      const nextPage = page.value + 1;
      const data = await getReviewFeed({ page: nextPage, pageSize: 20 });
      const existingIds = new Set(
        items.value.map((i) => `${i.review_kind ?? "rule"}:${i.id}`),
      );
      const incoming = (data.items ?? []).filter(
        (i) => !existingIds.has(`${i.review_kind ?? "rule"}:${i.id}`),
      );
      items.value = [...items.value, ...incoming];
      doubtfulCount.value = data.doubtful_count ?? doubtfulCount.value;
      const q = data.receipts_queue ?? { ..._emptyQueue };
      receiptsQueue.value = q;
      hasMore.value = data.has_more ?? false;
      page.value = nextPage;
      stampFresh();
      _persistState();
      if (q.pending > 0 || q.in_progress > 0 || q.sleeping > 0 || q.poisoned > 0) {
        markDirty();
        useLlmStore().markDirty();
      }
      if (nextPage === 1) await loadStuckReceipts();
    } catch (err) {
      if (navigator.onLine) {
        const toast = useToastStore();
        toast.show(err?.message || "Failed to load review feed", "error");
      }
    } finally {
      loading.value = false;
    }
  }

  async function correct(item, categoryId, scope = "all") {
    const toast = useToastStore();
    const catalog = useCatalogStore();
    try {
      let result;
      const isExpenseCorrection = item.review_kind === "expense_correction";
      if (isExpenseCorrection) {
        result = await correctCategory(item.expense_id ?? item.id, categoryId, "single");
      } else if (item.is_doubtful) {
        result = await approveRule(item.id, categoryId);
      } else {
        const expenseId = item.expense_id ?? item.id;
        result = await correctCategory(expenseId, categoryId, scope);
      }
      const count = result?.updated_expenses_count ?? result?.count ?? item.count ?? 1;
      const cat = catalog.findCategoryById(categoryId);
      const catName = cat?.name ?? "";
      if (item.is_doubtful) {
        items.value = items.value.filter(
          (i) =>
            i.id !== item.id ||
            (i.review_kind ?? "rule") !== (item.review_kind ?? "rule"),
        );
        doubtfulCount.value = Math.max(0, doubtfulCount.value - 1);
      } else {
        const idx = items.value.findIndex((i) => i.id === item.id);
        if (idx !== -1) {
          items.value[idx] = {
            ...items.value[idx],
            category_id: categoryId,
            category_name: catName,
          };
        }
      }
      if (isExpenseCorrection) {
        const expenseId = item.expense_id ?? item.id;
        expenses.value = expenses.value.map((e) =>
          e.id === expenseId
            ? { ...e, category_id: categoryId, category_name: catName, confidence_level: 4 }
            : e,
        );
      } else {
        expenses.value = expenses.value.map((e) =>
          e.rule_id === item.id
            ? { ...e, category_id: categoryId, category_name: catName, confidence_level: null }
            : e,
        );
      }
      _persistExpenses();
      _persistState();
      const suffix = isExpenseCorrection ? "" : " · rule saved";
      toast.show(`Updated ${count} expenses → ${catName}${suffix}`, "success");
    } catch (err) {
      toast.show(err?.message || "Correction failed", "error");
    }
  }

  function setOpenRow(id) {
    openRowId.value = id;
  }

  async function confirmAll(ruleIds) {
    const toast = useToastStore();
    try {
      const result = await confirmAllRules(ruleIds);
      const confirmedCount = result?.confirmed ?? ruleIds.length;
      items.value = items.value.filter(
        (i) => i.review_kind === "expense_correction" || !ruleIds.includes(i.id),
      );
      doubtfulCount.value = Math.max(0, doubtfulCount.value - confirmedCount);
      _persistState();
      resetExpenses();
      await loadExpensesNextPage();
      toast.show(`${confirmedCount} rules confirmed`, "success");
    } catch (err) {
      toast.show(err?.message || "Confirm all failed", "error");
    }
  }

  async function loadExpensesNextPage() {
    if (expensesLoading.value) return;
    if (expensesPage.value > 0 && !expensesHasMore.value) return;
    expensesLoading.value = true;
    try {
      const nextPage = expensesPage.value + 1;
      const data = await getExpensesFeed({ page: nextPage, pageSize: 20 });
      const existingIds = new Set(expenses.value.map((e) => e.id));
      const incoming = (data.items ?? []).filter((e) => !existingIds.has(e.id));
      expenses.value = [...expenses.value, ...incoming];
      expensesHasMore.value = data.has_more ?? false;
      expensesPage.value = nextPage;
      _persistExpenses();
    } catch (err) {
      if (navigator.onLine) {
        const toast = useToastStore();
        toast.show(err?.message || "Failed to load expenses", "error");
      }
    } finally {
      expensesLoading.value = false;
    }
  }

  async function loadExpensesIfNeeded() {
    if (isStale()) {
      resetExpenses();
      await loadExpensesNextPage();
    } else if (expensesPage.value === 0) {
      await loadExpensesNextPage();
    }
  }

  function resetExpenses() {
    expenses.value = [];
    expensesPage.value = 0;
    expensesHasMore.value = true;
    expensesLoading.value = false;
    try { localStorage.removeItem(EXPENSES_CACHE_KEY); } catch {}
  }

  async function updateExpense(id, payload) {
    const toast = useToastStore();
    try {
      await editExpense(id, payload);
      const confirmsCorrection =
        payload.category_id != null &&
        items.value.some(
          (i) =>
            i.review_kind === "expense_correction" &&
            (i.expense_id ?? i.id) === id,
        );
      if (payload.update_rule || confirmsCorrection) {
        const removed = items.value.filter(
          (i) =>
            (i.expense_id ?? i.id) === id &&
            i.is_doubtful &&
            (payload.update_rule || i.review_kind === "expense_correction"),
        );
        if (removed.length > 0) {
          const removedKeys = new Set(
            removed.map((i) => `${i.review_kind ?? "rule"}:${i.id}`),
          );
          items.value = items.value.filter(
            (i) => !removedKeys.has(`${i.review_kind ?? "rule"}:${i.id}`),
          );
          doubtfulCount.value = Math.max(0, doubtfulCount.value - removed.length);
          _persistState();
        }
      }
      if (confirmsCorrection) {
        expenses.value = expenses.value.map((e) =>
          e.id === id ? { ...e, confidence_level: 4 } : e,
        );
        _persistExpenses();
      }
      if (payload.update_rule) {
        const target = expenses.value.find((e) => e.id === id);
        if (target?.rule_id != null) {
          const patch = { confidence_level: null };
          if (payload.scope && payload.scope !== "single" && payload.category_id != null) {
            const catalog = useCatalogStore();
            const cat = catalog.findCategoryById(payload.category_id);
            patch.category_id = payload.category_id;
            patch.category_name = cat?.name ?? "";
          }
          expenses.value = expenses.value.map((e) =>
            e.rule_id === target.rule_id ? { ...e, ...patch } : e,
          );
          _persistExpenses();
        }
      }
    } catch (err) {
      toast.show(err?.message || "Update failed", "error");
      throw err;
    }
  }

  function patchExpense(id, patch) {
    expenses.value = expenses.value.map((e) => (e.id === id ? { ...e, ...patch } : e));
    _persistExpenses();
  }

  async function deleteExpense(id) {
    await apiDeleteExpense(id);
    expenses.value = expenses.value.filter((e) => e.id !== id);
    _persistExpenses();
    markDirty();
  }

  async function loadStuckReceipts() {
    if (stuckReceiptsLoading.value) return;
    stuckReceiptsLoading.value = true;
    try {
      const data = await getReceiptQueue({ page: 1, pageSize: 20 });
      stuckReceipts.value = data.items ?? [];
    } catch (err) {
      if (navigator.onLine) {
        useToastStore().show(err?.message || "Failed to load stuck receipts", "error");
      }
    } finally {
      stuckReceiptsLoading.value = false;
    }
  }

  async function resolveStuckReceipt(receiptId, payload) {
    const toast = useToastStore();
    await apiResolveReceipt(receiptId, payload);
    stuckReceipts.value = stuckReceipts.value.filter((i) => i.receipt_id !== receiptId);
    reset();
    await Promise.all([loadNextPage(), loadExpensesNextPage(), loadStuckReceipts()]);
    toast.show("Expense created", "success");
  }

  async function deleteReceipt(receiptId) {
    await apiDeleteReceipt(receiptId);
    reset();
    await loadNextPage();
    await loadExpensesNextPage();
  }

  function reset() {
    items.value = [];
    doubtfulCount.value = 0;
    receiptsQueue.value = { ..._emptyQueue };
    hasMore.value = true;
    page.value = 0;
    loading.value = false;
    lastFetchedAt.value = null;
    resetExpenses();
    clearCache();
    try {
      localStorage.removeItem(FETCHED_KEY);
    } catch {}
  }

  return {
    items,
    doubtfulCount,
    receiptsQueue,
    stuckReceipts,
    stuckReceiptsLoading,
    hasMore,
    page,
    loading,
    dirtyFlag,
    lastFetchedAt,
    openRowId,
    expenses,
    expensesPage,
    expensesHasMore,
    expensesLoading,
    markDirty,
    loadIfNeeded,
    loadNextPage,
    correct,
    confirmAll,
    setOpenRow,
    loadExpensesIfNeeded,
    loadExpensesNextPage,
    resetExpenses,
    updateExpense,
    patchExpense,
    deleteExpense,
    deleteReceipt,
    loadStuckReceipts,
    resolveStuckReceipt,
    reset,
  };
});
