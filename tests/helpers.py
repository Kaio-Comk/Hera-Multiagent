"""Base URL e credenciais para chamar a API nos testes (a API exige Host local + token)."""
import os

TEST_TOKEN = os.environ.get("HERA_API_KEY", "test-token-0123456789abcdef0123456789abcdef")
TEST_BASE_URL = "http://127.0.0.1:8000"
AUTH_HEADERS = {"X-Hera-Token": TEST_TOKEN}
