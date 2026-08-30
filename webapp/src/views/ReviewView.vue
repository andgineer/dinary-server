<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useReviewStore } from "../stores/review.js";
import { useOnline } from "../composables/useOnline.js";
import { useToastStore } from "../stores/toast.js";
import RuleRow from "../components/RuleRow.vue";
import ExpenseRow from "../components/ExpenseRow.vue";
import ExpenseEditSheet from "../components/ExpenseEditSheet.vue";
import CategorySheet from "../components/CategorySheet.vue";
import IconBtn from "../components/IconBtn.vue";

const reviewStore = useReviewStore();
const { isOnline } = useOnline();
const toast = useToastStore();

const doubtfulItems = computed(() => reviewStore.items.filter((i) => i.is_doubtful));
const doubtfulRules = computed(() =>
  doubtfulItems.value.filter((i) => i.review_kind !== "expense_correction"),
);

const groupedExpenses = computed(() => {
  const result = [];
  const list = reviewStore.expenses;
  let i = 0;
  while (i < list.length) {
    const e = list[i];
    if (!e.receipt_id) {
      result.push({ type: "single", expense: e });
      i++;
      continue;
    }
    const group = [e];
    while (i + 1 < list.length && list[i + 1].receipt_id === e.receipt_id) {
      i++;
      group.push(list[i]);
    }
    if (group.length === 1) {
      result.push({ type: "single", expense: group[0] });
    } else {
      const store = e.store_name ?? e.store ?? e.merchant ?? null;
      const total = group.reduce((s, x) => s + (Number(x.amount_original) || 0), 0);
      const currency = e.currency_original ?? "";
      const date = e.date ?? e.datetime ?? null;
      result.push({ type: "group", receipt_id: e.receipt_id, store, total, currency, date, expenses: group });
    }
    i++;
  }
  return result;
});

const expenseEditOpen = ref(false);
const editingExpense = ref(null);
const editingSuggestions = ref([]);
const editingRuleItem = ref(null);

const rulesSentinel = ref(null);
const expensesSentinel = ref(null);
let rulesObserver = null;
let expensesObserver = null;

function openExpenseEdit(expense, suggestions = [], ruleItem = null) {
  if (!isOnline.value) { toast.show("Not available offline", "info"); return; }
  editingExpense.value = expense;
  editingSuggestions.value = suggestions;
  editingRuleItem.value = ruleItem;
  expenseEditOpen.value = true;
}

function openReviewItem(item) {
  if (item.review_kind === "expense_correction") {
    openExpenseEdit(item);
    return;
  }
  openExpenseEdit(null, item.alternative_categories ?? [], item);
}

function closeExpenseEdit() {
  expenseEditOpen.value = false;
  editingExpense.value = null;
  editingSuggestions.value = [];
  editingRuleItem.value = null;
}

async function approveItem({ item, categoryId }) {
  if (!navigator.onLine) { toast.show("Not available offline", "info"); return; }
  await reviewStore.correct(item, categoryId, "all");
  window.dispatchEvent(new Event("online"));
}

async function handleConfirmAll() {
  if (!navigator.onLine) { toast.show("Not available offline", "info"); return; }
  const ruleIds = doubtfulRules.value.map((i) => i.id);
  await reviewStore.confirmAll(ruleIds);
  window.dispatchEvent(new Event("online"));
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.getDate()} ${d.toLocaleString("en", { month: "short" })}`;
}

const STUCK_STATUS_LABELS = { pending: "queued", in_progress: "processing", poisoned: "failed" };
const STUCK_STATUS_CHIP_CLASSES = {
  pending: "queue-chip--ready",
  in_progress: "queue-chip--processing",
  poisoned: "queue-chip--failed",
};

function stuckStatusLabel(status) {
  return STUCK_STATUS_LABELS[status] ?? status;
}

function stuckStatusChipClass(status) {
  return STUCK_STATUS_CHIP_CLASSES[status] ?? "";
}

function formatAge(iso) {
  if (!iso) return "";
  const then = new Date(iso.includes("T") ? iso : `${iso.replace(" ", "T")}Z`);
  const diffMin = Math.floor((Date.now() - then.getTime()) / 60_000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHours = Math.floor(diffMin / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  return `${Math.floor(diffHours / 24)}d ago`;
}

const stuckResolveItem = ref(null);
const stuckCategorySheetOpen = ref(false);

function openStuckResolve(item) {
  if (!isOnline.value) { toast.show("Not available offline", "info"); return; }
  stuckResolveItem.value = item;
  stuckCategorySheetOpen.value = true;
}

async function onStuckCategorySelect(categoryId) {
  const item = stuckResolveItem.value;
  if (!item) return;
  try {
    await reviewStore.resolveStuckReceipt(item.receipt_id, { categoryId });
  } catch (err) {
    toast.show(err?.message || "Resolve failed", "error");
  }
}

async function forceRefresh() {
  if (!navigator.onLine) { toast.show("Not available offline", "info"); return; }
  reviewStore.reset();
  await Promise.all([reviewStore.loadNextPage(), reviewStore.loadExpensesNextPage()]);
  window.dispatchEvent(new Event("online"));
}

function setupObservers() {
  if (typeof IntersectionObserver === "undefined") return;

  if (rulesSentinel.value) {
    rulesObserver = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !reviewStore.loading && reviewStore.hasMore && isOnline.value) {
          reviewStore.loadNextPage();
        }
      },
      { rootMargin: "120px" },
    );
    rulesObserver.observe(rulesSentinel.value);
  }

  if (expensesSentinel.value) {
    expensesObserver = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !reviewStore.expensesLoading && reviewStore.expensesHasMore && isOnline.value) {
          reviewStore.loadExpensesNextPage();
        }
      },
      { rootMargin: "120px" },
    );
    expensesObserver.observe(expensesSentinel.value);
  }
}

onMounted(async () => {
  if (isOnline.value) {
    await Promise.all([reviewStore.loadIfNeeded(), reviewStore.loadExpensesIfNeeded()]);
  }
  setupObservers();
});

onBeforeUnmount(() => {
  if (rulesObserver) rulesObserver.disconnect();
  if (expensesObserver) expensesObserver.disconnect();
});
</script>

<template>
  <div class="review-view" data-testid="review-view">
    <div
      v-if="reviewStore.receiptsQueue.pending > 0 || reviewStore.receiptsQueue.in_progress > 0 || reviewStore.receiptsQueue.sleeping > 0 || reviewStore.receiptsQueue.poisoned > 0"
      class="queue-section"
      data-testid="queue-section"
    >
      <div class="queue-header">
        <span class="queue-label">RECEIPT QUEUE</span>
      </div>
      <div class="queue-chips">
        <span v-if="reviewStore.receiptsQueue.pending > 0" class="queue-chip queue-chip--ready">{{ reviewStore.receiptsQueue.pending }} queued</span>
        <span v-if="reviewStore.receiptsQueue.in_progress > 0" class="queue-chip queue-chip--processing">{{ reviewStore.receiptsQueue.in_progress }} processing</span>
        <span v-if="reviewStore.receiptsQueue.sleeping > 0" class="queue-chip queue-chip--sleeping">{{ reviewStore.receiptsQueue.sleeping }} sleeping</span>
        <span v-if="reviewStore.receiptsQueue.poisoned > 0" class="queue-chip queue-chip--failed">{{ reviewStore.receiptsQueue.poisoned }} failed</span>
      </div>
    </div>

    <div
      v-if="reviewStore.stuckReceipts.length > 0"
      class="stuck-section"
      data-testid="stuck-section"
    >
      <div class="stuck-header">
        <span class="stuck-label">STUCK RECEIPTS</span>
      </div>
      <div
        v-for="item in reviewStore.stuckReceipts"
        :key="item.receipt_id"
        class="stuck-row"
        data-testid="stuck-row"
      >
        <div class="stuck-row-main">
          <span
            class="stuck-store"
            :class="{ 'stuck-store--placeholder': !item.store_name_raw }"
          >{{ item.store_name_raw || "Store name: —" }}</span>
          <span class="stuck-amount">
            <template v-if="item.amount != null">
              {{ Number(item.amount).toLocaleString(undefined, { maximumFractionDigits: 2 }) }}
              <span class="stuck-currency">{{ item.currency }}</span>
            </template>
            <template v-else>amount unknown</template>
          </span>
        </div>
        <div class="stuck-row-meta">
          <span class="queue-chip" :class="stuckStatusChipClass(item.status)">{{ stuckStatusLabel(item.status) }}</span>
          <span v-if="item.retry_count > 0" class="stuck-retries">{{ item.retry_count }} retries</span>
          <span class="stuck-age">{{ formatAge(item.created_at) }}</span>
          <button
            type="button"
            class="stuck-resolve-btn"
            data-testid="stuck-resolve-btn"
            :disabled="item.amount == null"
            @click="openStuckResolve(item)"
          >
            Save as expense
          </button>
        </div>
        <div v-if="item.last_error" class="stuck-reason">{{ item.last_error }}</div>
      </div>
    </div>

    <div class="review-header">
      <div
        v-if="reviewStore.doubtfulCount > 0"
        class="section-header section-header--warning"
      >
        <span class="section-label">NEEDS REVIEW</span>
        <span class="section-badge">{{ reviewStore.doubtfulCount }}</span>
        <span class="section-sort">by impact</span>
      </div>
      <IconBtn
        icon="refresh"
        tone="muted"
        label="Refresh"
        :disabled="reviewStore.loading"
        @click="forceRefresh()"
      />
    </div>

    <template v-for="item in reviewStore.items" :key="`${item.review_kind ?? 'rule'}:${item.id}`">
      <RuleRow
        :item="item"
        @tap="openReviewItem(item)"
        @approve="approveItem($event)"
      />
    </template>

<div v-if="reviewStore.loading" class="skeleton-rows" aria-label="Loading">
      <div class="skeleton-row" />
      <div class="skeleton-row" />
    </div>

    <div ref="rulesSentinel" class="scroll-sentinel" aria-hidden="true" />

    <div
      v-if="!reviewStore.hasMore && !reviewStore.loading && doubtfulRules.length > 0"
      class="confirm-all-wrap"
    >
      <button
        type="button"
        class="confirm-all-btn"
        data-testid="confirm-all-btn"
        @click="handleConfirmAll"
      >
        Confirm all ({{ doubtfulRules.length }})
      </button>
    </div>

    <!-- Expenses section -->
    <div class="section-header expenses-header">
      <span class="section-label">EXPENSES</span>
    </div>

    <template v-for="item in groupedExpenses" :key="item.type === 'single' ? item.expense.id : item.receipt_id">
      <ExpenseRow
        v-if="item.type === 'single'"
        :expense="item.expense"
        @tap="openExpenseEdit(item.expense)"
      />
      <div v-else class="receipt-group">
        <div class="receipt-group-header">
          <span v-if="item.date" class="receipt-group-date">{{ formatDate(item.date) }}</span>
          <span class="receipt-group-store">{{ item.store }}</span>
          <span class="receipt-group-total">
            {{ item.total.toLocaleString(undefined, { maximumFractionDigits: 2 }) }}
            <span class="receipt-group-currency">{{ item.currency }}</span>
          </span>
        </div>
        <ExpenseRow
          v-for="expense in item.expenses"
          :key="expense.id"
          :expense="expense"
          :hide-store="true"
          :hide-date="true"
          class="receipt-group-row"
          @tap="openExpenseEdit(expense)"
        />
      </div>
    </template>

    <div v-if="reviewStore.expensesLoading" class="skeleton-rows" aria-label="Loading expenses">
      <div class="skeleton-row" />
    </div>

    <div
      v-if="!reviewStore.expensesLoading && !reviewStore.expensesHasMore && reviewStore.expenses.length === 0"
      class="empty-state"
    >
      <p class="empty-text">No expenses yet</p>
    </div>

    <div ref="expensesSentinel" class="scroll-sentinel" aria-hidden="true" />
  </div>

  <ExpenseEditSheet
    :open="expenseEditOpen"
    :expense="editingExpense"
    :suggestions="editingSuggestions"
    :rule-item="editingRuleItem"
    @close="closeExpenseEdit"
  />

  <CategorySheet
    :open="stuckCategorySheetOpen"
    title="Select category"
    @select="onStuckCategorySelect"
    @close="stuckCategorySheetOpen = false"
  />
</template>

<style scoped>
.review-view {
  padding: 1rem 1.25rem;
  padding-bottom: 2rem;
  max-width: 480px;
  width: 100%;
  margin: 0 auto;
}

.review-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 0.25rem;
  margin-bottom: 0.5rem;
}

.expenses-header {
  margin-top: 1.5rem;
  margin-bottom: 0.5rem;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0 0.25rem;
  flex: 1;
}

.section-label {
  font-size: 0.6875rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
}

.section-header--warning .section-label {
  color: var(--warning);
}

.section-badge {
  background: var(--warning);
  color: #000;
  font-size: 0.65rem;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 999px;
  min-width: 18px;
  text-align: center;
}

.section-sort {
  margin-left: auto;
  font-size: 0.7rem;
  color: var(--muted);
}

.empty-state {
  padding: 3rem 1rem;
  text-align: center;
}

.empty-text {
  color: var(--muted);
  font-size: 0.9rem;
}

.skeleton-rows {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.skeleton-row {
  height: 72px;
  background: var(--field);
  border-radius: 10px;
  border: 1px solid var(--border);
  animation: skeleton-pulse 1.4s ease-in-out infinite;
}

@keyframes skeleton-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.scroll-sentinel {
  height: 1px;
}

.queue-section {
  margin-bottom: 0.5rem;
}

.queue-header {
  display: flex;
  align-items: center;
  padding: 0.5rem 0.25rem 0.4rem;
}

.queue-label {
  font-size: 0.65rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: var(--muted);
}

.queue-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  padding: 0 0.25rem 0.75rem;
}

.queue-chip {
  font-size: 0.72rem;
  font-weight: 600;
  padding: 0.2rem 0.55rem;
  border-radius: 999px;
  border: 1px solid currentColor;
}

.queue-chip--ready { color: var(--accent); }
.queue-chip--processing { color: var(--text); }
.queue-chip--sleeping { color: var(--muted); }
.queue-chip--failed { color: var(--error); }

.stuck-section {
  margin-bottom: 0.75rem;
}

.stuck-header {
  display: flex;
  align-items: center;
  padding: 0.5rem 0.25rem 0.4rem;
}

.stuck-label {
  font-size: 0.65rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: var(--warning);
}

.stuck-row {
  padding: 0.6rem 0.75rem;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--field);
  margin-bottom: 0.5rem;
}

.stuck-row-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.35rem;
}

.stuck-store {
  font-size: 0.85rem;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stuck-store--placeholder {
  font-weight: 400;
  font-style: italic;
  color: var(--muted);
}

.stuck-amount {
  font-family: var(--font-num);
  font-size: 0.85rem;
  flex-shrink: 0;
}

.stuck-currency {
  font-size: 0.7rem;
  color: var(--muted-2);
  margin-left: 2px;
}

.stuck-row-meta {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.stuck-retries,
.stuck-age {
  font-size: 0.7rem;
  color: var(--muted);
}

.stuck-reason {
  margin-top: 0.35rem;
  font-size: 0.72rem;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.stuck-resolve-btn {
  margin-left: auto;
  padding: 0.3rem 0.75rem;
  background: rgba(96, 165, 250, 0.15);
  border: 1px solid rgba(96, 165, 250, 0.3);
  border-radius: 999px;
  color: #60a5fa;
  font-size: 0.75rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.12s;
}

.stuck-resolve-btn:hover:not(:disabled) {
  background: rgba(96, 165, 250, 0.25);
}

.stuck-resolve-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.confirm-all-wrap {
  padding: 0.75rem 0;
  display: flex;
  justify-content: center;
}

.confirm-all-btn {
  padding: 0.5rem 1.5rem;
  background: rgba(96, 165, 250, 0.15);
  border: 1px solid rgba(96, 165, 250, 0.3);
  border-radius: 999px;
  color: #60a5fa;
  font-size: 0.85rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.12s;
}

.confirm-all-btn:hover {
  background: rgba(96, 165, 250, 0.25);
}

/* ── Receipt groups ── */
.receipt-group {
  margin-bottom: 0.5rem;
  border-radius: 10px;
  border: 1px solid rgba(96, 165, 250, 0.3);
  overflow: hidden;
}

.receipt-group-header {
  display: flex;
  align-items: center;
  padding: 0.35rem 0.75rem;
  background: rgba(96, 165, 250, 0.1);
  border-bottom: 1px solid rgba(96, 165, 250, 0.2);
}

.receipt-group-date {
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--muted);
  white-space: nowrap;
  flex-shrink: 0;
  margin-right: 0.4rem;
}

.receipt-group-store {
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.receipt-group-total {
  font-family: var(--font-num);
  font-size: 0.72rem;
  color: var(--muted);
  flex-shrink: 0;
  margin-left: 0.5rem;
}

.receipt-group-currency {
  font-size: 0.65rem;
  color: var(--muted-2);
  margin-left: 2px;
}

.receipt-group :deep(.row-wrap) {
  border: none;
  border-radius: 0;
  margin-bottom: 0;
  border-top: 1px solid var(--border);
}

.receipt-group :deep(.row-wrap:first-child) {
  border-top: none;
}

.receipt-group :deep(.row-wrap--warning) {
  border-left: 4px solid var(--warning);
}
</style>
