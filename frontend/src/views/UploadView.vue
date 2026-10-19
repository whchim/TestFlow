<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError, api, type SpecCreated } from '@/api'

/**
 * 上传文档 → 发起运行。
 *
 * ## 两步合成一次点击

 * 单独"上传成功"没有任何用处——用户要的是"开始测"。分成两个按钮
 * 只会让人在两屏之间搬运 `spec_id`。
 *
 * ## `warnings` 必须显示在**发起之前**
 *
 * 解析的降级（相对 URL、没有 `servers`、`$ref` 超深）不会让上传失败，
 * 但它们会决定这次运行的结果：`declared_targets` 为空意味着**所有请求
 * 都会被 Guardrail 拒绝**，于是报告是一片红。不在这里说清，用户会
 * 以为是平台坏了——而真正的原因在他自己那份文档里。
 */
const router = useRouter()

const content = ref('')
const filename = ref('pasted.json')
const busy = ref(false)
const error = ref('')
const warnings = ref<{ code: string; detail: string }[]>([])
const uploaded = ref<SpecCreated | null>(null)

async function onFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  content.value = await file.text()
  filename.value = file.name
}

async function submit() {
  busy.value = true
  error.value = ''
  warnings.value = []
  uploaded.value = null
  try {
    const spec = await api.uploadSpec(content.value, filename.value)
    uploaded.value = spec
    warnings.value = spec.warnings
    const run = await api.createRun(spec.spec_id)
    await router.push({ name: 'confirm', params: { id: run.run_id } })
  } catch (err) {
    error.value = formatError(err)
  } finally {
    busy.value = false
  }
}

function formatError(err: unknown): string {
  if (err instanceof ApiError) {
    // 解析类问题的逐条定位（`05` §4.1）：JSON Pointer + 说明。
    // 把它们逐行列出来，用户才能一次改完所有问题，而不是
    // "改一处、传一次、看下一个错误"来回几十轮。
    const items = (err.problem?.errors ?? []).map((e) => `  ${e.pointer}: ${e.message}`)
    return [err.message, ...items].join('\n')
  }
  return String(err)
}
</script>

<template>
  <h1>上传 OpenAPI 文档</h1>
  <p class="hint">
    上传后平台会生成测试用例，然后**等你确认**——没被批准的用例不会被执行。
  </p>

  <div v-if="error" class="alert">{{ error }}</div>

  <div class="card">
    <div class="row">
      <input type="file" accept=".json,.yaml,.yml" @change="onFile" />
      <span class="hint" style="margin: 0">或直接粘贴到下面</span>
    </div>
    <textarea
      v-model="content"
      spellcheck="false"
      placeholder='{"openapi": "3.0.3", "info": {...}, "servers": [...], "paths": {...}}'
    ></textarea>
    <div class="row" style="margin-top: 0.75rem">
      <button class="primary" :disabled="busy || !content.trim()" @click="submit">
        {{ busy ? '处理中…' : '上传并开始生成用例' }}
      </button>
      <span v-if="uploaded" class="hint" style="margin: 0">
        已识别 {{ uploaded.operation_count }} 个接口
        <template v-if="uploaded.reused">（同一份文档，复用了已有的解析结果）</template>
      </span>
    </div>
  </div>

  <div v-if="warnings.length" class="card">
    <h2>文档里有一些信息不全的地方</h2>
    <p class="hint">这些不会阻止运行，但会影响结果的准确性。</p>
    <ul>
      <li v-for="w in warnings" :key="w.code">
        <span class="badge warn">{{ w.code }}</span> {{ w.detail }}
      </li>
    </ul>
  </div>
</template>
