<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { fetchSecretsKeys, updateSecretsKeys, nadoPrepareLink, nadoSubmitLink, nadoLinkStatus } from '@/lib/api'
import { BrowserProvider } from 'ethers'
import Typography from '@/components/ui/Typography.vue'

const loading = ref(true)
const saving = ref(false)
const success = ref('')
const error = ref('')
const activeHelp = ref<string | null>(null)

const helpContent: Record<string, { title: string; steps: { text: string; link?: string; bold?: string; text2?: string }[] }> = {
  GRVT: {
    title: 'HOW TO GET YOUR API KEY:',
    steps: [
      { text: 'Create an account at grvt.io', link: 'https://grvt.io/exchange/perpetual/BTC-USDT' },
      { text: 'Go to Account \u2192 API Keys', link: 'https://grvt.io/exchange/account/api-keys' },
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
      { text: 'Go to Application \u2192 Cookies' },
      { text: 'Copy the ', bold: 'vr-token', text2: ' value' },
    ],
  },
  NADO: {
    title: 'HOW TO CONNECT YOUR WALLET:',
    steps: [
      { text: 'Install MetaMask or any EVM wallet' },
      { text: 'Deposit at least $5 USDT0 to your NADO account', link: 'https://app.nado.xyz' },
      { text: 'Click "Connect Wallet" below' },
      { text: 'Click "Authorize Bot" to sign the delegation message' },
      { text: 'Your wallet key never leaves MetaMask \u2014 only a signature is sent' },
    ],
  },
}

function toggleHelp(group: string) {
  activeHelp.value = activeHelp.value === group ? null : group
}

const keyFields = [
  { key: 'grvt_api_key', label: 'API Key', group: 'GRVT' },
  { key: 'grvt_private_key', label: 'Private Key', group: 'GRVT' },
  { key: 'grvt_trading_account_id', label: 'Trading Account ID', group: 'GRVT' },
  { key: 'extended_api_key', label: 'API Key', group: 'Extended' },
  { key: 'extended_public_key', label: 'Public Key', group: 'Extended' },
  { key: 'extended_private_key', label: 'Private Key', group: 'Extended' },
  { key: 'extended_vault', label: 'Vault ID', group: 'Extended' },
  { key: 'variational_jwt_token', label: 'JWT Token', group: 'Variational' },
]

const groups = ['GRVT', 'Extended', 'Variational', 'NADO']

// Current masked values from D1
const masked = ref<Record<string, string>>({})
// User input (only non-empty, non-masked values get saved)
const form = ref<Record<string, string>>({})

// ── NADO wallet-connect state ──────────────────────────
const nadoWallet = ref('')
const nadoConnecting = ref(false)
const nadoAuthorizing = ref(false)
const nadoStatus = ref<{ has_trading_key: boolean; wallet_address: string; subaccount_name: string; remote_linked_signer: string | null } | null>(null)
const nadoSubaccount = ref('default')
const nadoError = ref('')
const nadoSuccess = ref('')

async function connectNadoWallet() {
  nadoError.value = ''
  nadoConnecting.value = true
  try {
    if (!(window as any).ethereum) {
      nadoError.value = 'No wallet found. Please install MetaMask.'
      return
    }
    const provider = new BrowserProvider((window as any).ethereum)
    const accounts = await provider.send('eth_requestAccounts', [])
    if (accounts.length > 0) {
      nadoWallet.value = accounts[0]
    }
  } catch (e) {
    nadoError.value = e instanceof Error ? e.message : 'Wallet connection failed'
  } finally {
    nadoConnecting.value = false
  }
}

async function authorizeNadoBot() {
  nadoError.value = ''
  nadoSuccess.value = ''
  nadoAuthorizing.value = true
  try {
    if (!nadoWallet.value) {
      nadoError.value = 'Connect wallet first'
      return
    }
    // Step 1: get EIP-712 typed data from backend
    const prepared = await nadoPrepareLink(nadoWallet.value, nadoSubaccount.value)

    // Step 2: switch MetaMask to the NADO chain, then sign
    const domain = prepared.typed_data.domain as any
    const chainIdHex = '0x' + Number(domain.chainId).toString(16)
    try {
      await (window as any).ethereum.request({
        method: 'wallet_switchEthereumChain',
        params: [{ chainId: chainIdHex }],
      })
    } catch (switchErr: any) {
      // Chain not added yet — add it
      if (switchErr.code === 4902) {
        await (window as any).ethereum.request({
          method: 'wallet_addEthereumChain',
          params: [{
            chainId: chainIdHex,
            chainName: 'Nado',
            nativeCurrency: { name: 'ETH', symbol: 'ETH', decimals: 18 },
            rpcUrls: [Number(domain.chainId) === 57073
              ? 'https://rpc.nado.xyz'
              : 'https://rpc.sepolia.nado.xyz'],
          }],
        })
      } else {
        throw switchErr
      }
    }

    const provider = new BrowserProvider((window as any).ethereum)
    const signer = await provider.getSigner()
    const signature = await signer.signTypedData(
      domain,
      { LinkSigner: (prepared.typed_data.types as any).LinkSigner },
      prepared.typed_data.message as any,
    )

    // Step 3: submit signature to backend
    const result = await nadoSubmitLink(signature)
    if (result.status === 'success') {
      // Persist Nado keys to D1 so they survive container restarts
      try {
        await updateSecretsKeys({
          nado_linked_signer_key: result.trading_key,
          nado_wallet_address: result.wallet_address,
          nado_subaccount_name: result.subaccount_name,
        })
      } catch (e) {
        console.warn('Failed to persist Nado keys to D1:', e)
      }
      nadoSuccess.value = `Bot authorized! Trading address: ${result.trading_address}`
      await refreshNadoStatus()
    } else {
      nadoError.value = `Authorization failed: ${result.status}`
    }
  } catch (e) {
    nadoError.value = e instanceof Error ? e.message : 'Authorization failed'
  } finally {
    nadoAuthorizing.value = false
  }
}

async function refreshNadoStatus() {
  try {
    nadoStatus.value = await nadoLinkStatus()
    if (nadoStatus.value.wallet_address) {
      nadoWallet.value = nadoStatus.value.wallet_address
    }
    if (nadoStatus.value.subaccount_name) {
      nadoSubaccount.value = nadoStatus.value.subaccount_name
    }
  } catch { /* ignore */ }
}

onMounted(async () => {
  try {
    const data = await fetchSecretsKeys()
    masked.value = data.keys ?? {}
    // Pre-fill form with masked values so user sees current state
    for (const f of keyFields) {
      form.value[f.key] = masked.value[f.key] ?? ''
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load keys'
  } finally {
    loading.value = false
  }
  // Load NADO status
  await refreshNadoStatus()
})

async function handleSave() {
  saving.value = true
  error.value = ''
  success.value = ''
  try {
    const result = await updateSecretsKeys(form.value)
    if (result.changed.length > 0) {
      success.value = `Updated: ${result.changed.join(', ')}${result.container_updated ? ' — container reloaded' : ''}`
      // Refresh masked values
      const data = await fetchSecretsKeys()
      masked.value = data.keys ?? {}
      for (const f of keyFields) {
        form.value[f.key] = masked.value[f.key] ?? ''
      }
    } else {
      success.value = 'No changes detected'
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to save keys'
  } finally {
    saving.value = false
  }
}

function fieldsForGroup(group: string) {
  return keyFields.filter(f => f.group === group)
}
</script>

<template>
  <div :class="$style.page">
    <Typography size="text-h5" weight="semibold" font="bricolage">Settings</Typography>

    <div v-if="loading" :class="$style.empty">
      <Typography color="secondary">Loading...</Typography>
    </div>

    <template v-else>
      <form @submit.prevent="handleSave" :class="$style.form">
        <div v-for="group in groups" :key="group" :class="$style.section">
          <div :class="$style.sectionHeader">
            <Typography size="text-md" weight="semibold" color="secondary">{{ group }}</Typography>
            <button type="button" :class="[$style.helpBtn, activeHelp === group ? $style.helpBtnActive : '']" @click="toggleHelp(group)" title="Setup instructions">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5"/><text x="8" y="11.5" text-anchor="middle" fill="currentColor" font-size="10" font-weight="600" font-family="Inter, sans-serif">?</text></svg>
            </button>
          </div>

          <!-- NADO: custom wallet-connect section -->
          <template v-if="group === 'NADO'">
            <div :class="$style.nadoSection">
              <!-- Connection status -->
              <div :class="$style.nadoRow">
                <div :class="$style.nadoLabel">
                  <Typography size="text-sm" color="tertiary">Wallet</Typography>
                </div>
                <div :class="$style.nadoValue">
                  <template v-if="nadoWallet">
                    <Typography size="text-sm" :class="$style.nadoAddr">{{ nadoWallet.slice(0, 6) }}...{{ nadoWallet.slice(-4) }}</Typography>
                    <span :class="$style.nadoBadge">Connected</span>
                  </template>
                  <button v-else type="button" :class="$style.nadoBtn" :disabled="nadoConnecting" @click="connectNadoWallet">
                    <Typography size="text-sm" weight="semibold">
                      {{ nadoConnecting ? 'Connecting...' : 'Connect Wallet' }}
                    </Typography>
                  </button>
                </div>
              </div>

              <!-- Subaccount name -->
              <div :class="$style.field">
                <label :class="$style.label">
                  <Typography size="text-sm" color="tertiary">Subaccount Name</Typography>
                </label>
                <input
                  v-model="nadoSubaccount"
                  type="text"
                  placeholder="default"
                  :class="$style.input"
                  autocomplete="off"
                  spellcheck="false"
                />
              </div>

              <!-- Authorize button -->
              <div :class="$style.nadoRow">
                <div :class="$style.nadoLabel">
                  <Typography size="text-sm" color="tertiary">Authorization</Typography>
                </div>
                <div :class="$style.nadoValue">
                  <template v-if="nadoStatus?.has_trading_key">
                    <span :class="$style.nadoBadgeOk">Authorized</span>
                    <Typography v-if="nadoStatus?.remote_linked_signer" size="text-xs" color="tertiary" :class="$style.nadoAddr">
                      Signer: {{ nadoStatus.remote_linked_signer.slice(0, 10) }}...
                    </Typography>
                    <button type="button" :class="$style.nadoBtnSmall" :disabled="nadoAuthorizing || !nadoWallet" @click="authorizeNadoBot">
                      <Typography size="text-xs" weight="semibold">
                        {{ nadoAuthorizing ? 'Signing...' : 'Re-authorize' }}
                      </Typography>
                    </button>
                  </template>
                  <button v-else type="button" :class="$style.nadoBtn" :disabled="nadoAuthorizing || !nadoWallet" @click="authorizeNadoBot">
                    <Typography size="text-sm" weight="semibold">
                      {{ nadoAuthorizing ? 'Signing...' : 'Authorize Bot' }}
                    </Typography>
                  </button>
                </div>
              </div>

              <!-- NADO errors / success -->
              <div v-if="nadoError" :class="$style.error">
                <Typography size="text-sm" color="error">{{ nadoError }}</Typography>
              </div>
              <div v-if="nadoSuccess" :class="$style.success">
                <Typography size="text-sm" color="success">{{ nadoSuccess }}</Typography>
              </div>
            </div>
          </template>

          <!-- Standard key fields for other exchanges -->
          <div v-else :class="$style.fields">
            <div v-for="field in fieldsForGroup(group)" :key="field.key" :class="$style.field">
              <label :class="$style.label">
                <Typography size="text-sm" color="tertiary">{{ field.label }}</Typography>
              </label>
              <input
                v-model="form[field.key]"
                type="text"
                :placeholder="field.label"
                :class="$style.input"
                autocomplete="off"
                spellcheck="false"
              />
            </div>
          </div>
        </div>

        <div v-if="error" :class="$style.error">
          <Typography size="text-sm" color="error">{{ error }}</Typography>
        </div>

        <div v-if="success" :class="$style.success">
          <Typography size="text-sm" color="success">{{ success }}</Typography>
        </div>

        <div :class="$style.actions">
          <button type="submit" :class="$style.saveBtn" :disabled="saving">
            <Typography size="text-sm" weight="semibold">
              {{ saving ? 'Saving...' : 'Save Keys' }}
            </Typography>
          </button>
          <Typography size="text-xs" color="tertiary">
            Keys are encrypted with AES-256-GCM and stored securely. Only changed fields are updated.
          </Typography>
        </div>
      </form>
    </template>

    <!-- Help panel (slides in from the right) -->
    <Transition name="slide">
      <div v-if="activeHelp && helpContent[activeHelp]" :class="$style.helpPanel">
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
                  {{ step.text }}<strong>{{ step.bold }}</strong>{{ (step as any).text2 || '' }}
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
  padding: 50px 40px;
  max-width: 720px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-5);
}

.fields {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
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
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  font-family: 'SF Mono', 'Fira Code', monospace;
  outline: none;
  transition: border-color 0.15s;
}

.input::placeholder {
  color: var(--color-text-tertiary);
  font-family: Inter, system-ui, sans-serif;
}

.input:focus {
  border-color: var(--color-text-secondary);
}

.actions {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.saveBtn {
  padding: var(--space-2) var(--space-6);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  cursor: pointer;
  transition: all 0.15s;
}

.saveBtn:hover:not(:disabled) {
  background: var(--color-white-2);
  border-color: var(--color-text-tertiary);
}

.saveBtn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.error {
  padding: var(--space-3) var(--space-4);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-md);
}

.success {
  padding: var(--space-3) var(--space-4);
  background: var(--color-success-bg, rgba(34, 197, 94, 0.08));
  border: 1px solid var(--color-success-stroke, rgba(34, 197, 94, 0.2));
  border-radius: var(--radius-md);
}

.empty {
  padding: var(--space-10) 0;
  text-align: center;
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
  background: var(--color-white-4);
}

.helpPanel {
  position: fixed;
  top: 50px;
  right: 0;
  bottom: 0;
  width: 340px;
  z-index: 100;
  padding: var(--space-6);
  overflow-y: auto;
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

.nadoSection {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.nadoRow {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.nadoLabel {
  width: 100px;
  flex-shrink: 0;
}

.nadoValue {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex: 1;
}

.nadoAddr {
  font-family: 'SF Mono', 'Fira Code', monospace;
}

.nadoBadge {
  font-size: var(--text-xs);
  padding: 2px 8px;
  border-radius: var(--radius-md);
  background: rgba(96, 165, 250, 0.12);
  color: #60a5fa;
  font-weight: 600;
}

.nadoBadgeOk {
  font-size: var(--text-xs);
  padding: 2px 8px;
  border-radius: var(--radius-md);
  background: rgba(34, 197, 94, 0.12);
  color: #22c55e;
  font-weight: 600;
}

.nadoBtn {
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  cursor: pointer;
  transition: all 0.15s;
}

.nadoBtn:hover:not(:disabled) {
  background: var(--color-white-2);
  border-color: var(--color-text-tertiary);
}

.nadoBtn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.nadoBtnSmall {
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: transparent;
  cursor: pointer;
  transition: all 0.15s;
  margin-left: var(--space-2);
}

.nadoBtnSmall:hover:not(:disabled) {
  background: var(--color-white-4);
  border-color: var(--color-text-tertiary);
}

.nadoBtnSmall:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
