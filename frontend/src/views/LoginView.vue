<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import Typography from '@/components/ui/Typography.vue'

const router = useRouter()
const authStore = useAuthStore()

const mode = ref<'login' | 'register'>('login')
const email = ref('')
const password = ref('')
const name = ref('')
const submitting = ref(false)

async function handleSubmit() {
  submitting.value = true
  let success = false
  if (mode.value === 'login') {
    success = await authStore.login(email.value, password.value)
  } else {
    success = await authStore.register(email.value, password.value, name.value || email.value.split('@')[0])
  }
  submitting.value = false
  if (success) {
    router.push('/')
  }
}
</script>

<template>
  <div :class="$style.page">
    <div :class="$style.card">
      <div :class="$style.logo">
        <Typography size="text-h4" weight="bold" font="bricolage">TradeAutonom</Typography>
      </div>

      <Typography size="text-lg" weight="semibold" :class="$style.title">
        {{ mode === 'login' ? 'Sign In' : 'Create Account' }}
      </Typography>

      <form @submit.prevent="handleSubmit" :class="$style.form">
        <div v-if="mode === 'register'" :class="$style.field">
          <label :class="$style.label">
            <Typography size="text-sm" color="secondary">Name</Typography>
          </label>
          <input
            v-model="name"
            type="text"
            placeholder="Your name"
            :class="$style.input"
            autocomplete="name"
          />
        </div>

        <div :class="$style.field">
          <label :class="$style.label">
            <Typography size="text-sm" color="secondary">Email</Typography>
          </label>
          <input
            v-model="email"
            type="email"
            placeholder="you@example.com"
            required
            :class="$style.input"
            autocomplete="email"
          />
        </div>

        <div :class="$style.field">
          <label :class="$style.label">
            <Typography size="text-sm" color="secondary">Password</Typography>
          </label>
          <input
            v-model="password"
            type="password"
            placeholder="Min. 8 characters"
            required
            minlength="8"
            :class="$style.input"
            autocomplete="current-password"
          />
        </div>

        <div v-if="authStore.error" :class="$style.error">
          <Typography size="text-sm" color="error">{{ authStore.error }}</Typography>
        </div>

        <button type="submit" :class="$style.submitBtn" :disabled="submitting">
          <Typography size="text-sm" weight="semibold" color="primary">
            {{ submitting ? 'Please wait...' : (mode === 'login' ? 'Sign In' : 'Create Account') }}
          </Typography>
        </button>
      </form>

      <div :class="$style.toggle">
        <Typography size="text-sm" color="tertiary">
          {{ mode === 'login' ? "Don't have an account?" : 'Already have an account?' }}
        </Typography>
        <button :class="$style.toggleBtn" @click="mode = mode === 'login' ? 'register' : 'login'">
          <Typography size="text-sm" weight="semibold" color="brand">
            {{ mode === 'login' ? 'Create Account' : 'Sign In' }}
          </Typography>
        </button>
      </div>
    </div>
  </div>
</template>

<style module>
.page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: var(--space-6);
}

.card {
  width: 100%;
  max-width: 400px;
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-8);
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.logo {
  text-align: center;
}

.title {
  text-align: center;
}

.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.label {
  padding-left: 2px;
}

.input {
  width: 100%;
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  font-family: Inter, system-ui, sans-serif;
  outline: none;
  transition: border-color 0.15s;
}

.input::placeholder {
  color: var(--color-text-tertiary);
}

.input:focus {
  border-color: var(--color-text-secondary);
}

.error {
  padding: var(--space-2) var(--space-3);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-md);
}

.submitBtn {
  width: 100%;
  padding: var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  cursor: pointer;
  transition: all 0.15s;
}

.submitBtn:hover:not(:disabled) {
  background: var(--color-white-2);
  border-color: var(--color-text-tertiary);
}

.submitBtn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.toggle {
  text-align: center;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
}

.toggleBtn {
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
}

.toggleBtn:hover {
  text-decoration: underline;
}
</style>
