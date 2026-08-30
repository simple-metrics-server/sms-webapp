import os
import sys
from datetime import timedelta
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from webapp import cli


@pytest.fixture()
def runner(monkeypatch):
    """CliRunner with webapp.cli.run_app replaced by a recorder."""
    calls = []

    def fake_run_app(host, port, certfile, keyfile):
        calls.append((host, port, certfile, keyfile))

    monkeypatch.setattr(cli, "run_app", fake_run_app)
    return CliRunner(), calls


def combined_output(result) -> str:
    """Stdout+stderr across click versions."""
    out = result.output
    try:
        out += result.stderr
    except (ValueError, AttributeError):
        pass
    return out


def make_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Write a real cert+key pair for reuse tests."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cli.generate_self_signed(cert, key, "localhost", ["localhost"], 30)
    return cert, key


def read_san(cert_path: Path) -> list[str]:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    out = []
    for entry in san:
        if isinstance(entry, x509.DNSName):
            out.append(f"DNS:{entry.value}")
        else:
            out.append(f"IP:{entry.value}")
    return out


def test_http_no_keys_touched(runner, tmp_path):
    runner, calls = runner
    result = runner.invoke(
        cli.main, ["--scheme", "http", "--key-dir", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert calls == [(cli.DEFAULT_HOST, cli.DEFAULT_PORT, None, None)]
    assert not (tmp_path / "cert.pem").exists()
    assert not (tmp_path / "key.pem").exists()


def test_https_generates_files(runner, tmp_path):
    runner, calls = runner
    key_dir = tmp_path / "keys"
    result = runner.invoke(
        cli.main, ["--scheme", "https", "--key-dir", str(key_dir)]
    )
    assert result.exit_code == 0
    cert_path = key_dir / "cert.pem"
    key_path = key_dir / "key.pem"
    assert cert_path.exists() and key_path.exists()
    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert (cert_path.stat().st_mode & 0o777) == 0o644
    x509.load_pem_x509_certificate(cert_path.read_bytes())
    serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    san = read_san(cert_path)
    assert "DNS:localhost" in san
    assert "IP:127.0.0.1" in san
    assert "IP:::1" in san
    assert calls == [
        (
            cli.DEFAULT_HOST,
            cli.DEFAULT_PORT,
            str(cert_path),
            str(key_path),
        )
    ]


def test_https_generates_custom_san(runner, tmp_path):
    runner, _ = runner
    key_dir = tmp_path / "keys"
    result = runner.invoke(
        cli.main,
        [
            "--scheme",
            "https",
            "--key-dir",
            str(key_dir),
            "--san",
            "example.test",
            "--san",
            "10.0.0.5",
        ],
    )
    assert result.exit_code == 0
    san = read_san(key_dir / "cert.pem")
    assert san == ["IP:10.0.0.5", "DNS:example.test"] or sorted(san) == [
        "DNS:example.test",
        "IP:10.0.0.5",
    ]


def test_https_reuses_existing(runner, tmp_path):
    runner, _ = runner
    cert, key = make_pair(tmp_path)
    before = (cert.read_bytes(), key.read_bytes())
    result = runner.invoke(
        cli.main, ["--scheme", "https", "--key-dir", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert (cert.read_bytes(), key.read_bytes()) == before


def test_https_partial_files_error(runner, tmp_path):
    runner, calls = runner
    (tmp_path / "cert.pem").write_bytes(b"some cert")
    result = runner.invoke(
        cli.main, ["--scheme", "https", "--key-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "both exist or both be absent" in combined_output(result)
    assert calls == []


def test_https_no_generate_error(runner, tmp_path):
    runner, calls = runner
    result = runner.invoke(
        cli.main,
        [
            "--scheme",
            "https",
            "--key-dir",
            str(tmp_path / "keys"),
            "--no-generate",
        ],
    )
    assert result.exit_code == 1
    assert "--no-generate" in combined_output(result)
    assert not (tmp_path / "keys" / "cert.pem").exists()
    assert calls == []


def test_invalid_scheme_rejected(runner):
    runner, _ = runner
    result = runner.invoke(cli.main, ["--scheme", "ftp"])
    assert result.exit_code == 2


def test_host_port_forwarded(runner):
    runner, calls = runner
    result = runner.invoke(
        cli.main,
        ["--host", "127.0.0.1", "--port", "9999", "--scheme", "http"],
    )
    assert result.exit_code == 0
    assert calls == [("127.0.0.1", 9999, None, None)]


def test_custom_cert_key_paths(runner, tmp_path):
    runner, calls = runner
    cert = tmp_path / "my-cert.pem"
    key = tmp_path / "my-key.pem"
    result = runner.invoke(
        cli.main,
        [
            "--scheme",
            "https",
            "--key-dir",
            str(tmp_path / "keys"),
            "--cert",
            str(cert),
            "--key",
            str(key),
        ],
    )
    assert result.exit_code == 0
    assert cert.exists() and key.exists()
    assert calls == [(cli.DEFAULT_HOST, cli.DEFAULT_PORT, str(cert), str(key))]


def test_generate_self_signed_makes_valid_cert(tmp_path):
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cli.generate_self_signed(
        cert_path, key_path, "edge.test", ["edge.test", "192.168.0.1"], 30
    )
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert (
        cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        == "edge.test"
    )
    assert isinstance(cert.signature_hash_algorithm, hashes.SHA256)
    delta = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert timedelta(days=30, hours=1) == delta
    key = serialization.load_pem_private_key(
        key_path.read_bytes(), password=None
    )
    assert isinstance(key, rsa.RSAPrivateKey)
    assert key.key_size == 2048
    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert "BEGIN PRIVATE KEY" in key_path.read_text()


# --- systemd install ---


def sudo_prefix():
    return [] if os.geteuid() == 0 else ["sudo"]


def test_build_exec_args_http_defaults():
    args = cli.build_exec_args(
        cli.DEFAULT_HOST,
        cli.DEFAULT_PORT,
        "http",
        cli.DEFAULT_KEY_DIR,
        None,
        None,
        cli.DEFAULT_COMMON_NAME,
        (),
        cli.DEFAULT_DAYS,
    )
    assert args == [
        "--host",
        "0.0.0.0",
        "--port",
        "9555",
        "--scheme",
        "http",
    ]


def test_build_exec_args_https_defaults_skip_tls_opts():
    args = cli.build_exec_args(
        cli.DEFAULT_HOST,
        cli.DEFAULT_PORT,
        "https",
        cli.DEFAULT_KEY_DIR,
        None,
        None,
        cli.DEFAULT_COMMON_NAME,
        (),
        cli.DEFAULT_DAYS,
    )
    assert args == [
        "--host",
        "0.0.0.0",
        "--port",
        "9555",
        "--scheme",
        "https",
    ]


def test_build_exec_args_https_overrides():
    args = cli.build_exec_args(
        "127.0.0.1",
        1234,
        "https",
        "/tmp/keys",
        "/tmp/c.pem",
        "/tmp/k.pem",
        "edge",
        ("a.test", "10.0.0.1"),
        30,
    )
    assert args == [
        "--host",
        "127.0.0.1",
        "--port",
        "1234",
        "--scheme",
        "https",
        "--key-dir",
        "/tmp/keys",
        "--cert",
        "/tmp/c.pem",
        "--key",
        "/tmp/k.pem",
        "--common-name",
        "edge",
        "--san",
        "a.test",
        "--san",
        "10.0.0.1",
        "--days",
        "30",
    ]


def test_systemd_quote():
    assert cli._systemd_quote("plain") == "plain"
    assert cli._systemd_quote("/a/b-c_d.e") == "/a/b-c_d.e"
    assert cli._systemd_quote("with space") == '"with space"'
    assert cli._systemd_quote('say "hi"') == '"say \\"hi\\""'
    assert cli._systemd_quote("back\\slash") == '"back\\\\slash"'


def test_build_unit_contains_service_bits():
    unit = cli.build_unit(["--host", "0.0.0.0", "--scheme", "https"])
    assert "WantedBy=multi-user.target" in unit
    assert "After=network-online.target" in unit
    assert "Restart=on-failure" in unit
    lines = unit.splitlines()
    assert any(line.startswith("User=") for line in lines)
    assert any(line.startswith("Group=") for line in lines)
    assert any(line.startswith("WorkingDirectory=") for line in lines)
    assert (
        f"ExecStart={sys.executable} -m webapp.main"
        " --host 0.0.0.0 --scheme https" in unit
    )


def test_install_unit_invokes_systemctl(monkeypatch, tmp_path):
    calls = []
    written = []

    def fake_run(cmd):
        calls.append(cmd)
        if cmd[1] == "install":
            written.append(Path(cmd[4]).read_text())

    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli, "SYSTEM_UNIT_DIR", tmp_path)
    cli.install_unit("UNIT CONTENT")
    assert written == ["UNIT CONTENT"]
    prefix = sudo_prefix()
    assert calls[0] == prefix + [
        "install",
        "-m",
        "0644",
        calls[0][4],
        str(tmp_path / "sms-webapp.service"),
    ]
    assert calls[1] == prefix + ["systemctl", "daemon-reload"]
    assert calls[2] == prefix + ["systemctl", "enable", "sms-webapp.service"]
    assert calls[3] == prefix + [
        "systemctl",
        "restart",
        "sms-webapp.service",
    ]
    # the temp file is cleaned up
    assert not Path(calls[0][4]).exists()


def test_uninstall_unit_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SYSTEM_UNIT_DIR", tmp_path)

    def fail_run(cmd):
        raise AssertionError("must not run anything")

    monkeypatch.setattr(cli, "_run", fail_run)
    with pytest.raises(click.ClickException, match="not installed"):
        cli.uninstall_unit()


def test_uninstall_unit_invokes_systemctl(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli, "_run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(cli, "SYSTEM_UNIT_DIR", tmp_path)
    (tmp_path / "sms-webapp.service").write_text("[Unit]\n")
    cli.uninstall_unit()
    prefix = sudo_prefix()
    assert calls == [
        prefix + ["systemctl", "disable", "--now", "sms-webapp.service"],
        prefix + ["rm", "-f", str(tmp_path / "sms-webapp.service")],
        prefix + ["systemctl", "daemon-reload"],
    ]


def test_cli_install_systemd(monkeypatch, tmp_path, runner):
    runner, calls = runner
    installed = []
    monkeypatch.setattr(
        cli, "install_unit", lambda content: installed.append(content)
    )
    result = runner.invoke(cli.main, ["--scheme", "https", "--install-systemd"])
    assert result.exit_code == 0
    assert calls == []  # serving is the service's job, not ours
    assert len(installed) == 1
    assert "WantedBy=multi-user.target" in installed[0]
    assert "--scheme https" in installed[0]
    assert "installed and enabled" in result.output


def test_cli_install_systemd_warns_about_pidfile(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    monkeypatch.setattr(cli, "run_app", lambda *a: None)
    monkeypatch.setattr(cli, "__file__", str(repo / "webapp" / "cli.py"))
    repo.mkdir()
    (repo / "webapp.pid").write_text("1")
    monkeypatch.setattr(cli, "install_unit", lambda content: None)
    result = CliRunner().invoke(cli.main, ["--install-systemd"])
    assert result.exit_code == 0
    assert "just stop" in combined_output(result)


def test_cli_install_and_uninstall_are_exclusive(runner):
    runner, _ = runner
    result = runner.invoke(
        cli.main, ["--install-systemd", "--uninstall-systemd"]
    )
    assert result.exit_code == 2


def test_cli_uninstall_systemd(monkeypatch, runner):
    runner, calls = runner
    uninstalled = []
    monkeypatch.setattr(cli, "uninstall_unit", lambda: uninstalled.append(True))
    result = runner.invoke(cli.main, ["--uninstall-systemd"])
    assert result.exit_code == 0
    assert uninstalled == [True]
    assert calls == []
    assert "removed" in result.output
