# RtcView

**Mevcut** bir go2rtc'ye bağlanan, Frigate benzeri, sıfır-gecikmeli (WebRTC) kamera izleme, **kayıt / playback**, **ONVIF hareket & insan algılama**, **grup bazlı bildirim**, **disk yönetimi** ve **ağ izleme** arayüzü.
Kendi başına stream sunmaz; go2rtc'de tanımlı stream'leri WHEP üzerinden alır ve gösterir, kayıt için RTSP çıkışını FFmpeg ile segmentler halinde diske yazar.
PWA uyumludur, Ubuntu Noble (rk3399, arm64) üzerinde izole Python venv içinde çalışır — NanoPi R4S gibi SBC'ler dahil herhangi bir Linux/systemd cihazında çalışacak şekilde tasarlanmıştır.

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

### Kayıt
- **Sunucu tarafında FFmpeg** ile 24/7 veya zamanlı segment kayıt (`-c copy`, transcode yok — CPU dostu)
- Kamera başına **mod seçimi**: kapalı / sürekli / haftalık zamanlı / sadece manuel
- **Manuel kayıt butonu** her tile'da (varsayılan 10 dk pencere)
- **Snapshot** butonu (JPEG, opsiyonel diske kayıt + indir)
- **Ses kaydı** kamera başına ayarlanabilir
- **Otomatik retention** (gün) **VE** kota (GB) — çift kurallı background purger
- **Kayıt kilidi** — önemli segmentler otomatik temizlenmez
- Kayıt aktifken tile'da REC noktası + kırmızı kayıt butonu

### Playback / İzleme
- Tam ekran "Kayıtlar" görünümü (sidebar ▷ butonu veya <kbd>V</kbd>)
- **24 saatlik timeline şeridi** — segmentler renkli barlar (mavi = normal, sarı = kilitli, kırmızı = manuel, yeşil = oynatılan)
- Fare tekerleği ile timeline zoom, sürükleyerek pan, tıkla → o zamana seek
- Player: oynat/duraklat, ±10 sn, hız (0.5×–32×), kare-kare (<kbd>,</kbd>/<kbd>.</kbd>)
- **Segmentler otomatik sıralı oynanır** (kesintisiz)
- Segment bazında: indir / sil / kilitle / karesini kaydet
- Tarih navigasyonu (◀ önceki / bugün / sonraki ▶)

### Depolama & Disk Yönetimi
- Kayıt yolu **birden fazla** klasör olabilir; her yeni segment en boş yola yazılır, bir yol düşerse diğerleri kullanılmaya devam eder
- UI'dan (Ayarlar → Kayıt & Depolama) canlı olarak yol ekleme/çıkarma, kota, retention değiştirilir
- Disk kullanım çubuğu, en eski kaydın tarih/saati, "diski yeniden tara" ve "şimdi temizle" butonları
- **Depolama sağlığı akıllıdır**: disk dolması normal, rolling-storage davranışıdır — silinecek eski segment olduğu sürece hata/uyarı göstermez, yalnızca gerçekten çıkmaz sokaktaysa (silinecek hiçbir şey kalmadıysa veya izin/donanım sorunu varsa) kırmızı uyarı verir
- **Disk Yönetimi** paneli: bilgisayara bağlı fiziksel diskleri listeler (sistem/önyükleme diski hiçbir zaman gösterilmez), **ext4 veya f2fs** olarak biçimlendirir, `/etc/fstab`'a hiç dokunmadan `/mnt/rtcview/` altına bağlar, ayırır — UUID bazlı bağlama sayesinde her açılışta doğru disk otomatik yeniden bağlanır
- SQLite index (`<yol>/index.sqlite`), MP4 dosyaları `<yol>/<cam>/YYYY/MM/DD/`

### Hareket & İnsan Algılama (ONVIF)
- Kamera başına **ayrı ayrı** açılabilir: hareket algılama, insan algılama (ONVIF event stream üzerinden — go2rtc'ye ek yük bindirmez)
- Playback timeline'ında **renkli algılama şeridi**: turuncu = hareket, mavi = insan, gri = algılama yok
- Bazı kameralar (ör. Tapo) hareketin *bittiğini* bildirmiyor — bu yüzden kamera başına **"durdu" kabul etme süresi** (varsayılan 15 sn) ayarlanabilir: bu süre boyunca yeni olay gelmezse hareket/insan durmuş sayılır
- Kamera ayarlarında canlı durum kutucukları (yanan/sönük) + **"Gelişmiş / Hata Ayıklama"** paneli: bağlantı durumu, son olay zamanları, ham olay logu, "bağlantıyı test et" butonu

### Bildirimler (grup bazlı)
- Bildirim ayarları **kamera değil, grup** bazında yapılır — bir kamera birden fazla gruba ait olabilir, gruplardan **herhangi biri** o an aktifse bildirim gönderilir
- Grupsuz kameralar hiç bildirim göndermez
- Her grup için tek bir **açık/kapalı anahtar** vardır — bildirim gidip gitmeyeceğini yalnızca o anahtar belirler. Aşağısına eklenen **kurallar** ("Pzt, Sal, Çar · 09:00 → Bildirimleri Kapat" gibi) bu anahtarı belirtilen gün/saatte kendiliğinden açar ya da kapatır; anahtarı istediğiniz an elle de çevirebilirsiniz, bir sonraki kural gelene kadar öyle kalır. Kural yoksa bildirimler her zaman açıktır
- Aynı anahtar kenar çubuğundaki grup satırında da bulunur (🔔), Ayarlar'a girmeden tek dokunuşla açılıp kapatılabilir
- Sidebar'daki 🔔 ikonu okunmamış bildirim sayısını gösterir; panelden bir bildirime dokunmak doğrudan o kameranın o anındaki kaydına (playback) götürür
- "Tümünü sil" ile bildirim geçmişi tek tuşla temizlenir

### Kamera Grupları
- Kameralar birden fazla gruba atanabilir (ör. "İç Mekan", "Dış Mekan")
- Sidebar'da grup filtre çipleri hem kamera listesini hem de canlı ızgarayı filtreler
- Gruplar Ayarlar → Kameralar sekmesinden eklenir/yeniden adlandırılır/silinir

### Sistem & Bakım
Ayarlar → **Sistem** sekmesinde, açılır panel şeklinde:
- **Ağ Durumu** — cihazın tüm fiziksel ağ arayüzleri (birden fazla ethernet portu, wifi — NanoPi R4S gibi çift-ethernet kartlar dahil): bağlantı durumu, IP/MAC, anlık indirme/yükleme hızı, toplam veri, son bağlanma/kopma zamanları ve son olaylar listesi. Bağlantı kopma/gelme kernel netlink olaylarıyla anlık yakalanır (sabit aralıklı yoklamayı beklemez), bu yüzden birkaç saniyelik kısa kesintiler bile kaçırılmaz
- **Sistem Kaynakları** — CPU, bellek, aktif ffmpeg süreçleri (canlı, 2 sn'de bir güncellenebilir)
- **Kayıt Bekçisi** — bir kameranın ffmpeg süreci ayarlanabilir bellek sınırını aşarsa otomatik yeniden başlatılır (sızıntı koruması)
- **Sıcaklık Sensörü** — okunacak sysfs `thermal_zone` yolu cihaza göre ayarlanabilir (her cihazda aynı numarada olmayabilir), Sistem Kaynakları panelinde gösterilir
- **Güncelleme** — bkz. [Otomatik Güncelleme](#otomatik-güncelleme-github)
- **Yeniden Başlatma** — tek tuşla yalnızca RtcView servisini (kameralar birkaç saniyede geri gelir) veya cihazın tamamını yeniden başlatır; bağlı diskler açılışta otomatik yeniden bağlanır
- **Loglar** — servis kayıtlarını (journalctl) filtreli görüntüleme + panoya kopyalama

### Otomatik Güncelleme (GitHub)
- Ayarlar → Sistem → **Güncelleme** panelinde mevcut sürüm (git commit) gösterilir ve tek bir **"Şimdi Güncelle"** butonuyla GitHub'daki en son sürüm çekilip kurulur, servis yeniden başlatılır
- Uygulama kendi başına `sudo` çalıştırmaz (systemd sandbox'ı buna izin vermez) — sadece kendi yazma izni olan bir dosyaya dokunur, ayrı ve bağımsız çalışan bir sistem servisi bu dosyayı görüp güncellemeyi kendisi yürütür. Disk biçimlendirme/bağlama ve yeniden başlatma/kapatma da aynı desenle (tetik dosyası → ayrı root servisi) çalışır
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

İzole `python3 -m venv` oluşturur, `ffmpeg`/`f2fs-tools` paketlerini kurar, `rtcview` sistem kullanıcısı yaratır, systemd birimi ile açılışta başlatır. Ayrıca disk yönetimi (biçimlendirme/bağlama), otomatik güncelleme ve yeniden başlatma/kapatma için gereken root-yetkili yardımcı systemd birimlerini de kurar.

Servisin systemd sandbox'ı **/opt/rtcview**, seçtiğiniz kayıt yolu, ve `/mnt`, `/media`, `/srv`, `/var/lib` altına yazma yetkisi ile kurulur. Kayıt yolunu daha sonra bu dört kökten birinin altına yeni bir dizine değiştirmek istiyorsanız UI'dan doğrudan yapabilirsiniz. Başka bir yola geçmek isterseniz:

```bash
sudo rtcview-set-recording-path /istediginiz/mutlak/yol
```

## Kayıt yolu değiştirme

**UI'dan:** Ayarlar → "Kayıt & Depolama" → "+ Klasör ekle" ile yol ekleyin (yazılabilir mi kontrol edilir) → Kaydet. Ya da Disk Yönetimi panelinden bağladığınız bir diski doğrudan kayıt klasörü olarak ekleyin.

**Komut satırından (sandbox dışında bir yol için):**

```bash
sudo rtcview-set-recording-path /mnt/harici-disk/rtcview
# Ardından UI'dan da bu yolu ayarlar bölümüne girin.
```

**Mount önerileri:** Disk Yönetimi panelini kullanmıyorsanız ve NAS/USB'yi elle `/etc/fstab`'a ekliyorsanız, `uid=rtcview,gid=rtcview` veya `umask=002` seçenekleriyle mount edin ki servis kullanıcısı yazabilsin.

## Güncelleme

Kamera listesi, ayarlar ve mevcut kayıtlar korunur; sadece kod/asset yenilenir:

```bash
sudo bash scripts/update.sh
```

Otomatik olarak: config yedeği alır, servisi durdurur, `app/`, `requirements.txt`, `scripts/` üzerine yazar, pip bağımlılıklarını günceller, eksik varsayılan alanları config'e ekler, servisi yeniden başlatır. `config/`, `venv/`, `logs/`, kayıt klasörü **dokunulmaz**.

Aynı çalıştırma ayrıca şunları da kurar/yeniler:
- **Ayarlar → Sistem → Güncelleme** panelindeki tek-tuşlu "Şimdi Güncelle" özelliği: `rtcview-updater.path`/`.service` birimleri, GitHub'daki `origin` uzak deposunun HEAD'ini `<kurulum-dizini>-src` altında tutulan ayrı bir git kopyasına çekip `scripts/update.sh`'ı otomatik çalıştırır
- **Disk Yönetimi**: `rtcview-diskmgr.path`/`.service` (biçimlendirme/bağlama/ayırma işlerini kuyruk dosyasından işler) ve `rtcview-diskmgr-boot.service` (açılışta önceden bağlanmış diskleri UUID ile otomatik yeniden bağlar)
- **Yeniden Başlatma**: `rtcview-restart.path`/`.service` ve `rtcview-reboot.path`/`.service`

Bu birimlerin hepsi root olarak, `rtcview.service`'den bağımsız ayrı cgroup'larda çalışır — ana servis hiçbir zaman `sudo` çalıştırmaz (systemd sandbox'ı `NoNewPrivileges=yes` ile buna izin vermez), sadece kendi yazma izni olduğu bir dosyaya/dizine dokunur ve bu ayrı birimler o değişikliği görüp işi kendileri yürütür.

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
- <kbd>1</kbd>–<kbd>6</kbd> — 0.5× / 1× / 2× / 4× / 8× / 16×
- <kbd>E</kbd> — algılanan olaylar
- <kbd>Esc</kbd> — playback'ten çık

## Dosya düzeni

```
/opt/rtcview/
├── app/            # Flask kaynak
│   ├── main.py         # route'lar, uygulama fabrikası
│   ├── recorder.py     # ffmpeg segment kaydı
│   ├── storage.py      # disk sağlığı, retention/kota purge, playback index
│   ├── diskmgr.py      # blok cihaz listeleme + format/mount/unmount iş kuyruğu (yetkisiz taraf)
│   ├── netmon.py        # ağ arayüzü izleme (netlink olayları + bant genişliği)
│   ├── detection.py    # ONVIF hareket/insan algılama + bildirim kuralları
│   ├── ptz.py           # ONVIF PTZ
│   ├── go2rtc_client.py
│   ├── config.py        # config.json okuma/yazma, şema göçü
│   └── wsdl/            # ONVIF WSDL/XSD şemaları (detection + ptz bunu kullanır; bütün olarak taşınmalı)
├── venv/           # izole Python ortamı
├── config/config.json
├── logs/
└── recordings/     # (varsayılan) — kayıt yolu config'ten değiştirilebilir
    ├── index.sqlite       # segment index + detections + notifications tabloları
    ├── _snapshots/<cam>/YYYY/MM/DD/*.jpg
    └── <cam>/YYYY/MM/DD/<cam>_<ts>_NNNNN.mp4
/mnt/rtcview/<disk>/   # Disk Yönetimi panelinden biçimlendirilip bağlanan diskler (fstab'sız, UUID bazlı)
/opt/rtcview-src/   # otomatik güncelleme için tutulan ayrı git kopyası (yalnızca "Şimdi Güncelle" kullanıldıysa oluşur)
```
> Not: go2rtc **bu dizinde değildir**; ağınızdaki mevcut go2rtc kullanılır.

Kök yardımcı script'ler `scripts/` altında: `install.sh`, `update.sh`, `uninstall.sh`, `diskmgr_worker.py` (disk iş kuyruğunu işleyen root servisi), `diskmgr_boot.py` (açılışta disk yeniden bağlama), `self_update.sh`.

ONVIF hareket/insan algılama motorunun tamamı `app/detection.py` içindedir (kamera ile PullPoint event konuşması, algılama aralıklarının yazılması, bildirim kural motoru). Ayarlanabilir değişkenler dosyanın başındaki "Tunables" bölümündedir.

## API özeti

**Durum / ayarlar**
- `GET /api/status` · `GET /api/config`
- `GET /api/settings` · `POST /api/settings`
- `GET /api/go2rtc/settings` · `POST /api/go2rtc/settings` · `GET /api/go2rtc/streams`

**Kameralar**
- `GET /api/cameras` · `POST /api/cameras`
- `PUT /api/cameras/<id>` · `DELETE /api/cameras/<id>`
- `POST /api/cameras/reorder`

**Kayıt / Playback**
- `GET /api/recording/settings` · `POST /api/recording/settings`
- `GET /api/recording/status` — kamera başına recorder + disk istatistikleri (en eski segment dahil)
- `POST /api/recording/rescan` · `POST /api/recording/purge`
- `POST /api/cameras/<id>/record/start` · `/stop` — manuel tetik
- `GET /api/recordings?cam=<id>&from=<ts>&to=<ts>` — segment listesi (playback)
- `GET /api/recordings/<id>/stream` — Range destekli MP4 (HTML5 seek)
- `GET /api/recordings/<id>/download` · `DELETE /api/recordings/<id>` · `POST /api/recordings/<id>/lock`
- `POST /api/snapshot/<cam_id>` · `GET /api/snapshots` · `GET /api/snapshots/<id>`

**Hareket / İnsan Algılama**
- `GET /api/detection/status` — kamera başına canlı algılama durumu (bağlantı, son olay, hata logu)
- `GET /api/detection/events?cam=<id>&from=<ts>&to=<ts>` — playback timeline'ı için algılama aralıkları
- `POST /api/cameras/<id>/detection/test` — ONVIF bağlantısını anlık test eder

**PTZ**
- `POST /api/ptz/<id>/move` · `POST /api/ptz/<id>/stop`
- `GET /api/ptz/<id>/presets` · `POST /api/ptz/<id>/preset/<token>`

**Kamera Grupları / Bildirimler**
- `GET /api/groups` · `POST /api/groups`
- `PUT /api/groups/<id>` — ad, `notify_enabled`, `notify_schedule` (gün/saat bazlı aç-kapat kuralları)
- `DELETE /api/groups/<id>`
- `GET /api/notifications?unread_only=&limit=` · `DELETE /api/notifications` · `POST /api/notifications/read-all`

**Depolama / Disk Yönetimi**
- `GET /api/storage/devices` — bağlı fiziksel diskler (sistem diski hariç)
- `POST /api/storage/format` · `POST /api/storage/mount` · `POST /api/storage/unmount`
- `GET /api/storage/job/<job_id>` — asenkron disk işleminin sonucu

**Ağ**
- `GET /api/network/status` — arayüz listesi (durum, IP/MAC, bant genişliği, son bağlanma/kopma) + son olaylar

**Sistem / Güncelleme**
- `GET /api/system/stats` · `GET /api/system/logs`
- `GET /api/system/update/status` — mevcut git commit, "Şimdi Güncelle" hazır mı
- `POST /api/system/update` — güncellemeyi tetikler (bkz. [Güncelleme](#güncelleme))
- `POST /api/system/restart` — yalnızca RtcView servisini yeniden başlatır
- `POST /api/system/reboot` — cihazın tamamını yeniden başlatır

## Servis kontrolü

```bash
systemctl status rtcview
sudo systemctl restart rtcview
journalctl -u rtcview -f
```
