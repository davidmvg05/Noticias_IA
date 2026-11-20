"""
Dashboard Radar de Notícias IA (Streamlit + Firebase Firestore + Gemini 2.5 Flash).
- Identidade visual em vermelho claro vibrante (#F05252 em Light Mode / #F87171 em Dark Mode)
- Autenticação por PIN via st.secrets["MEU_PIN"]
- Leitura otimizada do Firebase Firestore (1 leitura por edição) com seletor de data
- Bloco executivo "Resumo do Dia" gerado por IA
- 5 categorias (10 notícias/tema em PT-PT) em grelha responsiva de 3 colunas
- Widget Google Tradutor no cabeçalho
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import streamlit as st

from execution.firestore_client import (
    fetch_available_dates,
    fetch_edition_from_firestore,
    get_firestore_client,
    list_local_dates,
    load_local_edition,
)
from execution.github_trigger import (
    fetch_latest_workflow_run,
    get_lisbon_today_str,
    trigger_noticias_workflow,
)
from execution.translator import get_article_translation, is_portuguese_source

# Garantir pasta temporária local
BASE_DIR = Path(__file__).resolve().parent
os.makedirs(BASE_DIR / ".tmp", exist_ok=True)

st.set_page_config(
    page_title="Radar de Notícias IA | Dashboard",
    page_icon="🗞️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Imagens temáticas curadas de alta resolução 800px (fallback quando um artigo não tem og:image)
CATEGORY_IMAGES: dict[str, list[str]] = {
    "economia": [
        "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1590283603385-17ffb3a7f29f?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1460925895917-afdab827c52f?w=800&auto=format&fit=crop&q=80",
    ],
    "ia": [
        "https://images.unsplash.com/photo-1677442136019-21780ecad995?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1620712943543-bcc4688e7485?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=800&auto=format&fit=crop&q=80",
    ],
    "automacoes": [
        "https://images.unsplash.com/photo-1518770660439-4636190af475?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1551288049-bebda4e38f71?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1526374965328-7f61d4dc18c5?w=800&auto=format&fit=crop&q=80",
    ],
    "marketing_digital": [
        "https://images.unsplash.com/photo-1533750516457-a7f992034fec?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1557804506-669a67965ba0?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1432888498266-38ffec3eaf0a?w=800&auto=format&fit=crop&q=80",
    ],
    "noticias_felizes": [
        "https://images.unsplash.com/photo-1499209974431-9dddcece7f88?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1464822759023-fed622ff2c3b?w=800&auto=format&fit=crop&q=80",
        "https://images.unsplash.com/photo-1518495973542-4542c06a5843?w=800&auto=format&fit=crop&q=80",
    ],
}

OPCOES_CATEGORIAS = [
    "Todos",
    "Economia & Mercados",
    "Inteligência Artificial",
    "Automações",
    "Marketing Digital",
    "Notícias Felizes",
]

MAPA_CATEGORIAS = {
    "Economia & Mercados": ("economia", "Economia & Mercados"),
    "Inteligência Artificial": ("ia", "Inteligência Artificial"),
    "Automações": ("automacoes", "Automações"),
    "Marketing Digital": ("marketing_digital", "Marketing Digital"),
    "Notícias Felizes": ("noticias_felizes", "Notícias Felizes"),
}


def sanitize_http_url(url: str) -> str:
    """Garante que apenas URLs HTTP/HTTPS válidos são aceites em links e imagens."""
    if not url or not isinstance(url, str):
        return ""
    cleaned = url.strip()
    if "\\" in cleaned or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        return ""
    try:
        parsed = urlparse(cleaned)
        _ = parsed.port
        if (
            parsed.scheme in ("http", "https")
            and parsed.netloc
            and parsed.username is None
            and parsed.password is None
            and "@" not in parsed.netloc
        ):
            return cleaned
    except ValueError:
        pass
    return ""


def get_category_fallback_image(item: dict[str, Any], categoria: str) -> str:
    """Retorna imagem temática determinística de alta resolução para a categoria."""
    imgs = CATEGORY_IMAGES.get(categoria, CATEGORY_IMAGES["economia"])
    seed = str(item.get("id") or item.get("titulo") or categoria).encode("utf-8")
    digest = int(hashlib.sha256(seed).hexdigest(), 16)
    return imgs[digest % len(imgs)]


def get_article_image(item: dict[str, Any], categoria: str) -> str:
    """Retorna imagem real do artigo (validada) ou fallback panorâmico."""
    img = sanitize_http_url(str(item.get("imagem") or ""))
    if img:
        if "100x100" in img:
            return img.replace("100x100", "800x450")
        return img
    return get_category_fallback_image(item, categoria)


def format_date_pt(date_str: str) -> str:
    """Converte YYYY-MM-DD para DD/MM/YYYY para apresentação amigável."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return date_str


# ==============================================================================
# ESTILOS GLOBAIS — PALETA VERMELHO CLARO (LIGHT #F05252 / DARK #F87171)
# ==============================================================================

st.html(
    """
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" />
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

    :root {
        --accent: #F05252;
        --accent-hover: #DC2626;
        --accent-bg: rgba(240, 82, 82, 0.08);
        --accent-bg-hover: rgba(240, 82, 82, 0.16);
        --accent-border: rgba(240, 82, 82, 0.35);
        --muted-text: #64748b;
        --card-bg: rgba(255, 255, 255, 0.65);
        --card-border: rgba(128, 128, 128, 0.18);
    }

    @media (prefers-color-scheme: dark) {
        :root {
            --accent: #F87171;
            --accent-hover: #EF4444;
            --accent-bg: rgba(248, 113, 113, 0.14);
            --accent-bg-hover: rgba(248, 113, 113, 0.24);
            --accent-border: rgba(248, 113, 113, 0.45);
            --muted-text: #94a3b8;
            --card-bg: rgba(255, 255, 255, 0.035);
            --card-border: rgba(128, 128, 128, 0.18);
        }
    }

    [data-theme="dark"],
    .stApp[data-theme="dark"] {
        --accent: #F87171;
        --accent-hover: #EF4444;
        --accent-bg: rgba(248, 113, 113, 0.14);
        --accent-bg-hover: rgba(248, 113, 113, 0.24);
        --accent-border: rgba(248, 113, 113, 0.45);
        --muted-text: #94a3b8;
        --card-bg: rgba(255, 255, 255, 0.035);
        --card-border: rgba(128, 128, 128, 0.18);
    }

    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }

    /* 1. Ocultar âncoras e decoração superior do Streamlit */
    [data-testid="stHeaderAnchor"],
    .stHeadingAnchor,
    a.anchor-link,
    [data-testid="stHeaderActionElements"],
    [data-testid="stDecoration"] {
        display: none !important;
    }

    header[data-testid="stHeader"] {
        background: transparent !important;
        height: 2rem !important;
    }

    /* 2. Contentor principal alinhado com o exemplo */
    .block-container {
        padding-top: 1.2rem !important;
        padding-bottom: 2.5rem !important;
        max-width: 1250px !important;
    }

    /* 3. Cabeçalho Centralizado e Compacto */
    .header-section {
        text-align: center;
        margin-top: 0 !important;
        margin-bottom: 1.5rem !important;
    }

    .main-title {
        font-size: 2.3rem !important;
        font-weight: 800 !important;
        letter-spacing: -0.025em !important;
        margin: 0 0 0.4rem 0 !important;
        color: inherit;
    }

    .main-title-accent {
        color: var(--accent);
    }

    .main-subtitle {
        font-size: 1rem !important;
        color: var(--muted-text) !important;
        margin: 0 !important;
        font-weight: 400;
    }

    /* Bloco Resumo do Dia */
    .briefing-card {
        background: var(--accent-bg);
        border: 1px solid var(--accent-border);
        border-radius: 14px;
        padding: 20px 24px;
        margin: 0 auto 1.8rem auto;
    }

    .briefing-badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: var(--accent);
        color: #ffffff;
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        padding: 4px 11px;
        border-radius: 9999px;
        margin-bottom: 10px;
    }

    .briefing-headline {
        font-size: 1.28rem;
        font-weight: 800;
        margin: 0 0 8px 0;
        line-height: 1.35;
        color: inherit;
    }

    .briefing-synthesis {
        font-size: 0.95rem;
        line-height: 1.6;
        margin: 0 0 14px 0;
        color: inherit;
        opacity: 0.92;
    }

    .briefing-points-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 10px;
    }

    .briefing-point-item {
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 10px;
        padding: 10px 13px;
        font-size: 0.85rem;
        line-height: 1.45;
    }

    .briefing-point-cat {
        font-weight: 700;
        color: var(--accent);
        display: block;
        margin-bottom: 3px;
        font-size: 0.78rem;
        text-transform: uppercase;
        letter-spacing: 0.03em;
    }

    /* 5. Pills de Categorias */
    div[data-testid="stButtonGroup"],
    .stButtonGroup,
    .st-key-category_pills,
    .st-key-category_pills div[data-testid="stButtonGroup"] {
        display: flex !important;
        justify-content: center !important;
        align-items: center !important;
        margin: 0.8rem auto 2.2rem auto !important;
        width: 100% !important;
    }

    div[data-testid="stButtonGroup"] > div,
    .stButtonGroup > div {
        display: flex !important;
        justify-content: center !important;
        gap: 12px !important;
        flex-wrap: wrap !important;
    }

    div[data-testid="stButtonGroup"] button,
    .stButtonGroup button,
    .st-key-category_pills button,
    [data-testid="stButtonGroup"] [data-testid="baseButton-secondary"] {
        border-radius: 9999px !important;
        border: 1.5px solid var(--accent) !important;
        color: var(--accent) !important;
        background-color: var(--accent-bg) !important;
        padding: 8px 22px !important;
        font-size: 0.95rem !important;
        font-weight: 600 !important;
        cursor: pointer !important;
        box-shadow: none !important;
        transform: none !important;
        transition: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease !important;
    }

    div[data-testid="stButtonGroup"] button:hover,
    .stButtonGroup button:hover,
    .st-key-category_pills button:hover {
        background-color: var(--accent-bg-hover) !important;
        border-color: var(--accent-hover) !important;
        color: var(--accent-hover) !important;
        box-shadow: none !important;
        transform: none !important;
    }

    div[data-testid="stButtonGroup"] button[aria-pressed="true"],
    div[data-testid="stButtonGroup"] button[aria-checked="true"],
    div[data-testid="stButtonGroup"] button[data-state="active"],
    div[data-testid="stButtonGroup"] button[data-selected="true"],
    .stButtonGroup button[aria-pressed="true"],
    .st-key-category_pills button[aria-pressed="true"] {
        background-color: var(--accent) !important;
        color: #ffffff !important;
        border: 1.5px solid var(--accent) !important;
        box-shadow: none !important;
        transform: none !important;
    }

    div[data-testid="stButtonGroup"] button[aria-pressed="true"] *,
    div[data-testid="stButtonGroup"] button[aria-checked="true"] * {
        color: #ffffff !important;
    }

    /* 6. Sidebar: Botões, Colapso (Hambúrguer / X) e Cartões */
    [data-testid="stSidebar"] [data-testid="stButton"] button {
        background: transparent !important;
        background-color: transparent !important;
        border: 1.5px solid var(--accent) !important;
        color: inherit !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        box-shadow: none !important;
        transform: none !important;
        transition: background-color 0.15s ease, border-color 0.15s ease !important;
    }

    [data-testid="stSidebar"] [data-testid="stButton"] button:hover {
        background-color: rgba(128, 128, 128, 0.15) !important;
        border-color: var(--accent) !important;
        color: inherit !important;
        box-shadow: none !important;
        transform: none !important;
    }

    [data-testid="stSidebarCollapseButton"] button *,
    [data-testid="stSidebarCollapsedControl"] button * {
        display: none !important;
        visibility: hidden !important;
        font-size: 0 !important;
        width: 0 !important;
        height: 0 !important;
        opacity: 0 !important;
    }

    /* Sidebar FECHADA: ícone hambúrguer sempre visível */
    [data-testid="stSidebarCollapsedControl"] {
        display: block !important;
        visibility: visible !important;
        z-index: 99999 !important;
    }

    [data-testid="stSidebarCollapsedControl"] button {
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        width: 40px !important;
        height: 40px !important;
        border-radius: 8px !important;
        background: rgba(255, 255, 255, 0.07) !important;
        border: 1px solid rgba(128, 128, 128, 0.22) !important;
        box-shadow: none !important;
        transform: none !important;
    }

    [data-testid="stSidebarCollapsedControl"] button::after {
        content: "\\f0c9" !important;
        font-family: "Font Awesome 6 Free" !important;
        font-weight: 900 !important;
        font-size: 1.25rem !important;
        color: inherit !important;
        display: block !important;
    }

    /* Sidebar ABERTA: botão X com hover vermelho */
    [data-testid="stSidebarCollapseButton"] button {
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        width: 38px !important;
        height: 38px !important;
        border-radius: 8px !important;
        border: 1px solid rgba(128, 128, 128, 0.2) !important;
        background: rgba(255, 255, 255, 0.05) !important;
        color: #64748b !important;
        position: relative !important;
        box-shadow: none !important;
        transform: none !important;
        transition: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease !important;
    }

    [data-testid="stSidebarCollapseButton"] button::after {
        content: "✕" !important;
        font-size: 1.25rem !important;
        font-weight: 700 !important;
        color: inherit !important;
        display: block !important;
        line-height: 1 !important;
    }

    [data-testid="stSidebarCollapseButton"] button:hover {
        background: var(--accent-bg) !important;
        border-color: var(--accent-border) !important;
        color: var(--accent) !important;
        box-shadow: none !important;
        transform: none !important;
    }

    [data-testid="stSidebarCollapseButton"] button:hover::after {
        color: var(--accent) !important;
    }

    .sidebar-header-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        margin-bottom: 1.5rem;
        padding-bottom: 0.5rem;
    }

    .sidebar-brand {
        font-size: 1.15rem;
        font-weight: 700;
        color: inherit;
        white-space: nowrap;
    }

    .session-active-badge {
        background: rgba(16, 185, 129, 0.15);
        color: #10b981;
        font-size: 0.75rem;
        font-weight: 600;
        padding: 3px 10px;
        border-radius: 9999px;
        white-space: nowrap;
    }

    .sidebar-stat-card {
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 10px;
        padding: 12px 14px;
        margin-bottom: 12px;
    }

    .sidebar-stat-title {
        font-size: 0.78rem;
        color: var(--muted-text);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        margin-bottom: 4px;
        display: flex;
        align-items: center;
        gap: 6px;
    }

    .sidebar-stat-value {
        font-size: 0.95rem;
        font-weight: 600;
        color: inherit;
        white-space: normal;
        word-break: break-word;
        line-height: 1.4;
    }

    .sidebar-stat-count {
        font-size: 1.5rem;
        font-weight: 800;
        color: var(--accent);
    }

    /* 7. Cartões de Notícias (Grelha 2 Colunas alinhada com o exemplo) */
    div[class*="st-key-card_"] {
        margin-bottom: 24px !important;
    }

    div[class*="st-key-card_"] > div[data-testid="stVerticalBlockBorderWrapper"],
    div[class*="st-key-card_"] [data-testid="stVerticalBlockBorderWrapper"] {
        height: 525px !important;
        background: var(--card-bg) !important;
        border: 1px solid var(--card-border) !important;
        border-radius: 14px !important;
        overflow: hidden !important;
        padding: 0 !important;
        box-shadow: none !important;
        transform: none !important;
        transition: border-color 0.15s ease !important;
    }

    div[class*="st-key-card_"] > div[data-testid="stVerticalBlockBorderWrapper"]:hover,
    div[class*="st-key-card_"] [data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: var(--accent) !important;
    }

    div[class*="st-key-card_"] div[data-testid="stVerticalBlock"] {
        gap: 0 !important;
        height: 100% !important;
        display: flex !important;
        flex-direction: column !important;
        justify-content: space-between !important;
    }

    .card-img-wrapper {
        position: relative !important;
        width: 100% !important;
        height: 215px !important;
        min-height: 215px !important;
        max-height: 215px !important;
        flex-shrink: 0 !important;
        overflow: hidden !important;
        background: #0f172a !important;
    }

    .card-img-top {
        display: block !important;
        width: 100% !important;
        height: 100% !important;
        min-width: 100% !important;
        min-height: 100% !important;
        max-width: 100% !important;
        max-height: 100% !important;
        object-fit: cover !important;
        object-position: center !important;
    }

    .source-badge-floating {
        position: absolute !important;
        top: 12px !important;
        left: 12px !important;
        z-index: 5 !important;
        background: var(--accent) !important;
        color: #ffffff !important;
        font-size: 0.76rem !important;
        font-weight: 700 !important;
        padding: 4px 12px !important;
        border-radius: 9999px !important;
        letter-spacing: 0.02em !important;
        box-shadow: none !important;
        transform: none !important;
    }

    .card-body-content {
        padding: 18px 20px 16px 20px;
        display: flex;
        flex-direction: column;
        flex-grow: 1;
        overflow: hidden;
    }

    .date-text-clean {
        font-size: 0.82rem;
        color: var(--muted-text);
        margin-bottom: 8px;
        font-weight: 500;
        flex-shrink: 0;
    }

    .card-news-title {
        font-size: 1.1rem;
        font-weight: 700;
        line-height: 1.4;
        margin: 0 0 8px 0;
        color: inherit;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
        text-overflow: ellipsis;
        height: 2.8em;
        flex-shrink: 0;
    }

    .card-news-lead {
        font-size: 0.9rem;
        line-height: 1.55;
        color: var(--muted-text);
        margin: 0 0 12px 0;
        display: -webkit-box;
        -webkit-line-clamp: 3;
        -webkit-box-orient: vertical;
        overflow: hidden;
        text-overflow: ellipsis;
        height: 4.65em;
        flex-grow: 1;
    }

    div[class*="st-key-card_"] div[data-testid="stHorizontalBlock"] {
        padding: 0 18px 16px 18px !important;
        margin-top: auto !important;
        align-items: center !important;
        gap: 10px !important;
    }

    /* Botão Traduzir (PT) - Secundário (inativo) */
    div[class*="st-key-btn_tr_"] button,
    div[class*="st-key-card_"] button[kind="secondary"] {
        border-radius: 8px !important;
        border: 1.5px solid var(--accent) !important;
        color: var(--accent) !important;
        background-color: var(--accent-bg) !important;
        font-size: 0.84rem !important;
        font-weight: 600 !important;
        padding: 6px 12px !important;
        box-shadow: none !important;
        transform: none !important;
        transition: all 0.15s ease !important;
        height: 38px !important;
    }

    div[class*="st-key-btn_tr_"] button:hover,
    div[class*="st-key-card_"] button[kind="secondary"]:hover {
        background-color: var(--accent-bg-hover) !important;
        color: var(--accent-hover) !important;
        border-color: var(--accent-hover) !important;
    }

    /* Botão Traduzir - Primário (ativo - Ver Original EN) */
    div[class*="st-key-btn_tr_"] button[kind="primary"],
    div[class*="st-key-card_"] button[kind="primary"] {
        border-radius: 8px !important;
        border: 1.5px solid var(--accent) !important;
        color: #ffffff !important;
        background-color: var(--accent) !important;
        background: var(--accent) !important;
        font-size: 0.84rem !important;
        font-weight: 600 !important;
        padding: 6px 12px !important;
        box-shadow: none !important;
        transform: none !important;
        transition: all 0.15s ease !important;
        height: 38px !important;
    }

    div[class*="st-key-btn_tr_"] button[kind="primary"]:hover,
    div[class*="st-key-card_"] button[kind="primary"]:hover {
        background-color: var(--accent-hover) !important;
        background: var(--accent-hover) !important;
        border-color: var(--accent-hover) !important;
    }

    div[class*="st-key-card_"] [data-testid="stLinkButton"] a {
        border-radius: 8px !important;
        background: var(--accent) !important;
        border: 1.5px solid var(--accent) !important;
        color: #ffffff !important;
        font-size: 0.84rem !important;
        font-weight: 600 !important;
        padding: 6px 14px !important;
        text-decoration: none !important;
        box-shadow: none !important;
        transform: none !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        height: 38px !important;
        white-space: nowrap !important;
        transition: background-color 0.15s ease, border-color 0.15s ease !important;
    }

    div[class*="st-key-card_"] [data-testid="stLinkButton"] a:hover {
        background: var(--accent-hover) !important;
        border-color: var(--accent-hover) !important;
    }

    div[class*="st-key-card_"] [data-testid="stLinkButton"] a * {
        color: #ffffff !important;
    }

    /* 8. Botão Submit e Input do PIN */
    div[data-testid="stFormSubmitButton"] > button,
    div[data-testid="stFormSubmitButton"] button,
    .stFormSubmitButton > button,
    button[kind="primaryFormSubmit"] {
        background-color: var(--accent) !important;
        background: var(--accent) !important;
        border: 1.5px solid var(--accent) !important;
        color: #ffffff !important;
        font-weight: 600 !important;
        box-shadow: none !important;
        transform: none !important;
        transition: background-color 0.15s ease, border-color 0.15s ease !important;
    }

    div[data-testid="stFormSubmitButton"] > button:hover,
    div[data-testid="stFormSubmitButton"] button:hover,
    .stFormSubmitButton > button:hover,
    button[kind="primaryFormSubmit"]:hover {
        background-color: var(--accent-hover) !important;
        background: var(--accent-hover) !important;
        border-color: var(--accent-hover) !important;
        color: #ffffff !important;
        box-shadow: none !important;
        transform: none !important;
    }

    /* Ícone do olho da password sem fundo */
    div[data-testid="stTextInput"] button,
    div[data-testid="stTextInput"] button:hover,
    div[data-testid="stTextInput"] button:focus,
    div[data-testid="stTextInput"] button:active,
    div[data-testid="stTextInput"] [data-baseweb="button"],
    div[data-testid="stTextInput"] [data-baseweb="button"]:hover,
    div[data-testid="stTextInput"] [data-baseweb="button"]:focus,
    button[aria-label*="password" i],
    button[aria-label*="Password"],
    button[aria-label*="Show" i],
    button[aria-label*="Hide" i] {
        background: transparent !important;
        background-color: transparent !important;
        border: none !important;
        outline: none !important;
        box-shadow: none !important;
        color: #64748b !important;
    }

    /* Borda única no container externo do input PIN */
    div[data-testid="stTextInput"] div[data-baseweb="input"] {
        border-radius: 8px !important;
        border: 1.5px solid rgba(128, 128, 128, 0.25) !important;
        background: transparent !important;
        background-color: transparent !important;
        transition: border-color 0.15s ease !important;
        outline: none !important;
        box-shadow: none !important;
    }

    div[data-testid="stTextInput"] div[data-baseweb="input"]:hover,
    div[data-testid="stTextInput"] div[data-baseweb="input"]:focus-within {
        border: 1.5px solid var(--accent) !important;
        border-color: var(--accent) !important;
        outline: none !important;
        box-shadow: none !important;
    }

    div[data-testid="stTextInput"] div[data-baseweb="base-input"],
    div[data-baseweb="base-input"],
    div[data-testid="stTextInput"] div[data-baseweb="base-input"]:hover,
    div[data-testid="stTextInput"] div[data-baseweb="base-input"]:focus,
    div[data-testid="stTextInput"] div[data-baseweb="base-input"]:focus-within,
    div[data-testid="stTextInput"] div[data-baseweb="base-input"]:active,
    div[data-testid="stTextInput"] input,
    div[data-testid="stTextInput"] input:hover,
    div[data-testid="stTextInput"] input:focus,
    div[data-testid="stTextInput"] input:focus-within,
    div[data-testid="stTextInput"] input:active,
    div[data-testid="stTextInput"] input:focus-visible {
        background: transparent !important;
        background-color: transparent !important;
        border: none !important;
        outline: none !important;
        box-shadow: none !important;
    }

    /* Pontos do PIN (placeholder e entrada) alinhados à esquerda como no exemplo azul */
    div[data-testid="stTextInput"] input,
    div[data-testid="stTextInput"] input::placeholder,
    div[data-testid="stTextInput"] input::-webkit-input-placeholder,
    input[type="password"],
    input[type="password"]::placeholder,
    input[type="password"]::-webkit-input-placeholder {
        text-align: left !important;
        text-align-last: left !important;
        letter-spacing: 0.15em !important;
        font-size: 1rem !important;
    }

    [data-testid="InputInstructions"],
    .stInputInstructions,
    div[data-testid="InputInstructions"] {
        display: none !important;
        visibility: hidden !important;
        height: 0 !important;
    }
    </style>
    """
)


# ==============================================================================
# 1. AUTENTICAÇÃO POR PIN (st.secrets["MEU_PIN"])
# ==============================================================================

try:
    pin_correto = str(st.secrets.get("MEU_PIN", "")).strip()
except Exception:
    pin_correto = os.environ.get("MEU_PIN", "").strip()

if not pin_correto:
    st.error("PIN não configurado. Defina `MEU_PIN` em `.streamlit/secrets.toml` ou nas definições Secrets.")
    st.stop()

if "autenticado" not in st.session_state:
    st.session_state["autenticado"] = False

if not st.session_state["autenticado"]:
    st.html(
        """
        <style>
        .stApp [data-testid="stMainBlockContainer"] {
            display: flex !important;
            flex-direction: column !important;
            justify-content: center !important;
            align-items: center !important;
            min-height: 100vh !important;
            padding-top: 0 !important;
            padding-bottom: 0 !important;
        }
        .stApp [data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] {
            flex: 0 1 auto !important;
            height: auto !important;
            min-height: 0 !important;
            width: 100% !important;
            margin-top: auto !important;
            margin-bottom: auto !important;
        }
        .stApp [data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"]:has([data-testid="stHtml"]) {
            display: none !important;
        }
        div[data-testid="stHorizontalBlock"]:has([data-testid="stForm"]) {
            width: 100% !important;
            align-items: center !important;
            justify-content: center !important;
        }
        div[data-testid="stTextInput"] label,
        div[data-testid="stWidgetLabel"],
        label[data-testid="stWidgetLabel"] {
            display: none !important;
            visibility: hidden !important;
            height: 0 !important;
            margin: 0 !important;
            padding: 0 !important;
        }
        div[data-testid="stForm"] {
            border: none !important;
            padding: 0 !important;
            background: transparent !important;
        }
        </style>
        """
    )

    _, col_center, _ = st.columns([1, 1.4, 1])
    with col_center:
        st.markdown(
            """
            <div style="text-align: center; margin-bottom: 22px;">
                <div style="display: flex; justify-content: center; margin-bottom: 14px;">
                    <svg width="60" height="60" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                        <rect x="3" y="11" width="18" height="11" rx="2" ry="2" fill="var(--accent-bg)"></rect>
                        <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
                        <circle cx="12" cy="16" r="1.5" fill="var(--accent)"></circle>
                    </svg>
                </div>
                <h2 style="text-align: center; margin: 0 0 8px 0; font-size: 1.65rem; font-weight: 800; color: inherit;">
                    Acesso Restrito
                </h2>
                <p style="color: #64748b; font-size: 0.95rem; margin: 0;">
                    Introduza o seu PIN de segurança para aceder ao painel.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.form("form_pin_auth", clear_on_submit=False):
            pin_digitado = st.text_input(
                "Introduza o PIN:",
                type="password",
                label_visibility="collapsed",
                placeholder="••••",
            )
            submetido = st.form_submit_button(
                "Desbloquear Painel",
                use_container_width=True,
                type="primary",
            )
            if submetido:
                if hmac.compare_digest(pin_digitado.strip(), pin_correto):
                    st.session_state["autenticado"] = True
                    st.rerun()
                else:
                    st.error("❌ PIN incorreto. Verifique o código e tente novamente.")

    st.stop()


# ==============================================================================
# 2. CARREGAMENTO DE DADOS (FIRESTORE + FALLBACK LOCAL JSON)
# ==============================================================================

def _get_firebase_secrets_dict() -> dict[str, Any] | None:
    try:
        if "firebase" in st.secrets:
            return dict(st.secrets["firebase"])
    except Exception:
        pass
    return None


def _get_github_token() -> str:
    try:
        token = str(st.secrets.get("GITHUB_TOKEN", "")).strip()
        if token:
            return token
    except Exception:
        pass
    return os.environ.get("GITHUB_TOKEN", "").strip()


@st.cache_data(ttl=300, show_spinner=False)
def cached_load_dates() -> tuple[list[str], str, str | None]:
    """Obtém a lista de datas disponíveis (Firestore ou fallback local)."""
    fb_creds = _get_firebase_secrets_dict()
    if fb_creds or os.environ.get("FIREBASE_SERVICE_ACCOUNT"):
        try:
            db = get_firestore_client(fb_creds)
            dates = fetch_available_dates(db)
            return dates, "firestore", None
        except Exception as exc:
            return list_local_dates(BASE_DIR), "local", f"Não foi possível ligar ao Firestore ({exc}). A usar cache local."

    return list_local_dates(BASE_DIR), "local", None


@st.cache_data(ttl=300, show_spinner=False)
def cached_load_edition(date_str: str | None, source_mode: str) -> tuple[dict[str, Any] | None, str | None]:
    """Lê a edição selecionada (1 leitura no Firestore ou ficheiro local)."""
    if source_mode == "firestore":
        try:
            db = get_firestore_client(_get_firebase_secrets_dict())
            return fetch_edition_from_firestore(db, date_str=date_str), None
        except Exception as exc:
            return None, f"Erro ao ler edição do Firestore: {exc}"
    return load_local_edition(date_str=date_str, base_dir=BASE_DIR), None


datas_disponiveis, origem_dados, erro_ligacao_datas = cached_load_dates()
if erro_ligacao_datas:
    st.warning(erro_ligacao_datas)


def _handle_on_demand_generation(available_dates: list[str], source_mode: str) -> None:
    """Se a edição de hoje (Europe/Lisbon) não existir, dispara o workflow on-demand e acompanha o progresso."""
    if source_mode != "firestore":
        return

    hoje_lisboa = get_lisbon_today_str()
    if hoje_lisboa in available_dates:
        return

    done_key = f"workflow_done_{hoje_lisboa}"
    if st.session_state.get(done_key):
        return

    github_token = _get_github_token()
    if not github_token:
        st.info(
            f"A edição de hoje ({format_date_pt(hoje_lisboa)}) ainda não foi gerada. "
            "Defina `GITHUB_TOKEN` nos Secrets para ativar a geração automática on-demand."
        )
        return

    dispatch_key = f"workflow_dispatched_{hoje_lisboa}"
    started_key = f"workflow_started_at_{hoje_lisboa}"

    if not st.session_state.get(dispatch_key):
        active_run, _ = fetch_latest_workflow_run(github_token)
        if active_run and active_run.get("status") in ("queued", "in_progress", "requested", "waiting"):
            st.session_state[dispatch_key] = True
            st.session_state[started_key] = str(active_run.get("created_at") or "")
        else:
            ok, err = trigger_noticias_workflow(github_token)
            if not ok:
                st.error(f"Não foi possível iniciar a geração da edição de hoje: {err}")
                st.session_state[done_key] = True
                return
            st.session_state[dispatch_key] = True
            st.session_state[started_key] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    created_after = st.session_state.get(started_key) or None
    data_pt = format_date_pt(hoje_lisboa)

    with st.status(
        f"A gerar a edição de hoje ({data_pt}) com Inteligência Artificial (~2,5 a 3,5 min)...",
        expanded=True,
    ) as status_box:
        info_placeholder = st.empty()
        progress_bar = st.progress(10)
        info_placeholder.write("Pedido enviado ao GitHub Actions. A aguardar início da recolha e curadoria...")

        max_polls = 36  # ~6 minutos (36 * 10s)
        for attempt in range(max_polls):
            run_info, poll_err = fetch_latest_workflow_run(github_token, created_after=created_after)
            if poll_err:
                info_placeholder.write(f"A aguardar resposta da API do GitHub ({poll_err})...")
            elif run_info:
                run_status = str(run_info.get("status") or "")
                conclusion = str(run_info.get("conclusion") or "")
                if run_status == "completed":
                    if conclusion == "success":
                        progress_bar.progress(100)
                        status_box.update(
                            label=f"Edição de {data_pt} concluída! A atualizar painel...",
                            state="complete",
                        )
                        st.session_state[done_key] = True
                        cached_load_dates.clear()
                        cached_load_edition.clear()
                        st.rerun()
                    else:
                        status_box.update(
                            label=f"A geração terminou com estado '{conclusion}'.",
                            state="error",
                        )
                        st.error("Não foi possível concluir a geração automática. Verifique o GitHub Actions ou tente recarregar.")
                        st.session_state[done_key] = True
                        return
                elif run_status == "in_progress":
                    pct = min(90, 25 + int((attempt / max_polls) * 65))
                    progress_bar.progress(pct)
                    info_placeholder.write("Em execução: a recolher feeds RSS e a curar notícias com Gemini...")
                else:
                    progress_bar.progress(15)
                    info_placeholder.write("Em fila de espera no GitHub Actions...")

            time.sleep(10)

        status_box.update(label="A geração ainda está a decorrer em segundo plano.", state="running")
        st.info("O processamento está a demorar mais do que o habitual. Clique em **Recarregar Edições** na barra lateral dentro de instantes.")


_handle_on_demand_generation(datas_disponiveis, origem_dados)


# ==============================================================================
# 3. BARRA LATERAL (SELETOR DE DATA + ESTATÍSTICAS + CONTROLOS)
# ==============================================================================

data_selecionada: str | None = None

with st.sidebar:
    st.markdown(
        """
        <div class="sidebar-header-row">
            <span class="sidebar-brand">
                <i class="fa-solid fa-bolt" style="color: var(--accent); margin-right: 6px;"></i>
                Notícias IA
            </span>
            <span class="session-active-badge">
                <i class="fa-solid fa-circle" style="font-size: 0.5rem; vertical-align: middle; margin-right: 4px;"></i>
                Ativo
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if datas_disponiveis:
        data_selecionada = st.selectbox(
            "Edição por Data",
            options=datas_disponiveis,
            index=0,
            format_func=lambda d: f"{format_date_pt(d)} (Mais recente)" if d == datas_disponiveis[0] else format_date_pt(d),
            key="edition_date_selector",
        )
    else:
        st.caption("Sem edições registadas ainda.")

    edicao, erro_edicao = cached_load_edition(data_selecionada, origem_dados)
    if erro_edicao:
        st.warning(erro_edicao)

    gerado_str = "Pendente"
    total_noticias = 0
    modelo_ia = "gemini-3.8-flash"
    if edicao:
        gerado_str = str(edicao.get("gerado_em_lisboa") or edicao.get("gerado_em") or "Desconhecido")
        stats = edicao.get("estatisticas", {})
        total_noticias = int(stats.get("total_noticias", 0))
        modelo_ia = str(edicao.get("modelo_ia", "gemini-3.8-flash"))

    fonte_badge = "Firebase Firestore" if origem_dados == "firestore" else "Modo Local (.tmp)"

    st.markdown(
        f"""
        <div class="sidebar-stat-card">
            <div class="sidebar-stat-title"><i class="fa-regular fa-clock" style="color: var(--accent);"></i> Curadoria</div>
            <div class="sidebar-stat-value">{html.escape(gerado_str)}</div>
        </div>
        <div class="sidebar-stat-card">
            <div class="sidebar-stat-title"><i class="fa-solid fa-layer-group" style="color: var(--accent);"></i> Notícias na Edição</div>
            <div class="sidebar-stat-count">{total_noticias}</div>
        </div>
        <div class="sidebar-stat-card">
            <div class="sidebar-stat-title"><i class="fa-solid fa-microchip" style="color: var(--accent);"></i> Motor & Origem</div>
            <div class="sidebar-stat-value">{html.escape(modelo_ia)}<br/><span style="font-size:0.8rem; color: var(--muted-text);">{html.escape(fonte_badge)}</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.write("")
    if st.button("Recarregar Edições", use_container_width=True):
        cached_load_dates.clear()
        cached_load_edition.clear()
        st.rerun()

    st.write("")
    if st.button("Terminar Sessão", use_container_width=True):
        st.session_state["autenticado"] = False
        st.rerun()


# ==============================================================================
# 4. CABEÇALHO & RESUMO DO DIA
# ==============================================================================

data_label = format_date_pt(edicao.get("data", "")) if edicao else "Sem edição"
subtitulo_info = (
    f"Edição de {data_label}"
    if edicao
    else "Aguardando primeira edição curada pelo Agente IA (GitHub Actions / execução local)."
)

st.markdown(
    f"""
    <div class="header-section">
        <h1 class="main-title">Radar de <span class="main-title-accent">Notícias IA</span></h1>
        <p class="main-subtitle">{html.escape(subtitulo_info)}</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if not edicao:
    st.info(
        "Ainda não existe nenhuma edição disponível. Configure o Firebase em `.streamlit/secrets.toml` "
        "ou execute `python execution/run_daily_agent.py --dry-run` para gerar uma edição local de teste."
    )
    st.stop()

# Bloco "Resumo do Dia"
resumo_dia = edicao.get("resumo_do_dia") or {}
if resumo_dia:
    manchete_safe = html.escape(str(resumo_dia.get("manchete", "Resumo do Dia")))
    sintese_safe = html.escape(str(resumo_dia.get("sintese", "")))
    pontos_html_parts = []
    for ponto in resumo_dia.get("pontos_chave", []):
        if isinstance(ponto, dict):
            cat_p = html.escape(str(ponto.get("categoria", "")))
            dest_p = html.escape(str(ponto.get("destaque", "")))
            if dest_p:
                pontos_html_parts.append(
                    f'<div class="briefing-point-item">'
                    f'<span class="briefing-point-cat">{cat_p}</span>'
                    f'{dest_p}</div>'
                )

    pontos_grid_html = (
        f'<div class="briefing-points-grid">{"".join(pontos_html_parts)}</div>'
        if pontos_html_parts
        else ""
    )

    st.markdown(
        f"""
        <div class="briefing-card">
            <span class="briefing-badge"><i class="fa-solid fa-wand-magic-sparkles"></i> Resumo do Dia</span>
            <h2 class="briefing-headline">{manchete_safe}</h2>
            <p class="briefing-synthesis">{sintese_safe}</p>
            {pontos_grid_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


# ==============================================================================
# 5. FILTRO DE CATEGORIAS (PILLS) & GRELHA RESPONSIVA DE 2 COLUNAS
# ==============================================================================

categoria_selecionada = st.pills(
    label="Filtrar por Categoria",
    options=OPCOES_CATEGORIAS,
    default="Todos",
    label_visibility="collapsed",
    key="category_pills",
)
if not categoria_selecionada:
    categoria_selecionada = "Todos"

categorias_dict = edicao.get("categorias") or {}

if categoria_selecionada == "Todos":
    artigos_exibicao: list[dict[str, Any]] = []
    for cat_key, _ in MAPA_CATEGORIAS.values():
        for art in categorias_dict.get(cat_key, []):
            art_copy = dict(art)
            art_copy["_cat_key"] = cat_key
            artigos_exibicao.append(art_copy)
    artigos_exibicao.sort(key=lambda x: str(x.get("data", "")), reverse=True)
else:
    cat_key, _ = MAPA_CATEGORIAS.get(categoria_selecionada, ("economia", "Economia & Mercados"))
    artigos_exibicao = [
        dict(art, _cat_key=cat_key)
        for art in categorias_dict.get(cat_key, [])
    ]


@st.fragment
def render_article_card(item: dict[str, Any], index: int) -> None:
    """Renderiza um cartão de notícia padronizado com tradução instantânea por fragmento."""
    cat_key = str(item.get("_cat_key", "economia"))
    img_url = html.escape(get_article_image(item, cat_key), quote=True)
    fallback_img = html.escape(get_category_fallback_image(item, cat_key), quote=True)

    raw_title = str(item.get("titulo", "Sem título"))
    raw_lead = str(item.get("resumo", "Sem resumo disponível."))
    raw_source = str(item.get("fonte", "Fonte"))
    raw_date = str(item.get("data", ""))
    pais = str(item.get("pais", ""))
    safe_link = sanitize_http_url(str(item.get("link") or item.get("url") or ""))

    card_hash = hashlib.sha256(f"{item.get('id', '')}_{index}_{cat_key}".encode("utf-8")).hexdigest()[:12]
    trans_key = f"trad_{card_hash}"

    is_pt_source = is_portuguese_source(fonte=raw_source, pais=pais)
    if is_pt_source:
        is_en, pt_title, pt_lead = False, raw_title, raw_lead
    else:
        pt_title = str(item.get("titulo_pt") or "")
        pt_lead = str(item.get("resumo_pt") or "")
        if pt_title and pt_lead and (pt_title != raw_title or pt_lead != raw_lead):
            is_en = True
        else:
            is_en, pt_title, pt_lead = get_article_translation(raw_title, raw_lead, fonte=raw_source, pais=pais)

    is_trad = st.session_state.get(trans_key, False)
    title_to_show = pt_title if (is_trad and pt_title) else raw_title
    lead_to_show = pt_lead if (is_trad and pt_lead) else raw_lead

    safe_title = html.escape(title_to_show)
    safe_lead = html.escape(lead_to_show)
    safe_source = html.escape(raw_source)
    safe_date = html.escape(raw_date)

    has_translation = is_en and (pt_title != raw_title or pt_lead != raw_lead)

    with st.container(border=True, key=f"card_{card_hash}"):
        st.markdown(
            f"""
            <div class="card-img-wrapper">
                <span class="source-badge-floating">{safe_source}</span>
                <img src="{img_url}" alt="{safe_title}" class="card-img-top" loading="lazy"
                     referrerpolicy="no-referrer"
                     onerror="this.onerror=null; this.src='{fallback_img}';" />
            </div>
            <div class="card-body-content">
                <div class="date-text-clean">{safe_date}</div>
                <div class="card-news-title">{safe_title}</div>
                <div class="card-news-lead">{safe_lead}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if has_translation:
            col_tr, col_lk = st.columns([1, 1], gap="small")
            with col_tr:
                btn_label = "Ver Original (EN)" if is_trad else "Traduzir (PT)"
                btn_type = "primary" if is_trad else "secondary"
                if st.button(btn_label, key=f"btn_tr_{card_hash}", type=btn_type, use_container_width=True):
                    st.session_state[trans_key] = not is_trad
                    st.rerun(scope="fragment")
            with col_lk:
                if safe_link:
                    st.link_button("Ler Artigo Original", url=safe_link, use_container_width=True)
        else:
            _, col_lk = st.columns([1, 1], gap="small")
            with col_lk:
                if safe_link:
                    st.link_button("Ler Artigo Original", url=safe_link, use_container_width=True)


if not artigos_exibicao:
    st.info("Nenhuma notícia disponível para a categoria selecionada nesta edição.")
else:
    for i in range(0, len(artigos_exibicao), 2):
        col1, col2 = st.columns(2)
        with col1:
            render_article_card(artigos_exibicao[i], i)
        if i + 1 < len(artigos_exibicao):
            with col2:
                render_article_card(artigos_exibicao[i + 1], i + 1)
