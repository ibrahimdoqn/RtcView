# RtcView

Kendi **go2rtc**'sini kurup yöneten, Frigate benzeri, sıfır-gecikmeli (WebRTC) kamera izleme, **kayıt / playback**, **Home Assistant tabanlı hareket/insan/araç algılama**, **grup bazlı bildirim** ve **ağ izleme** arayüzü.
go2rtc'de tanımlı stream'leri WHEP üzerinden alır ve gösterir, kayıt için RTSP çıkışını FFmpeg ile segmentler halinde diske yazar. go2rtc'nin kendisi de RtcView'ın kurulumunun bir parçasıdır — kendi systemd servisi olarak çalışır, yapılandırması (`config.yaml`) ve logları Ayarlar → go2rtc sekmesinden yönetilir. RtcView, upstream go2rtc'nin bilinen bir çökme hatasını (bkz. `vendor/go2rtc`, `scripts/go2rtc-writebuffer-recover.patch`) düzelten kendi derlemesini kullanır.
PWA uyumludur, Ubuntu Noble (rk3399, arm64) üzerinde izole Python venv içinde çalışır — NanoPi R4S gibi SBC'ler dahil herhangi bir Linux/systemd cihazında çalışacak şekilde tasarlanmıştır.

## Ön koşul

Kayıt için sistemde **`ffmpeg`** yüklü olmalı (installer otomatik kurar). go2rtc'yi ayrıca kurmanıza gerek yok —
installer kendi (hata düzeltmeli) go2rtc'sini indirip systemd servisi olarak kurar. Kameralar go2rtc'nin
`streams:` bölümünde tanımlı olmalı — bunu Ayarlar → go2rtc sekmesindeki yapılandırma editöründen yapabilirsiniz.
Hareket/insan/araç algılama istiyorsanız (isteğe bağlı) ağınızda çalışan bir **Home Assistant** olmalı,
kameralarınızın hareket/insan/araç durumu orada `binary_sensor` olarak görünüyor olmalı (Frigate, kameranızın
kendi entegrasyonu, vb. — kaynak fark etmez).

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
- **Otomatik retention** (gün) — background purger; disk dolarsa düşük disk marjı devreye girer (bkz. Depolama)
- **Kayıt kilidi** — önemli segmentler otomatik temizlenmez
- Kayıt aktifken tile'da REC noktası + kırmızı kayıt butonu

### Playback / İzleme
- Tam ekran "Kayıtlar" görünümü (sidebar ▷ butonu veya <kbd>V</kbd>)
- **24 saatlik timeline şeridi** — segmentler renkli barlar (mavi = normal, sarı = kilitli, kırmızı = manuel, yeşil = oynatılan)
- Fare tekerleği ile timeline zoom, sürükleyerek pan, tıkla → o zamana seek
- Player: oynat/duraklat, ±10 sn, hız (0.5×–32×), kare-kare (<kbd>,</kbd>/<kbd>.</kbd>)
- **Ses aç/kapat** kontrolü — varsayılan kapalı (eski kayıtları izlerken beklenmedik ses olmasın diye), istenirse hoparlör düğmesiyle açılır; panel her açılışında yeniden kapalıya döner
- **Segmentler otomatik sıralı oynanır** (kesintisiz)
- Segment bazında: indir / sil / kilitle / karesini kaydet
- Tarih navigasyonu (◀ önceki / bugün / sonraki ▶)

### Depolama
- **Bir veya birden fazla kayıt klasörü** kullanılabilir — RtcView disk biçimlendirme, bağlama veya ayırma yapmaz; hedef diskleri işletim sisteminde siz bağlarsınız (bkz. [Kurulum](#kurulum)), RtcView sadece bu yollara yazar. Birden fazla yol eklenirse sırayla doldurulur: ilki dolana kadar ona yazılır, sonra sıradaki devreye girer
- UI'dan (Ayarlar → Kayıt & Depolama) canlı olarak yol(lar), retention (gün) ve düşük disk marjı (MB) değiştirilir. Kota (toplam boyut sınırı) özelliği kaldırıldı — tek sınır retention + düşük disk marjı
- Disk kullanım çubuğu, en eski kaydın tarih/saati, "diski yeniden tara" butonu
- **Depolama sağlığı akıllıdır**: disk dolması normal, rolling-storage davranışıdır — silinecek eski segment olduğu sürece hata/uyarı göstermez. Her yeni segment/anlık görüntü başlamadan hemen önce, yapılandırılmış klasörlerin **hepsi** ayarlanabilir düşük disk marjının altındaysa (varsayılan 1 GB), retention süresi dolmasa bile en eski kayıtlar (o an aktif kamera sayısı kadar) otomatik silinip yer açılır — en az bir klasörde yer olduğu sürece hiçbir şey silinmez, sıralı doldurma zaten oraya geçer. `purge_once()` (yaş bazlı retention temizliği, varsayılan 60 sn'de bir) bu kontrolü de periyodik olarak tekrarlar — kayıt tamamen kapalıyken bile bir güvenlik ağı olarak. Yalnızca gerçekten çıkmaz sokaktaysa (silinecek hiçbir şey kalmadıysa veya izin/donanım sorunu varsa) kırmızı uyarı verir
- Bir segment silinirken dosya diskten fiilen kaldırılamazsa (geçici I/O hatası), bir sonraki temizlik turunda otomatik olarak disk yeniden taranır — index'te görünmeyen ama fiilen diskte duran dosyalar kendiliğinden yeniden kayda alınır. Arızalı/erişilemez bir disk, sağlıklı diğer disklerin temizliğini asla engellemez
- **Segmentler her zaman önce RAM'de (`/tmp`, tmpfs) oluşturulur** — artık isteğe bağlı bir seçenek değil, zorunlu mimari: ffmpeg hiçbir zaman doğrudan fiziksel diske yazmaz (aşağıdaki nadir istisna hariç), segment kapanınca hangi disk o an sırada müsaitse oraya taşınır. Bunun asıl noktası: canlı ffmpeg süreci yalnızca RAM'e bağımlı olduğu için **hangi disk dolu/yavaş/erişilemez olursa olsun kayıt hiçbir zaman durmaz veya yeniden başlamaz** — her kapanan segmentin hedefi taşınma anında yeniden hesaplanır, gün değişse veya en uygun disk değişse bile
- RtcView `/tmp`'nin gerçekten tmpfs (RAM) olduğunu kendisi doğrular; değilse (veya o an yeterli boş RAM yoksa, "Gerekli boş RAM" eşiği) **sadece o oturum için** doğrudan diske yazmaya döner — bir sonraki başlangıçta RAM'e geçiş yeniden denenir, kalıcı bir düşüş değildir. Bir kameranın RAM'de biriken taşınmamış verisi "kamera başına RAM sınırı"nı aşarsa (tüm diskler gerçekten dolu/erişilemez durumdaysa olur), kayıt durdurulmaz — bunun yerine RAM'deki en eski taşınmamış segment silinir (veri kaybı kabul edilir, kayıt kesintisiz devam eder)
- SQLite index (`<yol>/index.sqlite`), MP4 dosyaları `<yol>/<cam>/YYYY/MM/DD/`

### Hareket, İnsan & Araç Algılama (Home Assistant)
- Algılama artık RtcView'ın kameraya ONVIF event bağlantısı kurmasıyla değil, **Home Assistant**'taki `binary_sensor` varlıklarının durumunu izleyerek çalışır — Frigate, bir kameranın kendi entegrasyonu veya HA'da tanımlı herhangi bir sensör olabilir. RtcView'ın kendisi kameraya hiç bağlanmaz, sadece HA'nın WebSocket API'sine **tek bir kalıcı bağlantı** kurup ilgili sensörlerin `on`/`off` durumunu dinler
- Ayarlar → Genel'den **tek bir HA örneğine** bağlanılır (URL + long-lived access token); her kameranın ayarlarında bu bağlantıdan çekilen `binary_sensor.*` listesinden **hareket / insan / araç** için ayrı ayrı sensör seçilir — hiçbiri zorunlu değil, istediğiniz kadarını boş bırakabilirsiniz
- Playback timeline'ında **renkli algılama şeridi**: turuncu = hareket, mavi = insan, mor = araç, gri = algılama yok
- Durum değişiklikleri HA'dan anlık (WebSocket push) gelir — RtcView tarafında ayrıca bir "durdu kabul etme süresi" ayarına gerek yoktur, sensörün kendi mantığı ne zaman "off" olacağına karar verir. Bağlantı koparsa, yeniden bağlanınca HA'daki güncel durumla otomatik senkronize olunur
- Kamera ayarlarında canlı durum kutucukları (yanan/sönük) + **"Gelişmiş / Hata Ayıklama"** paneli: HA bağlantı durumu, son olay zamanları, ham olay logu
- **Kurulum**: HA'da profilinizin altındaki *Uzun Ömürlü Erişim Jetonları* (Long-Lived Access Tokens) bölümünden bir jeton oluşturun → RtcView'da Ayarlar → Genel → Home Assistant bağlantısı'na URL'yi ve jetonu girip "Bağlantıyı test et" ile doğrulayın → her kameranın kendi ayarlarından ilgili sensörleri seçin

### Bildirimler (grup bazlı, Home Assistant kontrollü)
- Bildirim ayarları **kamera değil, grup** bazında yapılır — bir kamera birden fazla gruba ait olabilir, gruplardan **herhangi biri** o an aktifse bildirim gönderilir
- Grupsuz kameralar hiç bildirim göndermez
- Bir grubun bildirim gönderip göndermeyeceğini artık RtcView değil, **Home Assistant'taki bir `input_boolean` değişkeni** belirler (ör. `input_boolean.fabrika_bildirim`) — değişken açıksa bildirim gelir, kapalıysa gelmez. Zamanlama, otomasyon, sesli asistan, elle açma/kapama — hepsi HA tarafında; RtcView yalnızca değişkenin o anki durumunu okur (aynı paylaşılan WebSocket bağlantısı üzerinden, hareket/insan/araç algılamasıyla birlikte)
- Her grup için hangi değişkenin izleneceği Ayarlar → Bildirimler'den seçilir; aynı durum kenar çubuğundaki grup satırında salt-okunur bir nokta olarak da gösterilir (yeşil = açık, kırmızı = kapalı, gri = değişken seçilmemiş)
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
- **Yeniden Başlatma** — tek tuşla yalnızca RtcView servisini (kameralar birkaç saniyede geri gelir) veya cihazın tamamını yeniden başlatır
- **Loglar** — RtcView, go2rtc ve güncelleme/yeniden başlatma/reboot tetikleyici birimlerinin kayıtları (journalctl, birleşik zaman sıralı tek akış), filtreli görüntüleme + panoya kopyalama

### Otomatik Güncelleme (GitHub)
- Ayarlar → Sistem → **Güncelleme** panelinde mevcut sürüm (git commit) gösterilir ve tek bir **"Şimdi Güncelle"** butonuyla GitHub'daki en son sürüm çekilip kurulur, servis yeniden başlatılır
- Uygulama kendi başına `sudo` çalıştırmaz (systemd sandbox'ı buna izin vermez) — sadece kendi yazma izni olan bir dosyaya dokunur, ayrı ve bağımsız çalışan bir sistem servisi bu dosyayı görüp güncellemeyi kendisi yürütür. Yeniden başlatma/kapatma da aynı desenle (tetik dosyası → ayrı root servisi) çalışır
- Bu özelliğin çalışması için cihazda **bir kez** `sudo bash scripts/update.sh` çalıştırılmış olması yeterli (aşağıdaki [Güncelleme](#güncelleme) bölümüne bakın) — gerekli sistem servisleri o sırada otomatik kurulur

## Kurulum

```bash
sudo bash scripts/install.sh
```

Kurulum sırasında sırayla sorulur:

- **RtcView portu** (varsayılan `5000`)
- **go2rtc API portu** (varsayılan `1984`) ve **RTSP portu** (varsayılan `8554`) — RtcView'ın kendi kuracağı go2rtc için
- **Kayıt klasörü** (varsayılan `/opt/rtcview/recordings`) — istediğiniz mutlak yol; harici bir diske kaydetmek isterseniz o diski bu adıma gelmeden önce siz mount etmiş olmalısınız (RtcView disk biçimlendirme/bağlama yapmaz, bkz. altta "Mount önerileri"). Kurulumdan sonra Ayarlar'dan ikinci, üçüncü bir disk daha ekleyebilirsiniz — bkz. "Birden fazla disk"

İzole `python3 -m venv` oluşturur, `ffmpeg` paketini kurar, `rtcview` sistem kullanıcısı yaratır, RtcView'ın kendi (hata düzeltmeli) go2rtc derlemesini kurup `go2rtc.service` olarak başlatır, RtcView'ı systemd birimi ile açılışta başlatır. Ayrıca otomatik güncelleme ve yeniden başlatma/kapatma için gereken root-yetkili yardımcı systemd birimlerini de kurar.

**Zaten elle kurulmuş bir go2rtc'niz varsa** (eski systemd birimi, Docker container, vb.): installer'ı çalıştırmadan önce onu durdurup devre dışı bırakın — yeni `go2rtc.service` aynı portları (1984/8554) dinler ve ikisi aynı anda çalışamaz.

Servisin systemd sandbox'ı **/opt/rtcview**, seçtiğiniz kayıt yolu, ve `/mnt`, `/media`, `/srv`, `/var/lib` altına yazma yetkisi ile kurulur. Kayıt yolunu daha sonra bu dört kökten birinin altına yeni bir dizine değiştirmek istiyorsanız UI'dan doğrudan yapabilirsiniz. Başka bir yola geçmek isterseniz:

```bash
sudo rtcview-set-recording-path /istediginiz/mutlak/yol
```

## Kayıt yolu değiştirme / birden fazla disk

Harici bir diske geçmek (veya ikinci bir disk eklemek) istediğinizde: diski önce siz `/etc/fstab` ile (kalıcı, önerilen) veya elle mount edin, sonra RtcView'a o yolu gösterin.

**UI'dan:** Ayarlar → "Kayıt & Depolama" → "Kayıt klasörleri" listesine yeni bir klasör ekleyin (yazılabilir mi kontrol edilir) veya mevcut birini kaldırın.

**Birden fazla disk — sıralı doldurma:** Listeye birden fazla klasör eklerseniz kayıt sırayla doldurulur: ilk klasör dolana kadar (güvenlik payı düşene kadar) her segment ona yazılır; dolunca listedeki bir sonraki klasör devreye girer, üçüncü bir disk varsa aynı şekilde devam eder. RtcView disklerin hiçbirini kendisi biçimlendirmez/bağlamaz/ayırmaz — her klasörü siz mount etmiş ve elle yönetiyor olmalısınız. Bu bilinçli bir sınır: RtcView'ın kendi kendine disk yönetimi yaptığı bir önceki sürümde, harici bir USB diskin donanımsal arızaya girmesi ve uygulamanın buna otomatik tepki veren temizlik mantığı birleşince ciddi bir olay yaşanmıştı. En az bir klasörde yer olduğu sürece hiçbir şey silinmez — sıralı doldurma zaten sıradaki klasöre geçer. Yapılandırılmış klasörlerin **hepsi** aynı anda düşük disk marjının altına düşerse (Ayarlar'dan ayarlanabilir, varsayılan 1 GB), her yeni segment başlamadan önce en eski kayıtlardan (o an aktif kamera sayısı kadar) otomatik silinip yer açılır — hangi klasörde olduklarına bakılmaksızın, en eskiden başlanarak.

**Komut satırından (sandbox dışında bir yol için):**

```bash
sudo rtcview-set-recording-path /mnt/harici-disk/rtcview
# Ardından UI'dan da bu yolu "Kayıt klasörleri" listesine ekleyin.
```

**Mount önerileri:** `/etc/fstab`'a `uid=rtcview,gid=rtcview` veya `umask=002` seçenekleriyle ekleyin ki servis kullanıcısı yazabilsin, ve makine her açıldığında disk otomatik bağlansın. RtcView diski kendisi bağlamaz/biçimlendirmez — bu bilinçli bir tercih: her diski elle yönetmek, uygulamanın kendi kendine format/mount yapmasından çok daha öngörülebilir ve güvenlidir.

## Güncelleme

Kamera listesi, ayarlar ve mevcut kayıtlar korunur; sadece kod/asset yenilenir:

```bash
sudo bash scripts/update.sh
```

Otomatik olarak: config yedeği alır, servisi durdurur, `app/`, `requirements.txt`, `scripts/` üzerine yazar, pip bağımlılıklarını günceller, eksik varsayılan alanları config'e ekler, servisi yeniden başlatır. `config/`, `venv/`, `logs/`, kayıt klasörü **dokunulmaz**.

Aynı çalıştırma ayrıca şunları da kurar/yeniler:
- **Ayarlar → Sistem → Güncelleme** panelindeki tek-tuşlu "Şimdi Güncelle" özelliği: `rtcview-updater.path`/`.service` birimleri, GitHub'daki `origin` uzak deposunun HEAD'ini `<kurulum-dizini>-src` altında tutulan ayrı bir git kopyasına çekip `scripts/update.sh`'ı otomatik çalıştırır
- **Yeniden Başlatma**: `rtcview-restart.path`/`.service` ve `rtcview-reboot.path`/`.service`
- **go2rtc**: `go2rtc.service` (RtcView'ın kendi hata düzeltmeli go2rtc derlemesi, `vendor/go2rtc`'den) ve `rtcview-go2rtc-restart.path`/`.service` — Ayarlar → go2rtc'deki "Yeniden Başlat" düğmesinin arkasındaki tetikleyici

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
- **PTZ / ONVIF kimlik bilgileri**: host/port/user/pass — isteğe bağlı, yalnızca PTZ için kullanılır
- **Hareket & Nesne Algılama**: hareket / insan / araç için ayrı ayrı Home Assistant `binary_sensor` seçimi (Ayarlar → Genel'den HA bağlantısı kurulmuş olmalı)
- **Gruplar**: kameranın ait olduğu grup(lar) — bildirim ayarları buradan miras alınır
- **Kayıt**: mod (kapalı/sürekli/zamanlı/manuel), haftalık zaman aralıkları, ses, retention override

go2rtc'nin stream tanımı (`streams:`) Ayarlar → **go2rtc** sekmesindeki yapılandırma editöründen (`go2rtc.yaml`, ham metin) yönetilir — kaydettikten sonra "go2rtc'yi Yeniden Başlat" ile etkinleştirin. Aynı sekmede go2rtc'nin bağlantı ayarları (host/port) ve logları da bulunur.

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
│   ├── storage.py      # çoklu-disk depolama (sıralı doldurma): sağlık, retention purge, dolma-önleyici temizlik, playback index
│   ├── netmon.py        # ağ arayüzü izleme (netlink olayları + bant genişliği)
│   ├── homeassistant.py # Home Assistant WebSocket bağlantısı: hareket/insan/araç algılama + grup bildirim anahtarları
│   ├── ptz.py           # ONVIF PTZ
│   ├── go2rtc_client.py # go2rtc HTTP API'sine salt-okunur istemci (stream listesi, snapshot)
│   ├── go2rtc_config.py # go2rtc.yaml okuma/yazma + yeniden başlatma tetikleyicisi
│   ├── config.py        # config.json okuma/yazma, şema göçü
│   ├── VERSION          # uygulama sürümü için tek gerçek kaynak (bkz. CHANGELOG.md)
│   └── wsdl/            # ONVIF WSDL/XSD şemaları (yalnızca ptz.py kullanır)
├── vendor/go2rtc/  # RtcView'ın hata düzeltmeli go2rtc derlemesi (mimariye göre ikili + VERSION)
├── go2rtc/         # kurulumda vendor'dan kopyalanan çalışan go2rtc ikili dosyası + go2rtc.yaml
├── venv/           # izole Python ortamı
├── config/config.json
├── logs/
└── recordings/     # (varsayılan) — kayıt klasör(ler)i config'ten değiştirilebilir, birden fazla olabilir
    ├── index.sqlite       # segment index + detections + notifications tabloları (her zaman burada, hangi kayıt kökü seçilirse seçilsin)
    ├── _snapshots/<cam>/YYYY/MM/DD/*.jpg   # her kayıt kökünün altında kendi _snapshots'ı olabilir — sıralı doldurmayı segmentlerle birlikte takip eder
    └── <cam>/YYYY/MM/DD/<cam>_<ts>_NNNNN.mp4
/opt/rtcview-src/   # otomatik güncelleme için tutulan ayrı git kopyası (yalnızca "Şimdi Güncelle" kullanıldıysa oluşur)
```
> go2rtc neden RtcView'ın kendi derlemesi: upstream go2rtc'de gerçek, tekrarlanabilir bir çökme hatası var
> (bir tüketicinin HTTP yanıtı, henüz gönderimde olan bir paketle aynı anda kapatıldığında oluşan nil-pointer
> panic — go2rtc'nin process'inin TAMAMINI, tüm kameraların akışını birden düşürüyor). Düzeltme
> `scripts/go2rtc-writebuffer-recover.patch`'te; `scripts/build_go2rtc.sh` bunu upstream go2rtc kaynağına
> uygulayıp `vendor/go2rtc/`'ye derler. Kayıt yolu harici bir diske ayarlıysa (`/mnt/...` gibi) o disk
> `/etc/fstab` ile sizin tarafınızdan bağlanmış olmalı — bkz. [Kayıt yolu değiştirme](#kayıt-yolu-değiştirme).

Kök yardımcı script'ler `scripts/` altında: `install.sh`, `update.sh`, `uninstall.sh`, `self_update.sh`.

Home Assistant entegrasyonunun tamamı `app/homeassistant.py` içindedir (WebSocket bağlantısı, kimlik doğrulama, durum senkronizasyonu, algılama aralıklarının yazılması). Grup bildirim anahtarları (her grubun `input_boolean.*` değişkeni) da aynı bağlantı üzerinden aynı modülde izlenir — ayrı bir zamanlama motoruna gerek yok, çünkü zamanlama artık tamamen Home Assistant tarafında.

## API özeti

**Durum / ayarlar**
- `GET /api/status` · `GET /api/config`
- `GET /api/settings` · `POST /api/settings`
- `GET /api/go2rtc/settings` · `POST /api/go2rtc/settings` · `GET /api/go2rtc/streams`
- `GET /api/go2rtc/config` · `POST /api/go2rtc/config` · `POST /api/go2rtc/restart`
- `GET /api/system/logs` — RtcView, go2rtc ve güncelleme/yeniden başlatma tetikleyici birimlerinin kayıtları birleşik (journalctl'e birden fazla `-u` verilir)

**Kameralar**
- `GET /api/cameras` · `POST /api/cameras`
- `PUT /api/cameras/<id>` · `DELETE /api/cameras/<id>`
- `POST /api/cameras/reorder`

**Kayıt / Playback**
- `GET /api/recording/settings` · `POST /api/recording/settings`
- `GET /api/recording/status` — kamera başına recorder + disk istatistikleri (en eski segment dahil)
- `POST /api/recording/rescan`
- `POST /api/cameras/<id>/record/start` · `/stop` — manuel tetik
- `GET /api/recordings?cam=<id>&from=<ts>&to=<ts>` — segment listesi (playback)
- `GET /api/recordings/<id>/stream` — Range destekli MP4 (HTML5 seek)
- `GET /api/recordings/<id>/download` · `DELETE /api/recordings/<id>` · `POST /api/recordings/<id>/lock`
- `POST /api/snapshot/<cam_id>` · `GET /api/snapshots` · `GET /api/snapshots/<id>`

**Hareket / İnsan / Araç Algılama**
- `GET /api/detection/status` — kamera başına canlı algılama durumu (HA bağlantısı, son olay, hata logu)
- `GET /api/detection/events?cam=<id>&from=<ts>&to=<ts>` — playback timeline'ı için algılama aralıkları

**Home Assistant**
- `GET /api/homeassistant/settings` · `POST /api/homeassistant/settings` — URL/token/verify_ssl (token asla geri okunmaz)
- `POST /api/homeassistant/test` — bağlantıyı anlık test eder
- `GET /api/homeassistant/entities?domain=binary_sensor|input_boolean` — varlık listesi (kamera sensör seçicisi ve grup bildirim anahtarı seçicisi için; varsayılan `binary_sensor`)

**PTZ**
- `POST /api/ptz/<id>/move` · `POST /api/ptz/<id>/stop`
- `GET /api/ptz/<id>/presets` · `POST /api/ptz/<id>/preset/<token>`

**Kamera Grupları / Bildirimler**
- `GET /api/groups` · `POST /api/groups` — her grup `ha_notify_entity` (bir `input_boolean.*`) ve canlı `notify_active` durumunu taşır
- `PUT /api/groups/<id>` — ad, `ha_notify_entity`
- `DELETE /api/groups/<id>`
- `GET /api/notifications?unread_only=&limit=` · `DELETE /api/notifications` · `POST /api/notifications/read-all`

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
