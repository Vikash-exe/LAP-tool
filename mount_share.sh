#!/usr/bin/env bash
# Makes the parcel-data share mount automatically at every boot, and checks that it mounted the RIGHT folder.
# Needs root, once:   sudo /path/to/LAP/mount_share.sh
#
# What it does:
#   1. installs cifs-utils if it is missing,
#   2. puts one line in /etc/fstab (after backing the file up) that mounts
#        \\145.145.53.17\Survey_data\Cadastral_Map_India\cadastral_data
#      read-only at LAP/data/mount, with `nofail` so a missing file server can never stop the machine
#      booting. The sub-folder is part of the share address itself (not the prefixpath option, which
#      was dropped on this machine and mounted the whole company share instead),
#   3. applies it (unmounts the current mount, mounts from fstab) and then checks the result: the mounted
#      source must be that sub-folder and its contents must look like the parcel data. If not, it says so and fails.
# The share login comes from ~/.smbcreds of the user who ran sudo (mode 600; lines username=... and
# password=...). Safe to run again: a previous entry for the same mount point is replaced.
set -euo pipefail

SHARE_PATH="//145.145.53.17/Survey_data/Cadastral_Map_India/cadastral_data"

[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo:  sudo $0"; exit 1; }
USER_NAME="${SUDO_USER:?run it with sudo from your own account, not as a root login}"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
MOUNTPOINT="$(cd "$(dirname "$0")" && pwd)/data/mount"
CREDS="$HOME_DIR/.smbcreds"
UID_N="$(id -u "$USER_NAME")"
GID_N="$(id -g "$USER_NAME")"

[ -f "$CREDS" ] || { echo "Missing $CREDS: create it first (username=... and password=... lines, chmod 600)."; exit 1; }
[ -d "$MOUNTPOINT" ] || mkdir -p "$MOUNTPOINT"

command -v mount.cifs >/dev/null 2>&1 || apt-get install -y cifs-utils

LINE="$SHARE_PATH $MOUNTPOINT cifs credentials=$CREDS,ro,vers=3.0,uid=$UID_N,gid=$GID_N,_netdev,nofail 0 0"

# Replace any earlier entry for this mount point (e.g. the prefixpath version), keeping a backup.
if grep -qF " $MOUNTPOINT cifs " /etc/fstab; then
  if grep -qxF "$LINE" /etc/fstab; then
    echo "/etc/fstab already has the right entry (left as it is)."
  else
    BACKUP="/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
    cp /etc/fstab "$BACKUP"
    grep -vF " $MOUNTPOINT cifs " /etc/fstab > /etc/fstab.new
    echo "$LINE" >> /etc/fstab.new
    cat /etc/fstab.new > /etc/fstab
    rm -f /etc/fstab.new
    echo "Replaced the earlier /etc/fstab entry (backup: $BACKUP):"
    echo "  $LINE"
  fi
else
  BACKUP="/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
  cp /etc/fstab "$BACKUP"
  echo "$LINE" >> /etc/fstab
  echo "Added to /etc/fstab (backup: $BACKUP):"
  echo "  $LINE"
fi

systemctl daemon-reload
if mountpoint -q "$MOUNTPOINT"; then umount "$MOUNTPOINT"; fi
mount "$MOUNTPOINT"   # uses the fstab entry, so success proves the entry works

SRC="$(findmnt -n -o SOURCE "$MOUNTPOINT")"
echo
echo "Mounted from /etc/fstab:"
findmnt "$MOUNTPOINT" -o TARGET,SOURCE,FSTYPE | cut -c1-200

# The check that matters: did it mount the right folder, not the whole share?
case "$SRC" in
  *Cadastral_Map_India/cadastral_data) ;;
  *) echo; echo "PROBLEM: mounted source is '$SRC', not the cadastral_data folder. Unmounting it again."; umount "$MOUNTPOINT"; exit 2 ;;
esac
if [ ! -e "$MOUNTPOINT/India_State_Boundary.zip" ] || [ ! -d "$MOUNTPOINT/Village_boundaries" ]; then
  echo; echo "PROBLEM: the mounted folder does not contain India_State_Boundary.zip and Village_boundaries/. Unmounting it again."
  umount "$MOUNTPOINT"; exit 3
fi

echo
echo "Contents (first entries of $(ls "$MOUNTPOINT" | wc -l)):"
ls "$MOUNTPOINT" | head -5
echo
echo "FSTAB-OK: the right folder is mounted, and it will be mounted again after a reboot."
