#!/usr/bin/env bash
# Makes the self-signed certificate the gateway uses for HTTPS (deploy/certs/lap.crt and lap.key, valid 10 years).
# Browsers show a one-time warning for a self-signed certificate; to avoid it, import deploy/certs/lap.crt as a
# trusted certificate on each PC (or replace the two files with a certificate from your own CA).
#
#   deploy/make_cert.sh                      names: this server's IP, its host name, localhost
#   deploy/make_cert.sh 10.1.2.3 lap.local   extra IPs / names to put in the certificate
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p certs
if [ -f certs/lap.crt ] && [ -z "${1:-}" ]; then
  echo "certs/lap.crt already exists (give extra names as arguments to make a new one, or delete it first)."
  exit 0
fi

san="DNS:localhost,IP:127.0.0.1,DNS:$(hostname)"
for ip in $(hostname -I 2>/dev/null); do san="$san,IP:$ip"; done
for extra in "$@"; do
  if [[ "$extra" =~ ^[0-9.]+$ ]]; then san="$san,IP:$extra"; else san="$san,DNS:$extra"; fi
done

openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout certs/lap.key -out certs/lap.crt \
  -subj "/CN=Cadastral Parcel Finder" -addext "subjectAltName=$san"
chmod 600 certs/lap.key
echo "Certificate made for: $san"
