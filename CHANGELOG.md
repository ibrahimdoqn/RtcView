# Changelog

Bu dosya RtcView'daki sürüm bazlı değişiklikleri listeler.
Biçim [Keep a Changelog](https://keepachangelog.com/) temel alınır, sürümleme [Semantic Versioning](https://semver.org/) izler (`MAJOR.MINOR.PATCH`).

## [Unreleased]

## [4.9.0] - 2026-08-21

### Değiştirildi
- **RAM'de segment oluşturma (tmpfs staging) artık zorunlu mimari, isteğe bağlı bir ayar değil.** Ayarlar → Kayıt & Depolama'daki açma/kapama kutusu kaldırıldı; ffmpeg artık her zaman önce `/tmp` (tmpfs) üzerine yazar, segment kapanınca sıradaki uygun diske taşınır. `/tmp` gerçekten tmpfs değilse veya o an yeterli boş RAM yoksa (yapılandırılabilir eşikler aynen duruyor: gerekli boş RAM / kamera başına RAM sınırı), yalnızca **o oturum için** doğrudan diske yazmaya dönülür — bir sonraki başlangıçta RAM'e geçiş yeniden denenir, kalıcı bir düşüş değildir.
- **Kapanan her segmentin hedef diski artık taşınma anında, her seferinde yeniden hesaplanıyor** (önceden kayıt başlarken bir kez seçilip sabitleniyordu). Bu, gün değişse veya en uygun disk segment kaydı sırasında değişse bile hiçbir yeniden başlatmaya gerek kalmadan doğru yere yazılmasını sağlıyor — canlı ffmpeg süreci yalnızca RAM'e bağımlı olduğundan, hangi fiziksel diskin şu an en uygun olduğu onu hiç etkilemiyor.
- **Kayıt artık hiçbir depolama sebebiyle yeniden başlatılmıyor.** Önceden "RAM'deki birikinti üst sınırı aştı" ve "gün değişti" durumları kamerayı yeniden başlatıyordu (birkaç saniyelik kesinti); ikisi de artık restart gerektirmiyor: gün değişimi zaten yukarıdaki dinamik hedef hesaplamasıyla kendiliğinden çözülüyor, RAM üst sınırı aşıldığında ise (yalnızca tüm diskler gerçekten dolu/erişilemezse olur) RAM'deki en eski taşınmamış segment silinip kayıt kesintisiz sürdürülüyor (veri kaybı kabul edilir, açıkça loglanır). Bellek sızıntısı ve donmuş akış bekçileri (ilgisiz, süreç/akış sağlığını korumaya yönelik) değişmeden duruyor.

### Kaldırıldı
- `recording.tmpfs_staging` ayarı ve arayüzdeki açma/kapama kutusu — yukarıdaki değişiklik nedeniyle artık anlamsız. RAM eşik ayarları (gerekli boş RAM / kamera başına RAM sınırı) aynen kalıyor ve artık her zaman görünür/etkin.

### Test
- 28 kontrollü yeni bir test paketiyle doğrulandı: bir segmentin RAM'den önce disk A'ya, sonra (disk A "dolunca") disk B'ye — hiç yeniden başlatma olmadan — taşındığı; zaten kayıtlı bir segmentin tekrar işlenmesinin güvenle no-op olduğu; taşıma başarısız olduğunda dosyanın yerinde kalıp bir sonraki denemeye bırakıldığı; RAM üst sınırı aşıldığında en eskinin silinip en yeninin asla silinmediği; gün değişiminin RAM'de yazarken hiç tetiklenmediği ama yedek doğrudan-diske modunda hâlâ çalıştığı; izleyici döngüsünün sıkışan bir taşımayı normal bekleme süresini beklemeden her tikte yeniden denediği.

## [4.8.1] - 2026-08-20

### Düzeltildi
- **Disk ekleme kutusu artık koyu/açık tema ile uyumlu.** Girdi kutusu bir `<label>` içinde olmadığı için hiç tema stilini almıyordu, tarayıcı varsayılanı beyaz arka planla görünüyordu. Artık üstteki "1 /mnt/rec Kaldır" kartıyla aynı görsel dil: tek, yuvarlak köşeli, tema renklerini kullanan bir kutu; "+ Ekle" butonu ayrı bir kontrol olarak yanda değil, aynı kutunun içinde sağda duruyor.
- **tmpfs (RAM'de segment oluşturma) ile çoklu disk arasındaki gerçek bir etkileşim boşluğu kapatıldı.** RAM'den diske taşıma işlemi de bir disk yazımıdır ve dolu diskte aynı şekilde başarısız olabilir — ama bu taşıma denemesinden ÖNCE değil, sadece SONRA yer açma kontrolü çalışıyordu, yani taşıma anında disk zaten doluysa hiç şans verilmeden başarısız oluyordu. Ayrıca (daha derin bir sorun): tmpfs açıkken canlı ffmpeg yazımı RAM'e gittiği için disk dolması ffmpeg'i hiç çökertmiyor — bu da normalde "3 saniyede bir yeniden başlat, doğru diski yeniden seç" kurtarma yolunu (disk dolu → ffmpeg çöker → supervisor yeniden başlatır) tamamen devre dışı bırakıyordu; tek kurtuluş RAM'deki birikinti `tmpfs_hard_cap_mb`'ı aşana kadar (varsayılan 512 MB) beklemekti. Artık: (1) taşıma denemesinden önce de yer açma kontrolü çalışıyor, (2) bir dosya RAM'de sıkışıp kaldığında ve BAŞKA bir disk müsaitse, kamera bu diski kullanmak üzere hemen (RAM üst sınırını beklemeden) yeniden başlatılıyor.
- 15 kontrollü bir testle doğrulandı: taşıma öncesi yer açma kontrolünün gerçekten çalıştığı, sıkışma durumunun doğru işaretlenip temizlendiği, ve yeniden başlatma sinyalinin yalnızca gerçekten farklı ve müsait bir disk varken tetiklendiği.

## [4.8.0] - 2026-08-20

### Kaldırıldı
- **Kota (toplam boyut sınırı) özelliği arayüzden kaldırıldı.** Ayarlar → Kayıt & Depolama'daki "Kota (GB)" alanı ve disk kullanım çubuğundaki kota satırı gitti; `POST /api/recording/settings` artık `max_gb` alanını kabul etmiyor. Tek depolama sınırı artık retention (gün) + düşük disk marjı. Daha önce kota ayarlamış olan kurulumlarda değer sessizce etkisiz kalır (config'te durur, hiçbir şey silmez).

### Değiştirildi
- **Kayıt & Depolama sekmesindeki uzun açıklama metinleri kısaltıldı** (en fazla ~2 satır); tam ayrıntılar README'nin "Depolama" ve "Kayıt yolu değiştirme / birden fazla disk" bölümlerine taşındı.
- **Disk ekleme kutusu mobilde artık taşmıyor** — çok uzun olan yer tutucu metni kısaltıldı ve satır, dar ekranlarda giriş kutusu ile butonun düzgün sarmalanacağı (flex-wrap) şekilde yeniden düzenlendi.

## [4.7.1] - 2026-08-20

### Düzeltildi
- **Eşzamanlı çağrılarda gereğinden fazla silme riski giderildi.** `free_up_for_new_segment()`'in "yer var mı kontrol et, yoksa N sil" akışı hiçbir kilit altında değildi — birden fazla kamera segmenti neredeyse aynı anda kapanıp bu fonksiyonu tetiklediğinde (veya `purge_once()`'un periyodik çağrısıyla çakıştığında), her çağrı bağımsız olarak "yer yok" görüp kendi N'ini silebiliyordu; N yerine N×(eşzamanlı çağrı sayısı) kadar segment gidebiliyordu. Artık tüm "kontrol et → sil" akışı tek bir kilit altında — bir çağrı beklerken diğeri işini bitirirse, bekleyen çağrı yeri zaten müsait bulup hiçbir şey silmeden döner.
- **Arızalı bir disk, çok sayıda eski kayıt tutuyorsa sağlıklı diskin temizliğini yine tıkayabiliyordu.** Aday listesini tek seferde (N'in birkaç katı, üst sınır 200) çekme yöntemi, arızalı diskin kendi eski kayıtları bu pencereyi tamamen doldurursa (ki sıralı doldurma modelinde önce dolan disk genelde çok daha fazla eski kayıt tutar), sağlıklı diskin adaylarına o pencerede hiç sıra gelmemesine yol açabiliyordu. Artık adaylar tek tek, arızalı disk keşfedildikçe sorgudan tamamen dışlanarak (`NOT LIKE`) çekiliyor — arızalı diskteki eski kayıt sayısı ne kadar fazla olursa olsun, sağlıklı diskin adayları asla erişilemez hale gelmiyor.
- Kapsamlı bir senaryo testiyle (21 kontrol) doğrulandı: eşzamanlı çağrılarda tam olarak ihtiyaç kadar silindiği, kilitli kayıtların hiçbir zaman dokunulmadığı, tamamen çıkmaz sokakta (silinecek kilitsiz kayıt yok) çökmeden düzgün durduğu, ve arızalı diskin — kaç eski kaydı olursa olsun — sağlıklı diskin temizliğini artık hiç engellemediği.

## [4.7.0] - 2026-08-20

### Düzeltildi
- **Arızalı bir disk artık diğer diskin temizliğini tıkayamıyor.** `free_up_for_new_segment()`, ilk silme hatasında tüm geçişi durduruyordu — global en-eski sıralamasında kalıcı olarak arızalı bir disk her zaman önde olacağından, bu diğer (sağlıklı) diskin segmentlerine hiç sıra gelmemesine yol açabiliyordu (orijinal olayın hafif bir versiyonu). Artık bir diskteki silme başarısız olunca sadece o disk bu geçiş için atlanıyor (tekrar denenmiyor), diğer disklerin adayları işlenmeye devam ediyor.
- **N artık sadece aktif kayıt yapan kamera sayısını sayıyor**, toplam yapılandırılı kamera sayısını değil — `record_mode: off` olan kameralar artık silinecek segment sayısını gereksiz yere şişirmiyor.
- **Disk-doluluk temizliği artık kayıt kapalıyken de çalışıyor.** Önceden sadece segment başlangıcında tetikleniyordu — kayıt tamamen durdurulmuşsa (bakım, tüm kameralar kapalı) disk başka bir sebeple dolarsa hiçbir şey tepki vermiyordu. `purge_once()` artık periyodik turunda da `free_up_for_new_segment()`'i çağırıyor, böylece her durumda bir güvenlik ağı var.

### Eklendi
- **Anlık görüntüler (snapshot) artık sıralı doldurmaya dahil.** Önceden her zaman ilk (primary) kayıt köküne yazılıyordu — o dolsa bile. Artık `pick_snapshot_root()` segment yazımıyla aynı politikayı izliyor: hangi disk müsaitse oraya yazılır. Her disk kendi `_snapshots` alt klasörüne sahip olabilir; `rescan()` artık bunların hepsini tanıyıp segment sanıp indexlemekten kaçınıyor (önceden sadece primary'nin `_snapshots`'ını biliyordu).
- **Anlık görüntü çekmek de artık disk-doluluk temizliğini tetikliyor** (`POST /api/snapshot/<id>`), tıpkı yeni bir segment başlangıcı gibi.

## [4.6.1] - 2026-08-20

### Kaldırıldı
- **"Şimdi temizle" butonu kaldırıldı** (Ayarlar → Kayıt & Depolama), beraberindeki `POST /api/recording/purge` route'u ile birlikte. 4.6.0'dan sonra disk-doluluk tabanlı silme zaten her segment başlangıcında otomatik çalıştığı için (`free_up_for_new_segment()`), manuel tetikleme artık hiçbir şey kazandırmıyordu.

## [4.6.0] - 2026-08-20

### Değiştirildi
- **Disk-doluluk tabanlı silme mekanizması baştan aşağı basitleştirildi.** Önceki tasarım periyodik bir arka plan turunda (varsayılan 60 sn'de bir) çalışan, "düşük disklerin kümesi" (`still_low`), "bu turluk arızalı disk karantinası" (`broken_roots`) ve tek-tek-sil-kontrol-et döngüsünden oluşan, hem reclaim hem ayrı bir "acil global temizlik" fazı içeren çok katmanlı bir yapıydı. Artık:
  - Yaş (retention_days) ve kota (max_gb) tabanlı temizlik **aynen kalıyor** — hâlâ `purge_once()` içinde, periyodik arka plan turunda çalışıyor.
  - Disk-doluluk tabanlı silme artık **periyodik değil, olay tetiklemeli**: her yeni segment başlamadan hemen önce (`storage.free_up_for_new_segment()`, `recorder.py`'den çağrılır) çalışır. Yapılandırılmış klasörlerin **hepsi** aynı anda düşük disk marjının altındaysa (tek bir klasörde bile yer varsa hiçbir şey silinmez — sıralı doldurma zaten oraya geçer), en eski **N** kilitsiz segment silinir; **N = o an yapılandırılı kamera sayısı**, hangi diskten/kameradan oldukları önemsenmeden global en-eskiden başlanarak. Sabit ve öngörülebilir bir miktar — "marj temizlenene kadar sil" döngüsü yerine; yetmediyse bir sonraki segment başlangıcında tekrar çalışır.
  - `still_low`/`broken_roots`/per-root LIKE sorgu scoping'i tamamen kaldırıldı — tek disk arızasının diğerini etkilememesi artık basitçe "bir silme başarısız olursa bu geçiş hemen durur" kuralıyla sağlanıyor (aynı `_delete_segment_impl`'in kendi unlink sonucunu döndürmesi mekanizması).
  - Manuel "Şimdi temizle" butonu (`POST /api/recording/purge`) artık hem `purge_once()`'u hem `free_up_for_new_segment()`'i çağırıp sonuçları birleştiriyor — davranışı kullanıcı için değişmedi.
  - Yeni testlerle doğrulandı: en az bir disk müsaitken hiçbir şey silinmediği, tüm diskler doluyken tam olarak kamera-sayısı-kadar segmentin (hangi diskten olduğuna bakılmaksızın global en-eskiden) silindiği, bir silme hatasında geçişin hemen durduğu, ve `purge_once()`'un artık marj tabanlı silme yapmadığı (sadece retention/kota).

## [4.5.0] - 2026-08-20

### Eklendi
- **Düşük disk marjı artık ayarlanabilir** (Ayarlar → Kayıt & Depolama → "Düşük disk marjı (MB)", varsayılan 1024 MB). Daha önce sabit 1 GB'lık bir sabitti — 128 GB'lık bir SD kartla 8 TB'lık bir dizi için aynı sayının anlamı aynı değil. `recording.low_space_margin_mb` config alanı; geçersiz/0/negatif bir değer sessizce varsayılana düşer (marjı tamamen kapatmak güvenli değil — ffmpeg'in bir segmenti diske ENOSPC'ye çarpmadan yazabilmesi için hâlâ biraz boşluğa ihtiyacı var).

### Değiştirildi
- **Yer açma (reclaim) ve acil durum temizliği artık gerçekten ihtiyaç kadar siliyor, sabit bir grup halinde değil.** Önceki tasarım her turda 20 (reclaim) veya 5 (acil temizlik) segmenti toptan silip sonra kontrol ediyordu — margin'i temizlemek için 1-2 segment yeterliyken bile 20'sinin tamamı gidiyordu, gereksiz yere görüntü kaybına yol açıyordu. Artık tek seferde **bir** segment siliniyor, hemen ardından o diskin boş alanı yeniden ölçülüyor, marjı temizlediği an o disk için durulup diğer düşük diske geçiliyor — silinen segment sayısı her zaman gerçekte gereken minimum sayı kadar.

### Düzeltildi
- **Çoklu disk reclaim'inde diskler-arası izolasyonu bozan gizli bir hata bulundu ve giderildi.** `_orphan_suspected` bayrağı örnek (instance) düzeyinde ve o turun sonuna kadar "yapışkan" tutulacak şekilde tasarlanmıştı (bir sonraki turun otomatik yeniden-taramasını tetiklemek için — bu kısım doğru). Ama reclaim döngüsü bu PAYLAŞILAN bayrağı "bu silme başarısız oldu mu" sinyali olarak okuyordu — yani A diskinde BİR silme başarısız olduğunda, aynı turda B diskinde (tamamen sağlıklı, gerçekten başarılı) yapılan silmeler de yanlışlıkla "başarısız" sayılıp B diski de gereksiz yere karantinaya alınıyordu. Bu, tam olarak 4.4.1'in koruduğunu iddia ettiği "bir diskin arızası diğerini asla etkilemez" garantisinin ihlaliydi. Artık her silme çağrısı kendi başarı/başarısızlık durumunu döndürüyor (`_delete_segment_impl`), paylaşılan bayrağa bakmıyor — bir diskin arızası artık gerçekten sadece o diski etkiliyor.
  - Yeni testlerle doğrulandı: tek düşük disk senaryosunda tam ihtiyaç kadar (1) segment silindiği, iki düşük diskte her birinin kendi minimum ihtiyacı kadar (1 ve 2) silindiği ve global en-eski-önce sıralamasının korunduğu, bir diskin silme hatasının diğer diskin kendi minimal temizliğini hiç etkilemediği, ve acil durum temizliğinin de tek tek işlediği.

## [4.4.1] - 2026-08-20

### Düzeltildi
- **Çoklu disk reclaim'i artık gerçekten en eski kaydı siliyor, her diskin kendi en eskisini değil.** 4.4.0'daki ilk sürüm, iki disk dolduğunda her diski bağımsız değerlendirip **o diskin kendi** en eski kayıtlarını siliyordu — sıralı doldurma modelinde ilk dolan disk (A) en eski görüntüleri tutarken, sonradan devreye giren disk (B) daha yeni görüntüler biriktirir; B dolduğunda B'nin kendi (nispeten yeni) kayıtları siliniyor, A'daki çok daha eski kayıtlar hiç dokunulmadan duruyordu — bir "rolling buffer"ın vermesi gereken "önce en eski gitsin" garantisinin tam tersi. Artık aynı anda dolu olan diskler arasında **gerçekten en eski segment önce siliniyor** — hangi diskte olursa olsun. Dolu olmayan (sağlıklı) bir disk hiçbir zaman dokunulmuyor, sadece gerçekten yer sıkıntısı çeken diskler arasında en eski öncelik kazanıyor (bir diskteki dosyayı silmek başka bir diskte yer açmıyor çünkü fiziksel olarak ayrı disklerdir).
  - Bu değişiklik yapılırken orijinal olayın (bkz. 4.4.0) düzeltmesi olan **disk başına izolasyon** korundu: bir diskin silme hatası (arıza, aralıklı I/O hatası) o diski o tur için karantinaya alıyor — o diskin segmentleri atlanıyor, tekrar denenmiyor — ama diğer dolu disklerin kendi temizliği hiç etkilenmiyor.
  - Kapsamlı testlerle doğrulandı: iki disk de doluyken gerçekten en eski segmentin önce silindiği, sağlıklı bir diskin (daha eski kayıt tutsa bile) hiç dokunulmadığı, ve bir diskin arızasının diğer dolu diski engellemediği.

## [4.4.0] - 2026-08-20

### Eklendi
- **Çoklu disk desteği yeniden eklendi — bu kez otomatik disk yönetimi olmadan.** Ayarlar → Kayıt & Depolama'da artık tek bir "Kayıt klasörü" yerine, birden fazla klasör ekleyip çıkarabileceğiniz bir liste var. Birden fazla klasör eklerseniz kayıt **sırayla doldurulur**: ilk klasör dolana kadar (güvenlik payı altına düşene kadar) ona yazılır, dolunca listedeki bir sonraki klasör devreye girer.
  - **Bu, daha önce bu uygulamadan tamamen kaldırılmış bir özelliğin geri getirilmesi — geçmişteki hatayı tekrarlamadan.** Önceki çoklu-disk sürümü RtcView'ın kendisine disk biçimlendirme/bağlama/ayırma yetkisi veriyordu; harici bir USB diskin donanımsal arızaya girmesi, uygulamanın buna otomatik tepki veren temizlik mantığıyla birleşince ciddi bir üretim olayına yol açmış ve bu yüzden özellik tamamen kaldırılıp tek-disk mimarisine geçilmişti. Bu sürüm o dersi koruyor: **RtcView hâlâ hiçbir diski kendisi biçimlendirmez/bağlamaz/ayırmaz** — her klasörü siz (fstab ile) mount etmiş olmalısınız, RtcView sadece zaten bağlı yollara sırayla yazar. Kota (varsayılan sınırsız) tüm disklerin toplamı üzerinden global olarak değerlendirilir, disk başına ayrı kota yok (basitlik için bilinçli bir tercih).
  - **Asıl güvenlik düzeltmesi — disk başına izole temizlik.** Bir diskin dolmaya yaklaşması, o diskin kendi eski kayıtlarını silme mantığını (rolling-buffer reclaim) tetikler; bu tamamen **o diske özel ve izole** çalışır. Bir diskteki silme işlemi başarısız olursa (ör. donanımsal arıza, aralıklı I/O hatası) — orijinal olaya yol açan tam senaryo — o diskin temizlik fazı **anında durur**, ne o diskin tüm indeksini boşaltmaya devam eder ne de başka bir diskin durumunu etkiler. Diğer disklerin kendi temizlik/kurtarma döngüsü tamamen bağımsız işler. Her disk için ayrı sağlık durumu (`health()`) raporlanır — bir disk bozuk/dolu olsa bile diğerleri çalışmaya devam ettiği sürece kayıt "bozuk" olarak görünmez.
  - Kapsamlı bir test paketiyle doğrulandı: sırayla doldurma, tüm disklerin aynı anda dolması (acil durum temizliği), bir diskin yazılamaz hale gelmesi (izin hatası/bağlantı kopması) ve toparlanması, **bir diskin silme hatalarının diğer diskleri hiç etkilememesi** (orijinal olayın tam senaryosu — 46 kontrolün tamamı geçti), `rescan()`'ın tüm diskleri gezmesi, ve eski tek-disk `storage_path` ayarının yeni listeye sorunsuz göçürülmesi (kurulum betikleri dahil — `install.sh`/`update.sh` artık mevcut bir çoklu-disk yapılandırmasını asla geri tek diske indirgemiyor).

## [4.3.6] - 2026-08-20

### Düzeltildi
- **Mobilde sekmeler arası geçişten dönünce "Depolama yazılamıyor" gibi sahte hatalar görünmesi giderildi.** Kök neden: `storage.health()`'in yazma testi, her çağrıda aynı sabit dosya adını (`.rtcview_write_test`) kullanıyordu — eşzamanlı iki çağrı bu dosyayı aynı anda yazıp silmeye çalışınca, biri diğerinin sildiği dosyayı silmeye çalışıp `FileNotFoundError` alıyor ve bu, gerçek bir disk sorunu yokken "Yazılamıyor: ..." hatası olarak kullanıcıya gösteriliyordu. Mobil tarayıcıda sekme arka plana alındığında Ayarlar sayfasındaki periyodik yenileme zamanlayıcıları (Kayıt & Depolama'nın 1sn'lik, Sistem Kaynakları'nın 2sn'lik) durmuyor, sadece işletim sistemi tarafından erteleniyordu — sekmeye geri dönüldüğünde bunlar art arda birikmiş halde neredeyse aynı anda ateşleniyor, bu da tam olarak bu yarış durumunu tetikliyordu. İki parçalı düzeltme: (1) yazma testi artık her çağrıda benzersiz bir dosya adı kullanıyor (süreç+iş parçacığı kimliğine göre), eşzamanlı çağrılar birbirini asla etkilemiyor; (2) bu zamanlayıcılar artık sekme arka plandayken hiç çalışmıyor (bildirim zilinin zaten kullandığı desenle aynı), sekme tekrar görünür olduğunda ise ilgili panel bir kerelik anında yenileniyor — hem birikmiş patlama önleniyor hem de eski/donmuş durum ekranda kalmıyor. Gerçek eşzamanlılık testiyle doğrulandı: eski kod 200 eşzamanlı çağrıdan 90'ında sahte hata veriyordu, düzeltmeyle 0.

## [4.3.5] - 2026-08-20

### Değişti
- **4.3.4'teki açılıp-kapanır açıklama metinleri geri alındı** — kullanıcı geri bildirimi üzerine. Ayarlar sayfasındaki tüm açıklama metinleri artık tekrar her zaman görünür (ⓘ düğmesi yok, tıklamaya gerek yok).
- Bunun yerine **gerçekten uzun olan açıklama metinleri sadeleştirildi** (Home Assistant bağlantısı, Bildirimler girişi, tmpfs staging, ffmpeg bellek sınırı, sıcaklık sensörü yolu, yeniden başlatma notu) — aynı bilgiyi daha kısa cümlelerle veriyor, kısa olanlar (tek satırlık ipuçları) değiştirilmedi.

## [4.3.4] - 2026-08-20

### Eklendi
- **Ayarlar → Kayıt & Depolama'daki tmpfs kullanım çubuğu artık canlı** — bu sekme ekrandayken 1 saniyede bir yenileniyor, sekmeden çıkılınca veya Ayarlar kapatılınca otomatik duruyor (Sistem sekmesinin auto-refresh'iyle aynı desen, gereksiz arka plan isteği bırakmıyor).
- **Ayarlar sayfasındaki tüm açıklama metinleri artık açılıp kapanabilir.** Önceden her sekmede (Genel, Kameralar, Bildirimler, Kayıt & Depolama, go2rtc, Sistem) sabit duran yardım paragrafları UI'yi kalabalıklaştırıyordu. Artık varsayılan olarak gizli; yanlarındaki küçük ⓘ düğmesiyle istendiğinde açılıp kapanıyor. Tek bir genel mekanizma (`small.info-text` + tek bir delege edilmiş tıklama işleyicisi) — 15 ayrı açıklama bloğunun her biri ayrı ayrı kodlanmadı, ileride eklenecek yeni bir açıklama da otomatik aynı davranışı alıyor. Gerçek tarayıcı (Playwright) testiyle doğrulandı.

## [4.3.3] - 2026-08-20

### Değişti
- **tmpfs staging artık `/tmp`'in gerçekten RAM (tmpfs) olduğunu kendisi doğruluyor, varsayımda bulunmuyor.** Önceden bu kontrol yalnızca bilgi amaçlı loglanıyordu — `/tmp` normal bir dizinse (aynı kayıt diskinin parçasıysa) özellik yine de "açık" görünüyor ama hiçbir fayda sağlamıyordu, sessizce. Artık bir kameranın oturumu, `/tmp` gerçekten tmpfs olarak doğrulanamadıkça staging'i hiç etkinleştirmiyor — doğrulanamazsa o oturum boyunca doğrudan kayıt diskine yazmaya devam ediyor (aynı, zaten var olan sessiz geri düşüş yolu).
- **Ayarlar → Kayıt & Depolama'da tmpfs alanının anlık durumu artık görünüyor** — kayıt diski için gösterilen kullanım çubuğuyla birebir aynı görsel (`✓ RAM (tmpfs) olarak doğrulandı` / `✕ RAM değil`, doluluk yüzdesi ile bir kullanım çubuğu). Bu, onay kutusu kapalıyken bile her zaman gösteriliyor — açmadan önce `/tmp`'in gerçekten uygun olup olmadığını kontrol edebilirsiniz.
- Fonksiyonel testlerle doğrulandı: bol boş alan olsa bile doğrulanamayan tmpfs'te staging'in etkinleşmediği, doğrulanan tmpfs'te normal çalıştığı, ve `/api/recording/status`'un yeni `tmpfs` alanını doğru döndürdüğü.

## [4.3.2] - 2026-08-20

### Düzeltildi
- **`systemctl stop`/`restart rtcview` artık kayıt oturumlarını düzgün kapatıyor.** `atexit` ile kayıtlı kapanış kancası (`recorder.stop()` — her kameranın açık segmentini diske yazıp kapatır) ham bir `SIGTERM` sinyalinde (systemd'nin varsayılan durdurma sinyali) hiç çalışmıyordu — Python yalnızca `SIGINT`'i otomatik olarak `KeyboardInterrupt`'a çeviriyor, işlenmemiş bir `SIGTERM` süreci `atexit` çalışmadan doğrudan sonlandırıyordu. Bu, 4.3.1'deki tmpfs staging özelliğiyle birlikte gerçek bir veri kaybı riskine dönüşüyordu: `rtcview.service`'in `PrivateTmp=yes` ayarı, servis her durduğunda özel `/tmp`'ini siliyor — yani henüz diske taşınmamış bir segment "oynatılamaz" durumda kalmakla kalmıyor, tamamen kayboluyordu. Artık `SIGTERM`/`SIGINT` yakalanıp normal bir kapanışa (`SystemExit`) çevriliyor, böylece mevcut `atexit` kancası her zamanki gibi çalışıp her kameranın son segmentini (tmpfs'te bekliyorsa dahil) diske aktarıyor. Gerçek bir alt süreç üzerinden `SIGTERM` gönderilerek doğrulandı: kapanış kancası artık güvenilir şekilde çalışıyor.
  - Bu, 4.3.1'in CHANGELOG kaydındaki "servis yeniden başladığında dosyalar otomatik kurtarılıyor" ifadesini netleştiriyor: aynı süreç içi kamera yeniden başlatmaları (ayar değişikliği, bellek sınırı, gün dönümü) için bu zaten doğruydu (`/tmp` o sırada silinmiyor) — eksik olan, `systemctl restart`/`stop` gibi servisin tamamen durduğu durumlarda temiz bir kapanışın hiç tetiklenmemesiydi; bu sürüm o boşluğu kapatıyor.

### Eklendi
- **tmpfs staging'in RAM sınırları artık ayarlanabilir.** Önceki sabit 256MB (gerekli boş RAM) / 512MB (kamera başına RAM sınırı) değerleri artık Ayarlar → Kayıt & Depolama'da, ilgili onay kutusu işaretlendiğinde görünen iki alan üzerinden değiştirilebiliyor (`tmpfs_safety_margin_mb`, `tmpfs_hard_cap_mb`). Uzun segment süresi veya yüksek bitrate'li kameralar için varsayılanlar yetersiz kalabileceğinden (ya da tam tersi, daha sıkı bir sınır isteniyorsa) artık kod değişikliği gerekmiyor.

## [4.3.1] - 2026-08-20

### Eklendi
- **İsteğe bağlı: segmentleri önce /tmp'de (RAM) oluştur, kapanınca kayıt diskine taşı.** Ayarlar → Kayıt & Depolama'da yeni bir onay kutusu (`tmpfs_staging`, varsayılan kapalı). Açıldığında ffmpeg her segmenti doğrudan `storage_path`'e (tipik olarak SD kart/eMMC) yazmak yerine önce `/tmp/rtcview-stage/<kamera>` altına yazıyor; segment kapandığında (veya kayıt durdurulduğunda) dosya tek seferde gerçek kayıt diskine taşınıyor. Bu, kaydın tamamı boyunca sürekli küçük tamponlanmış yazma yerine segment başına tek bir toplu yazmaya dönüştürüyor — hem disk yıpranmasını azaltıyor hem de ffmpeg'in yavaş/takılan bir diski beklerken segmenti bozmasını (moov/trailer eksik kalması) daha az olası kılıyor. Yalnızca `/tmp` gerçekten bir tmpfs (RAM disk) olarak bağlıysa fayda sağlıyor; normal bir dizinse (aynı diskin parçasıysa) davranış değişmiyor.
  - RAM güvenliği için iki bağımsız korunak var: (1) tmpfs'te en az 256MB boş alan yoksa bir kameranın oturumu bu özelliği hiç etkinleştirmiyor, sessizce doğrudan diske yazmaya devam ediyor; (2) bir kameranın taşınmamış (RAM'de bekleyen) verisi 512MB'ı aşarsa — ki bu normalde ancak kayıt diski yetişemiyorsa olur — o kamera otomatik olarak yeniden başlatılıp kalıcı olarak doğrudan-diske-yazma moduna dönüyor. Uygulama/kayıtçı yeniden başlatıldığında veya bu özellik daha sonra kapatıldığında, `/tmp`'te kalmış olabilecek herhangi bir dosya bir sonraki başlangıçta otomatik olarak kurtarılıp kayıt diskine taşınıyor — hiçbir segment RAM'de kaybolmuyor.
  - Kapsamlı fonksiyonel testlerle doğrulandı: normal taşıma/kaydetme akışı, yetersiz RAM'de sessiz geri düşüş, önceki oturumdan kalan dosyaların kurtarılması, ve taşma sınırının kalıcı geri düşüşü doğru tetiklediği.

## [4.2.11] - 2026-08-19

### Değişti
- **"Diski yeniden tara" artık arka planda çalışıyor ve gerçek ilerleme gösteriyor.** Önceki hâli, taranan her dosya için ayrı bir SQL sorgusu (var mı yok mu kontrolü) ve yeni bulunan her dosya için ayrı bir INSERT işlemi yapıyordu — büyük bir arşivde (on binlerce segment) bu, gerçek disk I/O'suna göre gereksiz derecede yavaştı ve tüm bu süre boyunca isteği tek bir HTTP çağrısı içinde (frontend'in artık 8 saniyelik zaman aşımına tabi `fetch` çağrısıyla) bekletiyordu — büyük bir taramada buton ya hiçbir şey yapmıyormuş gibi görünüyor ya da tarama arka planda devam ederken sahte bir "başarısız" hatası veriyordu. Artık: (1) mevcut dosya yolları tek bir sorguyla belleğe alınıp dosya başına sorgu yerine bellek içi karşılaştırma yapılıyor, (2) yeni kayıtlar tek tek değil, toplu (`executemany`, tek transaction) olarak yazılıyor, (3) tüm tarama arka plan iş parçacığında çalışıyor ve `/api/recording/rescan/status` üzerinden anlık ilerleme (sayılan/toplam dosya, faz: sayılıyor/taranıyor/temizleniyor) döndürüyor. Ayarlar → Kayıt & Depolama'da buton artık gerçek yüzdesel bir ilerleme çubuğu, canlı "X/Y dosya" sayacı ve net bir tamamlandı/başarısız durumu gösteriyor.

## [4.2.10] - 2026-08-19

### Düzeltildi
- **Sayfa yüklemesinin ara sıra 15-20 saniye sürmesine yol açan iki gerçek kilit/kaynak sorunu giderildi.** `RecordingManager`, kayıt döngüsünü (`_tick()`) ve manuel/yeniden-yükleme isteklerini (`reload_camera()`, `manual_start()`) yöneten kilidi (`self._lock`), `CameraRecorder.stop()`'un ffmpeg'i SIGTERM→SIGINT→SIGKILL ile kapatırken alabileceği ~7 saniyeye kadar süre boyunca elinde tutuyordu — bu sürede `/api/recording/status`'u (her sayfa yüklemesinde çağrılır) dahil aynı kilidi bekleyen her şey donuyordu. Kilidi `stop()` gibi yavaş işlemler boyunca tutmak yerine, artık bir "meşgul kümesi" (`self._busy`) kilidin altında hızlıca ayrılıp asıl yavaş `stop()` çağrısı kilidin DIŞINDA yapılıyor — aynı kamera için eşzamanlı bir yeniden başlatma/durdurma isteğini önceki (görev #70'te düzeltilen) çifte-kayıt/yetim kayıt yarış durumuna düşürmeden. Kapsamlı eşzamanlılık testleriyle doğrulandı (kontrollü yarış senaryosu + 6 iş parçacıklı, 6 saniyelik sürekli çekişme stres testi): `status()` çağrıları artık en kötü durumda bile milisaniyeler içinde dönüyor, sıfır ihlal.
- `storage.py`'deki periyodik temizlik turu (`purge_once()`, varsayılan her 60 saniyede bir), o turda hiçbir segment silinmemiş olsa bile her seferinde `PRAGMA incremental_vacuum` çalıştırıyordu — gerçek disk G/Ç'si gerektiren bu işlem, neredeyse her Storage okumasının (segment listesi, istatistikler, sağlık durumu, bildirimler) paylaştığı aynı kilidin altında çalışıyordu. Artık yalnızca o turda gerçekten segment silinmişse çalışıyor; 1 saniyeden uzun sürerse uyarı logu ekleniyor.
- Frontend `api` yardımcısına (`app.js`) her istek için 8 saniyelik bir üst sınır (`AbortSignal.timeout`) eklendi — daha önce `fetch()` hiçbir genel zaman aşımına sahip değildi, tek bir askıda kalan istek `init()`'i süresiz kilitleyebiliyor, kendi yeniden deneme mantığına hiç girmiyordu.
- `startPlayer()`'ın MSE dalı, `_startMSE(...)`'i beklemeden (fire-and-forget) dönüyordu — bu, `_drainStartQueue()`'nun eşzamanlı oynatıcı başlatma sınırlamasını (`_MAX_CONCURRENT_STARTS`) MSE modundaki cihazlar için etkisiz kılıyordu. Artık düzgün şekilde `await` ediliyor.

## [4.2.9] - 2026-08-17

### Düzeltildi
- **4.3.0'daki go2rtc WebSocket-röle mimarisi geri alındı — üretimde regresyona yol açtı.** Bazı kameralarda WebRTC bağlantısı sürekli "ws closed" ile kopuyordu. Sandbox testlerinde iki gerçek tarayıcı arasındaki tam WebRTC el sıkışması da güvenilir tamamlanamamıştı (o zaman "ortama özgü" diye değerlendirilmişti) — üretimdeki tekrarlayan kopmalar bunun gerçek, mimariye özgü bir kararlılık sorunu olduğunu doğruladı, sandbox'a özgü bir kısıtlama değil. 4.2.8'deki durum (HTTP tabanlı proxy: MSE için düz `GET .../stream.mp4`, WebRTC için düzeltilmiş tek seferlik WHEP — go2rtc'nin kendi `stream.html`'inin doğrudan MSE bağlantısıyla zaten aynı temel yaklaşım) geri getirildi: `app/__init__.py` boşaltıldı (gevent monkey-patch kaldırıldı), `app/main.py` `waitress.serve()`'e döndü, `go2rtc_ws_relay()` ve ilişkili WebSocket oynatıcı kaldırıldı, `requirements.txt`'ten `gevent`/`Flask-Sock` çıkarıldı `waitress` geri eklendi, `scripts/install.sh`'ın bağımlılık kontrolü eski haline döndü.

## [4.2.8] - 2026-08-17

### Düzeltildi
- **WebRTC (RTC) canlı izleme bağlantısı, go2rtc'nin kendi arayüzü kadar hızlı/güvenilir bağlanacak şekilde düzeltildi.** `_startWHEP()`, ICE candidate toplanmasını hiç beklemeden `pc.createOffer()`'ın statik (henüz hiçbir candidate içermeyen) SDP'sini gönderiyordu — go2rtc'nin `/api/webrtc` ucu tek seferlik (trickle olmayan) bir alışveriş olduğundan, bu candidate'sız teklif go2rtc'ye tarayıcıya nasıl ulaşacağını hiç söylemiyor, bağlantı ancak go2rtc'nin şans eseri aldığı bir STUN kontrolünü "peer-reflexive" olarak fark etmesiyle kuruluyordu — yavaş ve güvenilirliği düşük. Artık `setLocalDescription()` sonrası ICE toplanması bitene (veya 800ms güvenlik sınırına) kadar kısa bir süre beklenip, candidate'ları içeren `pc.localDescription.sdp` gönderiliyor — bu, go2rtc'nin kendi web arayüzünün de (farklı bir yoldan, WebSocket üzerinden trickle ICE ile) elde ettiği aynı "tam adres bilgisiyle bağlan" sonucunu, mimari değişikliği gerektirmeden sağlıyor. Playwright ile doğrulandı: eski kod 0 candidate ile gönderiyordu (`iceGatheringState: "new"`), yeni kod 4 candidate ile gönderiyor (`"complete"`) ve bu sadece ~120ms ek süre — algılanabilir bir gecikme yaratmıyor.
- `go2rtc_proxy()` artık her istekte yeni bir `requests` oturumu (ve dolayısıyla go2rtc'ye yeni bir TCP bağlantısı) açmıyor; kalıcı, paylaşılan bir `requests.Session` (keep-alive havuzu, `threads=64`'e uygun `pool_maxsize=64`) kullanıyor. Gerçek `waitress.serve()` üzerinden doğrulandı: 20 ardışık çağrı artık go2rtc'ye tek bir TCP bağlantısı üzerinden gidiyor (öncesinde 20 ayrı bağlantı), akış (stream.mp4) ve POST gövdesi (WHEP SDP) iletimi bozulmadan çalışmaya devam ediyor, 40 eşzamanlı çağrı da sorunsuz.

## [4.2.7] - 2026-08-17

### Düzeltildi
- **4.2.6'daki `channel_request_lookahead=1` değişikliği geri alındı — üretimde regresyona yol açtı.** Güncellemeden kısa süre sonra canlı izleme "Bağlanıyor" durumunda takılıp MSE zaman aşımına uğradı, sayfa açılması uzadı ("dün çok güzel çalışıyordu"). Bu ayar waitress'in tam olarak aynı mekanizmasını etkinleştiriyor (`channel_request_lookahead`) ve bu mekanizmanın geçmişte ciddi bir yarış durumu hatası vardı (CVE-2024-49768, waitress 3.0.1'de düzeltildi) — kurulu sürüm (3.0.2) bu CVE'ye karşı yamalı olsa da, bu oturumda yapılan doğrulama testi sadece kısa süreli (2 sn) bir senaryoyu kapsıyordu, üretimdeki uzun ömürlü MSE bağlantıları + kesintili RTSP + mobil ağ kopmaları kombinasyonunu değil. Riski üstlenmek yerine geri alındı: `go2rtc_proxy()` artık tekrar sadece yazma-hatası tetiklemeli bağlantı kopma tespiti kullanıyor (4.2.4/4.2.5'teki, "dün çalışan" davranış), `waitress.serve()` çağrısı `threads=64` ile (starvation düzeltmesi duruyor) ama `channel_request_lookahead` olmadan çalışıyor.

## [4.2.6] - 2026-08-17

### Değişti
- go2rtc proxy'sindeki (MSE `stream.mp4` vb.) ölü istemci tespiti artık sadece "bir sonraki yazma başarısız olunca fark et" yöntemine dayanmıyor — bu, temiz bağlantı kesmelerinde hızlı çalışsa da, "sessiz" ağ kopmalarında (kernel gönderim tamponları yazmaları bir süre yutabiliyor, OS'nin TCP zaman aşımını fark etmesi saniyeler-dakikalar sürebiliyor) gecikebiliyordu. Artık waitress'in kendi `channel_request_lookahead` mekanizması etkinleştirildi (`channel_request_lookahead=1`); waitress'in bağlantıyı izleyen kendi I/O thread'i istemcinin soketi kapattığını, o an istemciye yazan/bekleyen worker thread'den bağımsız olarak anında görüyor ve bunu isteğin ortamına (`waitress.client_disconnected`) canlı bir kontrol olarak koyuyor. Proxy artık her upstream chunk'ında bu kontrolü yapıp gitmiş istemciyi bir yazma denemesi beklemeden fark ediyor ve upstream go2rtc bağlantısını hemen kapatıyor — gerçek `waitress.serve()` üzerinden, ham soket ile ani bağlantı kesmesi simüle edilerek doğrulandı (upstream bağlantı sayısı, yazma hatası beklenmeden bir chunk aralığı içinde 0'a düşüyor).

## [4.2.5] - 2026-08-17

### Değişti
- Sistem → Loglar'a go2rtc'nin kayıtları geri eklendi (4.2.4'te çıkarılmıştı) — RtcView, go2rtc ve güncelleme/yeniden başlatma tetikleyici birimlerinin hepsi yine tek, zaman sıralı birleşik akış olarak görüntüleniyor. Menüdeki açıklama metni sadeleştirildi ("Servis kayıtlarını görüntüle") — hangi servislerin dahil olduğunu tek tek saymıyor.

## [4.2.4] - 2026-08-17

### Düzeltildi
- **Uygulamanın kendi kendine kilitlenmesine yol açan gerçek bir hata bulundu ve düzeltildi.** `go2rtc_proxy()`'deki MSE (`stream.mp4`) akışları sınırsız okuma zaman aşımıyla çalışıyor — bir canlı izleme sekmesi açık kaldığı sürece o bağlantı bir waitress worker thread'ini süresiz meşgul ediyordu. Sabit `threads=10` havuzuyla, birkaç MSE sekmesi (birden fazla cihazda açık kalan sekmeler, arka plandan dönüşte üst üste binen yeniden bağlanmalar) havuzu tüketince go2rtc ayarları, config editörü, log görüntüleyici, durum yoklaması dahil **her şey** boş thread bekleyerek asılı kalıyordu — "uygulama kendi kendine kitleniyor" hissi buradan geliyordu. 10 eşzamanlı MSE sekmesiyle gerçek `waitress.serve()` üzerinden yeniden üretildi (`/api/status` tamamen zaman aşımına uğradı) ve düzeltmeyle (thread havuzu 64'e çıkarıldı) aynı senaryo, hatta 40 eşzamanlı sekmeyle bile doğrulandı. Eski `threads=10` seçimi CPU context-switch endişesiyle yapılmıştı — ama bu thread'ler CPU'da çalışmıyor, neredeyse tamamen socket I/O'da bloke bekliyor, o endişe bu iş yüküne uymuyordu.
- Sistem → Loglar artık go2rtc'nin logunu içermiyor — sadece RtcView'ın kendi servis kaydı **artı** kendi güncelleme/yeniden başlatma/reboot/go2rtc-yeniden-başlatma tetikleyici birimlerinin kayıtları (birleşik, zaman sıralı). "Şimdi Güncelle" ile yapılan bir güncelleme artık buradan görülebiliyor — önceden `rtcview-updater.service`'in çıktısı hiçbir log panelinde görünmüyordu.

## [4.2.3] - 2026-08-17

### Değişti
- go2rtc'nin logları artık ayrı bir panelde değil, Ayarlar → Sistem → Loglar'daki mevcut RtcView log görüntüleyicisiyle **birleşik** gösteriliyor (`journalctl -u rtcview.service -u go2rtc.service`, zaman sırasına göre iç içe geçmiş tek akış) — iki ayrı log listesi yerine tek yer. Ayarlar → go2rtc sekmesindeki ayrı log paneli (ve buna özel `GET /api/go2rtc/logs` ucu) kaldırıldı; go2rtc sekmesinde bağlantı ayarları ve `go2rtc.yaml` editörü aynı şekilde duruyor.

## [4.2.2] - 2026-08-17

### Eklendi
- Ayarlar → go2rtc → Loglar panelinde "Panoya kopyala" düğmesi eklendi — Sistem sekmesindeki RtcView log görüntüleyicisiyle aynı davranış.

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

[Unreleased]: https://github.com/ibrahimdoqn/RtcView/compare/v4.2.5...HEAD
[4.2.5]: https://github.com/ibrahimdoqn/RtcView/compare/v4.2.4...v4.2.5
[4.2.4]: https://github.com/ibrahimdoqn/RtcView/compare/v4.2.3...v4.2.4
[4.2.3]: https://github.com/ibrahimdoqn/RtcView/compare/v4.2.2...v4.2.3
[4.2.2]: https://github.com/ibrahimdoqn/RtcView/compare/v4.2.1...v4.2.2
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
