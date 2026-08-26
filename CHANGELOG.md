# Changelog

Semua perubahan penting pada proyek dicatat di berkas ini.

## [Unreleased]

### Added
- Opsi `--server` pada perintah `backup` untuk menampilkan contoh perintah
  `scp` (nama file & path mengikuti hasil backup sebenarnya) sebagai petunjuk
  mengunduh hasil backup dari server.

## [1.1.0] - 2026-08-26

### Added
- Menu progress bar interaktif saat menjalankan backup/restore file (visual
  kemajuan operasi yang panjang).
- Opsi `--output-base PATH` untuk menentukan path output eksplisit tanpa
  ekstensi (override `--out` / `--name`).
- Opsi `--mysqldump PATH` dan `--mysql PATH` untuk menentukan path binary
  manual.
- Deteksi & pengecualian artefak backup lama (`*.zip`, `*.sql.gz`) dan junk
  OS (`.DS_Store`, `Thumbs.db`, `desktop.ini`) saat membuat zip.
- Restore: pencarian otomatis sumber dump `.sql.gz` (pasangan nama zip,
  atau file terbaru di folder yang sama, atau `--sql-gz`).

### Changed
- Restrukturisasi internal: pemisahan logika file/system utils, terminal
  styling, dan operasi backup/restore menjadi fungsi modular.
- Kredensial database dikirim ke client MySQL lewat environment variable
  `MYSQL_PWD` (bukan argumen), sehingga tidak muncul di process list.

### Fixed
- Restore: pengaman penolakan path absolut / `..` pada entri zip (anti
  zip-slip) serta dukungan symlink & permission POSIX.

## [1.0.0] - 2026-08-24

### Added
- Rilis pertama.
- Perintah `backup`: validasi folder WordPress, zip isi folder secara flat,
  dan dump database (`mysqldump` → `.sql.gz`) dengan kredensial otomatis dari
  `wp-config.php`.
- Perintah `restore`: validasi zip, ekstraksi aman, import dump, dan update
  otomatis `wp-config.php` (dengan cadangan `.bak`).
- Dokumentasi (`README.md`), konfigurasi ignore (`requirements.txt`),
  dan `.gitignore`.