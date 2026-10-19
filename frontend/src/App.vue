<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'

import { getToken, setToken } from '@/api'

// Token 存在 localStorage，刷新页面后不用重新输入。
// 它是单机自用工具，浏览器同源脚本本来就能读写这个页面——换个地方存
// 不会让它更安全，只会让用户每次打开都重新填一遍。
const token = ref(getToken())

function saveToken(): void {
  setToken(token.value)
}
</script>

<template>
  <header class="topbar">
    <RouterLink to="/upload" class="brand">TestFlow</RouterLink>
    <span class="tagline">上传 OpenAPI 文档 → 人工确认 → 看报告</span>

    <label class="token" title="与 .env 里的 TESTFLOW_API_TOKEN 相同；只保存在本机浏览器">
      <span>Token</span>
      <input
        v-model="token"
        type="password"
        autocomplete="off"
        spellcheck="false"
        placeholder="TESTFLOW_API_TOKEN"
        @input="saveToken"
      />
    </label>
  </header>

  <main class="page">
    <RouterView />
  </main>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  padding: 0.75rem 1.5rem;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
}
.brand {
  font-weight: 600;
  font-size: 1.05rem;
  color: var(--text);
  text-decoration: none;
}
.tagline {
  color: var(--muted);
  font-size: 0.85rem;
}
.token {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin-left: auto;
  color: var(--muted);
  font-size: 0.8rem;
}
.token input {
  width: 15rem;
  padding: 0.3rem 0.5rem;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  font: inherit;
  font-size: 0.8rem;
}
.token input:focus {
  outline: none;
  border-color: var(--accent);
}
.page {
  max-width: 60rem;
  margin: 0 auto;
  padding: 1.5rem;
}
</style>
