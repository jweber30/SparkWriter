#!/bin/bash
set -euo pipefail

# 1) Write the env file
mkdir -p /etc/tailscale
cat >/etc/tailscale/firstboot.env <<EOF
TS_AUTHKEY={{authkey}}
{% if hostname %}TS_HOSTNAME={{hostname}}{% else %}TS_HOSTNAME=pve-$(hostname){% endif %}
TS_EXTRA_FLAGS="--ssh"
{% if apt-proxy %}APT_CACHE_URL={{apt-proxy}}{% else %}APT_CACHE_URL={% endif %}
{% if tailscale-domain %}TS_DOMAIN={{tailscale-domain}}{% else %}TS_DOMAIN={% endif %}
EOF

# 2) Write the real firstboot script (Proxmox 9 focus)
cat >/etc/tailscale/firstboot.sh <<'EOF_FIRSTBOOT'
#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

ENV_FILE="/etc/tailscale/firstboot.env"
STAGE_FILE="/etc/tailscale/firstboot.stage"
DEFAULT_STAGE="prep"

log() {
    echo "[spark-firstboot] $*"
}

ensure_stage_file() {
    if [[ ! -f "$STAGE_FILE" ]]; then
        echo "$DEFAULT_STAGE" >"$STAGE_FILE"
    fi
}

current_stage() {
    ensure_stage_file
    tr -d '\r\n' <"$STAGE_FILE"
}

set_stage() {
    echo "$1" >"$STAGE_FILE"
}

deploy_cert_files() {
    local src_cert="$1"
    local src_key="$2"

    cat "$src_cert" >/etc/pve/local/pveproxy-ssl.pem
    cat "$src_key" >/etc/pve/local/pveproxy-ssl.key
    chmod 640 /etc/pve/local/pveproxy-ssl.pem /etc/pve/local/pveproxy-ssl.key || true
}

install_cert_cron() {
    local hostname="$1"

    cat >/usr/local/sbin/renew-pveproxy-cert <<'EOF_RENEW'
#!/usr/bin/env bash
set -euo pipefail

CERT_HOSTNAME="$1"

tailscale cert --cert-file=/root/pveproxy-ssl.pem --key-file=/root/pveproxy-ssl.key "${CERT_HOSTNAME}"
cat /root/pveproxy-ssl.pem >/etc/pve/local/pveproxy-ssl.pem
cat /root/pveproxy-ssl.key >/etc/pve/local/pveproxy-ssl.key
chmod 640 /etc/pve/local/pveproxy-ssl.pem /etc/pve/local/pveproxy-ssl.key || true
systemctl restart pveproxy
EOF_RENEW

    chmod 700 /usr/local/sbin/renew-pveproxy-cert

    cat >/etc/cron.d/renew-pveproxy-cert <<EOF_CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

0 3 1 * * root /usr/local/sbin/renew-pveproxy-cert "${hostname}" >>/var/log/renew-pveproxy-cert.log 2>&1
EOF_CRON
}

configure_apt_cache() {
    if [[ -z "${APT_CACHE_URL:-}" ]]; then
        return
    fi

    log "Configuring APT to use proxy ${APT_CACHE_URL}"
    cat >/etc/apt/apt.conf.d/99-spark-apt-proxy <<EOF_APT_PROXY
Acquire::http::Proxy "${APT_CACHE_URL}";
Acquire::https::Proxy "${APT_CACHE_URL}";
EOF_APT_PROXY
}

write_trixie_sources() {
    mkdir -p /etc/apt/sources.list.d
    rm -f /etc/apt/sources.list.d/*.list 2>/dev/null || true
    rm -f /etc/apt/sources.list.d/*.sources 2>/dev/null || true

    cat >/etc/apt/sources.list.d/debian.sources <<'EOF_DEBIAN'
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie
Components: main contrib
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://security.debian.org/debian-security
Suites: trixie-security
Components: main contrib
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie-updates
Components: main contrib
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF_DEBIAN

    cat >/etc/apt/sources.list.d/proxmox.sources <<'EOF_PVE_TRIXIE'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF_PVE_TRIXIE

    cat >/etc/apt/sources.list.d/ceph.sources <<'EOF_CEPH_TRIXIE'
Types: deb
URIs: http://download.proxmox.com/debian/ceph-squid
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF_CEPH_TRIXIE

}

apply_repo_policy() {
    if ! command -v pveversion >/dev/null 2>&1; then
        log "pveversion not found; skipping repo normalization"
        return
    fi

    local ver major
    ver=$(pveversion | awk -F'/' '{print $2}' | awk -F'-' '{print $1}')
    IFS='.' read -r major _ <<<"$ver"

    if [[ "$major" != "9" ]]; then
        log "Unsupported Proxmox version $ver; skipping repo normalization"
        return
    fi

    write_trixie_sources
}

run_apt_refresh() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get -o Dpkg::Options::="--force-confold" -y dist-upgrade
}

first_stage() {
    log "Normalizing Proxmox repositories (Trixie)"
    apply_repo_policy

    configure_apt_cache

    log "Refreshing packages"
    run_apt_refresh

    set_stage "post-upgrade"
    log "Rebooting to apply updates"
    systemctl reboot || reboot || true
    exit 0
}

second_stage() {
    log "Installing Tailscale"
    curl -fsSL https://tailscale.com/install.sh | sh

    log "Bringing Tailscale online"
    tailscale up \
        --authkey="${TS_AUTHKEY}" \
        --hostname="${TS_HOSTNAME:-$(hostname)}" \
        ${TS_EXTRA_FLAGS:-}

    set_stage "post-tailscale"
    log "Rebooting to finalize Tailscale registration"
    systemctl reboot || reboot || true
    exit 0
}

third_stage() {
    local cert_hostname="${TS_HOSTNAME:-$(hostname)}"
    if [[ -n "${TS_DOMAIN:-}" ]]; then
        cert_hostname="${cert_hostname}.${TS_DOMAIN#.}"
    fi

    log "Waiting for Tailscale to be fully online..."
    local max_wait=300
    local waited=0
    while ! tailscale status --json | grep -q '"BackendState":[[:space:]]*"Running"'; do
        if [[ $waited -ge $max_wait ]]; then
            log "ERROR: Tailscale did not reach Running state within ${max_wait}s"
            exit 1
        fi
        sleep 5
        waited=$((waited + 5))
        log "Still waiting for Tailscale... (${waited}s)"
    done
    log "Tailscale is fully online"

    # Additional wait to ensure control plane sync is complete (important)
    sleep 30

    log "Issuing Tailscale certificate for ${cert_hostname}"
    tailscale cert --cert-file=/root/pveproxy-ssl.pem --key-file=/root/pveproxy-ssl.key "${cert_hostname}"
    deploy_cert_files /root/pveproxy-ssl.pem /root/pveproxy-ssl.key
    systemctl restart pveproxy

    install_cert_cron "$cert_hostname"

    rm -f "$ENV_FILE" "$STAGE_FILE"
    systemctl disable ts-firstboot.service || true
}

main() {
    if [[ ! -f "$ENV_FILE" ]]; then
      log "No firstboot env file found, nothing to do."
      exit 0
    fi

    source "$ENV_FILE"
    if [[ -z "${TS_AUTHKEY:-}" ]]; then
        echo "TS_AUTHKEY not set, aborting."
        exit 1
    fi

    local stage
    stage="$(current_stage)"
    case "$stage" in
        prep)
            first_stage
            ;;
        post-upgrade)
            second_stage
            ;;
        post-tailscale)
            third_stage
            ;;
        *)
            log "Unknown stage '$stage', running tailscale install"
            second_stage
            ;;
    esac
}

main
EOF_FIRSTBOOT

chmod 700 /etc/tailscale/firstboot.sh

# 3) Write the systemd unit
cat >/etc/systemd/system/ts-firstboot.service <<'EOF_TS_UNIT'
[Unit]
Description=One-time Tailscale first-boot provisioning
Requires=network.target
After=network.target

[Service]
Type=oneshot
ExecStart=/etc/tailscale/firstboot.sh
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
EOF_TS_UNIT

systemctl daemon-reload
systemctl enable ts-firstboot.service
systemctl start ts-firstboot.service || true
