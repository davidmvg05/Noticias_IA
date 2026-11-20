"""
Orquestrador CLI diário do agente Notícias_IA (invocado pelo GitHub Actions às 10:00 de Lisboa
ou localmente em modo --dry-run).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Garantir que a raiz do projeto está no sys.path quando executado diretamente
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.ai_agent import (
    DEFAULT_MODEL,
    build_fallback_briefing,
    curate_category,
    generate_daily_briefing,
)
from execution.fetch_candidates import collect_all_candidates
from execution.firestore_client import (
    LISBON_TZ,
    cleanup_previous_years_if_january,
    get_firestore_client,
    save_edition_locally,
    save_edition_to_firestore,
    validate_date_id,
)
from execution.sources import CATEGORIES_META, TARGET_PER_CATEGORY

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("noticias_ia.runner")

AI_CALL_DELAY_SECONDS = 25.0


def should_run_at_lisbon_hour(now_utc: datetime | None = None, target_hour: int = 10) -> bool:
    """
    Verifica se a hora atual em Europe/Lisbon corresponde à hora alvo (por omissão 10:xx).
    Resolve automaticamente a mudança de hora de inverno (WET, UTC+0) e verão (WEST, UTC+1).
    """
    current_utc = now_utc or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    lisbon_now = current_utc.astimezone(LISBON_TZ)
    return lisbon_now.hour == target_hour


def build_edition_document(
    date_str: str,
    curated_by_category: dict[str, list[dict]],
    resumo_do_dia: dict[str, Any],
    model_name: str,
) -> dict[str, Any]:
    """Monta o documento normalizado da edição diária para o Firestore e cache local."""
    now_utc = datetime.now(timezone.utc)
    now_lisbon = now_utc.astimezone(LISBON_TZ)
    per_cat = {cat: len(curated_by_category.get(cat, [])) for cat in CATEGORIES_META}
    total_noticias = sum(per_cat.values())

    return {
        "data": date_str,
        "ano": int(date_str[:4]),
        "gerado_em": now_utc.isoformat(),
        "gerado_em_lisboa": now_lisbon.strftime("%d/%m/%Y às %H:%M (%Z)"),
        "modelo_ia": model_name,
        "resumo_do_dia": resumo_do_dia,
        "estatisticas": {
            "total_noticias": total_noticias,
            "por_categoria": per_cat,
        },
        "categorias": curated_by_category,
    }


def _curate_without_ai(
    candidates_by_category: dict[str, list[dict]],
    target_count: int = TARGET_PER_CATEGORY,
) -> dict[str, list[dict]]:
    """Modo determinístico de teste (--skip-ai): converte os top candidatos RSS para o schema final."""
    result: dict[str, list[dict]] = {}
    for cat_key in CATEGORIES_META:
        items = []
        for c in candidates_by_category.get(cat_key, [])[:target_count]:
            items.append({
                "id": c["id"],
                "titulo": c.get("titulo", ""),
                "resumo": c.get("resumo", "") or "Consulte o artigo original para mais detalhes.",
                "fonte": c.get("fonte", "Fonte"),
                "data": c.get("data_publicacao", ""),
                "link": c.get("link", ""),
                "imagem": c.get("imagem", ""),
                "origem_curadoria": "rss_dry_run",
            })
        result[cat_key] = items
    return result


def run_pipeline(
    dry_run: bool = False,
    skip_ai: bool = False,
    date_override: str | None = None,
    enrich_og_images: bool = True,
    target_per_category: int = TARGET_PER_CATEGORY,
) -> dict[str, Any]:
    """Executa o pipeline completo de coleta, curadoria IA, síntese, gravação e limpeza anual."""
    now_lisbon = datetime.now(LISBON_TZ)
    date_str = date_override or now_lisbon.strftime("%Y-%m-%d")
    if not validate_date_id(date_str):
        raise ValueError(f"Data inválida '{date_str}'. Use o formato YYYY-MM-DD.")

    logger.info("A iniciar pipeline Notícias_IA para %s (dry_run=%s, skip_ai=%s)", date_str, dry_run, skip_ai)

    # 1. Coleta de candidatos RSS + og:image
    candidates_by_cat = collect_all_candidates(enrich_og_images=enrich_og_images)

    # 2. Curadoria & Reescrita PT-PT com Gemini 2.5 Flash (ou modo --skip-ai)
    has_gemini_key = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    if skip_ai or (dry_run and not has_gemini_key):
        if not skip_ai:
            logger.warning("GEMINI_API_KEY não definida em --dry-run; a usar modo determinístico RSS.")
        curated_by_cat = _curate_without_ai(candidates_by_cat, target_count=target_per_category)
        resumo_do_dia = build_fallback_briefing(curated_by_cat, date_str)
        model_used = "rss-only (dry-run)"
    else:
        curated_by_cat = {}
        for idx, cat_key in enumerate(CATEGORIES_META):
            if idx > 0:
                logger.info("Pausa de %.0fs entre categorias (rate limit free tier)...", AI_CALL_DELAY_SECONDS)
                time.sleep(AI_CALL_DELAY_SECONDS)
            curated_by_cat[cat_key] = curate_category(
                category_key=cat_key,
                candidates=candidates_by_cat.get(cat_key, []),
                target_count=target_per_category,
                model=DEFAULT_MODEL,
            )
        logger.info("Pausa de %.0fs antes do Resumo do Dia (rate limit free tier)...", AI_CALL_DELAY_SECONDS)
        time.sleep(AI_CALL_DELAY_SECONDS)
        resumo_do_dia = generate_daily_briefing(
            curated_by_category=curated_by_cat,
            date_str=date_str,
            model=DEFAULT_MODEL,
        )
        model_used = DEFAULT_MODEL

    # 3. Construir documento da edição
    edition = build_edition_document(
        date_str=date_str,
        curated_by_category=curated_by_cat,
        resumo_do_dia=resumo_do_dia,
        model_name=model_used,
    )

    # Guardar sempre cópia local para inspeção/fallback
    local_path = save_edition_locally(edition, base_dir=PROJECT_ROOT)
    logger.info("Cópia local gravada em %s", local_path)

    # 4. Gravar no Firebase Firestore + Limpeza anual (1 de janeiro)
    if not dry_run:
        db = get_firestore_client()
        save_edition_to_firestore(db, edition)
        cleanup_result = cleanup_previous_years_if_january(db, reference_date=now_lisbon.date())
        logger.info("Resultado limpeza anual: %s", cleanup_result)
    else:
        logger.info("Modo --dry-run ativo: escrita no Firestore ignorada.")

    stats = edition["estatisticas"]
    logger.info(
        "Pipeline concluído! Total=%d notícias | Por categoria=%s",
        stats["total_noticias"],
        stats["por_categoria"],
    )
    return edition


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agente diário Notícias_IA (Gemini 2.5 Flash + Firestore)")
    parser.add_argument("--dry-run", action="store_true", help="Não escreve no Firestore; guarda apenas em .tmp/.")
    parser.add_argument("--skip-ai", action="store_true", help="Ignora chamadas ao Gemini (testa apenas recolha RSS).")
    parser.add_argument("--no-og", action="store_true", help="Ignora extração de og:image para execução rápida.")
    parser.add_argument("--date", type=str, default=None, help="Forçar data da edição (YYYY-MM-DD).")
    parser.add_argument(
        "--respect-lisbon-schedule",
        action="store_true",
        help="Só executa se forem 10:xx em Europe/Lisbon (usado pelo cron UTC do GitHub Actions).",
    )
    args = parser.parse_args(argv)

    if args.respect_lisbon_schedule:
        now_lisbon = datetime.now(LISBON_TZ)
        if not should_run_at_lisbon_hour(target_hour=10):
            logger.info(
                "Hora atual em Europe/Lisbon é %s (alvo: 10:xx). Execução agendada ignorada com sucesso.",
                now_lisbon.strftime("%H:%M %Z"),
            )
            return 0
        logger.info("Hora atual em Europe/Lisbon é %s — a executar edição das 10:00.", now_lisbon.strftime("%H:%M %Z"))

    run_pipeline(
        dry_run=args.dry_run,
        skip_ai=args.skip_ai,
        date_override=args.date,
        enrich_og_images=not args.no_og,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
