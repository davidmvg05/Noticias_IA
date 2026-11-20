"""
Suite de testes unitários para o projeto Notícias_IA.
Executa com: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.ai_agent import (
    FALLBACK_MODELS,
    GEMINI_HTTP_TIMEOUT_MS,
    MAX_CANDIDATE_SUMMARY_CHARS,
    _build_model_chain,
    _create_genai_client,
    _extract_json_block,
    _is_approved_domain,
    build_fallback_briefing,
    curate_category,
    generate_daily_briefing,
)
from execution.fetch_candidates import (
    MAX_FEED_BYTES,
    _SSRF_TLS,
    _safe_http_get,
    _ssrf_safe_create_connection,
    clean_url,
    deduplicate_candidates,
    fetch_feed_items,
    fetch_og_image,
    is_safe_http_url,
    strip_html_and_clean,
)
from execution.firestore_client import (
    cleanup_previous_years_if_january,
    filter_dates_for_current_year,
    list_local_dates,
    load_local_edition,
    save_edition_locally,
    save_edition_to_firestore,
    validate_date_id,
)
from execution.run_daily_agent import build_edition_document, should_run_at_lisbon_hour
from execution.sources import (
    CANDIDATE_POOL_PER_CATEGORY,
    CATEGORIES_META,
    FEEDS_BY_CATEGORY,
    TARGET_PER_CATEGORY,
)


class _FakeDocSnapshot:
    def __init__(self, doc_id: str, data: dict | None, collection_store: dict):
        self.id = doc_id
        self._data = data
        self.exists = data is not None
        self.reference = (doc_id, collection_store)

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, doc_id: str, store: dict):
        self._id = doc_id
        self._store = store

    def get(self):
        return _FakeDocSnapshot(self._id, self._store.get(self._id), self._store)

    def set(self, data: dict, merge: bool = False):
        if merge and self._id in self._store:
            merged = dict(self._store[self._id])
            merged.update(data)
            self._store[self._id] = merged
        else:
            self._store[self._id] = dict(data)


class _FakeCollection:
    def __init__(self, store: dict):
        self._store = store

    def stream(self):
        return [_FakeDocSnapshot(k, v, self._store) for k, v in list(self._store.items())]

    def document(self, doc_id: str):
        return _FakeDocRef(doc_id, self._store)


class _FakeBatch:
    def __init__(self):
        self._ops = []

    def delete(self, ref):
        self._ops.append(ref)

    def commit(self):
        for doc_id, store in self._ops:
            store.pop(doc_id, None)


class _FakeFirestoreDB:
    def __init__(self):
        self.collections: dict[str, dict] = {"edicoes": {}, "meta": {}}

    def collection(self, name: str):
        return _FakeCollection(self.collections.setdefault(name, {}))

    def batch(self):
        return _FakeBatch()


class TestSourcesConfiguration(unittest.TestCase):
    def test_five_categories_and_target_10(self):
        expected = {"economia", "ia", "automacoes", "marketing_digital", "noticias_felizes"}
        self.assertEqual(set(CATEGORIES_META.keys()), expected)
        self.assertEqual(set(FEEDS_BY_CATEGORY.keys()), expected)
        self.assertEqual(TARGET_PER_CATEGORY, 10)
        self.assertEqual(CANDIDATE_POOL_PER_CATEGORY, 35)

    def test_no_brazilian_sources(self):
        for cat, feeds in FEEDS_BY_CATEGORY.items():
            for feed in feeds:
                self.assertNotIn(".br", feed["url"].lower(), f"Fonte BR detetada em {cat}: {feed}")
                self.assertNotEqual(feed.get("pais", "").upper(), "BR")
        for cat, meta in CATEGORIES_META.items():
            for domain in meta["approved_domains"]:
                self.assertFalse(domain.lower().endswith(".br"), f"Domínio BR detetado em {cat}: {domain}")

    def test_etf_us_and_uk_sources_in_economia(self):
        econ_urls = " ".join(f["url"].lower() for f in FEEDS_BY_CATEGORY["economia"])
        self.assertIn("etftrends.com", econ_urls)
        self.assertIn("etfdb.com", econ_urls)
        self.assertIn("etf.com", econ_urls)
        self.assertIn("etfstream.com", econ_urls)
        focus = CATEGORIES_META["economia"]["editorial_focus"].lower()
        for keyword in ("etf", "s&p 500", "nasdaq 100", "biotecnologia", "defesa", "energia renovável"):
            self.assertIn(keyword, focus)

    def test_hacker_news_automation_feed_present(self):
        auto_names = " ".join(f["nome"].lower() for f in FEEDS_BY_CATEGORY["automacoes"])
        auto_urls = " ".join(f["url"].lower() for f in FEEDS_BY_CATEGORY["automacoes"])
        self.assertIn("hacker news", auto_names)
        self.assertIn("hnrss.org", auto_urls)


class TestSecurityAndUrlHelpers(unittest.TestCase):
    def test_clean_url_strips_tracking_and_blocks_bad_schemes(self):
        dirty = "https://www.ft.com/content/123?utm_source=twitter&utm_medium=social&id=42&fbclid=abc"
        self.assertEqual(clean_url(dirty), "https://www.ft.com/content/123?id=42")
        self.assertEqual(clean_url("javascript:alert(1)"), "")
        self.assertEqual(clean_url("file:///etc/passwd"), "")
        self.assertEqual(clean_url(""), "")

    def test_ssrf_protection_blocks_private_and_metadata_ips(self):
        blocked = [
            "http://localhost/admin",
            "http://127.0.0.1:8080/test",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/internal",
            "http://192.168.1.1/router",
            "http://[::1]/test",
            "ftp://example.com/file",
            "javascript:alert(1)",
        ]
        for url in blocked:
            self.assertFalse(is_safe_http_url(url), f"URL inseguro não foi bloqueado: {url}")

    def test_ssrf_redirect_to_metadata_ip_is_blocked(self):
        fake_redirect = MagicMock()
        fake_redirect.is_redirect = True
        fake_redirect.status_code = 302
        fake_redirect.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        def fake_is_safe(u: str) -> bool:
            return u.startswith("https://public.example.com")

        with patch("execution.fetch_candidates.is_safe_http_url", side_effect=fake_is_safe), \
             patch("execution.fetch_candidates.requests.get", return_value=fake_redirect) as mock_get:
            resp = _safe_http_get("https://public.example.com/article", timeout=3.0)
            self.assertIsNone(resp)
            mock_get.assert_called_once()
            fake_redirect.close.assert_called_once()

    def test_strip_html_and_deduplicate(self):
        self.assertEqual(strip_html_and_clean("<p>Olá <b>mundo</b> &amp; IA</p>"), "Olá mundo & IA")
        items = [
            {"id": "1", "titulo": "S&P 500 ETF hits record high today", "link": "https://a.com/1", "_timestamp": 200},
            {"id": "2", "titulo": "S&P 500 ETF hits record high today!", "link": "https://b.com/2", "_timestamp": 100},
            {"id": "3", "titulo": "ECB cuts interest rates in Europe", "link": "https://c.com/3", "_timestamp": 150},
        ]
        deduped = deduplicate_candidates(items)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0]["id"], "1")

    def test_ssrf_blocks_nat64_multicast_and_invalid_ports(self):
        blocked = [
            "http://[64:ff9b::169.254.169.254]/latest/meta-data/",
            "http://[64:ff9b::127.0.0.1]/",
            "http://[ff02::1]/",
            "http://224.0.0.1/",
            "https://example.com:99999/test",
            "https://example.com:abc/test",
            "https://evil.com\\@ft.com/news",
            "https://user:pass@ft.com/news",
        ]
        for url in blocked:
            self.assertFalse(is_safe_http_url(url), f"URL inseguro não foi bloqueado: {url}")
        self.assertEqual(fetch_og_image("https://example.com:99999/test"), "")
        self.assertEqual(clean_url("https://evil.com\\@ft.com/news"), "")
        self.assertEqual(clean_url("https://user:pass@ft.com/news"), "")
        self.assertEqual(clean_url("https://example.com:99999/test"), "")

    def test_ssrf_safe_create_connection_blocks_dns_rebinding_to_private_ip(self):
        fake_addrinfo = [(2, 1, 6, "", ("127.0.0.1", 443))]
        prev = getattr(_SSRF_TLS, "active", False)
        _SSRF_TLS.active = True
        try:
            with patch("execution.fetch_candidates.socket.getaddrinfo", return_value=fake_addrinfo):
                with self.assertRaises(ValueError):
                    _ssrf_safe_create_connection(("rebind.example.com", 443))
        finally:
            _SSRF_TLS.active = prev

    def test_fetch_feed_items_enforces_streaming_and_max_feed_bytes(self):
        oversized_resp = MagicMock()
        oversized_resp.status_code = 200
        oversized_resp.iter_content.return_value = [b"x" * (MAX_FEED_BYTES + 1)]
        oversized_resp.__enter__ = MagicMock(return_value=oversized_resp)
        oversized_resp.__exit__ = MagicMock(return_value=False)

        with patch("execution.fetch_candidates.is_safe_http_url", return_value=True), \
             patch("execution.fetch_candidates._safe_http_get", return_value=oversized_resp) as mock_get:
            items = fetch_feed_items({"url": "https://example.com/rss", "nome": "TestFeed", "pais": "US"})
            self.assertEqual(items, [])
            mock_get.assert_called_once()
            self.assertTrue(mock_get.call_args.kwargs.get("stream"))


class TestLisbonSchedule(unittest.TestCase):
    def test_winter_wet_utc_plus_0(self):
        self.assertTrue(should_run_at_lisbon_hour(datetime(2026, 12, 15, 10, 5, tzinfo=timezone.utc)))
        self.assertFalse(should_run_at_lisbon_hour(datetime(2026, 12, 15, 9, 5, tzinfo=timezone.utc)))

    def test_summer_west_utc_plus_1(self):
        self.assertTrue(should_run_at_lisbon_hour(datetime(2026, 7, 15, 9, 5, tzinfo=timezone.utc)))
        self.assertFalse(should_run_at_lisbon_hour(datetime(2026, 7, 15, 10, 5, tzinfo=timezone.utc)))


class TestAnnualCleanupAndLocalStorage(unittest.TestCase):
    def test_validate_date_id_and_filter_year(self):
        self.assertTrue(validate_date_id("2026-11-20"))
        self.assertFalse(validate_date_id("2026-13-40"))
        self.assertFalse(validate_date_id("latest"))
        kept, removed = filter_dates_for_current_year(
            ["2026-01-15", "2026-12-31", "2027-01-01"], current_year=2027
        )
        self.assertEqual(kept, ["2027-01-01"])
        self.assertEqual(removed, ["2026-01-15", "2026-12-31"])

    def test_january_cleanup_deletes_only_previous_years_and_preserves_latest(self):
        db = _FakeFirestoreDB()
        db.collections["edicoes"] = {
            "2026-05-10": {"data": "2026-05-10", "ano": 2026},
            "2026-12-31": {"data": "2026-12-31", "ano": 2026},
            "2027-01-01": {"data": "2027-01-01", "ano": 2027},
            "latest": {"data": "2027-01-01", "ano": 2027},
        }
        db.collections["meta"] = {
            "indice": {"datas_disponiveis": ["2027-01-01", "2026-12-31", "2026-05-10"]}
        }

        res = cleanup_previous_years_if_january(db, reference_date=date(2027, 1, 1))
        self.assertTrue(res["executed"])
        self.assertEqual(res["deleted_dates"], ["2026-05-10", "2026-12-31"])
        self.assertIn("2027-01-01", db.collections["edicoes"])
        self.assertIn("latest", db.collections["edicoes"])
        self.assertNotIn("2026-12-31", db.collections["edicoes"])
        self.assertEqual(db.collections["meta"]["indice"]["datas_disponiveis"], ["2027-01-01"])

    def test_non_january_does_not_cleanup(self):
        db = _FakeFirestoreDB()
        db.collections["edicoes"] = {"2025-12-31": {"data": "2025-12-31", "ano": 2025}}
        res = cleanup_previous_years_if_january(db, reference_date=date(2026, 11, 20))
        self.assertFalse(res["executed"])
        self.assertIn("2025-12-31", db.collections["edicoes"])

    def test_local_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            curated = {k: [] for k in CATEGORIES_META}
            briefing = build_fallback_briefing(curated, "2026-11-20")
            edition = build_edition_document("2026-11-20", curated, briefing, "gemini-2.5-flash")
            save_edition_locally(edition, base_dir=tmp)
            self.assertEqual(list_local_dates(tmp), ["2026-11-20"])
            loaded = load_local_edition("2026-11-20", base_dir=tmp)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["data"], "2026-11-20")
            self.assertEqual(loaded["ano"], 2026)

    def test_backfill_older_date_does_not_overwrite_latest(self):
        db = _FakeFirestoreDB()
        curated = {k: [] for k in CATEGORIES_META}
        ed_new = build_edition_document("2026-11-20", curated, build_fallback_briefing(curated, "2026-11-20"), "gemini-2.5-flash")
        ed_old = build_edition_document("2026-11-18", curated, build_fallback_briefing(curated, "2026-11-18"), "gemini-2.5-flash")

        save_edition_to_firestore(db, ed_new)
        save_edition_to_firestore(db, ed_old)
        self.assertEqual(db.collections["edicoes"]["latest"]["data"], "2026-11-20")
        self.assertEqual(db.collections["meta"]["indice"]["datas_disponiveis"], ["2026-11-20", "2026-11-18"])

        with tempfile.TemporaryDirectory() as tmp:
            save_edition_locally(ed_new, base_dir=tmp)
            save_edition_locally(ed_old, base_dir=tmp)
            latest_local = load_local_edition(None, base_dir=tmp)
            self.assertIsNotNone(latest_local)
            self.assertEqual(latest_local["data"], "2026-11-20")


class TestAIAgentCuration(unittest.TestCase):
    def test_curate_category_preserves_verified_links_and_rewrites_pt_pt(self):
        # Injetar módulo google.genai.types falso caso google-genai não esteja instalado no ambiente local
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")
        fake_types = types.ModuleType("google.genai.types")
        fake_types.GenerateContentConfig = lambda **kwargs: kwargs
        fake_genai.types = fake_types
        fake_google.genai = fake_genai

        sys.modules.setdefault("google", fake_google)
        sys.modules["google.genai"] = fake_genai
        sys.modules["google.genai.types"] = fake_types

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "selecionadas": [
                {
                    "candidate_id": "cand_1",
                    "titulo_pt": "ETF do S&P 500 atinge novo máximo histórico",
                    "resumo_pt": "Os fundos cotados ligados ao S&P 500 registaram entradas recorde esta semana.",
                },
                {
                    "candidate_id": "id_inexistente_alucinado",
                    "titulo_pt": "Ignorar este",
                    "resumo_pt": "Não deve entrar.",
                },
            ]
        })
        mock_client.models.generate_content.return_value = mock_response

        candidates = [
            {
                "id": "cand_1",
                "titulo": "S&P 500 ETF hits record high",
                "resumo": "Original English summary",
                "fonte": "ETF Trends (VettaFi)",
                "pais": "US",
                "data_publicacao": "2026-11-20 09:00",
                "link": "https://www.etftrends.com/sp500-record",
                "imagem": "https://www.etftrends.com/img.jpg",
            }
        ]

        curated = curate_category(
            "economia",
            candidates,
            target_count=1,
            enable_search_fallback=False,
            client=mock_client,
        )
        self.assertEqual(len(curated), 1)
        self.assertEqual(curated[0]["titulo"], "S&P 500 ETF hits record high")
        self.assertEqual(curated[0]["titulo_pt"], "ETF do S&P 500 atinge novo máximo histórico")
        self.assertTrue(curated[0]["is_en"])
        self.assertEqual(curated[0]["pais"], "US")
        self.assertEqual(curated[0]["link"], "https://www.etftrends.com/sp500-record")
        self.assertEqual(curated[0]["imagem"], "https://www.etftrends.com/img.jpg")
        self.assertEqual(curated[0]["fonte"], "ETF Trends (VettaFi)")

    def test_extract_json_block_handles_preamble_brackets_and_trailing_text(self):
        raw = '[Nota editorial]\n{"selecionadas": [{"candidate_id": "c1"}]}\nFim da resposta.'
        parsed = _extract_json_block(raw)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["selecionadas"][0]["candidate_id"], "c1")

    def test_is_approved_domain_enforces_whitelist_and_blocks_br(self):
        domains = CATEGORIES_META["economia"]["approved_domains"]
        self.assertTrue(_is_approved_domain("https://www.ft.com/content/abc", domains))
        self.assertTrue(_is_approved_domain("https://etftrends.com/etf-news", domains))
        self.assertFalse(_is_approved_domain("https://g1.globo.com/economia", domains))
        self.assertFalse(_is_approved_domain("https://noticias.uol.com.br/economia", domains))
        self.assertFalse(_is_approved_domain("https://example.br/news", domains))
        self.assertFalse(_is_approved_domain("https://evilft.com/phishing", domains))
        self.assertFalse(_is_approved_domain("https://evil.com\\@ft.com/news", domains))
        self.assertFalse(_is_approved_domain("https://example.com.br\\@ft.com/news", domains))
        self.assertFalse(_is_approved_domain("https://user:pass@ft.com/news", domains))
        self.assertFalse(_is_approved_domain("https://ft.com:99999/news", domains))

    def test_generate_daily_briefing_falls_back_cleanly_on_error(self):
        broken_client = MagicMock()
        broken_client.models.generate_content.side_effect = RuntimeError("API offline")
        curated = {k: [] for k in CATEGORIES_META}
        with patch("execution.ai_agent.time.sleep"):
            briefing = generate_daily_briefing(curated, "2026-11-20", client=broken_client)
        self.assertIn("2026-11-20", briefing["manchete"])
        self.assertEqual(len(briefing["pontos_chave"]), 5)

    def test_create_genai_client_sets_90s_timeout(self):
        mock_client_cls = MagicMock()
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")
        fake_genai.Client = mock_client_cls
        fake_google.genai = fake_genai

        with patch.dict(sys.modules, {"google": fake_google, "google.genai": fake_genai}):
            _create_genai_client(api_key="test-key-123")

        mock_client_cls.assert_called_once_with(
            api_key="test-key-123",
            http_options={"timeout": GEMINI_HTTP_TIMEOUT_MS},
        )
        self.assertEqual(GEMINI_HTTP_TIMEOUT_MS, 90000)

    def test_build_model_chain_prioritizes_custom_model_without_duplicates(self):
        chain_default = _build_model_chain("gemini-3.5-flash")
        self.assertEqual(
            chain_default,
            list(FALLBACK_MODELS),
        )
        chain_custom = _build_model_chain("gemini-custom-test")
        self.assertEqual(chain_custom[0], "gemini-custom-test")
        self.assertEqual(chain_custom[1:], list(FALLBACK_MODELS))

    def test_curate_category_falls_back_across_models_on_503_504_and_429(self):
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")
        fake_types = types.ModuleType("google.genai.types")
        fake_types.GenerateContentConfig = lambda **kwargs: kwargs
        fake_genai.types = fake_types
        fake_google.genai = fake_genai

        called_models: list[str] = []

        def fake_generate_content(model, contents, config):
            called_models.append(model)
            if model == "gemini-3.5-flash":
                raise RuntimeError("504 DEADLINE_EXCEEDED: Gateway Timeout")
            if model == "gemini-3.5-flash-lite":
                raise RuntimeError("503 UNAVAILABLE: high demand")
            if model == "gemini-3.8-flash":
                raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
            resp = MagicMock()
            resp.text = json.dumps({
                "selecionadas": [
                    {
                        "candidate_id": "cand_1",
                        "titulo_pt": "Título curado pelo modelo de fallback",
                        "resumo_pt": "Resumo em PT-PT.",
                    }
                ]
            })
            return resp

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = fake_generate_content

        candidates = [
            {
                "id": "cand_1",
                "titulo": "Original title",
                "resumo": "Original summary",
                "fonte": "FT",
                "pais": "UK",
                "data_publicacao": "2026-11-20 09:00",
                "link": "https://www.ft.com/content/1",
                "imagem": "",
            }
        ]

        with patch("execution.ai_agent.time.sleep"), patch.dict(
            sys.modules,
            {"google": fake_google, "google.genai": fake_genai, "google.genai.types": fake_types},
        ):
            curated = curate_category(
                "economia",
                candidates,
                target_count=1,
                model="gemini-3.5-flash",
                enable_search_fallback=False,
                client=mock_client,
            )

        self.assertEqual(
            called_models,
            ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash"],
        )
        self.assertEqual(len(curated), 1)
        self.assertEqual(curated[0]["origem_curadoria"], "rss_gemini")
        self.assertEqual(curated[0]["titulo"], "Original title")
        self.assertEqual(curated[0]["titulo_pt"], "Título curado pelo modelo de fallback")

    def test_curate_category_limits_pool_to_35_and_truncates_summary(self):
        fake_google = types.ModuleType("google")
        fake_genai = types.ModuleType("google.genai")
        fake_types = types.ModuleType("google.genai.types")
        fake_types.GenerateContentConfig = lambda **kwargs: kwargs
        fake_genai.types = fake_types
        fake_google.genai = fake_genai

        captured_prompts: list[str] = []

        def fake_generate_content(model, contents, config):
            captured_prompts.append(contents)
            resp = MagicMock()
            resp.text = json.dumps({"selecionadas": []})
            return resp

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = fake_generate_content

        long_summary = "A" * 400
        candidates = [
            {
                "id": f"cand_{i}",
                "titulo": f"Notícia {i}",
                "resumo": long_summary,
                "fonte": "FT",
                "pais": "UK",
                "data_publicacao": "2026-11-20 09:00",
                "link": f"https://www.ft.com/content/{i}",
                "imagem": "",
            }
            for i in range(50)
        ]

        with patch.dict(
            sys.modules,
            {"google": fake_google, "google.genai": fake_genai, "google.genai.types": fake_types},
        ):
            curate_category(
                "economia",
                candidates,
                target_count=5,
                enable_search_fallback=False,
                client=mock_client,
            )

        self.assertEqual(len(captured_prompts), 1)
        prompt = captured_prompts[0]
        json_part = prompt.split("Candidatos JSON:\n", 1)[1]
        sent_candidates = json.loads(json_part)
        self.assertEqual(len(sent_candidates), CANDIDATE_POOL_PER_CATEGORY)
        self.assertEqual(CANDIDATE_POOL_PER_CATEGORY, 35)
        self.assertTrue(all(len(c["resumo"]) <= MAX_CANDIDATE_SUMMARY_CHARS for c in sent_candidates))
        self.assertTrue(sent_candidates[0]["resumo"].endswith("…"))


from execution.github_trigger import (
    fetch_latest_workflow_run,
    get_lisbon_today_str,
    trigger_noticias_workflow,
)


class TestGitHubTrigger(unittest.TestCase):
    def test_get_lisbon_today_str_respects_timezone(self):
        # 23:30 UTC in winter is still 23:30 in Lisbon; in summer (UTC+1) it's next day
        winter_dt = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
        summer_dt = datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(get_lisbon_today_str(winter_dt), "2026-01-15")
        self.assertEqual(get_lisbon_today_str(summer_dt), "2026-07-16")

    def test_trigger_workflow_requires_token(self):
        ok, err = trigger_noticias_workflow("")
        self.assertFalse(ok)
        self.assertIn("GITHUB_TOKEN", err)

    def test_trigger_workflow_success_on_204(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        with patch("execution.github_trigger.requests.post", return_value=mock_resp) as mock_post:
            ok, err = trigger_noticias_workflow("ghp_test_token")
        self.assertTrue(ok)
        self.assertIsNone(err)
        mock_post.assert_called_once()

    def test_fetch_latest_workflow_run_filters_created_after(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "workflow_runs": [
                {
                    "id": 200,
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/davidmvg05/Noticias_IA/actions/runs/200",
                    "created_at": "2026-11-20T23:10:00Z",
                },
                {
                    "id": 100,
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/davidmvg05/Noticias_IA/actions/runs/100",
                    "created_at": "2026-11-20T10:00:00Z",
                },
            ]
        }
        with patch("execution.github_trigger.requests.get", return_value=mock_resp):
            run, err = fetch_latest_workflow_run("ghp_test_token", created_after="2026-11-20T23:05:00Z")
        self.assertIsNone(err)
        self.assertIsNotNone(run)
        self.assertEqual(run["id"], 200)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["conclusion"], "success")


if __name__ == "__main__":
    unittest.main()
