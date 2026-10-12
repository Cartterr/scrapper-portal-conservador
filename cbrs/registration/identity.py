"""Identity construction for a registration: payload, password, address."""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass

from .rut_provider import PersonIdentity

# Portal password requirements: >=8 chars, >=1 digit, >=1 lowercase, >=1 uppercase.
_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "@#$%&*"
_PASSWORD_LENGTH = 15


@dataclass(frozen=True)
class RegistrationIdentity:
    person: PersonIdentity
    email: str
    password: str
    direccion: str
    celular: str

    def create_payload(self, recaptcha_token: str) -> dict:
        """Exact body the SPA posts to /api/v1/account/create."""

        return {
            "idUsuario": None,
            "nombres": self.person.nombres,
            "apellidoPaterno": self.person.apellido_paterno,
            "apellidoMaterno": self.person.apellido_materno,
            "email": self.email,
            "emailConfirmar": self.email,
            "rut": None,
            "dv": None,
            "nombreCompleto": None,
            "rutDv": self.person.rut,
            "direccion": self.direccion,
            "contactoCc": None,
            "contactoCcNombre": None,
            "celular": self.celular,
            "telefono": None,
            "password": self.password,
            "passwordConfirmar": self.password,
            "recaptchaToken": recaptcha_token,
            "cfTurnstileToken": None,
            "tos": True,
            "extras": None,
        }


def generate_password(rng: random.Random | None = None) -> str:
    """Generate a password that satisfies every portal requirement."""

    rng = rng or random.Random()
    required = [
        rng.choice(string.ascii_uppercase),
        rng.choice(string.ascii_uppercase),
        rng.choice(string.ascii_lowercase),
        rng.choice(string.ascii_lowercase),
        rng.choice(string.digits),
        rng.choice(string.digits),
    ]
    rest = [
        rng.choice(_PASSWORD_ALPHABET)
        for _ in range(_PASSWORD_LENGTH - len(required))
    ]
    characters = required + rest
    rng.shuffle(characters)
    password = "".join(characters)
    assert password_meets_requirements(password)
    return password


def password_meets_requirements(password: str) -> bool:
    return (
        len(password) >= 8
        and bool(re.search(r"\d", password))
        and bool(re.search(r"[a-z]", password))
        and bool(re.search(r"[A-Z]", password))
    )


def choose_address(address_pool: tuple[str, ...], rut: str) -> str:
    """Pick a deterministic address for a RUT so retries stay consistent."""

    if not address_pool:
        raise ValueError("Address pool is empty")
    digest = sum(ord(char) for char in rut) + len(rut) * 31
    return address_pool[digest % len(address_pool)]


def build_identity(
    person: PersonIdentity,
    *,
    email_address: str,
    address_pool: tuple[str, ...],
    celular: str,
    rng: random.Random | None = None,
) -> RegistrationIdentity:
    return RegistrationIdentity(
        person=person,
        email=email_address,
        password=generate_password(rng),
        direccion=choose_address(address_pool, person.rut),
        celular=celular,
    )
