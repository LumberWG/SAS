<script setup lang="ts">
// 根组件：仅承载路由视图，布局由 DefaultLayout 提供
import ToastHost from './components/ToastHost.vue'
</script>

<template>
  <router-view />
  <ToastHost />
</template>
