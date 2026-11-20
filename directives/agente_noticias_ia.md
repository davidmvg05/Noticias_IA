# Diretiva — Agente Diário `Notícias_IA`

## 1. Objetivo
Recolher, curar e sintetizar diariamente **50 notícias verificadas** (10 por categoria) de fontes credíveis de **Portugal (PT), Estados Unidos (US) e Reino Unido (UK)** — sem qualquer fonte brasileira — usando o modelo **`gemini-3.5-flash`** (Google AI Studio), guardando o resultado no **Firebase Firestore** às **10:00 (Europe/Lisbon)** para leitura no dashboard Streamlit.

## 2. Categorias e Fontes Monitorizadas (PT, US & UK)
Definidas em `execution/sources.py`:
- **Economia & Mercados (`economia`)**:
  - **PT**: Expresso, Público, Jornal de Negócios, ECO.
  - **US & UK (Mercados)**: Financial Times, Reuters, CNBC Finance, Yahoo Finance.
  - **US & UK (ETFs — S&P 500, Nasdaq 100, Biotecnologia, Defesa/Armas, Energia Renovável, temáticos/UCITS)**: ETF Trends (VettaFi), ETFdb (VettaFi), ETF.com, ETF Stream (UK), Investing.com ETFs.
- **Inteligência Artificial (`ia`)**:
  - **PT**: Expresso Tecnologia, Público Tecnologia.
  - **US & UK**: MIT Technology Review, TechCrunch AI, The Verge AI, Ars Technica, Wired AI, VentureBeat AI.
- **Automações (`automacoes`)**:
  - **US & UK**: Make Blog, Zapier Blog, n8n Blog, The New Stack, Harvard Business Review, MIT Sloan Tech.
- **Marketing Digital (`marketing_digital`)**:
  - **PT**: Marketeer.
  - **US & UK**: Search Engine Journal, Search Engine Land, Marketing Dive, HubSpot Blog, Marketing Week (UK), Social Media Examiner.
- **Notícias Felizes (`noticias_felizes`)**:
  - **PT**: Público Ciência & Ambiente, Expresso Sociedade & Ciência.
  - **US & UK**: Positive News (UK), Good News Network (US), Reasons to be Cheerful (US), The Guardian — The Upside (UK), BBC Science & Environment (UK), Good Good Good (US).

## 3. Pipeline de Execução (`execution/run_daily_agent.py`)
1. **Verificação horária (`--respect-lisbon-schedule`)**: Em execuções agendadas do GitHub Actions (`0 9 * * *` e `0 10 * * *` UTC), confirma se a hora local em `Europe/Lisbon` é `10:xx`.
2. **Coleta Híbrida (`execution/fetch_candidates.py`)**:
   - Descarrega feeds RSS/Atom em paralelo com `ThreadPoolExecutor`.
   - Limpa parâmetros de tracking (`utm_*`, `ref`, `fbclid`, `gclid`).
   - Valida URLs públicos (`is_safe_http_url`) para proteção SSRF antes de obter `og:image`.
   - Pré-desduplica e intercala fontes para gerar um pool de ~55 candidatos por categoria.
3. **Curadoria IA (`execution/ai_agent.py`)**:
   - Usa `google-genai` com `gemini-2.5-flash` e Structured Outputs (Pydantic).
   - Remove duplicados semânticos e clickbait, escolhendo até **10 notícias** por categoria.
   - Reescreve título e resumo (2–3 frases) estritamente em **Português de Portugal (PT-PT)**.
   - Aciona **Google Search Grounding** (restrito aos domínios aprovados PT/US/UK) caso alguma categoria fique abaixo de 20 itens.
   - Gera o briefing executivo **`resumo_do_dia`** (`manchete`, `sintese`, 5 `pontos_chave`).
4. **Persistência Firestore (`execution/firestore_client.py`)**:
   - `edicoes/{YYYY-MM-DD}`: documento completo da edição.
   - `edicoes/latest`: cópia da edição mais recente para arranque rápido (1 leitura).
   - `meta/indice`: `{ "datas_disponiveis": [...], "ultima_atualizacao": "..." }`.
5. **Limpeza Anual Automática (1 de Janeiro)**:
   - Sempre que corre em janeiro (`month == 1`), elimina em batch todos os documentos de `edicoes` com `ano < ano_atual` (ex.: em `01/01/2027` elimina registos de `2026`, preservando `latest`) e remove essas datas de `meta/indice`.

## 4. Segurança e Segredos
- Nunca fazer commit de chaves API, `.streamlit/secrets.toml` ou ficheiros JSON de conta de serviço.
- GitHub Actions usa `GEMINI_API_KEY` e `FIREBASE_SERVICE_ACCOUNT` via GitHub Secrets.
- Streamlit Cloud usa `MEU_PIN` e `[firebase]` via `st.secrets`.
