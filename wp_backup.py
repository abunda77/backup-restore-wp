#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wp_backup.py - Backup & restore file WordPress + database.

Backup
------
- Zip seluruh isi folder WordPress (mis. /home/mywordpress/public_html)
  secara FLAT: isi folder langsung menjadi root archive, tanpa dibungkus
  folder baru.
- Ekspor database via `mysqldump` menjadi .sql.gz; kredensial dibaca
  otomatis dari wp-config.php.
- Kedua file output disimpan di root project (folder tempat skrip ini).

Restore
-------
- Validasi bahwa zip adalah backup WordPress, ekstrak flat ke folder target,
  import dump .sql.gz dengan kredensial database BARU (diinput user),
  lalu perbarui DB_* pada wp-config.php hasil ekstraksi.

Hanya memakai Python standard library. Client `mysqldump` dan `mysql`
harus tersedia di PATH (atau diberikan lewat --mysqldump / --mysql).

Contoh:
    python wp_backup.py backup --site /home/mywordpress/public_html
    python wp_backup.py backup --site /home/mywordpress/public_html \
        --name mywp --exclude "cache/*"
    python wp_backup.py restore --zip mywp_20250101_120000.zip \
        --target /home/mywordpress/public_html \
        --db-name db_baru --db-user user_baru --db-pass rahasia --create-db
"""

import argparse
import fnmatch
import getpass
import gzip
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def log(msg=""):
    print(msg, flush=True)


def die(msg, code=1):
    print("ERROR: %s" % msg, file=sys.stderr, flush=True)
    sys.exit(code)


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def split_host_port(host):
    """Split 'host:port' into (host, port); port defaults to None."""
    if ":" in host:
        h, _, p = host.rpartition(":")
        if p.isdigit():
            return h, int(p)
    return host, None


def _read_secret_line():
    """Read a secret: hidden on a real TTY, plain input when piped."""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            return getpass.getpass("")
    except Exception:
        pass
    return input("")


def prompt(text, default=None, required=False, secret=False):
    """Ask for a value; pressing Enter picks the default when available."""
    if secret:
        suffix = " [Enter = pakai nilai lama]: " if default else ": "
    elif default is not None:
        suffix = " [%s]: " % default
    else:
        suffix = ": "
    while True:
        label = text + suffix
        if secret:
            print(label, end="", flush=True)
            value = _read_secret_line()
        else:
            value = input(label)
        value = value.strip()
        if value:
            return value
        if default:
            return default
        if not required:
            return ""


def confirm(text, default_yes=False):
    hint = "[Y/n]" if default_yes else "[y/N]"
    ans = input("%s %s: " % (text, hint)).strip().lower()
    if not ans:
        return default_yes
    return ans in ("y", "ya", "yes", "1", "true")


def ask_overwrite(path):
    if not os.path.exists(path):
        return True
    return confirm("File sudah ada: %s. Timpa?" % path)


# ---------------------------------------------------------------------------
# WordPress detection & wp-config.php handling
# ---------------------------------------------------------------------------

WP_CONFIG_NAME = "wp-config.php"
REQUIRED_WP_FILES = (WP_CONFIG_NAME, "wp-settings.php")
WP_CONTENT_DIR = "wp-content"

# Value body: any escaped pair (\anychar) or any char except the current
# quote, so values containing \' or \" are handled correctly.
DEFINE_RE = re.compile(
    r"define\s*\(\s*(['\"])(DB_NAME|DB_USER|DB_PASSWORD|DB_HOST)\1\s*,\s*"
    r"(['\"])((?:\\.|(?!\3).)*)\3\s*\)\s*;",
    re.DOTALL,
)

ESCAPE_SEQ_RE = re.compile(r"\\(.)")

# Constant name -> normalized key used throughout this script.
WP_DB_KEYS = {
    "DB_NAME": "name",
    "DB_USER": "user",
    "DB_PASSWORD": "password",
    "DB_HOST": "host",
}
WP_DB_CONSTANTS = {
    "name": "DB_NAME",
    "user": "DB_USER",
    "password": "DB_PASSWORD",
    "host": "DB_HOST",
}

JUNK_BASENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
DEFAULT_EXCLUDES = ("*.zip", "*.sql.gz")


def is_wordpress_dir(path):
    """Return (is_wp, missing_items) for a candidate WordPress folder."""
    if not os.path.isdir(path):
        return False, ["folder tidak ditemukan: %s" % path]
    missing = []
    for name in REQUIRED_WP_FILES:
        if not os.path.isfile(os.path.join(path, name)):
            missing.append(name)
    if not os.path.isdir(os.path.join(path, WP_CONTENT_DIR)):
        missing.append(WP_CONTENT_DIR + "/")
    return (not missing), missing


def parse_wp_config(config_path):
    """Extract DB credentials from wp-config.php.

    Returns a dict with normalized keys: name, user, password, host.
    """
    try:
        with open(config_path, "r", encoding="utf-8",
                  errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        die("Tidak bisa membaca %s: %s" % (config_path, exc))
    cfg = {v: "" for v in WP_DB_KEYS.values()}
    for _q1, key, _q2, raw_value in DEFINE_RE.findall(content):
        # Decode PHP escape sequences: \x -> x for any escaped char.
        cfg[WP_DB_KEYS[key]] = ESCAPE_SEQ_RE.sub(r"\1", raw_value)
    return cfg


def update_wp_config(config_path, updates):
    """Rewrite DB_* define() values; keep wp-config.php.bak-<ts> first.

    `updates` uses normalized keys (name/user/password/host); returns the
    list of PHP constants that were actually rewritten.
    """
    try:
        with open(config_path, "r", encoding="utf-8",
                  errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        die("Tidak bisa membaca %s: %s" % (config_path, exc))

    changed = []
    new_content = content

    def make_repl(new_value, key):
        def _repl(match):
            quote = match.group(1)
            escaped = (new_value.replace("\\", "\\\\")
                                .replace(quote, "\\" + quote))
            return "define( %s%s%s, %s%s%s );" % (
                quote, key, quote, quote, escaped, quote)
        return _repl

    for norm_key, value in updates.items():
        const = WP_DB_CONSTANTS.get(norm_key)
        if not const:
            continue
        pattern = re.compile(
            r"define\s*\(\s*(['\"])(%s)\1\s*,\s*(['\"])((?:\\.|(?!\3).)*)"
            r"\3\s*\)\s*;" % re.escape(const),
            re.DOTALL,
        )
        new_content, n = pattern.subn(make_repl(value, const), new_content,
                                      count=1)
        if n:
            changed.append(const)

    if changed:
        bak_path = "%s.bak-%s" % (config_path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(config_path, bak_path)
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write(new_content)
    return changed


# ---------------------------------------------------------------------------
# Backup: files -> flat zip
# ---------------------------------------------------------------------------


def should_exclude(rel_path, name, extra_patterns):
    if name in JUNK_BASENAMES:
        return True
    rel_norm = rel_path.replace(os.sep, "/")
    for pat in DEFAULT_EXCLUDES + tuple(extra_patterns):
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_norm, pat):
            return True
    return False


class BackupStats(object):
    def __init__(self):
        self.files = 0
        self.raw_bytes = 0
        self.skipped = []


def backup_files(source_dir, zip_path, extra_excludes=()):
    """Zip the CONTENTS of source_dir flat (archive root == source_dir)."""
    stats = BackupStats()
    with zipfile.ZipFile(zip_path, "w", allowZip64=True) as zf:

        def add_dir_entry(arcname, mode=0o755):
            info = zipfile.ZipInfo(arcname.rstrip("/") + "/",
                                   date_time=time.localtime()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((mode | 0o40000) << 16) | 0x10
            zf.writestr(info, b"")

        # wp-config.php goes first so tools can identify the zip quickly.
        config_src = os.path.join(source_dir, WP_CONFIG_NAME)
        if os.path.isfile(config_src):
            with open(config_src, "rb") as fh:
                data = fh.read()
            info = zipfile.ZipInfo.from_file(config_src, WP_CONFIG_NAME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            stats.files += 1
            stats.raw_bytes += len(data)

        for root, dirs, files in os.walk(source_dir, topdown=True):
            dirs.sort()
            files.sort()

            rel_root = os.path.relpath(root, source_dir)
            prefix = "" if rel_root == "." else rel_root.replace(os.sep, "/")

            # Prune excluded directories so we never descend into them.
            for d in list(dirs):
                arc = prefix + "/" + d if prefix else d
                if should_exclude(arc, d, extra_excludes):
                    dirs.remove(d)
                    stats.skipped.append(arc + "/")

            for d in dirs:
                add_dir_entry(prefix + "/" + d if prefix else d)

            for fname in files:
                if fname == WP_CONFIG_NAME and root == source_dir:
                    continue  # already written above
                full = os.path.join(root, fname)
                arc = prefix + "/" + fname if prefix else fname
                if should_exclude(arc, fname, extra_excludes):
                    stats.skipped.append(arc)
                    continue

                if os.path.islink(full):
                    target = os.readlink(full)
                    info = zipfile.ZipInfo.from_file(full, arc)
                    info.create_system = 3  # Unix
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (0o120777 << 16) | 0xA
                    zf.writestr(info, target.encode("utf-8"))
                    stats.raw_bytes += len(target.encode("utf-8"))
                else:
                    try:
                        zf.write(full, arc)
                        stats.raw_bytes += os.lstat(full).st_size
                    except OSError as exc:
                        stats.skipped.append("%s (%s)" % (arc, exc))
                        continue
                stats.files += 1

    return stats


def backup_database(cfg, out_path, mysqldump_bin):
    """Stream mysqldump output through gzip into out_path (.sql.gz)."""
    for key in ("name", "user"):
        if not cfg.get(key):
            die("Kredensial %s tidak ditemukan di wp-config.php."
                % ("DB_NAME" if key == "name" else "DB_USER"))
    host, port = split_host_port(cfg.get("host") or "localhost")

    cmd = [
        mysqldump_bin,
        "--single-transaction",
        "--quick",
        "--routines",
        "--triggers",
        "--events",
        "--no-tablespaces",
        "--default-character-set=utf8mb4",
        "--host=%s" % host,
    ]
    if port:
        cmd.append("--port=%d" % port)
    cmd += ["--user=%s" % cfg["user"], cfg["name"]]

    env = dict(os.environ)
    env["MYSQL_PWD"] = cfg.get("password", "")

    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)

    # Drain stderr into a temp file so a chatty mysqldump cannot deadlock
    # on a full pipe while we are busy streaming stdout to gzip.
    with tempfile.TemporaryFile() as err_fh:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=err_fh, env=env, shell=False)
        except FileNotFoundError:
            die("Program '%s' tidak ditemukan. Instal MySQL client atau "
                "set --mysqldump /path/mysqldump." % mysqldump_bin)

        total = 0
        try:
            with gzip.open(out_path, "wb", compresslevel=6) as gz:
                while True:
                    chunk = proc.stdout.read(1024 * 256)
                    if not chunk:
                        break
                    gz.write(chunk)
                    total += len(chunk)
            rc = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            if proc.stdout:
                proc.stdout.close()
        err_fh.seek(0)
        err = err_fh.read().decode("utf-8", errors="replace").strip()
    return rc, total, err


# ---------------------------------------------------------------------------
# Restore: zip validation & extraction
# ---------------------------------------------------------------------------


def common_prefix_of(names):
    """Shared 'folder/' prefix when every entry lives inside one folder."""
    parts = [n.split("/", 1)[0] + "/" for n in names if n]
    # A single entry proves nothing; require several before stripping.
    if len(parts) < 2:
        return ""
    candidate = parts[0]
    if not candidate.endswith("/"):
        return ""
    if all(p == candidate for p in parts):
        return candidate
    return ""


def strip_prefix(name, prefix):
    return name[len(prefix):] if prefix and name.startswith(prefix) else name


def zip_wordpress_report(zf):
    """Inspect zip entries; return (ok, missing, prefix, top_level_names)."""
    names = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            continue  # unsafe entries are ignored during validation
        if name.endswith("/"):
            continue
        names.append(name)

    prefix = common_prefix_of(names)

    def has(after_prefix):
        return any(strip_prefix(n, prefix) == after_prefix for n in names)

    def has_dir(dirname):
        marker = dirname + "/"
        return any(strip_prefix(n, prefix).startswith(marker) for n in names)

    missing = []
    for name in REQUIRED_WP_FILES:
        if not has(name):
            missing.append(name)
    if not has_dir(WP_CONTENT_DIR):
        missing.append(WP_CONTENT_DIR + "/")

    top = sorted({strip_prefix(n, prefix).split("/", 1)[0]
                  for n in names})
    return (not missing), missing, prefix, top


def safe_extract(zf, target_dir, prefix=""):
    """Zip-slip-safe flat extraction; returns count of extracted entries."""
    count = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.endswith("/"):
            continue
        stripped = strip_prefix(name, prefix)
        if not stripped or stripped.endswith("/"):
            continue
        if stripped.startswith("/") or ".." in stripped.split("/"):
            raise RuntimeError("Entri tidak aman di dalam zip: %s"
                               % info.filename)

        dest = os.path.join(target_dir, *stripped.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        attrs = info.external_attr >> 16
        is_symlink = ((attrs >> 12) & 0o17) == 0o12
        if is_symlink:
            link_target = zf.read(info).decode("utf-8", errors="replace")
            if os.path.lexists(dest):
                os.remove(dest)
            try:
                os.symlink(link_target, dest)
            except (OSError, NotImplementedError):
                with open(dest, "wb") as fh:  # fallback: store as file
                    fh.write(link_target.encode("utf-8"))
        else:
            with zf.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 256)
            if attrs and os.name == "posix":
                os.chmod(dest, attrs & 0o7777)
        count += 1
    return count


# ---------------------------------------------------------------------------
# Restore: database import
# ---------------------------------------------------------------------------


def find_sql_gz(zip_path, explicit=None):
    """Pick the dump to import: explicit flag > sibling of zip > newest."""
    if explicit:
        if not os.path.isfile(explicit):
            die("File SQL tidak ditemukan: %s" % explicit)
        return explicit
    zip_dir = os.path.dirname(os.path.abspath(zip_path)) or "."
    guess = os.path.splitext(os.path.abspath(zip_path))[0] + ".sql.gz"
    if os.path.isfile(guess):
        return guess
    candidates = [
        os.path.join(zip_dir, f) for f in os.listdir(zip_dir)
        if f.endswith(".sql.gz")
    ]
    if not candidates:
        die("File .sql.gz tidak ditemukan. Letakkan di sebelah file zip "
            "atau gunakan --sql-gz <path>.")
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def build_mysql_cmd(mysql_bin, db_name, db_user, db_host):
    """Build the mysql client command; db_name may be '' for CREATE."""
    host, port = split_host_port(db_host or "localhost")
    cmd = [mysql_bin, "--host=%s" % host, "--default-character-set=utf8mb4"]
    if port:
        cmd.append("--port=%d" % port)
    if db_name:
        cmd.append(db_name)
    return cmd


def create_database(admin_user, admin_password, admin_host, new_db,
                    mysql_bin):
    """CREATE DATABASE IF NOT EXISTS using the new credentials."""
    cmd = build_mysql_cmd(mysql_bin, "", admin_user, admin_host)
    sql = ("CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 "
           "COLLATE utf8mb4_unicode_ci;" % new_db.replace("`", ""))
    env = dict(os.environ)
    env["MYSQL_PWD"] = admin_password or ""
    with tempfile.TemporaryFile() as err_fh:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=err_fh, env=env)
        except FileNotFoundError:
            die("Program '%s' tidak ditemukan. Instal MySQL client atau "
                "set --mysql /path/mysql." % mysql_bin)
        try:
            proc.communicate(input=sql.encode("utf-8"))
            rc = proc.returncode
        finally:
            for s in (proc.stdout, proc.stdin):
                if s and not s.closed:
                    s.close()
        if rc != 0:
            err_fh.seek(0)
            err = err_fh.read().decode("utf-8", errors="replace").strip()
            die("Gagal membuat database '%s':\n%s" % (new_db, err))


def import_database(sql_gz_path, db_name, db_user, db_password, db_host,
                    mysql_bin):
    """Decompress .sql.gz on the fly and pipe it into the mysql client."""
    cmd = build_mysql_cmd(mysql_bin, db_name, db_user, db_host)
    env = dict(os.environ)
    env["MYSQL_PWD"] = db_password or ""

    with tempfile.TemporaryFile() as err_fh:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=err_fh, env=env)
        except FileNotFoundError:
            die("Program '%s' tidak ditemukan. Instal MySQL client atau "
                "set --mysql /path/mysql." % mysql_bin)

        bytes_in = 0
        try:
            with gzip.open(sql_gz_path, "rb") as gz:
                while True:
                    chunk = gz.read(1024 * 256)
                    if not chunk:
                        break
                    try:
                        proc.stdin.write(chunk)
                    except BrokenPipeError:
                        break  # real exit code collected below
                    bytes_in += len(chunk)
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
            rc = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        err_fh.seek(0)
        err = err_fh.read().decode("utf-8", errors="replace").strip()
    return rc, bytes_in, err


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_backup(args):
    site = os.path.abspath(args.site)
    ok, missing = is_wordpress_dir(site)
    if not ok:
        log("Folder ini BUKAN project WordPress yang valid:")
        for item in missing:
            log("  - hilang: %s" % item)
        die("Backup dibatalkan: %s bukan instalasi WordPress." % site)
    log("[OK] Terdeteksi project WordPress: %s" % site)

    cfg = parse_wp_config(os.path.join(site, WP_CONFIG_NAME))
    if not cfg.get("name"):
        die("DB_NAME tidak ditemukan di wp-config.php.")
    log("[OK] Database target: %s @ %s"
        % (cfg.get("name"), cfg.get("host") or "localhost"))

    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = args.name or os.path.basename(site.rstrip("/\\")) or "wordpress"
    out_dir = os.path.abspath(args.out) if args.out else SCRIPT_DIR

    zip_path = os.path.join(out_dir, "%s_%s.zip" % (prefix, stamp))
    sql_path = os.path.join(out_dir, "%s_%s.sql.gz" % (prefix, stamp))
    if args.output_base:
        zip_path = args.output_base + ".zip"
        sql_path = args.output_base + ".sql.gz"

    if not args.yes:
        for p in (zip_path, sql_path):
            if os.path.exists(p) and not ask_overwrite(p):
                die("Backup dibatalkan oleh pengguna.")

    # ---- files ----
    log("")
    log("== Backup FILE ==")
    stats = backup_files(site, zip_path, extra_excludes=args.exclude)
    zip_size = os.path.getsize(zip_path)
    log("[OK] Zip   : %s (%s -> %s, %d entri)"
        % (zip_path, human_size(stats.raw_bytes), human_size(zip_size),
           stats.files))
    if stats.skipped:
        log("[--] Dilewati %d item (kecualikan/artefak backup):"
            % len(stats.skipped))
        for item in stats.skipped[:20]:
            log("      - %s" % item)
        if len(stats.skipped) > 20:
            log("      ... dan %d lainnya" % (len(stats.skipped) - 20))

    # ---- database ----
    log("")
    log("== Backup DATABASE ==")
    rc, total, _err = backup_database(cfg, sql_path, args.mysqldump)
    if rc != 0:
        if os.path.exists(sql_path):
            os.remove(sql_path)
        die("mysqldump gagal (exit code %d). Cek kredensial DB pada "
            "wp-config.php." % rc)
    if total == 0:
        log("[!] PERINGATAN: dump database kosong (0 byte).")
    log("[OK] SQL   : %s (%s)" % (sql_path, human_size(total)))

    log("")
    log("Selesai.")
    log("  File  : %s" % zip_path)
    log("  Database: %s" % sql_path)


def cmd_restore(args):
    zip_path = os.path.abspath(args.zip)
    if not os.path.isfile(zip_path):
        die("File zip tidak ditemukan: %s" % zip_path)

    # ---- validate zip ----
    log("== Validasi ZIP ==")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                die("Zip korup pada entri: %s" % bad)
            ok, missing, prefix, top = zip_wordpress_report(zf)
    except zipfile.BadZipFile:
        die("File '%s' bukan arsip ZIP yang valid." % zip_path)
    if not ok:
        log("Zip ini BUKAN backup WordPress yang valid.")
        for item in missing:
            log("  - hilang di root zip: %s" % item)
        die("Restore dibatalkan.")
    log("[OK] Zip valid sebagai backup WordPress.")
    if prefix:
        log("[i] Entri zip berada di dalam folder pembungkus '%s' dan akan "
            "diekstrak flat ke target." % prefix)
    log("[i] Isi root zip: %s" % ", ".join(top[:12])
        + (" ..." if len(top) > 12 else ""))

    # ---- target dir ----
    target = os.path.abspath(args.target) if args.target else prompt(
        "Folder tujuan restore", default=os.getcwd())
    if os.path.exists(target) and not os.path.isdir(target):
        die("Path tujuan sudah ada dan bukan folder: %s" % target)
    if os.path.isdir(target) and os.listdir(target) and not args.yes:
        if not confirm("Folder target tidak kosong. Lanjutkan ekstraksi?"):
            die("Restore dibatalkan oleh pengguna.")
    os.makedirs(target, exist_ok=True)

    # ---- extract files ----
    log("")
    log("== Ekstrak FILE ==")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            count = safe_extract(zf, target, prefix=prefix)
    except RuntimeError as exc:
        die(str(exc))
    log("[OK] %d entri diekstrak ke: %s" % (count, target))

    extracted_config = os.path.join(target, WP_CONFIG_NAME)
    old_cfg = {"name": "", "user": "", "password": "", "host": ""}
    if os.path.isfile(extracted_config):
        old_cfg = parse_wp_config(extracted_config)
        log("[OK] wp-config.php hasil ekstraksi terbaca.")

    # ---- database ----
    log("")
    log("== Restore DATABASE ==")
    sql_gz = find_sql_gz(zip_path, args.sql_gz)
    log("[i] Sumber dump: %s" % sql_gz)

    new = {
        "name": args.db_name or old_cfg.get("name", ""),
        "user": args.db_user or old_cfg.get("user", ""),
        "password": (args.db_pass if args.db_pass is not None
                     else old_cfg.get("password", "")),
        "host": args.db_host or old_cfg.get("host") or "localhost",
    }
    if not args.yes:
        # Offer NEW credentials interactively; pressing Enter keeps the
        # value currently set (old wp-config.php values by default).
        log("Detail database BARU untuk import (Enter = pakai nilai yang "
            "tampil):")
        new["name"] = prompt("  Nama database", default=new["name"] or None,
                             required=not new["name"])
        new["user"] = prompt("  Username database",
                             default=new["user"] or None,
                             required=not new["user"])
        new["host"] = prompt("  Host database",
                             default=new["host"] or "localhost")
        new["password"] = prompt("  Password database", secret=True,
                                 default=new["password"] or None)

    still_missing = [k for k in ("name", "user", "host") if not new.get(k)]
    if still_missing:
        die("Kredensial database belum lengkap (%s). Berikan lewat opsi "
            "--db-name/--db-user/--db-host atau jalankan tanpa --yes."
            % ", ".join(still_missing))

    log("[i] Target DB : %s @ %s (user: %s)"
        % (new["name"], new["host"], new["user"]))

    if args.create_db:
        create_database(new["user"], new["password"], new["host"],
                        new["name"], args.mysql)
        log("[OK] Database siap (created if not exists).")

    rc, bytes_in, err = import_database(
        sql_gz, new["name"], new["user"], new["password"], new["host"],
        args.mysql)
    if rc != 0:
        detail = ("Pesan MySQL:\n%s" % err) if err else \
                 ("Periksa username/password/hak akses user '%s' ke "
                  "database '%s'." % (new["user"], new["name"]))
        die("Import database gagal (exit code %d). %s" % (rc, detail))
    if bytes_in == 0:
        log("[!] PERINGATAN: dump kosong (0 byte setelah dekompresi).")
    else:
        log("[OK] Import selesai (%s SQL dimasukkan)." % human_size(bytes_in))

    # ---- sync wp-config.php ----
    updates = {}
    for norm_key in ("name", "user", "password", "host"):
        old_value = old_cfg.get(norm_key, "")
        if old_value != new[norm_key]:
            updates[norm_key] = new[norm_key] or ""

    if updates and os.path.isfile(extracted_config):
        if args.no_config_update:
            log("[i] wp-config.php TIDAK diubah (--no-config-update).")
            log("[i] Jangan lupa ubah manual: %s" % ", ".join(sorted(updates)))
        else:
            changed = update_wp_config(extracted_config, updates)
            if changed:
                log("[OK] wp-config.php diperbarui: %s "
                    "(cadangan .bak tersimpan)." % ", ".join(changed))
            else:
                log("[!] Pola define() untuk %s tidak ditemukan; ubah manual."
                    % ", ".join(sorted(updates)))

    log("")
    log("Selesai. Situs direstore ke: %s" % target)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="wp_backup.py",
        description="Backup & restore file WordPress (zip flat) + database "
                    "(mysqldump -> .sql.gz).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pb = sub.add_parser("backup", help="Backup file + database WordPress.")
    pb.add_argument("--site", required=True,
                    help="Path folder WordPress, mis. /home/mywordpress/"
                         "public_html")
    pb.add_argument("--out", help="Folder output (default: folder skrip ini)")
    pb.add_argument("--name", help="Prefiks nama file output")
    pb.add_argument("--exclude", action="append", default=[],
                    help="Pola glob tambahan untuk dikecualikan (boleh "
                         "berulang)")
    pb.add_argument("--mysqldump", default="mysqldump",
                    help="Path binary mysqldump")
    pb.add_argument("--output-base",
                    help="Path dasar output tanpa ekstensi (override --out/"
                         "--name)")
    pb.add_argument("-y", "--yes", action="store_true",
                    help="Timpa file output tanpa konfirmasi")

    pr = sub.add_parser("restore", help="Restore zip + database WordPress.")
    pr.add_argument("--zip", required=True, help="File zip hasil backup")
    pr.add_argument("--target", help="Folder tujuan ekstraksi")
    pr.add_argument("--sql-gz", help="File .sql.gz untuk import "
                                     "(default: otomatis)")
    pr.add_argument("--db-name", help="Nama database baru")
    pr.add_argument("--db-user", help="Username database baru")
    pr.add_argument("--db-pass", help="Password database baru "
                                      "(kosongkan utk prompt)")
    pr.add_argument("--db-host", help="Host database baru")
    pr.add_argument("--create-db", action="store_true",
                    help="CREATE DATABASE IF NOT EXISTS sebelum import")
    pr.add_argument("--no-config-update", action="store_true",
                    help="Jangan ubah wp-config.php setelah restore")
    pr.add_argument("--mysql", default="mysql", help="Path binary mysql")
    pr.add_argument("-y", "--yes", action="store_true",
                    help="Lewati konfirmasi interaktif")

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "backup":
        cmd_backup(args)
    else:
        cmd_restore(args)


if __name__ == "__main__":
    main()
