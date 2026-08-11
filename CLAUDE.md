# RtcView — geliştirme notları

## Sürümleme

Bu proje [Semantic Versioning](https://semver.org/) kullanır (`MAJOR.MINOR.PATCH`). Mevcut taban: **v1.0.0**.

Bundan sonraki her anlamlı değişiklik grubunda (özellik/düzeltme seti, kullanıcının onayladığı bir iş birimi):
1. `CHANGELOG.md`'ye yeni bir sürüm bölümü eklenir (`## [x.y.z] - YYYY-MM-DD`), `[Unreleased]` içeriği o bölüme taşınır.
2. Sürüm artışı: yeni özellik → **MINOR** artır (`1.1.0`), geriye uyumlu hata düzeltmesi → **PATCH** artır (`1.0.1`), geriye uyumsuz/yıkıcı değişiklik → **MAJOR** artır (`2.0.0`).
3. Mümkünse commit sonrası `git tag -a vX.Y.Z -m "..."` ile etiketlenir ve `git push origin vX.Y.Z` denenir. Bu oturumun git kimlik bilgileri yalnızca tanımlı branch'e push izinlidir — tag push 403 ile reddedilirse (ve GitHub MCP araçlarında tag/release oluşturan bir araç yoksa) kullanıcıya bunu bildir, `CHANGELOG.md` kaydını yine de commit'e dahil et; tag'i kullanıcı isterse kendisi push eder veya GitHub arayüzünden Release olarak oluşturur.
