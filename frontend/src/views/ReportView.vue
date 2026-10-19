<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

import { ApiError, TERMINAL_PHASES, api, describePhase, type RunStatus } from '@/api'

/**
 * 报告页。
 *
 * ## 为什么直接显示 Markdown 原文

 * 服务端存的是**写入时渲染好的快照**（RA4），不是视图。前端把它原样
 * 放进 `<pre>` 里，是最不容易出错的做法：任何一点点"顺手美化"都会
 * 让屏幕上的东西与归档的那份不再逐字一致——而报告会被引用到工单与
 * 评审材料里，"当时看到的"和"现在查到的"必须一致。
 *
 * `<pre>` 而不是 `v-html`：后者会把报告里的内容当 HTML 执行。
 * 报告的内容来自模型生成 + 用户上传的文档描述——**是不可信输入**。
 * 一个 `<script>` 或 `onerror=` 就能在别人的浏览器里跑起来。
 * 显示格式差一点，比一个存储型 XSS 好得多。
 *
 * ## 报告没就绪时不说"失败"
 *
 * `report_not_ready`（404）的含义是"还没跑到那一步"，不是出错了。
 * 界面要说清当前阶段，并告诉用户等一等——把它渲染成红色错误会让
 * 用户以为要重跑。
 */
const props = defineProps<{ id: string }>()

const status = ref<RunStatus | null>(null)
const markdown = ref('')
const notReady = ref(false)
const error = ref('')
const loading = ref(true)

let poller: number | null = null

async function refresh() {
  try {
    status.value = await api.getRun(props.id)
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : String(err)
    loading.value = false
    return
  }

  try {
    markdown.value = await api.getReportMarkdown(props.id)
    notReady.value = false
  } catch (err) {
    if (err instanceof ApiError && err.code === 'report_not_ready') {
      notReady.value = true
    } else {
      error.value = err instanceof ApiError ? err.message : String(err)
    }
  } finally {
    loading.value = false
  }

  if (notReady.value && !TERMINAL_PHASES.has(status.value.phase) && poller === null) {
    // 报告还没生成，而运行还在跑 → 定时看一眼。用户不需要手动刷新。
    poller = window.setInterval(() => void refresh(), 5000)
  }
}

function stopPolling() {
  if (poller !== null) {
    window.clearInterval(poller)
    poller = null
  }
}

onMounted(async () => {
  await refresh()
  if (!notReady.value) stopPolling()
})

onUnmounted(stopPolling)
</script>

<template>
  <h1>测试报告</h1>

  <div v-if="loading" class="card">加载中…</div>

  <div v-if="status" class="card">
    <div class="row">
      <span class="badge">{{ describePhase(status.phase) }}</span>
      <span class="hint" style="margin: 0">
        用例 {{ status.settled_case_count }}/{{ status.expected_case_count }} 已执行
      </span>
      <RouterLink :to="{ name: 'confirm', params: { id } }">
        <button>回到确认页</button>
      </RouterLink>
    </div>
    <p v-if="status.failure_reason" class="hint" style="margin-top: 0.5rem">
      失败原因：{{ status.failure_reason }}
    </p>
  </div>

  <div v-if="error" class="alert">{{ error }}</div>

  <div v-if="notReady" class="card">
    <h2>报告还没生成</h2>
    <p class="hint">
      当前阶段是「{{ describePhase(status?.phase ?? '') }}」。测试跑完之后报告会出现在这里，
      这个页面会自动更新——不用手动刷新。
    </p>
    <p class="hint">
      如果这个运行停在「等待人工确认」，先去确认页批准用例：没被批准的用例不会被执行。
    </p>
  </div>

  <pre v-if="markdown" class="report">{{ markdown }}</pre>
</template>

<style scoped>
.report {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 1.25rem;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 0.82rem;
  line-height: 1.55;
  margin: 0;
}
</style>
