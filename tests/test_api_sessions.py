"""Consent-gated sessions: creation, Bearer auth, isolation, deletion, retention.

Time is driven by the ``clock`` fixture (an injected ``get_now``), so expiry
and the session-risk window are tested without sleeping.
"""

import hashlib
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import session_store as storage
from crisis_safety import CRISIS_RESOURCES
from tests.fakes import CONSENT, bearer, model_json, open_session, running_app

DAY = 86_400
HOUR = 3_600
CRISIS_TEXT = "quiero suicidarme"
HIGH_TEXT = "ya no aguanto"
BENIGN_TEXT = "hoy fue un buen día"
HOTLINE = "Línea de la Vida: 800 911 2000"


def analizar(client, texto, headers=None):
    return client.post("/analizar", json={"texto": texto}, headers=headers)


def query(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def digest_of(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ─── POST /sesion ───────────────────────────────────────────────


def test_sesion_returns_201_with_a_url_safe_token_and_the_retention(anon_client):
    response = anon_client.post("/sesion", json=CONSENT)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"token", "expira_en_dias"}
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body["token"])  # token_urlsafe(32)
    assert body["expira_en_dias"] == 30


def test_every_session_gets_its_own_token(anon_client):
    assert open_session(anon_client) != open_session(anon_client)


def test_only_the_sha256_of_the_token_is_stored(anon_client, db_path):
    token = open_session(anon_client)
    analizar(anon_client, "hola", headers=bearer(token))

    digest = digest_of(token)
    assert query(db_path, "SELECT token_hash FROM sessions") == [(digest,)]
    assert {r[0] for r in query(db_path, "SELECT token_hash FROM messages")} == {digest}
    assert token.encode() not in db_path.read_bytes()


def test_the_session_records_the_consent_version_and_a_fixed_expiry(
    anon_client, db_path, clock
):
    open_session(anon_client, version_consentimiento="2026-10-v9")

    assert query(
        db_path, "SELECT created_at, expires_at, consent_version FROM sessions"
    ) == [(clock(), clock() + 30 * DAY, "2026-10-v9")]


def test_the_retention_comes_from_the_environment(anon_client, db_path, clock, monkeypatch):
    monkeypatch.setenv("PSICOLOBOT_RETENTION_DAYS", "7")

    response = anon_client.post("/sesion", json=CONSENT)

    assert response.json()["expira_en_dias"] == 7
    assert query(db_path, "SELECT expires_at - created_at FROM sessions") == [(7 * DAY,)]


@pytest.mark.parametrize(
    "body",
    [
        {**CONSENT, "acepta_terminos": False},
        {**CONSENT, "mayor_de_edad": False},
        {**CONSENT, "acepta_terminos": False, "mayor_de_edad": False},
        {**CONSENT, "acepta_terminos": "true"},
        {**CONSENT, "mayor_de_edad": 1},
        {**CONSENT, "mayor_de_edad": None},
        {"acepta_terminos": True, "mayor_de_edad": True},
        {"acepta_terminos": True, "version_consentimiento": "v1"},
        {**CONSENT, "version_consentimiento": ""},
        {**CONSENT, "version_consentimiento": "   "},
        {**CONSENT, "version_consentimiento": "v" * 41},
        {**CONSENT, "version_consentimiento": 3},
        {},
    ],
)
def test_sesion_is_422_without_explicit_consent_and_creates_nothing(
    anon_client, db_path, body
):
    response = anon_client.post("/sesion", json=body)

    assert response.status_code == 422
    assert query(db_path, "SELECT * FROM sessions") == []


def test_a_consent_version_of_40_characters_is_accepted(anon_client):
    response = anon_client.post(
        "/sesion", json={**CONSENT, "version_consentimiento": "v" * 40}
    )

    assert response.status_code == 201


def test_unknown_fields_in_the_consent_body_are_ignored(anon_client):
    response = anon_client.post("/sesion", json={**CONSENT, "extra": "x"})

    assert response.status_code == 201


# ─── Bearer authentication ──────────────────────────────────────

PROTECTED = [
    ("POST", "/analizar", {"json": {"texto": BENIGN_TEXT}}),
    ("DELETE", "/sesion", {}),
]


def call(client, method, path, kwargs, headers=None):
    return client.request(method, path, headers=headers, **kwargs)


@pytest.mark.parametrize("method, path, kwargs", PROTECTED)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer token-que-no-existe"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "token-sin-esquema"},
    ],
)
def test_missing_or_unknown_credentials_are_401_with_a_bearer_challenge(
    anon_client, fake_generator, method, path, kwargs, headers
):
    response = call(anon_client, method, path, kwargs, headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert fake_generator.calls == []


def test_authentication_is_checked_before_the_request_body(anon_client):
    response = anon_client.post("/analizar", json={"texto": ""})

    assert response.status_code == 401


@pytest.mark.parametrize("method, path, kwargs", PROTECTED)
def test_an_expired_token_is_401(anon_client, clock, method, path, kwargs):
    token = open_session(anon_client)
    clock.advance(days=30, seconds=1)

    response = call(anon_client, method, path, kwargs, bearer(token))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_the_token_works_until_the_last_second_and_expiry_is_not_sliding(
    anon_client, clock
):
    token = open_session(anon_client)

    clock.advance(days=29)
    assert analizar(anon_client, BENIGN_TEXT, bearer(token)).status_code == 200
    clock.advance(days=1, seconds=-1)  # day 30 minus one second since creation
    assert analizar(anon_client, BENIGN_TEXT, bearer(token)).status_code == 200
    clock.advance(seconds=1)  # exactly 30 days: using it did not extend it
    assert analizar(anon_client, BENIGN_TEXT, bearer(token)).status_code == 401


@pytest.mark.parametrize("method, path, kwargs", PROTECTED)
def test_a_deleted_session_stops_working(anon_client, method, path, kwargs):
    token = open_session(anon_client)
    assert anon_client.delete("/sesion", headers=bearer(token)).status_code == 204

    response = call(anon_client, method, path, kwargs, bearer(token))

    assert response.status_code == 401


def test_health_stays_open_without_a_token(anon_client):
    assert anon_client.get("/health").status_code == 200


def test_a_session_id_in_the_body_is_ignored_and_not_in_the_schema(
    client, db_path, session_token
):
    response = client.post(
        "/analizar", json={"texto": BENIGN_TEXT, "session_id": "default_session"}
    )

    assert response.status_code == 200
    assert {r[0] for r in query(db_path, "SELECT token_hash FROM messages")} == {
        digest_of(session_token)
    }
    schema = client.get("/openapi.json").json()
    assert "session_id" not in schema["components"]["schemas"]["PostRequest"]["properties"]


# ─── Optional API key ───────────────────────────────────────────


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("PSICOLOBOT_API_KEY", "clave-de-prueba")
    return {"X-API-Key": "clave-de-prueba"}


def test_no_api_key_is_needed_when_none_is_configured(anon_client):
    assert anon_client.post("/sesion", json=CONSENT).status_code == 201


def test_health_is_always_open(anon_client, api_key):
    assert anon_client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": "otra-clave"}, {"X-API-Key": ""}, {"X-API-Key": "clave-de-prueba "}],
)
def test_sesion_requires_the_api_key_when_configured(anon_client, api_key, db_path, headers):
    response = anon_client.post("/sesion", json=CONSENT, headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "ApiKey"
    assert query(db_path, "SELECT * FROM sessions") == []


def test_a_correct_api_key_opens_the_session_endpoint(anon_client, api_key):
    assert anon_client.post("/sesion", json=CONSENT, headers=api_key).status_code == 201


def test_the_api_key_is_also_required_to_analyze_and_to_delete(
    anon_client, api_key, fake_generator
):
    token = open_session(anon_client, headers=api_key)

    no_key = analizar(anon_client, BENIGN_TEXT, bearer(token))
    wrong_key = anon_client.delete(
        "/sesion", headers={**bearer(token), "X-API-Key": "mal"}
    )

    assert no_key.status_code == 401
    assert no_key.headers["www-authenticate"] == "ApiKey"
    assert wrong_key.status_code == 401
    assert fake_generator.calls == []
    ok = analizar(anon_client, BENIGN_TEXT, {**bearer(token), **api_key})
    assert ok.status_code == 200
    assert anon_client.delete("/sesion", headers={**bearer(token), **api_key}).status_code == 204


def test_a_valid_api_key_does_not_replace_the_session_token(anon_client, api_key):
    response = analizar(anon_client, BENIGN_TEXT, api_key)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_non_ascii_api_key_header_is_a_401_not_a_crash(anon_client, api_key):
    response = anon_client.post(
        "/sesion", json=CONSENT, headers={"X-API-Key": "clavé".encode("latin-1")}
    )

    assert response.status_code == 401


# ─── Isolation and history ──────────────────────────────────────


def test_one_user_never_sees_the_history_of_another(anon_client, fake_generator):
    user_a = bearer(open_session(anon_client))
    user_b = bearer(open_session(anon_client))

    analizar(anon_client, "secreto de A", user_a)
    analizar(anon_client, "mensaje de B", user_b)
    analizar(anon_client, "otra vez A", user_a)

    from_b, from_a_again = fake_generator.user_content(1), fake_generator.user_content(2)
    assert "secreto de A" not in from_b
    assert "Historial reciente" not in from_b
    assert '- Paciente dijo: "secreto de A"' in from_a_again
    assert "mensaje de B" not in from_a_again


def test_the_history_is_the_last_four_messages_of_the_own_session(
    anon_client, fake_generator
):
    headers = bearer(open_session(anon_client))
    fake_generator.respond_with(model_json(respuesta="respuesta uno"), model_json(respuesta="respuesta dos"), model_json(respuesta="respuesta tres"), model_json(respuesta="respuesta cuatro"), model_json())
    for number in range(1, 6):
        analizar(anon_client, f"mensaje {number}", headers)

    prompt = fake_generator.user_content(4)
    assert '- Paciente dijo: "mensaje 3"' in prompt
    assert '- Paciente dijo: "mensaje 4"' in prompt
    assert "respuesta tres" in prompt and "respuesta cuatro" in prompt
    assert "mensaje 2" not in prompt and "mensaje 1" not in prompt
    assert "respuesta uno" not in prompt and "respuesta dos" not in prompt
    assert prompt.count("- Paciente dijo:") == 2
    assert prompt.count("- PsicoloBot respondió") == 2


def test_stored_messages_carry_their_creation_time(client, db_path, clock):
    analizar(client, "hola")
    clock.advance(seconds=90)
    analizar(client, "otra vez")

    times = [r[0] for r in query(db_path, "SELECT created_at FROM messages ORDER BY id")]
    assert times == [clock() - 90] * 2 + [clock()] * 2


# ─── DELETE /sesion ─────────────────────────────────────────────


def test_delete_returns_204_and_removes_the_session_with_its_messages(
    anon_client, db_path
):
    mine = open_session(anon_client)
    other = open_session(anon_client)
    analizar(anon_client, "mío", bearer(mine))
    analizar(anon_client, "ajeno", bearer(other))

    response = anon_client.delete("/sesion", headers=bearer(mine))

    assert response.status_code == 204
    assert response.content == b""
    assert query(db_path, "SELECT token_hash FROM sessions") == [(digest_of(other),)]
    assert {r[0] for r in query(db_path, "SELECT token_hash FROM messages")} == {
        digest_of(other)
    }
    assert analizar(anon_client, "hola", bearer(mine)).status_code == 401
    assert anon_client.delete("/sesion", headers=bearer(mine)).status_code == 401
    assert analizar(anon_client, "hola", bearer(other)).status_code == 200


def test_a_session_deleted_while_the_model_runs_leaves_no_messages(
    anon_client, db_path, fake_generator
):
    token = open_session(anon_client)

    class DeletingGenerator:
        def generate(self, messages):
            storage.delete_session(digest_of(token))
            return model_json()

    import psicolobot_api

    psicolobot_api.app.dependency_overrides[psicolobot_api.get_generator] = (
        lambda: DeletingGenerator()
    )

    response = analizar(anon_client, "texto que no debe quedarse", bearer(token))

    assert response.status_code == 200
    assert query(db_path, "SELECT * FROM messages") == []


# ─── Retention ──────────────────────────────────────────────────


def test_creating_a_session_purges_expired_sessions_and_their_messages(
    anon_client, db_path, clock
):
    old = open_session(anon_client)
    analizar(anon_client, "viejo", bearer(old))
    clock.advance(days=31)

    fresh = open_session(anon_client)

    assert query(db_path, "SELECT token_hash FROM sessions") == [(digest_of(fresh),)]
    assert query(db_path, "SELECT * FROM messages") == []


def test_startup_purges_what_expired_while_the_server_was_down(
    db_path, fake_generator, clock
):
    expired = storage.create_session("v1", clock(), 30)
    storage.record_exchange(digest_of(expired), "viejo", "{}", "BAJO", clock(), 86_400)
    alive = storage.create_session("v1", clock() + 20 * DAY, 30)
    clock.advance(days=31)

    with running_app(fake_generator, clock):
        pass

    assert query(db_path, "SELECT token_hash FROM sessions") == [(digest_of(alive),)]
    assert query(db_path, "SELECT * FROM messages") == []


def test_the_legacy_memory_table_survives_every_operation(
    db_path, fake_generator, clock
):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE memory (session_id TEXT, role TEXT, content TEXT)")
    conn.execute("INSERT INTO memory VALUES ('default_session', 'user', 'texto viejo')")
    conn.commit()
    conn.close()

    with running_app(fake_generator, clock) as client:
        token = open_session(client)
        analizar(client, "hola", bearer(token))
        clock.advance(days=40)
        client.delete("/sesion", headers=bearer(token))
        open_session(client)

    assert query(db_path, "SELECT * FROM memory") == [
        ("default_session", "user", "texto viejo")
    ]
    assert "texto viejo" not in {r[0] for r in query(db_path, "SELECT content FROM messages")}


# ─── Session risk ───────────────────────────────────────────────


def test_a_calm_message_after_a_crisis_keeps_the_session_risk_and_the_resources(
    client, fake_generator, clock
):
    fake_generator.respond_with(model_json(riesgo="BAJO"))
    first = analizar(client, CRISIS_TEXT).json()
    clock.advance(hours=2)
    second = analizar(client, BENIGN_TEXT).json()

    assert (first["riesgo"], first["riesgo_sesion"]) == ("CRITICO", "CRITICO")
    assert second["riesgo"] == "BAJO"
    assert second["riesgo_sesion"] == "CRITICO"
    assert second["recursos_crisis"] == list(CRISIS_RESOURCES)
    assert second["guardrail_activado"] is False
    assert HOTLINE not in second["respuesta"]


def test_the_next_prompt_carries_a_safety_note_after_a_high_risk(
    client, fake_generator, clock
):
    analizar(client, CRISIS_TEXT)
    clock.advance(hours=2)
    analizar(client, BENIGN_TEXT)

    first, second = fake_generator.user_content(0), fake_generator.user_content(1)
    assert "Nota de seguridad" not in first
    assert "Nota de seguridad:" in second
    assert "24 horas" in second
    assert "especial cuidado" in second
    # The note sits between the history and the current message.
    assert second.index("Historial reciente") < second.index("Nota de seguridad:")
    assert second.index("Nota de seguridad:") < second.index("Mensaje actual del usuario:")
    assert second.endswith("Mensaje actual del usuario:\n" + BENIGN_TEXT)


def test_the_safety_note_also_appears_after_a_high_risk_without_history(
    anon_client, fake_generator, clock, db_path
):
    token = open_session(anon_client)
    storage.record_exchange(
        digest_of(token), "u", "a", "ALTO", clock() - HOUR, 24 * HOUR
    )
    # Drop the history so only the session risk remains.
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM messages")
    conn.commit()
    conn.close()

    analizar(anon_client, BENIGN_TEXT, bearer(token))

    prompt = fake_generator.user_content()
    assert "Historial reciente" not in prompt
    assert "Nota de seguridad:" in prompt


def test_a_medio_session_risk_adds_neither_a_note_nor_resources(
    client, fake_generator, clock
):
    fake_generator.respond_with(model_json(riesgo="MEDIO"), model_json(riesgo="BAJO"))
    analizar(client, BENIGN_TEXT)
    clock.advance(hours=1)
    body = analizar(client, BENIGN_TEXT).json()

    assert body["riesgo"] == "BAJO"
    assert body["riesgo_sesion"] == "MEDIO"
    assert body["recursos_crisis"] == []
    assert "Nota de seguridad" not in fake_generator.user_content(1)


def test_a_calmer_message_after_alto_still_reports_alto(client, fake_generator, clock):
    fake_generator.respond_with(model_json(riesgo="ALTO"), model_json(riesgo="MEDIO"))
    analizar(client, BENIGN_TEXT)
    clock.advance(hours=3)
    body = analizar(client, BENIGN_TEXT).json()

    assert (body["riesgo"], body["riesgo_sesion"]) == ("MEDIO", "ALTO")
    assert body["recursos_crisis"]
    assert "Nota de seguridad:" in fake_generator.user_content(1)


def test_the_session_risk_returns_to_the_current_risk_after_24_hours(
    client, fake_generator, clock
):
    analizar(client, CRISIS_TEXT)

    clock.advance(hours=24, seconds=-1)
    inside = analizar(client, BENIGN_TEXT).json()
    clock.advance(seconds=1)  # exactly 24 h after the crisis message
    outside = analizar(client, BENIGN_TEXT).json()

    assert inside["riesgo_sesion"] == "CRITICO"
    assert (outside["riesgo"], outside["riesgo_sesion"]) == ("BAJO", "BAJO")
    assert outside["recursos_crisis"] == []
    assert "Nota de seguridad" not in fake_generator.user_content(-1)


def test_a_new_high_risk_message_restarts_the_window(client, clock):
    analizar(client, CRISIS_TEXT)
    clock.advance(hours=20)
    analizar(client, CRISIS_TEXT)
    clock.advance(hours=10)  # 30 h after the first, 10 h after the second

    body = analizar(client, BENIGN_TEXT).json()

    assert body["riesgo_sesion"] == "CRITICO"


def test_a_lower_risk_message_does_not_extend_the_window(client, clock):
    analizar(client, CRISIS_TEXT)
    clock.advance(hours=20)
    during = analizar(client, HIGH_TEXT).json()  # ALTO < CRITICO: not stored
    clock.advance(hours=5)  # 25 h after the crisis

    after = analizar(client, BENIGN_TEXT).json()

    assert (during["riesgo"], during["riesgo_sesion"]) == ("ALTO", "CRITICO")
    assert after["riesgo_sesion"] == "BAJO"


def test_the_risk_window_hours_come_from_the_environment(
    client, clock, monkeypatch
):
    monkeypatch.setenv("PSICOLOBOT_SESSION_RISK_HOURS", "2")
    analizar(client, CRISIS_TEXT)

    clock.advance(hours=2, seconds=-1)
    inside = analizar(client, BENIGN_TEXT).json()
    clock.advance(seconds=1)
    outside = analizar(client, BENIGN_TEXT).json()

    assert inside["riesgo_sesion"] == "CRITICO"
    assert outside["riesgo_sesion"] == "BAJO"


def test_the_session_risk_is_persisted_with_its_timestamp(client, db_path, clock):
    analizar(client, CRISIS_TEXT)

    assert query(db_path, "SELECT max_risk, max_risk_at FROM sessions") == [
        ("CRITICO", clock())
    ]


def test_one_users_crisis_does_not_leak_into_another_session(
    anon_client, fake_generator
):
    user_a = bearer(open_session(anon_client))
    user_b = bearer(open_session(anon_client))
    analizar(anon_client, CRISIS_TEXT, user_a)

    body = analizar(anon_client, BENIGN_TEXT, user_b).json()

    assert (body["riesgo"], body["riesgo_sesion"]) == ("BAJO", "BAJO")
    assert body["recursos_crisis"] == []
    assert "Nota de seguridad" not in fake_generator.user_content(-1)


def test_a_failing_model_still_remembers_and_reports_the_session_risk(
    client, failing_generator, clock
):
    first = analizar(client, CRISIS_TEXT).json()
    clock.advance(hours=1)
    second = analizar(client, BENIGN_TEXT).json()

    assert first["riesgo_sesion"] == "CRITICO"
    assert (second["riesgo"], second["riesgo_sesion"]) == ("MEDIO", "CRITICO")
    assert second["recursos_crisis"]


# ─── CORS ───────────────────────────────────────────────────────

PREFLIGHT = {
    "Origin": "https://app.example",
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "authorization,content-type,x-api-key",
}


def test_there_are_no_cors_headers_by_default(db_path, monkeypatch):
    monkeypatch.delenv("PSICOLOBOT_CORS_ORIGINS", raising=False)
    import psicolobot_api

    app = psicolobot_api.create_app()
    with TestClient(app) as default_client:
        simple = default_client.get("/health", headers={"Origin": "https://app.example"})
        preflight = default_client.options("/analizar", headers=PREFLIGHT)

    assert "access-control-allow-origin" not in simple.headers
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.parametrize("value", ["", "  ", " , ,"])
def test_an_empty_cors_configuration_allows_no_origin(db_path, monkeypatch, value):
    monkeypatch.setenv("PSICOLOBOT_CORS_ORIGINS", value)
    import psicolobot_api

    with TestClient(psicolobot_api.create_app()) as test_client:
        response = test_client.get("/health", headers={"Origin": "https://app.example"})

    assert "access-control-allow-origin" not in response.headers


def test_only_the_configured_origins_are_allowed(db_path, monkeypatch):
    monkeypatch.setenv(
        "PSICOLOBOT_CORS_ORIGINS", "https://app.example, https://otra.example/"
    )
    import psicolobot_api

    with TestClient(psicolobot_api.create_app()) as test_client:
        first = test_client.get("/health", headers={"Origin": "https://app.example"})
        second = test_client.get("/health", headers={"Origin": "https://otra.example"})
        denied = test_client.get("/health", headers={"Origin": "https://evil.example"})

    assert first.headers["access-control-allow-origin"] == "https://app.example"
    assert second.headers["access-control-allow-origin"] == "https://otra.example"
    assert "access-control-allow-origin" not in denied.headers
    assert "access-control-allow-credentials" not in first.headers


def test_the_preflight_lists_only_the_needed_methods_and_headers(db_path, monkeypatch):
    monkeypatch.setenv("PSICOLOBOT_CORS_ORIGINS", "https://app.example")
    import psicolobot_api

    with TestClient(psicolobot_api.create_app()) as test_client:
        response = test_client.options("/analizar", headers=PREFLIGHT)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.example"
    assert "access-control-allow-credentials" not in response.headers
    methods = {m.strip() for m in response.headers["access-control-allow-methods"].split(",")}
    headers = {
        h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")
    }
    assert methods == {"GET", "POST", "DELETE"}
    assert {"authorization", "content-type", "x-api-key"} <= headers
    assert "*" not in headers


def test_a_cross_origin_401_exposes_www_authenticate(db_path, monkeypatch):
    # Browsers hide WWW-Authenticate from scripts unless it is exposed, and the
    # web client needs it to tell an expired session from a bad deployment key.
    monkeypatch.setenv("PSICOLOBOT_CORS_ORIGINS", "https://app.example")
    import psicolobot_api

    with TestClient(psicolobot_api.create_app()) as test_client:
        response = test_client.post(
            "/analizar", json={"texto": "hola"}, headers={"Origin": "https://app.example"}
        )

    assert response.status_code == 401
    exposed = {
        h.strip().lower()
        for h in response.headers["access-control-expose-headers"].split(",")
    }
    assert exposed == {"www-authenticate"}


# ─── Network binding ────────────────────────────────────────────


def test_the_server_binds_to_loopback_on_8000_by_default(monkeypatch):
    import psicolobot_api

    monkeypatch.delenv("PSICOLOBOT_HOST", raising=False)
    monkeypatch.delenv("PSICOLOBOT_PORT", raising=False)

    assert psicolobot_api.get_server_address() == ("127.0.0.1", 8000)


def test_the_bind_address_comes_from_the_environment(monkeypatch):
    import psicolobot_api

    monkeypatch.setenv("PSICOLOBOT_HOST", "0.0.0.0")
    monkeypatch.setenv("PSICOLOBOT_PORT", "9001")

    assert psicolobot_api.get_server_address() == ("0.0.0.0", 9001)


@pytest.mark.parametrize("port", ["", "abc", "0", "70000", "-1"])
def test_an_invalid_port_falls_back_to_8000(monkeypatch, port):
    import psicolobot_api

    monkeypatch.setenv("PSICOLOBOT_PORT", port)

    assert psicolobot_api.get_server_address()[1] == 8000


def test_a_blank_host_falls_back_to_loopback(monkeypatch):
    import psicolobot_api

    monkeypatch.setenv("PSICOLOBOT_HOST", "  ")

    assert psicolobot_api.get_server_address()[0] == "127.0.0.1"


# ─── Privacy: tokens are never logged ───────────────────────────


def test_tokens_and_their_hashes_are_never_printed(anon_client, capsys):
    token = open_session(anon_client)
    analizar(anon_client, CRISIS_TEXT, bearer(token))
    analizar(anon_client, BENIGN_TEXT, bearer("token-equivocado-xyz"))
    anon_client.delete("/sesion", headers=bearer(token))
    anon_client.delete("/sesion", headers=bearer(token))

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert token not in output
    assert "token-equivocado-xyz" not in output
    assert digest_of(token) not in output
    assert CRISIS_TEXT not in output
