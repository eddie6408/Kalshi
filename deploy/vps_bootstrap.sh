#!/bin/bash
# STEP 1 - run this ON THE DROPLET, as root, before anything is copied.
#
#   ssh root@143.198.124.232
#   bash vps_bootstrap.sh
#
# Prepares a public Ubuntu 24.04 server to hold an API key and run a trading
# bot unattended. The droplet arrives with 154 pending updates, 125 of them
# security, root login enabled and no firewall. None of that is acceptable
# for a machine that will hold a Kalshi credential.
set -euo pipefail

BOT_USER=kalshi

echo "==> 1/6  Security updates (154 pending, 125 security)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

echo "==> 2/6  Packages"
apt-get install -y -qq python3-venv python3-pip rsync ufw fail2ban \
                       unattended-upgrades sqlite3 curl git

echo "==> 3/6  Unattended security upgrades"
# A server nobody logs into for a fortnight has to patch itself.
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> 4/6  Non-root user '$BOT_USER'"
if ! id "$BOT_USER" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "$BOT_USER"
fi
# Give it your SSH key so you can rsync straight to it.
install -d -m 700 -o "$BOT_USER" -g "$BOT_USER" "/home/$BOT_USER/.ssh"
if [ -f /root/.ssh/authorized_keys ]; then
    cp /root/.ssh/authorized_keys "/home/$BOT_USER/.ssh/authorized_keys"
    chown "$BOT_USER:$BOT_USER" "/home/$BOT_USER/.ssh/authorized_keys"
    chmod 600 "/home/$BOT_USER/.ssh/authorized_keys"
    echo "    copied your SSH key to $BOT_USER"
else
    cat <<'KEYNOTE'

    ############################################################
    ##  STOP - no SSH key found at /root/.ssh/authorized_keys  ##
    ############################################################

    The 'kalshi' user has been created, but it has no key, so the
    transfer from your Mac WILL fail with:

        kalshi@<ip>: Permission denied (publickey)

    You are reaching this server by password (or the web console).
    Password auth on a public IP that is about to hold an API key
    is not a position to stay in.

    Fix it from your Mac, not from here:

        ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C kalshibot
        ssh-copy-id root@<this-server-ip>

    then re-run this script. It will find the key and copy it to
    the kalshi user.

    Note: ssh-copy-id kalshi@<ip> does NOT work as a shortcut - the
    user above was created with --disabled-password, so there is no
    password for it to authenticate with.

KEYNOTE
fi

echo "==> 5/6  Firewall: SSH only"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
# Nothing else is opened. The dashboard is a local file; it is never served.

echo "==> 6/6  SSH hardening"
SSHD=/etc/ssh/sshd_config.d/99-kalshibot.conf
if [ -f /root/.ssh/authorized_keys ]; then
    cat > "$SSHD" <<'SSHEOF'
PasswordAuthentication no
PermitRootLogin prohibit-password
SSHEOF
    systemctl reload ssh 2>/dev/null || systemctl reload sshd
    echo "    password auth off, root login key-only"
else
    echo "    SKIPPED - no SSH key present, refusing to lock you out"
fi

echo
IP=$(hostname -I | awk '{print $1}')
if [ -f "/home/$BOT_USER/.ssh/authorized_keys" ]; then
    echo "Done. Next, ON YOUR MAC:"
    echo "    bash ~/trading/kalshi-bot/deploy/vps_transfer.sh $IP"
else
    echo "Done, EXCEPT the SSH key - see the note above. The transfer will"
    echo "fail until '$BOT_USER' has your key. Fix that, re-run this script,"
    echo "then transfer."
fi
