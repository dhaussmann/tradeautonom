# **Variational API – Dokumentation: Portfolio Positions View**

Basis-URL: `https://omni.variational.io`  
 Aufgezeichnet: 2026-04-05 · Page: `/portfolio?tab=positions`  
 Wallet: `0x8E1e55cfD6f43b7bD027cd9dC9912bDC17475835`

---

## **REST API Endpoints**

### **1\. `GET /api/positions`**

**Zweck:** Lädt die aktuellen offenen Positionen des verbundenen Wallets.

|  |  |
| ----- | ----- |
| Methode | `GET` |
|  |  |
| Response | `200 OK` · JSON · \~4.7 KB (unkomprimiert) |
| Caching | `DYNAMIC` (kein Cache) |
| Protocol | HTTP/3 |

**Request Headers:**

GET /api/positions HTTP/3  
Host: omni.variational.io  
accept: \*/\*  
content-type: application/json  
vr-connected-address: 0x8E1e55cfD6f43b7bD027cd9dC9912bDC17475835

**Polling-Verhalten:** Der Client löst diesen Call via Svelte-Reaktivität (`e.$$.update`) aus – wahrscheinlich interval-basiertes Polling (kein Push).

---

### **2\. `GET /api/metadata/supported_assets`**

**Zweck:** Lädt die Liste aller unterstützten Assets (Instrument-Definitionen, Symbole, Dezimalstellen etc.). Wird für die Darstellung der Positionsdaten benötigt.

|  |  |
| ----- | ----- |
| Methode | `GET` |
|  |  |
| Response | `200 OK` · JSON · **\~333 KB** (unkomprimiert), 63 KB komprimiert |
| Caching | `public, max-age=60, stale-while-revalidate=1800` |
| Cache-Hit | `HIT` (Cloudflare Edge) |
| Protocol | HTTP/3 |

**Request Headers:**

GET /api/metadata/supported\_assets HTTP/3  
Host: omni.variational.io  
content-type: application/json  
vr-connected-address: 0x8E1e55cfD6f43b7bD027cd9dC9912bDC17475835

Hinweis: Dieser Endpoint ist öffentlich cachebar – kein Wallet-Auth nötig. Die Antwort ist sehr groß (\~333 KB). Client nutzt `If-Modified-Since`\-Header für conditional requests.

---

### **3\. `GET /api/banner`**

**Zweck:** Lädt einen optionalen Informationsbanner (z.B. Wartungshinweise, Ankündigungen).

|  |  |
| ----- | ----- |
| Methode | `GET` |
| Auth | keiner |
| Response | `200 OK` · JSON · 161 Bytes |
| Caching | `public, max-age=30, stale-while-revalidate=1800` |
| Cache-Hit | `HIT` |
| Protocol | HTTP/3 |

---

### **4\. `GET /api/version`**

**Zweck:** Prüft die aktuelle Backend-Version. Wird für automatische Reloads bei Deployments verwendet (Poller im Client).

|  |  |
| ----- | ----- |
| Methode | `GET` |
| Auth | keiner |
| Response | `200 OK` · JSON · 26 Bytes |
| Caching | `public, max-age=30, stale-while-revalidate=1800` |
| Protocol | HTTP/3 |

**Polling-Verhalten:** Läuft in einem `setTimeout`\-Loop (`poll → restartPoller`). Der Client vergleicht offenbar die Version und löst ggf. einen Reload aus.

---

### **5\. `GET /api/loss_refund/company_stats`**

**Zweck:** Lädt Statistiken zum Loss-Refund-Programm von Variational (wird im Portfolio-Header/Stats-Bereich angezeigt).

|  |  |
| ----- | ----- |
| Methode | `GET` |
|  |  |
| Response | `200 OK` · JSON · 277 Bytes |
| Caching | `DYNAMIC` (kein Cache) |
| Protocol | HTTP/3 |

---

### **6\. `GET /api/referrals/rewards_summary`**

**Zweck:** Lädt die Referral-Rewards-Zusammenfassung für den verbundenen User.

|  |  |
| ----- | ----- |
| Methode | `GET` |
|  |  |
| Response | `200 OK` · JSON · 64 Bytes |
| Caching | `DYNAMIC` (kein Cache) |
| Protocol | HTTP/3 |

---

### **7\. `HEAD /api/ping`**

**Zweck:** Connectivity-Check / Latenzprobe gegen den eigenen Backend-Server.

|  |  |
| ----- | ----- |
| Methode | `HEAD` |
| Auth | keiner |
| Response | `204 No Content` |
| Caching | `no-store` |

---

## **WebSocket – Echtzeit-Preise**

### **`wss://omni-ws-server.prod.ap-northeast-1.variational.io/prices`**

**Zweck:** Liefert Live-Pricing-Updates für alle Perpetual-Future-Instrumente (benötigt für P\&L-Berechnung der Positionen).

**Verbindungsaufbau:**

GET wss://omni-ws-server.prod.ap-northeast-1.variational.io/prices  
Upgrade: websocket  
Origin: https://omni.variational.io  
Sec-WebSocket-Version: 13

Kein Auth-Header – der WebSocket ist nicht wallet-authentifiziert.

**Subscribe-Message (Client → Server):**

{  
  "action": "subscribe",  
  "instruments": \[  
    { "underlying": "XAUT", "instrument\_type": "perpetual\_future", "settlement\_asset": "USDC", "funding\_interval\_s": 3600 },  
    { "underlying": "PAXG", "instrument\_type": "perpetual\_future", "settlement\_asset": "USDC", "funding\_interval\_s": 3600 },  
    { "underlying": "LINK", "instrument\_type": "perpetual\_future", "settlement\_asset": "USDC", "funding\_interval\_s": 3600 },  
    { "underlying": "HYPE", "instrument\_type": "perpetual\_future", "settlement\_asset": "USDC", "funding\_interval\_s": 3600 },  
    { "underlying": "BNB",  "instrument\_type": "perpetual\_future", "settlement\_asset": "USDC", "funding\_interval\_s": 3600 }  
  \]  
}

**Channel-Format (Server → Client):**

instrument\_price:{INSTRUMENT\_ID}

Beispiel-Channel: `instrument_price:P-XAUT-USDC-3600`

**Pricing-Message (Server → Client):**

{  
  "channel": "instrument\_price:P-XAUT-USDC-3600",  
  "pricing": {  
    "price": "4629.23",  
    "native\_price": "0.9996",  
    "delta": "1",  
    "gamma": "0",  
    "theta": "0",  
    "vega": "0",  
    "rho": "0",  
    "iv": "0",  
    "underlying\_price": "4630.87",  
    "interest\_rate": "0.0000500000000000000023960868",  
    "timestamp": "2026-04-05T08:55:26.517365Z"  
  }  
}

**Heartbeat-Message:**

{  
  "timestamp": "2026-04-05T08:55:25.865076624Z",  
  "type": "heartbeat"  
}

**Update-Frequenz:** \~1 Sekunde pro Instrument  
 **Heartbeat-Intervall:** \~5 Sekunden

**Instrument-ID Schema:**

P-{UNDERLYING}-{SETTLEMENT}-{FUNDING\_INTERVAL\_SECONDS}

**Beobachtete Instrumente:**

| Instrument-ID | Underlying | Settlement | Funding Interval |
| ----- | ----- | ----- | ----- |
| `P-XAUT-USDC-3600` | Gold Token (XAUT) | USDC | 1h |
| `P-PAXG-USDC-3600` | PAX Gold (PAXG) | USDC | 1h |
| `P-LINK-USDC-3600` | Chainlink (LINK) | USDC | 1h |
| `P-HYPE-USDC-3600` | Hyperliquid (HYPE) | USDC | 1h |
| `P-BNB-USDC-3600` | BNB | USDC | 1h |

---

## **Pricing-Felder – Bedeutung**

| Feld | Typ | Beschreibung |
| ----- | ----- | ----- |
| `price` | string (Decimal) | Mark Price des Perps (für P\&L) |
| `native_price` | string (Decimal) | Preis relativ zum Underlying (≈ 1.0) |
| `delta` | string | Delta (immer `"1"` bei Perps) |
| `gamma` / `theta` / `vega` / `rho` | string | Optionsgriechen (bei Perps `"0"`) |
| `iv` | string | Implizite Volatilität (bei Perps `"0"`) |
| `underlying_price` | string (Decimal) | Spot-Preis des Underlyings |
| `interest_rate` | string (Decimal) | Aktueller Funding Rate |
| `timestamp` | ISO-8601 | Zeitstempel des letzten Price-Updates |

---

## **Request-Reihenfolge beim Page Load**

1\. GET /api/positions              ← Eigene Positionen (auth)  
2\. GET /api/metadata/supported\_assets ← Asset-Definitionen (public, cached)  
3\. GET /api/banner                 ← Banner (public, cached)  
4\. GET /api/version                ← Version-Check (public, cached)  
5\. GET /api/loss\_refund/company\_stats ← Programm-Stats (auth)  
6\. GET /api/referrals/rewards\_summary ← Referral-Daten (auth)  
7\. HEAD /api/ping                  ← Connectivity-Check  
   HEAD google.com/generate\_204    ← Internet-Konnektivitätsprüfung  
8\. WSS /prices                     ← Subscribe auf Instrumente aus Positions

---

## **Infrastruktur-Details**

| Eigenschaft | Wert |
| ----- | ----- |
| CDN | Cloudflare |
| Server-IP | `104.18.22.115` |
| Protocol | HTTP/3 (h3) überall |
| WebSocket Region | `ap-northeast-1` (AWS) |
| Framework | SvelteKit (erkennbar an `_app/immutable/chunks/`) |
| Monitoring | Datadog RUM (`mon.variational.io`) |
| App-Version | `omni-v1.25.2` |

---

---

## **Neu entdeckte Endpoints (aus `/portfolio?tab=rpnl` und anderen Tabs)**

### **8\. `GET /api/transfers`**

**Zweck:** Universeller Paginated-Endpoint für alle Transfer-Typen. Wird auf mehreren Portfolio-Tabs mit unterschiedlichem `type`\-Parameter aufgerufen.

|  |  |
| ----- | ----- |
| Methode | `GET` |
|  |  |
| Response | `200 OK` · JSON |
| Caching | `DYNAMIC` (kein Cache) |
| Protocol | HTTP/3 |

**Query-Parameter:**

| Parameter | Typ | Pflicht | Beschreibung |
| ----- | ----- | ----- | ----- |
| `order_by` | string | ja | Sortierfeld – immer `created_at` |
| `order` | string | ja | Sortierrichtung: `asc` | `desc` |
| `limit` | integer | ja | Seitengröße – immer `20` |
| `offset` | integer | ja | Pagination-Offset (0-basiert) |
| `created_at_gte` | ISO-8601 | ja | Zeitraum-Start (URL-encoded) |
| `created_at_lte` | ISO-8601 | ja | Zeitraum-Ende (URL-encoded) |
| `type` | string | ja | Transfer-Typ (siehe unten) |

**Beobachtete `type`\-Werte:**

| type | Tab | Response-Größe | Beschreibung |
| ----- | ----- | ----- | ----- |
| `realized_pnl` | Realized PnL | \~15.9 KB | Realisierte Gewinne/Verluste |
| `funding` | Funding Payments | \~14.9 KB | Funding-Rate-Zahlungen |
| `loss_refund_deposit`, `loss_refund_referred_deposit`, `referral_reward` | Rewards | \~n/a | Loss-Refund- und Referral-Transfers (kommasepariert) |

**Beispiel-Requests:**

GET /api/transfers?order\_by=created\_at\&order=desc\&limit=20\&offset=0  
  \&created\_at\_gte=2026-03-29T22%3A00%3A00.000Z  
  \&created\_at\_lte=2026-04-05T21%3A59%3A59.999Z  
  \&type=realized\_pnl

GET /api/transfers?order\_by=created\_at\&order=desc\&limit=20\&offset=0  
  \&created\_at\_gte=2026-03-29T22%3A00%3A00.000Z  
  \&created\_at\_lte=2026-04-05T21%3A59%3A59.999Z  
  \&type=funding

GET /api/transfers?limit=20\&offset=0\&order\_by=created\_at\&order=desc  
  \&created\_at\_gte=2026-03-29T22%3A00%3A00.000Z  
  \&created\_at\_lte=2026-04-05T21%3A59%3A59.999Z  
  \&type=loss\_refund\_deposit%2Closs\_refund\_referred\_deposit%2Creferral\_reward

Hinweis: Der Zeitraum entspricht immer der aktuellen ISO-Woche (Montag 22:00 UTC bis Sonntag 21:59:59 UTC – entspricht der deutschen Zeitzone).

---

### **9\. `GET /api/trades`**

**Zweck:** Trade-Historie des Wallets (Tab "Trades").

|  |  |
| ----- | ----- |
| Methode | `GET` |
|  |  |
| Response | `200 OK` · JSON · \~14.1 KB |
| Caching | `DYNAMIC` |

**Query-Parameter:**

| Parameter | Typ | Beschreibung |
| ----- | ----- | ----- |
| `limit` | integer | Seitengröße – `20` |
| `offset` | integer | Pagination-Offset |
| `order_by` | string | Sortierfeld: `created_at` |
| `order` | string | `desc` |
| `created_at_gte` | ISO-8601 | Zeitraum-Start |
| `created_at_lte` | ISO-8601 | Zeitraum-Ende |

**Beispiel-Request:**

GET /api/trades?limit=20\&offset=0\&order\_by=created\_at\&order=desc  
  \&created\_at\_gte=2026-03-29T22%3A00%3A00.000Z  
  \&created\_at\_lte=2026-04-05T21%3A59%3A59.999Z

---

### **10\. `GET /api/orders/v2`**

**Zweck:** Order-Historie (Tab "Orders"). Beachte: versionierter Endpoint `/v2`.

|  |  |
| ----- | ----- |
| Methode | `GET` |
|  |  |
| Response | `200 OK` · JSON · \~16.0 KB |
| Caching | `DYNAMIC` |

**Query-Parameter:** identisch mit `/api/trades` (limit, offset, order\_by, order, created\_at\_gte, created\_at\_lte).

**Beispiel-Request:**

GET /api/orders/v2?limit=20\&offset=0\&order\_by=created\_at\&order=desc  
  \&created\_at\_gte=2026-03-29T22%3A00%3A00.000Z  
  \&created\_at\_lte=2026-04-05T21%3A59%3A59.999Z

---

## **Vollständige Endpoint-Übersicht**

| \# | Methode | Endpoint | Auth | Caching | Tab |
| ----- | ----- | ----- | ----- | ----- | ----- |
| 1 | GET | `/api/positions` | Wallet | DYNAMIC | Positions |
| 2 | GET | `/api/metadata/supported_assets` | Wallet | 60s | Alle |
| 3 | GET | `/api/banner` | – | 30s | Alle |
| 4 | GET | `/api/version` | – | 30s | Alle |
| 5 | GET | `/api/loss_refund/company_stats` | Wallet | DYNAMIC | Alle |
| 6 | GET | `/api/referrals/rewards_summary` | Wallet | DYNAMIC | Alle |
| 7 | HEAD | `/api/ping` | – | no-store | Alle |
| 8 | GET | `/api/transfers?type=realized_pnl` | Wallet | DYNAMIC | Realized PnL |
| 9 | GET | `/api/transfers?type=funding` | Wallet | DYNAMIC | Funding |
| 10 | GET | `/api/transfers?type=loss_refund_...` | Wallet | DYNAMIC | Rewards |
| 11 | GET | `/api/trades` | Wallet | DYNAMIC | Trades |
| 12 | GET | `/api/orders/v2` | Wallet | DYNAMIC | Orders |
| – | WSS | `/prices` | – | Push | Positions |

---

## **Pagination-Schema**

Alle paginierten Endpoints (`/api/transfers`, `/api/trades`, `/api/orders/v2`) verwenden dasselbe Schema:

// Request  
{  
  limit: 20,         // Seitengröße  
  offset: 0,         // Start-Index  
  order\_by: 'created\_at',  
  order: 'desc',  
  created\_at\_gte: '2026-03-29T22:00:00.000Z',  
  created\_at\_lte: '2026-04-05T21:59:59.999Z'  
}

// Für nächste Seite:  
offset \+= limit

## **Zeitraum-Berechnung**

Der Frontend-Client berechnet den Zeitraum als aktuelle ISO-Woche in **Europe/Berlin**:

* `created_at_gte`: Letzter Montag 22:00:00 UTC (= Dienstag 00:00 MEZ)  
* `created_at_lte`: Nächster Montag 21:59:59 UTC (= Montag 23:59 MEZ)

// Beispiel TypeScript-Implementierung  
function getWeekRange(): { gte: string; lte: string } {  
  const now \= new Date();  
  const day \= now.getUTCDay(); // 0=So, 1=Mo, ...  
  const daysToMonday \= day \=== 0 ? 6 : day \- 1;  
    
  const monday \= new Date(now);  
  monday.setUTCDate(now.getUTCDate() \- daysToMonday);  
  monday.setUTCHours(22, 0, 0, 0);  
    
  const nextMonday \= new Date(monday);  
  nextMonday.setUTCDate(monday.getUTCDate() \+ 7);  
  nextMonday.setUTCMilliseconds(-1);  
    
  return {  
    gte: monday.toISOString(),  
    lte: nextMonday.toISOString()  
  };  
}

---

## **Notizen für den Trading Bot**

* **Positions-Polling:** Der Client pollt `/api/positions` reaktiv. Für den Bot empfiehlt sich ein Polling-Intervall von 1–5s oder ein WebSocket-basierter Ansatz wenn verfügbar.  
* **Instrument-Subscription:** Die subscribten Instrumente entsprechen genau den Underlyings der offenen Positionen – der Client leitet die Liste dynamisch aus `/api/positions` ab.  
* **Funding Rate:** Direkt im `interest_rate`\-Feld jedes Pricing-Updates verfügbar, ohne separaten Endpoint.  
* **WebSocket-Reconnect:** Der Client implementiert Auto-Reconnect (`_handleClose → _connect`).

