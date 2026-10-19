<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError, TERMINAL_PHASES, api, describePhase, type CaseItem, type RunStatus } from '@/api'
import CaseCard from '@/components/CaseCard.vue'

/**
 * 人工确认门（IP-1）。
 *
 * ## REST 是权威，SSE 只用来知道"该查了"（API7）
 *
 * 这里**不用** SSE 的数据：即使事件流里的阶段是新鲜的，界面显示的
 * 仍然是 `GET /api/runs/{id}` 的结果。理由有两条，都很实际：
 *
 * * SSE 连接可能断、可能被中间层缓冲。一旦把它当数据源，断线期间界面
 *   会显示一个**过期的**状态，而用户不知道它过期了
 * * 事件里本来也没有数据（这是刻意的设计）。所以"收到信号 → 立刻 REST
 *   拉一次"是唯一正确的用法
 *
 * `EventSource` 断开时浏览器会自动重连，而重连之后我们会立刻拉一次状态
 * ——所以断线期间发生的变化不会丢。
 *
 * ## 决策必须**一次提交全部**
 *
 * `05` §4.5：只提交一部分会让图带着"还有未决用例"的状态恢复，而那些
 * 没被提到的用例**既不执行也不出现在报告里**。所以这里统计"已决定"
 * 与"总数"，不齐就不让提交——并在按钮上写清还差几条。让服务端报 422
 * 也行，但用户要多跑一个来回才知道自己漏了什么。
 */
const props = defineProps<{ id: string }>()
const router = useRouter()

const status = ref<RunStatus | null>(null)
const cases = ref<CaseItem[]>([])
const total = ref(0)
const decisions = ref<Record<string, 'approve' | 'reject'>>({})
const error = ref('')
const busy = ref(false)
const loading = ref(true)

let source: EventSource | null = null
let poller: number | null = null

const decidedCount = computed(() => Object.keys(decisions.value).length)
const isAwaiting = computed(() => status.value?.phase === 'awaiting_review')
const allDecided = computed(() => total.value > 0 && decidedCount.value >= total.value)
const lowCount = computed(() => cases.value.filter((c) => c.confidence === 'low').length)

async function refresh() {
  try {
    const s = await api.getRun(props.id)
    status.value = s
    if (s.phase === 'awaiting_review') {
      // 上限 500 与后端一致（`05` §4.3 的契约上限）。
      // 一次拿不完时分页——但那属于"用例特别多"的场景，界面上会提示。
      const page = await api.getCases(props.id, 500, 0)
      cases.value = page.items
      total.value = page.total
    }
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function decide(caseId: string, decision: 'approve' | 'reject') {
  decisions.value = { ...decisions.value, [caseId]: decision }
}

function approveAll() {
  const next: Record<string, 'approve' | 'reject'> = {}
  for (const c of cases.value) next[c.case_id] = 'approve'
  decisions.value = next
}

async function submit() {
  busy.value = true
  error.value = ''
  try {
    await api.submitDecisions(
      props.id,
      Object.entries(decisions.value).map(([case_id, decision]) => ({ case_id, decision })),
    )
    await router.push({ name: 'report', params: { id: props.id } })
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : String(err)
    // 409 的含义是"这个 Run 已经不需要你确认了"。刷新一次让界面
    // 追上真实状态——比让用户对着一个过期的界面反复点击要好。
    await refresh()
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await refresh()
  if (!TERMINAL_PHASES.has(status.value?.phase ?? '')) {
    source = new EventSource(`/api/runs/${encodeURIComponent(props.id)}/events`)
    source.addEventListener('run.phase', () => void refresh())
    // 兜底轮询：SSE 的中间层（代理、nginx）可能缓冲掉事件，
    // 而事件流的实现本身是**短间隔轮询**（M1 的偏差，`05` §4.4）。
    // 一个低频的兜底查询保证界面最终会追上，代价可以忽略。
    poller = window.setInterval(() => void refresh(), 5000)
  }
})

onUnmounted(() => {
  source?.close()
  if (poller !== null) window.clearInterval(poller)
})
</script>

<template>
  <h1>确认测试用例</h1>

  <div v-if="loading" class="card">加载中…</div>

  <div v-else-if="status" class="card">
    <div class="row">
      <span class="badge">{{ describePhase(status.phase) }}</span>
      <span class="hint" style="margin: 0">
        用例 {{ status.settled_case_count }}/{{ status.expected_case_count }} 已执行
      </span>
    </div>

    <template v-if="!isAwaiting">
      <p class="hint" style="margin-top: 0.6rem">
        当前没有待确认的用例。这个页面会在状态变化时自动更新。
      </p>
      <RouterLink v-if="status.phase !== 'init'" :to="{ name: 'report', params: { id } }">
        <button>去看报告</button>
      </RouterLink>
    </template>

    <p v-else class="hint" style="margin-top: 0.6rem">
      共 {{ total }} 条用例，其中
      <strong>{{ lowCount }}</strong> 条是低置信度（期望值有一部分是推断的）。
      没被批准的用例**不会被执行**，也不会出现在报告里。
    </p>
  </div>

  <div v-if="error" class="alert">{{ error }}</div>

  <template v-if="isAwaiting">
    <div class="row" style="margin-bottom: 0.75rem">
      <button :disabled="busy" @click="approveAll">全部批准</button>
      <button class="primary" :disabled="busy || !allDecided" @click="submit">
        {{
          allDecided
            ? `提交 ${decidedCount} 条决策`
            : `还差 ${total - decidedCount} 条未决定`
        }}
      </button>
      <span class="hint" style="margin: 0">已决定 {{ decidedCount }}/{{ total }}</span>
    </div>

    <p class="hint">
      低置信度的用例排在前面——它们的期望值有一部是推断出来的，最值得你逐条核对。
    </p>

    <CaseCard
      v-for="item in cases"
      :key="item.case_id"
      :item="{ ...item, review_status: decisions[item.case_id] === 'approve' ? 'approved' : decisions[item.case_id] === 'reject' ? 'rejected' : item.review_status }"
      :decided="busy"
      @decide="decide"
    />
  </template>
</template>
