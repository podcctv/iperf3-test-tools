"""Dashboard authentication helpers and CLI utilities."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import Request, Response

from .config import settings

import secrets
import string

logger = logging.getLogger(__name__)

DEFAULT_DASHBOARD_PASSWORD = "iperf-pass"


class DashboardAuthManager:
    def __init__(self) -> None:
        self._password = self._load_password()
        self._ensure_password_file()
        self._log_password()

    @staticmethod
    def _normalize(password: Optional[str]) -> str:
        if password is None:
            return ""
        return password.strip()

    @staticmethod
    def _generate_random_password(length: int = 12) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def _dashboard_token(self, password: str) -> str:
        secret = settings.dashboard_secret.encode()
        return hmac.new(secret, password.encode(), hashlib.sha256).hexdigest()

    def _load_password(self) -> str | None:
        env_password = os.getenv("DASHBOARD_PASSWORD")
        if env_password is not None:
            return self._normalize(env_password)

        path = settings.dashboard_password_file
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return self._normalize(content)
            except OSError:
                logger.exception("Failed to read stored dashboard password from %s", path)

        return None

    def _save_password(self, password: str) -> None:
        path = settings.dashboard_password_file
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(self._normalize(password), encoding="utf-8")
        except OSError:
            logger.exception("Failed to persist dashboard password to %s", path)

    def _ensure_password_file(self) -> None:
        if self._password:
            # If we found a password (env or file), just ensure it's synced to file if it was from env? 
            # The original logic was: if file exists, read it. If not, save current.
            # But we might have loaded from env.
            # Let's keep it simple: if file doesn't exist, save what we have.
            if not settings.dashboard_password_file.exists():
                self._save_password(self._password)
            return

        # No password found in env or file. Generate one.
        logger.info("No dashboard password found. Generating a secure random password.")
        new_pass = self._generate_random_password()
        self._password = new_pass
        self._save_password(new_pass)

    def _log_password(self) -> None:
        logger.warning("Dashboard password initialized: %s", self.current_password())

    def current_password(self) -> str:
        return self._password or DEFAULT_DASHBOARD_PASSWORD

    def normalize_password(self, password: Optional[str]) -> str:
        return self._normalize(password)

    def verify_password(self, raw_password: Optional[str]) -> bool:
        return self._normalize(raw_password) == self.current_password()

    def update_password(
        self,
        new_password: str,
        *,
        current_password: Optional[str] = None,
        force: bool = False,
    ) -> str:
        normalized_new = self._normalize(new_password)
        if len(normalized_new) < 6:
            raise ValueError("password_too_short")

        if not force and current_password is not None:
            if not self.verify_password(current_password):
                raise ValueError("invalid_password")

        self._password = normalized_new
        self._save_password(normalized_new)
        return normalized_new

    def is_authenticated(self, request: Request) -> bool:
        stored = request.cookies.get(settings.dashboard_cookie_name)
        return stored == self._dashboard_token(self.current_password())

    def set_auth_cookie(self, response: Response, password: Optional[str] = None) -> None:
        normalized = self._normalize(password or self.current_password())
        response.set_cookie(
            settings.dashboard_cookie_name,
            self._dashboard_token(normalized),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24,
        )

    def describe_password_location(self) -> Path:
        return settings.dashboard_password_file


_auth_manager = DashboardAuthManager()


def auth_manager() -> DashboardAuthManager:
    return _auth_manager


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage dashboard password inside the container")
    parser.add_argument(
        "--set-password",
        dest="new_password",
        help="Set a new dashboard password (minimum 6 characters)",
    )
    parser.add_argument(
        "--current-password",
        dest="current_password",
        help="Current password for validation (omit when using --force)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass current password validation (useful for recovery from lock-out)",
    )
    parser.add_argument(
        "--show-location",
        action="store_true",
        help="Print where the password file is stored inside the container",
    )
    return parser


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = _build_cli()
    args = parser.parse_args(argv)

    manager = auth_manager()

    if args.show_location:
        print(f"Password file: {manager.describe_password_location()}")

    if args.new_password:
        try:
            manager.update_password(
                args.new_password,
                current_password=args.current_password,
                force=args.force,
            )
        except ValueError as exc:  # pragma: no cover - CLI path
            print(f"Failed to update password: {exc}")
            return 1
        manager.set_auth_cookie(Response(), args.new_password)
        print("Dashboard password updated successfully.")
        return 0

    if not args.show_location:
        parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI utility
    raise SystemExit(_cli())
