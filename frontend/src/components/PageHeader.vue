<script setup>
defineProps({
  eyebrow: { type: String, required: true },
  title: { type: String, required: true },
  description: { type: String, default: '' },
  context: { type: String, default: '' },
})
</script>

<template>
  <header class="page-header">
    <div>
      <span>{{ eyebrow }}</span>
      <h1 tabindex="-1">{{ title }}</h1>
      <p v-if="description">{{ description }}</p>
      <small v-if="context" class="page-context">{{ context }}</small>
    </div>
    <div v-if="$slots.default" class="page-actions"><slot /></div>
  </header>
</template>
