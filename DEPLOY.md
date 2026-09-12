# Nasadenie na kocku (Coolify)

Cieľ: `https://zdroje.system15.win/mcp/<MCP_TOKEN>` pre Claude.ai, `.../mcp` + Bearer pre Claude Code a Hermes.

## 1. Tajomstvá (pripraviť dopredu)

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Výstup je `MCP_TOKEN`. Ulož si ho do správcu hesiel; do repa nikdy.

`DENNIKN_COOKIE` (voliteľné, ale bez neho Denník N vracia len úvody):

1. V Chrome sa prihlás na dennikn.sk.
2. DevTools → Network → obnov stránku → klikni na prvý request na `dennikn.sk` → Request Headers → skopíruj celú hodnotu hlavičky `Cookie` (jeden dlhý riadok).
3. Vlož ju do Coolify ako secret (pozri nižšie). Cookie nikdy nevkladaj do chatu ani do súboru v repe.

## 2. Coolify: nová aplikácia

1. **Projects → (projekt) → + New → Public Repository**, URL repa na GitHube, branch `main`.
2. **Build Pack: Dockerfile**. Port **8000**.
3. **Domains**: `https://zdroje.system15.win` (DNS už existuje; certifikát vybaví Traefik).
4. **Environment Variables** (všetky ako *Build Variable = nie*, bežné runtime premenné):

   | Názov | Hodnota |
   |---|---|
   | `MCP_TOKEN` | z kroku 1 |
   | `DENNIKN_COOKIE` | z kroku 1 (voliteľné) |
   | `USER_AGENT_CONTACT` | tvoj e-mail alebo URL (voliteľné, ide do User-Agent) |
   | `TZ` | `Europe/Bratislava` |

5. **Storages → + Add**: Volume mount, path v kontajneri `/data` (názov napr. `zdroje-data`). Bez toho sa index stratí pri redeployi.
6. **Health check**: Dockerfile ho už definuje (`/healthz`), v Coolify nechaj default.
7. **Deploy**. Build trvá 1–2 min (poppler + uv).

## 3. Prvý backfill indexu (jednorazovo)

V Coolify → aplikácia → **Terminal** (alebo `docker exec` na kocke):

```bash
python -m zdroje.indexer --all
```

2018–2026 je zhruba 90 zasadnutí, stovky PDF/ZIP; pri 1 požiadavke za sekundu počítaj s 30–60 minútami. Beží to v kontajneri, môžeš zavrieť terminál (spusti cez `nohup ... &` alebo nechaj otvorené). Potom už nočný cron (03:10) dopĺňa len aktuálny a minulý rok.

## 4. Pripojenie klientov

**Claude.ai** → Settings → Connectors → *Add custom connector*:
`https://zdroje.system15.win/mcp/<MCP_TOKEN>` (bez OAuth).

**Claude Code** (Windows aj kocka):

```bash
claude mcp add --transport http zdroje https://zdroje.system15.win/mcp --header "Authorization: Bearer <MCP_TOKEN>"
```

**Hermes**: rovnaké URL + hlavička `Authorization: Bearer <MCP_TOKEN>` v konfigurácii MCP serverov.

## 5. Overenie

```bash
curl -s https://zdroje.system15.win/healthz
```

Očakávané: `{"ok": true, "service": "zdroje-mcp"}`. Potom v Claude.ai: „zavolaj list_sources“ → má vrátiť 5 zdrojov a štatistiku indexu.

## 6. Údržba

- **Cookie expirovala**: `fetch` vráti chybu `cookie_expired`. Zopakuj krok 1 pre `DENNIKN_COOKIE`, ulož v Coolify, *Restart*.
- **Rotácia tokenu**: zmeň `MCP_TOKEN` v Coolify, *Restart*, aktualizuj connector v Claude.ai a `claude mcp add` u klientov.
- **Záloha**: volume `/data` obsahuje `zdroje.sqlite` (index) a `msz/raw/` (stiahnuté súbory, dajú sa znova stiahnuť). Stačí zálohovať SQLite: `sqlite3 /data/zdroje.sqlite ".backup /data/backup.sqlite"`.
- **Logy**: Coolify → Logs. Indexer loguje každý dokument; server loguje volania nástrojov a chyby zdrojov.
- **Traefik access log**: Coolify proxy má access log štandardne vypnutý; ak ho zapneš, token v ceste sa doň zapíše. Vtedy radšej používaj Bearer variant všade, kde sa dá.
