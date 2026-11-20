"""
Cliente leve para disparar e monitorizar o workflow GitHub Actions on-demand.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

DEFAULT_GITHUB_REPO = "davidmvg05/Noticias_IA"
DEFAULT_WORKFLOW_FILE = "atualizar_noticias.yml"
DEFAULT_BRANCH = "main"
GITHUB_API_BASE = "https://api.github.com"
LISBON_TZ = ZoneInfo("Europe/Lisbon")


def get_lisbon_today_str(now_utc: datetime | None = None) -> str:
    """Devolve a data atual no fuso horário Europe/Lisbon em formato YYYY-MM-DD."""
    if now_utc is None:
        return datetime.now(LISBON_TZ).strftime("%Y-%m-%d")
    return now_utc.astimezone(LISBON_TZ).strftime("%Y-%m-%d")


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def trigger_noticias_workflow(
    token: str,
    repo: str = DEFAULT_GITHUB_REPO,
    workflow_file: str = DEFAULT_WORKFLOW_FILE,
    ref: str = DEFAULT_BRANCH,
    timeout: float = 15.0,
) -> tuple[bool, str | None]:
    """Dispara o workflow `workflow_dispatch` via API REST do GitHub."""
    clean_token = (token or "").strip()
    if not clean_token:
        return False, "GITHUB_TOKEN não configurado."

    url = f"{GITHUB_API_BASE}/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    try:
        resp = requests.post(
            url,
            headers=_github_headers(clean_token),
            json={"ref": ref},
            timeout=timeout,
        )
        if resp.status_code in (200, 201, 204):
            return True, None
        detail = resp.text[:300].strip() if resp.text else ""
        return False, f"GitHub API devolveu HTTP {resp.status_code}: {detail}"
    except requests.RequestException as exc:
        return False, f"Erro de ligação ao GitHub Actions: {exc}"


def fetch_latest_workflow_run(
    token: str,
    repo: str = DEFAULT_GITHUB_REPO,
    workflow_file: str = DEFAULT_WORKFLOW_FILE,
    created_after: str | None = None,
    timeout: float = 15.0,
) -> tuple[dict[str, Any] | None, str | None]:
    """Obtém o estado do run `workflow_dispatch` mais recente do workflow."""
    clean_token = (token or "").strip()
    if not clean_token:
        return None, "GITHUB_TOKEN não configurado."

    url = f"{GITHUB_API_BASE}/repos/{repo}/actions/workflows/{workflow_file}/runs"
    try:
        resp = requests.get(
            url,
            headers=_github_headers(clean_token),
            params={"event": "workflow_dispatch", "per_page": 5},
            timeout=timeout,
        )
        if resp.status_code != 200:
            detail = resp.text[:300].strip() if resp.text else ""
            return None, f"GitHub API devolveu HTTP {resp.status_code}: {detail}"

        runs = resp.json().get("workflow_runs") or []
        for run in runs:
            run_created = str(run.get("created_at") or "")
            if created_after and run_created and run_created < created_after:
                continue
            return {
                "id": run.get("id"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "html_url": run.get("html_url"),
                "created_at": run_created,
            }, None

        return None, None
    except requests.RequestException as exc:
        return None, f"Erro ao consultar estado do GitHub Actions: {exc}"
