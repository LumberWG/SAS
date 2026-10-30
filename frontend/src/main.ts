import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import { useThemeStore } from './stores/theme'
import './style.css'

const app = createApp(App)

const pinia = createPinia()
app.use(pinia)
app.use(router)

// 挂载前把主题写到 <html data-theme>，避免首帧闪一下默认主题
useThemeStore(pinia).apply()

app.mount('#app')
