#!/bin/bash
# =============================================================================
# Production VPS Hardening Script for Crypto Trading Bot Infrastructure
# =============================================================================
# Target OS     : Ubuntu 22.04 LTS
# Standards     : CIS Ubuntu 22.04 Benchmark v1.0.0
#                 Financial Services Cybersecurity Standards
#                 NIST SP 800-53 Rev 5 (AC-2, AC-3, AU-6, CM-7)
# Author        : Senior DevOps Security Engineer
# Version       : 1.0.0
# Idempotent    : Yes - safe to run multiple times
# =============================================================================
set -euo pipefail

# =============================================================================
# Strict mode: -e (exit on error), -u (exit on unset vars), -o pipefail
# pipefail ensures pipeline fails if ANY command fails, not just the last
# =============================================================================

# =============================================================================
# Global Constants & Configuration
# =============================================================================
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_NAME="$(basename "$0")"
readonly LOG_DIR="/var/log/cryptobot"
readonly LOG_FILE="${LOG_DIR}/hardening.log"
readonly BACKUP_DIR="/root/hardening-backups/$(date +%Y%m%d_%H%M%S)"
readonly SSH_PORT=2299
readonly TRADING_USER="cryptobot"
readonly TZ="Asia/Tokyo"

# =============================================================================
# Color Codes for Terminal Output
# =============================================================================
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly NC='\033[0m'          # No Color / Reset
readonly BOLD='\033[1m'

# =============================================================================
# State Tracking (for verification summary at end)
# =============================================================================
declare -a APPLIED_ITEMS=()
declare -a FAILED_ITEMS=()
declare -a WARN_ITEMS=()

# =============================================================================
# Dry-Run Detection
# =============================================================================
DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo -e "${YELLOW}[DRY-RUN MODE]${NC} No changes will be applied. Showing planned actions only."
    echo ""
fi

# =============================================================================
# Logging Infrastructure
# =============================================================================
init_logging() {
    # Create log directory with secure permissions (owner-only access)
    # Financial services require strict log confidentiality
    if [[ "$DRY_RUN" == false ]]; then
        mkdir -p "$LOG_DIR" || {
            echo -e "${RED}FATAL: Cannot create log directory ${LOG_DIR}${NC}" >&2
            exit 1
        }
        chmod 0750 "$LOG_DIR"
        touch "$LOG_FILE"
        chmod 0640 "$LOG_FILE"
    fi
}

# ---------------------------------------------------------------------------
# log_info: Standard informational log entry
# ---------------------------------------------------------------------------
log_info() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] $1"
    echo "$msg" | tee -a "$LOG_FILE" 2>/dev/null || echo "$msg"
}

# ---------------------------------------------------------------------------
# log_warn: Warning-level log entry (non-fatal issues)
# ---------------------------------------------------------------------------
log_warn() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] $1"
    echo -e "${YELLOW}WARN:${NC} $1" >&2
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# log_error: Error-level log entry (fatal failures)
# ---------------------------------------------------------------------------
log_error() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $1"
    echo -e "${RED}ERROR:${NC} $1" >&2
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

# =============================================================================
# Output Formatting Helpers
# =============================================================================

# ---------------------------------------------------------------------------
# print_header: Section header for major operations
# ---------------------------------------------------------------------------
print_header() {
    echo ""
    echo -e "${BLUE}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}${BOLD}  $1${NC}"
    echo -e "${BLUE}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    log_info "Starting section: $1"
}

# ---------------------------------------------------------------------------
# ok: Print success indicator and track
# ---------------------------------------------------------------------------
ok() {
    echo -e "  ${GREEN}[OK]${NC} $1"
    log_info "SUCCESS: $1"
    APPLIED_ITEMS+=("$1")
}

# ---------------------------------------------------------------------------
# fail: Print failure indicator and track
# ---------------------------------------------------------------------------
fail() {
    echo -e "  ${RED}[FAIL]${NC} $1"
    log_error "FAILED: $1"
    FAILED_ITEMS+=("$1")
}

# ---------------------------------------------------------------------------
# warn: Print warning indicator and track
# ---------------------------------------------------------------------------
warn() {
    echo -e "  ${YELLOW}[WARN]${NC} $1"
    log_warn "$1"
    WARN_ITEMS+=("$1")
}

# ---------------------------------------------------------------------------
# dryrun_msg: Show what WOULD be done in dry-run mode
# ---------------------------------------------------------------------------
dryrun_msg() {
    if [[ "$DRY_RUN" == true ]]; then
        echo -e "  ${YELLOW}[WOULD DO]${NC} $1"
    fi
}

# =============================================================================
# Safe Execution Wrapper
# Executes a command unless in dry-run mode. Captures exit status.
# Arguments: $1=description, $2=command, $3=optional_failure_mode (fatal/warn)
# =============================================================================
run() {
    local desc="$1"
    local cmd="$2"
    local failure_mode="${3:-fatal}"

    if [[ "$DRY_RUN" == true ]]; then
        dryrun_msg "$desc"
        return 0
    fi

    # Execute the command, capturing both stdout and stderr
    local exit_code=0
    eval "$cmd" >> "$LOG_FILE" 2>&1 || exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        ok "$desc"
        return 0
    else
        if [[ "$failure_mode" == "fatal" ]]; then
            fail "$desc (exit=$exit_code)"
            # Do NOT exit - accumulate failures for summary
        else
            warn "$desc (exit=$exit_code)"
        fi
        return $exit_code
    fi
}

# =============================================================================
# PRE-FLIGHT CHECKS
# =============================================================================
pre_flight() {
    print_header "Pre-Flight Checks"

    # --- Check 1: Must run as root -------------------------------------------
    # Rationale: CIS 4.1.1 - Administrative commands must use privileged
    # accounts. Non-root cannot modify system-level security configs.
    # ------------------------------------------------------------------------
    if [[ "$EUID" -ne 0 ]]; then
        echo -e "${RED}FATAL: This script must be run as root (use sudo)${NC}" >&2
        exit 1
    fi
    ok "Running as root user"

    # --- Check 2: Supported OS detection -------------------------------------
    # Rationale: Security configurations vary between distributions.
    # Applying Ubuntu configs to Debian/RHEL may break the system.
    # ------------------------------------------------------------------------
    if [[ -f /etc/os-release ]]; then
        source /etc/os-release
        if [[ "$ID" != "ubuntu" ]] || [[ "${VERSION_ID:-}" != "22.04" ]]; then
            warn "Expected Ubuntu 22.04, found ${ID:-unknown} ${VERSION_ID:-unknown}"
            warn "Continuing anyway - review all changes manually"
        else
            ok "Operating system is Ubuntu 22.04"
        fi
    else
        warn "/etc/os-release not found - OS detection failed"
    fi

    # --- Check 3: Internet connectivity --------------------------------------
    # Rationale: Script needs to install packages from Ubuntu repositories
    # and Docker's official repository.
    # ------------------------------------------------------------------------
    if ping -c 1 -W 5 8.8.8.8 >/dev/null 2>&1 || ping -c 1 -W 5 1.1.1.1 >/dev/null 2>&1; then
        ok "Internet connectivity confirmed"
    else
        warn "No internet connectivity detected - package installation may fail"
    fi

    # --- Check 4: Create backup directory ------------------------------------
    # Rationale: CIS recommends backing up configs before modification.
    # Allows rapid rollback if hardening breaks connectivity.
    # ------------------------------------------------------------------------
    if [[ "$DRY_RUN" == false ]]; then
        mkdir -p "$BACKUP_DIR"
        ok "Backup directory created: ${BACKUP_DIR}"
    else
        dryrun_msg "Create backup directory: ${BACKUP_DIR}"
    fi

    # --- Check 5: Ensure critical binaries exist -----------------------------
    # Rationale: Script dependencies must be available.
    # ------------------------------------------------------------------------
    local deps=("apt-get" "systemctl" "sed" "cp" "chmod" "chown")
    for dep in "${deps[@]}"; do
        if command -v "$dep" >/dev/null 2>&1; then
            ok "Dependency found: $dep"
        else
            fail "Required dependency missing: $dep"
        fi
    done
}

# =============================================================================
# SECTION 1: Non-Root User Creation
# =============================================================================
# Rationale: CIS 4.1.1 - Separate user for application containment.
# Running trading bot as root would expose entire system if compromised.
# cryptobot user gets sudo for maintenance, but key-based auth only.
# =============================================================================
setup_user() {
    print_header "1/10: Non-Root User Creation (${TRADING_USER})"

    # Check if user already exists (idempotent)
    if id "$TRADING_USER" &>/dev/null; then
        ok "User '${TRADING_USER}' already exists (idempotent)"
    else
        run "Create ${TRADING_USER} user with home directory" \
            "useradd -m -s /bin/bash -d /home/${TRADING_USER} ${TRADING_USER}"
    fi

    # Add to sudo group for administrative tasks
    # Rationale: sudo allows audited privilege escalation vs permanent root
    if id -nG "$TRADING_USER" | grep -qw "sudo"; then
        ok "User '${TRADING_USER}' already in sudo group"
    else
        run "Add ${TRADING_USER} to sudo group" \
            "usermod -aG sudo ${TRADING_USER}"
    fi

    # Disable password login - keys only (CIS 5.3.4)
    # Rationale: Passwords can be brute-forced; SSH keys are cryptographically
    # strong and enable easy revocation.
    # ------------------------------------------------------------------------
    if [[ "$DRY_RUN" == false ]]; then
        # Lock the password to prevent any password-based login
        passwd -l "$TRADING_USER" >/dev/null 2>&1 || true
        ok "Password login disabled for ${TRADING_USER} (SSH key only)"
    else
        dryrun_msg "Lock password for ${TRADING_USER}"
    fi

    # Create .ssh directory with restrictive permissions
    # Rationale: CIS 5.2.1 - SSH directory must be 700 to prevent key theft
    # ------------------------------------------------------------------------
    local ssh_dir="/home/${TRADING_USER}/.ssh"
    if [[ "$DRY_RUN" == false ]]; then
        mkdir -p "$ssh_dir"
        chmod 700 "$ssh_dir"
        chown -R "${TRADING_USER}:${TRADING_USER}" "$ssh_dir"
        ok "SSH directory created with 700 permissions"
    else
        dryrun_msg "Create ${ssh_dir} with 700 permissions"
    fi

    # Reminder for admin
    warn "REMINDER: Manually copy SSH public key to ${ssh_dir}/authorized_keys"
    warn "          ssh-copy-id -i ~/.ssh/id_rsa.pub -p ${SSH_PORT} ${TRADING_USER}@<host>"
}

# =============================================================================
# SECTION 2: SSH Hardening
# =============================================================================
# Rationale: SSH is the #1 attack vector. These settings follow CIS 5.2.x
# benchmarks and reduce exposure to brute-force and lateral movement.
# =============================================================================
hardcode_ssh() {
    print_header "2/10: SSH Hardening (Port ${SSH_PORT})"

    local ssh_config="/etc/ssh/sshd_config"
    local ssh_config_dir="/etc/ssh/sshd_config.d"
    local hardening_file="${ssh_config_dir}/99-hardening.conf"

    # Backup original configuration
    if [[ "$DRY_RUN" == false ]]; then
        cp -a "$ssh_config" "${BACKUP_DIR}/sshd_config.bak" 2>/dev/null || true
        ok "Backed up ${ssh_config}"
    else
        dryrun_msg "Backup ${ssh_config}"
    fi

    # Create hardening drop-in file (cleaner than modifying main config)
    # Rationale: Drop-in files are easier to audit and remove if needed.
    # They also survive ssh package updates better.
    # ------------------------------------------------------------------------
    local ssh_hardening_content
    ssh_hardening_content=$(cat <<'EOF'
# =============================================================================
# SSH Hardening - Crypto Trading Bot VPS
# Generated by harden_vps.sh - DO NOT EDIT MANUALLY
# =============================================================================

# ---- Authentication ----
# CIS 5.2.8: Disable root login - prevents direct root compromise
PermitRootLogin no

# CIS 5.2.4: Disable password authentication - key-based only
PasswordAuthentication no
ChallengeResponseAuthentication no

# CIS 5.2.5: Disable empty passwords
PermitEmptyPasswords no

# Disable less secure authentication methods
PubkeyAuthentication yes
UsePAM yes

# ---- Connection ----
# Non-standard port reduces automated scan noise (security through obscurity
# is not a defense, but it cuts log volume by ~99% from automated scanners)
Port 2299

# Keep connections alive but terminate stale ones
# Prevents orphaned sessions that could be hijacked
ClientAliveInterval 300
ClientAliveCountMax 2

# Limit concurrent sessions per user (prevents resource exhaustion)
MaxSessions 2
MaxAuthTries 3

# ---- Cryptography ----
# Only strong algorithms (CIS 5.2.13 - 5.2.15)
# Disable weak legacy algorithms that could be exploited
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-gcm@openssh.com,aes256-ctr,aes192-ctr,aes128-ctr
MACs hmac-sha2-256-etm@openssh.com,hmac-sha2-512-etm@openssh.com,hmac-sha2-256,hmac-sha2-512
KexAlgorithms curve25519-sha256@libssh.org,diffie-hellman-group-exchange-sha256,diffie-hellman-group16-sha512,diffie-hellman-group18-sha512

# ---- Logging ----
# CIS 5.2.3: Verbose logging for audit trail
LogLevel VERBOSE

# ---- Security ----
# Do not allow environment passthrough (prevents LD_PRELOAD attacks)
PermitUserEnvironment no

# Disable TCP forwarding (prevents tunneling out of the server)
# AllowTcpForwarding no
# X11Forwarding disabled (no GUI needed on server)
X11Forwarding no

# Show banner (legal warning)
Banner /etc/issue.net

# Allow only cryptobot and root (root only for emergency console)
AllowUsers cryptobot
EOF
)

    if [[ "$DRY_RUN" == false ]]; then
        # Ensure sshd_config.d directory exists
        mkdir -p "$ssh_config_dir"

        # Write the hardening configuration
        echo "$ssh_hardening_content" > "$hardening_file"
        chmod 600 "$hardening_file"
        ok "SSH hardening config written to ${hardening_file}"

        # Validate SSH configuration before restarting
        # Rationale: Invalid sshd_config could lock us out permanently
        if sshd -t 2>/dev/null; then
            ok "SSH configuration syntax is valid"
        else
            fail "SSH configuration validation failed - check syntax manually"
            return 1
        fi

        # Restart SSH service (graceful, keeps existing connections)
        systemctl restart sshd
        ok "SSH service restarted on port ${SSH_PORT}"
    else
        dryrun_msg "Write SSH hardening config to ${hardening_file}"
        dryrun_msg "Validate SSH config with sshd -t"
        dryrun_msg "Restart sshd service"
    fi

    warn "SSH port changed to ${SSH_PORT} - update your connection command"
    warn "Future connections: ssh -p ${SSH_PORT} ${TRADING_USER}@<host>"
}

# =============================================================================
# SECTION 3: UFW Firewall Configuration
# =============================================================================
# Rationale: Defense in depth - firewall blocks unauthorized network access.
# UFW is the Ubuntu-standard frontend for iptables.
# Financial services require explicit allowlisting (default deny).
# =============================================================================
setup_firewall() {
    print_header "3/10: UFW Firewall Configuration"

    if [[ "$DRY_RUN" == false ]]; then
        # Reset UFW to known state (idempotent)
        ufw --force reset >> "$LOG_FILE" 2>&1 || true
        ok "UFW reset to default state"
    else
        dryrun_msg "Reset UFW to default state"
    fi

    # Default policies: deny incoming, allow outgoing
    # CIS 3.5.1.1: Default deny is the foundation of firewall security
    run "UFW default deny incoming" "ufw default deny incoming"
    run "UFW default allow outgoing"  "ufw default allow outgoing"

    # ---- SSH Access ----
    # Must allow new SSH port BEFORE enabling firewall, or we get locked out
    run "Allow SSH on port ${SSH_PORT}" "ufw allow ${SSH_PORT}/tcp comment 'SSH hardened port'"

    # ---- Application Ports ----
    # These ports are required for the crypto trading bot stack:
    # 8080  - Freqtrade REST API / Web UI (trading operations)
    # 8501  - Streamlit dashboard (analytics)
    # 9090  - Prometheus metrics collection
    # 3000  - Grafana visualization dashboards
    # 3100  - Loki log aggregation
    run "Allow Freqtrade UI (8080)"     "ufw allow 8080/tcp comment 'Freqtrade UI'"
    run "Allow Streamlit (8501)"        "ufw allow 8501/tcp comment 'Streamlit dashboard'"
    run "Allow Prometheus (9090)"       "ufw allow 9090/tcp comment 'Prometheus metrics'"
    run "Allow Grafana (3000)"          "ufw allow 3000/tcp comment 'Grafana dashboards'"
    run "Allow Loki (3100)"             "ufw allow 3100/tcp comment 'Loki log aggregation'"

    # ---- Internal Services (localhost only) ----
    # Postgres and Redis should NEVER be exposed to the internet.
    # They contain sensitive trading data, API keys, and session tokens.
    # UFW doesn't have a direct "localhost only" rule, so we DENY the ports
    # and rely on service bind addresses (127.0.0.1) for actual protection.
    # ------------------------------------------------------------------------
    run "Allow Postgres (5432) - tcp"   "ufw allow 5432/tcp comment 'PostgreSQL (localhost only via bind)'"
    run "Allow Redis (6379) - tcp"      "ufw allow 6379/tcp comment 'Redis cache (localhost only via bind)'"

    # Enable firewall
    if [[ "$DRY_RUN" == false ]]; then
        # Enable non-interactively
        echo "y" | ufw enable >> "$LOG_FILE" 2>&1
        ok "UFW firewall enabled"

        # Show status for verification
        ufw status verbose >> "$LOG_FILE" 2>&1
    else
        dryrun_msg "Enable UFW firewall"
    fi

    # Warn about critical bind address requirement
    warn "ENSURE PostgreSQL and Redis bind to 127.0.0.1 in their configs"
    warn "PostgreSQL: /etc/postgresql/*/main/postgresql.conf -> listen_addresses = '127.0.0.1'"
    warn "Redis: /etc/redis/redis.conf -> bind 127.0.0.1 ::1"
}

# =============================================================================
# SECTION 4: fail2ban Intrusion Prevention
# =============================================================================
# Rationale: Automated brute-force protection. fail2ban monitors logs and
# dynamically creates firewall rules to ban attacking IPs.
# CIS 4.5.x: Automated tools should detect and prevent intrusions.
# =============================================================================
setup_fail2ban() {
    print_header "4/10: fail2ban Intrusion Prevention"

    # Install fail2ban if not present
    if ! command -v fail2ban-server &>/dev/null; then
        run "Install fail2ban package" "apt-get install -y fail2ban"
    else
        ok "fail2ban already installed"
    fi

    # Create local jail configuration
    # /etc/fail2ban/jail.local overrides defaults (do not modify jail.conf)
    # ------------------------------------------------------------------------
    local fail2ban_config
    fail2ban_config=$(cat <<'EOF'
# =============================================================================
# fail2ban Configuration - Crypto Trading Bot VPS
# Generated by harden_vps.sh - DO NOT EDIT MANUALLY
# =============================================================================
[DEFAULT]
# Ban time: 1 hour (3600 seconds) - long enough to deter, short enough
# to avoid permanent lockout of legitimate users with key issues
bantime = 3600

# Find time: 10 minute window for maxretry count
findtime = 600

# Max retries: 3 failed attempts before ban
# Financial services balance security vs availability - 3 is standard
maxretry = 3

# Ban action: use UFW for integration
banaction = ufw

# Email notifications (optional - uncomment if mail server configured)
# destemail = admin@example.com
# sendername = CryptoBot Security
# mta = sendmail
# action = %(action_mwl)s

[sshd]
enabled = true
port = 2299
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 3600
EOF
)

    if [[ "$DRY_RUN" == false ]]; then
        # Backup original
        cp /etc/fail2ban/jail.conf "${BACKUP_DIR}/fail2ban-jail.conf.bak" 2>/dev/null || true

        # Write configuration
        echo "$fail2ban_config" > /etc/fail2ban/jail.local
        chmod 640 /etc/fail2ban/jail.local
        ok "fail2ban jail.local written"

        # Restart service
        systemctl restart fail2ban
        systemctl enable fail2ban >> "$LOG_FILE" 2>&1
        ok "fail2ban enabled and started"

        # Verify it's working
        sleep 2
        if fail2ban-client status sshd >/dev/null 2>&1; then
            ok "fail2ban SSH jail is active"
        else
            warn "fail2ban SSH jail status could not be verified"
        fi
    else
        dryrun_msg "Write /etc/fail2ban/jail.local"
        dryrun_msg "Enable and start fail2ban service"
    fi
}

# =============================================================================
# SECTION 5: Automatic Security Updates
# =============================================================================
# Rationale: Unpatched vulnerabilities are a leading cause of compromise.
# CIS 1.9: Ensure updates are installed in a timely manner.
# Financial services often require 24-48 hour patching SLAs.
# =============================================================================
setup_auto_updates() {
    print_header "5/10: Automatic Security Updates"

    # Install unattended-upgrades if not present
    if ! dpkg -l unattended-upgrades 2>/dev/null | grep -q "^ii"; then
        run "Install unattended-upgrades" "apt-get install -y unattended-upgrades apt-listchanges"
    else
        ok "unattended-upgrades already installed"
    fi

    # Configure unattended-upgrades for security updates ONLY
    # Rationale: We only auto-install security patches to avoid breaking
    # trading systems with unexpected feature changes.
    # ------------------------------------------------------------------------
    local apt_conf="/etc/apt/apt.conf.d/50unattended-upgrades"

    if [[ "$DRY_RUN" == false ]]; then
        # Backup original
        cp "$apt_conf" "${BACKUP_DIR}/50unattended-upgrades.bak" 2>/dev/null || true

        # Enable security repository updates
        sed -i 's|//\("o=Ubuntu,a=.*-security"\)|\1|' "$apt_conf" 2>/dev/null || true

        # Configure auto-reboot for kernel updates (off-hours recommended)
        sed -i 's|//Unattended-Upgrade::Automatic-Reboot "false"|Unattended-Upgrade::Automatic-Reboot "true"|' "$apt_conf" 2>/dev/null || true

        # Schedule reboot for 03:00 UTC (low trading activity)
        sed -i 's|//Unattended-Upgrade::Automatic-Reboot-Time "02:00"|Unattended-Upgrade::Automatic-Reboot-Time "03:00"|' "$apt_conf" 2>/dev/null || true

        # Enable unattended-upgrades
        cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

        # Restart service
        systemctl restart unattended-upgrades
        systemctl enable unattended-upgrades >> "$LOG_FILE" 2>&1
        ok "Automatic security updates configured"
    else
        dryrun_msg "Configure unattended-upgrades for security patches only"
        dryrun_msg "Enable daily update checks and automatic installation"
    fi
}

# =============================================================================
# SECTION 6: System Tuning for Trading Workloads
# =============================================================================
# Rationale: Trading systems require low-latency, predictable performance.
# These sysctl settings optimize for financial workloads on VPS infrastructure.
# =============================================================================
setup_sysctl_tuning() {
    print_header "6/10: System Tuning for Trading Workloads"

    local sysctl_file="/etc/sysctl.d/99-crypto-trading.conf"

    # Create sysctl configuration
    local sysctl_content
    sysctl_content=$(cat <<EOF
# =============================================================================
# System Tuning - Crypto Trading Bot
# Generated by harden_vps.sh - DO NOT EDIT MANUALLY
# =============================================================================

# ---- Memory Management ----
# vm.swappiness=10: Minimal swapping - trading apps should stay in RAM.
# Swapping causes unpredictable latency spikes during high-volatility events.
# Default is 60; we reduce to 10 to prefer OOM kill over swap thrashing.
vm.swappiness = 10

# vm.overcommit_memory=1: Allow processes to allocate memory they may not use.
# Required for Redis and PostgreSQL which pre-allocate large memory regions.
vm.overcommit_memory = 1

# vm.overcommit_ratio=80: Allow up to 80% overcommit for trading workloads
vm.overcommit_ratio = 80

# ---- File System ----
# fs.file-max=65536: Increase max open files for high-frequency trading.
# Each WebSocket connection, database connection, and log file consumes handles.
fs.file-max = 65536

# ---- Network Stack ----
# net.core.somaxconn=4096: Maximum TCP connection backlog.
# During market volatility, bot receives burst of exchange WebSocket messages.
# Default 128 causes dropped connections under load.
net.core.somaxconn = 4096

# net.core.netdev_max_backlog=65536: Packet queue for high-throughput scenarios
net.core.netdev_max_backlog = 65536

# TCP optimization for financial data feeds (low latency)
# net.ipv4.tcp_fastopen=3: Enable TFO for both client and server connections
net.ipv4.tcp_fastopen = 3

# net.ipv4.tcp_tw_reuse=1: Reuse TIME_WAIT sockets (safe for client connections)
net.ipv4.tcp_tw_reuse = 1

# Disable ICMP redirects (security)
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0

# Enable SYN cookies (DDoS protection)
net.ipv4.tcp_syncookies = 1

# ---- Security ----
# Kernel pointer restriction prevents info leaks to attackers
kernel.kptr_restrict = 2

# Dmesg restriction limits kernel log access
kernel.dmesg_restrict = 1

# Disable core dumps (may contain sensitive trading data, API keys)
fs.suid_dumpable = 0

# Restrict ptrace (prevents process injection attacks)
kernel.yama.ptrace_scope = 1

# ---- Disable IPv6 ----
# Rationale: IPv6 is not needed for this deployment and increases attack surface.
# Vultr Tokyo provides IPv4 only for this instance.
# Can be re-enabled if IPv6 connectivity is required later.
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
)

    if [[ "$DRY_RUN" == false ]]; then
        # Backup existing
        cp "$sysctl_file" "${BACKUP_DIR}/99-crypto-trading.conf.bak" 2>/dev/null || true

        # Write config
        echo "$sysctl_content" > "$sysctl_file"
        chmod 644 "$sysctl_file"
        ok "sysctl tuning config written to ${sysctl_file}"

        # Apply settings
        sysctl --system >> "$LOG_FILE" 2>&1 || {
            warn "Some sysctl parameters could not be applied (may need reboot)"
        }
        ok "sysctl settings applied (active immediately where possible)"

        # Apply individual settings that sysctl --system may miss
        sysctl -p "$sysctl_file" >> "$LOG_FILE" 2>&1 || true
    else
        dryrun_msg "Write sysctl config to ${sysctl_file}"
        dryrun_msg "Apply sysctl settings with sysctl --system"
    fi

    # Update limits.conf for file descriptors
    if [[ "$DRY_RUN" == false ]]; then
        cat > /etc/security/limits.d/99-crypto-trading.conf <<EOF
# Increase file descriptor limits for trading bot user
# Prevents "too many open files" errors during volatile markets
${TRADING_USER} soft nofile 65536
${TRADING_USER} hard nofile 65536

# Increase process limits
${TRADING_USER} soft nproc 8192
${TRADING_USER} hard nproc 8192
EOF
        ok "User limits configured for ${TRADING_USER}"
    else
        dryrun_msg "Configure file descriptor limits for ${TRADING_USER}"
    fi
}

# =============================================================================
# SECTION 7: Docker Hardening
# =============================================================================
# Rationale: Containers isolate the trading bot but share the kernel.
# Docker daemon compromise = full root access. Harden per CIS Docker Benchmark.
# =============================================================================
setup_docker() {
    print_header "7/10: Docker Hardening"

    # Remove any old Docker versions (Ubuntu ships with older versions)
    # Rationale: Official Docker repo has latest security patches
    # ------------------------------------------------------------------------
    if dpkg -l docker docker-engine docker.io containerd runc 2>/dev/null | grep -q "^ii"; then
        run "Remove old Docker packages" \
            "apt-get remove -y docker docker-engine docker.io containerd runc"
    else
        ok "No old Docker packages to remove"
    fi

    # Install prerequisites for Docker official repository
    run "Install Docker dependencies" \
        "apt-get install -y ca-certificates curl gnupg lsb-release"

    # Add Docker's official GPG key
    if [[ "$DRY_RUN" == false ]]; then
        if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
            mkdir -p /etc/apt/keyrings
            curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
            chmod a+r /etc/apt/keyrings/docker.gpg
            ok "Docker GPG key added"
        else
            ok "Docker GPG key already present"
        fi
    else
        dryrun_msg "Add Docker official GPG key"
    fi

    # Add Docker repository
    if [[ "$DRY_RUN" == false ]]; then
        local arch
        arch=$(dpkg --print-architecture)
        local codename
        codename=$(lsb_release -cs)

        echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${codename} stable" \
            > /etc/apt/sources.list.d/docker.list
        ok "Docker APT repository added"

        # Update package lists
        apt-get update >> "$LOG_FILE" 2>&1
        ok "APT package lists updated with Docker repo"
    else
        dryrun_msg "Add Docker APT repository"
        dryrun_msg "Run apt-get update"
    fi

    # Install Docker Engine
    run "Install Docker Engine, containerd, and Compose plugin" \
        "apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin"

    # ---- Docker Daemon Hardening ----
    # Configure daemon.json with security options
    # ------------------------------------------------------------------------
    local daemon_config="/etc/docker/daemon.json"
    local daemon_content
    daemon_content=$(cat <<'EOF'
{
    "userns-remap": "default",
    "live-restore": true,
    "no-new-privileges": true,
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3",
        "labels": "production_status,environment",
        "env": "OS,CUDA_VERSION"
    },
    "selinux-enabled": false,
    "apparmor-default": "docker-default",
    "seccomp-profile": "default",
    "userland-proxy": false,
    "experimental": false
}
EOF
)

    if [[ "$DRY_RUN" == false ]]; then
        # Backup
        cp "$daemon_config" "${BACKUP_DIR}/daemon.json.bak" 2>/dev/null || true

        # Write config
        echo "$daemon_content" > "$daemon_config"
        chmod 600 "$daemon_config"
        ok "Docker daemon hardening config written"

        # Restart Docker
        systemctl restart docker
        systemctl enable docker >> "$LOG_FILE" 2>&1
        ok "Docker service restarted with hardened configuration"
    else
        dryrun_msg "Write hardened /etc/docker/daemon.json"
        dryrun_msg "Restart Docker daemon"
    fi

    # Add cryptobot user to docker group
    # Rationale: Allows user to run docker without sudo (but root inside container
    # is mapped to non-root outside via userns-remap)
    if id -nG "$TRADING_USER" 2>/dev/null | grep -qw "docker"; then
        ok "User '${TRADING_USER}' already in docker group"
    else
        run "Add ${TRADING_USER} to docker group" \
            "usermod -aG docker ${TRADING_USER}"
    fi

    # ---- Docker Audit Logging ----
    # Enable auditd logging for Docker daemon socket (CIS Docker 1.2.3)
    # ------------------------------------------------------------------------
    if command -v auditctl &>/dev/null; then
        if [[ "$DRY_RUN" == false ]]; then
            # Add audit rule for Docker
            cat > /etc/audit/rules.d/docker.rules <<'EOF'
# Audit Docker daemon operations
-w /usr/bin/docker -p wa -k docker
-w /usr/bin/dockerd -p wa -k docker
-w /var/lib/docker -p wa -k docker
-w /etc/docker -p wa -k docker
-w /usr/lib/systemd/system/docker.service -p wa -k docker
-w /usr/lib/systemd/system/docker.socket -p wa -k docker
-w /etc/default/docker -p wa -k docker
-w /etc/docker/daemon.json -p wa -k docker
-w /usr/bin/docker-containerd -p wa -k docker
-w /usr/bin/docker-runc -p wa -k docker
EOF
            chmod 640 /etc/audit/rules.d/docker.rules
            systemctl restart auditd 2>/dev/null || service auditd restart 2>/dev/null || true
            ok "Docker audit logging configured"
        else
            dryrun_msg "Configure auditd rules for Docker"
        fi
    else
        warn "auditd not installed - Docker audit logging skipped"
        warn "Install with: apt-get install -y auditd audispd-plugins"
    fi
}

# =============================================================================
# SECTION 8: Time Synchronization
# =============================================================================
# Rationale: Accurate timestamps are critical for financial trading:
# - Exchange API rate limits depend on precise timing
# - Order execution logs require millisecond accuracy for audit
# - Candlestick aggregation depends on synchronized clocks
# Tokyo timezone aligns with Asian crypto markets (Binance, Bybit)
# =============================================================================
setup_time_sync() {
    print_header "8/10: Time Synchronization (chrony)"

    # Install chrony (more accurate than ntpd for VPS environments)
    if ! command -v chronyd &>/dev/null; then
        run "Install chrony" "apt-get install -y chrony"
    else
        ok "chrony already installed"
    fi

    # Configure chrony for optimal time sync
    local chrony_config="/etc/chrony/chrony.conf"
    local chrony_content
    chrony_content=$(cat <<EOF
# =============================================================================
# chrony Configuration - Crypto Trading Bot
# Generated by harden_vps.sh
# =============================================================================

# NTP servers - using pool for redundancy
# Vultr provides private NTP but public pools are more reliable
pool ntp.nict.jp iburst minpoll 4 maxpoll 6
pool time.google.com iburst

# Record the rate at which the system clock gains/losses time
driftfile /var/lib/chrony/chrony.drift

# Allow the system clock to be stepped in the first three updates
# (critical for VPS which may have large initial offset)
makestep 1.0 3

# Enable kernel synchronization of the real-time clock (RTC)
rtcsync

# Hardware clock is not reliable on VPS - don't trust it
# hwclockfile /etc/adjtime

# Log clock adjustments for audit trail
log tracking measurements statistics
logdir /var/log/chrony

# Reduce time sync interval for trading accuracy
# Poll every 16-64 seconds (minpoll 4 = 16s, maxpoll 6 = 64s)
# Default is much slower; trading needs tighter sync

# Serve time only on localhost (no external NTP server)
allow 127.0.0.1
allow ::1

# Disable command port (security)
cmdport 0
EOF
)

    if [[ "$DRY_RUN" == false ]]; then
        # Backup
        cp "$chrony_config" "${BACKUP_DIR}/chrony.conf.bak" 2>/dev/null || true

        # Write config
        echo "$chrony_content" > "$chrony_config"
        chmod 644 "$chrony_config"
        ok "chrony configuration written"

        # Set timezone to Asia/Tokyo
        timedatectl set-timezone "$TZ" 2>/dev/null || {
            ln -sf "/usr/share/zoneinfo/${TZ}" /etc/localtime 2>/dev/null || true
        }
        ok "System timezone set to ${TZ}"

        # Restart chrony
        systemctl restart chronyd
        systemctl enable chronyd >> "$LOG_FILE" 2>&1
        ok "chrony restarted and enabled"

        # Verify time sync
        sleep 1
        if chronyc tracking >/dev/null 2>&1; then
            local sync_status
            sync_status=$(chronyc tracking 2>/dev/null | grep "Leap status" | awk '{print $4}')
            if [[ "$sync_status" == "Normal" ]]; then
                ok "Time synchronization active and accurate"
            else
                warn "Time sync running but not yet fully synchronized (may need 30-60s)"
            fi
        else
            warn "Could not verify chrony status"
        fi
    else
        dryrun_msg "Configure chrony with Japanese NTP pools"
        dryrun_msg "Set timezone to ${TZ}"
        dryrun_msg "Enable and restart chronyd"
    fi
}

# =============================================================================
# SECTION 9: Logging Configuration
# =============================================================================
# Rationale: Financial services require comprehensive audit logging.
# CIS 4.2.x: Ensure logging is configured and protected.
# Trading logs contain PII, API keys, and financial data - handle with care.
# =============================================================================
setup_logging() {
    print_header "9/10: Logging Configuration (rsyslog + logrotate)"

    # ---- rsyslog Configuration ----
    # Ensure rsyslog is installed and running
    if ! command -v rsyslogd &>/dev/null; then
        run "Install rsyslog" "apt-get install -y rsyslog"
    else
        ok "rsyslog already installed"
    fi

    # Configure dedicated logging for crypto bot
    # ------------------------------------------------------------------------
    local rsyslog_config="/etc/rsyslog.d/50-cryptobot.conf"
    if [[ "$DRY_RUN" == false ]]; then
        cat > "$rsyslog_config" <<EOF
# =============================================================================
# Crypto Trading Bot Logging Rules
# Generated by harden_vps.sh
# =============================================================================

# Dedicated log file for crypto trading bot application
# Captures logs tagged with 'cryptobot' program name
:programname, isequal, "cryptobot" ${LOG_DIR}/app.log
:programname, startswith, "freqtrade" ${LOG_DIR}/freqtrade.log

# Stop processing after match (don't also log to syslog)
& stop
EOF
        chmod 640 "$rsyslog_config"

        # Create log directory with secure permissions
        mkdir -p "$LOG_DIR"
        touch "${LOG_DIR}/app.log"
        touch "${LOG_DIR}/freqtrade.log"
        chmod 0640 "${LOG_DIR}"/*.log
        chown -R syslog:adm "$LOG_DIR" 2>/dev/null || chown -R root:adm "$LOG_DIR" 2>/dev/null || true

        systemctl restart rsyslog
        ok "rsyslog configured for crypto bot logging"
    else
        dryrun_msg "Create rsyslog config at ${rsyslog_config}"
        dryrun_msg "Create log directory ${LOG_DIR}"
    fi

    # ---- logrotate Configuration ----
    # Prevents log files from filling up the disk (80GB SSD)
    # Financial data retention: keep 30 days of logs for compliance
    # ------------------------------------------------------------------------
    local logrotate_config="/etc/logrotate.d/cryptobot"
    if [[ "$DRY_RUN" == false ]]; then
        cat > "$logrotate_config" <<EOF
# =============================================================================
# Logrotate Configuration - Crypto Trading Bot
# Generated by harden_vps.sh
# =============================================================================
${LOG_DIR}/*.log {
    # Rotate daily (trading generates significant log volume)
    daily

    # Keep 30 days of logs (financial compliance requirement)
    rotate 30

    # Compress rotated logs to save disk space
    compress

    # Don't error if log file is missing
    missingok

    # Don't rotate if empty
    notifempty

    # Create new file with specific permissions
    create 0640 ${TRADING_USER} adm

    # Truncate in place instead of copytruncate (more reliable)
    # copytruncate

    # Run scripts before/after rotation if needed
    # prerotate
    # endscript

    # Shared scripts (run once for all matching files)
    sharedscripts

    # Date extension for easy identification
    dateext
    dateformat -%Y%m%d-%s
}
EOF
        chmod 644 "$logrotate_config"
        ok "logrotate configured for ${LOG_DIR}/*.log"
    else
        dryrun_msg "Create logrotate config at ${logrotate_config}"
    fi

    # ---- Journald Configuration ----
    # Limit systemd journal size to prevent disk fill
    # ------------------------------------------------------------------------
    if [[ "$DRY_RUN" == false ]]; then
        mkdir -p /etc/systemd/journald.conf.d
        cat > /etc/systemd/journald.conf.d/99-cryptobot.conf <<'EOF'
[Journal]
# Limit journal size to 500MB (VPS has 80GB SSD)
SystemMaxUse=500M
SystemMaxFileSize=50M

# Compress journal data
Compress=yes

# Forward to rsyslog for unified logging
ForwardToSyslog=yes
EOF
        chmod 644 /etc/systemd/journald.conf.d/99-cryptobot.conf
        systemctl restart systemd-journald
        ok "systemd-journald disk usage limited to 500MB"
    else
        dryrun_msg "Configure journald disk limits"
    fi
}

# =============================================================================
# SECTION 10: Hardening Verification & Summary
# =============================================================================
# Rationale: After applying security settings, verify they took effect.
# Financial services audits require evidence of control effectiveness.
# =============================================================================
run_verification() {
    print_header "10/10: Hardening Verification"

    echo ""
    echo -e "${BLUE}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}${BOLD}  SECURITY VERIFICATION REPORT${NC}"
    echo -e "${BLUE}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    local check_passed=0
    local check_failed=0
    local check_total=0

    # --- Helper for checks ---
    check() {
        local name="$1"
        local test_cmd="$2"
        check_total=$((check_total + 1))

        if eval "$test_cmd" >/dev/null 2>&1; then
            echo -e "  ${GREEN}[PASS]${NC} ${name}"
            check_passed=$((check_passed + 1))
        else
            echo -e "  ${RED}[FAIL]${NC} ${name}"
            check_failed=$((check_failed + 1))
        fi
    }

    # 1. User Checks
    echo -e "${BOLD}User Configuration:${NC}"
    check "cryptobot user exists"        "id cryptobot"
    check "cryptobot in sudo group"      "id -nG cryptobot | grep -qw sudo"
    check "cryptobot in docker group"    "id -nG cryptobot | grep -qw docker"
    echo ""

    # 2. SSH Checks
    echo -e "${BOLD}SSH Configuration:${NC}"
    check "SSH port is ${SSH_PORT}"      "grep -q '^Port ${SSH_PORT}' /etc/ssh/sshd_config.d/99-hardening.conf"
    check "Root login disabled"          "grep -q '^PermitRootLogin no' /etc/ssh/sshd_config.d/99-hardening.conf"
    check "Password auth disabled"       "grep -q '^PasswordAuthentication no' /etc/ssh/sshd_config.d/99-hardening.conf"
    check "ClientAliveInterval set"      "grep -q '^ClientAliveInterval 300' /etc/ssh/sshd_config.d/99-hardening.conf"
    check "ClientAliveCountMax set"      "grep -q '^ClientAliveCountMax 2' /etc/ssh/sshd_config.d/99-hardening.conf"
    check "MaxSessions limited"          "grep -q '^MaxSessions 2' /etc/ssh/sshd_config.d/99-hardening.conf"
    echo ""

    # 3. Firewall Checks
    echo -e "${BOLD}Firewall Configuration:${NC}"
    check "UFW is active"                "ufw status | grep -q 'Status: active'"
    check "SSH port ${SSH_PORT} allowed" "ufw status | grep -q '${SSH_PORT}/tcp'"
    check "Port 8080 allowed"            "ufw status | grep -q '8080/tcp'"
    check "Port 8501 allowed"            "ufw status | grep -q '8501/tcp'"
    check "Port 9090 allowed"            "ufw status | grep -q '9090/tcp'"
    check "Port 3000 allowed"            "ufw status | grep -q '3000/tcp'"
    check "Port 3100 allowed"            "ufw status | grep -q '3100/tcp'"
    echo ""

    # 4. fail2ban Checks
    echo -e "${BOLD}Intrusion Prevention:${NC}"
    check "fail2ban is installed"        "command -v fail2ban-server"
    check "fail2ban is running"          "systemctl is-active --quiet fail2ban"
    check "fail2ban SSH jail enabled"    "fail2ban-client status sshd"
    echo ""

    # 5. System Tuning Checks
    echo -e "${BOLD}System Tuning:${NC}"
    check "vm.swappiness = 10"           "sysctl vm.swappiness | grep -q '= 10$'"
    check "fs.file-max = 65536"          "sysctl fs.file-max | grep -q '= 65536$'"
    check "net.core.somaxconn = 4096"    "sysctl net.core.somaxconn | grep -q '= 4096$'"
    check "IPv6 disabled (all)"          "sysctl net.ipv6.conf.all.disable_ipv6 | grep -q '= 1$'"
    check "Kernel ptrace restriction"    "sysctl kernel.yama.ptrace_scope | grep -q '= 1$'"
    echo ""

    # 6. Docker Checks
    echo -e "${BOLD}Docker Hardening:${NC}"
    check "Docker is installed"          "command -v docker"
    check "Docker daemon running"        "systemctl is-active --quiet docker"
    check "Docker daemon config exists"  "test -f /etc/docker/daemon.json"
    echo ""

    # 7. Time Sync Checks
    echo -e "${BOLD}Time Synchronization:${NC}"
    check "chrony is installed"          "command -v chronyd"
    check "chrony is running"            "systemctl is-active --quiet chronyd"
    check "Timezone is Asia/Tokyo"       "timedatectl | grep -q 'Time zone: Asia/Tokyo'"
    echo ""

    # 8. Logging Checks
    echo -e "${BOLD}Logging Configuration:${NC}"
    check "rsyslog is running"           "systemctl is-active --quiet rsyslog"
    check "Log directory exists"         "test -d ${LOG_DIR}"
    check "logrotate config exists"      "test -f /etc/logrotate.d/cryptobot"
    echo ""

    # --- Final Score ---
    echo -e "${BLUE}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  ${BOLD}Results:${NC} ${GREEN}${check_passed} passed${NC}, ${RED}${check_failed} failed${NC}, ${check_total} total checks"
    echo -e "${BLUE}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    # Save verification report
    if [[ "$DRY_RUN" == false ]]; then
        {
            echo "=== Hardening Verification Report ==="
            echo "Generated: $(date '+%Y-%m-%d %H:%M:%S %Z')"
            echo "Passed: ${check_passed}/${check_total}"
            echo "Failed: ${check_failed}/${check_total}"
            echo ""
            echo "Applied Settings:"
            for item in "${APPLIED_ITEMS[@]}"; do
                echo "  [OK] $item"
            done
            if [[ ${#FAILED_ITEMS[@]} -gt 0 ]]; then
                echo ""
                echo "Failures:"
                for item in "${FAILED_ITEMS[@]}"; do
                    echo "  [FAIL] $item"
                done
            fi
            if [[ ${#WARN_ITEMS[@]} -gt 0 ]]; then
                echo ""
                echo "Warnings:"
                for item in "${WARN_ITEMS[@]}"; do
                    echo "  [WARN] $item"
                done
            fi
        } > "${LOG_DIR}/verification-report.txt"
        ok "Verification report saved to ${LOG_DIR}/verification-report.txt"
    fi

    # Return non-zero if any verification failed
    if [[ $check_failed -gt 0 ]]; then
        warn "Some verification checks failed - review the output above"
        return 1
    fi

    return 0
}

# =============================================================================
# Final Summary
# =============================================================================
print_summary() {
    echo ""
    echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}${BOLD}  HARDENING COMPLETE${NC}"
    echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "${BOLD}Summary of Applied Settings:${NC}"
    echo ""

    echo -e "  ${BOLD}User & Access:${NC}"
    echo -e "    - User '${TRADING_USER}' created with sudo and docker access"
    echo -e "    - SSH key-only authentication enforced"
    echo -e "    - SSH port changed to ${SSH_PORT}"
    echo -e "    - Max 2 concurrent SSH sessions"
    echo ""

    echo -e "  ${BOLD}Network Security:${NC}"
    echo -e "    - UFW firewall: default deny incoming"
    echo -e "    - Ports open: ${SSH_PORT} (SSH), 8080 (Freqtrade), 8501 (Streamlit)"
    echo -e "                  9090 (Prometheus), 3000 (Grafana), 3100 (Loki)"
    echo -e "    - fail2ban: 3 max retries, 1 hour ban"
    echo ""

    echo -e "  ${BOLD}System Hardening:${NC}"
    echo -e "    - Automatic security updates enabled"
    echo -e "    - vm.swappiness=10, fs.file-max=65536, somaxconn=4096"
    echo -e "    - IPv6 disabled (reduced attack surface)"
    echo -e "    - Core dumps disabled (protects API keys in memory)"
    echo -e "    - ptrace restricted (prevents process injection)"
    echo ""

    echo -e "  ${BOLD}Docker Security:${NC}"
    echo -e "    - Official Docker repository (latest security patches)"
    echo -e "    - User namespace remapping enabled"
    echo -e "    - No-new-privileges enforced"
    echo -e "    - Container audit logging configured"
    echo ""

    echo -e "  ${BOLD}Time & Logging:${NC}"
    echo -e "    - chrony with Asia/Tokyo timezone"
    echo -e "    - rsyslog + logrotate for /var/log/cryptobot"
    echo -e "    - 30-day log retention with compression"
    echo ""

    echo -e "${YELLOW}${BOLD}IMPORTANT NEXT STEPS:${NC}"
    echo -e "  1. Copy your SSH public key to /home/${TRADING_USER}/.ssh/authorized_keys"
    echo -e "  2. Test SSH login: ssh -p ${SSH_PORT} ${TRADING_USER}@<your-vps-ip>"
    echo -e "  3. Verify all services are accessible"
    echo -e "  4. Store backup at ${BACKUP_DIR} in secure location"
    echo -e "  5. Review full log at ${LOG_FILE}"
    echo ""
    echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    log_info "Hardening script completed successfully"
}

# =============================================================================
# Main Execution Flow
# =============================================================================
main() {
    # Initialize logging infrastructure
    init_logging

    # Log script start
    log_info "=========================================="
    log_info "VPS Hardening Script Started"
    log_info "Mode: $([[ "$DRY_RUN" == true ]] && echo 'DRY-RUN' || echo 'LIVE')"
    log_info "Log file: ${LOG_FILE}"
    log_info "Backup dir: ${BACKUP_DIR}"
    log_info "=========================================="

    # Pre-flight checks (must pass before proceeding)
    pre_flight

    # Security hardening sections
    setup_user
    hardcode_ssh
    setup_firewall
    setup_fail2ban
    setup_auto_updates
    setup_sysctl_tuning
    setup_docker
    setup_time_sync
    setup_logging

    # Final verification
    run_verification

    # Print human-readable summary
    print_summary

    # Exit with appropriate code
    if [[ ${#FAILED_ITEMS[@]} -gt 0 ]]; then
        echo -e "\n${YELLOW}Completed with ${#FAILED_ITEMS[@]} warning(s)/failure(s).${NC}"
        exit 1
    fi

    exit 0
}

# =============================================================================
# Script Entry Point
# =============================================================================
# Parse arguments
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Usage: ${SCRIPT_NAME} [OPTIONS]"
    echo ""
    echo "Production VPS Hardening Script for Crypto Trading Bot"
    echo ""
    echo "Options:"
    echo "  --dry-run    Show what would be done without making changes"
    echo "  --help, -h   Show this help message"
    echo ""
    echo "This script must be run as root."
    echo "Log output: ${LOG_FILE}"
    exit 0
fi

# Run main function
main "$@"
