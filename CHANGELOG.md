# Changelog

Bu dosya RtcView'daki sürüm bazlı değişiklikleri listeler.
Biçim [Keep a Changelog](https://keepachangelog.com/) temel alınır, sürümleme [Semantic Versioning](https://semver.org/) izler (`MAJOR.MINOR.PATCH`).

## [Unreleased]

## [4.2.1] - 2026-08-17

### Düzeltildi
- Anasayfadaki canlı izleme, sekme/uygulama arka plana alınıp geri dönüldüğünde (özellikle mobilde) donuk bir karede takılı kalabiliyordu — hiçbir hata bildirilmediği için ne WHEP'in ICE durum değişikliği ne de mevcut yeniden bağlanma mantığı bunu fark ediyordu. Sebep: tarayıcı arka plandayken video decode'unu ve/veya bağlantıyı sessizce askıya alabiliyor; WHEP'in `oniceconnectionstatechange`'i gerçek bir ICE durumu değişikliği olmadan hiç tetiklenmeyebiliyor, MSE'nin ise ilk bağlantı kurulduktan sonra hiçbir sağlık kontrolü yok. Artık sayfa görünür hale geldiğinde (`visibilitychange`, ayrıca iOS Safari'nin geri-ileri önbelleği için `pageshow`) — arka planda en az 2 saniye kaldıysa (kısa bir uygulama geçişinde gereksiz yeniden bağlanmayı önlemek için) — ızgaradaki her canlı oynatıcı otomatik olarak yeniden başlatılıyor.

## [4.2.0] - 2026-08-16

### Eklendi
- **RtcView artık kendi go2rtc'sini kurar ve yönetir** — kurulum go2rtc'yi harici/mevcut bir servis olarak varsaymıyor, kendi `go2rtc.service`'ini kuruyor (`vendor/go2rtc`'den, aynı `rtcview` kullanıcısıyla, `NoNewPrivileges=yes` altında). Yeni Ayarlar → **go2rtc** sekmesi: bağlantı ayarları (host/port, eski Genel sekmesinden taşındı), ham `go2rtc.yaml` yapılandırma editörü (kaydet + yeniden başlat), ve go2rtc'nin systemd loglarını gösteren bir log görüntüleyici. Yeni backend uçları: `GET/POST /api/go2rtc/config`, `POST /api/go2rtc/restart`, `GET /api/go2rtc/logs` — hepsi `app/go2rtc_config.py`'de, mevcut tetik-dosyası + root-yetkili systemd birimi deseniyle (self-update/restart/reboot ile aynı mekanizma).
- **go2rtc'nin gerçek bir çökme hatası düzeltildi — client-side bir geçici çözüm değil, kaynağında bir yama.** Bu oturumda kök neden tam olarak tespit edildi: `pkg/core/writebuffer.go`'daki bir nil-pointer panic, bir tüketicinin (consumer) HTTP yanıtı arka planda kapatılırken (istemci bağlantıyı kesince, ya da stream yeniden yüklenince) hâlâ göndermede olan bir paket varsa go2rtc'nin **TÜM sürecini** çökertiyor — tek bir kameranın akışını değil, o an bağlı olan her kamerayı. Bu, PCM/FLAC ses koduna özgü değil (H264/H265 video Sender'ları üzerinden de tetiklenebiliyor, bkz. upstream issue `AlexxIT/go2rtc#1261`'deki üç ayrı panic raporu — üçü de aynı nil-pointer adresini paylaşıyor). `scripts/go2rtc-writebuffer-recover.patch`, `WriteBuffer.Write()`'a bir `recover()` koruması ekliyor — çökmeyi tüm sürece yaymak yerine tek bir tüketicinin hatası olarak sınırlıyor. Patch, go2rtc'nin tam olarak üretimde çalışan sürümüne (`v1.9.14`/`b5948cf`) karşı yazıldı, derlendi (linux/amd64 + linux/arm64) ve regresyon testiyle doğrulandı (`scripts/go2rtc-writebuffer-recover_test.go`: patch'siz sürümde gerçek çökmeyi birebir tekrarlıyor, patch'li sürümde temiz bir hata dönüyor). `scripts/build_go2rtc.sh` bu derlemeyi yeniden üretilebilir kılıyor.

### Değişti
- MSE canlı izleme (`stream.mp4?...&mp4=all`) artık bilinçli olarak kabul edilen bir risk taşımıyor — 4.1.1/4.1.2'de tartışılan "ses mi, kararlılık mı" ikilemi ortadan kalktı: patch sayesinde go2rtc artık PCM ses paketlerini FLAC'a sararken çökmüyor, bu yüzden hem ses çalışıyor hem de çökme riski yok.
- Ayarlar → Genel'deki "go2rtc bağlantısı" fieldset'i yeni go2rtc sekmesine taşındı.

### Belgeler
- `README.md`: "Ön koşul", kurulum adımları, dosya ağacı ve API tablosu artık go2rtc'nin RtcView tarafından kurulup yönetildiğini yansıtıyor; zaten elle kurulmuş bir go2rtc'den geçiş için not eklendi.

## [4.1.2] - 2026-08-16

### Düzeltildi
- 4.1.1'deki `mp4=` (boş) değişikliği geri alındı — canlı izlemede (MSE) sesi tamamen sessiz bırakıyordu. Sebep: go2rtc'nin `pkg/mp4/consumer.go`'daki ses işleme mantığı gerçek bir transcoder değil, sadece kameranın **zaten gönderdiği** codec'i paketliyor (AAC → doğrudan paketleme, PCM ailesi → FLAC'a sarma). `mp4=` isteği go2rtc'ye "bana sadece AAC ver" dedirtiyor; kameralar ham PCM (PCMA/PCMU) gönderdiği için eşleşen codec bulunamıyor ve ses hattı hiç kurulmuyordu. `stream.mp4` isteği tekrar `mp4=all` kullanıyor — ses geri geldi, 4.1.1'in önlemeye çalıştığı go2rtc çökme riski (bkz. aşağı) bilinçli olarak kabul edildi: go2rtc arada bir çöküp ~2 sn içinde kendiliğinden yeniden başlayabiliyor, kayıt hattı bundan etkilenmiyor, sadece canlı izleme kısa bir kesinti yaşayabiliyor. Kalıcı çözüm için go2rtc'nin kendi stream tanımında (bu repo dışında, `go2rtc.yaml`) kamera sesini `ffmpeg:...#audio=aac` ile önceden AAC'ye çevirmek gerekiyor.

## [4.1.1] - 2026-08-16

### Düzeltildi
- Canlı izlemedeki (MSE/fMP4) sürekli tekrarlanan go2rtc çökmesini önlemeye çalışan bir değişiklik yapıldı: `stream.mp4` isteği `mp4=all` parametresiyle go2rtc'ye PCM ailesi (PCMA/PCMU/PCM/PCML) ses codec'lerini de teklif ediyordu, go2rtc bunu fMP4 çıktısı için FLAC'a sarmaya çalışırken (`pkg/pcm.FLACEncoder`) çöküp yeniden başlıyordu — bu sırada RtcView'a bağlı tüm kameraların RTSP beslemesi birkaç saniyeliğine kesiliyordu (kayıt tarafı otomatik toparlanıyordu, ama canlı izleme daha uzun etkileniyordu). İstek `mp4=` (boş/legacy, sadece AAC) olarak değiştirildi. **Bu değişiklik 4.1.2'de geri alındı** — aşağıya bakın.

## [4.1.0] - 2026-08-16

### Eklendi
- Kayıt oynatma (Geçmiş Kayıt) ekranına ses aç/kapat kontrolü eklendi. Varsayılan olarak **kapalı** geliyor — eski kayıtları izlerken beklenmedik bir sesle karşılaşmamak için — istenirse kontrol çubuğundaki hoparlör düğmesiyle açılabiliyor. Seçim, playback paneli açık kaldığı sürece segmentler/saatler arasında geçiş yaparken korunuyor; panel her kapatılıp yeniden açıldığında tekrar kapalıya dönüyor.
- `app/VERSION` dosyası eklendi — uygulamanın sürümü için tek gerçek kaynak. `/api/status`'un `version` alanı ve Ayarlar → Sistem → Güncelleme paneli artık buradan okuyor; önceden `main.py` içinde elle güncellenmesi gereken sabit bir metin vardı ve birkaç sürümdür CHANGELOG'un gerisinde kalmıştı.

### Düzeltildi
- `/api/status`'taki uygulama sürümü, gerçek CHANGELOG sürümünden geride kalmıştı (`app/VERSION` eklenmeden önce elle senkronize edilen sabit bir metindi) — artık her zaman tek kaynaktan okunuyor.

## [4.0.2] - 2026-08-13

### Düzeltildi
- Sidebar bildirim listesi araç (vehicle) algılamalarını yanlışlıkla "Hareket algılandı" olarak gösteriyordu ve nokta rengi tanımsızdı (`.notif-dot.vehicle` CSS kuralı eksikti) — araç bildirimi artık doğru etiket ve mor renkle görünüyor.
- `POST /api/recording/settings`: geçersiz bir `storage_path` gönderildiğinde, aynı istekteki diğer alanlar (`retention_days`, `max_gb` vb.) artık kalıcı olarak kaydedilmiyor — önceden istek 400 dönerken bu alanlar sessizce zaten kaydedilmiş oluyordu, kısmi/tutarsız bir durum bırakıyordu.
- Kamera `onvif_port`/`retention_days_override` alanları artık hem `POST /api/cameras` hem `PUT /api/cameras/<id>`'de düzgün doğrulanıyor: bozuk bir değer (ör. sayısal olmayan bir metin) POST'ta önceden yakalanmamış bir hataya (500) düşüyordu, PUT'ta ise hiç dönüştürülmeden olduğu gibi kaydedilip yalnızca PTZ kullanılınca sonradan patlıyordu — ikisi de artık temiz bir 400 döndürüyor.
- `record_mode` artık `POST`/`PUT /api/cameras`'ta geçerli değerlerle (`off`/`always`/`schedule`/`manual`) sınırlandırılıyor; önceden geçersiz bir değer sessizce kabul edilip kamerayı hiç kayıt yapmaz hale getiriyordu.

### Temizlendi
- `storage.get_segment_by_path`, artık `Storage` sınıfının gerçek bir metodu — önceden `RecordingManager.__init__` içinde `storage._db`/`storage._lock`'a doğrudan erişerek monkey-patch ile ekleniyordu.
- `CameraRecorder`'ın hiçbir yerden kullanılmayan `container`/mkv desteği (config'te hiç alanı yoktu, her zaman varsayılan "mp4") kaldırıldı — ölü kod.
- `recorder.py`/`storage.py`'deki artık yanlış olan yorumlar güncellendi: çoklu-disk döneminden kalan bir yorum (v2.0.0'dan beri geçersiz) ve `detections`/`notifications` tablolarının `kind` sütununun hâlâ yalnızca `'motion' | 'person'` olduğunu söyleyen yorumlar (araç türü v3.0.0'dan beri var).

## [4.0.1] - 2026-08-13

### Düzeltildi
- Ses kaydı açık kameralarda sürekli tekrarlayan `Non-monotonic DTS` / `Queue input is backward in time` ffmpeg uyarıları giderildi. Sebep: `-use_wallclock_as_timestamps` her paketi (video + ses) gerçek varış zamanıyla damgalıyor; video stream-copy olduğu için bundan etkilenmiyor ama AAC encoder'a giden ses zaman damgaları ağ jitter'ı yüzünden ara sıra monoton olmayabiliyor. Ses akışına `-af aresample=async=1` eklendi — encoder'a ulaşmadan önce zaman damgalarını düzgün bir eksene resample ediyor, hem log gürültüsünü hem de küçük ses/görüntü kayma birikimi riskini ortadan kaldırıyor.
- Home Assistant tabanlı hareket/insan/araç algılamasının "başladı"/"durdu" satırları artık sistem logunu (journalctl) doldurmuyor — bu bilgi hâlâ kamera ayarlarındaki "Gelişmiş / Hata Ayıklama" panelinde canlı olarak görünüyor, sadece gerçek hatalar (bağlantı kopması, kimlik doğrulama hatası vb.) artık sistem loguna düşüyor.
- Servis başlatma logu artık `RtcView başladı — http://host:port adresinde dinleniyor` şeklinde, tüm bağımlı bileşenler (kayıt, Home Assistant bağlantısı, ağ izleme) hazır olduktan sonra yazılıyor — servisin gerçekten ayağa kalktığını journalctl'den net görmek için.

## [4.0.0] - 2026-08-12

### Yıkıcı değişiklik
- **Grup bildirim zamanlaması artık RtcView'da değil, Home Assistant'ta.** Her grubun eski `notify_enabled` (elle aç/kapat anahtarı) ve `notify_schedule` (gün/saat bazlı otomatik kurallar) alanları tamamen kaldırıldı; yerine tek bir `ha_notify_entity` alanı geldi — bir Home Assistant `input_boolean.*` varlığının entity_id'si (ör. `input_boolean.fabrika_bildirim`). O değişkenin o anki açık/kapalı durumu, kameraların hareket/insan/araç algılama sensörleriyle aynı paylaşılan WebSocket bağlantısı üzerinden okunur ve grubun bildirim göndermesini doğrudan belirler. Zamanlama, otomasyon veya elle açma/kapama artık tamamen Home Assistant tarafında.
- Kaldırılan dosya: `app/notify_rules.py` (eski zamanlama motoru). Grup bildirim anahtarı izleme mantığı `app/homeassistant.py`'ye taşındı (`HAManager.group_notify_active`, `HAManager.reload()`'daki ikinci watch map).
- `PUT /api/groups/<id>` artık `notify_enabled`/`notify_schedule` değil `ha_notify_entity` kabul ediyor (bir `input_boolean.*` entity_id olmalı). `GET`/`POST /api/groups` yanıtları artık her grup için canlı, hesaplanan bir `notify_active` alanı taşıyor (hiç saklanmıyor, doğrudan HA'nın anlık durumundan okunuyor).
- `GET /api/homeassistant/entities` artık `?domain=binary_sensor|input_boolean` parametresi kabul ediyor — grup bildirim anahtarı seçicisi `input_boolean.*` varlıklarını listelemek için bunu kullanıyor.
- Ayarlar → Bildirimler'deki kural editörü (gün/saat seçici, "+ Kural" listesi) tamamen kaldırıldı; yerine her grup kartında tek bir Home Assistant değişken seçici + salt-okunur durum etiketi ("Bildirimler açık" / "kapalı" / "Değişken seçilmedi") geldi. Kenar çubuğundaki grup satırındaki anahtar da artık tıklanamıyor — küçük, salt-okunur bir durum noktasına (yeşil/kırmızı/gri) dönüştü.

### Düzeltildi
- Kayıt oynatmadaki **algılanan olaylar** çekmecesi artık zaman çizelgesiyle aynı mantığı kullanıyor: aynı türden (veya çakışan farklı türden) algılamalar arasında 1 dakikadan kısa boşluk varsa tek bir olayda birleştiriliyor, ve her olay en az 1 dakikalık bir süre olarak gösteriliyor (çok kısa bir algılama artık "anlık" yerine kendi orta noktası etrafında genişletilmiş bir süreyle listeleniyor) — çekmece ve zaman çizelgesi artık her zaman aynı "bir olay" tanımında hemfikir.

## [3.0.1] - 2026-08-12

### Düzeltildi
- Kayıt oynatma zaman çizelgesindeki hareket/insan/araç şeritleri artık daha okunaklı: aynı türden algılamalar arasında 1 dakikadan kısa bir boşluk varsa tek bir şeritte birleştiriliyor, ve tek bir algılama (birleşmemiş olsa bile) çok kısa sürse dahi (ör. 5 sn) en az 1 dakikalık bir bölüm işaretliyor (algılamanın orta noktası etrafında genişletilerek). Önceden birkaç saniyelik ayrı algılamalar zoom seviyesine göre görünmeyecek kadar ince şeritler halinde çiziliyordu.

## [3.0.0] - 2026-08-12

### Yıkıcı değişiklik
- **ONVIF PullPoint tabanlı hareket/insan algılama motoru tamamen kaldırıldı, yerine Home Assistant entegrasyonu geldi.** RtcView artık kameralara ONVIF event bağlantısı kurmuyor — bunun yerine Home Assistant'taki `binary_sensor` varlıklarının durumunu tek bir kalıcı WebSocket bağlantısı üzerinden izliyor. Kaynak ne olursa olsun (Frigate, kameranın kendi entegrasyonu, herhangi bir sensör) HA'da bir `binary_sensor` olarak görünüyorsa RtcView'a bağlanabilir.
- **Araç algılama eklendi** — hareket ve insanın yanına üçüncü bir algılama türü olarak. Playback timeline'ında ayrı renk (mor), ayrı bildirim türü, ayrı canlı durum göstergesi.
- Kaldırılan dosya: `app/detection.py`. Yeni dosyalar: `app/homeassistant.py` (WebSocket bağlantısı + algılama), `app/notify_rules.py` (grup bildirim zamanlama motoru — detection.py'den bağımsız bir modüle taşındı, algılama kaynağından etkilenmez).
- Kamera şeması değişti: `motion_detection_enabled`, `person_detection_enabled`, `motion_timeout_seconds` kaldırıldı; yerine `ha_motion_entity`, `ha_person_entity`, `ha_vehicle_entity` (her biri boş veya bir `binary_sensor.*` entity_id) geldi. `onvif_host`/`onvif_port`/`onvif_user`/`onvif_pass` **korundu** — PTZ hâlâ bunları kullanıyor, algılamayla ilgisi kalmadı.
- Yeni config bölümü: `home_assistant` (`url`, `token`, `verify_ssl`) — tek bir HA örneğine bağlanmak için.
- Kaldırılan API uç noktası: `POST /api/cameras/<id>/detection/test` (ONVIF'e özgüydü). Yeni uç noktalar: `GET/POST /api/homeassistant/settings`, `POST /api/homeassistant/test`, `GET /api/homeassistant/entities`.
- Yeni bağımlılık: `websocket-client`. `onvif-zeep`/`zeep`/`lxml` **kaldırılmadı** — PTZ hâlâ ONVIF kullanıyor.
- Ayarlar arayüzünde kamera formundaki "Hareket ve İnsan Algılama (ONVIF)" bölümü, üç Home Assistant sensör seçici + durum paneline dönüştürüldü; "durdu kabul etme süresi" ayarı ve "bağlantıyı test et" (kamera bazlı) kaldırıldı — artık gerek yok, HA sensörün durumunu anlık ve kesin bildiriyor. Ayarlar → Genel'e yeni bir "Home Assistant bağlantısı" bölümü eklendi.

## [2.0.0] - 2026-08-12

### Yıkıcı değişiklik
- **Disk Yönetimi özelliği tamamen kaldırıldı.** RtcView artık disk biçimlendirme, bağlama veya ayırma yapmıyor — bir NanoPi R4S üzerinde harici bir USB diskin bağlantısı fiziksel olarak kararsız çıkması ve bunun üzerine otomatik root-bazlı temizlik mantığının duruma yanlış tepki vermesiyle ciddi bir olay yaşandıktan sonra bilinçli bir sadeleştirme kararı: tek bir diski elle (fstab ile) yönetmek, uygulamanın kendi kendine format/mount/unmount yapmasından çok daha öngörülebilir ve güvenli.
- **Depolama artık tek bir kayıt köküne (`recording.storage_path`) sadeleştirildi** — önceki çoklu-disk `storage_paths` listesi, sıralı-doldurma (`pick_write_root`) mantığı ve buna bağlı tüm per-root purge/quota kodu kaldırıldı. Mevcut `config.json`'daki eski `storage_paths` girdisi ilk açılışta otomatik olarak tek bir `storage_path`'e göçürülür (ilk kayıtlı yol kullanılır).
- Kaldırılan API uç noktaları: `GET /api/storage/devices`, `POST /api/storage/format`, `POST /api/storage/mount`, `POST /api/storage/unmount`, `GET /api/storage/job/<id>`. `POST /api/recording/settings` artık `storage_paths` (liste) değil `storage_path` (tek metin) kabul ediyor.
- Kaldırılan dosyalar: `app/diskmgr.py`, `scripts/diskmgr_worker.py`, `scripts/diskmgr_boot.py`, ve bunlara bağlı `rtcview-diskmgr.path`/`.service`/`rtcview-diskmgr-boot.service` systemd birimleri (`update.sh` mevcut kurulumlarda bunları otomatik temizler).
- Ayarlar arayüzünde "Disk Yönetimi" paneli ve çoklu-klasör ("+ Klasör ekle") listesi kaldırıldı; Kayıt & Depolama artık tek bir yol + kota alanı.

### Eklendi
- Kayıt kökü fiziksel olarak dolmaya yaklaştığında (boş alan güvenlik payının altına düştüğünde), en eski kayıtlar — global retention süresi (varsayılan 14 gün) dolmasını beklemeden — otomatik silinip yer açılıyor (tek-disk mimarisine taşınan, önceki root-bazlı önleyici temizliğin karşılığı).
- Bir segment silinirken dosya diskten fiilen kaldırılamazsa, bir sonraki temizlik turunda otomatik olarak disk yeniden taranıyor — index'te görünmeyen ama fiilen diskte duran dosyalar kendiliğinden yeniden kayda alınıp temizlenebilir hale geliyor.

### Düzeltildi
- Depolama sağlığı artık disk gerçekten doluyken bazı dosya sistemlerinin (ör. f2fs) `ENOSPC` yerine `EIO` döndürmesini de "disk dolu" durumu olarak tanıyor; silinecek eski kayıt olduğu sürece sert hata yerine normal rolling-storage davranışı gösteriyor.
- Tüm temizlik aşamaları, bir silme işlemi dosyayı fiilen serbest bırakmadığı anda o turdaki temizliği hemen durduruyor — bir kökün gerçekten silme dahi yapamayacak kadar dolu olduğu (üretimde arızalı bir USB bağlantısında gözlemlendi) durumlarda index'in tamamının boşuna boşaltılmasını önlüyor.

## [1.1.1] - 2026-08-12

### Düzeltildi
- **Kritik**: 1.1.0'daki root bazlı önleyici temizlik, bir kök gerçekten silme işlemi bile başaramayacak kadar dolu olduğunda (üretimde f2fs'te gözlemlendi — disk o kadar dolu ki dosya silmenin kendi meta veri yazması bile `EIO` ile başarısız oluyordu) her turda o kökteki **neredeyse tüm** kayıtları, hiçbir gerçek alan kazanmadan, index'ten düşürüyordu — kayıtlar diskten silinmiyordu ama uygulama artık onları göremiyordu (playback/timeline'da "kayboluyorlardı"). Tüm temizlik aşamaları (retention, kota, root bazlı, küresel acil durum) artık bir silme işlemi dosyayı fiilen serbest bırakmadığı anda o turdaki temizliği hemen durduruyor, tüm tabloyu tüketmek yerine. Etkilenen kayıtlar bir sonraki "Diski yeniden tara" ile geri kazanılabilir (dosyalar fiziksel olarak diskte duruyor, yalnızca index kaydı gitmişti).

## [1.1.0] - 2026-08-12

### Eklendi
- **Root bazlı önleyici temizlik**: bir kayıt kökü (`storage_paths` girdisi) fiziksel olarak dolmaya yaklaştığında (boş alan güvenlik payının altına düştüğünde), o kökün kendi en eski kayıtları — global retention süresi (varsayılan 14 gün) dolmasını beklemeden — otomatik silinip yer açılıyor. Önceden bu yalnızca *tüm* kökler aynı anda dolduğunda devreye giren küresel bir "acil durum" temizliğiyle sınırlıydı; artık her kök birbirinden bağımsız olarak kendi kendine yer açabiliyor.
- Bir segment silinirken dosya diskten fiilen kaldırılamazsa (geçici I/O hatası vb.), bir sonraki temizlik turunda otomatik olarak disk yeniden taranıyor — index'te görünmeyen ama fiilen diskte duran "hayalet" dosyalar kendiliğinden yeniden kayda alınıp temizlenebilir hale geliyor.

### Düzeltildi
- Depolama sağlığı (`Ayarlar → Kayıt & Depolama`) artık disk gerçekten doluyken bazı dosya sistemlerinin (ör. f2fs, boş segment kalmadığında) `ENOSPC` yerine `EIO` ("Input/output error") döndürmesini de "disk dolu" durumu olarak tanıyor; silinecek eski kayıt olduğu sürece bunu sert hata yerine normal rolling-storage davranışı olarak gösteriyor. Gerçekten alanla ilgisi olmayan bir yazma hatası (izin/donanım) hâlâ sert hata olarak kalıyor.

## [1.0.0] - 2026-08-11

İlk etiketlenmiş sürüm. Bu noktaya kadarki tüm geliştirme geçmişini kapsar.

### Özellikler
- WebRTC (WHEP) ile canlı, düşük gecikmeli çoklu kamera izleme; grid görünüm, ONVIF PTZ, PWA desteği
- Sunucu tarafında FFmpeg ile 24/7 / zamanlı / manuel segment kayıt, retention + kota bazlı otomatik temizlik, kayıt kilidi
- Tam ekran playback: 24 saatlik timeline, segment/algılama şeritleri, değişken hız, kare-kare gezinme
- ONVIF hareket & insan algılama (kamera başına ayrı ayrı açılabilir), playback timeline'ında algılama şeridi
- Grup bazlı bildirim sistemi: tek açık/kapalı anahtar + gün/saat bazlı otomatik kurallar, sidebar hızlı kontrolü
- Kamera grupları ve sidebar grup filtreleme
- Depolama & Disk Yönetimi: çoklu kayıt yolu, disk sağlığı izleme, fiziksel disk biçimlendirme (ext4/f2fs) ve fstab'sız UUID bazlı bağlama/ayırma
- Ağ İzleme: tüm fiziksel ağ arayüzleri (çoklu ethernet + wifi) için bağlantı durumu, IP/MAC, bant genişliği, kernel netlink tabanlı anlık kopma/bağlanma yakalama ve olay geçmişi
- Sistem & Bakım paneli: sistem kaynakları, kayıt bekçisi (bellek sızıntısı koruması), sıcaklık sensörü, servis/log görüntüleme
- Tek tuşla GitHub üzerinden otomatik güncelleme, servis/cihaz yeniden başlatma — hepsi tetik-dosyası + ayrı root-yetkili systemd birimi deseniyle, ana servis hiç `sudo` çalıştırmadan

[Unreleased]: https://github.com/ibrahimdoqn/RtcView/compare/v4.2.1...HEAD
[4.2.1]: https://github.com/ibrahimdoqn/RtcView/compare/v4.2.0...v4.2.1
[4.2.0]: https://github.com/ibrahimdoqn/RtcView/compare/v4.1.2...v4.2.0
[4.1.2]: https://github.com/ibrahimdoqn/RtcView/compare/v4.1.1...v4.1.2
[4.1.1]: https://github.com/ibrahimdoqn/RtcView/compare/v4.1.0...v4.1.1
[4.1.0]: https://github.com/ibrahimdoqn/RtcView/compare/v4.0.2...v4.1.0
[4.0.2]: https://github.com/ibrahimdoqn/RtcView/compare/v4.0.1...v4.0.2
[4.0.1]: https://github.com/ibrahimdoqn/RtcView/compare/v4.0.0...v4.0.1
[4.0.0]: https://github.com/ibrahimdoqn/RtcView/compare/v3.0.1...v4.0.0
[3.0.1]: https://github.com/ibrahimdoqn/RtcView/compare/v3.0.0...v3.0.1
[3.0.0]: https://github.com/ibrahimdoqn/RtcView/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/ibrahimdoqn/RtcView/compare/v1.1.1...v2.0.0
[1.1.1]: https://github.com/ibrahimdoqn/RtcView/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/ibrahimdoqn/RtcView/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/ibrahimdoqn/RtcView/releases/tag/v1.0.0
