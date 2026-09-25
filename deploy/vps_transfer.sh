#!/bin/bash
# STEP 2 - run this ON YOUR MAC, in Terminal. Not through Claude: the bridge
# has no route to the droplet.
#
#   bash ~/Downloads/vps_transfer.sh 143.198.124.232
#
# Copies the bot to the droplet. What is deliberately left behind:
#
#   .venv/                    54M of macOS binaries. Cannot run on Linux and
#                             must be rebuilt there; copying it would produce
#                             a venv that fails in a confusing way.
#   _retired-2026-09-17/      31M of retired engine code.
#   kalshi-bot-backup-*/      175M of old full copies.
#   _to_delete/, _removed-*/  already-quarantined installers.
#   __pycache__/, .DS_Store   noise.
#
# What deliberately DOES go: data/journal.db. That file is the record - 106
# distinct contracts and every settled forecast. The whole point of moving to
# a server is to continue it, not restart it.
#
# It also carries .env and kalshi_demo_key.pem over SSH, which is encrypted in
# transit. Permissions are re-asserted on the far side by vps_setup.sh.
set -euo pipefail

HOST="${1:?usage: vps_transfer.sh <droplet-ip> [user]}"
USER_="${2:-kalshi}"
SRC="$HOME/trading/kalshi-bot/"
DEST="$USER_@$HOST:~/kalshi-bot/"

[ -d "$SRC" ] || { echo "no $SRC on this Mac"; exit 1; }

# Pre-flight. rsync's failure for this is eleven lines of io_read_nonblocking
# that say nothing about the actual cause, which is almost always that the
# bot user has no authorized_keys yet.
echo "==> Checking SSH to $USER_@$HOST"
if ! ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
        "$USER_@$HOST" true 2>/dev/null; then
    echo
    echo "    Cannot log in as '$USER_' with a key."
    echo
    echo "    That user is created by deploy/vps_bootstrap.sh, which also copies"
    echo "    root's authorized_keys across to it. If you have not run the"
    echo "    bootstrap on the droplet yet, this is expected."
    echo
    if ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$HOST" true 2>/dev/null; then
        echo "    root DOES accept your key. On the droplet, run:"
        echo "        scp ~/trading/kalshi-bot/deploy/vps_bootstrap.sh root@$HOST:~/"
        echo "        ssh root@$HOST 'bash vps_bootstrap.sh'"
    else
        echo "    root does not accept your key either, so there is no key on"
        echo "    the droplet at all. On this Mac:"
        echo "        ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519 -C kalshibot"
        echo "        ssh-copy-id root@$HOST        # uses the root password"
        echo "    then copy and run vps_bootstrap.sh as above."
        echo
        echo "    ssh-copy-id $USER_@$HOST will NOT work as a shortcut: the"
        echo "    bootstrap creates that user with --disabled-password, so it"
        echo "    has no password for ssh-copy-id to authenticate with."
    fi
    echo
    exit 1
fi
echo "    ok"

echo "==> Copying $SRC  ->  $DEST"
rsync -avz --human-readable --progress \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.DS_Store' \
  --exclude '_retired-2026-09-17/' \
  --exclude '_backup-*/' \
  --exclude '*.pyc' \
  "$SRC" "$DEST"

echo
echo "Copied. Now on the droplet:"
echo "    ssh $USER_@$HOST"
echo "    bash ~/kalshi-bot/deploy/vps_setup.sh"
