"""
Lista curada de fontes credíveis e verificadas (100% PT, US e UK — sem fontes brasileiras)
para as 5 categorias do projeto Notícias_IA.
"""

from __future__ import annotations

TARGET_PER_CATEGORY = 10
CANDIDATE_POOL_PER_CATEGORY = 35

CATEGORIES_META: dict[str, dict] = {
    "economia": {
        "label": "Economia & Mercados",
        "editorial_focus": (
            "Macroeconomia (Portugal, Zona Euro, EUA e Reino Unido), mercados financeiros, "
            "bancos centrais (BCE, Fed, BoE), resultados empresariais e forte cobertura de ETFs "
            "(S&P 500, Nasdaq 100, ETFs temáticos e setoriais: Biotecnologia, Defesa e Armamento, "
            "Energia Renovável, Semicondutores, Obrigações e UCITS europeus). "
            "Garantir um equilíbrio saudável entre notícias macro/mercados e notícias de ETFs."
        ),
        "approved_domains": [
            "expresso.pt",
            "publico.pt",
            "jornaldenegocios.pt",
            "eco.sapo.pt",
            "ft.com",
            "reuters.com",
            "cnbc.com",
            "finance.yahoo.com",
            "etftrends.com",
            "etfdb.com",
            "etf.com",
            "etfstream.com",
            "investing.com",
        ],
    },
    "ia": {
        "label": "Inteligência Artificial",
        "editorial_focus": (
            "Modelos de linguagem (LLMs), investigação em IA, regulação europeia e americana, "
            "hardware/chips de IA, aplicações empresariais e impacto tecnológico."
        ),
        "approved_domains": [
            "expresso.pt",
            "publico.pt",
            "technologyreview.com",
            "techcrunch.com",
            "theverge.com",
            "arstechnica.com",
            "wired.com",
            "venturebeat.com",
        ],
    },
    "automacoes": {
        "label": "Automações",
        "editorial_focus": (
            "Automação de processos, workflows no-code/low-code (Make, Zapier, n8n), "
            "agentes autónomos de IA, produtividade operacional, integrações de APIs e DevOps/engineering automation."
        ),
        "approved_domains": [
            "make.com",
            "zapier.com",
            "blog.n8n.io",
            "news.ycombinator.com",
            "thenewstack.io",
            "hbr.org",
            "sloanreview.mit.edu",
        ],
    },
    "marketing_digital": {
        "label": "Marketing Digital",
        "editorial_focus": (
            "SEO, AI Overviews / pesquisa generativa, publicidade paga (Google/Meta Ads), "
            "marketing de conteúdo, redes sociais, CRM, tendências de marcas e estratégia digital."
        ),
        "approved_domains": [
            "marketeer.sapo.pt",
            "searchenginejournal.com",
            "searchengineland.com",
            "marketingdive.com",
            "blog.hubspot.com",
            "marketingweek.com",
            "socialmediaexaminer.com",
        ],
    },
    "noticias_felizes": {
        "label": "Notícias Felizes",
        "editorial_focus": (
            "Avanços científicos positivos, saúde e medicina, conservação ambiental, energia limpa, "
            "iniciativas comunitárias inspiradoras, cultura e soluções construtivas."
        ),
        "approved_domains": [
            "publico.pt",
            "expresso.pt",
            "positive.news",
            "goodnewsnetwork.org",
            "reasonstobecheerful.world",
            "theguardian.com",
            "bbc.co.uk",
            "goodgoodgood.co",
        ],
    },
}

FEEDS_BY_CATEGORY: dict[str, list[dict[str, str]]] = {
    "economia": [
        # Portugal
        {
            "nome": "Expresso",
            "pais": "PT",
            "url": "https://feeds.feedburner.com/expresso-geral",
        },
        {
            "nome": "Público",
            "pais": "PT",
            "url": "https://feeds.feedburner.com/PublicoRSS",
        },
        {
            "nome": "Jornal de Negócios",
            "pais": "PT",
            "url": "https://www.jornaldenegocios.pt/rss",
        },
        {
            "nome": "ECO",
            "pais": "PT",
            "url": "https://eco.sapo.pt/feed/",
        },
        # US & UK — Mercados & Macroeconomia
        {
            "nome": "Financial Times",
            "pais": "UK",
            "url": "https://www.ft.com/markets?format=rss",
        },
        {
            "nome": "Reuters",
            "pais": "UK/US",
            "url": "https://news.google.com/rss/search?q=site:reuters.com+markets+OR+economy&hl=en-US&gl=US&ceid=US:en",
        },
        {
            "nome": "CNBC Finance",
            "pais": "US",
            "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
        },
        {
            "nome": "Yahoo Finance",
            "pais": "US",
            "url": "https://news.google.com/rss/search?q=site:finance.yahoo.com+stocks+OR+markets+OR+ETF&hl=en-US&gl=US&ceid=US:en",
        },
        # US & UK — ETFs (S&P 500, Nasdaq 100, Biotecnologia, Defesa/Armas, Energia Renovável, etc.)
        {
            "nome": "ETF Trends (VettaFi)",
            "pais": "US",
            "url": "https://www.etftrends.com/feed/",
        },
        {
            "nome": "ETFdb (VettaFi)",
            "pais": "US",
            "url": "https://etfdb.com/feed/",
        },
        {
            "nome": "ETF.com",
            "pais": "US",
            "url": (
                "https://news.google.com/rss/search?"
                "q=site:etf.com+ETF+OR+%22S%26P+500%22+OR+Nasdaq+OR+biotech+OR+defense+OR+%22clean+energy%22"
                "&hl=en-US&gl=US&ceid=US:en"
            ),
        },
        {
            "nome": "ETF Stream (UK)",
            "pais": "UK",
            "url": "https://news.google.com/rss/search?q=site:etfstream.com&hl=en-GB&gl=GB&ceid=GB:en",
        },
        {
            "nome": "Investing.com ETFs",
            "pais": "US/UK",
            "url": "https://www.investing.com/rss/news_25.rss",
        },
    ],
    "ia": [
        # Portugal
        {
            "nome": "Expresso Tecnologia",
            "pais": "PT",
            "url": "https://news.google.com/rss/search?q=site:expresso.pt+%22inteligencia+artificial%22+OR+tecnologia&hl=pt-PT&gl=PT&ceid=PT:pt-150",
        },
        {
            "nome": "Público Tecnologia",
            "pais": "PT",
            "url": "https://news.google.com/rss/search?q=site:publico.pt+%22inteligencia+artificial%22+OR+tecnologia&hl=pt-PT&gl=PT&ceid=PT:pt-150",
        },
        # US & UK
        {
            "nome": "MIT Technology Review",
            "pais": "US",
            "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
        },
        {
            "nome": "TechCrunch AI",
            "pais": "US",
            "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        },
        {
            "nome": "The Verge AI",
            "pais": "US",
            "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        },
        {
            "nome": "Ars Technica",
            "pais": "US",
            "url": "https://feeds.arstechnica.com/arstechnica/technology-lab",
        },
        {
            "nome": "Wired AI",
            "pais": "US",
            "url": "https://www.wired.com/feed/tag/ai/latest/rss",
        },
        {
            "nome": "VentureBeat AI",
            "pais": "US",
            "url": "https://venturebeat.com/category/ai/feed/",
        },
    ],
    "automacoes": [
        {
            "nome": "Make Blog",
            "pais": "US/UK",
            "url": "https://news.google.com/rss/search?q=site:make.com/en/blog&hl=en-US&gl=US&ceid=US:en",
        },
        {
            "nome": "Zapier Blog",
            "pais": "US",
            "url": "https://zapier.com/blog/feeds/latest/",
        },
        {
            "nome": "n8n Blog",
            "pais": "US/UK",
            "url": "https://blog.n8n.io/rss/",
        },
        {
            "nome": "Hacker News (Automation/Agents)",
            "pais": "US",
            "url": "https://hnrss.org/newest?q=automation+OR+agent+OR+workflow&points=20",
        },
        {
            "nome": "The New Stack",
            "pais": "US",
            "url": "https://news.google.com/rss/search?q=site:thenewstack.io+automation+OR+agent+OR+workflow&hl=en-US&gl=US&ceid=US:en",
        },
        {
            "nome": "Harvard Business Review",
            "pais": "US",
            "url": "https://news.google.com/rss/search?q=site:hbr.org+automation+OR+%22ai+agents%22+OR+workflow&hl=en-US&gl=US&ceid=US:en",
        },
        {
            "nome": "MIT Sloan Tech",
            "pais": "US",
            "url": "https://news.google.com/rss/search?q=site:sloanreview.mit.edu+automation+OR+AI&hl=en-US&gl=US&ceid=US:en",
        },
    ],
    "marketing_digital": [
        # Portugal
        {
            "nome": "Marketeer",
            "pais": "PT",
            "url": "https://marketeer.sapo.pt/feed/",
        },
        # US & UK
        {
            "nome": "Search Engine Journal",
            "pais": "US",
            "url": "https://www.searchenginejournal.com/feed/",
        },
        {
            "nome": "Search Engine Land",
            "pais": "US",
            "url": "https://news.google.com/rss/search?q=site:searchengineland.com&hl=en-US&gl=US&ceid=US:en",
        },
        {
            "nome": "Marketing Dive",
            "pais": "US",
            "url": "https://www.marketingdive.com/feeds/news/",
        },
        {
            "nome": "HubSpot Blog",
            "pais": "US",
            "url": "https://blog.hubspot.com/marketing/rss.xml",
        },
        {
            "nome": "Marketing Week (UK)",
            "pais": "UK",
            "url": "https://www.marketingweek.com/feed/",
        },
        {
            "nome": "Social Media Examiner",
            "pais": "US",
            "url": "https://www.socialmediaexaminer.com/feed/",
        },
    ],
    "noticias_felizes": [
        # Portugal
        {
            "nome": "Público Ciência & Ambiente",
            "pais": "PT",
            "url": "https://news.google.com/rss/search?q=site:publico.pt+ciencia+OR+ambiente+OR+descoberta&hl=pt-PT&gl=PT&ceid=PT:pt-150",
        },
        {
            "nome": "Expresso Sociedade & Ciência",
            "pais": "PT",
            "url": "https://news.google.com/rss/search?q=site:expresso.pt+ciencia+OR+ambiente+OR+sustentabilidade&hl=pt-PT&gl=PT&ceid=PT:pt-150",
        },
        # US & UK
        {
            "nome": "Positive News (UK)",
            "pais": "UK",
            "url": "https://news.google.com/rss/search?q=site:positive.news&hl=en-GB&gl=GB&ceid=GB:en",
        },
        {
            "nome": "Good News Network",
            "pais": "US",
            "url": "https://www.goodnewsnetwork.org/feed/",
        },
        {
            "nome": "Reasons to be Cheerful",
            "pais": "US",
            "url": "https://reasonstobecheerful.world/feed/",
        },
        {
            "nome": "The Guardian — The Upside",
            "pais": "UK",
            "url": "https://www.theguardian.com/world/series/the-upside/rss",
        },
        {
            "nome": "BBC Science & Environment",
            "pais": "UK",
            "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        },
        {
            "nome": "Good Good Good",
            "pais": "US",
            "url": "https://www.goodgoodgood.co/articles/rss.xml",
        },
    ],
}
