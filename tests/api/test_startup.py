"""Dockyard refuses to start on an incomplete configuration or when Docker is unreachable."""

import hashlib
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .conftest import instance_env

ESSENTIAL = [
    "DOCKYARD_SECRET_KEY",
    "DOCKYARD_CF_ACCESS_TEAM",
    "DOCKYARD_CF_ACCESS_AUD",
    "DOCKYARD_FIRST_ADMINISTRATOR_EMAIL",
]
UNREACHABLE_DOCKER = "tcp://127.0.0.1:9"


def start_dockyard(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dockyard"],
        env={"PATH": os.environ["PATH"], "DOCKER_HOST": UNREACHABLE_DOCKER, **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("missing", ESSENTIAL)
def test_dockyard_refuses_to_start_without_an_essential_setting(
    tmp_path: Path, missing: str
) -> None:
    env = instance_env(tmp_path)
    del env[missing]

    result = start_dockyard(env)

    assert result.returncode != 0
    assert missing in result.stderr
    assert "configuration" in result.stderr


def test_every_missing_setting_is_reported_at_once() -> None:
    result = start_dockyard({})

    assert result.returncode != 0
    for name in ESSENTIAL:
        assert name in result.stderr


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("DOCKYARD_SECRET_KEY", "too-short"),
        ("DOCKYARD_FIRST_ADMINISTRATOR_EMAIL", "not-an-email"),
        ("DOCKYARD_CF_ACCESS_TEAM", "https://acme.cloudflareaccess.com"),
        ("DOCKYARD_DEV_IDENTITY", "maybe"),
        ("DOCKYARD_LISTEN_PORT", "eighty"),
        ("DOCKYARD_LISTEN_PORT", "70000"),
        ("DOCKYARD_PROJECTS_DNS", "dns.example.com"),
        ("DOCKYARD_PROJECTS_DNS", "1.1.1.1;9.9.9.9"),
        ("DOCKYARD_PROJECTS_DNS", ","),
        ("DOCKYARD_PROJECT_MAX_CPUS", "two"),
        ("DOCKYARD_PROJECT_MAX_CPUS", "0.25"),
        ("DOCKYARD_PROJECT_MAX_CPUS", "inf"),
        ("DOCKYARD_PROJECT_MAX_MEMORY_GIB", "4g"),
        ("DOCKYARD_PROJECT_MAX_MEMORY_GIB", "0.5"),
        ("DOCKYARD_MAX_ACTIVE_PROJECTS", "0"),
        ("DOCKYARD_MAX_ACTIVE_PROJECTS", "2.5"),
        ("DOCKYARD_MAX_ACTIVE_PROJECTS", "nan"),
    ],
)
def test_dockyard_refuses_to_start_with_an_invalid_setting(
    tmp_path: Path, setting: str, value: str
) -> None:
    result = start_dockyard({**instance_env(tmp_path), setting: value})

    assert result.returncode != 0
    assert setting in result.stderr


def test_the_development_identity_does_not_need_proxy_settings_to_start(tmp_path: Path) -> None:
    env = instance_env(tmp_path)
    del env["DOCKYARD_CF_ACCESS_TEAM"], env["DOCKYARD_CF_ACCESS_AUD"]

    result = start_dockyard({**env, "DOCKYARD_DEV_IDENTITY": "true"})

    assert "configuration" not in result.stderr
    assert UNREACHABLE_DOCKER in result.stderr  # it got as far as looking for Docker


def test_dockyard_refuses_to_start_when_docker_is_unreachable(tmp_path: Path) -> None:
    result = start_dockyard(instance_env(tmp_path))

    assert result.returncode != 0
    assert UNREACHABLE_DOCKER in result.stderr
    assert "socket proxy" in result.stderr


LOCAL_SIGN_IN = {"DOCKYARD_LOCAL_PORT": "8443", "DOCKYARD_LOCAL_ADDRESS": "192.168.1.10"}


def without_cloudflare(env: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in env.items()
        if name not in ("DOCKYARD_CF_ACCESS_TEAM", "DOCKYARD_CF_ACCESS_AUD")
    }


@pytest.mark.parametrize(
    ("given", "missing"),
    [
        ("DOCKYARD_CF_ACCESS_TEAM", "DOCKYARD_CF_ACCESS_AUD"),
        ("DOCKYARD_CF_ACCESS_AUD", "DOCKYARD_CF_ACCESS_TEAM"),
    ],
)
def test_half_of_the_cloudflare_access_settings_refuses_to_start_naming_the_other(
    tmp_path: Path, given: str, missing: str
) -> None:
    env = {**instance_env(tmp_path), **LOCAL_SIGN_IN}
    del env[missing]

    result = start_dockyard(env)

    assert result.returncode != 0
    assert "configuration" in result.stderr
    assert f"{missing} is missing" in result.stderr


def test_without_any_way_in_dockyard_refuses_to_start_and_says_how_to_enable_one(
    tmp_path: Path,
) -> None:
    result = start_dockyard(without_cloudflare(instance_env(tmp_path)))

    assert result.returncode != 0
    assert "configuration" in result.stderr
    for setting in ("DOCKYARD_CF_ACCESS_TEAM", "DOCKYARD_CF_ACCESS_AUD", "DOCKYARD_LOCAL_PORT"):
        assert setting in result.stderr


def test_local_sign_in_alone_is_enough_to_start(tmp_path: Path) -> None:
    result = start_dockyard({**without_cloudflare(instance_env(tmp_path)), **LOCAL_SIGN_IN})

    assert "configuration" not in result.stderr
    assert UNREACHABLE_DOCKER in result.stderr  # it got as far as looking for Docker


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("DOCKYARD_LOCAL_PORT", "eighty"),
        ("DOCKYARD_LOCAL_PORT", "0"),
        ("DOCKYARD_LOCAL_PORT", "8000"),  # the tunnel listener's
        ("DOCKYARD_LOCAL_PORT", "08000"),
        ("DOCKYARD_LOCAL_ADDRESS", ""),
        ("DOCKYARD_LOCAL_ADDRESS", "dockyard.lan"),
        ("DOCKYARD_LOCAL_ADDRESS", "192.168.1.300"),
        ("DOCKYARD_LOCAL_ADDRESS", "fd00::10"),
        ("DOCKYARD_LOCAL_ADDRESS", "::"),
        ("DOCKYARD_LOCAL_NETWORKS", "lan"),
        ("DOCKYARD_LOCAL_NETWORKS", "192.168.0.0/33"),
        ("DOCKYARD_LOCAL_NETWORKS", "192.168.0.0/16;10.0.0.0/8"),
        ("DOCKYARD_LOCAL_NETWORKS", "fd00::/8"),
        ("DOCKYARD_LOCAL_URL", "dockyard.lan:8443"),
        ("DOCKYARD_LOCAL_URL", "http://dockyard.lan:8443"),
        ("DOCKYARD_LOCAL_URL", "https://"),
    ],
)
def test_dockyard_refuses_to_start_with_an_invalid_local_setting(
    tmp_path: Path, setting: str, value: str
) -> None:
    result = start_dockyard({**instance_env(tmp_path), **LOCAL_SIGN_IN, setting: value})

    assert result.returncode != 0
    assert "configuration" in result.stderr
    assert setting in result.stderr


def test_local_settings_without_the_local_port_are_ignored(tmp_path: Path) -> None:
    env = {**instance_env(tmp_path), "DOCKYARD_LOCAL_ADDRESS": "fd00::10"}

    result = start_dockyard(env)

    assert "configuration" not in result.stderr
    assert "Local sign-in: off" in result.stderr


def test_the_startup_log_names_the_ways_in_and_the_local_port(tmp_path: Path) -> None:
    both = start_dockyard({**instance_env(tmp_path), **LOCAL_SIGN_IN}).stderr
    cloudflare_only = start_dockyard(instance_env(tmp_path)).stderr

    assert "Cloudflare Access: on" in both
    assert "Local sign-in: on, port 8443" in both
    assert "Cloudflare Access: on" in cloudflare_only
    assert "Local sign-in: off" in cloudflare_only


def test_publishing_the_local_port_on_every_interface_starts_with_a_warning(
    tmp_path: Path,
) -> None:
    env = {**instance_env(tmp_path), **LOCAL_SIGN_IN, "DOCKYARD_LOCAL_ADDRESS": "0.0.0.0"}

    result = start_dockyard(env)

    assert "configuration" not in result.stderr
    assert "WARNING" in result.stderr
    assert "every interface" in result.stderr


FINGERPRINT = re.compile(r"SHA-256 fingerprint: ((?:[0-9A-F]{2}:){31}[0-9A-F]{2})")


def logged_fingerprint(result: subprocess.CompletedProcess[str]) -> str:
    found = FINGERPRINT.search(result.stderr)
    assert found, result.stderr
    return found.group(1)


def test_without_a_certificate_one_is_made_at_first_start_and_kept(tmp_path: Path) -> None:
    env = {**instance_env(tmp_path), **LOCAL_SIGN_IN}

    first = start_dockyard(env)
    second = start_dockyard(env)

    assert "self-signed" in first.stderr
    assert logged_fingerprint(first) == logged_fingerprint(second)


def test_another_instance_makes_another_certificate(tmp_path: Path) -> None:
    one = start_dockyard({**instance_env(tmp_path / "one"), **LOCAL_SIGN_IN})
    another = start_dockyard({**instance_env(tmp_path / "another"), **LOCAL_SIGN_IN})

    assert logged_fingerprint(one) != logged_fingerprint(another)


def provide_certificate(data_dir: Path) -> str:
    """The Administrator's own certificate in the data volume; its SHA-256 fingerprint."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dockyard.lan")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=90))
        .sign(key, hashes.SHA256())
    )
    tls = data_dir / "tls"
    tls.mkdir(parents=True)
    (tls / "cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (tls / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    der = certificate.public_bytes(serialization.Encoding.DER)
    return ":".join(f"{byte:02X}" for byte in hashlib.sha256(der).digest())


def test_a_certificate_provided_in_the_data_volume_is_used(tmp_path: Path) -> None:
    fingerprint = provide_certificate(tmp_path)

    result = start_dockyard({**instance_env(tmp_path), **LOCAL_SIGN_IN})

    assert "provided" in result.stderr
    assert logged_fingerprint(result) == fingerprint


@pytest.mark.parametrize("missing", ["cert.pem", "key.pem"])
def test_a_certificate_without_its_key_or_the_other_way_round_refuses_to_start(
    tmp_path: Path, missing: str
) -> None:
    provide_certificate(tmp_path)
    (tmp_path / "tls" / missing).unlink()

    result = start_dockyard({**instance_env(tmp_path), **LOCAL_SIGN_IN})

    assert result.returncode != 0
    assert missing in result.stderr
    assert UNREACHABLE_DOCKER not in result.stderr


def test_a_certificate_with_someone_elses_key_refuses_to_start(tmp_path: Path) -> None:
    provide_certificate(tmp_path / "other")
    provide_certificate(tmp_path)
    (tmp_path / "other" / "tls" / "key.pem").replace(tmp_path / "tls" / "key.pem")

    result = start_dockyard({**instance_env(tmp_path), **LOCAL_SIGN_IN})

    assert result.returncode != 0
    assert "does not belong" in result.stderr
