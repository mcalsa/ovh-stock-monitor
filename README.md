# OVHcloud VPS stock monitor

Bu repo, OVHcloud VPS configurator'ın kullandığı public JSON endpoint'ini beş
dakikada bir GitHub Actions üzerinden kontrol eder. Varsayılan hedef:

- Plan: `VPS Model 2` (`vps-2027-model2`)
- Lokasyon: Europe / Germany / Limburg
- OVH kaydı: `datacenter=DE`, `code=eu-west-lim`
- İşletim sistemi sinyali: Ubuntu/Linux

Configurator client-side çalıştığı için HTML metni parse edilmez. OVH'nin
`/v1/vps/order/rule/datacenter` yanıtındaki `datacenter`, `code`, `status` ve
`linuxStatus` alanları yapısal olarak doğrulanır. Hem `status` hem
`linuxStatus` değeri `available` olduğunda stok var kabul edilir. HTTP/JSON
hatası veya hedef lokasyonun yanıttan kaybolması stok varmış gibi yorumlanmaz;
workflow hata ile sonlanır ve state değiştirilmez.

## Telegram kurulumu

GitHub reposunda **Settings → Secrets and variables → Actions → New repository
secret** yoluyla şu iki secret'ı ekleyin:

- `TELEGRAM_BOT_TOKEN`: BotFather tarafından verilen bot token'ı
- `TELEGRAM_CHAT_ID`: Bildirimin gönderileceği kullanıcı/grup chat ID'si

Token hiçbir repo dosyasına yazılmaz ve loglanmaz. Bildirim şuna benzer:

```text
OVH VPS STOCK AVAILABLE
Location: Germany - Limburg
Plan: VPS Model 2 (vps-2027-model2)
Checked at (UTC): 2026-09-13T09:05:00Z
Configurator: https://www.ovhcloud.com/...
```

## Çalışma ve spam önleme

[`.github/workflows/check-ovh-stock.yml`](.github/workflows/check-ovh-stock.yml)
hem `workflow_dispatch` hem `*/5 * * * *` cron tetikleyicisine sahiptir. Beş
dakika GitHub Actions cron'un desteklediği minimum aralıktır; yoğunlukta zamanlı
çalışmalar gecikebilir.

Son bilinen ikili stok durumu GitHub Actions cache içinde tutulur. Cache yalnızca
ilk başarılı gözlemde veya gerçek bir durum değişiminde yeniden kaydedilir;
repoya otomatik commit atılmaz. `concurrency` aynı anda iki kontrolün state ile
yarışmasını engeller.

İlk çalıştırma mevcut durumu başlangıç state'i olarak kaydeder ve stok o anda
açık olsa bile bildirim göndermez. Sonraki `out of stock → available` geçişi bir
kez bildirilir. Stok tekrar biterse state güncellenir ve sonraki açılış yeniden
bildirilebilir. Telegram gönderimi başarısız olursa `available` state'i
kaydedilmez; sonraki çalıştırma bildirimi tekrar dener.

Manuel kontrol için GitHub'da **Actions → Check OVH VPS stock → Run workflow**
yolunu kullanın. Çalışma logunda ham `status`, `linuxStatus` ve hesaplanan
`available` değeri görünür. İlk manuel çalıştırmanın yalnızca state oluşturması
normaldir.

## Takip edilen lokasyonu değiştirme

Workflow dosyasındaki job `env` değerlerini değiştirin:

- `OVH_PLAN_CODE`: Public endpoint'e gönderilen gerçek plan kodu
- `OVH_PLAN_LABEL`: Telegram'da gösterilecek ad
- `OVH_OS`: Kontrol edilen OS (Linux sinyali ayrıca doğrulanır)
- `OVH_DATACENTER` ve `OVH_LOCATION_CODE`: API kaydının iki kimliği
- `OVH_LOCATION_LABEL`: Telegram'da gösterilecek lokasyon
- `OVH_SUBSIDIARY`: OVH satış bölgesi (`WE` varsayılandır)
- `OVH_API_URL`: Subsidiary'nin API hostu
- `OVH_CONFIGURATOR_URL`: Bildirimde açılacak satın alma bağlantısı

`Germany - Limburg` configurator içinde ana `vps-2027-model2` planının
`eu-west-lim` kaydıdır. Verilen configurator bağlantısı `.LZ` ile başlasa da
sayfa hem ana planı hem Local Zone planını ayrı API çağrılarıyla birleştirir;
bu nedenle Limburg için `OVH_PLAN_CODE=vps-2027-model2` kullanılır.

## Yerel test

```bash
python -m pip install --requirement requirements.txt
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
```

Yerel gerçek kontrol secret olmadan çalıştırılabilir; stok geçişi Telegram
gerektirirse eksik secret nedeniyle güvenli şekilde hata verir:

```bash
python scripts/check_ovh_stock.py
```
