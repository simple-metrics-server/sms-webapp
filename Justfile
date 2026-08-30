# webapp Justfile

set shell := ["bash", "-c"]

venv    := ".venv"
py      := venv / "bin" / "python"
pip     := venv / "bin" / "pip"
pidfile := "webapp.pid"
logfile := "webapp.log"
port    := "9555"
mode    := env_var_or_default("MODE", "https")

# list available targets
default:
    @just --list

# create the virtual environment
prepare:
    @test -d {{ venv }} || python3 -m venv {{ venv }}
    @echo "venv ready: {{ venv }}"

# install the package into the venv (editable, with dev dependencies)
install-venv: prepare
    {{ pip }} install -q -e ".[dev]"
    @echo "installed"

# build a wheel into dist/
build: prepare
    {{ pip }} wheel -q --no-deps -w dist .
    @echo "built into dist/"

# remove all generated files
clean: stop
    rm -rf {{ venv }} dist build *.egg-info .pytest_cache .mypy_cache .ruff_cache
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    rm -f {{ logfile }} data/metrics.cache
    rm -rf data/keys
    @echo "clean"

# lint the code (ruff + black check)
lint: install-venv
    {{ venv }}/bin/ruff check webapp/ tests/
    {{ venv }}/bin/black --check webapp/ tests/
    @echo "lint ok"

# run the test suite
test: install-venv
    {{ py }} -m pytest tests/ -q

# run lint and tests
check: lint test
    @echo "check ok"

# start the app in the background (MODE=https for TLS)
start: install-venv
    @if [ -f {{ pidfile }} ] && kill -0 "$(cat {{ pidfile }})" 2>/dev/null; then \
        echo "already running (pid $(cat {{ pidfile }}))"; \
    else \
        nohup {{ py }} -m webapp.main --scheme {{ mode }} > {{ logfile }} 2>&1 & \
        echo $! > {{ pidfile }}; \
        sleep 1; \
        echo "started (pid $(cat {{ pidfile }})) on {{ mode }}://0.0.0.0:{{ port }}/metrics"; \
    fi

# stop the app
stop:
    @if [ -f {{ pidfile }} ] && kill -0 "$(cat {{ pidfile }})" 2>/dev/null; then \
        kill "$(cat {{ pidfile }})"; \
        echo "stopped (pid $(cat {{ pidfile }}))"; \
    else \
        echo "not running"; \
    fi; \
    rm -f {{ pidfile }}

# install the app as a systemd service enabled on boot (sudo prompt)
install-systemd: install-venv
    {{ py }} -m webapp.main --scheme {{ mode }} --install-systemd

# remove the systemd service
uninstall-systemd: install-venv
    {{ py }} -m webapp.main --uninstall-systemd
