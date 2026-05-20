#!/bin/bash
# poly-stack AWS user-data bootstrap (Amazon Linux 2023 / Ubuntu 22.04).
#
# Provisions a t3.medium in us-east-1 with:
#   - Python 3.11 + venv
#   - Rust toolchain (for poly-signer)
#   - The sentinel-portfolio-manager repo (sentinel-poly branch)
#   - systemd units for poly-signal-bus, poly-runner, poly-signer
#
# Run as EC2 user-data (cloud-init executes as root).
# Required ENV (set via AWS Systems Manager Parameter Store or instance
# metadata; do NOT bake into AMI):
#   POLY_PRIVATE_KEY, POLY_MAKER_ADDRESS, POLY_SIG_TYPE,
#   POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE,
#   CL_API_KEY, CL_API_SECRET, POLYGON_RPC,
#   HALT_TOKEN, GITHUB_TOKEN

set -euxo pipefail

# ────────────────────────── Detect distro ──────────────────────────
if [ -f /etc/os-release ]; then
    . /etc/os-release
fi

# ────────────────────────── System packages ──────────────────────────
if [[ "${ID:-}" == "amzn" ]] || [[ "${ID:-}" == "rhel" ]]; then
    dnf -y update
    dnf -y install python3.11 python3.11-pip python3.11-devel git \
        gcc gcc-c++ make pkgconfig openssl-devel curl tar gzip systemd
    PYTHON=/usr/bin/python3.11
elif [[ "${ID:-}" == "ubuntu" ]]; then
    apt-get update
    apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip \
        git build-essential pkg-config libssl-dev curl ca-certificates
    PYTHON=/usr/bin/python3.11
else
    echo "unknown distro; aborting"; exit 1
fi

# ────────────────────────── User ──────────────────────────
useradd -m -s /bin/bash poly || true
install -d -o poly -g poly /home/poly/.cargo
install -d -o poly -g poly /var/data
install -d -o poly -g poly /var/log/poly

# ────────────────────────── Rust ──────────────────────────
sudo -u poly bash -lc 'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal'

# ────────────────────────── Clone repo ──────────────────────────
sudo -u poly bash -lc "
    cd /home/poly &&
    if [[ -n \"${GITHUB_TOKEN:-}\" ]]; then
      git clone -b sentinel-poly \
        https://Dapperscyphozoa:${GITHUB_TOKEN}@github.com/Dapperscyphozoa/sentinel-portfolio-manager.git spm
    else
      git clone -b sentinel-poly \
        https://github.com/Dapperscyphozoa/sentinel-portfolio-manager.git spm
    fi
"

# ────────────────────────── Python venv ──────────────────────────
sudo -u poly bash -lc "
    cd /home/poly/spm &&
    $PYTHON -m venv .venv &&
    source .venv/bin/activate &&
    pip install --upgrade pip wheel &&
    pip install httpx websockets aiohttp
    if [[ -f requirements.txt ]]; then
        pip install -r requirements.txt
    fi
"

# ────────────────────────── Build signer ──────────────────────────
sudo -u poly bash -lc "
    cd /home/poly/spm/poly_signer &&
    source /home/poly/.cargo/env &&
    cargo build --release
"

# ────────────────────────── systemd units ──────────────────────────
cat >/etc/systemd/system/poly-signal-bus.service <<'UNIT'
[Unit]
Description=poly-signal-bus
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=poly
Group=poly
WorkingDirectory=/home/poly/spm
EnvironmentFile=-/etc/poly/poly-env
Environment=PYTHONPATH=/home/poly/spm
ExecStart=/home/poly/spm/.venv/bin/python -m poly_signal_bus.server
Restart=always
RestartSec=5
StandardOutput=append:/var/log/poly/signal-bus.log
StandardError=append:/var/log/poly/signal-bus.log

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/poly-signer.service <<'UNIT'
[Unit]
Description=poly-signer (Rust EIP-712 signer)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=poly
Group=poly
WorkingDirectory=/home/poly/spm/poly_signer
EnvironmentFile=-/etc/poly/poly-env
ExecStart=/home/poly/spm/poly_signer/target/release/poly-signer
Restart=always
RestartSec=5
StandardOutput=append:/var/log/poly/signer.log
StandardError=append:/var/log/poly/signer.log

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/poly-runner.service <<'UNIT'
[Unit]
Description=poly-runner
After=poly-signal-bus.service poly-signer.service
Requires=poly-signal-bus.service poly-signer.service

[Service]
Type=simple
User=poly
Group=poly
WorkingDirectory=/home/poly/spm
EnvironmentFile=-/etc/poly/poly-env
Environment=PYTHONPATH=/home/poly/spm
ExecStart=/home/poly/spm/.venv/bin/python -m poly_runner.server
Restart=always
RestartSec=5
StandardOutput=append:/var/log/poly/runner.log
StandardError=append:/var/log/poly/runner.log

[Install]
WantedBy=multi-user.target
UNIT

install -d -m 750 -o root -g poly /etc/poly
if [[ ! -f /etc/poly/poly-env ]]; then
  # Empty placeholder; operator must populate via secure provisioning
  install -m 640 -o root -g poly /dev/null /etc/poly/poly-env
  cat >>/etc/poly/poly-env <<'EOF'
# Populate before starting services. Example template (DO NOT commit real values):
# POLY_PRIVATE_KEY=
# POLY_MAKER_ADDRESS=
# POLY_SIG_TYPE=proxy
# POLY_API_KEY=
# POLY_API_SECRET=
# POLY_API_PASSPHRASE=
# CL_API_KEY=
# CL_API_SECRET=
# POLY_LIVE=0
# STATE_DIR=/var/data
# HALT_TOKEN=
EOF
fi

systemctl daemon-reload
systemctl enable poly-signal-bus poly-signer poly-runner
echo "BOOTSTRAP COMPLETE — populate /etc/poly/poly-env then: systemctl start poly-signal-bus poly-signer poly-runner"
