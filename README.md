# Proje Tanımı: Bybit Spot Hacim ve Likidite Duvarı Radarı

Bu proje, sadece bir "fiyat alarmı" değildir. Asıl amacı, **Piyasa Yapıcı (Market Maker) ve Absorpsiyon (Iceberg) aktivitesini** tespit ederek, fiyatı kaydırmadan (slippage) ve makas yemeden güvenli **hacim kasma (volume farming)** bölgelerini bulmaktır.

## Amaç

Bybit Spot borsasında, belirli bir takip listesindeki coinleri saniyeler içinde tarayarak; **hacmin aşırı arttığı ancak fiyatın sabit kaldığı** (Absorpsiyon/Iceberg Order) anları tespit eder.

## 1. Teknik Gereksinimler ve Altyapı

- **Dil:** Python 3.10+
- **Kütüphaneler:** ccxt (Borsa bağlantısı), pyTelegramBotAPI (Arayüz), statistics (Matematiksel analiz)
  - Not: Kaynak tüketimini azaltmak için **pandas kullanılmamalıdır**.
- **Çalışma Modu:** Multi-threaded (Bir thread Bybit'i tararken, diğeri Telegram komutlarını dinler).
- **Dayanıklılık:** Bağlantı kopmalarına (ConnectionError, RemoteDisconnected) karşı otomatik yeniden bağlanma (reconnect) mekanizması olmalıdır.

## 2. Strateji ve Algoritma Mantığı (Kritik)

Bot, her coin için şu 4 ana filtreyi doğrulamalıdır:

**A. Hacim Patlaması (Volume Spike)**  
Mevcut 1 dakikalık mumun hacmi, geçmiş 30 dakika ile 10 dakika arasındaki "temiz" dönemin **medyan hacminden en az 5 kat (5x)** büyük olmalıdır.  
> Son 10 dakikanın hariç tutulma sebebi, tespit edilen botun kendi hacminin ortalamayı "zehirlemesini" engellemektir.

**B. Fiyat Sabitliği (Body Change)**  
Hacim patlamasına rağmen fiyatın ilerleyemediğini doğrulamak için; 1 dakikalık mumun açılış ve kapanış fiyatı arasındaki fark (gövde) **%0.05 veya daha az** olmalıdır.

**C. Emir Yogunlugu Artisi (Order Count Spike)**  
Tahtanin ilk seviyelerinde, anlamli tutardaki emir sayisi (notional >= belirli esik) **gecmis ortalamaya gore artmis** olmalidir. Bu, fiyat sabitken "suni emir yigmalarini" ayirt etmeye yardim eder.

**D. Tek Tarafli Emir Yigma (Side Imbalance)**  
Piyasa yapici/absorpsiyon davranisi icin, emir yogunlugu ve derinlik artisi **tek bir tarafta** (bid veya ask) belirgin olmalidir. Bu sayede "her iki tarafta eszamanli likidite artisi" gibi alakasiz durumlar elenir.

## 3. Dinamik Derinlik Takibi (Order Book Analysis)

Bot, her taramada tahtanın (Order Book) ilk 5 kademesindeki **USDT derinliğini** kaydetmelidir.

Eğer anlık derinlik, o coinin geçmiş derinlik ortalamasının **3 katı (3x)** üzerine çıkarsa, bu durum bir **"Duvar (Wall)"** olarak etiketlenmelidir.

## 4. Bildirim ve Kullanıcı Arayüzü (Telegram)

- **Komutlar:** `/ekle`, `/sil`, `/liste` komutları ile takip listesi canlı olarak güncellenebilmelidir.
- **Seri Takibi (Streak):** Eğer bir coin üst üste birden fazla mumda aynı sinyali veriyorsa, mesajın başına her mum için bir **"!"** eklenmelidir (Örn: `!! FIRSAT DEVAM EDİYOR !!`).
- **Spam Engelleme:** Aynı coin için başarılı bir tespitten sonra **60 saniye cooldown** (bekleme süresi) uygulanmalıdır.
- **Mum Onayı:** Şartlar yakalandıktan sonra, sinyal **mum kapanışında** onaylanır.

## 5. Özet Analiz Akışı (Pseudo-Code)

1. Bybit'ten ilgili coinin **Order Book** ve **1m mum** verisini çek.  
2. Emir yogunlugu/derinlik artisini ve **tek tarafli yigma** kosullarini kontrol et.  
3. Son 40 mumun verisini çek, hacim ve fiyat gövde değişimini hesapla.  
4. **Hacim > 5x**, **Değişim < %0.05** ve **emir yogunlugu + tek tarafli yigma** sağlanırsa bildirim gönder.  
5. Hata oluşursa (İnternet/API) **10 saniye bekle** ve döngüyü sürdür.
