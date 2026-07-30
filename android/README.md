# RtcView Android

RtcView web arayüzünü saran, kendi sunucunuza (Tailscale veya yerel ağ üzerinden) bağlanan ince bir Android istemcisi.

## Nasıl çalışır

- **Arayüz**: Ayrı bir native ekran seti değil — telefonda tam ekran bir `WebView` içinde doğrudan RtcView'in kendi web arayüzü açılır (grid, canlı izleme, playback, ayarlar — hepsi). Web tarafında yapılan her geliştirme otomatik olarak Android uygulamasına da yansır, ayrı bir bakım yükü yoktur. Native bir Toolbar yok — web arayüzünün kendi hamburger menüsü zaten var; yenile/sunucu değiştir gibi iki native aksiyon sağ üstteki küçük cam butonda.
- **Sunucu adresi**: İlk açılışta bir kere sorulur (uygulama içi menüden sonradan değiştirilebilir), cihazda yerel olarak saklanır. Bağlanılamazsa (yanlış adres, sunucu kapalı) otomatik olarak bu ekrana geri döner.
- **Bildirimler yok — sadece izleme**: Uygulama arka planda hiçbir şey kontrol etmez, bildirim göndermez. Bunun sebebi denenip vazgeçildi: sunucuda HTTPS olmadığı için standart Web Push/FCM zaten mümkün değildi; alternatif olarak denenen `WorkManager` tabanlı periyodik kontrol de Android'in izin verdiği en sık aralık olan ~15 dakikada bir çalışabiliyordu, bu da bildirimleri pratikte işe yaramayacak kadar geç bırakıyordu. Anlık bildirim isteyen kullanıcılar web arayüzünün kendi bildirim zilini (tarayıcıdan açık tutarak) kullanabilir.

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

## Proje yapısı

```
android/
  app/src/main/java/com/rtcview/app/
    SetupActivity.kt   — sunucu adresi girme ekranı (ilk açılış / "Sunucu değiştir" / bağlanılamazsa)
    MainActivity.kt    — tam ekran WebView sarmalayıcı (autoplay, fullscreen, indirme, geri tuşu)
    Prefs.kt           — SharedPreferences (yalnızca sunucu adresi)
    NetUtils.kt        — küçük HTTP yardımcıları (harici kütüphane yok)
```

## Bilinçli sınırlamalar

- Bildirim yok, sadece izleme — yukarıda açıklandı.
- Uygulama kendi kamera/mikrofon erişimi istemez — go2rtc zaten sunucu tarafında yayını sağlıyor, telefonun donanımına ihtiyaç yok.
- Sunucu adresi cihaz yedeklerine dahil edilmez (farklı bir cihaz/ağda anlamsız olurdu) — yeni cihazda tekrar girilmesi gerekir.
