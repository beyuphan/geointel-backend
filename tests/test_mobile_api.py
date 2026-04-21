"""
tests/test_mobile_api.py — Mobil API Test Suite

Test coverage:
1. JWT token creation/decoding
2. Password hashing/verification
3. ApiResponse envelope structure
4. Schema validation (all request/response models)
5. Auth guard behavior
6. Architecture contracts (endpoint registration, dependency)
"""
import sys
import os
import json
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ORCH_PATH = os.path.join(ROOT, "services", "orchestrator")
if ORCH_PATH not in sys.path:
    sys.path.insert(0, ORCH_PATH)


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 1: JWT TOKEN
# ═══════════════════════════════════════════════════════════════════════════

class TestJWT:
    """JWT token oluşturma ve doğrulama."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from api.deps import create_access_token, create_refresh_token, decode_token
        self.create_access = create_access_token
        self.create_refresh = create_refresh_token
        self.decode = decode_token

    def test_access_token_creation(self):
        token = self.create_access("test-uuid-123", "testuser")
        assert isinstance(token, str)
        assert len(token) > 50

    def test_access_token_decode(self):
        token = self.create_access("user-id-456", "eyup")
        payload = self.decode(token)
        assert payload is not None
        assert payload["sub"] == "user-id-456"
        assert payload["username"] == "eyup"
        assert payload["type"] == "access"

    def test_refresh_token_decode(self):
        token = self.create_refresh("user-id-789")
        payload = self.decode(token)
        assert payload is not None
        assert payload["sub"] == "user-id-789"
        assert payload["type"] == "refresh"

    def test_invalid_token_returns_none(self):
        payload = self.decode("totally.invalid.token")
        assert payload is None

    def test_empty_token_returns_none(self):
        payload = self.decode("")
        assert payload is None

    def test_access_and_refresh_are_different(self):
        access = self.create_access("id1", "user1")
        refresh = self.create_refresh("id1")
        assert access != refresh


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 2: PASSWORD HASHING
# ═══════════════════════════════════════════════════════════════════════════

class TestPasswordHashing:
    """Bcrypt password hashing."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from api.deps import hash_password, verify_password
        self.hash = hash_password
        self.verify = verify_password

    def test_hash_returns_string(self):
        h = self.hash("test123")
        assert isinstance(h, str)
        assert h.startswith("$2b$") or h.startswith("$2a$")

    def test_verify_correct_password(self):
        h = self.hash("mypassword")
        assert self.verify("mypassword", h) is True

    def test_verify_wrong_password(self):
        h = self.hash("correct_pass")
        assert self.verify("wrong_pass", h) is False

    def test_hash_is_different_each_time(self):
        h1 = self.hash("same_pass")
        h2 = self.hash("same_pass")
        assert h1 != h2  # bcrypt salt ensures uniqueness


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 3: API RESPONSE ENVELOPE
# ═══════════════════════════════════════════════════════════════════════════

class TestApiEnvelope:
    """ApiResponse standart envelope yapısı."""

    @pytest.fixture(autouse=True)
    def setup(self):
        from api.schemas import ApiResponse, ApiError, ApiMetadata
        self.ApiResponse = ApiResponse
        self.ApiError = ApiError
        self.ApiMetadata = ApiMetadata

    def test_success_response(self):
        resp = self.ApiResponse(
            success=True,
            data={"message": "test"},
            metadata=self.ApiMetadata(response_time_ms=42),
        )
        d = resp.model_dump()
        assert d["success"] is True
        assert d["data"]["message"] == "test"
        assert d["error"] is None
        assert d["metadata"]["api_version"] == "v1"
        assert d["metadata"]["response_time_ms"] == 42

    def test_error_response(self):
        resp = self.ApiResponse(
            success=False,
            error=self.ApiError(code="AUTH_FAILED", message="Hatalı şifre"),
        )
        d = resp.model_dump()
        assert d["success"] is False
        assert d["data"] is None
        assert d["error"]["code"] == "AUTH_FAILED"
        assert d["error"]["message"] == "Hatalı şifre"

    def test_metadata_defaults(self):
        meta = self.ApiMetadata()
        assert meta.api_version == "v1"
        assert meta.response_time_ms is None
        assert meta.session_id is None


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 4: SCHEMA VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:
    """Request/Response model validation."""

    def test_register_request_min_length(self):
        from api.schemas import RegisterRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RegisterRequest(username="ab", password="short")  # username < 3

    def test_register_request_valid(self):
        from api.schemas import RegisterRequest
        req = RegisterRequest(username="eyup", password="strongpass")
        assert req.username == "eyup"

    def test_chat_request_empty_message(self):
        from api.schemas import ChatRequest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ChatRequest(message="")  # min_length=1

    def test_chat_request_valid(self):
        from api.schemas import ChatRequest
        req = ChatRequest(message="Merhaba", session_id="s1")
        assert req.message == "Merhaba"
        assert req.session_id == "s1"

    def test_vehicle_update_fuel_type(self):
        from api.schemas import VehicleUpdate
        v = VehicleUpdate(
            brand="Toyota", model="Corolla", year=2022,
            fuel_type="gasoline",
            city_consumption=7.5, highway_consumption=5.2,
        )
        assert v.fuel_type == "gasoline"

    def test_vehicle_update_year_range(self):
        from api.schemas import VehicleUpdate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VehicleUpdate(
                brand="X", model="Y", year=1800,  # < 1990
                fuel_type="diesel",
                city_consumption=8, highway_consumption=6,
            )

    def test_location_create(self):
        from api.schemas import LocationCreate
        loc = LocationCreate(name="Ev", coordinates="41.0,29.0", category="home")
        assert loc.name == "Ev"
        assert loc.category == "home"

    def test_map_marker(self):
        from api.schemas import MapMarker
        m = MapMarker(lat=41.0, lon=29.0, title="Başlangıç", type="origin", icon="pin_green")
        assert m.lat == 41.0
        assert m.type == "origin"

    def test_action_card(self):
        from api.schemas import ActionCard
        c = ActionCard(id="nav", label="Başlat", action="start_navigation", icon="navigation")
        assert c.style == "secondary"  # default

    def test_chat_response_structure(self):
        from api.schemas import ChatResponse, MapData
        resp = ChatResponse(
            message="Rota hazır",
            intent={"category": "routing"},
            map=MapData(polyline="encoded..."),
            model_used="claude",
        )
        assert resp.message == "Rota hazır"
        assert resp.map.polyline == "encoded..."

    def test_profile_response(self):
        from api.schemas import ProfileResponse, UserPublic
        p = ProfileResponse(
            user=UserPublic(id="uuid", username="test"),
            locations=[],
            preferences=[],
        )
        assert p.user.username == "test"
        assert p.vehicle is None


# ═══════════════════════════════════════════════════════════════════════════
# TEST GROUP 5: ARCHITECTURE CONTRACTS
# ═══════════════════════════════════════════════════════════════════════════

class TestMobileArchitecture:
    """Mimari kurallar."""

    def test_auth_router_exists(self):
        auth_path = os.path.join(ORCH_PATH, "api", "auth.py")
        assert os.path.exists(auth_path)

    def test_profile_router_exists(self):
        profile_path = os.path.join(ORCH_PATH, "api", "profile.py")
        assert os.path.exists(profile_path)

    def test_history_router_exists(self):
        history_path = os.path.join(ORCH_PATH, "api", "history.py")
        assert os.path.exists(history_path)

    def test_schemas_has_envelope(self):
        schemas_path = os.path.join(ORCH_PATH, "api", "schemas.py")
        with open(schemas_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "class ApiResponse" in content
        assert "class ApiError" in content
        assert "class ApiMetadata" in content

    def test_deps_has_auth_guard(self):
        deps_path = os.path.join(ORCH_PATH, "api", "deps.py")
        with open(deps_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "get_current_user" in content
        assert "get_optional_user" in content
        assert "HTTPBearer" in content

    def test_main_registers_all_routers(self):
        main_path = os.path.join(ORCH_PATH, "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "auth_router" in content
        assert "profile_router" in content
        assert "history_router" in content
        assert "/api/v1" in content

    def test_requirements_has_jwt_deps(self):
        req_path = os.path.join(ORCH_PATH, "requirements.txt")
        with open(req_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "python-jose" in content
        assert "bcrypt" in content

    def test_user_model_has_password_hash(self):
        db_path = os.path.join(ORCH_PATH, "core", "db.py")
        with open(db_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "password_hash" in content

    def test_env_has_jwt_secret(self):
        env_path = os.path.join(ROOT, ".env")
        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "JWT_SECRET_KEY" in content

    def test_docker_compose_passes_jwt_secret(self):
        compose_path = os.path.join(ROOT, "docker-compose.yml")
        with open(compose_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "JWT_SECRET_KEY" in content

    def test_v1_chat_endpoint_exists(self):
        routes_path = os.path.join(ORCH_PATH, "api", "routes.py")
        with open(routes_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "/api/v1/chat" in content
        assert "chat_v1" in content
        assert "get_optional_user" in content


# ═══════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-x"])
