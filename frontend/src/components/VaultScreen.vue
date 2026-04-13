<script setup lang="ts">
import { ref, computed } from 'vue'
import { useAppStore } from '@/stores/app'
import Typography from '@/components/ui/Typography.vue'

const appStore = useAppStore()
const submitting = ref(false)
const activeHelp = ref<string | null>(null)

const helpContent: Record<string, { title: string; steps: { text: string; link?: string; bold?: string; text2?: string }[] }> = {
  GRVT: {
    title: 'HOW TO GET YOUR API KEY:',
    steps: [
      { text: 'Create an account at grvt.io', link: 'https://grvt.io/exchange/perpetual/BTC-USDT' },
      { text: 'Go to Account → API Keys', link: 'https://grvt.io/exchange/account/api-keys' },
      { text: 'Click "Create API Key"' },
      { text: 'Select permissions: enable Trading' },
      { text: 'Copy the API Key and Private Key' },
      { text: 'Copy your Trading Account ID from the account page' },
    ],
  },
  Extended: {
    title: 'HOW TO GET YOUR API KEY:',
    steps: [
      { text: 'Instructions will be provided separately' },
    ],
  },
  Variational: {
    title: 'HOW TO GET YOUR TOKEN:',
    steps: [
      { text: 'Log in to omni.variational.io', link: 'https://omni.variational.io' },
      { text: 'Open Developer Tools (F12)' },
      { text: 'Go to Application → Cookies' },
      { text: 'Copy the ', bold: 'vr-token', text2: ' value' },
    ],
  },
}

const groups = ['GRVT', 'Extended', 'Variational']

function fieldsForGroup(group: string) {
  return keyFields.filter(f => f.group === group)
}

function toggleHelp(group: string) {
  activeHelp.value = activeHelp.value === group ? null : group
}

// Legacy vault password fields
const password = ref('')
const confirmPassword = ref('')

// API key fields
const keys = ref({
  grvt_api_key: '',
  grvt_private_key: '',
  grvt_trading_account_id: '',
  extended_api_key: '',
  extended_public_key: '',
  extended_private_key: '',
  extended_vault: '',
  variational_jwt_token: '',
})

const mode = computed(() => {
  if (appStore.needsKeySetup) return 'keys'
  if (appStore.vaultSetupRequired) return 'setup'
  return 'unlock'
})

async function handleSubmit() {
  submitting.value = true
  if (mode.value === 'keys') {
    // Filter out empty values
    const nonEmpty: Record<string, string> = {}
    for (const [k, v] of Object.entries(keys.value)) {
      if (v.trim()) nonEmpty[k] = v.trim()
    }
    if (Object.keys(nonEmpty).length === 0) {
      appStore.vaultError = 'Please enter at least one API key'
      submitting.value = false
      return
    }
    await appStore.saveKeys(nonEmpty)
  } else if (mode.value === 'setup') {
    if (password.value !== confirmPassword.value) {
      appStore.vaultError = 'Passwords do not match'
      submitting.value = false
      return
    }
    if (password.value.length < 8) {
      appStore.vaultError = 'Password must be at least 8 characters'
      submitting.value = false
      return
    }
    await appStore.setupVault(password.value)
  } else {
    await appStore.unlockVault(password.value)
  }
  submitting.value = false
}

const keyFields = [
  { key: 'grvt_api_key', label: 'GRVT API Key', group: 'GRVT' },
  { key: 'grvt_private_key', label: 'GRVT Private Key', group: 'GRVT' },
  { key: 'grvt_trading_account_id', label: 'GRVT Trading Account ID', group: 'GRVT' },
  { key: 'extended_api_key', label: 'Extended API Key', group: 'Extended' },
  { key: 'extended_public_key', label: 'Extended Public Key', group: 'Extended' },
  { key: 'extended_private_key', label: 'Extended Private Key', group: 'Extended' },
  { key: 'extended_vault', label: 'Extended Vault ID', group: 'Extended' },
  { key: 'variational_jwt_token', label: 'Variational JWT Token', group: 'Variational' },
]
</script>

<template>
  <div :class="$style.page">
    <div :class="[$style.card, mode === 'keys' ? $style.wideCard : '']">
      <div :class="$style.logo">
        <Typography size="text-h4" weight="bold" font="bricolage">TradeAutonom</Typography>
      </div>

      <div :class="$style.icon">{{ mode === 'keys' ? '�' : '�' }}</div>

      <Typography size="text-lg" weight="semibold" :class="$style.title">
        {{ mode === 'keys' ? 'Configure API Keys' : (mode === 'setup' ? 'Set Up Vault Password' : 'Unlock Vault') }}
      </Typography>

      <Typography size="text-sm" color="secondary" :class="$style.subtitle">
        {{ mode === 'keys'
          ? 'Enter your exchange API keys to get started. Keys are encrypted and stored securely.'
          : mode === 'setup'
            ? 'Create a password to encrypt your exchange API keys.'
            : 'Enter your vault password to decrypt exchange credentials and start trading.'
        }}
      </Typography>

      <form @submit.prevent="handleSubmit" :class="$style.form">
        <!-- API Key mode -->
        <template v-if="mode === 'keys'">
          <div v-for="group in groups" :key="group" :class="$style.section">
            <div :class="$style.sectionHeader">
              <Typography size="text-sm" weight="semibold" color="secondary">{{ group }}</Typography>
              <button type="button" :class="[$style.helpBtn, activeHelp === group ? $style.helpBtnActive : '']" @click="toggleHelp(group)" title="Setup instructions">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5"/><text x="8" y="11.5" text-anchor="middle" fill="currentColor" font-size="10" font-weight="600" font-family="Inter, sans-serif">?</text></svg>
              </button>
            </div>
            <div v-for="field in fieldsForGroup(group)" :key="field.key" :class="$style.field">
              <label :class="$style.label">
                <Typography size="text-sm" color="secondary">{{ field.label }}</Typography>
              </label>
              <input
                v-model="(keys as any)[field.key]"
                type="text"
                :placeholder="field.label"
                :class="$style.input"
                autocomplete="off"
              />
            </div>
          </div>
        </template>

        <!-- Password mode (unlock / setup) -->
        <template v-else>
          <div :class="$style.field">
            <label :class="$style.label">
              <Typography size="text-sm" color="secondary">Password</Typography>
            </label>
            <input
              v-model="password"
              type="password"
              :placeholder="mode === 'setup' ? 'Min. 8 characters' : 'Vault password'"
              required
              :minlength="mode === 'setup' ? 8 : 1"
              :class="$style.input"
              autocomplete="current-password"
            />
          </div>

          <div v-if="mode === 'setup'" :class="$style.field">
            <label :class="$style.label">
              <Typography size="text-sm" color="secondary">Confirm Password</Typography>
            </label>
            <input
              v-model="confirmPassword"
              type="password"
              placeholder="Repeat password"
              required
              minlength="8"
              :class="$style.input"
              autocomplete="new-password"
            />
          </div>
        </template>

        <div v-if="appStore.vaultError" :class="$style.error">
          <Typography size="text-sm" color="error">{{ appStore.vaultError }}</Typography>
        </div>

        <button type="submit" :class="$style.submitBtn" :disabled="submitting">
          <Typography size="text-sm" weight="semibold" color="primary">
            {{ submitting ? 'Please wait...' : (mode === 'keys' ? 'Save & Connect' : (mode === 'setup' ? 'Set Password' : 'Unlock')) }}
          </Typography>
        </button>
      </form>
    </div>
    <!-- Help panel (slides in from the right) -->
    <Transition name="slide">
      <div v-if="activeHelp && helpContent[activeHelp]" :class="$style.helpPanel" @click.self="activeHelp = null">
        <div :class="$style.helpCard">
          <div :class="$style.helpHeader">
            <Typography size="text-sm" weight="semibold" color="secondary">
              {{ helpContent[activeHelp].title }}
            </Typography>
            <button type="button" :class="$style.helpClose" @click="activeHelp = null">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M11 3L3 11M3 3l8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
            </button>
          </div>
          <ol :class="$style.helpSteps">
            <li v-for="(step, idx) in helpContent[activeHelp].steps" :key="idx" :class="$style.helpStep">
              <Typography size="text-sm">
                <template v-if="step.link">
                  <a :href="step.link" target="_blank" rel="noopener" :class="$style.helpLink">{{ step.text }}</a>
                </template>
                <template v-else-if="step.bold">
                  {{ step.text }}<strong>{{ step.bold }}</strong>{{ step.text2 || '' }}
                </template>
                <template v-else>{{ step.text }}</template>
              </Typography>
            </li>
          </ol>
        </div>
      </div>
    </Transition>
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

.wideCard {
  max-width: 520px;
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
  gap: var(--space-4);
}

.logo {
  text-align: center;
}

.icon {
  text-align: center;
  font-size: 2rem;
}

.title {
  text-align: center;
}

.subtitle {
  text-align: center;
  line-height: 1.5;
}

.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  margin-top: var(--space-2);
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

.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
}

.sectionHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.helpBtn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: none;
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  transition: color 0.15s, background 0.15s;
  padding: 0;
}

.helpBtn:hover,
.helpBtnActive {
  color: var(--color-text-primary);
  background: var(--color-white-2);
}

.helpPanel {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: 340px;
  z-index: 100;
  display: flex;
  flex-direction: column;
  padding: var(--space-6);
  padding-top: 80px;
}

.helpCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.helpHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.helpClose {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: var(--radius-md);
  border: none;
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  transition: color 0.15s;
}

.helpClose:hover {
  color: var(--color-text-primary);
}

.helpSteps {
  margin: 0;
  padding-left: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.helpStep {
  line-height: 1.6;
}

.helpLink {
  color: var(--color-brand, #60a5fa);
  text-decoration: none;
}

.helpLink:hover {
  text-decoration: underline;
}
</style>
