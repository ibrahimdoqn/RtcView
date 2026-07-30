# RtcView Android

RtcView web arayüzünü saran, kendi sunucunuza (Tailscale veya yerel ağ üzerinden) bağlanan ince bir Android istemcisi.

## Nasıl çalışır

- **Arayüz**: Ayrı bir native ekran seti değil — telefonda bir `WebView` içinde doğrudan RtcView'in kendi web arayüzü açılır (grid, canlı izleme, playback, ayarlar — hepsi). Web tarafında yapılan her geliştirme otomatik olarak Android uygulamasına da yansır, ayrı bir bakım yükü yoktur.
- **Sunucu adresi**: İlk açılışta bir kere sorulur (`Ayarlar` menüsünden sonradan değiştirilebilir), cihazda yerel olarak saklanır.
- **Bildirimler**: Sunucuda **HTTPS olmadığı için** standart Web Push / FCM çalışmıyor. Bunun yerine uygulama arka planda **~15 dakikada bir** (Android'in `WorkManager` ile izin verdiği en sık aralık) `/api/notifications` uç noktasını kontrol eder ve yeni bildirimler için normal Android bildirimi gösterir. Bildirime dokunmak, RtcView'i doğrudan ilgili kameranın o anındaki kayda götürür.
  - Bilinçli tercih: sürekli açık bağlantı (anlık bildirim) yerine periyodik kontrol seçildi — bunun bedeli 0-15 dakikalık gecikme, karşılığında bildirim çubuğunda sabit/kapatılamaz bir "servis çalışıyor" simgesi olmuyor ve pil kullanımı çok daha düşük oluyor.

## Gereksinimler

- Telefonunuzdan sunucuya **düz HTTP** ile erişebiliyor olmanız (Tailscale IP/hostname veya yerel ağ). Uygulama `usesCleartextTraffic` ile buna izin verir.
- Sunucu tarafında herhangi bir ek kurulum **gerekmez** — mevcut RtcView API'lerini kullanır.

## Derleme

Bu proje **Android Studio ile açılıp derlenmek üzere tasarlandı** — en kolay ve önerilen yol budur:

1. Android Studio'da **File → Open** ile bu `android/` klasörünü açın.
2. Android Studio, Gradle wrapper'ı (jar dosyası bu repoya dahil edilmedi — ikili/binary dosya olduğu için) ilk açılışta otomatik indirip kuracaktır. Gradle senkronizasyonunun bitmesini bekleyin.
3. Bir cihaz/emülatör seçip **Run ▶** ile kurun, ya da **Build → Generate Signed Bundle / APK** ile bir APK üretin.

### Komut satırından (Gradle kuruluysa)

```bash
cd android
gradle wrapper --gradle-version 8.7   # bir kerelik: wrapper jar'ını oluşturur
./gradlew assembleDebug
# APK: app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

> **Not:** Bu proje bu ortamda (Android SDK/emülatör bulunmayan bir sandbox) derlenip test **edilemedi**. Kod standart, iyi bilinen AndroidX API'leri kullanıyor ve mimarisi basit tutuldu, ama ilk derlemede bir hata çıkarsa (ör. bir bağımlılık sürümü güncel Maven'de bulunamazsa) hata mesajını paylaşın, birlikte düzeltelim.

## Proje yapısı

```
android/
  app/src/main/java/com/rtcview/app/
    SetupActivity.kt        — sunucu adresi girme ekranı (ilk açılış / "Sunucu değiştir")
    MainActivity.kt         — WebView sarmalayıcı (autoplay, fullscreen, indirme, geri tuşu)
    NotificationScheduler.kt — WorkManager periyodik iş kaydı
    NotificationWorker.kt    — arka plan bildirim kontrolü + native bildirim gösterimi
    Prefs.kt                 — SharedPreferences (sunucu adresi, son görülen bildirim id'si)
    NetUtils.kt               — küçük HTTP yardımcıları (harici kütüphane yok)
```

## Bilinçli sınırlamalar

- Bildirimler gerçek zamanlı değil, ~15 dakikaya kadar gecikebilir (yukarıda açıklandı).
- Uygulama kendi kamera/mikrofon erişimi istemez — go2rtc zaten sunucu tarafında yayını sağlıyor, telefonun donanımına ihtiyaç yok.
- Sunucu adresi cihaz yedeklerine dahil edilmez (farklı bir cihaz/ağda anlamsız olurdu) — yeni cihazda tekrar girilmesi gerekir.
