# 📰 Radar de Notícias IA

Sistema automatizado de curadoria diária de notícias movido por **Google Gemini (`gemini-3.5-flash` com fallback automático)**, **Firebase Firestore** (com separação IAM de menor privilégio), **GitHub Actions** (execução diária às 10:00 hora de Lisboa) e **Streamlit Community Cloud**.

---

## ✨ Funcionalidades Principais

- **50 Notícias/Dia (10 por categoria)** de fontes verificadas de **Portugal, Estados Unidos e Reino Unido** *(zero fontes brasileiras)*:
  1. **Economia & Mercados** — macroeconomia, mercados e cobertura dedicada de **ETFs US & UK** (S&P 500, Nasdaq 100, Biotecnologia, Defesa/Armas, Energia Renovável, UCITS).
  2. **Inteligência Artificial** — investigação, LLMs, hardware e regulação.
  3. **Automações** — agentes autónomos, workflows (Make, Zapier, n8n), Hacker News e engenharia.
  4. **Marketing Digital** — SEO, AI Overviews, publicidade paga e estratégia digital.
  5. **Notícias Felizes** — avanços científicos, saúde, ambiente e soluções construtivas.
- **Agente Híbrido de Curadoria**:
  - Recolha paralela de feeds RSS/Atom + extração segura de `og:image` com proteção contra SSRF e DNS rebinding.
  - Desduplicação semântica, reescrita em **Português de Portugal (PT-PT)** e fallback com **Google Search Grounding** (restrito a domínios aprovados).
- **Briefing "Resumo do Dia"**: síntese executiva gerada por IA no topo do painel com os 5 destaques do dia.
- **Histórico por Data + Limpeza Anual**: seletor de edições na barra lateral alimentado por `meta/indice` e limpeza automática em janeiro (elimina registos de anos anteriores).
- **Visual & Segurança**:
  - Tema **vermelho claro vibrante** adaptado a **Light Mode (`#F05252`)** e **Dark Mode (`#F87171`)**.
  - Autenticação por **PIN** (`st.secrets["MEU_PIN"]`) com comparação em tempo constante (`hmac.compare_digest`).
  - Widget **Google Tradutor** no cabeçalho para leitura noutros idiomas.