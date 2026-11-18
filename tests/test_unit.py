"""Pruebas unitarias puras (sin red ni base de datos)."""
import os
import subprocess
import sys
import time

import jwt
import pytest

from app.core.config import collect_env_errors
from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.modules.catalog import APORTE_QUOTA_BY_COURSE, CAMPUS_BY_COURSE
from app.modules.padron.course_parser import parse_curso_texto
from app.shared.crypto.field_encryption import decrypt_field, encrypt_field, hash_field
from app.shared.utilities.dates import parse_js_date, to_js_iso
from app.shared.utilities.escape_regexp import escape_regexp
from app.shared.validators.cedula import validate_ecuadorian_dni, validate_full_name, validate_person_name


class TestCedula:
    @pytest.mark.parametrize("dni", ["0102030400", "0912345675", "1710034065", "0105566772"])
    def test_validas(self, dni):
        assert validate_ecuadorian_dni(dni)

    @pytest.mark.parametrize("dni", ["0102030405", "123", "01020304000", "2502030400", "0062030400", "0172030400", "abcdefghij", ""])
    def test_invalidas(self, dni):
        assert not validate_ecuadorian_dni(dni)

    def test_nombre_completo(self):
        assert validate_full_name("Juan Perez Lopez")
        assert not validate_full_name("Juan Perez")
        assert not validate_full_name("Juan P Lopez")
        assert validate_person_name("María José Núñez")
        assert not validate_person_name("Juan Perez Lopez2")


class TestCifrado:
    # Valores producidos por el backend NestJS original con la misma clave (verificación cruzada).
    NODE_CIPHERTEXT = "U+yWsebcFoxDSFUN:GrAjMUZHIC4UAU7rpNJBYw==:0oTffm5lbznCZQ=="
    NODE_HASH = "7bc0eb3caf7cad949afc87ff0f812eea88d99e639142fa68e4508159a1bf260e"

    def test_descifra_lo_cifrado_por_node(self):
        assert decrypt_field(self.NODE_CIPHERTEXT) == "0105566770"

    def test_hash_identico_a_node(self):
        assert hash_field("0105566770") == self.NODE_HASH

    def test_iv_aleatorio_y_formato(self):
        a, b = encrypt_field("0912345675"), encrypt_field("0912345675")
        assert a != b and a.count(":") == 2
        assert decrypt_field(a) == decrypt_field(b) == "0912345675"

    def test_manipulacion_detectada(self):
        iv, tag, ct = encrypt_field("0912345675").split(":")
        with pytest.raises(Exception):
            decrypt_field(":".join([iv, tag, "AAAA" + ct[4:]]))


class TestSeguridad:
    def test_bcrypt_cost_10_compatible(self):
        h = hash_password("secreta")
        assert h.startswith("$2b$10$") and verify_password("secreta", h) and not verify_password("otra", h)
        # hash generado por bcrypt de Node (admin123)
        assert verify_password("admin123", "$2b$10$Tv/Szlbx60yvtMAy5aF1f.UWL0cWBModVl/VBH42mRFCdY3szm0lC")

    def test_jwt_8h(self):
        secret = "x" * 40
        tok = create_access_token({"sub": "1", "username": "admin", "role": "admin"}, secret)
        p = decode_access_token(tok, secret)
        assert p["exp"] - p["iat"] == 8 * 3600 and p["username"] == "admin"

    def test_jwt_expirado_y_firma(self):
        secret = "x" * 40
        expired = jwt.encode({"sub": "1", "exp": int(time.time()) - 5}, secret, algorithm="HS256")
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_access_token(expired, secret)
        with pytest.raises(jwt.InvalidSignatureError):
            decode_access_token(create_access_token({"sub": "1"}, secret), "y" * 40)

    def test_validacion_de_entorno(self):
        assert collect_env_errors({}) == ["JWT_SECRET no está definida.", "PADRON_ENCRYPTION_KEY no está definida."]
        errs = collect_env_errors({"JWT_SECRET": "corto", "PADRON_ENCRYPTION_KEY": "zz"})
        assert "JWT_SECRET debe tener al menos 32 caracteres (tiene 5)." in errs
        assert any("PADRON_ENCRYPTION_KEY debe ser una cadena hexadecimal" in e for e in errs)
        assert collect_env_errors({"JWT_SECRET": "a" * 32, "PADRON_ENCRYPTION_KEY": "ab" * 32}) == []

    def test_servidor_no_arranca_sin_secreto(self):
        env = {k: v for k, v in os.environ.items() if k not in ("JWT_SECRET",)}
        env["JWT_SECRET"] = ""
        r = subprocess.run([sys.executable, "-c", "import app.main"], env=env, capture_output=True, text=True,
                           cwd=os.path.dirname(os.path.dirname(__file__)))
        assert r.returncode == 1 and "JWT_SECRET no está definida." in r.stderr


class TestDominio:
    def test_campus(self):
        assert CAMPUS_BY_COURSE["4to_Basica"] == "Carlos_Crespi"
        assert CAMPUS_BY_COURSE["5to"] == CAMPUS_BY_COURSE["7mo"] == "Maria_Auxiliadora"
        assert CAMPUS_BY_COURSE["8vo"] == CAMPUS_BY_COURSE["3ro_Bach"] == "Yanuncay"

    def test_cupos(self):
        assert APORTE_QUOTA_BY_COURSE["1ro_Basica"] == APORTE_QUOTA_BY_COURSE["10mo"] == 4
        assert APORTE_QUOTA_BY_COURSE["1ro_Bach"] == APORTE_QUOTA_BY_COURSE["3ro_Bach"] == 6

    @pytest.mark.parametrize("texto,course,paralelo", [
        ("Quinto de Básica B", "5to", "B"),
        ("Primero Bach. F1", "1ro_Bach", "F1"),
        ("5to B", "5to", "B"),
        ("10mo de Básica A", "10mo", "A"),
        ("Séptimo C", "7mo", "C"),
        ("Tercer Bachillerato Técnico", "3ro_Bach", None),
        ("Curso raro", None, None),
    ])
    def test_parser_de_curso(self, texto, course, paralelo):
        assert parse_curso_texto(texto) == {"course": course, "paralelo": paralelo}

    def test_escape_regexp(self):
        assert escape_regexp("PM-2026-000(") == r"PM-2026-000\("
        assert escape_regexp("a.b*c") == r"a\.b\*c"

    def test_fechas_como_javascript(self):
        assert to_js_iso(parse_js_date("2026-01-01")) == "2026-01-01T00:00:00.000Z"
        assert to_js_iso(parse_js_date("2026-01-01T05:00:00-05:00")) == "2026-01-01T10:00:00.000Z"
        assert to_js_iso(parse_js_date("2026-12-31T10:00:00Z")) == "2026-12-31T10:00:00.000Z"
