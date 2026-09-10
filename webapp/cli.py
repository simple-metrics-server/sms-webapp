"""Command-line interface: preflight TLS material, then serve the app."""

import grp
import ipaddress
import logging
import os
import pwd
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from webapp.main import DEFAULT_HOST, DEFAULT_PORT, run_app

log = logging.getLogger(__name__)

DEFAULT_KEY_DIR = "data/keys"
DEFAULT_SANS = ["localhost", "127.0.0.1", "::1"]
DEFAULT_COMMON_NAME = "localhost"
DEFAULT_DAYS = 365

UNIT_NAME = "sms-webapp.service"
SYSTEM_UNIT_DIR = Path("/etc/systemd/system")
UNIT_TEMPLATE = """\
[Unit]
Description=webapp - Prometheus etrics exporter
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={workdir}
ExecStart={python} -m webapp.main {exec_args}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _write_private_key(key_path: Path, key: rsa.RSAPrivateKey) -> None:
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)


def _write_certificate(cert_path: Path, cert: x509.Certificate) -> None:
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(cert_path, 0o644)


def _parse_san(value: str) -> x509.GeneralName:
    """Turn a string into a DNS or IP SAN entry."""
    try:
        return x509.IPAddress(ipaddress.ip_address(value))
    except ValueError:
        return x509.DNSName(value)


def generate_self_signed(
    cert_path: Path,
    key_path: Path,
    common_name: str,
    sans: list[str],
    days: int,
) -> None:
    """Write a self-signed certificate and its unencrypted key."""
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.SubjectAlternativeName([_parse_san(s) for s in sans]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    _write_certificate(cert_path, cert)
    _write_private_key(key_path, key)
    log.warning(
        "generated self-signed certificate %s (key %s) — browsers "
        "will warn about it; use a CA-signed cert in production",
        cert_path,
        key_path,
    )


def preflight_tls(
    cert_path: Path,
    key_path: Path,
    key_dir: Path,
    no_generate: bool,
    common_name: str,
    sans: list[str],
    days: int,
) -> tuple[str, str]:
    """Ensure cert and key exist (generating them if allowed)."""
    key_dir.mkdir(parents=True, exist_ok=True)
    cert_exists = cert_path.exists()
    key_exists = key_path.exists()
    if cert_exists and key_exists:
        log.info("reusing existing certificate %s", cert_path)
        return str(cert_path), str(key_path)
    if cert_exists != key_exists:
        raise click.ClickException(
            f"cert and key must both exist or both be absent "
            f"(cert={cert_path} exists={cert_exists}, "
            f"key={key_path} exists={key_exists})"
        )
    if no_generate:
        raise click.ClickException(
            f"no certificate at {cert_path} and no key at {key_path}; "
            f"--no-generate is set, refusing to create them"
        )
    generate_self_signed(cert_path, key_path, common_name, sans, days)
    return str(cert_path), str(key_path)


# --- systemd installation ---


def _systemd_quote(token: str) -> str:
    """Quote one ExecStart argument using systemd's escaping rules."""
    if token and not re.search(r"[^A-Za-z0-9_.\-/@^=:+,]", token):
        return token
    return '"' + token.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_exec_args(
    host: str,
    port: int,
    scheme: str,
    key_dir: str,
    cert: str | None,
    key: str | None,
    common_name: str,
    san: tuple[str, ...],
    days: int,
) -> list[str]:
    """ExecStart arguments for the service, mirroring this invocation."""
    args = ["--host", host, "--port", str(port), "--scheme", scheme]
    if scheme == "https":
        if key_dir != DEFAULT_KEY_DIR:
            args += ["--key-dir", key_dir]
        if cert is not None:
            args += ["--cert", cert]
        if key is not None:
            args += ["--key", key]
        if common_name != DEFAULT_COMMON_NAME:
            args += ["--common-name", common_name]
        for entry in san:
            args += ["--san", entry]
        if days != DEFAULT_DAYS:
            args += ["--days", str(days)]
    return args


def build_unit(exec_args: list[str]) -> str:
    """Render sms-webapp.service for the current user and repo."""
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    workdir = Path(__file__).resolve().parent.parent
    return UNIT_TEMPLATE.format(
        user=user,
        group=group,
        workdir=workdir,
        python=sys.executable,
        exec_args=" ".join(_systemd_quote(a) for a in exec_args),
    )


def _sudo_prefix() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo"]


def _run(cmd: list[str]) -> None:
    """Run a privileged helper command, failing with a clear error."""
    log.info("running: %s", shlex.join(cmd))
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise click.ClickException(f"command not found: {cmd[0]}") from e
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip().splitlines()
        hint = detail[-1] if detail else f"exit code {e.returncode}"
        raise click.ClickException(f"`{shlex.join(cmd)}` failed: {hint}")


def install_unit(content: str) -> None:
    """Write the unit file, then enable and (re)start the service."""
    target = SYSTEM_UNIT_DIR / UNIT_NAME
    prefix = _sudo_prefix()
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".service", text=True)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        _run(prefix + ["install", "-m", "0644", str(tmp), str(target)])
        _run(prefix + ["systemctl", "daemon-reload"])
        _run(prefix + ["systemctl", "enable", UNIT_NAME])
        _run(prefix + ["systemctl", "restart", UNIT_NAME])
    finally:
        tmp.unlink(missing_ok=True)


def uninstall_unit() -> None:
    """Stop and disable the service, then remove the unit file."""
    target = SYSTEM_UNIT_DIR / UNIT_NAME
    if not target.exists():
        raise click.ClickException(
            f"{UNIT_NAME} is not installed at {SYSTEM_UNIT_DIR}"
        )
    prefix = _sudo_prefix()
    _run(prefix + ["systemctl", "disable", "--now", UNIT_NAME])
    _run(prefix + ["rm", "-f", str(target)])
    _run(prefix + ["systemctl", "daemon-reload"])


def pidfile_warning() -> str | None:
    """A warning string when a `just start` instance may hold the port."""
    pidfile = Path(__file__).resolve().parent.parent / "webapp.pid"
    if pidfile.exists():
        return (
            "warning: webapp.pid exists — if the app is running via"
            " `just start`, run `just stop` before the service binds"
            " the port"
        )
    return None


@click.command()
@click.option(
    "--host",
    default=DEFAULT_HOST,
    show_default=True,
    help="interface to bind",
)
@click.option(
    "--port",
    default=DEFAULT_PORT,
    type=int,
    show_default=True,
    help="port to bind",
)
@click.option(
    "--scheme",
    type=click.Choice(["http", "https"]),
    default="http",
    show_default=True,
    help="serve plain http or https",
)
@click.option(
    "--key-dir",
    default=DEFAULT_KEY_DIR,
    type=click.Path(file_okay=False),
    show_default=True,
    help="directory for cert/key material",
)
@click.option(
    "--cert",
    default=None,
    type=click.Path(dir_okay=False),
    help="certificate path (default: <key-dir>/cert.pem)",
)
@click.option(
    "--key",
    default=None,
    type=click.Path(dir_okay=False),
    help="private key path (default: <key-dir>/key.pem)",
)
@click.option(
    "--no-generate",
    is_flag=True,
    default=False,
    help="fail instead of generating missing cert/key",
)
@click.option(
    "--common-name",
    default=DEFAULT_COMMON_NAME,
    show_default=True,
    help="subject CN when generating",
)
@click.option(
    "--san",
    multiple=True,
    help="subjectAltName entry when generating (repeatable)",
)
@click.option(
    "--days",
    default=DEFAULT_DAYS,
    show_default=True,
    type=int,
    help="certificate validity in days when generating",
)
@click.option(
    "--install-systemd",
    is_flag=True,
    default=False,
    help="install as a systemd service enabled on boot, then exit",
)
@click.option(
    "--uninstall-systemd",
    is_flag=True,
    default=False,
    help="remove the systemd service, then exit",
)
def main(
    host: str,
    port: int,
    scheme: str,
    key_dir: str,
    cert: str | None,
    key: str | None,
    no_generate: bool,
    common_name: str,
    san: tuple[str, ...],
    days: int,
    install_systemd: bool,
    uninstall_systemd: bool,
) -> None:
    """Preflight TLS material if needed, then serve the webapp."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if install_systemd and uninstall_systemd:
        raise click.UsageError(
            "--install-systemd and --uninstall-systemd are mutually exclusive"
        )
    if install_systemd:
        warning = pidfile_warning()
        if warning:
            click.echo(warning, err=True)
        exec_args = build_exec_args(
            host, port, scheme, key_dir, cert, key, common_name, san, days
        )
        install_unit(build_unit(exec_args))
        click.echo(f"installed and enabled {UNIT_NAME}")
        click.echo("logs: journalctl -u sms-webapp -f")
        return
    if uninstall_systemd:
        uninstall_unit()
        click.echo(f"removed {UNIT_NAME}")
        return
    dir_path = Path(key_dir)
    cert_path = Path(cert) if cert else dir_path / "cert.pem"
    key_path = Path(key) if key else dir_path / "key.pem"
    certfile = keyfile = None
    if scheme == "https":
        sans = list(san) if san else DEFAULT_SANS
        certfile, keyfile = preflight_tls(
            cert_path,
            key_path,
            dir_path,
            no_generate,
            common_name,
            sans,
            days,
        )
    run_app(host, port, certfile, keyfile)
