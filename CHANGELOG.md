# Changelog

Bu dosya RtcView'daki sürüm bazlı değişiklikleri listeler.
Biçim [Keep a Changelog](https://keepachangelog.com/) temel alınır, sürümleme [Semantic Versioning](https://semver.org/) izler (`MAJOR.MINOR.PATCH`).

## [Unreleased]

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

[Unreleased]: https://github.com/ibrahimdoqn/RtcView/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/ibrahimdoqn/RtcView/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/ibrahimdoqn/RtcView/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/ibrahimdoqn/RtcView/releases/tag/v1.0.0
