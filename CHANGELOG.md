# Changelog

Bu dosya RtcView'daki sürüm bazlı değişiklikleri listeler.
Biçim [Keep a Changelog](https://keepachangelog.com/) temel alınır, sürümleme [Semantic Versioning](https://semver.org/) izler (`MAJOR.MINOR.PATCH`).

## [Unreleased]

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

[Unreleased]: https://github.com/ibrahimdoqn/RtcView/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ibrahimdoqn/RtcView/releases/tag/v1.0.0
