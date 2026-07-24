# RtcView

**Mevcut** bir go2rtc'ye bağlanan, Frigate benzeri, sıfır-gecikmeli (WebRTC) kamera izleme arayüzü.
Kendi başına stream sunmaz; go2rtc'de tanımlı stream'leri WHEP üzerinden alır ve gösterir.
PWA uyumludur, Ubuntu Noble (rk3399, arm64) üzerinde izole Python venv içinde çalışır.

## Ön koşul

Ağınızda çalışan bir **go2rtc** olmalı (varsayılan API `http://127.0.0.1:1984`).
Kameralar go2rtc'nin `streams:` bölümünde tanımlı olmalı.

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

Kurulum sırasında:

- **RtcView portu** sorulur (varsayılan `5000`) ve kullanımda olup olmadığı kontrol edilir
- **go2rtc host / API portu** sorulur (varsayılan `127.0.0.1:1984`) ve bağlantı test edilir
- İzole `python3 -m venv` oluşturur (`/opt/rtcview/venv`)
- `rtcview` isimli sistem kullanıcısı yaratır
- `rtcview.service` systemd birimi ile açılışta başlatır

Sonra: `http://<makine-ip>:<port>` adresini tarayıcıda açın.

## Güncelleme

Kamera listesi ve ayarları korunur; sadece kod/asset yenilenir:

```bash
sudo bash scripts/update.sh
```

Otomatik olarak: config yedeği alır (`/tmp/rtcview-config-*.json`), servisi durdurur,
`app/`, `requirements.txt`, `scripts/` üzerine yazar, pip bağımlılıklarını günceller,
servisi yeniden başlatır. `config/`, `venv/`, `logs/` **dokunulmaz**.

## Kaldırma

```bash
sudo bash scripts/uninstall.sh
# konfigürasyonu yedeklemek için:
sudo bash scripts/uninstall.sh --keep-config
```

## Kamera Ekleme

Arayüzde **+ Kamera Ekle** ile:

- **Ad**: örn. "Ön Kapı"
- **go2rtc Stream**: go2rtc.yaml'da tanımlı stream adı (dropdown, `/api/streams` üzerinden çekilir; ↻ ile yenilenir)
- **PTZ**: ONVIF host/port/user/pass — isteğe bağlı

RtcView go2rtc yapılandırmasını **değiştirmez**; sadece mevcut stream'leri okur ve WHEP ile oynatır.
Ayarlar penceresinden go2rtc host/port'unu değiştirebilirsiniz.

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
└── logs/
```
> Not: go2rtc **bu dizinde değildir**; ağınızdaki mevcut go2rtc kullanılır.

## Servis kontrolü

```bash
systemctl status rtcview
sudo systemctl restart rtcview
journalctl -u rtcview -f
```
