#!/usr/bin/env bash
# VPeeN release canary - post-publication watchdog (v4.9.0 "Sangar").
#
# Runs on a schedule (or on demand) and verifies the LATEST release is
# exactly what CI published:
#   1. the release is reachable and tagged vX.Y.Z
#   2. the Windows Setup installer asset exists        (the #1 promise)
#   3. SHA256SUMS.txt exists and every hash matches a fresh download
#   4. every release asset has a sums entry            (nothing unverified)
#   5. asset structure is sane (exe = MZ, zip = PK, tar.gz = gzip magic)
#   6. no newer v* tag exists without its release      (publish failures)
#
# READ-ONLY by design: it never creates, edits or deletes anything on
# GitHub - a canary cannot break the thing it watches.  Any drift fails
# the workflow loudly so the maintainer gets an email immediately.

set -euo pipefail

REPO="SirBNL/VPeeN"
API="https://api.github.com/repos/${REPO}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "::error::CANARY-FAIL: $*" >&2; exit 1; }
note() { echo "canary: $*"; }

AUTH=()
if [ -n "${GITHUB_TOKEN:-}" ]; then
  AUTH=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
fi

# ---------------------------------------------------------------- 1. latest
rel="$(curl -fsSL "${AUTH[@]}" "${API}/releases/latest" \
  || fail "cannot reach /releases/latest - is the release hidden/deleted?")"
tag="$(jq -r '.tag_name' <<<"$rel")"
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "latest release tag '${tag}' is not a stable vX.Y.Z"
note "latest release: ${tag}"

# ---------------------------------------------------------------- 2. assets
assets="$(jq -r '.assets[] | "\(.name)\t\(.browser_download_url)"' <<<"$rel")"
[ -n "$assets" ] || fail "the release carries no assets at all"

echo "$assets" | cut -f1 | sort > "$TMP/asset-names.txt"
sums_name="SHA256SUMS.txt"
grep -qx "$sums_name" "$TMP/asset-names.txt" \
  || fail "SHA256SUMS.txt is missing from the release"
echo "$assets" | grep -P '^VPeeN-Setup-[0-9.]+\.exe\t' > /dev/null \
  || fail "no VPeeN-Setup-*.exe asset - the Windows installer promise is broken"
note "setup installer asset: present"

# ---------------------------------------------------------------- 3. hashes
while IFS=$'\t' read -r name url; do
  curl -fsSL -o "$TMP/$name" "$url" || fail "asset '$name' failed to download"
done <<<"$assets"

( cd "$TMP" && sha256sum -c "$sums_name" ) > "$TMP/verify.log" 2>&1 \
  || { cat "$TMP/verify.log" >&2; fail "hash mismatch - an asset does not match SHA256SUMS.txt"; }
note "hashes: $(grep -c ': OK' "$TMP/verify.log") assets verified against SHA256SUMS.txt"

# ---------------------------------------------------------------- 4. coverage
cut -d' ' -f3- "$TMP/$sums_name" | awk '{print $1}' | sort > "$TMP/sums-names.txt"
sort "$TMP/asset-names.txt" | grep -v '^SHA256SUMS.txt$' > "$TMP/asset-names-no-sums.txt"
if ! diff -u "$TMP/asset-names-no-sums.txt" "$TMP/sums-names.txt" > "$TMP/cov.diff"; then
  cat "$TMP/cov.diff" >&2
  fail "assets and SHA256SUMS.txt entries disagree (above) - something ships unverified"
fi
note "coverage: every asset has a checksum entry"

# ---------------------------------------------------------------- 5. structure
head -c2 "$TMP"/VPeeN-Setup-*.exe | grep -q 'MZ' \
  || fail "the Setup exe does not start with the PE magic 'MZ' - corrupted upload?"
for z in "$TMP"/*.zip; do
  head -c2 "$z" | grep -q 'PK' || fail "'$(basename "$z")' is not a ZIP (no PK magic)"
done
for t in "$TMP"/*.tar.gz; do
  [ "$(head -c2 "$t" | od -An -tx1 | tr -d ' ')" = "1f8b" ] \
    || fail "'$(basename "$t")' is not gzip data"
done
note "structure: PE/ZIP/gzip magic bytes OK"

# ---------------------------------------------------------------- 6. freshness
newest_tag="$(git ls-remote --tags "https://github.com/${REPO}.git" 'refs/tags/v*' \
  | grep -v '\^{}' | cut -f2 | sed 's|refs/tags/||' | sort -V | tail -1)"
[ "$newest_tag" = "$tag" ] \
  || fail "tag ${newest_tag} exists but the latest release is still ${tag} - did publishing fail (or is the newest one a draft/prerelease)?"
note "freshness: newest tag ${newest_tag} == latest release"

echo "CANARY-OK: release ${tag} is complete, checksummed and current."
