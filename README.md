# wp_backup — Backup & Restore WordPress (File + Database)

Skrip Python **tanpa dependency eksternal** (standard library saja) untuk:

- **Backup file**: zip seluruh isi folder WordPress (`public_html`) secara
  **flat** — isi folder langsung menjadi root archive, tanpa dibungkus
  folder baru.
- **Backup database**: `mysqldump` → dikompres menjadi `.sql.gz`.
  User/password/nama DB dibaca otomatis dari `wp-config.php`.
- **Validasi**: sebelum backup, folder dicek apakah benar project WordPress;
  sebelum restore, zip dicek apakah berupa backup WordPress.
- **Restore database**: import `.sql.gz` dengan opsi input
  **username / password / nama DB / host yang baru**, lalu `wp-config.php`
  hasil restore diperbarui otomatis (dengan cadangan `.bak`).

Kedua file hasil backup disimpan di **root project** (folder tempat
`wp_backup.py` berada), dengan nama seragam:

```
<prefix>_<YYYYmmdd_HHMMSS>.zip      <- file WordPress
<prefix>_<YYYYmmdd_HHMMSS>.sql.gz   <- dump database
```

## Prasyarat

- Python 3.8+ (**tidak butuh library Python eksternal** — stdlib saja; vede
  `requirements.txt`).
- Client MySQL: `mysqldump` dan `mysql` tersedia di PATH
  (atau tentukan path lewat `--mysqldump` / `--mysql`).
- Password dikirim ke client MySQL lewat environment variable `MYSQL_PWD`,
  sehingga tidak muncul di process list.

## Backup

```bash
# Contoh dasar sesuai skenario:
python wp_backup.py backup --site /home/mywordpress/public_html

# Dengan prefiks nama & pengecualian tambahan:
python wp_backup.py backup --site /home/mywordpress/public_html \
    --name mywordpress \
    --exclude "wp-content/cache/*" --exclude "*.log"
```

Yang dilakukan:

1. Validasi folder: wajib ada `wp-config.php`, `wp-settings.php`, dan
   `wp-content/`. Jika bukan WordPress → dibatalkan, tidak ada file setengah jadi.
2. Zip isi `public_html` **flat** (`wp-config.php`, `wp-content/`, dst.
   langsung di root zip). Artefak backup lama (`*.zip`, `*.sql.gz`) dan junk
   OS (`.DS_Store`, `Thumbs.db`, `desktop.ini`) dilewati otomatis.
3. Baca kredensial dari `wp-config.php`, jalankan:
   `mysqldump --single-transaction --quick --routines --triggers --events
   --no-tablespaces --default-character-set=utf8mb4` lalu stream hasilnya
   langsung ke `.sql.gz` (hemat memori untuk DB besar).

Opsi lain: `--out DIR` (folder output lain), `--output-base PATH`
(path output eksplisit tanpa ekstensi), `--mysqldump PATH`, `-y`
(timpa tanpa konfirmasi).

## Restore

```bash
python wp_backup.py restore --zip mywordpress_20250101_120000.zip \
    --target /home/mywordpress/public_html
```

Tanpa flag DB, skrip akan **bertanya interaktif**:

```
Detail database BARU untuk import (Enter = pakai nilai yang tampil):
  Nama database [wp_dummy_db]:
  Username database [wp_dummy_user]:
  Host database [localhost]:
  Password database [Enter = pakai nilai lama]:
```

Atau lengkap non-interaktif:

```bash
python wp_backup.py restore --zip mywordpress_20250101_120000.zip \
    --target /home/mywordpress/public_html \
    --db-name db_baru --db-user user_baru --db-pass rahasia --db-host localhost \
    --create-db -y
```

Yang dilakukan:

1. **Validasi zip**: harus memuat `wp-config.php`, `wp-settings.php`, dan
   entri `wp-content/` di root archive. Zip bukan-backup-WordPress ditolak.
   (Zip yang isinya dibungkus satu folder, mis. `public_html/…`, tetap
   diterima dan diekstrak flat.)
2. **Ekstraksi aman** ke `--target`: tolak path absolut/`..` (anti zip-slip),
   pertahankan permission Unix (POSIX), dukung symlink; folder target yang
   tidak kosong meminta konfirmasi (atau `-y`).
3. **Import database**: dekompresi `.sql.gz` on-the-fly dan dipipe ke
   `mysql`. Sumber dump dicari otomatis: file `.sql.gz` bernama pasangan
   si zip, atau `.sql.gz` terbaru di folder yang sama, atau pakai `--sql-gz`.
4. **Update `wp-config.php`**: nilai `DB_NAME`, `DB_USER`, `DB_PASSWORD`,
   `DB_HOST` disesuaikan dengan kredensial baru; versi lama disimpan sebagai
   `wp-config.php.bak-<timestamp>`.

Opsi penting:

| Opsi | Keterangan |
|---|---|
| `--target DIR` | Folder tujuan ekstraksi (default: prompt, fallback cwd) |
| `--sql-gz FILE` | File dump eksplisit untuk import |
| `--db-name/--db-user/--db-pass/--db-host` | Kredensial database baru |
| `--create-db` | Jalankan `CREATE DATABASE IF NOT EXISTS` (utf8mb4) dulu |
| `--no-config-update` | Jangan sentuh `wp-config.php` |
| `--mysql PATH` / `--mysqldump PATH` | Path binary manual |
| `-y` | Non-interaktif (timpa/lanjut tanpa tanya) |

## Contoh alur penuh

```bash
python wp_backup.py backup --site /home/mywordpress/public_html --name mywp
# -> mywp_20250101_120000.zip + mywp_20250101_120000.sql.gz

python wp_backup.py restore --zip mywp_20250101_120000.zip \
    --target /home/otheruser/public_html \
    --db-name new_db --db-user new_user --db-pass secret --create-db
```

## Dependencies

Skrip memakai **solo pustaka standard Python**; tidak bisogno `install` pip
rumo. Detail ada di `requirements.txt`.
