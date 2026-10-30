<script setup lang="ts">
// 根组件：承载路由视图 + 全局浮层（提示 / 令牌输入），布局由 DefaultLayout 提供
import ToastHost from './components/ToastHost.vue'
import TokenGate from './components/TokenGate.vue'
</script>

<template>
  <router-view />
  <ToastHost />
  <TokenGate />
</template>
