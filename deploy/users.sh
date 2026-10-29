#!/usr/bin/env bash
# Who may sign in to the dashboard. The users and their password hashes (scrypt) are in deploy/auth/users.
# Changes apply at once: the app reads the file at every sign-in, and a user that is removed loses access immediately.
#
#   deploy/users.sh add <name>      adds the user, or changes the password of an existing one (asks for it twice)
#   deploy/users.sh remove <name>
#   deploy/users.sh list
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p auth
file=auth/users
touch "$file"
chmod 600 "$file"

cmd="${1:-}"
name="${2:-}"

# scrypt hash of the password read from stdin, in the format backend/app/auth.py checks
hash_password() {
  python3 -c 'import hashlib, secrets, sys
pw = sys.stdin.read().encode()
salt = secrets.token_bytes(16)
digest = hashlib.scrypt(pw, salt=salt, n=16384, r=8, p=1, dklen=32)
print("scrypt$16384$8$1$%s$%s" % (salt.hex(), digest.hex()))'
}

case "$cmd" in
  list)
    cut -d: -f1 "$file"
    ;;
  add)
    [[ "$name" =~ ^[A-Za-z0-9._@-]+$ ]] || { echo "Give a user name (letters, digits, . _ @ -)."; exit 1; }
    read -r -s -p "Password for $name: " pw1; echo
    read -r -s -p "Again: " pw2; echo
    [ "$pw1" = "$pw2" ] || { echo "The two passwords differ; nothing changed."; exit 1; }
    [ "${#pw1}" -ge 8 ] || { echo "Use at least 8 characters; nothing changed."; exit 1; }
    hash="$(printf '%s' "$pw1" | hash_password)"
    tmp="$(mktemp -p auth)"
    { grep -v -E "^${name//./\\.}:" "$file" || true; echo "$name:$hash"; } > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$file"
    echo "User '$name' saved."
    ;;
  remove)
    [ -n "$name" ] || { echo "Usage: users.sh remove <name>"; exit 1; }
    tmp="$(mktemp -p auth)"
    { grep -v -E "^${name//./\\.}:" "$file" || true; } > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$file"
    echo "User '$name' removed (if there was one)."
    ;;
  *)
    echo "Usage: deploy/users.sh add <name> | remove <name> | list"
    exit 1
    ;;
esac
