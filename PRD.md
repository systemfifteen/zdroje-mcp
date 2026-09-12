# PRD: Zdroje MCP

Pracovný názov: **zdroje-mcp** (názov domény a repa sa môže zmeniť)
Verzia dokumentu: 0.3 (12. 9. 2026, M0–M3 implementované lokálne, čaká sa na nasadenie)
Autori: fifteen + Claude

---

## 1. Zhrnutie

Vlastný MCP server na serveri **kocka**, ktorý dá Claudovi (Claude.ai, Claude Code, prípadne Hermes) trvalý a štruktúrovaný prístup k lokálnym banskobystrickým médiám, k Denníku N (s vlastným predplatným) a k dokumentom Mestského zastupiteľstva Banská Bystrica (zápisnice, uznesenia, hlasovania, materiály). Cieľ: pri rozboroch komunálnej politiky už nelepiť fakty z Google snippetov, ale mať k dispozícii celé články a primárne zdroje s dátumom a odkazom.

## 2. Problém

Pri doterajších rozboroch (Skubín, Molitorisove odchody, klubová príslušnosť Sobotu, voľby 2026) narážame na tri diery:

1. **Paywall.** Denník N má najlepšie regionálne texty, ale Claude sa k nim bez cookie nedostane. Cesta cez Claude in Chrome funguje, ale po jednom článku a len keď beží Chrome.
2. **Slabá indexácia primárnych zdrojov.** Zápisnice a hlasovania MsZ sú PDF v portáli CG eGOV (`egov.banskabystrica.sk`), ktorý je ASP.NET WebForms s postbackmi a opaknými linkami na súbory. Google ich indexuje mizerne, fulltext nad nimi neexistuje.
3. **Roztrieštené hľadanie.** Každé médium má iné vyhľadávanie (WP REST API, HTML, RSS), Claude ich musí skúšať jedno po druhom a polovica zlyhá na bot-ochrane.

## 3. Používateľ a použitie

Jediný používateľ: fifteen. Klienti: Claude.ai (custom connector), Claude Code na Windows, voliteľne Hermes na kocke.

Typické scenáre:

- „Čo písali lokálne médiá o predaji domu v Skubíne?“ → jedno volanie `search`, výsledky z 3–5 zdrojov s dátumami, potom `fetch` na dva najrelevantnejšie články.
- „Ako hlasoval poslanec X o bode Y na MsZ v júni 2026?“ → `msz_search` nájde hlasovanie aj zápisnicu, `msz_document` vráti relevantné strany.
- „Daj mi prehľad, čo sa dialo v BB za posledný týždeň.“ → `search` s `since` cez všetky zdroje (v2: `recent`).
- Overenie tvrdenia z článku proti primárnemu zdroju (uznesenie, materiál).

## 4. Rozsah

### V1 (MVP)

- MCP server (Python, FastMCP, transport Streamable HTTP) v Dockeri, nasadený cez Coolify na kocke, vystavený cez Traefik s TLS na subdoméne `*.system15.win`.
- Nástroje `list_sources`, `search`, `fetch`, `msz_search`, `msz_document`, `msz_sessions`.
- Adaptéry pre: BBonline, Bystricoviny, Bystricak, Denník N (vyhľadávanie + fetch s cookie), MsZ eGOV.
- Nočný indexer MsZ dokumentov do SQLite + FTS5.
- Autentizácia tajným tokenom.
- Cache a rate-limit voči zdrojom.

### V2 (po MVP)

- SME / MY Bystrica (bot-ochrana, pozri riziká).
- Nástroj `recent(source, days)` a prípadne denný digest.
- OCR pre skenované PDF (ocrmypdf / tesseract).
- Ďalšie zdroje: banskabystrica.sk (tlačové správy), Úradná tabuľa v eGOV, VZN, zmluvy/faktúry (eGOV má open data exporty XML/JSON).
- OAuth pre Claude.ai connector namiesto tokenu v URL.
- Sémantické vyhľadávanie (embeddingy) nad MsZ korpusom, ak FTS5 nebude stačiť.

### Mimo rozsahu

- Verejný prístup alebo viac používateľov.
- Redistribúcia obsahu (archív článkov pre iných, RSS proxy a podobne).
- Obchádzanie paywallu inak než vlastným predplatným.

## 5. Zdroje: stav overený 12. 9. 2026

Všetko testované z kocky (Debian 13, verejná IP), nie z desktopu.

| Zdroj | Vyhľadávanie | Celý článok | Poznámka |
|---|---|---|---|
| **BBonline** (bbonline.sk) | WP REST API `/wp-json/wp/v2/posts?search=` → 200, JSON s `title`, `date`, `content.rendered`, `link` | v odpovedi API | Najjednoduchší zdroj. Má aj archív videí zo zasadnutí MsZ (tveso). |
| **Bystricoviny** (bystricoviny.sk) | WP REST API → **401** (vypnuté). Funguje RSS `/feed/?s=` (20 položiek) aj HTML `?s=` | HTML parser článku | robots.txt: `Allow: /`. Má sitemap.xml (použiteľné na backfill). |
| **Bystricak** (www.bystricak.sk) | HTML `?s=` → 200, 11 článkov; RSS `/feed/?s=` 404; WP REST 404 | HTML parser | Nie je WordPress (žiadne wp-content markery). Treba napísať selektory ručne. |
| **Denník N** (dennikn.sk) | HTML `?s=` → 200; RSS `/feed/?s=` → 200; regionálny feed `/tema/banska-bystrica/feed/` → 200 (10 položiek); WP REST → 401 | Paywall, potrebná session cookie predplatného | VOP (`/vseobecne-obchodne-podmienky/`): prihlasovacie údaje sú neprenosné (I/4.2), blokovanie pri používaní viacerými osobami (I/3.6). **Žiadna klauzula o automatizovanom prístupe ani scrapingu.** Jeden používateľ na vlastnom serveri je v rámci pravidiel. |
| **SME / MY Bystrica** (mybystrica.sme.sk) | HTML aj `/search` → **403** zo servera (bot-ochrana). RSS `/rss` → 200 | 403 | Odložené do V2. Možnosti: RSS ako jediný vstup, browser-like klient (curl_cffi / Playwright), alebo fallback cez Claude in Chrome. |
| **MsZ eGOV** (egov.banskabystrica.sk) | Grid zasadnutí: stĺpce Dátum / Pozvánka / Zápisnica / Uznesenia / Iné; filter roka je `<select>` + ASP.NET postback (`__VIEWSTATE`, `__EVENTVALIDATION`) | Súbory cez `FileOutputHttpHandler.ashx?arguments=<opakný token>`; `title` atribút ikony nesie názov, typ a dátum zverejnenia (napr. „HLASOVANIE MSZ BB 8.9.2026.PDF“, „DOPLNENÝ MATERIÁL MSZ 8.9.2026.ZIP“) | Roky 2018–2026, v 2026 zatiaľ 9 zasadnutí. Iba MsZ; komisie a MsR sú v tom istom gride cez druhý `<select>` (orgán). |

## 6. Funkčné požiadavky: MCP nástroje

Všetky odpovede sú JSON-serializovateľné, texty v Markdowne, dátumy ISO 8601, URL vždy absolútne. Každý výsledok nesie `source` (id zo registra) a `url`, aby Claude vedel citovať.

### 6.1 `list_sources()`

Vráti register zdrojov: `id`, `name`, `kind` (`news` | `council`), `capabilities` (`search`, `fetch`, `paywalled`), `status` (posledný úspešný request, posledná chyba). Slúži aj ako health-check.

### 6.2 `search(query, sources=None, since=None, until=None, limit=10)`

- Spustí adaptéry paralelne (asyncio, timeout 8 s na zdroj), zlúči výsledky, dedupe podľa kanonického URL.
- Výstup: zoznam `{source, title, url, published_at, snippet, author?, paywalled}` zoradený podľa dátumu zostupne.
- Chyba jedného zdroja nezhodí výsledok, objaví sa v poli `errors: [{source, message}]`.
- Ak `sources` obsahuje `msz`, interne volá `msz_search`.

### 6.3 `fetch(url, max_chars=20000)`

- Podľa domény vyberie adaptér; pre Denník N priloží cookie zo secretu.
- Výstup: `{source, title, url, published_at, author, text_markdown, paywalled, truncated, fetched_at}`.
- Extrakcia: trafilatura ako default, per-zdroj selektory kde treba. Odstrániť navigáciu, reklamy, „čítajte tiež“.
- Ak Denník N vráti paywall stránku napriek cookie (expirovaná), vráti chybu `cookie_expired` s návodom, čo obnoviť.

### 6.4 `msz_search(query, year=None, doc_type=None, limit=10)`

- FTS5 dotaz (slovenčina: unicode61 tokenizer s `remove_diacritics 2`, prefix matching).
- `doc_type` ∈ `zapisnica | uznesenia | hlasovanie | material | pozvanka | ine`.
- Výstup: `{doc_id, session_date, doc_type, doc_name, page, snippet, source_url}`; snippet s `<b>` zvýraznením z FTS5.

### 6.5 `msz_document(doc_id, page_from=None, page_to=None, max_chars=20000)`

- Vráti text dokumentu po stranách; bez rozsahu vráti prvé `max_chars` a `total_pages`.
- Pre ZIP materiály: `doc_id` ukazuje na konkrétny súbor vo vnútri (indexer rozbalí).

### 6.6 `msz_sessions(year=None, body="msz")`

- Zoznam zasadnutí s dátumom, číslom a zoznamom dostupných dokumentov (typ, názov, `doc_id`, indexované áno/nie).

## 7. Nefunkčné požiadavky

**Autentizácia.** Claude.ai custom connector dnes umožňuje zadať URL a voliteľne OAuth, nie vlastné hlavičky. Preto V1: tajný segment v ceste `https://<host>/mcp/<token>` (token ≥ 32 náhodných bajtov), plus akceptovaná hlavička `Authorization: Bearer <token>` pre Claude Code a Hermes. Všetko ostatné vráti 404 bez detailov. Traefik logy tokenu: vypnúť access log pre tento router alebo maskovať cestu.

**Slušnosť voči zdrojom.** Max 1 request/s na hostiteľa, vlastný User-Agent s kontaktom, rešpektovať robots.txt tam, kde nejde o vlastné predplatné. Cache: výsledky `search` 1 h, články 24 h, MsZ dokumenty trvalo (sú verejné). Paywallovaný text Denníka N sa necachuje dlhšie než 24 h a nikdy neopúšťa server inak než ako odpoveď na volanie nástroja.

**Citácie.** Server nič nevynucuje, ale Claude pri paywallovanom obsahu cituje krátko (do 15 slov) a odkazuje na URL. Toto je pravidlo použitia, nie kódu.

**Cookie Denníka N.** Uložená len ako Coolify secret `DENNIKN_COOKIE`; do servera ju vkladá používateľ sám, Claude ju nikdy nevidí ani neprepisuje. Server hlási expiráciu cez `list_sources` aj cez chybu `cookie_expired`. Očakávaná údržba: obnova raz za niekoľko týždňov až mesiacov.

**Spoľahlivosť.** Server je bezstavový okrem SQLite; reštart nič nestratí. Indexer je idempotentný (kľúč dokumentu = dátum zasadnutia + názov súboru, lebo `arguments` token sa môže meniť).

**Pozorovateľnosť.** Štruktúrovaný log (JSON) na stdout → Coolify. Počítadlá: volania per nástroj, chyby per zdroj, trvanie. Voliteľne `/healthz` pre Coolify health-check.

**Výkon.** `search` cez všetky zdroje do 15 s (paralelne; limit určuje Bystricoviny, ktorých WordPress vyhľadávanie trvá ~8 s na ich strane, výsledky sa cachujú 1 h), `fetch` do 5 s, `msz_search` do 200 ms.

## 8. Architektúra

```
Claude.ai / Claude Code / Hermes
        │  HTTPS (Streamable HTTP MCP)
        ▼
Traefik (Coolify proxy, TLS)  →  zdroje.system15.win
        │
        ▼
┌──────────────────────────── Docker (Coolify app) ────────────────────────────┐
│  FastMCP server (Python 3.13, uvicorn)                                       │
│   ├─ tools: list_sources, search, fetch, msz_*                               │
│   ├─ registry: sources.yaml → adaptéry                                       │
│   │    ├─ WordPressApiAdapter   (bbonline)                                   │
│   │    ├─ WordPressRssAdapter   (bystricoviny, dennikn search)               │
│   │    ├─ HtmlSearchAdapter     (bystricak; selektory v yaml)                │
│   │    ├─ DennikNFetch          (cookie, paywall detekcia)                   │
│   │    └─ EgovAdapter           (postbacky, parsing gridu, download)         │
│   ├─ cache: SQLite tabuľka http_cache (url, body, fetched_at, ttl)           │
│   └─ msz index: SQLite + FTS5 (documents, pages, pages_fts)                  │
│                                                                              │
│  indexer (cron v kontajneri, 03:00): eGOV → PDF/ZIP → pdftotext → FTS5      │
│  volume: /data (zdroje.sqlite, msz/raw/*.pdf)                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Voľby a dôvody**

- *FastMCP + Streamable HTTP*: najkratšia cesta k remote MCP, ktorý Claude.ai aj Claude Code priamo podporujú. Transport SSE je deprecated, nepoužívať.
- *Jeden kontajner, cron vo vnútri (supercronic)*: jednoduchšie než dve Coolify služby; indexer beží v tom istom image, aby zdieľal kód adaptéra.
- *SQLite*: objem MsZ korpusu je rádovo stovky MB textu za 8 rokov, FTS5 to zvláda v milisekundách. Žiadny ďalší DB kontajner.
- *pdftotext (poppler)*: už je na kocke, v image sa doinštaluje. `-layout` pre tabuľky hlasovaní.
- *Registry v YAML*: pridanie WordPress zdroja = 5 riadkov bez kódu; HTML zdroj = yaml + CSS selektory.

## 9. Dátový model (SQLite)

```sql
sessions(id, body, year, number, date, egov_row_id)
documents(id, session_id, doc_type, name, published_at, egov_arguments, sha256, pages, indexed_at, parent_zip_id)
pages(id, document_id, page_no, text)
pages_fts  -- FTS5 nad pages.text, content='pages', tokenize='unicode61 remove_diacritics 2'
http_cache(url PRIMARY KEY, status, body, fetched_at, ttl_seconds)
source_status(source_id PRIMARY KEY, last_ok_at, last_error, last_error_at)
```

`sources.yaml` (ukážka):

```yaml
- id: bbonline
  name: BBonline
  kind: news
  adapter: wordpress_api
  base_url: https://bbonline.sk
- id: bystricoviny
  name: Bystricoviny
  kind: news
  adapter: wordpress_rss
  base_url: https://www.bystricoviny.sk
  article:
    selectors: {title: "h1.entry-title", body: ".entry-content", date: "time[datetime]"}
- id: dennikn
  name: Denník N
  kind: news
  adapter: wordpress_rss
  base_url: https://dennikn.sk
  paywalled: true
  cookie_env: DENNIKN_COOKIE
  extra_feeds: ["/tema/banska-bystrica/feed/"]
- id: bystricak
  name: Bystricak
  kind: news
  adapter: html_search
  base_url: https://www.bystricak.sk
  search: {path: "/?s={q}", item: "article", title: "h2 a", date: "time"}
- id: msz
  name: MsZ Banská Bystrica (eGOV)
  kind: council
  adapter: egov
  base_url: https://egov.banskabystrica.sk
  nav_state: "261:0:"
```

## 10. Nasadenie a prevádzka

- Repo: `zdroje-mcp` (git, GitHub public). Coolify app typu Dockerfile, auto-deploy z `main`.
- Doména: `zdroje.system15.win` (DNS hotové; Traefik certifikát vybaví Coolify ako pri ostatných aplikáciách).
- Env/secrets v Coolify: `MCP_TOKEN`, `DENNIKN_COOKIE`, `USER_AGENT_CONTACT`, `TZ=Europe/Bratislava`.
- Volume: `/data` (SQLite + surové PDF). Záloha: týždenný `sqlite3 .backup` do existujúceho WebDAV/Obsidian priestoru alebo rsync; surové PDF sa dajú znova stiahnuť, nie sú kritické.
- Prvý beh indexera: backfill 2018–2026 ručne (`python -m zdroje.indexer --all-years`), potom cron len aktuálny a minulý rok.
- Klienti:
  - Claude.ai → Settings → Connectors → Add custom connector → URL s tokenom.
  - Claude Code → `claude mcp add --transport http zdroje https://zdroje.system15.win/mcp --header "Authorization: Bearer …"`.
  - Hermes → konfigurácia MCP servera na rovnaké URL.

## 11. Riziká a otvorené otázky

| Riziko | Dopad | Zmiernenie |
|---|---|---|
| eGOV zmení štruktúru gridu alebo formát `arguments` | indexer prestane nachádzať nové dokumenty | kľúč dokumentu podľa názvu + dátumu, alert cez `source_status`, parser v jednom module s testami na uložených HTML fixtures |
| Cookie Denníka N expiruje alebo Denník N zavedie device-binding | `fetch` vracia paywall | jasná chyba `cookie_expired`, manuálna obnova; fallback Claude in Chrome stále funguje |
| Denník N vyhodnotí prístup ako „viac osôb“ (I/3.6) | blok účtu | jediný klient, nízka frekvencia, žiadny backfill Denníka N, len on-demand `fetch` |
| SME 403 zo serverovej IP | zdroj chýba v V1 | RSS ako čiastočný vstup, V2 browser-like klient; ak aj to padne, ostáva Chrome |
| Skenované PDF bez textovej vrstvy (staršie zápisnice, prílohy materiálov) | FTS ich nevidí; v roku 2026 je to 13 z ~450 súborov, väčšinou prílohy (stanoviská, situácie) | indexer označí `no_text_layer` a zaloguje; OCR v V2. Zápisnice vo formáte DOCX indexer číta (pseudo-strany po 3 000 znakoch); XLS/XLSX prílohy sú mimo rozsahu |
| Token v URL unikne (logy, history) | cudzí prístup k tvojmu predplatnému cez `fetch` | token rotovateľný cez Coolify secret; Traefik access log pre router vypnutý; V2 OAuth |
| Bystricak zmení šablónu | search pre ten zdroj padne | selektory v yaml, test fixture, chyba izolovaná v `errors[]` |

Rozhodnuté 12. 9. 2026:

1. Doména `zdroje.system15.win`, DNS záznam už existuje.
2. Backfill MsZ od roku 2018 (eGOV staršie roky neponúka, select má 2018–2026).
3. Hermes je klientom už vo V1, rovnaký prístup ako Claude Code (Bearer token).
4. SME ide do V2.
5. Repo je **public** na GitHube; tokeny a cookie žijú len v Coolify secrets, nikdy v repe. `.gitignore` kryje `.env`, `data/` a fixtures s osobnými dátami.

## 12. Akceptačné kritériá V1

- [ ] `search("Skubín")` z Claude.ai vráti výsledky aspoň zo 4 zdrojov (BBonline, Bystricoviny, Bystricak, Denník N) do 8 s, každý s dátumom a URL.
- [ ] `fetch` článku Denníka N za paywallom vráti celý text (> 2 000 znakov) a `paywalled: true`.
- [ ] `fetch` s expirovanou cookie vráti `cookie_expired`, nie prázdny text.
- [ ] `msz_search("Skubín")` nájde zápisnicu alebo hlasovanie s dátumom zasadnutia a číslom strany; `msz_document` vráti text tej strany.
- [ ] Indexer po nočnom behu zaindexuje nové dokumenty z posledného zasadnutia bez duplikátov (opakovaný beh = 0 nových riadkov).
- [ ] Server beží ako Coolify app s TLS, reštart kontajnera nestratí index.
- [ ] Connector funguje v Claude.ai aj v Claude Code (overené jedným dotazom z každého).
- [ ] Neautorizovaný request (zlý token) dostane 404.

## 13. Míľniky

| # | Míľnik | Obsah | Odhad |
|---|---|---|---|
| M0 | Skeleton | FastMCP server, `list_sources`, Dockerfile, lokálny beh, token auth | hotové 12. 9. |
| M1 | Správy | adaptéry BBonline, Bystricoviny, Bystricak; `search` + `fetch`; cache; testy na fixtures | hotové 12. 9. |
| M2 | Denník N | RSS/HTML search, fetch s cookie, detekcia paywallu, `cookie_expired` | hotové 12. 9. (cookie ešte neoverená) |
| M3 | MsZ | eGOV adaptér (postbacky, roky), download, pdftotext, FTS5, `msz_*` nástroje, indexer + cron, backfill | hotové 12. 9. (rok 2026 lokálne: 8 zasadnutí, 48 súborov, 2 297 strán); backfill 2018+ po nasadení |
| M4 | Nasadenie | Coolify app, doména, secrets, volume, záloha, pripojenie Claude.ai + Claude Code, akceptačné testy | postup v DEPLOY.md, čaká na GitHub repo a Coolify |
| M5 | V2 kandidáti | SME, `recent`, OCR, OAuth | podľa potreby |

Odhady sú pri práci s Claude Code; poradie M1 ↔ M3 sa dá prehodiť, ak je MsZ dôležitejšie než médiá.

## 14. Rozhodnutia zatiaľ prijaté

- Python 3.13 + FastMCP, nie Node. Dôvod: pdftotext/SQLite/trafilatura ekosystém, rovnaký jazyk ako ostatné veci na kocke.
- Docker cez Coolify, nie systemd. Dôvod: tak beží všetko ostatné na kocke, TLS a doména zadarmo.
- Streamable HTTP, nie SSE ani stdio. Dôvod: Claude.ai remote connector to vyžaduje.
- Token v ceste + Bearer, OAuth až V2. Dôvod: Claude.ai UI nemá custom hlavičky, OAuth server je práca navyše bez prínosu pre jedného používateľa.
