# RtcView

**Mevcut** bir go2rtc'ye bağlanan, Frigate benzeri, sıfır-gecikmeli (WebRTC) kamera izleme ve **kayıt / playback** arayüzü.
Kendi başına stream sunmaz; go2rtc'de tanımlı stream'leri WHEP üzerinden alır ve gösterir, kayıt için RTSP çıkışını FFmpeg ile segmentler halinde diske yazar.
PWA uyumludur, Ubuntu Noble (rk3399, arm64) üzerinde izole Python venv içinde çalışır.

## Ön koşul

Ağınızda çalışan bir **go2rtc** olmalı (varsayılan API `http://127.0.0.1:1984`, RTSP `:8554`).
Kameralar go2rtc'nin `streams:` bölümünde tanımlı olmalı.
Kayıt için sistemde **`ffmpeg`** yüklü olmalı (installer otomatik kurar).

## Özellikler

### Canlı izleme
- **WebRTC (WHEP)** ile canlı, düşük gecikmeli görüntü (go2rtc üzerinden)
- **PWA** — telefon/tabletten "Ana Ekrana Ekle", çevrimdışı kabuk
- **Grid** görünüm (1–8 sütun), çift tık ile solo, tekerlek/pinch ile zoom, sürükle ile pan
- **PTZ** (ONVIF): 8 yön, zoom, presetler
- Sürükle-bırak sıralama, arama, koyu/açık tema

### Kayıt (yeni)
- **Sunucu tarafında FFmpeg** ile 24/7 veya zamanlı segment kayıt (`-c copy`, transcode yok — CPU dostu)
- Kamera başına **mod seçimi**: kapalı / sürekli / haftalık zamanlı / sadece manuel
- **Manuel kayıt butonu** her tile'da (varsayılan 10 dk pencere)
- **Snapshot** butonu (JPEG, opsiyonel diske kayıt + indir)
- **Ses kaydı** kamera başına ayarlanabilir
- **Otomatik retention** (gün) **VE** kota (GB) — çift kurallı background purger
- **Kayıt kilidi** — önemli segmentler otomatik temizlenmez
- Kayıt aktifken tile'da REC noktası + kırmızı kayıt butonu

### Playback / İzleme (yeni)
- Tam ekran "Kayıtlar" görünümü (sidebar ▷ butonu veya <kbd>V</kbd>)
- **24 saatlik timeline şeridi** — segmentler renkli barlar (mavi = normal, sarı = kilitli, kırmızı = manuel, yeşil = oynatılan)
- Fare tekerleği ile timeline zoom, sürükleyerek pan, tıkla → o zamana seek
- Player: oynat/duraklat, ±10 sn, hız (0.5×/1×/2×/4×), kare-kare (<kbd>,</kbd>/<kbd>.</kbd>)
- **Segmentler otomatik sıralı oynanır** (kesintisiz)
- Segment bazında: indir / sil / kilitle / karesini kaydet
- Tarih navigasyonu (◀ önceki / bugün / sonraki ▶)

### Depolama (yapılandırılabilir)
- Kayıt yolu **istediğiniz** dizin: `/opt/rtcview/recordings` (varsayılan), `/mnt/nas/...`, `/media/usb/...`, vb.
- UI'dan (Ayarlar → Kayıt & Depolama) canlı olarak yol/kota/retention değiştirilir
- Disk kullanım çubuğu, "diski yeniden tara" ve "şimdi temizle" butonları
- SQLite index (`<yol>/index.sqlite`), MP4 dosyaları `<yol>/<cam>/YYYY/MM/DD/`

## Kurulum

```bash
sudo bash scripts/install.sh
```

Kurulum sırasında sırayla sorulur:

- **RtcView portu** (varsayılan `5000`)
- **go2rtc API host / port** (varsayılan `127.0.0.1:1984`)
- **go2rtc RTSP portu** (varsayılan `8554`) — kayıt için
- **Kayıt klasörü** (varsayılan `/opt/rtcview/recordings`) — istediğiniz mutlak yol

İzole `python3 -m venv` oluşturur, `ffmpeg` paketini kurar, `rtcview` sistem kullanıcısı yaratır, systemd birimi ile açılışta başlatır.

Servisin systemd sandbox'ı **/opt/rtcview**, seçtiğiniz kayıt yolu, ve `/mnt`, `/media`, `/srv`, `/var/lib` altına yazma yetkisi ile kurulur. Kayıt yolunu daha sonra bu dört kökten birinin altına yeni bir dizine değiştirmek istiyorsanız UI'dan doğrudan yapabilirsiniz. Başka bir yola geçmek isterseniz:

```bash
sudo rtcview-set-recording-path /istediginiz/mutlak/yol
```

## Kayıt yolu değiştirme

**UI'dan:** Ayarlar → "Kayıt & Depolama" → "Kayıt klasörü" alanına yazın → ✓ butonuna basın (yol yazılabilir mi kontrol edilir) → Kaydet.

**Komut satırından (sandbox dışında bir yol için):**

```bash
sudo rtcview-set-recording-path /mnt/harici-disk/rtcview
# Ardından UI'dan da bu yolu ayarlar bölümüne girin.
```

**Mount önerileri:** NAS/USB için `/etc/fstab`'a `uid=rtcview,gid=rtcview` veya `umask=002` seçenekleriyle mount edin ki servis kullanıcısı yazabilsin.

## Güncelleme

Kamera listesi, ayarlar ve mevcut kayıtlar korunur; sadece kod/asset yenilenir:

```bash
sudo bash scripts/update.sh
```

Otomatik olarak: config yedeği alır, servisi durdurur, `app/`, `requirements.txt`, `scripts/` üzerine yazar, pip bağımlılıklarını günceller, eksik varsayılan alanları (recording bloğu, `rtsp_port`) config'e ekler, servisi yeniden başlatır. `config/`, `venv/`, `logs/`, kayıt klasörü **dokunulmaz**.

## Kaldırma

```bash
sudo bash scripts/uninstall.sh
# konfigürasyonu yedeklemek için:
sudo bash scripts/uninstall.sh --keep-config
```

## Kamera Ekleme

Arayüzde **+ Kamera** ile:

- **Ad**: örn. "Ön Kapı"
- **go2rtc Stream**: dropdown'dan seçilir (↻ ile yenilenir)
- **PTZ**: ONVIF host/port/user/pass — isteğe bağlı
- **Kayıt**: mod (kapalı/sürekli/zamanlı/manuel), haftalık zaman aralıkları, ses, retention override

RtcView go2rtc yapılandırmasını **değiştirmez**; sadece stream'leri okur ve WHEP ile oynatır, RTSP'den okuyup diske yazar.

## Klavye kısayolları

Canlı görünüm:
- <kbd>B</kbd> / <kbd>Tab</kbd> — menü aç/kapat
- <kbd>F</kbd> — tam ekran
- <kbd>P</kbd> — PTZ paneli
- <kbd>V</kbd> — kayıtları izle (playback)
- <kbd>G</kbd> / <kbd>Esc</kbd> — ızgaraya dön
- <kbd>R</kbd> — seçili görüntüde zoom sıfırla
- <kbd>1</kbd>–<kbd>8</kbd> — sütun sayısı

Playback:
- <kbd>Space</kbd> — oynat/duraklat
- <kbd>←</kbd> / <kbd>→</kbd> — ±10 sn
- <kbd>,</kbd> / <kbd>.</kbd> — kare-kare
- <kbd>1</kbd>–<kbd>4</kbd> — 0.5× / 1× / 2× / 4×
- <kbd>Esc</kbd> — playback'ten çık

## Dosya düzeni

```
/opt/rtcview/
├── app/            # Flask kaynak (main, recorder, storage, ptz, go2rtc_client, config)
├── venv/           # izole Python ortamı
├── config/config.json
├── logs/
└── recordings/     # (varsayılan) — kayıt yolu config'ten değiştirilebilir
    ├── index.sqlite
    ├── _snapshots/<cam>/YYYY/MM/DD/*.jpg
    └── <cam>/YYYY/MM/DD/<cam>_<ts>_NNNNN.mp4
```
> Not: go2rtc **bu dizinde değildir**; ağınızdaki mevcut go2rtc kullanılır.

## API özeti (yeni)

- `GET  /api/recording/settings` · `POST /api/recording/settings`
- `GET  /api/recording/status` — kamera başına recorder + disk istatistikleri
- `POST /api/recording/rescan` · `POST /api/recording/purge`
- `POST /api/cameras/<id>/record/start` · `/stop` — manuel tetik
- `GET  /api/recordings?cam=<id>&from=<ts>&to=<ts>` — segment listesi (playback)
- `GET  /api/recordings/<id>/stream` — Range destekli MP4 (HTML5 seek)
- `GET  /api/recordings/<id>/download` · `DELETE /api/recordings/<id>` · `POST /api/recordings/<id>/lock`
- `POST /api/snapshot/<cam_id>` — go2rtc'den JPEG alır, opsiyonel diske yazar

## Servis kontrolü

```bash
systemctl status rtcview
sudo systemctl restart rtcview
journalctl -u rtcview -f
```

## Hareket algılama?

Bu sürümde yok. Motion-triggered kayıt için ileride go2rtc/Frigate event dinleyicisi eklenebilir. Şu anda: sürekli, zamanlı veya manuel kayıt kullanın.
