"""
Agente de Curadoria e Síntese com Google Gemini 3.8 Flash (SDK oficial google-genai).
- Curadoria de 10 notícias por categoria com desduplicação semântica.
- Reescrita de título e resumo (2-3 frases) em Português de Portugal (PT-PT).
- Fallback automático com Google Search Grounding caso o pool RSS fique abaixo de 20 itens.
- Geração do Briefing Executivo "Resumo do Dia".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from execution.fetch_candidates import clean_url, fetch_og_image
from execution.sources import CANDIDATE_POOL_PER_CATEGORY, CATEGORIES_META, TARGET_PER_CATEGORY

logger = logging.getLogger("noticias_ia.ai_agent")

FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
)
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash").strip() or "gemini-3.5-flash"
GEMINI_HTTP_TIMEOUT_MS = 90000
MAX_CANDIDATE_SUMMARY_CHARS = 180

CATEGORY_CURATION_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "selecionadas": {
            "type": "ARRAY",
            "description": "Lista ordenada das notícias mais relevantes selecionadas.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "candidate_id": {
                        "type": "STRING",
                        "description": "ID exato do candidato selecionado do pool fornecido.",
                    },
                    "titulo_pt": {
                        "type": "STRING",
                        "description": "Título jornalístico claro e fiel em Português de Portugal (PT-PT).",
                    },
                    "resumo_pt": {
                        "type": "STRING",
                        "description": "Resumo objetivo de 2 a 3 frases em Português de Portugal (PT-PT), sem clickbait.",
                    },
                },
                "required": ["candidate_id", "titulo_pt", "resumo_pt"],
            },
        }
    },
    "required": ["selecionadas"],
}

DAILY_BRIEFING_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "manchete": {
            "type": "STRING",
            "description": "Título curto e impactante que resume o pulso do dia em PT-PT.",
        },
        "sintese": {
            "type": "STRING",
            "description": "Parágrafo executivo de 3 a 4 frases em Português de Portugal (PT-PT) que sintetiza o dia.",
        },
        "pontos_chave": {
            "type": "ARRAY",
            "description": "Exatamente 5 pontos-chave, um por cada categoria do painel.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "categoria": {"type": "STRING"},
                    "destaque": {"type": "STRING"},
                },
                "required": ["categoria", "destaque"],
            },
        },
    },
    "required": ["manchete", "sintese", "pontos_chave"],
}


def _create_genai_client(api_key: str | None = None):
    """Inicializa o cliente oficial google-genai com timeout HTTP de 45s."""
    key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
    if not key:
        raise ValueError("Variável de ambiente GEMINI_API_KEY não configurada.")
    from google import genai  # import tardio para permitir testes sem SDK instalado

    return genai.Client(api_key=key, http_options={"timeout": GEMINI_HTTP_TIMEOUT_MS})


def _build_model_chain(primary_model: str | None = None) -> list[str]:
    """Constrói a lista ordenada de modelos para fallback automático sem duplicados."""
    ordered: list[str] = []
    first = (primary_model or os.environ.get("GEMINI_MODEL", "") or DEFAULT_MODEL).strip()
    if first:
        ordered.append(first)
    for model_name in FALLBACK_MODELS:
        if model_name not in ordered:
            ordered.append(model_name)
    return ordered


def _is_model_fallback_error(exc: Exception) -> bool:
    """Devolve True se o erro (503, 504, 429 ou 404) justificar troca imediata de modelo."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (404, 429, 503, 504):
        return True
    msg = str(exc).upper()
    return any(
        token in msg
        for token in (
            "503",
            "504",
            "429",
            "404",
            "UNAVAILABLE",
            "DEADLINE_EXCEEDED",
            "GATEWAY TIMEOUT",
            "RESOURCE_EXHAUSTED",
            "NOT_FOUND",
        )
    )


def _truncate_text(text: str, max_chars: int = MAX_CANDIDATE_SUMMARY_CHARS) -> str:
    """Trunca texto longo para reduzir tokens de entrada enviados ao Gemini."""
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _call_with_retry(fn, max_retries: int = 2, base_delay: float = 2.0):
    """Executa chamada à API Gemini com backoff exponencial simples."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                wait_s = base_delay * (2**attempt)
                logger.warning("Tentativa %d falhou (%s). A repetir em %.1fs...", attempt + 1, exc, wait_s)
                time.sleep(wait_s)
    raise last_exc  # type: ignore[misc]


def _call_with_model_fallback(
    call_for_model,
    primary_model: str = DEFAULT_MODEL,
    max_retries_other_errors: int = 1,
    base_delay: float = 2.0,
):
    """
    Executa chamada à API Gemini percorrendo a cadeia de modelos em caso de 503/429/404.
    Em erros 503, 429 ou 404 passa imediatamente para o modelo seguinte da lista.
    """
    models = _build_model_chain(primary_model)
    last_exc: Exception | None = None

    for idx, model_name in enumerate(models):
        for attempt in range(max_retries_other_errors + 1):
            try:
                return call_for_model(model_name)
            except Exception as exc:
                last_exc = exc
                if _is_model_fallback_error(exc):
                    if idx < len(models) - 1:
                        logger.warning(
                            "Modelo '%s' devolveu erro elegível para fallback (%s). A mudar para '%s'...",
                            model_name,
                            exc,
                            models[idx + 1],
                        )
                        if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc).upper():
                            time.sleep(5.0)
                    else:
                        logger.warning("Último modelo '%s' também falhou (%s).", model_name, exc)
                    break
                if attempt < max_retries_other_errors:
                    wait_s = base_delay * (2**attempt)
                    logger.warning(
                        "Tentativa %d no modelo '%s' falhou (%s). A repetir em %.1fs...",
                        attempt + 1,
                        model_name,
                        exc,
                        wait_s,
                    )
                    time.sleep(wait_s)
                else:
                    raise

    raise last_exc  # type: ignore[misc]


def _extract_json_block(text: str) -> Any:
    """Extrai o primeiro bloco JSON válido (objeto ou array) de uma resposta textual."""
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    idx = 0
    length = len(stripped)
    while idx < length:
        next_obj = stripped.find("{", idx)
        next_arr = stripped.find("[", idx)
        positions = [p for p in (next_obj, next_arr) if p != -1]
        if not positions:
            return None
        start = min(positions)
        try:
            obj, _ = decoder.raw_decode(stripped, start)
            return obj
        except json.JSONDecodeError:
            idx = start + 1
    return None


def _is_approved_domain(url: str, approved_domains: list[str]) -> bool:
    """Valida que o URL pertence a um domínio PT/US/UK aprovado e nunca a um domínio .br."""
    if not url or not isinstance(url, str):
        return False
    cleaned = url.strip()
    if "\\" in cleaned or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        return False
    try:
        parsed = urlparse(cleaned)
        _ = parsed.port
        if parsed.scheme not in ("http", "https") or parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
            return False
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not hostname or hostname.endswith(".br"):
        return False
    for domain in approved_domains:
        d = domain.lower().lstrip(".")
        if hostname == d or hostname.endswith(f".{d}"):
            return True
    return False


def _search_grounding_fallback(
    client,
    category_key: str,
    missing_count: int,
    existing_links: set[str],
    model: str = DEFAULT_MODEL,
) -> list[dict]:
    """
    Complementa notícias em falta recorrendo a Google Search Grounding do Gemini,
    restrito exclusivamente aos domínios PT, US e UK aprovados.
    """
    if missing_count <= 0:
        return []

    from google.genai import types

    meta = CATEGORIES_META[category_key]
    domains_str = ", ".join(meta["approved_domains"])
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = (
        f"Data de hoje: {today_str}.\n"
        f"Encontra {missing_count} notícias recentes e verificadas para a categoria '{meta['label']}'.\n"
        f"Foco editorial: {meta['editorial_focus']}\n"
        f"Regras obrigatórias:\n"
        f"1. Usa APENAS artigos reais publicados nos seguintes domínios (PT, US ou UK): {domains_str}.\n"
        f"2. NUNCA incluas fontes brasileiras (.com.br).\n"
        f"3. Escreve 'titulo' e 'resumo' (2-3 frases) em Português de Portugal (PT-PT).\n"
        f"4. Devolve APENAS um array JSON com objetos contendo: "
        f"titulo, resumo, fonte, data (YYYY-MM-DD), link.\n"
    )

    try:
        response = _call_with_model_fallback(
            lambda m: client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                ),
            ),
            primary_model=model,
        )
        parsed = _extract_json_block(response.text or "")
        if not isinstance(parsed, list):
            return []
    except Exception as exc:
        logger.warning("Fallback Search Grounding falhou para '%s': %s", category_key, exc)
        return []

    results: list[dict] = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    for entry in parsed:
        if len(results) >= missing_count:
            break
        if not isinstance(entry, dict):
            continue
        link = clean_url(str(entry.get("link", "")))
        if not link or link.lower() in existing_links:
            continue
        # Garantir estritamente domínios PT/US/UK aprovados e bloquear .br
        if not _is_approved_domain(link, meta["approved_domains"]):
            continue

        titulo = str(entry.get("titulo", "")).strip()
        resumo = str(entry.get("resumo", "")).strip()
        fonte = str(entry.get("fonte", "Fonte Verificada")).strip()
        data_pub = str(entry.get("data", now_str)).strip() or now_str
        if not titulo or not resumo:
            continue

        img_url = fetch_og_image(link)
        item_id = hashlib.sha256(f"{titulo}_{link}".encode("utf-8")).hexdigest()[:16]
        existing_links.add(link.lower())
        results.append({
            "id": item_id,
            "titulo": titulo,
            "resumo": resumo,
            "fonte": fonte,
            "data": data_pub,
            "link": link,
            "imagem": img_url,
            "origem_curadoria": "search_grounding",
        })

    logger.info("Search Grounding adicionou %d notícias para '%s'", len(results), category_key)
    return results


def curate_category(
    category_key: str,
    candidates: list[dict],
    target_count: int = TARGET_PER_CATEGORY,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    enable_search_fallback: bool = True,
    client=None,
) -> list[dict]:
    """
    Seleciona até `target_count` (20) notícias de maior qualidade para a categoria,
    eliminando duplicados semânticos e reescrevendo título e resumo em PT-PT.
    """
    meta = CATEGORIES_META[category_key]
    active_client = client or _create_genai_client(api_key=api_key)
    by_id = {c["id"]: c for c in candidates if c.get("id")}

    curated: list[dict] = []
    used_ids: set[str] = set()
    used_links: set[str] = set()
    primary_call_failed = False

    if candidates:
        try:
            from google.genai import types

            pool_candidates = candidates[:CANDIDATE_POOL_PER_CATEGORY]
            compact_candidates = [
                {
                    "candidate_id": c["id"],
                    "fonte": c.get("fonte", ""),
                    "pais": c.get("pais", ""),
                    "data": c.get("data_publicacao", ""),
                    "titulo": c.get("titulo", ""),
                    "resumo": _truncate_text(c.get("resumo", ""), MAX_CANDIDATE_SUMMARY_CHARS),
                }
                for c in pool_candidates
            ]

            prompt = (
                f"És um editor-chefe português rigoroso para o painel 'Radar de Notícias IA'.\n"
                f"Categoria: {meta['label']}\n"
                f"Foco editorial: {meta['editorial_focus']}\n\n"
                f"Tarefa:\n"
                f"1. Analisa os candidatos abaixo e seleciona EXATAMENTE até {target_count} notícias distintas "
                f"(as mais relevantes, atuais e informativas).\n"
                f"2. Elimina duplicados semânticos (se dois jornais cobrem exatamente o mesmo evento, mantém apenas o melhor).\n"
                f"3. Descarta clickbait, anúncios, páginas genéricas ou conteúdo irrelevante para a categoria.\n"
                f"4. Mantém diversidade de fontes (PT, US e UK) sempre que possível.\n"
                f"5. Reescreve 'titulo_pt' e 'resumo_pt' (1 a 2 frases claras, factuais e bem redigidas) "
                f"estritamente em Português de Portugal (PT-PT — ex.: 'utilizadores', 'equipas', 'ações', 'ficheiro', 'telemóvel').\n"
                f"6. Devolve o 'candidate_id' exato de cada candidato escolhido.\n\n"
                f"Candidatos JSON:\n{json.dumps(compact_candidates, ensure_ascii=False)}"
            )

            response = _call_with_model_fallback(
                lambda m: active_client.models.generate_content(
                    model=m,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=CATEGORY_CURATION_SCHEMA,
                        temperature=0.2,
                    ),
                ),
                primary_model=model,
            )
            parsed_obj = _extract_json_block(response.text or "")
            selected_list = (
                parsed_obj.get("selecionadas", []) if isinstance(parsed_obj, dict) else []
            )

            for item in selected_list:
                if len(curated) >= target_count:
                    break
                cid = str(item.get("candidate_id", "")).strip()
                if not cid or cid in used_ids or cid not in by_id:
                    continue
                orig = by_id[cid]
                link = orig.get("link", "")
                if not link or link.lower() in used_links:
                    continue

                titulo_pt = str(item.get("titulo_pt", "")).strip() or orig.get("titulo", "")
                resumo_pt = str(item.get("resumo_pt", "")).strip() or orig.get("resumo", "")
                pais = str(orig.get("pais", "")).strip()
                is_pt = pais.upper() == "PT"

                used_ids.add(cid)
                used_links.add(link.lower())
                curated.append({
                    "id": cid,
                    "titulo": titulo_pt if is_pt else (orig.get("titulo", "") or titulo_pt),
                    "resumo": resumo_pt if is_pt else (orig.get("resumo", "") or resumo_pt),
                    "titulo_pt": titulo_pt,
                    "resumo_pt": resumo_pt,
                    "is_en": not is_pt,
                    "pais": pais,
                    "fonte": orig.get("fonte", "Fonte"),
                    "data": orig.get("data_publicacao", ""),
                    "link": link,
                    "imagem": orig.get("imagem", ""),
                    "origem_curadoria": "rss_gemini",
                })
        except Exception as exc:
            primary_call_failed = True
            logger.error("Erro na curadoria Gemini para '%s': %s", category_key, exc)

    # Fallback Search Grounding apenas se o pool RSS inicial não tiver candidatos suficientes (< target_count)
    # Evita chamadas redundantes à API Gemini quando já existem dezenas de candidatos RSS válidos
    if (
        enable_search_fallback
        and not primary_call_failed
        and len(candidates) < target_count
        and len(curated) < target_count
    ):
        missing = target_count - len(curated)
        logger.info(
            "Categoria '%s' com %d/%d itens após RSS; a acionar Search Grounding para %d itens.",
            category_key,
            len(curated),
            target_count,
            missing,
        )
        grounded = _search_grounding_fallback(
            client=active_client,
            category_key=category_key,
            missing_count=missing,
            existing_links=used_links,
            model=model,
        )
        curated.extend(grounded)

    # Último recurso determinístico: completar com candidatos RSS ainda não utilizados
    if len(curated) < target_count:
        for orig in candidates:
            if len(curated) >= target_count:
                break
            cid = orig.get("id", "")
            link = orig.get("link", "")
            if cid in used_ids or not link or link.lower() in used_links:
                continue
            pais = str(orig.get("pais", "")).strip()
            used_ids.add(cid)
            used_links.add(link.lower())
            curated.append({
                "id": cid,
                "titulo": orig.get("titulo", ""),
                "resumo": orig.get("resumo", "") or "Consulte o artigo original para mais detalhes.",
                "pais": pais,
                "is_en": pais.upper() != "PT",
                "fonte": orig.get("fonte", "Fonte"),
                "data": orig.get("data_publicacao", ""),
                "link": link,
                "imagem": orig.get("imagem", ""),
                "origem_curadoria": "rss_fallback",
            })

    logger.info("Categoria '%s' finalizada com %d notícias.", category_key, len(curated))
    return curated[:target_count]


def generate_daily_briefing(
    curated_by_category: dict[str, list[dict]],
    date_str: str,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    client=None,
) -> dict:
    """Gera o bloco executivo 'Resumo do Dia' em PT-PT com base nas 5 categorias curadas."""
    try:
        active_client = client or _create_genai_client(api_key=api_key)
        from google.genai import types

        highlights_payload: dict[str, list[dict[str, str]]] = {}
        for cat_key, meta in CATEGORIES_META.items():
            items = curated_by_category.get(cat_key, [])[:6]
            highlights_payload[meta["label"]] = [
                {"titulo": i.get("titulo", ""), "resumo": i.get("resumo", ""), "fonte": i.get("fonte", "")}
                for i in items
            ]

        prompt = (
            f"Data da edição: {date_str}.\n"
            f"Com base nos principais destaques curados hoje nas 5 áreas abaixo, cria um Briefing Executivo "
            f"'Resumo do Dia' em Português de Portugal (PT-PT), claro, incisivo e útil para leitura em 30 segundos.\n"
            f"- 'manchete': frase curta (máx. 14 palavras) que capta o tema dominante do dia.\n"
            f"- 'sintese': 1 parágrafo de 3 a 4 frases ligando as principais tendências do dia.\n"
            f"- 'pontos_chave': exatamente 5 itens, um para cada categoria "
            f"(Economia & Mercados, Inteligência Artificial, Automações, Marketing Digital, Notícias Felizes).\n\n"
            f"Destaques do dia:\n{json.dumps(highlights_payload, ensure_ascii=False)}"
        )

        response = _call_with_model_fallback(
            lambda m: active_client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=DAILY_BRIEFING_SCHEMA,
                    temperature=0.3,
                ),
            ),
            primary_model=model,
        )
        parsed = _extract_json_block(response.text or "")
        if isinstance(parsed, dict) and parsed.get("sintese"):
            return {
                "manchete": str(parsed.get("manchete", f"Resumo do Dia — {date_str}")).strip(),
                "sintese": str(parsed.get("sintese", "")).strip(),
                "pontos_chave": [
                    {
                        "categoria": str(p.get("categoria", "")).strip(),
                        "destaque": str(p.get("destaque", "")).strip(),
                    }
                    for p in parsed.get("pontos_chave", [])
                    if isinstance(p, dict) and p.get("destaque")
                ],
            }
    except Exception as exc:
        logger.error("Erro ao gerar Resumo do Dia com Gemini: %s", exc)

    return build_fallback_briefing(curated_by_category, date_str)


def build_fallback_briefing(curated_by_category: dict[str, list[dict]], date_str: str) -> dict:
    """Gera um resumo determinístico de salvaguarda caso a síntese IA falhe ou em modo --skip-ai."""
    pontos = []
    for cat_key, meta in CATEGORIES_META.items():
        items = curated_by_category.get(cat_key, [])
        if items:
            top = items[0]
            pontos.append({
                "categoria": meta["label"],
                "destaque": f"{top.get('titulo', '')} ({top.get('fonte', '')}).",
            })
        else:
            pontos.append({
                "categoria": meta["label"],
                "destaque": "Sem destaques disponíveis nesta edição.",
            })

    total = sum(len(v) for v in curated_by_category.values())
    return {
        "manchete": f"Edição de {date_str} — {total} notícias selecionadas",
        "sintese": (
            f"A edição de {date_str} reúne {total} notícias verificadas de fontes de Portugal, "
            f"Estados Unidos e Reino Unido, cobrindo Economia & Mercados (incluindo ETFs), "
            f"Inteligência Artificial, Automações, Marketing Digital e Notícias Felizes."
        ),
        "pontos_chave": pontos,
    }
