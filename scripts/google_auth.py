"""Получить Google refresh token (создаёт/перезаписывает token.json).

Полный runbook (симптомы, проверка, добавление скоупа) — docs/GOOGLE_AUTH.md.

Флоу требует браузер, а разработка на сервере, поэтому:
  1. Google Cloud Console → OAuth client (тип Desktop app) → скачать JSON
     → положить в корень проекта как credentials.json
  2. с ноутбука: ssh -L 8765:localhost:8765 <server>
  3. на сервере: uv run python scripts/google_auth.py
  4. напечатанную ссылку открыть в ЛОКАЛЬНОМ браузере, авторизоваться
  5. token.json появится на сервере; credentials.json больше не нужен
"""

from google_auth_oauthlib.flow import InstalledAppFlow

from tutor.tools.google_auth import CREDENTIALS_FILE, SCOPES, TOKEN_FILE


def main() -> None:
    if not CREDENTIALS_FILE.exists():
        raise SystemExit(
            f"Нет {CREDENTIALS_FILE}. Google Cloud Console → APIs & Services → "
            "Credentials → Create OAuth client ID (Desktop app) → Download JSON."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(
        port=8765,  # 8080 занят code-server; Desktop-клиент принимает loopback на любом порту
        open_browser=False,  # ссылку открываем на ноутбуке через port-forward
        access_type="offline",
        prompt="consent",  # иначе Google может не выдать refresh_token повторно
    )
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    print(f"OK: {TOKEN_FILE} записан, refresh_token={'есть' if creds.refresh_token else 'НЕТ!'}")


if __name__ == "__main__":
    main()
