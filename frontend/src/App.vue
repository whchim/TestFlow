<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <header class="topbar">
    <RouterLink to="/upload" class="brand">TestFlow</RouterLink>
    <span class="tagline">上传 OpenAPI 文档 → 人工确认 → 看报告</span>
  </header>

  <main class="page">
    <RouterView />
  </main>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: baseline;
  gap: 0.75rem;
  padding: 0.9rem 1.5rem;
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
.page {
  max-width: 60rem;
  margin: 0 auto;
  padding: 1.5rem;
}
</style>
