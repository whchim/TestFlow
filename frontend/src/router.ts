import { createRouter, createWebHistory } from 'vue-router'

import ConfirmView from '@/views/ConfirmView.vue'
import ReportView from '@/views/ReportView.vue'
import UploadView from '@/views/UploadView.vue'

/**
 * 前端的三个路由。
 *
 * 用 **history 模式**（`createWebHistory`）而不是 hash 模式：URL 是
 * `/runs/01J.../confirm` 而不是 `/#/runs/...`——后者在工单、聊天记录、
 * 报告里被复制粘贴时会被截断或变形，而这些恰恰是这个链接最常见的去处。
 *
 * 代价是服务端必须把不存在的路径回退到 `index.html`
 * （`entrypoints/gateway.py::_install_frontend`）。
 */
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/upload' },
    { path: '/upload', name: 'upload', component: UploadView },
    { path: '/runs/:id/confirm', name: 'confirm', component: ConfirmView, props: true },
    { path: '/runs/:id/report', name: 'report', component: ReportView, props: true },
    // 未知路径回上传页，而不是白屏。用户从一个过期的链接进来时，
    // 一个空白页面不会告诉他该做什么。
    { path: '/:pathMatch(.*)*', redirect: '/upload' },
  ],
})
