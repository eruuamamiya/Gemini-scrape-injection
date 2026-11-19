# Gemini Scrape Injection

Server API berbasis **FastAPI + Playwright** yang mengirim prompt ke Gemini web (`gemini.google.com`) lewat browser Chromium headless, mengambil jawabannya, lalu mengeksposnya sebagai endpoint HTTP, termasuk endpoint yang kompatibel dengan OpenAI (`/v1/chat/completions`).

Mendukung multi-akun dan multi-tab per room (mencegah tabrakan prompt, Error 524, dan kerusakan blok kode). Awalnya dibangun untuk agent Hermes, tetapi bisa dipakai untuk chat AI biasa juga.

---

## Fitur

- **Multi-akun**: banyak akun di folder `accounts/`, dipilih otomatis berdasarkan akun yang paling tidak sibuk dan paling sedikit room-nya.
- **Multi-tab per room**: tiap `room_id` punya tab sendiri, maksimal 5 tab per akun (`MAX_TABS_PER_ACC`). Tab tertua ditutup bila melebihi batas.
- **`DEFAULT_ROOM`**: bila di-set dan request tidak mengirim `X-Room-ID`, semua chat masuk ke 1 room tetap, sehingga Gemini web mengingat percakapan sebelumnya (sesi tunggal).
- **Endpoint OpenAI-compatible**: `/v1/chat/completions` dan `/v1/models`.
- **Key per akun atau token global** untuk autentikasi.
- **Ekstraksi blok kode**: blok kode diubah ke Markdown dengan label bahasa. Bila label tidak ada, bahasa ditebak dari isi kode (html, json, php, python, javascript, sql, bash, css).
- **Profil Chromium persisten** per akun (`<nama>_profile`).
- **Cookie fleksibel**: JSON (list atau dict) atau cookie header mentah.

---

## Instalasi

```bash
pip install fastapi uvicorn playwright pydantic
playwright install chromium
```

---

## Menyiapkan Akun

Semua data disimpan di `GEMINI_DATA` (default `~/gemini-data`).

```
~/gemini-data/
├── accounts/
│   ├── akun1.json        # cookie akun 1
│   ├── akun1.key         # (opsional) key khusus akun 1, isi 1 baris
│   └── akun1_profile/    # dibuat otomatis (profil Chromium)
├── cookies.json          # fallback bila folder accounts/ tidak punya file .json
└── token                 # (opsional) token global, isi 1 baris
```

Format cookie di `accounts/<nama>.json`, salah satu dari:

```json
[{"name": "SID", "value": "..."}, {"name": "HSID", "value": "..."}]
```
```json
{"SID": "...", "HSID": "...", "NID": "..."}
```
```
SID=...; HSID=...; NID=...
```

Cookie hanya diinjeksi sebagai bibit saat profil browser baru dibuat (folder profil masih kosong).

---

## Menjalankan

```bash
python3 gemini_api.py
```

Default berjalan di `http://127.0.0.1:3464`.

### Variabel Environment

| Variabel       | Default          | Keterangan                                              |
|----------------|------------------|---------------------------------------------------------|
| `GEMINI_DATA`  | `~/gemini-data`  | Folder data (akun, cookie, token)                       |
| `PORT`         | `3464`           | Port server                                             |
| `HOST`         | `127.0.0.1`      | Alamat bind                                             |
| `DEFAULT_ROOM` | *(kosong)*       | Room tetap bila request tidak mengirim `X-Room-ID`      |
| `MAX_WAIT`     | `85`             | Batas tunggu respons Gemini (detik)                     |

Contoh dengan sesi tunggal:

```bash
DEFAULT_ROOM=hermes-sesi-utama python3 gemini_api.py
```

---

## Autentikasi

1. Bila token cocok dengan `key` salah satu akun, request dikunci ke akun itu.
2. Bila ada file `token`, token harus cocok dan akun bebas dipilih.
3. Bila tidak ada key akun maupun file `token`, API terbuka tanpa autentikasi.

Endpoint native memakai header `X-API-Key: <token>`. Endpoint OpenAI memakai `Authorization: Bearer <token>`.

---

## Endpoint

| Method | Path                   | Auth | Keterangan                                      |
|--------|------------------------|------|-------------------------------------------------|
| POST   | `/ask`                 | Ya   | Body: `{"prompt": "...", "room_id": "..."}`     |
| POST   | `/v1/chat/completions` | Ya   | Format OpenAI, room lewat header `X-Room-ID`    |
| GET    | `/v1/models`           | Ya   | Model: `gemini-hydrogen`                        |
| GET    | `/accounts`            | Ya   | Nama akun, room aktif, dan status sibuk         |
| GET    | `/health`              | Tidak| Jumlah akun dan `default_room`                  |

### Contoh `/ask`

```bash
curl -X POST http://127.0.0.1:3464/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: TOKEN_ANDA" \
  -d '{"prompt": "Halo", "room_id": "demo"}'
```

Respons: `{"model": "gemini-hydrogen", "room_id": "demo", "answer": "..."}`

### Contoh `/v1/chat/completions`

```bash
curl -X POST http://127.0.0.1:3464/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer TOKEN_ANDA" \
  -H "X-Room-ID: demo" \
  -d '{"messages": [{"role": "user", "content": "Halo"}]}'
```

---

## Perilaku Room

- **Tanpa `room_id` dan tanpa `DEFAULT_ROOM`**: dibuat tab sementara yang ditutup setelah request selesai.
- **Room sudah punya tab**: hanya pesan `user` terakhir yang dikirim (riwayat sudah ada di thread Gemini web).
- **Room belum punya tab**: seluruh `messages` digabung menjadi satu prompt dengan label `Instruksi`, `User`, `Asisten`.
- Setiap prompt yang digabung diberi tambahan instruksi agar Gemini langsung mengerjakan permintaan terakhir, dan memakai format ` ```bahasa:namafile.ekstensi ` untuk file kode.
- Bila memakai `DEFAULT_ROOM` dengan klien yang selalu mengirim full history (seperti Hermes), memori menjadi ganda: history API dan memori thread Gemini web.

---

## Batasan

- Respons selalu 1 JSON utuh. Streaming dinonaktifkan (`want_stream = False`).
- Model `gemini-hydrogen` dideklarasikan tanpa vision dan tanpa tools.
- Nilai `usage` hanyalah estimasi (jumlah karakter dibagi 4).
- Satu akun memproses satu request per waktu (lock per akun).
- Bila Gemini tidak merespons dalam `MAX_WAIT` detik (misalnya sesi login habis atau antrean penuh), server mengembalikan error HTTP 502.
