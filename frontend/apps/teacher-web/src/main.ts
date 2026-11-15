import { createApp } from 'vue'
import { createPinia } from 'pinia'
import 'ant-design-vue/dist/reset.css'
// Use the static stylesheet so chart loading preserves Vite's page styles.
import 'vue-echarts/csp/style.css'
import App from './App.vue'
import router from './router'
import './styles/global.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)

app.mount('#app')
