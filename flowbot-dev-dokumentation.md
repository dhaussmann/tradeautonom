# FlowBot — Entwickler-Dokumentation

Vollständige technische Referenz für Nachbau im gleichen Stil Analysiert von: `flowbot.pro/dashboard` — Stand: April 2026

---

## 1\. Tech-Stack Übersicht

| Kategorie | Technologie | Details |
| :---- | :---- | :---- |
| **UI-Framework** | Vue 3 | Composition API, `<script setup>` |
| **Routing** | Vue Router 4 | History-Mode |
| **State Management** | Pinia | Multiple Stores |
| **Server State / Fetching** | TanStack Query (Vue Query) | `VUE_QUERY_CLIENT` Provider |
| **Build-Tool** | Vite | Code-Splitting, Hash-basierte Assets |
| **Styling** | CSS Modules \+ CSS Custom Properties | Kein Tailwind |
| **Charts** | Lightweight Charts (TradingView) | `lightweight-charts.production.js` |
| **Web3 / Wallet** | Web3.js \+ Reown AppKit (WalletConnect v2) | `w3m-modal` Web Component |
| **Fonts** | Inter, Bricolage Grotesque (Google Fonts) \+ KHTeka (Reown CDN) |  |
| **Icons** | Eigene SVG-Assets | Hash-benannte `.js`\-Chunks pro Icon |

---

## 2\. Projektstruktur & Konfiguration

### 2.1 Vite-Konfiguration (empfohlen)

// vite.config.ts

import { defineConfig } from 'vite'

import vue from '@vitejs/plugin-vue'

export default defineConfig({

  plugins: \[vue()\],

  css: {

    modules: {

      // Naming-Pattern wie auf FlowBot: \_ComponentName\_hash\_

      generateScopedName: '\_\[local\]\_\[hash:base64:5\]'

    }

  },

  build: {

    rollupOptions: {

      output: {

        manualChunks: (id) \=\> {

          if (id.includes('lightweight-charts')) return 'lightweight-charts.production'

          if (id.includes('node\_modules/@reown') || id.includes('node\_modules/@walletconnect')) return 'walletconnect'

        }

      }

    }

  }

})

### 2.2 Abhängigkeiten (package.json)

{

  "dependencies": {

    "vue": "^3.x",

    "vue-router": "^4.x",

    "pinia": "^2.x",

    "@tanstack/vue-query": "^5.x",

    "lightweight-charts": "^4.x",

    "@reown/appkit": "^1.x",

    "@reown/appkit-adapter-wagmi": "^1.x",

    "web3": "^4.x"

  },

  "devDependencies": {

    "@vitejs/plugin-vue": "^5.x",

    "vite": "^5.x",

    "typescript": "^5.x"

  }

}

---

## 3\. Vue-App Setup

### 3.1 Entry Point (main.ts)

import { createApp } from 'vue'

import { createPinia } from 'pinia'

import { VueQueryPlugin } from '@tanstack/vue-query'

import router from './router'

import App from './App.vue'

import './assets/styles/variables.css'

const app \= createApp(App)

app.use(createPinia())

app.use(router)

app.use(VueQueryPlugin)

app.mount('\#app')

### 3.2 Root App.vue

\<\!-- index.html \--\>

\<div id="app"\>\</div\>

\<w3m-modal\>\</w3m-modal\>

\<\!-- App.vue \--\>

\<template\>

  \<div class="container"\>

    \<header class="header"\>

      \<nav class="nav"\>...\</nav\>

    \</header\>

    \<RouterView /\>

  \</div\>

\</template\>

Die Container-Struktur ist flach:

body

└── \#app

    └── div.container          (display: flex; flex-direction: column)

        ├── header.header      (height: 56px; padding: 0 2.5rem)

        └── \<RouterView\>       (Seiteninhalt)

---

## 4\. Routing

// router/index.ts

import { createRouter, createWebHistory } from 'vue-router'

const routes \= \[

  { path: '/',           name: 'home',      component: () \=\> import('@/views/HomeView.vue') },

  { path: '/dashboard',  name: 'dashboard', component: () \=\> import('@/views/DashboardView.vue') },

  { path: '/stats',      name: 'stats',     component: () \=\> import('@/views/StatsView.vue') },

  { path: '/quant',      name: 'quant',     component: () \=\> import('@/views/QuantView.vue') },

  { path: '/referrals',  name: 'referrals', component: () \=\> import('@/views/ReferralsView.vue') },

  { path: '/admin',      name: 'admin',     component: () \=\> import('@/views/AdminView.vue') },

  { path: '/:pathMatch(.\*)\*', redirect: '/' }

\]

export default createRouter({

  history: createWebHistory(),

  routes,

  scrollBehavior: () \=\> ({ top: 0 })

})

---

## 5\. Pinia Stores

FlowBot nutzt mehrere dedizierte Stores. Hier die beobachtete Struktur:

// stores/bots.ts

import { defineStore } from 'pinia'

export const useBotsStore \= defineStore('bots', {

  state: () \=\> ({

    bots: \[\],

    tempBots: \[\],

    platforms: \[\],

    performance: null,

    performancePeriod: 'all',

    performanceLoading: false,

    loading: false,

    error: null,

    \_pollTimerId: null,

    \_inFlight: false,

  }),

  actions: {

    async fetchBots() { /\* GET /api/dashboard/bots \*/ },

    async fetchPerformance() { /\* GET /api/dashboard/performance?period=all \*/ },

    createTempBots() { /\* ... \*/ },

  }

})

// stores/dn.ts  (Delta-Neutral / Arb-Funding Daten)

export const useDnStore \= defineStore('dn', {

  state: () \=\> ({

    activeBots: \[\],

    sessions: \[\],

    totalVolume: 0,

    totalFunding: 0,

    fundRates: \[\],

    sessionsLoading: false,

    loading: false,

    starting: false,

    stopping: false,

    error: null,

  }),

  actions: {

    async fetchActiveBots() { /\* polling \*/ },

    async fetchSessions() { /\* ... \*/ },

    async fetchFundRates() { /\* GET /api/fundings \*/ },

  }

})

// Weitere Stores: status, markets, bot:{platform}:{id}

---

## 6\. API-Struktur

Die App kommuniziert über eine REST-API auf demselben Origin (/api/...). TanStack Query übernimmt Caching und Polling.

GET /api/dashboard/bots                         → Bot-Liste

GET /api/dashboard/performance?period=all       → Performance-Daten

GET /api/fundings                               → Funding-Raten (wird gepollt)

GET /api/referrals/code                         → Referral-Code

GET /api/{platform}/config/balance?bot\_id={id}  → Bot-Balance

Polling-Pattern mit TanStack Query:

import { useQuery } from '@tanstack/vue-query'

const { data: bots } \= useQuery({

  queryKey: \['bots'\],

  queryFn: () \=\> fetch('/api/dashboard/bots').then(r \=\> r.json()),

  refetchInterval: 5000, // Polling alle 5 Sekunden

})

---

## 7\. Design System — Farben

### 7.1 App-Eigene CSS-Variablen

:root {

  /\* ── Hintergründe ─────────────────────────── \*/

  \--color-bg-primary:              \#0a0a0a;

  \--color-bg-secondary:            \#17181f;

  \--color-bg-transparent-dark-30:  rgba(10, 10, 10, 0.3);

  \--color-white-2:                 rgba(255, 255, 255, 0.02);

  \--color-white-10:                rgba(255, 255, 255, 0.1);

  /\* ── Text ────────────────────────────────── \*/

  \--color-text-primary:   \#ffffff;

  \--color-text-secondary: \#95979e;

  \--color-text-tertiary:  \#4e5055;

  \--color-text-dark:      \#0a0a0a;

  \--color-white:          \#ffffff;

  /\* ── Borders / Divider ───────────────────── \*/

  \--color-stroke-divider: \#23282e;

  \--color-stroke-primary: \#4d4d4d;

  \--color-stroke-white:   \#ffffff;

  /\* ── Semantic ────────────────────────────── \*/

  \--color-success:        \#28a745;

  \--color-success-light:  \#64d17d;

  \--color-success-bg:     rgba(40, 167, 69, 0.1);

  \--color-success-stroke: rgba(40, 167, 69, 0.3);

  \--color-error:          \#dc3545;

  \--color-error-light:    \#ff849c;

  \--color-error-bg:       rgba(220, 53, 69, 0.1);

  \--color-error-stroke:   rgba(220, 53, 69, 0.3);

  \--color-warning:        \#ffb200;

  \--color-warning-bg:     rgba(255, 178, 0, 0.05);

  \--color-warning-stroke: rgba(255, 178, 0, 0.2);

  /\* ── FlowBot Brand ───────────────────────── \*/

  \--color-flowbot-brand:  \#1fd24f;

  \--color-flowbot-bg:     rgba(31, 210, 79, 0.15);

  \--color-flowbot-stroke: rgba(31, 210, 79, 0.3);

}

### 7.2 Plattform-Farben (Muster für jede Integration)

Jede integrierte Plattform hat ein konsistentes Farbset aus 4 Tokens:

:root {

  /\* Muster: \--color-{platform}-{variant} \*/

  /\* Extended (Grün) \*/

  \--color-extended-brand:        \#00bc83;

  \--color-extended-brand-light:  \#11E389;

  \--color-extended-bg:           rgba(0, 188, 131, 0.15);

  \--color-extended-stroke:       rgba(0, 188, 131, 0.3);

  \--color-extended-gradient: radial-gradient(

    41.82% 56.22% at 51.81% 56.38%,

    rgba(0, 188, 131, 0.2) 8.17%,

    rgba(5, 198, 140, 0.2) 52.88%,

    rgba(10, 10, 10, 0\) 100%

  );

  /\* Hyperliquid (Türkis) \*/

  \--color-hyperliquid-brand:     \#98fce4;

  \--color-hyperliquid-bg:        rgba(152, 252, 228, 0.15);

  \--color-hyperliquid-stroke:    rgba(152, 252, 228, 0.3);

  /\* GRVT (Lime) \*/

  \--color-grvt-brand:            \#B0E870;

  \--color-grvt-bg:               rgba(176, 232, 112, 0.15);

  \--color-grvt-stroke:           rgba(176, 232, 112, 0.3);

  /\* Lighter (Blau/Lila) \*/

  \--color-lighter-brand:         \#c1cdfb;

  \--color-lighter-bg:            rgba(193, 205, 251, 0.15);

  \--color-lighter-stroke:        rgba(193, 205, 251, 0.3);

  /\* Variational (Blau) \*/

  \--color-variational-brand:     \#4c9af8;

  \--color-variational-bg:        rgba(76, 154, 248, 0.15);

  \--color-variational-stroke:    rgba(76, 154, 248, 0.3);

  /\* Paradex (Cyan) \*/

  \--color-paradex-brand:         \#5ccddf;

  \--color-paradex-bg:            rgba(92, 205, 223, 0.15);

  \--color-paradex-stroke:        rgba(92, 205, 223, 0.3);

  /\* Pacifica (Hellblau) \*/

  \--color-pacifica-brand:        \#7fd5eb;

  \--color-pacifica-bg:           rgba(127, 213, 235, 0.15);

  \--color-pacifica-stroke:       rgba(127, 213, 235, 0.3);

  /\* NADO (Weiß) \*/

  \--color-nado-brand:            \#ffffff;

  \--color-nado-bg:               rgba(255, 255, 255, 0.1);

  \--color-nado-stroke:           rgba(255, 255, 255, 0.22);

  /\* Dynamisch gesetzt per JS für die aktive Plattform \*/

  \--color-platform-brand:        ;

  \--color-platform-bg:           ;

  \--color-platform-stroke:       ;

  \--color-platform-gradient:     ;

}

---

## 8\. Design System — Typografie

### 8.1 Font-Einbindung

\<\!-- index.html \--\>

\<link rel="preconnect" href="https://fonts.googleapis.com"\>

\<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin\>

\<link href="https://fonts.googleapis.com/css2?family=Inter:wght@100..900\&family=Bricolage+Grotesque:wght@200..800\&display=swap" rel="stylesheet"\>

:root {

  \--font-inter:     "Inter", system-ui, \-apple-system, "Segoe UI", Roboto, Arial, sans-serif;

  \--font-bricolage: "Bricolage Grotesque", system-ui, \-apple-system, "Segoe UI", Roboto, Arial, sans-serif;

}

Inter ist die primäre UI-Schrift für alle Buttons, Labels und Body-Text. Bricolage Grotesque wird für dekorative/Display-Elemente verwendet.

### 8.2 Typography-Skala

:root {

  /\* Schriftgrößen \*/

  \--text-sm:  12px;   \--text-md:  14px;   \--text-lg:  16px;

  \--text-h6:  20px;   \--text-h5:  26px;   \--text-h4:  32px;

  \--text-h3:  38px;   \--text-h2:  44px;   \--text-h1:  50px;

  /\* Zeilenhöhen \*/

  \--lh-sm: 14px;   \--lh-md: 16px;   \--lh-lg: 18px;

  \--lh-h6: 20px;   \--lh-h5: 26px;   \--lh-h4: 32px;

  \--lh-h3: 38px;   \--lh-h2: 44px;   \--lh-h1: 50px;

  /\* Letter-Spacing \*/

  \--ls-sm:  \-0.12px;   \--ls-md: \-0.14px;   \--ls-lg: \-0.16px;

  \--ls-h6:  \-0.60px;   \--ls-h5: \-0.26px;   \--ls-h4: \-0.32px;

  \--ls-h3:  \-0.76px;   \--ls-h2: \-0.88px;   \--ls-h1: \-0.84px;

}

### 8.3 Typography-Komponente

\<\!-- Typography.vue \--\>

\<template\>

  \<component

    :is="as"

    :class="\[

      $style.Typography,

      $style\[\`Typography--${size}\`\],

      $style\[\`Typography--${weight}\`\],

      $style\[\`Typography--${font}\`\],

      $style\[\`Typography--${color}\`\],

    \]"

  \>

    \<slot /\>

  \</component\>

\</template\>

\<script setup lang="ts"\>

defineProps\<{

  as?:     string  // 'span' | 'p' | 'h1' | 'h2' etc. (default: 'span')

  size?:   string  // 'text-sm' | 'text-md' | 'text-lg' | 'text-h1'...'text-h6'

  weight?: string  // 'normal' | 'medium'

  font?:   string  // 'inter' | 'bricolage'

  color?:  string  // 'primary' | 'secondary' | 'tertiary' | 'success' | 'error'

}\>()

\</script\>

Beobachtete CSS-Klassen:

\_Typography\_hash             → Basis

\_Typography--text-md\_hash    → 14px

\_Typography--text-6xl\_hash   → Display-Größe (Tabs-Header)

\_Typography--normal\_hash     → font-weight: 400

\_Typography--inter\_hash      → font-family: Inter

\_Typography--primary\_hash    → color: \#ffffff

\_Typography--secondary\_hash  → color: \#95979e

\_Typography--tertiary\_hash   → color: \#4e5055

---

## 9\. Design System — Abstände & Border-Radius

### 9.1 Spacing-Skala

:root {

  \--space-0:   0px;    \--space-01:  2px;

  \--space-1:   4px;    \--space-2:   8px;

  \--space-3:   12px;   \--space-4:   16px;

  \--space-5:   20px;   \--space-6:   24px;

  \--space-7:   28px;   \--space-8:   32px;

  \--space-9:   36px;   \--space-10:  40px;

  \--space-12:  48px;   \--space-14:  56px;

  \--space-16:  64px;

}

### 9.2 Border-Radius

:root {

  \--radius-sm:    8px;    /\* kleine Elemente, Chips \*/

  \--radius-md:    10px;   /\* Buttons \*/

  \--radius-lg:    12px;   /\* Platform-Cards, History-Button \*/

  \--radius-xl:    16px;   /\* Stats-Cards, Bot-Cards \*/

  \--radius-round: 9999px; /\* Vollrund (Badges, Dots) \*/

}

---

## 10\. Animationen & Übergänge

:root {

  /\* Dauer \*/

  \--duration-sm:  75ms;

  \--duration-md:  125ms;

  \--duration-lg:  200ms;

  \--duration-xl:  400ms;

  /\* Easing \*/

  \--ease-out-1:    cubic-bezier(0.12, 0.04, 0.20, 1.06);

  \--ease-out-2:    cubic-bezier(0.23, 0.09, 0.08, 1.13);

  \--ease-in-1:     cubic-bezier(0.88, \-0.06, 0.80, 0.96);

  \--ease-inout-1:  cubic-bezier(0.88, 0.04, 0.12, 1.06);

  \--ease-inout-2:  cubic-bezier(0.77, 0.09, 0.23, 1.13);

}

### Conic-Gradient Border Animation (aktiver Bot-Card)

@property \--border-angle {

  syntax: '\<angle\>';

  initial-value: 0deg;

  inherits: false;

}

@keyframes border-rotate {

  to { \--border-angle: 360deg; }

}

.arb-bot-container.active::before {

  content: '';

  position: absolute;

  inset: \-1px;

  border-radius: 17px;

  padding: 1px;

  background: conic-gradient(

    from var(--border-angle),

    transparent 0%,

    var(--color-flowbot-brand) 25%,

    transparent 40%,

    transparent 72%,

    var(--color-flowbot-brand) 85%,

    transparent 100%

  );

  animation: border-rotate 3s linear infinite;

  \-webkit-mask: linear-gradient(\#fff 0 0\) content-box,

                linear-gradient(\#fff 0 0);

  mask-composite: exclude;

}

### Pulse-Animation (aktive Bots)

@keyframes pulse {

  0%, 100% { transform: scale(1); opacity: 1; }

  50%       { transform: scale(1.4); opacity: 0.6; }

}

.arb-bot-container.active .pulse {

  animation: pulse 1.2s ease-in-out infinite;

  background: var(--color-flowbot-brand);

}

---

## 11\. Komponentenkatalog

### 11.1 Button

\<button :class="\[

  $style.Button,

  $style\[\`Button--${variant}\`\],  // 'outline' | 'solid' | 'ghost'

  $style\[\`Button--${size}\`\],     // 'sm' | 'md' | 'lg'

  $style\[\`Button--${color}\`\],    // 'success' | 'default' | 'error'

\]"\>

  \<span :class="$style.Button\_\_prefix"\>\<slot name="prefix" /\>\</span\>

  \<span :class="$style.Button\_\_label"\>\<slot /\>\</span\>

  \<span :class="$style.Button\_\_suffix"\>\<slot name="suffix" /\>\</span\>

\</button\>

/\* Outline Success (Wallet-Button im Header) \*/

.Button--outline.Button--success {

  background:     transparent;

  border:         1px solid \#28a745;

  color:          \#28a745;

  border-radius:  10px;

  height:         40px;

  padding:        0 12px;

  font-family:    Inter, sans-serif;

  font-size:      14px;

  font-weight:    500;

  display:        inline-flex;

  align-items:    center;

  gap:            8px;

}

/\* Outline Default (Add Bot Button) \*/

.Button--outline.Button--default {

  border:  1px solid \#4d4d4d;

  color:   \#ffffff;

  height:  40px;

  padding: 0 24px;

}

/\* Solid Success (Start-Button) \*/

.Button--solid.Button--success {

  background:     \#1fd24f;

  border:         none;

  color:          \#0a0a0a;

  border-radius:  10px;

  height:         40px;

  font-weight:    600;

}

### 11.2 Stats Card

\<div class="stats-card"\>

  \<div class="stats-card-icon"\>\<\!-- SVG Icon \--\>\</div\>

  \<div class="stats-card-info"\>

    \<Typography size="text-sm" color="secondary"\>{{ label }}\</Typography\>

    \<Typography size="text-h4" color="primary" weight="medium"\>{{ value }}\</Typography\>

  \</div\>

\</div\>

.stats-card {

  border-radius: 16px;

  border:        1px solid var(--color-stroke-divider);

  background:    var(--color-white-2);

  padding:       1.25rem;

  display:       flex;

  gap:           0.75rem;

  align-items:   start;

  flex:          1 1 0%;

}

.stats-card-icon {

  border-radius:   10px;

  background:      var(--color-white-10);

  width:           36px;

  height:          36px;

  display:         flex;

  align-items:     center;

  justify-content: center;

}

### 11.3 Platform Card (Sidebar)

.platform-card {

  border-radius: 12px;

  border:        2px solid var(--color-stroke-divider);

  background:    var(--color-white-2);

  cursor:        pointer;

  position:      relative;

  height:        64px;

}

.platform-card.expanded {

  background: var(--color-white-10);

  border:     2px solid var(--color-platform-stroke);

}

### 11.4 Bot Container Card

.arb-bot-container {

  border-radius:  16px;

  border:         1px solid var(--color-stroke-divider);

  background:     rgba(255, 255, 255, 0.04);

  padding:        16px;

  display:        flex;

  flex-direction: column;

  gap:            12px;

  position:       relative;

  overflow:       visible;

  transition:     box-shadow 0.45s;

}

.arb-bot-container.connected {

  box-shadow: 0 0 12px rgba(31, 210, 79, 0.1);

}

### 11.5 History Button

.history-btn {

  border-radius: 12px;

  background:    rgba(255, 255, 255, 0.02);

  border:        2px solid \#23282e;

  display:       flex;

  align-items:   center;

  height:        48px;

  padding:       12px 14px;

  cursor:        pointer;

}

### 11.6 Chip / Badge

.Chip {

  display:       inline-flex;

  align-items:   center;

  border-radius: 8px;

  padding:       2px 8px;

}

.Chip--long  { background: rgba(40, 167, 69, 0.15); color: \#28a745; }

.Chip--short { background: rgba(220, 53, 69, 0.15); color: \#dc3545; }

### 11.7 Navigation Tabs

.tabs-header {

  display:         flex;

  align-items:     center;

  justify-content: space-between;

  gap:             2.25rem;

  min-width:       100%;

  margin-bottom:   0.75rem;

}

.tabs-header-item {

  cursor:          pointer;

  opacity:         0.3;

  display:         flex;

  align-items:     center;

  justify-content: center;

  gap:             0.5rem;

  white-space:     nowrap;

  flex-shrink:     0;

  padding:         0.5rem 0;

  height:          40px;

  transition:      opacity 0.2s;

}

.tabs-header-item:not(.active):hover { opacity: 0.5; }

.tabs-header-item.active {

  opacity:       1;

  border-bottom: 1px solid var(--color-text-primary);

}

---

## 12\. Dashboard-Layout

.arb-funding                        (flex-direction: column; gap: 20px)

├── .stats-cards                    (display: flex; gap: 20px)

│   ├── .stats-card                 (flex: 1 1 0%)

│   ├── .stats-card

│   └── .stats-card

└── .arb-funding-layout             (display: flex; gap: 24px)

    ├── .sidebar-col                (width: 260px; position: sticky; top: 50px; z-index: 20\)

    │   ├── .sidebar                (flex-direction: column; gap: 8px)

    │   │   ├── .platform-card.grvt

    │   │   ├── .platform-card.nado

    │   │   └── ...

    │   └── .history-btn

    └── .main-content               (flex: 1; flex-direction: column; gap: 20px)

        ├── \[Bot-Status-Bar\]        (Stopped/Active \+ Controls)

        └── .arb-bots-grid          (flex-direction: column; gap: 20px)

            ├── .arb-bot-container

            └── .arb-bot-container

---

## 13\. Header

.header {

  padding:     0 2.5rem;

  height:      56px;

  position:    relative;

  display:     flex;

  align-items: center;

  transition:  background 0.25s;

}

.header--mobile-open {

  background: var(--color-bg-secondary);

}

.nav {

  display:         flex;

  justify-content: space-between;

  align-items:     center;

  width:           100%;

  padding:         8px 0;

}

.links { display: flex; gap: 2rem; }

.link {

  font-size:  14px;

  color:      var(--color-text-secondary);

  transition: all;

}

.link.active,

.router-link-exact-active { color: var(--color-text-primary); }

---

## 14\. Web3 / Wallet-Integration

// lib/web3.ts

import { createAppKit } from '@reown/appkit'

import { WagmiAdapter } from '@reown/appkit-adapter-wagmi'

const projectId \= 'YOUR\_WALLETCONNECT\_PROJECT\_ID'

const wagmiAdapter \= new WagmiAdapter({

  projectId,

  networks: \[/\* gewünschte Chains \*/\],

})

createAppKit({

  adapters: \[wagmiAdapter\],

  projectId,

  themeMode: 'dark',

  themeVariables: {

    '--w3m-accent':               '\#98FCE4',

    '--w3m-color-mix':            '\#00BC83',

    '--w3m-color-mix-strength':   '20%',

    '--w3m-font-family':          'KHTeka',

    '--w3m-font-size-master':     '10px',

    '--w3m-border-radius-master': '4px',

  },

})

Im HTML:

\<w3m-modal\>\</w3m-modal\>

\<w3m-button /\>

---

## 15\. Lightweight Charts (TradingView)

import { createChart, ColorType } from 'lightweight-charts'

const chart \= createChart(container, {

  layout: {

    background: { type: ColorType.Solid, color: '\#0a0a0a' },

    textColor:  '\#95979e',

  },

  grid: {

    vertLines: { color: '\#23282e' },

    horzLines: { color: '\#23282e' },

  },

  crosshair:       { mode: 1 },

  rightPriceScale: { borderColor: '\#23282e' },

  timeScale:       { borderColor: '\#23282e' },

})

---

## 16\. Globale Body / Reset Styles

\*, \*::before, \*::after { box-sizing: border-box; }

body {

  background-color: var(--color-bg-primary);

  color:            var(--color-text-primary);

  font-family:      var(--font-inter);

  font-size:        16px;

  font-weight:      400;

  margin:           0;

  padding:          0;

  overflow:         hidden auto;

  min-height:       100vh;

}

\#app        { min-height: 100vh; }

.container  { display: flex; flex-direction: column; min-height: 100vh; }

.app-main   { flex: 1; overflow: auto; }

.dashboard  { padding: 50px 40px; }

---

## 17\. Reown AppKit CSS-Tokens

:root {

  \--apkt-colors-black:       \#202020;

  \--apkt-colors-white:       \#FFFFFF;

  \--apkt-fontFamily-regular: KHTeka;

  \--apkt-fontFamily-mono:    KHTekaMono;

  \--apkt-fontWeight-regular: 400;

  \--apkt-fontWeight-medium:  500;

  \--apkt-spacing-1:  4px;   \--apkt-spacing-2:  8px;

  \--apkt-spacing-3:  12px;  \--apkt-spacing-4:  16px;

  \--apkt-spacing-6:  24px;

  \--apkt-durations-sm: 75ms;   \--apkt-durations-md: 125ms;

  \--apkt-durations-lg: 200ms;  \--apkt-durations-xl: 400ms;

}

---

## 18\. Empfohlene Verzeichnisstruktur

src/

├── assets/

│   ├── styles/

│   │   ├── variables.css      ← Alle \--color-\* und \--font-\* Tokens

│   │   ├── reset.css          ← Body/Reset

│   │   └── animations.css     ← @keyframes

│   └── icons/                 ← SVGs als Vue-Komponenten

├── components/

│   ├── ui/

│   │   ├── Button.vue

│   │   ├── Typography.vue

│   │   ├── Chip.vue

│   │   ├── Icon.vue

│   │   ├── Input.vue

│   │   ├── Tooltip.vue

│   │   └── ScrollBox.vue

│   ├── layout/

│   │   ├── AppHeader.vue

│   │   └── AppNav.vue

│   └── dashboard/

│       ├── StatsCard.vue

│       ├── PlatformCard.vue

│       ├── BotCard.vue

│       └── BotStatusBar.vue

├── views/

│   ├── DashboardView.vue

│   ├── StatsView.vue

│   ├── QuantView.vue

│   └── ReferralsView.vue

├── stores/

│   ├── bots.ts

│   ├── markets.ts

│   ├── status.ts

│   └── dn.ts

├── router/

│   └── index.ts

├── lib/

│   ├── web3.ts

│   └── api.ts

└── main.ts

---

**Zusammenfassung:** Vue 3 \+ Vite \+ Pinia \+ TanStack Query. Styling ausschließlich über CSS Modules und CSS Custom Properties (kein Tailwind). Dark Theme mit \#0a0a0a Background, weiße Primärfarbe, plattformspezifische Akzentfarben. Inter als Hauptschrift. Karten mit rgba(255,255,255,0.02) Background und \#23282e Border. Web3 via Reown AppKit (WalletConnect v2).

![][image1]  


[image1]: <data:image/png;base64,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>