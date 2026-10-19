<script setup lang="ts">
import { computed } from 'vue'

import type { CaseItem } from '@/api'

/**
 * 一条用例 + 批准 / 拒绝。
 *
 * ## 为什么把断言也显示出来

 * 确认界面的**全部意义**就是让人看"这条用例到底要断言什么"。只显示
 * 名字与维度的话，用户只能凭自然语言的标题猜——而标题是模型写的，
 * 断言是结构化的，两者可以不一致（一个像样的标题配一条错的断言）。
 *
 * ## 低置信度要高亮，但**不能**自动勾选
 *
 * 高亮是"请多看一眼"，自动拒绝是"我替你决定了"。两者混起来会让
 * 用户以为系统已经筛过一遍——而这是他要亲手签的字。
 */
const props = defineProps<{ item: CaseItem; decided: boolean }>()
const emit = defineEmits<{ (e: 'decide', caseId: string, decision: 'approve' | 'reject'): void }>()

const isLow = computed(() => props.item.confidence === 'low')

/** 把一条断言渲染成人能读的一行。**只读已知字段**，其余原样 JSON。 */
function describeAssertion(a: Record<string, unknown>): string {
  const type = String(a.type ?? '?')
  if (type === 'status_code') return `状态码 = ${String(a.expected ?? '?')}`
  if (type === 'json_path' || type === 'schema' || type === 'header') {
    const path = a.path ?? a.name ?? ''
    const op = a.op ?? a.operator ?? ''
    const expected = a.expected !== undefined ? ` ${String(a.expected)}` : ''
    return `${type} ${String(path)} ${String(op)}${expected}`.trim()
  }
  return JSON.stringify(a)
}

function sourceOf(a: Record<string, unknown>): string {
  return a.derived_from === 'spec' ? '文档声明' : '推断'
}
</script>

<template>
  <div class="case" :class="{ low: isLow, rejected: item.review_status === 'rejected' }">
    <div class="head">
      <span class="badge method">{{ item.request_method }}</span>
      <span class="path">{{ item.request_path }}</span>
      <span v-if="isLow" class="badge warn" title="期望值有一部分是推断出来的，请重点核对">
        低置信度
      </span>
      <span v-if="item.is_destructive" class="badge warn" title="破坏性方法不会被执行">
        破坏性 · 不会执行
      </span>
    </div>

    <div class="name">{{ item.name }}</div>
    <div class="meta">维度 {{ item.dimension }} · {{ item.category }}</div>

    <ul class="assertions">
      <li v-for="(a, i) in item.assertions" :key="i">
        {{ describeAssertion(a) }}
        <span class="src" :class="{ inferred: a.derived_from === 'inferred' }">
          {{ sourceOf(a) }}
        </span>
      </li>
    </ul>

    <div class="row">
      <button
        :class="{ primary: item.review_status === 'approved' }"
        :disabled="decided"
        @click="emit('decide', item.case_id, 'approve')"
      >
        批准
      </button>
      <button
        :class="{ danger: item.review_status === 'rejected' }"
        :disabled="decided"
        @click="emit('decide', item.case_id, 'reject')"
      >
        拒绝
      </button>
      <span v-if="item.review_status !== 'pending'" class="badge">
        {{ item.review_status === 'approved' ? '已批准' : '已拒绝' }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.case {
  border: 1px solid var(--line);
  border-left-width: 3px;
  border-radius: 6px;
  padding: 0.7rem 0.9rem;
  margin-bottom: 0.6rem;
  background: var(--surface);
}
.case.low {
  border-left-color: var(--warn);
  background: var(--warn-soft);
}
.case.rejected {
  opacity: 0.55;
}
.head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.path {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 0.85rem;
}
.name {
  margin-top: 0.3rem;
  font-weight: 500;
}
.meta {
  color: var(--muted);
  font-size: 0.8rem;
}
.assertions {
  margin: 0.5rem 0 0.6rem;
  padding-left: 1.1rem;
  font-size: 0.85rem;
}
.src {
  color: var(--muted);
  font-size: 0.75rem;
  margin-left: 0.35rem;
}
.src.inferred {
  color: var(--warn);
}
button.danger {
  border-color: var(--danger);
  color: var(--danger);
}
</style>
