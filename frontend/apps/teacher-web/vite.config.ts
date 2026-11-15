import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { AntDesignVueResolver } from 'unplugin-vue-components/resolvers'
import { resolve } from 'path'

export default defineConfig({
  plugins: [
    vue(),
    // 按页面按需引入 Ant Design Vue 组件，避免主入口打包全部组件。
    Components({
      resolvers: [AntDesignVueResolver({ importStyle: false })],
      dts: false,
    }),
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/socket.io': {
        target: 'http://localhost:8000',
        ws: true,
      },
      '/ws': {
        target: 'http://localhost:8000',
        ws: true,
        rewrite: (path) => `/api${path}`,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'vue-vendor': ['vue', 'vue-router', 'pinia'],
          'echarts-vendor': ['echarts', 'vue-echarts/csp'],
        },
      },
    },
  },
  css: {
    preprocessorOptions: {
      less: {
        modifyVars: {
          'primary-color': '#315FE8',
        },
        javascriptEnabled: true,
      },
    },
  },
})
