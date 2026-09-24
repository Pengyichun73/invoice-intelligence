<script setup>
defineProps({ eyebrow: { type: String, required: true }, title: { type: String, required: true }, description: { type: String, default: '' } })
</script>

<template>
  <header class="page-header"><div><span>{{ eyebrow }}</span><h1>{{ title }}</h1><p v-if="description">{{ description }}</p></div><div class="page-actions"><slot /></div></header>
</template>
