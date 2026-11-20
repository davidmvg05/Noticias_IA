# 📰 Radar de Notícias IA

Sistema de curadoria diária de notícias movido por **Google Gemini (`gemini-3.5-flash` com fallback automático)**, **Firebase Firestore** (com separação IAM de menor privilégio), **GitHub Actions** (geração *on-demand* ao abrir num dia novo) e **Streamlit Community Cloud**.

---

## ✨ Funcionalidades Principais

- **50 Notícias/Dia (10 por categoria)** de fontes verificadas de **Portugal, Estados Unidos e Reino Unido** *(zero fontes brasileiras)*:
  1. **Economia & Mercados** — macroeconomia, mercados e cobertura dedicada de **ETFs US & UK** (S&P 500, Nasdaq 100, Biotecnologia, Defesa/Armas, Energia Renovável, UCITS).
  2. **Inteligência Artificial** — investigação, LLMs, hardware e regulação.
  3. **Automações** — agentes autónomos, workflows (Make, Zapier, n8n), Hacker News e engenharia.
  4. **Marketing Digital** — SEO, AI Overviews, publicidade paga e estratégia digital.
  5. **Notícias Felizes** — avanços científicos, saúde, ambiente e soluções construtivas.
- **Geração On-Demand ao Abrir num Dia Novo**:
  - Ao aceder à dashboard num novo dia (`Europe/Lisbon`), verifica automaticamente se a edição do dia já existe no Firestore.
  - Se ainda não existir, dispara o workflow do **GitHub Actions** via `GITHUB_TOKEN`, evita disparos duplicados por sessão/concorrência, apresenta o progresso em tempo real e atualiza o painel automaticamente quando conclui.
  - Acessos seguintes no mesmo dia carregam de forma instantânea a partir do Firestore.
- **Agente Híbrido de Curadoria**:
  - Recolha paralela de feeds RSS/Atom + extração segura de `og:image` com proteção contra SSRF e DNS rebinding.
  - Desduplicação semântica e fallback com **Google Search Grounding** (restrito a domínios aprovados).
- **Idioma Original & Tradução Instantânea por Cartão**:
  - Notícias de jornais portugueses de Portugal permanecem em português.
  - Notícias de fontes internacionais (US/UK) são apresentadas no inglês original com botão **`Traduzir (PT)`** / **`Ver Original (EN)`** para alternar instantaneamente para **Português de Portugal (PT-PT)**.
- **Briefing "Resumo do Dia"**: síntese executiva gerada por IA no topo do painel com os 5 destaques do dia.
- **Histórico por Data + Limpeza Anual**: seletor de edições na barra lateral alimentado por `meta/indice`, botão rápido **Recarregar Edições** e limpeza automática em janeiro (elimina registos de anos anteriores).
- **Visual & Segurança**:
  - Grelha de **2 colunas** com tema vermelho claro adaptado a **Light Mode (`#F05252`)** e **Dark Mode (`#F87171`)**.
  - Ecrã **Acesso Restrito** protegido por **PIN** (`st.secrets["MEU_PIN"]`) com comparação em tempo constante (`hmac.compare_digest`).
