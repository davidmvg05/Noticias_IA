"""
Coleta paralela de candidatos a notícias (RSS/Atom + extração segura de og:image)
para alimentar o agente de curadoria Gemini 2.5 Flash.
"""

from __future__ import annotations

import calendar
import contextlib
import hashlib
import html
import ipaddress
import logging
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests

from execution.sources import CANDIDATE_POOL_PER_CATEGORY, FEEDS_BY_CATEGORY

logger = logging.getLogger("noticias_ia.fetch_candidates")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,en-GB;q=0.8,en-US;q=0.7",
}

FEED_TIMEOUT = 10
OG_IMAGE_TIMEOUT = 4.0
MAX_HTML_BYTES = 262_144  # 256 KB suficientes para ler <head> / og:image
MAX_FEED_BYTES = 2_097_152  # 2 MB máximo para resposta RSS/Atom

_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")

TRACKED_PARAMS = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "ref",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ocid",
    "source",
})


def _is_public_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Garante que o endereço IP é unicast público e não encapsula IPs internos (NAT64/6to4/Teredo)."""
    if (
        not ip_obj.is_global
        or ip_obj.is_multicast
        or ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
        return False
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None and not _is_public_ip(ip_obj.ipv4_mapped):
            return False
        if ip_obj.sixtofour is not None and not _is_public_ip(ip_obj.sixtofour):
            return False
        if ip_obj.teredo is not None:
            server, client = ip_obj.teredo
            if not _is_public_ip(server) or not _is_public_ip(client):
                return False
        if ip_obj in _NAT64_WELL_KNOWN or ip_obj in _NAT64_LOCAL_USE:
            embedded_v4 = ipaddress.IPv4Address(int(ip_obj) & 0xFFFFFFFF)
            if not _is_public_ip(embedded_v4):
                return False
    return True


def _has_unsafe_url_syntax(raw_url: str, parsed) -> bool:
    """Rejeita barras invertidas, whitespace/controlo, userinfo (@) e portas inválidas."""
    cleaned = raw_url.strip()
    if "\\" in cleaned or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        return True
    try:
        _ = parsed.port
    except ValueError:
        return True
    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        return True
    return False


def is_safe_http_url(url: str) -> bool:
    """Valida se o URL é HTTP/HTTPS público (proteção contra SSRF e esquemas inseguros)."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https"):
        return False
    if _has_unsafe_url_syntax(url, parsed):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False

    lowered = hostname.lower()
    if lowered in ("localhost", "127.0.0.1", "::1") or lowered.endswith(".local"):
        return False

    # Se for IP literal, validar diretamente
    try:
        ip_obj = ipaddress.ip_address(hostname)
        return _is_public_ip(ip_obj)
    except ValueError:
        pass

    # Resolver DNS e rejeitar endereços privados, loopback, link-local, multicast ou NAT64
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addr_info = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        if not addr_info:
            return False
        for info in addr_info:
            ip_str = info[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
            if not _is_public_ip(ip_obj):
                return False
    except (OSError, ValueError):
        return False

    return True


def clean_url(raw_url: str) -> str:
    """Remove parâmetros de tracking (utm_*, ref, fbclid, etc.) e valida o esquema."""
    if not raw_url or not isinstance(raw_url, str):
        return ""
    try:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        if _has_unsafe_url_syntax(raw_url, parsed):
            return ""
        query_pairs = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in TRACKED_PARAMS
        ]
        new_query = urlencode(query_pairs)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ""))
    except Exception:
        return ""


def strip_html_and_clean(text: str, max_chars: int = 380) -> str:
    """Remove tags HTML, desescapa entidades e normaliza espaços."""
    if not text:
        return ""
    cleaned = html.unescape(text)
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_chars:
        return cleaned[:max_chars].rsplit(" ", 1)[0] + "..."
    return cleaned


def normalize_title(title: str) -> str:
    """Normaliza o título para comparação léxica."""
    cleaned = re.sub(r"[^\w\s]", "", title.lower())
    return " ".join(cleaned.split())


def compute_token_similarity(title_a: str, title_b: str) -> float:
    """Calcula a similaridade de Jaccard entre tokens dos títulos."""
    tokens_a = set(normalize_title(title_a).split())
    tokens_b = set(normalize_title(title_b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def parse_entry_date(entry: dict) -> tuple[str, float]:
    """Retorna data formatada (YYYY-MM-DD HH:MM) em UTC e timestamp numérico."""
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct:
        try:
            ts = float(calendar.timegm(time_struct))
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M"), ts
        except Exception:
            pass

    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M"), now.timestamp()


def _normalize_image_url(url: str) -> str:
    """Valida e melhora resolução básica de URLs de imagem."""
    if not url:
        return ""
    u = html.unescape(url.strip())
    if not u.startswith(("http://", "https://")):
        return ""
    if "100x100" in u:
        u = u.replace("100x100", "800x450")
    return u


def extract_image_from_entry(entry: dict, raw_content: str = "") -> str:
    """Extrai imagem direta dos metadados RSS ou tags HTML do conteúdo."""
    media_content = entry.get("media_content")
    if isinstance(media_content, list):
        for m in media_content:
            u = _normalize_image_url(m.get("url", ""))
            if u and any(ext in u.lower() for ext in (".jpg", ".jpeg", ".png", ".webp", "format=", "zenfs", "image")):
                return u

    media_thumb = entry.get("media_thumbnail")
    if isinstance(media_thumb, list):
        for t in media_thumb:
            u = _normalize_image_url(t.get("url", ""))
            if u:
                return u

    enclosures = entry.get("enclosures")
    if isinstance(enclosures, list):
        for enc in enclosures:
            href = _normalize_image_url(enc.get("href") or enc.get("url") or "")
            if href and any(ext in href.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")):
                return href

    links = entry.get("links")
    if isinstance(links, list):
        for link_item in links:
            if "image" in str(link_item.get("type", "")).lower():
                u = _normalize_image_url(link_item.get("href", ""))
                if u:
                    return u

    corpus = f"{entry.get('summary') or ''} {entry.get('description') or ''} {raw_content}"
    match = re.search(
        r'<img[^>]+src=["\'](https?://[^"\']+\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
        corpus,
        re.IGNORECASE,
    )
    if match:
        return _normalize_image_url(match.group(1))

    return ""


_SSRF_TLS = threading.local()
_CREATE_CONN_LOCK = threading.Lock()
_ORIG_CREATE_CONNECTION = None


def _ssrf_safe_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, socket_options=None):
    """Wrapper de ligação TCP que valida o IP numa única resolução DNS (anti-DNS-rebinding)."""
    if not getattr(_SSRF_TLS, "active", False):
        return _ORIG_CREATE_CONNECTION(address, timeout=timeout, source_address=source_address, socket_options=socket_options)

    host, port = address
    clean_host = host.strip("[]") if isinstance(host, str) else host
    if not clean_host or str(clean_host).lower() in ("localhost", "127.0.0.1", "::1") or str(clean_host).lower().endswith(".local"):
        raise ValueError(f"Ligação SSRF bloqueada para destino interno: {host}")

    # Resolver uma única vez, validar todos os IPs e ligar diretamente ao IP validado
    err = None
    addr_info = socket.getaddrinfo(clean_host, port, 0, socket.SOCK_STREAM)
    if not addr_info:
        raise OSError(f"Sem resolução DNS para {host}")

    validated_targets = []
    for family, socktype, proto, _canonname, sockaddr in addr_info:
        ip_obj = ipaddress.ip_address(sockaddr[0])
        if not _is_public_ip(ip_obj):
            raise ValueError(f"Ligação SSRF bloqueada para IP não público: {sockaddr[0]}")
        validated_targets.append((family, socktype, proto, sockaddr))

    for family, socktype, proto, sockaddr in validated_targets:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if socket_options:
                for opt in socket_options:
                    sock.setsockopt(*opt)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            err = exc
            if sock is not None:
                sock.close()
    if err is not None:
        raise err
    raise OSError("Falha ao estabelecer ligação segura")


@contextlib.contextmanager
def _pinned_ssrf_connection():
    """Ativa validação/fixação de IP no momento da ligação TCP em urllib3 (thread-safe)."""
    global _ORIG_CREATE_CONNECTION
    try:
        import urllib3.util.connection as urllib3_conn
    except ImportError:
        yield
        return

    with _CREATE_CONN_LOCK:
        if _ORIG_CREATE_CONNECTION is None:
            _ORIG_CREATE_CONNECTION = urllib3_conn.create_connection
            urllib3_conn.create_connection = _ssrf_safe_create_connection

    prev = getattr(_SSRF_TLS, "active", False)
    _SSRF_TLS.active = True
    try:
        yield
    finally:
        _SSRF_TLS.active = prev


def _safe_http_get(
    url: str,
    timeout: float,
    stream: bool = False,
    max_redirects: int = 5,
) -> requests.Response | None:
    """Executa GET validando is_safe_http_url e fixando o IP na ligação TCP (anti-SSRF/DNS-rebinding)."""
    current_url = url
    with _pinned_ssrf_connection():
        for _ in range(max_redirects + 1):
            if not is_safe_http_url(current_url):
                return None
            resp = requests.get(
                current_url,
                headers=HEADERS,
                timeout=timeout,
                stream=stream,
                allow_redirects=False,
            )
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "").strip()
                resp.close()
                if not location:
                    return None
                current_url = urljoin(current_url, location)
                continue
            return resp
    return None


def fetch_og_image(url: str, timeout: float = OG_IMAGE_TIMEOUT) -> str:
    """Obtém a imagem real (og:image / twitter:image) da página original de forma segura."""
    if not is_safe_http_url(url):
        return ""
    # Links redirecionados do Google News raramente expõem og:image sem JS
    if "news.google.com" in urlparse(url).netloc.lower():
        return ""

    final_url = url
    try:
        resp = _safe_http_get(url, timeout=timeout, stream=True)
        if resp is None:
            return ""
        with resp:
            if resp.status_code != 200:
                return ""
            final_url = resp.url or url
            content_type = resp.headers.get("Content-Type", "").lower()
            if "html" not in content_type and "xml" not in content_type:
                return ""
            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=16_384):
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_HTML_BYTES:
                    break
            raw_html = b"".join(chunks).decode("utf-8", errors="ignore")
    except Exception:
        return ""

    patterns = (
        r'<meta[^>]+property=[\'"]og:image[\'"][^>]+content=[\'"]([^\'"]+)[\'"]',
        r'<meta[^>]+content=[\'"]([^\'"]+)[\'"][^>]+property=[\'"]og:image[\'"]',
        r'<meta[^>]+name=[\'"]twitter:image[\'"][^>]+content=[\'"]([^\'"]+)[\'"]',
        r'<meta[^>]+content=[\'"]([^\'"]+)[\'"][^>]+name=[\'"]twitter:image[\'"]',
    )
    for pattern in patterns:
        match = re.search(pattern, raw_html, re.IGNORECASE)
        if match:
            raw_img = html.unescape(match.group(1).strip())
            resolved_img = urljoin(final_url, raw_img)
            img_url = _normalize_image_url(resolved_img)
            if img_url:
                return img_url
    return ""


def fetch_feed_items(feed_info: dict[str, str], max_per_feed: int = 20) -> list[dict]:
    """Descarrega e analisa um feed RSS/Atom individual."""
    feed_url = feed_info["url"]
    fonte_nome = feed_info["nome"]
    pais = feed_info.get("pais", "INT")

    if not is_safe_http_url(feed_url):
        logger.warning("URL bloqueado por segurança: %s (%s)", fonte_nome, feed_url)
        return []

    try:
        response = _safe_http_get(feed_url, timeout=FEED_TIMEOUT, stream=True)
        if response is None:
            logger.warning("Redirecionamento inseguro ou excedido para %s", fonte_nome)
            return []
        with response:
            if response.status_code != 200:
                logger.warning("HTTP %s ao aceder a %s", response.status_code, fonte_nome)
                return []
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=16_384):
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FEED_BYTES:
                    logger.warning("Feed %s excedeu o limite de %d bytes", fonte_nome, MAX_FEED_BYTES)
                    return []
                chunks.append(chunk)
            parsed_feed = feedparser.parse(b"".join(chunks))
    except requests.exceptions.Timeout:
        logger.warning("Timeout (%ss) ao obter %s", FEED_TIMEOUT, fonte_nome)
        return []
    except Exception as exc:
        logger.warning("Erro ao obter %s: %s", fonte_nome, exc)
        return []

    if getattr(parsed_feed, "bozo", False) and not parsed_feed.entries:
        logger.warning("Feed malformatado ou vazio para %s", fonte_nome)
        return []

    items: list[dict] = []
    for entry in parsed_feed.entries[:max_per_feed]:
        title = strip_html_and_clean(entry.get("title", ""), max_chars=240)
        if not title:
            continue

        # Em feeds Google News, o título traz muitas vezes " - Nome da Fonte" no final
        if "news.google.com" in feed_url and " - " in title:
            title_parts = title.rsplit(" - ", 1)
            if len(title_parts[0]) >= 20:
                title = title_parts[0].strip()

        link = clean_url(entry.get("link", ""))
        if not link:
            continue

        summary_raw = entry.get("summary") or entry.get("description") or ""
        content_raw = ""
        content_list = entry.get("content")
        if isinstance(content_list, list) and content_list:
            content_raw = content_list[0].get("value", "")
            if not summary_raw:
                summary_raw = content_raw

        cleaned_summary = strip_html_and_clean(summary_raw, max_chars=380)
        formatted_date, timestamp = parse_entry_date(entry)
        image_url = extract_image_from_entry(entry, content_raw)

        item_id = hashlib.sha256(f"{title}_{link}".encode("utf-8")).hexdigest()[:16]

        items.append({
            "id": item_id,
            "titulo": title,
            "resumo": cleaned_summary,
            "fonte": fonte_nome,
            "pais": pais,
            "data_publicacao": formatted_date,
            "link": link,
            "imagem": image_url,
            "_timestamp": timestamp,
        })

    return items


def deduplicate_candidates(items: list[dict], similarity_threshold: float = 0.72) -> list[dict]:
    """Remove duplicados exatos por URL e títulos muito semelhantes, mantendo os mais recentes."""
    sorted_items = sorted(items, key=lambda x: x.get("_timestamp", 0.0), reverse=True)
    seen_urls: set[str] = set()
    unique_items: list[dict] = []

    for item in sorted_items:
        url_key = item.get("link", "").lower().rstrip("/")
        if not url_key or url_key in seen_urls:
            continue

        is_dup = False
        for existing in unique_items:
            if compute_token_similarity(item["titulo"], existing["titulo"]) >= similarity_threshold:
                is_dup = True
                break

        if not is_dup:
            seen_urls.add(url_key)
            unique_items.append(item)

    return unique_items


def balance_by_source(items: list[dict], limit: int) -> list[dict]:
    """Intercala candidatos por fonte (round-robin) para evitar domínio de um único feed."""
    buckets: dict[str, list[dict]] = {}
    for item in items:
        buckets.setdefault(item["fonte"], []).append(item)

    balanced: list[dict] = []
    source_names = list(buckets.keys())
    idx = 0
    while len(balanced) < limit and source_names:
        active_sources = []
        for src in source_names:
            bucket = buckets[src]
            if idx < len(bucket):
                balanced.append(bucket[idx])
                if len(balanced) >= limit:
                    break
                if idx + 1 < len(bucket):
                    active_sources.append(src)
        source_names = active_sources
        idx += 1

    return balanced


def _enrich_image_if_missing(item: dict) -> dict:
    """Enriquece o candidato com og:image caso o feed RSS não inclua imagem."""
    if not item.get("imagem"):
        og_img = fetch_og_image(item.get("link", ""))
        if og_img:
            item["imagem"] = og_img
    return item


def collect_candidates_for_category(
    category_key: str,
    pool_limit: int = CANDIDATE_POOL_PER_CATEGORY,
    enrich_og_images: bool = True,
) -> list[dict]:
    """Recolhe e prepara o pool de candidatos para uma categoria."""
    feeds = FEEDS_BY_CATEGORY.get(category_key, [])
    if not feeds:
        return []

    raw_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(feeds)))) as executor:
        results = list(executor.map(fetch_feed_items, feeds))
        for feed_items in results:
            raw_items.extend(feed_items)

    deduped = deduplicate_candidates(raw_items)
    selected_pool = balance_by_source(deduped, limit=pool_limit)

    if enrich_og_images and selected_pool:
        with ThreadPoolExecutor(max_workers=8) as executor:
            selected_pool = list(executor.map(_enrich_image_if_missing, selected_pool))

    logger.info(
        "Categoria '%s': %d brutos -> %d únicos -> %d no pool",
        category_key,
        len(raw_items),
        len(deduped),
        len(selected_pool),
    )
    return selected_pool


def collect_all_candidates(
    pool_limit: int = CANDIDATE_POOL_PER_CATEGORY,
    enrich_og_images: bool = True,
) -> dict[str, list[dict]]:
    """Recolhe candidatos para todas as 5 categorias."""
    all_candidates: dict[str, list[dict]] = {}
    for category_key in FEEDS_BY_CATEGORY:
        all_candidates[category_key] = collect_candidates_for_category(
            category_key,
            pool_limit=pool_limit,
            enrich_og_images=enrich_og_images,
        )
    return all_candidates
