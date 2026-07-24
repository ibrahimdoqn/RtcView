# RtcView

go2rtc tabanlı, Frigate benzeri, sıfır-gecikmeli (WebRTC) kamera izleme arayüzü.
PWA uyumludur, Ubuntu Noble (rk3399, arm64) üzerinde izole Python venv içinde çalışır.

## Özellikler

- **WebRTC (WHEP)** ile canlı, düşük gecikmeli izleme (go2rtc üzerinden)
- **PWA** — telefon/tabletten "Ana Ekrana Ekle", çevrimdışı kabuk
- **Grid** görünüm (1–8 sütun, ayarlardan seçilir)
- **Kamera seçimi**: sol menüden veya tile'a tıklayarak; seçili tile mavi çerçeve + büyütülmüş beyaz halkalı ad/rozet
- **Çift tık** ile tam ekran solo, tekrar çift tık / `Esc` / **Tüm Izgara** ile geri
- **Sürükle-bırak** ile sol menüde yeniden sıralama (yuvarlak köşeli, gölgeli önizleme)
- **Mouse tekerleği zoom** — imleç merkezli, sürükleyerek pan, sağ tık sıfırlar
- **PTZ** kontrolleri (ONVIF): 8 yön, zoom, presetler; seçili kameraya bağlı
- Otomatik yeniden bağlanma (üstel gecikme), durum rozetleri, arama, tema (koyu/açık)
- Tüm ayarlar tek dosyada: `/opt/rtcview/config/config.json`

## Kurulum

Depoyu Ubuntu Noble (rk3399, arm64) makineye kopyalayın ve:

```bash
sudo bash scripts/install.sh
```

Kurulum sırasında port sorulur (varsayılan `5000`) ve kullanımda olup olmadığı kontrol edilir.
Kurulum:

- İzole `python3 -m venv` oluşturur (`/opt/rtcview/venv`)
- `go2rtc` binary'sini indirir (`/opt/rtcview/go2rtc`)
- `rtcview` isimli sistem kullanıcısı yaratır
- `rtcview.service` systemd birimi ile açılışta başlatır

Sonra: `http://<makine-ip>:<port>` adresini tarayıcıda açın.

## Kaldırma

```bash
sudo bash scripts/uninstall.sh
# konfigürasyonu yedeklemek için:
sudo bash scripts/uninstall.sh --keep-config
```

## Kamera Ekleme

Arayüzde **+ Kamera Ekle** ile:

- **Ad**: örn. "Ön Kapı"
- **Stream URL**: `rtsp://user:pass@192.168.1.10:554/stream1` (rtsp/rtsps/http/hls kabul edilir)
- **PTZ**: ONVIF host/port/user/pass — isteğe bağlı

RtcView, go2rtc `go2rtc.yaml` dosyasını otomatik günceller ve go2rtc'yi yeniden yükler.

## Klavye kısayolları

- `Esc` — solo tam ekrandan çık
- Çift tık — tile'ı tam ekran/geri
- Sağ tık — zoom'u sıfırla
- Mouse tekerleği — imleç merkezli zoom

## Dosya düzeni

```
/opt/rtcview/
├── app/            # Flask kaynak
├── venv/           # izole Python ortamı
├── config/config.json
├── go2rtc          # binary
├── go2rtc.yaml     # RtcView tarafından otomatik yazılır
└── logs/
```

## Servis kontrolü

```bash
systemctl status rtcview
sudo systemctl restart rtcview
journalctl -u rtcview -f
```
