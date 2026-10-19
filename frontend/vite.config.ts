import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    // 开发模式（`npm run dev`，5173 端口）把 API 请求转给 Gateway。
    //
    // **生产构建不需要它**：dist 是由 Gateway 自己托管的
    // （`entrypoints/gateway.py::_install_frontend`），同源，没有跨域问题。
    // 而开发时必须转，否则浏览器会因为跨域把请求拦下来——
    // 而那个失败在控制台里看起来像"后端挂了"。
    //
    // ⚠️ 只转 `/api`。**不要转 `/internal`**：那些端点需要内部令牌
    // （`05` §5.3），而浏览器里不该有任何东西持有它。
    proxy: {
      '/api': { target: 'http://127.0.0.1:3000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    // sourcemap 不发布：它会把未压缩的源码一起送出去，
    // 而这是个会存凭证的系统，没有必要让攻击者省下这一步。
    sourcemap: false,
  },
})
