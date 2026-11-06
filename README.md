# Weekly IG Post Generator (Rumah123)

Pipeline otomatis yang mengubah berita properti terbaru menjadi caption Instagram untuk Gen Z/milenial pencari rumah pertama, dengan validator aturan brand sebelum hasilnya disimpan.

## Alur
1. `feeds.txt` berisi RSS berita properti. Script mengambil berita 7 hari terakhir, membuang duplikat, lalu memilih yang terbaru.
2. Tiap berita dikirim ke LLM dengan `prompts/system_prompt.md` dan `prompts/user_prompt.md`. Output berupa JSON (caption, facts_used, angle).
3. `validate()` memeriksa aturan: 120-180 kata, 6-8 hashtag, 4-6 emoji, "Rumah123" tepat sekali, tanpa nominal harga/suku bunga/nama bank/kompetitor/janji KPR, dan **setiap angka harus ada di sumber berita** (anti-halusinasi).
4. Jika gagal, daftar masalah dikirim balik ke LLM untuk revisi (maksimal 3 percobaan).
5. Hasil disimpan ke `output/posts_YYYY-MM-DD.md` dan `.json`. Draf tetap perlu ditinjau manusia sebelum diposting.
6. GitHub Actions menjalankan ini tiap Senin 08:00 WIB dan meng-commit hasilnya.

## Menjalankan lokal
```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...        # atau LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python generate.py --dry-run     # cek berita yang terpilih
python generate.py
```

## Menjalankan di GitHub Actions
Settings > Secrets and variables > Actions: tambahkan secret `GEMINI_API_KEY` (atau `ANTHROPIC_API_KEY`). Opsional, variable `LLM_PROVIDER` dan `MODEL`. Lalu Actions > Weekly IG Post Generator > Run workflow.

## Keputusan desain
- Prompt dan kode dipisah (`prompts/`) agar tim konten bisa mengubah gaya tanpa menyentuh kode.
- Validator berbasis aturan dipakai selain prompt karena LLM sering melanggar batasan numerik (jumlah kata, hashtag) dan menambah angka yang tidak ada di sumber.
- Model dan provider bisa diganti lewat environment variable.

## Keterbatasan
- RSS hanya memberi judul dan ringkasan pendek, bukan isi artikel penuh, sehingga caption tetap umum.
- Pengecekan angka tidak menangkap klaim non-angka yang keliru.
- Belum ada pembuatan gambar atau posting otomatis ke Instagram (sengaja, agar ada review manusia).
