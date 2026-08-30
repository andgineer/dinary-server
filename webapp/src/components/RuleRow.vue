<script setup>
import { computed, watch } from "vue";
import { Pencil, Check, Sparkles } from "lucide-vue-next";
import { useCatalogStore } from "../stores/catalog.js";
import { useReviewStore } from "../stores/review.js";
import { useSwipeRow } from "../composables/useSwipeRow.js";

const PANEL_DOUBTFUL = 168;
const PANEL_CERTAIN = 92;

const props = defineProps({
  item: { type: Object, required: true },
});
const emit = defineEmits(["tap", "approve"]);

const catalog = useCatalogStore();
const reviewStore = useReviewStore();

const isDoubtful = computed(() => props.item.is_doubtful);
const isCorrection = computed(() => props.item.review_kind === "expense_correction");
const panelWidth = computed(() => (isDoubtful.value ? PANEL_DOUBTFUL : PANEL_CERTAIN));

const { sliderEl, isOpen, isCommit, onPointerDown, onPointerMove, endDrag, shouldFireTap, close } =
  useSwipeRow({
    panelWidth: panelWidth.value,
    onPrimary: () => {
      if (isDoubtful.value) {
        emit("approve", { item: props.item, categoryId: suggestedCategoryId.value });
      } else {
        emit("tap");
      }
    },
  });

watch(isOpen, (val) => {
  if (val) reviewStore.setOpenRow(props.item.id);
});

watch(
  () => reviewStore.openRowId,
  (id) => {
    if (isOpen.value && id !== props.item.id) close();
  },
);

const categoryId = computed(() => props.item.category_id);
const suggestedCategoryId = computed(() => props.item.suggested_category_id ?? props.item.category_id);

const currentCategory = computed(() => catalog.findCategoryById(categoryId.value));
const currentGroup = computed(() => {
  const cat = currentCategory.value;
  if (!cat) return null;
  return catalog.snapshot?.category_groups?.find((g) => g.id === cat.group_id) ?? null;
});

const suggestedCategory = computed(() => catalog.findCategoryById(suggestedCategoryId.value));

const llmDiffers = computed(
  () =>
    suggestedCategoryId.value &&
    Number(suggestedCategoryId.value) !== Number(categoryId.value),
);

const altCategories = computed(() => (props.item.alternative_categories ?? []).slice(0, 2));

const tags = computed(() => props.item.tags ?? []);

const usedCategoryIds = computed(() => {
  const ids = new Set();
  if (suggestedCategoryId.value) ids.add(Number(suggestedCategoryId.value));
  for (const alt of altCategories.value) ids.add(Number(alt.id));
  return ids;
});

const frequentPicks = computed(() =>
  catalog.frequentCategories.filter((c) => !usedCategoryIds.value.has(Number(c.id))),
);

function formatMoney(amount, currency) {
  const value = Number(amount);
  if (!Number.isFinite(value)) return "—";
  const formatted = value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return `${formatted} ${currency ?? ""}`.trim();
}

function approveFromButton(e, catId) {
  e.stopPropagation();
  close();
  emit("approve", { item: props.item, categoryId: catId });
}

function onEditClick(e) {
  e.stopPropagation();
  close();
  emit("tap");
}

function onRowClick() {
  if (shouldFireTap()) emit("tap");
}
</script>

<template>
  <div
    class="row-wrap"
    :class="[
      isDoubtful ? 'row-wrap--warning' : '',
      isDoubtful ? `row-wrap--c${[1,2,3].includes(item.confidence_level) ? item.confidence_level : 2}` : '',
    ]"
    :data-testid="isDoubtful ? 'doubtful-row' : 'certain-row'"
    :data-rule-id="item.id"
  >
    <!-- Swipe panel -->
    <div
      class="row-panel"
      :class="{ 'panel--doubtful': isDoubtful }"
      :style="{ pointerEvents: isOpen ? 'auto' : 'none' }"
    >
      <template v-if="isDoubtful">
        <button
          type="button"
          class="panel-btn"
          :class="{ 'panel-btn--shrink': isCommit }"
          aria-label="Edit"
          @click.stop="onEditClick($event)"
        >
          <Pencil :size="13" aria-hidden="true" />
          Edit
        </button>
        <button
          type="button"
          class="panel-btn panel-btn--approve"
          :class="{ 'panel-btn--commit': isCommit }"
          aria-label="Approve"
          @click.stop="approveFromButton($event, suggestedCategoryId)"
        >
          <Check :size="13" aria-hidden="true" />
          Approve
        </button>
      </template>
      <template v-else>
        <button
          type="button"
          class="panel-btn panel-btn--commit-always"
          :class="{ 'panel-btn--commit': isCommit }"
          aria-label="Edit"
          @click.stop="onEditClick($event)"
        >
          <Pencil :size="13" aria-hidden="true" />
          Edit
        </button>
      </template>
    </div>

    <!-- Slider (visible content) -->
    <div
      ref="sliderEl"
      class="row-slider"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="endDrag"
      @pointercancel="endDrag"
      @click="onRowClick"
    >
      <!-- Top row: name · store -->
      <div class="row-top">
        <span class="row-name">{{ item.name ?? item.store }}</span>
        <span v-if="item.name && item.store" class="row-store">{{ item.store }}</span>
      </div>

      <!-- Bottom row -->
      <div class="row-bottom">
        <template v-if="isDoubtful">
          <div v-if="isCorrection" class="correction-totals">
            <span class="correction-amount" data-testid="correction-amount">
              {{ formatMoney(item.total, item.currency) }} correction
            </span>
            <span class="receipt-total" data-testid="receipt-total">
              {{ formatMoney(item.receipt_total, item.currency) }} receipt
            </span>
          </div>

          <!-- Tag chips -->
          <span v-for="tag in tags" :key="tag.id ?? tag" class="tag-chip">
            <template v-if="tag.icon">{{ tag.icon }} </template>{{ tag.name ?? tag }}
          </span>

          <!-- Approve chip (LLM suggestion) -->
          <button
            v-if="suggestedCategory"
            type="button"
            class="approve-chip"
            :data-testid="`approve-chip-${suggestedCategoryId}`"
            @click.stop="approveFromButton($event, suggestedCategoryId)"
          >
            <Sparkles v-if="llmDiffers" :size="9" class="sparkle-icon" aria-hidden="true" />
            <Check :size="10" aria-hidden="true" />
            {{ suggestedCategory.name }}
          </button>

          <!-- Alternative chips (up to 2) -->
          <button
            v-for="alt in altCategories"
            :key="alt.id"
            type="button"
            class="alt-chip"
            :data-testid="`alt-chip-${alt.id}`"
            @click.stop="approveFromButton($event, alt.id)"
          >
            {{ alt.name }}
          </button>

          <!-- Frequent-category quick picks -->
          <button
            v-for="cat in frequentPicks"
            :key="cat.id"
            type="button"
            class="alt-chip freq-chip"
            :data-testid="`freq-chip-${cat.id}`"
            @click.stop="approveFromButton($event, cat.id)"
          >
            {{ cat.name }}
          </button>

          <!-- Edit icon -->
          <button
            type="button"
            class="edit-btn"
            aria-label="Edit"
            data-testid="edit-btn"
            @click.stop="onEditClick($event)"
          >
            <Pencil :size="13" aria-hidden="true" />
          </button>
        </template>

        <template v-else>
          <span v-if="currentCategory" class="row-category">
            <template v-if="currentGroup">{{ currentGroup.name }} › </template>{{ currentCategory.name }}
          </span>
          <span v-else-if="item.category_name" class="row-category">{{ item.category_name }}</span>
          <span class="row-chevron">›</span>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.row-wrap {
  position: relative;
  overflow: hidden;
  background-color: var(--bg);
  border-radius: 10px;
  border: 1px solid var(--border);
  margin-bottom: 0.5rem;
}

.row-wrap--warning {
  border-radius: 0 10px 10px 0;
}

.row-wrap--c1 {
  border-left: 4px solid var(--error);
}

.row-wrap--c2 {
  border-left: 4px solid var(--warning);
}

.row-wrap--c3 {
  border-left: 4px solid rgba(245, 158, 11, 0.75);
}

.row-panel {
  position: absolute;
  top: 0;
  bottom: 0;
  right: 0;
  display: flex;
  align-items: stretch;
  pointer-events: none;
  width: 84px;
}

.panel--doubtful {
  width: 168px;
}

.panel-btn {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  flex: 1;
  background: var(--surface-2);
  border: none;
  color: var(--text);
  font-size: 0.68rem;
  cursor: pointer;
  transition: background 0.15s, flex 0.15s;
  min-width: 0;
}

.panel-btn--approve {
  background: rgba(34, 197, 94, 0.2);
  color: var(--success);
}

.panel-btn--approve.panel-btn--commit {
  background: var(--success);
  color: #000;
  flex: 2;
}

.panel-btn--shrink {
  flex: 0.5;
}

.panel-btn--commit {
  background: var(--accent);
  color: #fff;
}

.panel-btn--commit-always.panel-btn--commit {
  background: var(--accent);
}

.row-slider {
  position: relative;
  z-index: 1;
  background-color: var(--bg);
  touch-action: pan-y;
  user-select: none;
  padding: 0.625rem 0.75rem;
  transition: transform 0.2s cubic-bezier(0.4, 0, 0.2, 1);
  cursor: pointer;
}

.row-wrap--warning .row-slider {
  background-image: linear-gradient(rgba(245, 158, 11, 0.07), rgba(245, 158, 11, 0.07));
}

.row-top {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  margin-bottom: 4px;
}

.row-name {
  font-weight: 600;
  font-size: 0.9375rem;
  color: var(--text);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-wrap--warning .row-name {
  font-weight: 700;
}

.row-store {
  font-size: 0.8rem;
  color: var(--muted);
  white-space: nowrap;
  flex-shrink: 0;
}

.row-bottom {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  flex-wrap: wrap;
}

.correction-totals {
  display: flex;
  align-items: baseline;
  gap: 0.45rem;
  flex-basis: 100%;
  font-size: 0.74rem;
}

.correction-amount {
  color: var(--error);
  font-weight: 700;
}

.receipt-total {
  color: var(--muted);
}

.row-category {
  font-size: 0.78rem;
  color: var(--muted);
}

.tag-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 0.68rem;
  padding: 1px 5px;
  border-radius: 999px;
  background: var(--field);
  border: 1px solid var(--border);
  color: var(--muted);
}

.approve-chip {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 0.72rem;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(34, 197, 94, 0.15);
  color: var(--success);
  border: 1px solid rgba(34, 197, 94, 0.3);
  cursor: pointer;
  width: auto;
  transition: background 0.12s;
}

.approve-chip:hover {
  background: rgba(34, 197, 94, 0.25);
}

.sparkle-icon {
  color: #7aabff;
  flex-shrink: 0;
}

.alt-chip {
  display: inline-flex;
  align-items: center;
  font-size: 0.72rem;
  padding: 2px 7px;
  border-radius: 999px;
  background: var(--field);
  border: 1px solid var(--border);
  color: var(--muted);
  cursor: pointer;
  width: auto;
  transition: border-color 0.12s;
}

.alt-chip:hover {
  border-color: var(--border-strong);
}

.edit-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  background: none;
  border: none;
  color: var(--muted-2);
  cursor: pointer;
  border-radius: 6px;
  margin-left: auto;
  flex-shrink: 0;
  transition: color 0.12s;
}

.edit-btn:hover {
  color: var(--text);
}

.row-chevron {
  margin-left: auto;
  color: var(--muted-2);
  font-size: 0.9rem;
}
</style>
