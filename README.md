# RtcView

**Mevcut** bir go2rtc'ye bağlanan, Frigate benzeri, sıfır-gecikmeli (WebRTC) kamera izleme, **kayıt / playback**, **ONVIF hareket & insan algılama** ve **grup bazlı bildirim** arayüzü.
Kendi başına stream sunmaz; go2rtc'de tanımlı stream'leri WHEP üzerinden alır ve gösterir, kayıt için RTSP çıkışını FFmpeg ile segmentler halinde diske yazar.
PWA uyumludur, Ubuntu Noble (rk3399, arm64) üzerinde izole Python venv içinde çalışır. Ayrıca `android/` klasöründe, aynı sunucuya bağlanan bir Android istemcisi de bulunur (bkz. [Android Uygulaması](#android-uygulaması)).

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

### Hareket & İnsan Algılama (ONVIF)
- Kamera başına **ayrı ayrı** açılabilir: hareket algılama, insan algılama (ONVIF event stream üzerinden — go2rtc'ye ek yük bindirmez)
- Playback timeline'ında **renkli algılama şeridi**: turuncu = hareket, mavi = insan, gri = algılama yok
- Bazı kameralar (ör. Tapo) hareketin *bittiğini* bildirmiyor — bu yüzden kamera başına **"durdu" kabul etme süresi** (varsayılan 15 sn) ayarlanabilir: bu süre boyunca yeni olay gelmezse hareket/insan durmuş sayılır
- Kamera ayarlarında canlı durum kutucukları (yanan/sönük) + **"Gelişmiş / Hata Ayıklama"** paneli: bağlantı durumu, son olay zamanları, ham olay logu, "bağlantıyı test et" butonu

### Bildirimler (grup bazlı)
- Bildirim ayarları **kamera değil, grup** bazında yapılır — bir kamera birden fazla gruba ait olabilir, gruplardan **herhangi biri** o an aktifse bildirim gönderilir
- Grupsuz kameralar hiç bildirim göndermez; bir grubun zaman aralığı yoksa o grup da hiç bildirim göndermez
- Her grup için: etkin/kapalı anahtarı, haftalık zaman aralığı (gün + saat), **Ertele** (belirlenen saate kadar bildirimleri geçici olarak durdur) ve **Manuel Aç** (zamanlama dışında da elle, belirlenen saate kadar bildirimleri zorla aç) — ikisi de kalan süreyi canlı gösterir
- Sidebar'daki 🔔 ikonu okunmamış bildirim sayısını gösterir; panelden bir bildirime dokunmak doğrudan o kameranın o anındaki kaydına (playback) götürür
- "Tümünü sil" ile bildirim geçmişi tek tuşla temizlenir

### Kamera Grupları
- Kameralar birden fazla gruba atanabilir (ör. "İç Mekan", "Dış Mekan")
- Sidebar'da grup filtre çipleri hem kamera listesini hem de canlı ızgarayı filtreler
- Gruplar Ayarlar → Kameralar sekmesinden eklenir/yeniden adlandırılır/silinir

### Tek Sayfa Ayarlar
- Kamera ekleme/düzenleme dahil **tüm ayarlar** tek bir tam ekran sayfada, sekmeler halinde: **Genel · Kameralar · Bildirimler · Kayıt & Depolama · Sistem**
- Sidebar'da artık ayrı bir "Kamera Ekle" düğmesi yok — kamera ekleme de Ayarlar → Kameralar sekmesinden yapılır

### Otomatik Güncelleme (GitHub)
- Ayarlar → Sistem → **Güncelleme** panelinde mevcut sürüm (git commit) gösterilir ve tek bir **"Şimdi Güncelle"** butonuyla GitHub'daki en son sürüm çekilip kurulur, servis yeniden başlatılır
- Uygulama kendi başına `sudo` çalıştırmaz (systemd sandbox'ı buna izin vermez) — sadece kendi yazma izni olan bir dosyaya dokunur, ayrı ve bağımsız çalışan bir sistem servisi bu dosyayı görüp güncellemeyi kendisi yürütür
- Bu özelliğin çalışması için cihazda **bir kez** `sudo bash scripts/update.sh` çalıştırılmış olması yeterli (aşağıdaki [Güncelleme](#güncelleme) bölümüne bakın) — gerekli sistem servisleri o sırada otomatik kurulur

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

Otomatik olarak: config yedeği alır, servisi durdurur, `app/`, `requirements.txt`, `scripts/` üzerine yazar, pip bağımlılıklarını günceller, eksik varsayılan alanları config'e ekler, servisi yeniden başlatır. `config/`, `venv/`, `logs/`, kayıt klasörü **dokunulmaz**.

Aynı çalıştırma ayrıca **Ayarlar → Sistem → Güncelleme** panelindeki tek-tuşlu "Şimdi Güncelle" özelliğini de kurar/yeniler: `rtcview-updater.path` ve `rtcview-updater.service` adında iki sistemd birimi oluşturur (root olarak, `rtcview.service`'den bağımsız bir cgroup'ta çalışırlar) ve etkinleştirir. Bu iki birim kurulu olduktan sonra, UI'daki butona basmak GitHub'daki `origin` uzak deposunun HEAD'ini `<kurulum-dizini>-src` altında tutulan ayrı bir git kopyasına çeker ve `scripts/update.sh`'ı otomatik çalıştırır — yukarıdaki komutu elle çalıştırmakla aynı sonucu verir, sadece SSH'a gerek kalmadan.

## Kaldırma

```bash
sudo bash scripts/uninstall.sh
# konfigürasyonu yedeklemek için:
sudo bash scripts/uninstall.sh --keep-config
```

## Kamera Ekleme

Ayarlar → **Kameralar** sekmesindeki **+ Kamera Ekle** ile:

- **Ad**: örn. "Ön Kapı"
- **go2rtc Stream**: dropdown'dan seçilir (↻ ile yenilenir)
- **PTZ / ONVIF kimlik bilgileri**: host/port/user/pass — isteğe bağlı, PTZ ve hareket/insan algılama aynı bağlantı bilgilerini paylaşır
- **Hareket & İnsan Algılama**: ayrı ayrı açma/kapama, "durdu" kabul etme süresi
- **Gruplar**: kameranın ait olduğu grup(lar) — bildirim ayarları buradan miras alınır
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
├── app/            # Flask kaynak (main, recorder, storage, ptz, detection, go2rtc_client, config)
├── venv/           # izole Python ortamı
├── config/config.json
├── logs/
└── recordings/     # (varsayılan) — kayıt yolu config'ten değiştirilebilir
    ├── index.sqlite       # segment index + detections + notifications tabloları
    ├── _snapshots/<cam>/YYYY/MM/DD/*.jpg
    └── <cam>/YYYY/MM/DD/<cam>_<ts>_NNNNN.mp4
/opt/rtcview-src/   # otomatik güncelleme için tutulan ayrı git kopyası (yalnızca "Şimdi Güncelle" kullanıldıysa oluşur)
```
> Not: go2rtc **bu dizinde değildir**; ağınızdaki mevcut go2rtc kullanılır.

Repo kökünde ayrıca `tapo_detector/` (vendored ONVIF hareket/insan algılama motoru) ve `android/` (Android istemcisi kaynak kodu, sunucuya deploy edilmez) bulunur.

## API özeti (yeni)

**Kayıt / Playback**
- `GET  /api/recording/settings` · `POST /api/recording/settings`
- `GET  /api/recording/status` — kamera başına recorder + disk istatistikleri
- `POST /api/recording/rescan` · `POST /api/recording/purge`
- `POST /api/cameras/<id>/record/start` · `/stop` — manuel tetik
- `GET  /api/recordings?cam=<id>&from=<ts>&to=<ts>` — segment listesi (playback)
- `GET  /api/recordings/<id>/stream` — Range destekli MP4 (HTML5 seek)
- `GET  /api/recordings/<id>/download` · `DELETE /api/recordings/<id>` · `POST /api/recordings/<id>/lock`
- `POST /api/snapshot/<cam_id>` — go2rtc'den JPEG alır, opsiyonel diske yazar

**Hareket / İnsan Algılama**
- `GET  /api/detection/status` — kamera başına canlı algılama durumu (bağlantı, son olay, hata logu)
- `GET  /api/detection/events?cam=<id>&from=<ts>&to=<ts>` — playback timeline'ı için algılama aralıkları
- `POST /api/cameras/<id>/detection/test` — ONVIF bağlantısını anlık test eder

**Kamera Grupları**
- `GET  /api/groups` · `POST /api/groups`
- `PUT  /api/groups/<id>` — ad, `notify_enabled`, `notify_schedule`, `notify_snooze_until`, `notify_force_until`
- `DELETE /api/groups/<id>`

**Bildirimler**
- `GET  /api/notifications?unread_only=&limit=` · `DELETE /api/notifications`
- `POST /api/notifications/read-all`

**Sistem / Güncelleme**
- `GET  /api/system/stats` · `GET  /api/system/logs`
- `GET  /api/system/update/status` — mevcut git commit, "Şimdi Güncelle" hazır mı
- `POST /api/system/update` — güncellemeyi tetikler (bkz. [Güncelleme](#güncelleme))

## Servis kontrolü

```bash
systemctl status rtcview
sudo systemctl restart rtcview
journalctl -u rtcview -f
```

## Android Uygulaması

`android/` klasöründe, aynı sunucuya (Tailscale veya yerel ağ üzerinden, düz HTTP ile) bağlanan bir Android istemcisi bulunur. RtcView'in web arayüzünü bir WebView içinde açar — ayrı bir native arayüz değildir, web tarafındaki her geliştirme otomatik yansır. HTTPS olmadığı için standart Web Push/FCM kullanılamıyor; bunun yerine arka planda periyodik olarak (~15 dk) yeni bildirimler kontrol edilip native Android bildirimi gösterilir, dokunulduğunda ilgili kameranın o anına götürür.

Derleme talimatları ve mimari detaylar için: [`android/README.md`](android/README.md).
