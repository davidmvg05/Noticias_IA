"""
Cliente Firebase Firestore para leitura/escrita das edições diárias do Notícias_IA,
gestão do índice de datas e limpeza anual automática em janeiro (a partir de 01/01).
Inclui suporte a persistência local (.tmp/) para modo --dry-run e desenvolvimento offline.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("noticias_ia.firestore_client")

COLLECTION_EDICOES = "edicoes"
DOC_LATEST = "latest"
COLLECTION_META = "meta"
DOC_INDICE = "indice"
DATE_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LISBON_TZ = ZoneInfo("Europe/Lisbon")


def _normalize_service_account_dict(raw_creds: dict[str, Any]) -> dict[str, Any]:
    """Normaliza o dicionário de credenciais (incluindo quebras de linha na private_key)."""
    creds = dict(raw_creds)
    private_key = creds.get("private_key")
    if isinstance(private_key, str) and "\\n" in private_key:
        creds["private_key"] = private_key.replace("\\n", "\n")
    return creds


def load_service_account_from_env(env_var: str = "FIREBASE_SERVICE_ACCOUNT") -> dict[str, Any] | None:
    """Carrega credenciais de conta de serviço a partir de variável de ambiente (JSON ou caminho)."""
    raw_value = os.environ.get(env_var, "").strip()
    if not raw_value:
        return None

    if raw_value.startswith("{"):
        return _normalize_service_account_dict(json.loads(raw_value))

    cred_path = Path(raw_value)
    if cred_path.is_file():
        with cred_path.open("r", encoding="utf-8") as fh:
            return _normalize_service_account_dict(json.load(fh))

    raise ValueError(f"Valor inválido em {env_var}: não é JSON válido nem ficheiro existente.")


def get_firestore_client(credentials_dict: dict[str, Any] | None = None):
    """
    Inicializa (ou reutiliza) a ligação ao Firebase Firestore.
    Aceita um dicionário de credenciais (ex.: st.secrets['firebase']) ou lê FIREBASE_SERVICE_ACCOUNT.
    """
    import firebase_admin
    from firebase_admin import credentials, firestore

    app_name = "noticias_ia_app"
    try:
        app = firebase_admin.get_app(app_name)
        return firestore.client(app=app)
    except ValueError:
        pass

    creds_data = (
        _normalize_service_account_dict(credentials_dict)
        if credentials_dict
        else load_service_account_from_env()
    )
    if not creds_data:
        raise ValueError(
            "Credenciais Firebase não configuradas (forneça st.secrets['firebase'] ou FIREBASE_SERVICE_ACCOUNT)."
        )

    cred = credentials.Certificate(creds_data)
    app = firebase_admin.initialize_app(cred, name=app_name)
    return firestore.client(app=app)


def validate_date_id(date_str: str) -> bool:
    """Verifica se uma string está no formato estrito YYYY-MM-DD."""
    if not date_str or not DATE_ID_RE.match(date_str):
        return False
    try:
        date.fromisoformat(date_str)
        return True
    except ValueError:
        return False


def filter_dates_for_current_year(dates: list[str], current_year: int) -> tuple[list[str], list[str]]:
    """
    Separa datas em (mantidas, removidas) consoante o ano civil.
    Mantém apenas datas com ano >= current_year (ex.: em 2027-01-01 remove todas as de 2026).
    """
    kept: list[str] = []
    removed: list[str] = []
    for d in dates:
        if not validate_date_id(d):
            continue
        year_val = int(d[:4])
        if year_val < current_year:
            removed.append(d)
        else:
            kept.append(d)
    kept_sorted = sorted(set(kept), reverse=True)
    removed_sorted = sorted(set(removed))
    return kept_sorted, removed_sorted


def cleanup_previous_years_if_january(
    db,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """
    Limpeza anual automática a 1 de janeiro (idempotente durante todo o mês de janeiro).
    Ex.: em 01/01/2027 elimina todos os documentos em `edicoes` com ano < 2027 e limpa `meta/indice`.
    """
    ref_date = reference_date or datetime.now(LISBON_TZ).date()
    current_year = ref_date.year

    if ref_date.month != 1:
        return {
            "executed": False,
            "reason": "Fora do mês de janeiro",
            "current_year": current_year,
            "deleted_dates": [],
        }

    edicoes_ref = db.collection(COLLECTION_EDICOES)
    docs_to_delete = []
    deleted_dates: list[str] = []

    for doc_snap in edicoes_ref.stream():
        doc_id = doc_snap.id
        if doc_id == DOC_LATEST:
            continue

        data = doc_snap.to_dict() or {}
        doc_year = data.get("ano")
        if doc_year is None and validate_date_id(doc_id):
            doc_year = int(doc_id[:4])

        if isinstance(doc_year, int) and not isinstance(doc_year, bool) and doc_year < current_year:
            docs_to_delete.append(doc_snap.reference)
            deleted_dates.append(doc_id)

    # Eliminar em lotes de 400 (abaixo do limite de 500 escritas por batch do Firestore)
    batch_size = 400
    for i in range(0, len(docs_to_delete), batch_size):
        batch = db.batch()
        for ref in docs_to_delete[i : i + batch_size]:
            batch.delete(ref)
        batch.commit()

    # Atualizar meta/indice removendo datas de anos anteriores
    indice_ref = db.collection(COLLECTION_META).document(DOC_INDICE)
    indice_snap = indice_ref.get()
    if indice_snap.exists:
        indice_data = indice_snap.to_dict() or {}
        raw_dates = indice_data.get("datas_disponiveis", [])
        if isinstance(raw_dates, list):
            kept_dates, _ = filter_dates_for_current_year(raw_dates, current_year)
            indice_ref.set(
                {
                    "datas_disponiveis": kept_dates,
                    "ultima_atualizacao": datetime.now(timezone.utc).isoformat(),
                    "ultima_limpeza_anual": ref_date.isoformat(),
                },
                merge=True,
            )

    logger.info(
        "Limpeza anual (%s): %d documentos de anos anteriores a %d eliminados.",
        ref_date.isoformat(),
        len(deleted_dates),
        current_year,
    )
    return {
        "executed": True,
        "current_year": current_year,
        "deleted_dates": sorted(deleted_dates),
    }


def save_edition_to_firestore(db, edition: dict[str, Any]) -> None:
    """Guarda a edição em `edicoes/{YYYY-MM-DD}`, atualiza `edicoes/latest` (se for a mais recente) e `meta/indice`."""
    date_str = str(edition.get("data", "")).strip()
    if not validate_date_id(date_str):
        raise ValueError(f"Data de edição inválida: '{date_str}' (esperado YYYY-MM-DD).")

    edicoes_ref = db.collection(COLLECTION_EDICOES)
    edicoes_ref.document(date_str).set(edition)

    indice_ref = db.collection(COLLECTION_META).document(DOC_INDICE)
    indice_snap = indice_ref.get()
    existing_dates: list[str] = []
    if indice_snap.exists:
        data = indice_snap.to_dict() or {}
        if isinstance(data.get("datas_disponiveis"), list):
            existing_dates = [d for d in data["datas_disponiveis"] if validate_date_id(d)]

    updated_dates = sorted(set(existing_dates + [date_str]), reverse=True)
    if not existing_dates or date_str >= max(existing_dates):
        edicoes_ref.document(DOC_LATEST).set(edition)

    indice_ref.set(
        {
            "datas_disponiveis": updated_dates,
            "ultima_atualizacao": datetime.now(timezone.utc).isoformat(),
        },
        merge=True,
    )
    logger.info("Edição '%s' gravada com sucesso no Firestore (%d datas no índice).", date_str, len(updated_dates))


def fetch_available_dates(db) -> list[str]:
    """Lê a lista de datas disponíveis a partir de `meta/indice` (1 leitura)."""
    indice_snap = db.collection(COLLECTION_META).document(DOC_INDICE).get()
    if not indice_snap.exists:
        return []
    data = indice_snap.to_dict() or {}
    dates = data.get("datas_disponiveis", [])
    if not isinstance(dates, list):
        return []
    return sorted({d for d in dates if validate_date_id(d)}, reverse=True)


def fetch_edition_from_firestore(db, date_str: str | None = None) -> dict[str, Any] | None:
    """Lê uma edição específica (`YYYY-MM-DD`) ou `latest` (1 leitura)."""
    doc_id = date_str if (date_str and validate_date_id(date_str)) else DOC_LATEST
    snap = db.collection(COLLECTION_EDICOES).document(doc_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


# ==============================================================================
# Fallback Local (.tmp/) para testes sem Firestore configurado
# ==============================================================================

def save_edition_locally(edition: dict[str, Any], base_dir: Path | str | None = None) -> Path:
    """Guarda a edição num ficheiro JSON local em `.tmp/edicoes/{YYYY-MM-DD}.json` e `latest.json`."""
    root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    edicoes_dir = root / ".tmp" / "edicoes"
    edicoes_dir.mkdir(parents=True, exist_ok=True)

    date_str = str(edition.get("data", "")).strip()
    if not validate_date_id(date_str):
        raise ValueError(f"Data de edição inválida: '{date_str}'.")

    existing_local = list_local_dates(root)
    dated_path = edicoes_dir / f"{date_str}.json"
    latest_path = edicoes_dir / "latest.json"
    payload = json.dumps(edition, ensure_ascii=False, indent=2)
    dated_path.write_text(payload, encoding="utf-8")
    if not existing_local or date_str >= max(existing_local) or not latest_path.is_file():
        latest_path.write_text(payload, encoding="utf-8")
    return dated_path


def list_local_dates(base_dir: Path | str | None = None) -> list[str]:
    """Lista as datas disponíveis em `.tmp/edicoes/`."""
    root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    edicoes_dir = root / ".tmp" / "edicoes"
    if not edicoes_dir.is_dir():
        return []
    dates = [
        p.stem for p in edicoes_dir.glob("*.json")
        if validate_date_id(p.stem)
    ]
    return sorted(dates, reverse=True)


def load_local_edition(date_str: str | None = None, base_dir: Path | str | None = None) -> dict[str, Any] | None:
    """Lê uma edição guardada localmente em `.tmp/edicoes/`."""
    root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
    edicoes_dir = root / ".tmp" / "edicoes"
    if date_str and validate_date_id(date_str):
        target = edicoes_dir / f"{date_str}.json"
    else:
        target = edicoes_dir / "latest.json"
    if not target.is_file():
        return None
    return json.loads(target.read_text(encoding="utf-8"))
