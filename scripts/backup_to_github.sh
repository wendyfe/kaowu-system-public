#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DB_PATH="${DB_PATH:-$PROJECT_ROOT/app/db/kaowu.db}"
BACKUP_REPO_DIR="${BACKUP_REPO_DIR:?Set BACKUP_REPO_DIR to a local clone of your private backup repo}"
BACKUP_SUBDIR="${BACKUP_SUBDIR:-kaowu-system}"
KEEP_BACKUPS="${KEEP_BACKUPS:-30}"
GIT_BRANCH="${GIT_BRANCH:-main}"

GPG_RECIPIENT="${GPG_RECIPIENT:-}"
GPG_PASSPHRASE_FILE="${GPG_PASSPHRASE_FILE:-}"

command -v sqlite3 >/dev/null || { echo "sqlite3 is required" >&2; exit 1; }
command -v gpg >/dev/null || { echo "gpg is required" >&2; exit 1; }
command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v gzip >/dev/null || { echo "gzip is required" >&2; exit 1; }

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database not found: $DB_PATH" >&2
  exit 1
fi

if [[ ! -d "$BACKUP_REPO_DIR/.git" ]]; then
  echo "BACKUP_REPO_DIR must be an existing git clone: $BACKUP_REPO_DIR" >&2
  exit 1
fi

if [[ -z "$GPG_RECIPIENT" && -z "$GPG_PASSPHRASE_FILE" ]]; then
  echo "Set GPG_RECIPIENT for public-key encryption, or GPG_PASSPHRASE_FILE for symmetric encryption" >&2
  exit 1
fi

if [[ -n "$GPG_PASSPHRASE_FILE" && ! -f "$GPG_PASSPHRASE_FILE" ]]; then
  echo "GPG_PASSPHRASE_FILE not found: $GPG_PASSPHRASE_FILE" >&2
  exit 1
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

backup_db="$tmpdir/kaowu-$timestamp.db"
archive="$tmpdir/kaowu-$timestamp.db.gz"
encrypted="$tmpdir/kaowu-$timestamp.db.gz.gpg"
dest_dir="$BACKUP_REPO_DIR/$BACKUP_SUBDIR"
dest_file="$dest_dir/kaowu-$timestamp.db.gz.gpg"

mkdir -p "$dest_dir"

sqlite3 "$DB_PATH" ".backup '$backup_db'"
integrity="$(sqlite3 "$backup_db" "PRAGMA integrity_check;")"
if [[ "$integrity" != "ok" ]]; then
  echo "Backup integrity check failed: $integrity" >&2
  exit 1
fi

gzip -c "$backup_db" > "$archive"

if [[ -n "$GPG_RECIPIENT" ]]; then
  gpg --batch --yes --trust-model always \
    --encrypt --recipient "$GPG_RECIPIENT" \
    --output "$encrypted" "$archive"
else
  gpg --batch --yes --pinentry-mode loopback \
    --symmetric --cipher-algo AES256 \
    --passphrase-file "$GPG_PASSPHRASE_FILE" \
    --output "$encrypted" "$archive"
fi

install -m 600 "$encrypted" "$dest_file"

if [[ "$KEEP_BACKUPS" =~ ^[0-9]+$ && "$KEEP_BACKUPS" -gt 0 ]]; then
  while IFS= read -r old_backup; do
    [[ -n "$old_backup" ]] && rm -- "$old_backup"
  done < <(
    find "$dest_dir" -maxdepth 1 -type f -name 'kaowu-*.db.gz.gpg' \
      | sort \
      | head -n "-$KEEP_BACKUPS"
  )
fi

git -C "$BACKUP_REPO_DIR" add "$BACKUP_SUBDIR"

if git -C "$BACKUP_REPO_DIR" diff --cached --quiet; then
  echo "No backup changes to commit"
  exit 0
fi

git -C "$BACKUP_REPO_DIR" commit -m "Add kaowu backup $timestamp"
git -C "$BACKUP_REPO_DIR" push origin "$GIT_BRANCH"

echo "Encrypted backup pushed: $BACKUP_SUBDIR/$(basename "$dest_file")"
